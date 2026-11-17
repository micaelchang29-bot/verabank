"""audit.append: risk_svc audita sin tocar audit.audit_log, y la cadena de hashes se mantiene."""

import uuid

import psycopg
import pytest
from conftest import ADMIN


def append(conn, actor, action, entity_id, detail, correlation_id=None):
    return conn.execute(
        "SELECT audit.append(%s, %s, 'AML_ALERT', %s, %s, %s::jsonb)",
        (actor, action, entity_id, correlation_id, detail),
    ).fetchone()[0]


def test_append_inserts_and_chains_the_hash(provisioned_db):
    correlation = uuid.uuid4()
    with provisioned_db.connect("risk_svc") as conn:
        first = append(conn, "risk-service", "ALERT_OPENED", "a-1", '{"rule": "THRESHOLD"}', correlation)
        second = append(conn, "analyst:ana", "ALERT_CLOSED", "a-1", '{"status": "CLOSED_FALSE_POSITIVE"}')
    assert second == first + 1  # sin huecos
    with provisioned_db.connect(ADMIN) as admin:
        rows = admin.execute(
            "SELECT audit_id, actor, action, entity_type, entity_id, correlation_id, detail, prev_hash, row_hash "
            "FROM audit.audit_log WHERE audit_id IN (%s, %s) ORDER BY audit_id", (first, second)).fetchall()
        (_, actor, action, entity_type, entity_id, corr, detail, _prev1, hash1), (_, actor2, _, _, _, corr2, _, prev2, hash2) = rows
        assert (actor, action, entity_type, entity_id, corr, detail) == (
            "risk-service", "ALERT_OPENED", "AML_ALERT", "a-1", correlation, {"rule": "THRESHOLD"})
        assert (actor2, corr2) == ("analyst:ana", None)
        assert prev2 == hash1 and hash1 != hash2  # prev_hash apunta a la fila anterior
        # La cadena completa es consistente.
        assert admin.execute(
            """SELECT count(*) FROM audit.audit_log a JOIN audit.audit_log p ON p.audit_id = a.audit_id - 1
               WHERE a.prev_hash IS DISTINCT FROM p.row_hash""").fetchone() == (0,)
        assert admin.execute("SELECT prev_hash IS NULL FROM audit.audit_log ORDER BY audit_id LIMIT 1").fetchone() == (True,)
        # row_hash = sha256(prev_hash || datos), con la misma fórmula del trigger.
        assert admin.execute(
            """SELECT row_hash = sha256(coalesce(prev_hash, ''::bytea) || convert_to(concat_ws('|',
                 to_char(occurred_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US'),
                 actor, action, entity_type, entity_id, detail::text), 'UTF8'))
               FROM audit.audit_log WHERE audit_id = %s""", (second,)).fetchone() == (True,)


def test_append_chains_after_rows_written_by_the_owner(provisioned_db):
    """La cadena es única: una fila de risk_svc continúa la última, sea de quien sea."""
    with provisioned_db.connect("verabank_transfer") as owner:
        owner.execute("INSERT INTO audit.audit_log (actor, action, entity_type, entity_id, detail) "
                      "VALUES ('transfer-service', 'X', 'TRANSFER', 't-1', '{}')")
        last = owner.execute("SELECT audit_id, row_hash FROM audit.audit_log ORDER BY audit_id DESC LIMIT 1").fetchone()
    with provisioned_db.connect("risk_svc") as conn:
        new_id = append(conn, "risk-service", "Y", "a-2", "{}")
    with provisioned_db.connect(ADMIN) as admin:
        assert admin.execute("SELECT audit_id, prev_hash FROM audit.audit_log WHERE audit_id = %s", (new_id,)).fetchone() == (last[0] + 1, last[1])


def test_append_rejects_incomplete_entries(provisioned_db):
    with provisioned_db.connect("risk_svc") as conn:
        with pytest.raises(psycopg.errors.NotNullViolation):
            conn.execute("SELECT audit.append('risk-service', NULL, 'AML_ALERT', 'a-1', NULL, '{}')")
        with pytest.raises(psycopg.errors.NotNullViolation):
            conn.execute("SELECT audit.append('risk-service', 'X', 'AML_ALERT', 'a-1', NULL, NULL)")


def test_the_log_stays_append_only_through_the_function(provisioned_db):
    with provisioned_db.connect("risk_svc") as conn:
        append(conn, "risk-service", "Z", "a-3", "{}")
    with provisioned_db.connect("verabank_transfer") as owner:
        for sql in ("UPDATE audit.audit_log SET actor = 'x'", "DELETE FROM audit.audit_log"):
            with pytest.raises(psycopg.errors.RaiseException, match="solo inserción"):
                owner.execute(sql)


def test_append_runs_with_a_pinned_search_path(provisioned_db):
    """SECURITY DEFINER sin search_path fijo permitiría suplantar funciones con un esquema propio."""
    with provisioned_db.connect(ADMIN) as conn:
        secdef, config = conn.execute(
            "SELECT prosecdef, proconfig FROM pg_proc WHERE oid = 'audit.append(text,text,text,text,uuid,jsonb)'::regprocedure"
        ).fetchone()
    assert secdef and config == ["search_path=pg_catalog, pg_temp"]
