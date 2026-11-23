"""Fase 5: esquema analytics (vista de §4, drift_metrics, fraud_reasons) y roles ml_job y BI (5.A)."""

import hashlib
import re
import uuid
from datetime import datetime, timedelta, timezone

import psycopg
import pytest
from conftest import ADMIN, ROOT

DENIED = psycopg.errors.InsufficientPrivilege
VERIFY = "audit.verify_chain(bigint)"


def design_view_ddl() -> str:
    """El CREATE VIEW de docs/diseno.md §4, tal cual."""
    text = (ROOT / "docs/diseno.md").read_text()
    match = re.search(r"CREATE VIEW analytics\.v_transfer_outcomes AS.*?;\n", text, re.S)
    assert match, "no se encontró el DDL de la vista en §4"
    return match.group(0)


def test_view_migration_is_the_ddl_of_section_4_verbatim():
    migration = (ROOT / "db/migrations/analytics/V2__v_transfer_outcomes.sql").read_text()
    assert migration == design_view_ddl()


def test_analytics_migrations_apply_from_scratch_and_twice(provisioned_db):
    db = provisioned_db
    with db.connect(ADMIN) as conn:
        history = conn.execute(
            "SELECT version, checksum FROM flyway_analytics.flyway_schema_history WHERE version IS NOT NULL "
            "AND success ORDER BY installed_rank").fetchall()  # fmt: skip
        objects = conn.execute(
            "SELECT relname, relkind FROM pg_class WHERE relnamespace = 'analytics'::regnamespace "
            "AND relkind IN ('r', 'v') AND relname <> 'probe' ORDER BY 1").fetchall()  # fmt: skip
    assert [v for v, _ in history] == ["1", "2", "3", "4", "5", "6"]
    assert objects == [("drift_metrics", "r"), ("fraud_reasons", "r"), ("v_transfer_outcomes", "v")]
    again = db.script("db-migrate-analytics.sh")
    assert "up to date" in again.stdout.lower() or "no migration necessary" in again.stdout.lower(), again.stdout
    with db.connect(ADMIN) as conn:
        assert conn.execute(
            "SELECT version, checksum FROM flyway_analytics.flyway_schema_history WHERE version IS NOT NULL "
            "AND success ORDER BY installed_rank").fetchall() == history  # fmt: skip


def test_fresh_and_existing_volumes_get_the_same_analytics(provisioned_db, fresh_db):
    sig = "SELECT table_name, column_name, data_type FROM information_schema.columns WHERE table_schema = 'analytics' ORDER BY 1, 2"
    with provisioned_db.connect(ADMIN) as a, fresh_db.connect(ADMIN) as b:
        assert a.execute(sig).fetchall() == b.execute(sig).fetchall()


def columns(conn, table):
    return conn.execute(
        """SELECT column_name, data_type, numeric_precision, numeric_scale, column_default IS NOT NULL
           FROM information_schema.columns WHERE table_schema = 'analytics' AND table_name = %s
           ORDER BY ordinal_position""", (table,)).fetchall()  # fmt: skip


def test_drift_metrics_has_the_phase_5_columns_key_and_partial_index(provisioned_db):
    with provisioned_db.connect(ADMIN) as conn:
        assert columns(conn, "drift_metrics") == [
            ("window_start", "timestamp with time zone", None, None, False),
            ("window_end", "timestamp with time zone", None, None, False),
            ("feature", "text", None, None, False),
            ("psi", "numeric", 10, 6, False),
            ("n_current", "integer", 32, 0, False),
            ("n_bins", "smallint", 16, 0, False),
            ("degenerate", "boolean", None, None, False),
            ("insufficient", "boolean", None, None, False),
            ("is_top_feature", "boolean", None, None, False),
            ("alert", "boolean", None, None, True),
            ("model_version", "text", None, None, False),
            ("computed_at", "timestamp with time zone", None, None, True),
            ("reference", "text", None, None, True),
        ]  # fmt: skip
        pk = conn.execute("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid = 'analytics.drift_metrics'::regclass AND contype = 'p'").fetchone()
        assert pk == ("PRIMARY KEY (window_start, feature, reference)",)
        index = conn.execute("SELECT indexdef FROM pg_indexes WHERE schemaname = 'analytics' AND indexname = 'ix_drift_alert'").fetchone()[0]
        assert "(window_start DESC) WHERE alert" in index


INSERT_METRIC = """INSERT INTO analytics.drift_metrics (window_start, window_end, feature, psi, n_current, n_bins, degenerate,
                     insufficient, is_top_feature, model_version) VALUES ('2026-01-01', '2026-01-02', %s, 0.1, 300, 10,
                     false, false, true, 'm/1')"""


def test_v6_keeps_existing_rows_as_train_and_lets_both_references_coexist(provisioned_db):
    """V6 se aplica sobre una tabla de V3 con filas: se rellenan con 'train' y la clave admite la misma ventana en 'valid'."""
    v3 = (ROOT / "db/migrations/analytics/V3__drift_metrics.sql").read_text()
    v6 = (ROOT / "db/migrations/analytics/V6__drift_metrics_reference.sql").read_text()
    with provisioned_db.connect(ADMIN) as conn, conn.transaction(force_rollback=True):
        conn.execute("DROP TABLE analytics.drift_metrics")
        conn.execute(v3)
        conn.execute(INSERT_METRIC, ("score",))
        conn.execute(INSERT_METRIC, ("amount",))
        conn.execute(v6)
        assert conn.execute("SELECT feature, reference FROM analytics.drift_metrics ORDER BY 1").fetchall() == [
            ("amount", "train"), ("score", "train")]  # fmt: skip
        conn.execute("INSERT INTO analytics.drift_metrics (window_start, window_end, feature, psi, n_current, n_bins, "
                     "degenerate, insufficient, is_top_feature, model_version, reference) SELECT window_start, window_end, "
                     "feature, 0.9, n_current, n_bins, degenerate, insufficient, is_top_feature, model_version, 'valid' "
                     "FROM analytics.drift_metrics")  # fmt: skip
        assert conn.execute("SELECT count(*) FROM analytics.drift_metrics").fetchone() == (4,)
        with pytest.raises(psycopg.errors.UniqueViolation), conn.transaction():
            conn.execute(INSERT_METRIC, ("score",))              # misma clave y referencia por defecto: 'train'
        with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
            conn.execute("UPDATE analytics.drift_metrics SET reference = 'otra'")


def test_fraud_reasons_has_three_ranks_per_transfer(provisioned_db):
    with provisioned_db.connect(ADMIN) as conn:
        assert [c[0] for c in columns(conn, "fraud_reasons")] == [
            "transfer_id", "rank", "feature", "contribution", "model_version", "computed_at"]  # fmt: skip
        pk = conn.execute("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid = 'analytics.fraud_reasons'::regclass AND contype = 'p'").fetchone()
        assert pk == ("PRIMARY KEY (transfer_id, rank)",)
        tid = uuid.uuid4()
        with conn.transaction(force_rollback=True):
            for rank in (1, 3):
                conn.execute("INSERT INTO analytics.fraud_reasons (transfer_id, rank, feature, contribution, model_version) "
                             "VALUES (%s, %s, 'amount', 0.1, 'm/1')", (tid, rank))  # fmt: skip
            for rank in (0, 4):
                with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
                    conn.execute("INSERT INTO analytics.fraud_reasons (transfer_id, rank, feature, contribution, model_version) "
                                 "VALUES (%s, %s, 'amount', 0.1, 'm/1')", (tid, rank))  # fmt: skip


def test_credit_scores_is_not_created_without_the_credit_extension(provisioned_db):
    with provisioned_db.connect(ADMIN) as conn:
        assert conn.execute("SELECT to_regclass('analytics.credit_scores')").fetchone() == (None,)


# --- la vista con datos -------------------------------------------------------------------------------


def _uuid(label: str) -> uuid.UUID:
    return uuid.UUID(hashlib.md5(label.encode()).hexdigest())


@pytest.fixture(scope="module")
def outcomes(provisioned_db):
    """Dos clientes, la cuenta CLEARING y tres transferencias: una de siembra desde CLEARING, una con etiqueta."""
    base = datetime(2099, 1, 1, tzinfo=timezone.utc)  # posterior a cualquier otra fila: fija max(occurred_at)
    ids = {k: _uuid(f"analytics-test:{k}") for k in ("cust", "acc1", "acc2", "clearing", "t1", "t2", "t3")}
    rows = [  # transfer, origen, destino, occurred_at
        ("t1", "acc1", "acc2", base),
        ("t2", "acc2", "acc1", base + timedelta(hours=72)),
        ("t3", "clearing", "acc1", base + timedelta(hours=1)),
    ]
    with provisioned_db.connect("verabank_transfer") as conn:
        conn.execute("INSERT INTO core.customers (customer_id, segment) VALUES (%s, 'PYME')", (ids["cust"],))
        conn.execute("INSERT INTO core.accounts (account_id, customer_id, balance) VALUES (%s, %s, 1000), (%s, %s, 1000)",
                     (ids["acc1"], ids["cust"], ids["acc2"], ids["cust"]))  # fmt: skip
        conn.execute("INSERT INTO core.accounts (account_id, account_type) VALUES (%s, 'CLEARING')", (ids["clearing"],))
        for key, src, dst, at in rows:
            conn.execute(
                """INSERT INTO core.transfers (transfer_id, client_id, idempotency_key, request_hash, source_account_id,
                     dest_account_id, dest_country, rail, transfer_type, amount, currency, status, fraud_decision,
                     decision_source, model_version, scoring_latency_ms, occurred_at)
                   VALUES (%s, 'test', %s, %s, %s, %s, 'PE', 'INTERNAL', 'TRANSFER', 10, 'PEN', 'COMPLETED', 'APPROVE',
                     'MODEL', 'm/1', 7, %s)""",
                (ids[key], uuid.uuid4(), "0" * 64, ids[src], ids[dst], at))  # fmt: skip
    with provisioned_db.connect("risk_svc") as conn:
        conn.execute("INSERT INTO risk.fraud_labels (transfer_id, is_fraud, label_source) VALUES (%s, true, 'SIMULATOR')", (ids["t1"],))
    yield ids
    with provisioned_db.connect(ADMIN) as conn:
        with conn.transaction():
            conn.execute("DELETE FROM risk.fraud_labels WHERE transfer_id = %s", (ids["t1"],))
            conn.execute("DELETE FROM core.transfers WHERE transfer_id = ANY(%s)", ([ids[k] for k in ("t1", "t2", "t3")],))
            conn.execute("DELETE FROM core.accounts WHERE account_id = ANY(%s)", ([ids["acc1"], ids["acc2"], ids["clearing"]],))
            conn.execute("DELETE FROM core.customers WHERE customer_id = %s", (ids["cust"],))


def test_bi_reads_the_view_but_not_core(provisioned_db, outcomes):
    with provisioned_db.connect("verabank_bi") as conn:
        rows = conn.execute(
            "SELECT transfer_id, segment, is_fraud, label_mature FROM analytics.v_transfer_outcomes "
            "WHERE transfer_id = ANY(%s) ORDER BY occurred_at",
            ([outcomes[k] for k in ("t1", "t2", "t3")],)).fetchall()  # fmt: skip
        with pytest.raises(DENIED):
            conn.execute("SELECT * FROM core.transfers")
    # La de siembra desde CLEARING queda fuera por el JOIN con core.customers; t1 es 72 h anterior a la última.
    assert rows == [(outcomes["t1"], "PYME", True, True), (outcomes["t2"], "PYME", False, False)]


# --- roles ----------------------------------------------------------------------------------------------


class TestMlJob:
    def test_reads_scoring_log_and_labels_and_writes_its_two_tables(self, provisioned_db):
        with provisioned_db.connect("ml_job") as conn:
            conn.execute("SELECT * FROM risk.scoring_log LIMIT 1")
            conn.execute("SELECT * FROM risk.fraud_labels LIMIT 1")
            with conn.transaction(force_rollback=True):
                conn.execute(
                    """INSERT INTO analytics.drift_metrics (window_start, window_end, feature, psi, n_current, n_bins,
                         degenerate, insufficient, is_top_feature, model_version)
                       VALUES ('2026-01-01', '2026-01-02', 'score', 0.1, 300, 10, false, false, true, 'm/1')""")
                conn.execute("UPDATE analytics.drift_metrics SET alert = true WHERE feature = 'score'")
                conn.execute("INSERT INTO analytics.fraud_reasons (transfer_id, rank, feature, contribution, model_version) "
                             "VALUES (gen_random_uuid(), 1, 'amount', 0.5, 'm/1')")  # fmt: skip
                conn.execute("SELECT * FROM analytics.fraud_reasons")

    @pytest.mark.parametrize("sql", [
        "SELECT * FROM core.transfers",
        "SELECT * FROM core.accounts",
        "SELECT * FROM audit.audit_log",
        "SELECT * FROM analytics.v_transfer_outcomes",
        "SELECT * FROM risk.aml_alerts",
        "INSERT INTO risk.scoring_log (transfer_id, source_ref, dest_ref, amount, occurred_at, features, score, decision, model_version) "
        "VALUES (gen_random_uuid(), 's', 'd', 1, now(), '{}', 0.1, 'APPROVE', 'm/1')",
        "UPDATE risk.fraud_labels SET is_fraud = true",
        "DELETE FROM analytics.drift_metrics",
        "DELETE FROM analytics.fraud_reasons",
        "INSERT INTO analytics.probe VALUES (3)",
        "CREATE TABLE analytics.extra (n INT)",
        "SELECT audit.append('a','b','c','d',NULL,'{}')",
        "SELECT * FROM audit.verify_chain()",
    ])  # fmt: skip
    def test_cannot_touch_anything_else(self, provisioned_db, sql):
        with provisioned_db.connect("ml_job") as conn:
            with pytest.raises(DENIED):
                conn.execute(sql)

    def test_table_privileges_are_exactly_the_granted_ones(self, provisioned_db):
        with provisioned_db.connect(ADMIN) as conn:
            grants = set(conn.execute(
                """SELECT table_schema || '.' || table_name, privilege_type FROM information_schema.role_table_grants
                   WHERE grantee = 'ml_job'""").fetchall())  # fmt: skip
        assert grants == {
            ("risk.scoring_log", "SELECT"), ("risk.fraud_labels", "SELECT"),
            *{(f"analytics.{t}", p) for t in ("drift_metrics", "fraud_reasons") for p in ("SELECT", "INSERT", "UPDATE")},
        }  # fmt: skip


class TestBiOnAnalytics:
    """verabank_bi, el rol de solo lectura de BI (db/README.md): SELECT en analytics y en risk.aml_alerts, nada en core."""

    def test_reads_analytics_and_aml_alerts(self, provisioned_db):
        with provisioned_db.connect("verabank_bi") as conn:
            for rel in ("analytics.drift_metrics", "analytics.fraud_reasons", "analytics.v_transfer_outcomes", "risk.aml_alerts"):
                conn.execute(f"SELECT * FROM {rel} LIMIT 1")

    @pytest.mark.parametrize("sql", [
        "SELECT * FROM core.customers",
        "SELECT * FROM audit.audit_log",
        "SELECT * FROM flyway_analytics.flyway_schema_history",
        "INSERT INTO analytics.drift_metrics (window_start, window_end, feature, psi, n_current, n_bins, degenerate, "
        "insufficient, is_top_feature, model_version) VALUES (now(), now() + interval '1 day', 'x', 0, 0, 0, false, false, false, 'm')",
        "SELECT * FROM audit.verify_chain()",
    ])  # fmt: skip
    def test_cannot_read_core_audit_or_write(self, provisioned_db, sql):
        with provisioned_db.connect("verabank_bi") as conn:
            with pytest.raises(DENIED):
                conn.execute(sql)


def test_only_the_admin_executes_verify_chain(provisioned_db):
    roles = ("risk_svc", "sim_loader", "ml_job", "verabank_bi", "verabank_risk", "verabank_transfer", "public")
    with provisioned_db.connect(ADMIN) as conn:
        who = {r: conn.execute(f"SELECT has_function_privilege(%s, '{VERIFY}', 'EXECUTE')", (r,)).fetchone()[0] for r in roles}
        owner = conn.execute(f"SELECT proowner::regrole::text FROM pg_proc WHERE oid = '{VERIFY}'::regprocedure").fetchone()[0]
    assert who == dict.fromkeys(roles, False)
    assert owner == ADMIN
