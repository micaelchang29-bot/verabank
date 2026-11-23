"""Roles: risk_svc, sim_loader y BI. Mínimo privilegio sobre risk, core, audit y analytics."""

import uuid

import psycopg
import pytest
from conftest import ADMIN

DENIED = psycopg.errors.InsufficientPrivilege
RISK_TABLES = ["scoring_log", "fraud_labels", "aml_alerts", "transfer_facts", "aml_rules",
               "country_risk", "aml_exempt_accounts", "processed_messages"]  # fmt: skip
APPEND = "audit.append(text,text,text,text,uuid,jsonb)"


def scoring_row(transfer_id):
    return ("INSERT INTO risk.scoring_log (transfer_id, source_ref, dest_ref, amount, occurred_at, features, "
            "score, decision, model_version) VALUES (%s, 'src-01', 'dst-01', 10.00, now(), '{}', 0.5, 'APPROVE', 'm/1')",
            (transfer_id,))  # fmt: skip


class TestRiskSvc:
    def test_cannot_read_core_or_write_audit_directly(self, provisioned_db):
        with provisioned_db.connect("risk_svc") as conn:
            for sql in (
                "SELECT * FROM core.accounts",
                "SELECT * FROM core.transfers",
                "SELECT * FROM audit.audit_log",
                "INSERT INTO audit.audit_log (actor, action, entity_type, entity_id, detail) "
                "VALUES ('x', 'x', 'x', 'x', '{}')",
                "UPDATE audit.audit_log SET actor = 'x'",
                "DELETE FROM audit.audit_log",
            ):
                with pytest.raises(DENIED):
                    conn.execute(sql)

    def test_reads_and_writes_every_risk_table_but_owns_nothing(self, provisioned_db):
        with provisioned_db.connect("risk_svc") as conn:
            for table in RISK_TABLES:
                conn.execute(f"SELECT * FROM risk.{table} LIMIT 1")
            transfer_id = uuid.uuid4()
            conn.execute(*scoring_row(transfer_id))
            conn.execute("UPDATE risk.scoring_log SET score = 0.6 WHERE transfer_id = %s", (transfer_id,))
            conn.execute("DELETE FROM risk.scoring_log WHERE transfer_id = %s", (transfer_id,))
            with pytest.raises(DENIED):
                conn.execute("CREATE TABLE risk.extra (n INT)")
            with pytest.raises(DENIED):
                conn.execute("CREATE TABLE public.extra (n INT)")
            with pytest.raises(DENIED):
                conn.execute("DROP TABLE risk.scoring_log")

    def test_cannot_use_the_other_schemas(self, provisioned_db):
        with provisioned_db.connect("risk_svc") as conn:
            with pytest.raises(DENIED):
                conn.execute("SELECT * FROM analytics.probe")

    def test_future_risk_tables_are_covered_by_default_privileges(self, provisioned_db):
        with provisioned_db.connect("verabank_risk") as owner:
            owner.execute("CREATE TABLE risk.future_probe (n INT)")
        try:
            with provisioned_db.connect("risk_svc") as conn:
                conn.execute("INSERT INTO risk.future_probe VALUES (1)")
            with provisioned_db.connect("verabank_bi") as bi:
                assert bi.execute("SELECT n FROM risk.future_probe").fetchone() == (1,)
            with provisioned_db.connect("sim_loader") as loader, pytest.raises(DENIED):
                loader.execute("INSERT INTO risk.future_probe VALUES (2)")
        finally:
            with provisioned_db.connect("verabank_risk") as owner:
                owner.execute("DROP TABLE risk.future_probe")


class TestSimLoader:
    def test_only_inserts_into_scoring_log(self, provisioned_db):
        transfer_id = uuid.uuid4()
        with provisioned_db.connect("sim_loader") as conn:
            conn.execute(*scoring_row(transfer_id))
            with pytest.raises(DENIED):
                conn.execute("UPDATE risk.scoring_log SET score = 0.9 WHERE transfer_id = %s", (transfer_id,))
            with pytest.raises(DENIED):
                conn.execute("DELETE FROM risk.scoring_log")
            with pytest.raises(DENIED):
                conn.execute("SELECT * FROM risk.scoring_log")
        with provisioned_db.connect("risk_svc") as conn:
            assert conn.execute("SELECT score FROM risk.scoring_log WHERE transfer_id = %s", (transfer_id,)).fetchone()[0] == 0.5
            conn.execute("DELETE FROM risk.scoring_log WHERE transfer_id = %s", (transfer_id,))

    def test_cannot_read_or_write_anything_else(self, provisioned_db):
        with provisioned_db.connect("sim_loader") as conn:
            for table in RISK_TABLES[1:]:
                with pytest.raises(DENIED):
                    conn.execute(f"SELECT * FROM risk.{table}")
                with pytest.raises(DENIED):
                    conn.execute(f"DELETE FROM risk.{table}")
            for sql in ("SELECT * FROM core.accounts", "SELECT * FROM audit.audit_log",
                        "SELECT * FROM analytics.probe", f"SELECT audit.append('a','b','c','d',NULL,'{{}}')"):  # fmt: skip
                with pytest.raises(DENIED):
                    conn.execute(sql)

    def test_has_no_table_privilege_besides_that_insert(self, provisioned_db):
        with provisioned_db.connect(ADMIN) as conn:
            grants = conn.execute(
                """SELECT table_schema || '.' || table_name, privilege_type FROM information_schema.role_table_grants
                   WHERE grantee = 'sim_loader'""").fetchall()
        assert grants == [("risk.scoring_log", "INSERT")]


def test_sim_loader_can_retry_a_warmup_insert_with_on_conflict_do_nothing(provisioned_db):
    """El reintento del calentamiento del replay: duplicados ignorados sin SELECT (un destino de conflicto lo exige)."""
    transfer_id = uuid.uuid4()
    with provisioned_db.connect("sim_loader") as conn:
        conn.execute(*scoring_row(transfer_id))
        sql, params = scoring_row(transfer_id)
        assert conn.execute(sql + " ON CONFLICT DO NOTHING", params).rowcount == 0
        with pytest.raises(DENIED):
            conn.execute(sql + " ON CONFLICT (transfer_id) DO NOTHING", params)


class TestStatementTimeout:
    """Respaldo del lado del servidor del límite de 100 ms del cliente (ajuste heredado de 4.C)."""

    def test_risk_svc_has_a_250ms_statement_timeout_and_it_is_enforced(self, provisioned_db):
        with provisioned_db.connect("risk_svc") as conn:
            assert conn.execute("SHOW statement_timeout").fetchone() == ("250ms",)
            with pytest.raises(psycopg.errors.QueryCanceled):
                conn.execute("SELECT pg_sleep(1)")

    def test_the_other_roles_have_no_statement_timeout(self, provisioned_db):
        for role in ("sim_loader", "verabank_bi"):
            with provisioned_db.connect(role) as conn:
                assert conn.execute("SHOW statement_timeout").fetchone() == ("0",)

    def test_granting_twice_keeps_the_setting(self, provisioned_db):
        provisioned_db.script("db-grants.sh")
        with provisioned_db.connect("risk_svc") as conn:
            assert conn.execute("SHOW statement_timeout").fetchone() == ("250ms",)


class TestBi:
    def test_reads_risk_and_analytics_but_writes_nothing(self, provisioned_db):
        with provisioned_db.connect("verabank_bi") as conn:
            for table in RISK_TABLES:
                conn.execute(f"SELECT * FROM risk.{table} LIMIT 1")
            assert conn.execute("SELECT n FROM analytics.probe").fetchone() == (1,)
            with pytest.raises(DENIED):
                conn.execute("INSERT INTO risk.aml_exempt_accounts (account_id, reason) VALUES (gen_random_uuid(), 'x')")
            with pytest.raises(DENIED):
                conn.execute("INSERT INTO analytics.probe VALUES (2)")
            with pytest.raises(DENIED):
                conn.execute("SELECT * FROM core.accounts")


def test_only_risk_svc_can_execute_audit_append(provisioned_db):
    with provisioned_db.connect(ADMIN) as conn:
        who = {
            role: conn.execute(f"SELECT has_function_privilege(%s, '{APPEND}', 'EXECUTE')", (role,)).fetchone()[0]
            for role in ("risk_svc", "sim_loader", "verabank_bi", "verabank_risk", "public")
        }
    assert who == {"risk_svc": True, "sim_loader": False, "verabank_bi": False, "verabank_risk": False, "public": False}


class TestAuditOwner:
    """T-16 (HU-07): transfer-service escribe la auditoría, pero no es dueño de nada en audit (db/init/grants.sql).

    Como dueño podía vaciar audit_log, desactivar trg_audit_immutable o redefinir el trigger de la cadena, y
    verify_chain no lo ve: una tabla vacía no tiene nada que verificar. Cada intento va en una transacción que se
    revierte, por si alguno pasara.
    """

    TAMPERING = [
        "TRUNCATE audit.audit_log",
        "ALTER TABLE audit.audit_log DISABLE TRIGGER trg_audit_immutable",
        "ALTER TABLE audit.audit_log DISABLE TRIGGER trg_audit_chain",
        "DROP TRIGGER trg_audit_immutable ON audit.audit_log",
        "DROP TABLE audit.audit_log",
        "ALTER TABLE audit.audit_log OWNER TO verabank_transfer",
        "CREATE OR REPLACE FUNCTION audit.chain_hash() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RETURN NEW; END $$",
        "CREATE OR REPLACE FUNCTION audit.deny_change() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RETURN NEW; END $$",
        f"ALTER FUNCTION {APPEND} SECURITY INVOKER",
        "DROP SCHEMA audit CASCADE",
        "CREATE TABLE audit.extra (n INT)",
    ]

    @pytest.mark.parametrize("sql", TAMPERING)
    def test_transfer_service_cannot_empty_or_disarm_the_log(self, provisioned_db, sql):
        with provisioned_db.connect("verabank_transfer") as conn:
            with pytest.raises(DENIED):
                with conn.transaction(force_rollback=True):
                    conn.execute(sql)

    def test_transfer_service_still_writes_and_the_trigger_still_answers(self, provisioned_db):
        with provisioned_db.connect("verabank_transfer") as conn:
            # Una fila que se queda: sin filas, los triggers de fila no llegan a dispararse.
            conn.execute("INSERT INTO audit.audit_log (actor, action, entity_type, entity_id, detail) "
                         "VALUES ('transfer-service', 'X', 'TRANSFER', 't-owner', '{}')")  # fmt: skip
            for sql in ("UPDATE audit.audit_log SET actor = 'x'", "DELETE FROM audit.audit_log"):
                with pytest.raises(psycopg.errors.RaiseException, match="solo inserción"):
                    conn.execute(sql)

    def test_even_a_superuser_truncate_is_rejected_by_a_trigger(self, provisioned_db):
        with provisioned_db.connect(ADMIN) as conn:
            with pytest.raises(psycopg.errors.RaiseException, match="solo inserción"):
                with conn.transaction(force_rollback=True):
                    conn.execute("TRUNCATE audit.audit_log")

    def test_audit_belongs_to_a_role_nobody_logs_in_with(self, provisioned_db):
        with provisioned_db.connect(ADMIN) as conn:
            owners = conn.execute(
                """SELECT 'schema', nspowner::regrole::text FROM pg_namespace WHERE nspname = 'audit'
                   UNION ALL SELECT relname, relowner::regrole::text FROM pg_class
                    WHERE relnamespace = 'audit'::regnamespace AND relkind = 'r'
                   UNION ALL SELECT proname, proowner::regrole::text FROM pg_proc
                    WHERE pronamespace = 'audit'::regnamespace AND proname <> 'verify_chain'""").fetchall()
            assert set(owners) == {("schema", "verabank_audit"), ("audit_log", "verabank_audit"),
                                   ("chain_hash", "verabank_audit"), ("deny_change", "verabank_audit"),
                                   ("append", "verabank_audit")}  # fmt: skip
            assert conn.execute("SELECT rolcanlogin FROM pg_roles WHERE rolname = 'verabank_audit'").fetchone() == (False,)

    def test_granting_twice_changes_nothing_in_audit(self, provisioned_db):
        acl = """SELECT relname, relowner::regrole::text, relacl::text FROM pg_class
                 WHERE relnamespace = 'audit'::regnamespace ORDER BY 1"""
        with provisioned_db.connect(ADMIN) as conn:
            before = conn.execute(acl).fetchall()
        provisioned_db.script("db-grants.sh")
        with provisioned_db.connect(ADMIN) as conn:
            assert conn.execute(acl).fetchall() == before
