"""Migraciones: desde cero, dos veces seguidas, y sin depender de roles que solo existen con risk."""

import psycopg
import pytest
from conftest import ADMIN, risk_signature

RISK_TABLES = {
    "scoring_log", "fraud_labels", "aml_alerts",  # fase 1 (V2–V4): no se modifican
    "transfer_facts", "aml_rules", "country_risk", "aml_exempt_accounts", "processed_messages",
}  # fmt: skip


def versions(conn, history: str) -> list[str]:
    rows = conn.execute(
        f"SELECT version FROM {history}.flyway_schema_history WHERE version IS NOT NULL AND success "
        "ORDER BY installed_rank"
    ).fetchall()
    return [r[0] for r in rows]


def test_transfer_service_migrations_apply_in_a_database_without_risk_svc(stage1_db):
    """V13 (audit.append) no puede depender de risk_svc: transfer-service y sus pruebas no lo tienen."""
    with stage1_db.connect(ADMIN) as conn:
        assert conn.execute(
            "SELECT count(*) FROM pg_roles WHERE rolname IN ('risk_svc', 'sim_loader')"
        ).fetchone() == (0,)
        assert versions(conn, "flyway_transfer") == [str(n) for n in range(1, 14)]
        assert conn.execute(
            "SELECT has_function_privilege('public', 'audit.append(text,text,text,text,uuid,jsonb)', 'EXECUTE')"
        ).fetchone() == (False,)


def test_risk_migrations_apply_from_scratch_and_twice(provisioned_db):
    db = provisioned_db
    with db.connect(ADMIN) as conn:
        assert versions(conn, "flyway_risk") == [str(n) for n in range(1, 11)]
        tables = {r[0] for r in conn.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'risk'")}
        first = risk_signature(conn)
        history = conn.execute("SELECT installed_rank, version, checksum FROM flyway_risk.flyway_schema_history ORDER BY 1").fetchall()
        rules = conn.execute("SELECT * FROM risk.aml_rules ORDER BY rule_code, version").fetchall()
    assert tables == RISK_TABLES

    again = db.script("db-migrate-risk.sh")
    assert "up to date" in again.stdout.lower() or "no migration necessary" in again.stdout.lower(), again.stdout
    db.script("db-grants.sh")  # los permisos también se repiten sin cambios
    db.script("db-roles.sh")
    with db.connect(ADMIN) as conn:
        assert risk_signature(conn) == first
        assert conn.execute("SELECT installed_rank, version, checksum FROM flyway_risk.flyway_schema_history ORDER BY 1").fetchall() == history
        assert conn.execute("SELECT * FROM risk.aml_rules ORDER BY rule_code, version").fetchall() == rules


def test_fresh_volume_and_existing_volume_end_up_identical(provisioned_db, fresh_db):
    with provisioned_db.connect(ADMIN) as a, fresh_db.connect(ADMIN) as b:
        assert risk_signature(a) == risk_signature(b)


def test_support_tables_have_the_specified_columns_and_indexes(provisioned_db):
    with provisioned_db.connect(ADMIN) as conn:
        def columns(table):
            return conn.execute(
                """SELECT column_name, data_type, character_maximum_length, numeric_precision, numeric_scale, is_nullable
                   FROM information_schema.columns WHERE table_schema = 'risk' AND table_name = %s
                   ORDER BY ordinal_position""", (table,)).fetchall()
        assert columns("transfer_facts") == [
            ("transfer_id", "uuid", None, None, None, "NO"),
            ("source_account_id", "uuid", None, None, None, "NO"),
            ("dest_ref", "text", None, None, None, "NO"),
            ("dest_country", "character", 2, None, None, "NO"),
            ("transfer_type", "text", None, None, None, "NO"),
            ("amount", "numeric", None, 18, 2, "NO"),
            ("occurred_at", "timestamp with time zone", None, None, None, "NO"),
            ("emitted_at", "timestamp with time zone", None, None, None, "NO"),
            ("completed_at", "timestamp with time zone", None, None, None, "NO"),
        ]  # fmt: skip
        indexes = dict(conn.execute("SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'transfer_facts'").fetchall())
        assert "(source_account_id, occurred_at DESC) INCLUDE (amount)" in indexes["ix_facts_source"]
        assert "(dest_ref, occurred_at DESC) INCLUDE (amount, source_account_id)" in indexes["ix_facts_dest"]
        assert [c[0] for c in columns("aml_rules")] == ["rule_code", "version", "params", "active", "valid_from"]
        assert columns("country_risk") == [
            ("country", "character", 2, None, None, "NO"),
            ("level", "text", None, None, None, "NO"),
            ("source", "text", None, None, None, "NO"),
            ("list_date", "date", None, None, None, "NO"),
            ("valid_from", "date", None, None, None, "NO"),
            ("valid_to", "date", None, None, None, "YES"),
        ]  # fmt: skip
        assert [c[0] for c in columns("aml_exempt_accounts")] == ["account_id", "reason", "created_at"]
        assert columns("processed_messages") == [
            ("consumer", "text", None, None, None, "NO"),
            ("event_id", "uuid", None, None, None, "NO"),
            ("processed_at", "timestamp with time zone", None, None, None, "NO"),
        ]  # fmt: skip
        core = conn.execute(
            """SELECT column_name, data_type, is_nullable FROM information_schema.columns
               WHERE table_schema = 'core' AND table_name = 'processed_messages' ORDER BY ordinal_position""").fetchall()
        mine = conn.execute(
            """SELECT column_name, data_type, is_nullable FROM information_schema.columns
               WHERE table_schema = 'risk' AND table_name = 'processed_messages' ORDER BY ordinal_position""").fetchall()
        assert core == mine  # igual que core.processed_messages
        assert conn.execute("SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname = 'risk'").fetchone() == ("verabank_risk",)
        owned = conn.execute("SELECT count(*) FROM pg_class WHERE relowner IN ('risk_svc'::regrole, 'sim_loader'::regrole)").fetchone()
        assert owned == (0,)  # los roles de ejecución no son dueños de nada


def test_country_risk_rejects_bad_codes_and_levels(provisioned_db):
    with provisioned_db.connect("risk_svc") as conn:
        for country, level in (("ZZZ", "HIGH"), ("pe", "HIGH"), ("PE", "MEDIUM")):
            with pytest.raises((psycopg.errors.CheckViolation, psycopg.errors.StringDataRightTruncation)):
                conn.execute(
                    "INSERT INTO risk.country_risk (country, level, source, list_date, valid_from) "
                    "VALUES (%s, %s, 'x', DATE '2026-01-01', DATE '2026-01-01')", (country, level))
