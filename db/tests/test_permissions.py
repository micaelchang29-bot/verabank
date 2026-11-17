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
