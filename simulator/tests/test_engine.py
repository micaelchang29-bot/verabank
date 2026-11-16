from __future__ import annotations

import uuid

import pytest

from tests.conftest import command
from verabank_sim.rail import messages as m
from verabank_sim.rail.engine import REASON_CODES, RailConfig
from verabank_sim.rail.store import SqliteStore

INSTR, QUERY, CANCEL = m.CREDIT_INSTRUCTION, m.CREDIT_STATUS_QUERY, m.CREDIT_CANCEL
FIXED_DELAY = {"delay_min_ms": 100, "delay_max_ms": 100}


def status_of(messages: list[m.Message]) -> str:
    (msg,) = messages
    assert msg.event_type == m.CREDIT_STATUS
    return msg.envelope["payload"]["state"]


def run_all(engine, clock, transfer_ids, *, horizon=1.0):
    """Instruye todas, deja pasar el retraso y devuelve lo emitido."""
    for tid in transfer_ids:
        engine.handle(command(INSTR, tid))
    clock.advance(horizon)
    return engine.due()


def test_determinismo_misma_semilla_mismo_resultado(make_engine):
    tids = [str(uuid.uuid4()) for _ in range(200)]
    results = []
    for _ in range(2):
        engine = make_engine(reject_rate=0.4, silent_rate=0.2, lost_rate=0.1, dup_rate=0.3, seed=7)
        for tid in tids:
            engine.handle(command(INSTR, tid))
        results.append({t: (c.outcome, c.detail, c.silent, c.dup, c.due_at - 1_700_000_000.0)
                        for t in tids if (c := engine.store.get(t))})
    assert results[0] == results[1]
    assert 0 < len(results[0]) < len(tids)  # algunas se perdieron


def test_semilla_distinta_cambia_decisiones(make_engine):
    tids = [str(uuid.uuid4()) for _ in range(100)]
    outcomes = []
    for seed in (1, 2):
        engine = make_engine(reject_rate=0.5, seed=seed)
        for tid in tids:
            engine.handle(command(INSTR, tid))
        outcomes.append([engine.store.get(t).outcome for t in tids])
    assert outcomes[0] != outcomes[1]


def test_reentrega_no_cambia_la_decision(make_engine, clock):
    engine = make_engine(reject_rate=0.5, **FIXED_DELAY)
    tid = str(uuid.uuid4())
    engine.handle(command(INSTR, tid))
    before = engine.store.get(tid)
    clock.advance(5)
    engine.handle(command(INSTR, tid))
    assert engine.store.get(tid) == before


@pytest.mark.parametrize(
    ("param", "rate"), [("reject_rate", 0.3), ("silent_rate", 0.1), ("lost_rate", 0.05), ("dup_rate", 0.2)]
)
def test_tasas_observadas_con_10000_transferencias(make_engine, param, rate):
    # lost_rate distinto de 0 reduce la muestra: se mide cada tasa sobre las instrucciones que quedan.
    engine = make_engine(**{param: rate})
    n = 10_000
    for i in range(n):
        engine.handle(command(INSTR, str(uuid.UUID(int=i))))
    credits = [engine.store.get(str(uuid.UUID(int=i))) for i in range(n)]
    kept = [c for c in credits if c is not None]
    observed = {
        "reject_rate": sum(c.outcome == "REJECTED" for c in kept) / len(kept),
        "silent_rate": sum(c.silent for c in kept) / len(kept),
        "lost_rate": 1 - len(kept) / n,
        "dup_rate": sum(c.dup for c in kept) / len(kept),
    }[param]
    assert observed == pytest.approx(rate, abs=0.02)


def test_retraso_uniforme_dentro_del_rango(make_engine, clock):
    engine = make_engine(delay_min_ms=50, delay_max_ms=150)
    delays = []
    for i in range(500):
        tid = str(uuid.UUID(int=i))
        engine.handle(command(INSTR, tid))
        delays.append(engine.store.get(tid).due_at - clock())
    assert min(delays) >= 0.05 and max(delays) <= 0.15
    assert max(delays) - min(delays) > 0.05


def test_la_instruccion_no_responde_hasta_vencer(make_engine, clock):
    engine = make_engine(**FIXED_DELAY)
    tid = str(uuid.uuid4())
    assert engine.handle(command(INSTR, tid)) == []
    clock.advance(0.099)
    assert engine.due() == []
    clock.advance(0.002)
    (msg,) = engine.due()
    assert msg.event_type == m.CREDIT_CONFIRMED
    assert msg.envelope["payload"]["rail_ref"]
    assert engine.due() == []  # una sola vez


def test_rechazo_lleva_reason_code_de_la_lista(make_engine, clock):
    engine = make_engine(reject_rate=1.0, **FIXED_DELAY)
    (msg,) = run_all(engine, clock, [str(uuid.uuid4())])
    assert msg.event_type == m.CREDIT_REJECTED
    assert msg.envelope["payload"]["reason_code"] in REASON_CODES


def test_instruccion_duplicada_no_cambia_resultado_y_reemite_mismo_event_id(make_engine, clock):
    engine = make_engine(reject_rate=0.5, **FIXED_DELAY)
    tid = str(uuid.uuid4())
    engine.handle(command(INSTR, tid))
    # pendiente: la duplicada no emite
    assert engine.handle(command(INSTR, tid)) == []
    clock.advance(1)
    (first,) = engine.due()
    clock.advance(10)
    (again,) = engine.handle(command(INSTR, tid))  # reemisión inmediata, sin retraso
    assert again.event_id == first.event_id == m.deterministic_event_id(tid, first.event_type)
    assert again.event_type == first.event_type
    assert again.envelope["payload"] == first.envelope["payload"]
    assert engine.due() == []


def test_event_id_determinista_entre_motores_y_status_uuid4(make_engine, clock):
    tid = str(uuid.uuid4())
    ids = []
    for _ in range(2):
        engine = make_engine(**FIXED_DELAY)
        (msg,) = run_all(engine, clock, [tid])
        ids.append(msg.event_id)
    assert ids[0] == ids[1]
    engine = make_engine()
    a = engine.handle(command(QUERY, tid))[0].event_id
    b = engine.handle(command(QUERY, tid))[0].event_id
    assert a != b and uuid.UUID(a).version == 4


def test_sobre_hereda_correlacion_y_occurred_at_del_comando(make_engine, clock):
    engine = make_engine(**FIXED_DELAY)
    tid, corr = str(uuid.uuid4()), str(uuid.uuid4())
    engine.handle(command(INSTR, tid, correlation_id=corr, occurred_at="2026-01-02T03:04:05.000Z"))
    clock.advance(1)
    (msg,) = engine.due()
    env = msg.envelope
    assert env["aggregate_id"] == tid and msg.key == tid
    assert env["correlation_id"] == corr
    assert env["occurred_at"] == "2026-01-02T03:04:05.000Z"
    assert env["emitted_at"] == m.iso(clock())
    assert dict(msg.headers())["correlation_id"] == corr.encode()


# ---- dup-mode -------------------------------------------------------------------------


def test_dup_same_id_repite_el_mismo_mensaje(make_engine, clock):
    engine = make_engine(dup_rate=1.0, dup_mode="same-id", **FIXED_DELAY)
    a, b = run_all(engine, clock, [str(uuid.uuid4())])
    assert a.event_id == b.event_id and a.envelope == b.envelope


def test_dup_new_id_repite_el_payload_con_otro_event_id(make_engine, clock):
    engine = make_engine(dup_rate=1.0, dup_mode="new-id", **FIXED_DELAY)
    a, b = run_all(engine, clock, [str(uuid.uuid4())])
    assert a.event_id != b.event_id
    assert a.envelope["payload"] == b.envelope["payload"] and a.event_type == b.event_type
    assert engine.counters.duplicated == 1


def test_sin_dup_se_emite_una_vez(make_engine, clock):
    engine = make_engine(dup_rate=0.0, **FIXED_DELAY)
    assert len(run_all(engine, clock, [str(uuid.uuid4())])) == 1


# ---- consulta -------------------------------------------------------------------------


def test_consulta_unknown_antes_de_vencer_y_resultado_despues(make_engine, clock):
    engine = make_engine(reject_rate=0.0, **FIXED_DELAY)
    tid = str(uuid.uuid4())
    assert status_of(engine.handle(command(QUERY, tid))) == "UNKNOWN"  # nunca llegó
    engine.handle(command(INSTR, tid))
    assert status_of(engine.handle(command(QUERY, tid))) == "UNKNOWN"  # en curso
    clock.advance(0.1)
    assert status_of(engine.handle(command(QUERY, tid))) == "CONFIRMED"


def test_consulta_rechazada(make_engine, clock):
    engine = make_engine(reject_rate=1.0, **FIXED_DELAY)
    tid = str(uuid.uuid4())
    engine.handle(command(INSTR, tid))
    clock.advance(1)
    assert status_of(engine.handle(command(QUERY, tid))) == "REJECTED"


def test_consulta_unknown_si_se_perdio(make_engine, clock):
    engine = make_engine(lost_rate=1.0, **FIXED_DELAY)
    tid = str(uuid.uuid4())
    assert engine.handle(command(INSTR, tid)) == []
    assert engine.store.get(tid) is None  # no deja estado
    clock.advance(60)
    assert status_of(engine.handle(command(QUERY, tid))) == "UNKNOWN"
    assert engine.counters.lost == 1


def test_consulta_revela_resultado_silenciado(make_engine, clock):
    engine = make_engine(silent_rate=1.0, reject_rate=1.0, **FIXED_DELAY)
    tid = str(uuid.uuid4())
    engine.handle(command(INSTR, tid))
    clock.advance(60)
    assert engine.due() == []  # la respuesta nunca se emite
    assert status_of(engine.handle(command(QUERY, tid))) == "REJECTED"
    assert engine.counters.silenced == 1


def test_silenciada_no_emite_ni_reemite(make_engine, clock):
    engine = make_engine(silent_rate=1.0, **FIXED_DELAY)
    tid = str(uuid.uuid4())
    engine.handle(command(INSTR, tid))
    clock.advance(1)
    assert engine.due() == []
    assert engine.handle(command(INSTR, tid)) == []


# ---- cancelación ----------------------------------------------------------------------


def test_cancelar_antes_de_vencer(make_engine, clock):
    engine = make_engine(**FIXED_DELAY)
    tid = str(uuid.uuid4())
    engine.handle(command(INSTR, tid))
    assert status_of(engine.handle(command(CANCEL, tid))) == "CANCELLED"
    clock.advance(10)
    assert engine.due() == []  # nunca emite Confirmed/Rejected
    assert status_of(engine.handle(command(QUERY, tid))) == "CANCELLED"
    assert engine.handle(command(INSTR, tid)) == []  # la reentrega tampoco


def test_cancelar_una_perdida(make_engine, clock):
    engine = make_engine(lost_rate=1.0)
    tid = str(uuid.uuid4())
    engine.handle(command(INSTR, tid))
    assert status_of(engine.handle(command(CANCEL, tid))) == "CANCELLED"
    assert status_of(engine.handle(command(QUERY, tid))) == "CANCELLED"


def test_cancelar_algo_que_nunca_llego_y_instruccion_tardia_no_emite(make_engine, clock):
    engine = make_engine(**FIXED_DELAY)
    tid = str(uuid.uuid4())
    assert status_of(engine.handle(command(CANCEL, tid))) == "CANCELLED"
    assert engine.handle(command(INSTR, tid)) == []
    clock.advance(10)
    assert engine.due() == []
    assert engine.store.get(tid).cancelled


def test_cancelar_tras_vencer_devuelve_el_resultado_real(make_engine, clock):
    for reject, expected in ((0.0, "CONFIRMED"), (1.0, "REJECTED")):
        engine = make_engine(reject_rate=reject, **FIXED_DELAY)
        tid = str(uuid.uuid4())
        engine.handle(command(INSTR, tid))
        clock.advance(1)
        assert status_of(engine.handle(command(CANCEL, tid))) == expected
        assert not engine.store.get(tid).cancelled


def test_cancelar_silenciada_ya_vencida_devuelve_resultado(make_engine, clock):
    engine = make_engine(silent_rate=1.0, **FIXED_DELAY)
    tid = str(uuid.uuid4())
    engine.handle(command(INSTR, tid))
    clock.advance(1)
    assert status_of(engine.handle(command(CANCEL, tid))) == "CONFIRMED"


def test_cancelar_dos_veces_es_idempotente(make_engine):
    engine = make_engine()
    tid = str(uuid.uuid4())
    assert status_of(engine.handle(command(CANCEL, tid))) == "CANCELLED"
    assert status_of(engine.handle(command(CANCEL, tid))) == "CANCELLED"


# ---- reinicio -------------------------------------------------------------------------


def test_reinicio_reprograma_envios_pendientes(tmp_path, make_engine, clock):
    db = tmp_path / "state" / "rail.db"
    engine = make_engine(SqliteStore(db), **FIXED_DELAY)
    pend, cancelada = (str(uuid.uuid4()) for _ in range(2))
    for tid in (pend, cancelada):
        engine.handle(command(INSTR, tid))
    engine.handle(command(CANCEL, cancelada))
    engine.store.close()

    reopened = make_engine(SqliteStore(db), **FIXED_DELAY)
    assert reopened.recover() == 1
    assert reopened.next_due_at() == pytest.approx(clock() + 0.1)
    clock.advance(1)
    (msg,) = reopened.due()
    assert msg.transfer_id == pend
    # tras emitir y reiniciar de nuevo no queda nada pendiente
    reopened.store.close()
    again = make_engine(SqliteStore(db), **FIXED_DELAY)
    assert again.recover() == 0


def test_reinicio_con_vencidos_los_emite_de_inmediato(tmp_path, make_engine, clock):
    db = tmp_path / "rail.db"
    engine = make_engine(SqliteStore(db), **FIXED_DELAY)
    tid = str(uuid.uuid4())
    engine.handle(command(INSTR, tid))
    engine.store.close()
    clock.advance(3600)  # el proceso estuvo caído
    reopened = make_engine(SqliteStore(db), **FIXED_DELAY)
    reopened.recover()
    (msg,) = reopened.due()
    assert msg.event_id == m.deterministic_event_id(tid, msg.event_type)


def test_contadores(make_engine, clock):
    engine = make_engine(**FIXED_DELAY)
    tid = str(uuid.uuid4())
    engine.handle(command(INSTR, tid))
    engine.handle(command(QUERY, tid))
    engine.handle(command(CANCEL, str(uuid.uuid4())))
    c = engine.counters
    assert (c.received, c.confirmed, c.queries, c.cancels) == (3, 1, 1, 1)
    assert "recibidas=3" in c.summary()


def test_config_invalida():
    with pytest.raises(ValueError):
        RailConfig(reject_rate=1.5)
    with pytest.raises(ValueError):
        RailConfig(delay_min_ms=300, delay_max_ms=100)
    with pytest.raises(ValueError):
        RailConfig(dup_mode="otro")  # type: ignore[arg-type]
