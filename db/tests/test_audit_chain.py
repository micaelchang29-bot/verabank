"""T-16 (HU-07): auditoría de solo inserción y audit.verify_chain, que detecta la alteración de la cadena.

Las alteraciones las hace el superusuario dentro de una transacción que se revierte al final: la base de la prueba
queda con la cadena íntegra para la siguiente.
"""

import threading
import time

import psycopg
import pytest
from conftest import ADMIN, make_database

MESSAGE = "audit_log es de solo inserción"


@pytest.fixture(scope="module")
def chain_db(tmp_path_factory):
    """Base propia: las pruebas de este módulo escriben más de 100.000 filas de auditoría."""
    db = make_database(tmp_path_factory, runtime_roles_at_init=True)
    db.provision(with_roles_script=False)
    with db.connect("risk_svc") as conn:
        for i in range(10):
            conn.execute("SELECT audit.append('risk-service', 'SEED', 'AML_ALERT', %s, NULL, %s::jsonb)",
                         (f"a-{i}", f'{{"i": {i}, "texto": "ñandú"}}'))  # fmt: skip
    yield db
    db.teardown()


def verify(conn, p_from=1):
    return conn.execute("SELECT checked, first_bad_id, problem FROM audit.verify_chain(%s)", (p_from,)).fetchone()


def count(conn):
    return conn.execute("SELECT count(*) FROM audit.audit_log").fetchone()[0]


def test_an_intact_chain_verifies(chain_db):
    with chain_db.connect(ADMIN) as conn:
        assert verify(conn) == (count(conn), None, None)


def test_verifying_from_the_middle_checks_only_the_rest(chain_db):
    with chain_db.connect(ADMIN) as conn:
        assert verify(conn, 4) == (count(conn) - 3, None, None)


@pytest.mark.parametrize("role", ["verabank_transfer", ADMIN])
@pytest.mark.parametrize("sql", ["UPDATE audit.audit_log SET detail = '{}' WHERE audit_id = 2",
                                 "DELETE FROM audit.audit_log WHERE audit_id = 2"])  # fmt: skip
def test_t16a_update_and_delete_fail_with_the_trigger_message(chain_db, role, sql):
    with chain_db.connect(role) as conn:
        with pytest.raises(psycopg.errors.RaiseException, match=MESSAGE):
            conn.execute(sql)


def tampered(conn, *statements):
    """Desactiva trg_audit_immutable, ejecuta las alteraciones y lo reactiva, como haría un superusuario."""
    conn.execute("ALTER TABLE audit.audit_log DISABLE TRIGGER trg_audit_immutable")
    for sql in statements:
        conn.execute(sql)
    conn.execute("ALTER TABLE audit.audit_log ENABLE TRIGGER trg_audit_immutable")


def test_t16b_a_superuser_altering_detail_is_detected_at_that_row(chain_db):
    with chain_db.connect(ADMIN) as conn:
        with conn.transaction(force_rollback=True):
            tampered(conn, """UPDATE audit.audit_log SET detail = '{"i": 5, "texto": "alterado"}' WHERE audit_id = 6""")
            assert verify(conn) == (count(conn), 6, "HASH_MISMATCH")
        assert verify(conn)[1] is None  # revertido


@pytest.mark.parametrize("column, value", [("actor", "'otro'"), ("action", "'OTRA'"), ("entity_type", "'X'"),
                                           ("entity_id", "'a-x'"), ("occurred_at", "occurred_at + interval '1 microsecond'")])  # fmt: skip
def test_every_hashed_field_is_covered(chain_db, column, value):
    with chain_db.connect(ADMIN) as conn:
        with conn.transaction(force_rollback=True):
            tampered(conn, f"UPDATE audit.audit_log SET {column} = {value} WHERE audit_id = 3")
            assert verify(conn)[1:] == (3, "HASH_MISMATCH")


def test_t16c_deleting_a_middle_row_is_a_gap(chain_db):
    with chain_db.connect(ADMIN) as conn:
        with conn.transaction(force_rollback=True):
            tampered(conn, "DELETE FROM audit.audit_log WHERE audit_id = 4")
            assert verify(conn) == (count(conn), 4, "GAP")


def test_deleting_the_first_row_is_a_gap(chain_db):
    with chain_db.connect(ADMIN) as conn:
        with conn.transaction(force_rollback=True):
            tampered(conn, "DELETE FROM audit.audit_log WHERE audit_id = 1")
            assert verify(conn)[1:] == (1, "GAP")


def test_t16d_altering_prev_hash_is_detected(chain_db):
    with chain_db.connect(ADMIN) as conn:
        with conn.transaction(force_rollback=True):
            tampered(conn, "UPDATE audit.audit_log SET prev_hash = sha256('x'::bytea) WHERE audit_id = 7")
            assert verify(conn)[1:] == (7, "PREV_MISMATCH")


def test_a_rewritten_row_hash_breaks_the_next_link(chain_db):
    """Quien altera una fila y recalcula su row_hash rompe el prev_hash de la siguiente."""
    with chain_db.connect(ADMIN) as conn:
        with conn.transaction(force_rollback=True):
            tampered(conn, """UPDATE audit.audit_log SET detail = '{"x": 1}',
                                row_hash = sha256(coalesce(prev_hash, ''::bytea) || convert_to(concat_ws('|',
                                  to_char(occurred_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US'),
                                  actor, action, entity_type, entity_id, '{"x": 1}'), 'UTF8'))
                              WHERE audit_id = 5""")
            assert verify(conn)[1:] == (6, "PREV_MISMATCH")


def test_known_limit_truncating_the_tail_is_not_detected(chain_db):
    """Límite declarado (db/README.md, D5): sin ancla externa, cortar el final deja una cadena válida."""
    with chain_db.connect(ADMIN) as conn:
        with conn.transaction(force_rollback=True):
            last = conn.execute("SELECT max(audit_id) FROM audit.audit_log").fetchone()[0]
            tampered(conn, f"DELETE FROM audit.audit_log WHERE audit_id = {last}")
            assert verify(conn)[1] is None


def test_t16e_twenty_threads_keep_the_chain_intact_and_gapless(chain_db):
    errors = []
    with chain_db.connect(ADMIN) as conn:
        before = conn.execute("SELECT coalesce(max(audit_id), 0) FROM audit.audit_log").fetchone()[0]

    def worker(n):
        try:
            with chain_db.connect("risk_svc") as conn:
                for i in range(1000):
                    conn.execute("SELECT audit.append('risk-service', 'LOAD', 'AML_ALERT', %s, NULL, %s::jsonb)",
                                 (f"t{n}-{i}", f'{{"thread": {n}, "i": {i}}}'))  # fmt: skip
        except Exception as exc:  # noqa: BLE001 — se informa abajo
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    with chain_db.connect(ADMIN) as conn:
        ids = conn.execute("SELECT count(*), min(audit_id), max(audit_id) FROM audit.audit_log WHERE audit_id > %s", (before,)).fetchone()
        assert ids == (20_000, before + 1, before + 20_000)
        assert verify(conn) == (count(conn), None, None)


def test_t16f_a_session_in_another_time_zone_keeps_the_chain_valid(chain_db):
    with chain_db.connect("verabank_transfer") as conn:
        conn.execute("SET timezone = 'America/Lima'")
        conn.execute("INSERT INTO audit.audit_log (actor, action, entity_type, entity_id, detail) "
                     "VALUES ('transfer-service', 'TZ', 'TRANSFER', 't-lima', '{}')")  # fmt: skip
        conn.execute("INSERT INTO audit.audit_log (occurred_at, actor, action, entity_type, entity_id, detail) "
                     "VALUES ('2026-03-01 23:30:00', 'transfer-service', 'TZ', 'TRANSFER', 't-lima-2', '{}')")  # fmt: skip
    with chain_db.connect(ADMIN) as conn:
        conn.execute("SET timezone = 'Asia/Tokyo'")
        assert verify(conn)[1] is None


def test_t16h_verify_chain_over_100k_rows(chain_db, record_property):
    with chain_db.connect("verabank_transfer") as conn:
        conn.execute("""INSERT INTO audit.audit_log (actor, action, entity_type, entity_id, detail)
                        SELECT 'transfer-service', 'BULK', 'TRANSFER', 't-' || g, jsonb_build_object('g', g)
                        FROM generate_series(1, 100000) g""")
    with chain_db.connect(ADMIN) as conn:
        total = count(conn)
        assert total >= 100_000
        start = time.perf_counter()
        result = verify(conn)
        elapsed = time.perf_counter() - start
    record_property("verify_chain_rows", total)
    record_property("verify_chain_seconds", round(elapsed, 3))
    print(f"\nverify_chain: {total} filas en {elapsed:.3f} s")
    assert result == (total, None, None)
    assert elapsed < 60
