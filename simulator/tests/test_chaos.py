"""Informe y decisión de éxito del subcomando chaos, con un cliente HTTP falso (sin servicios)."""

from __future__ import annotations

import random
import threading
import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from verabank_sim.chaos import cli as chaos_cli
from verabank_sim.chaos.report import Report, evaluate, format_report
from verabank_sim.chaos.runner import (
    InsufficientBudget,
    allocate_sources,
    build_plan,
    make_amounts,
    run_chaos,
    seed_account_id,
)

NOW = datetime(2026, 11, 16, 10, 0, tzinfo=UTC)


class FakeApi:
    """Imita transfer-service: idempotencia por clave y estados que avanzan con cada consulta."""

    def __init__(self, fail_every: int = 0, stuck: int = 0, replay_header: bool = True, break_reuse: bool = False,
                 polls_to_final: int = 1) -> None:
        self.fail_every, self.stuck, self.replay_header = fail_every, stuck, replay_header
        self.break_reuse, self.polls_to_final = break_reuse, polls_to_final
        self.by_key: dict[str, tuple[dict, str]] = {}
        self.polls: dict[str, int] = {}
        self.order: list[str] = []
        self.lock = threading.Lock()

    def post_transfer(self, key, body):
        with self.lock:
            if key in self.by_key:
                first, tid = self.by_key[key]
                if first != body:
                    if self.break_reuse:
                        return 201, {}, {"transfer_id": str(uuid.uuid4())}
                    return 422, {}, {"type": "idempotency-key-reuse"}
                return 200, ({"idempotent-replayed": "true"} if self.replay_header else {}), {"transfer_id": tid}
            tid = str(uuid.uuid4())
            self.by_key[key] = (body, tid)
            self.order.append(tid)
            return 201, {}, {"transfer_id": tid, "status": "PROCESSING"}

    def get_transfer(self, tid):
        with self.lock:
            seen = self.polls[tid] = self.polls.get(tid, 0) + 1
            position = self.order.index(tid)
        if position < self.stuck:
            return 200, {}, {"status": "PROCESSING"}
        if seen < self.polls_to_final:
            return 200, {}, {"status": "PROCESSING"}
        failed = self.fail_every and position % self.fail_every == 0
        return 200, {}, {"status": "FAILED" if failed else "COMPLETED"}

    def get_account(self, account_id):
        return 200, {}, {"available": "1000.00"}


def make_plan(n=200, dup=0.05, reuse=0.01, seed=1):
    rng = random.Random(seed)
    amounts = make_amounts(n, rng)
    sources = allocate_sources({seed_account_id(19): Decimal("100000")}, amounts)
    return build_plan(n, dup, reuse, sources, amounts, rng, NOW)


def run(api, plan, reject, timeout=30.0):
    ticks = iter(range(10_000))
    return run_chaos(api, plan, reject, concurrency=4, timeout_s=timeout, poll_interval_s=0,
                     clock=lambda: float(next(ticks)), sleep=lambda s: None)


# ---------------------------------------------------------------- plan

def test_el_plan_reparte_replays_y_reusos_disjuntos_y_con_claves_unicas():
    plan = make_plan(n=2000)
    assert sum(p.replay for p in plan) == 100          # 5 %
    assert sum(p.reuse for p in plan) == 20            # 1 %
    assert not any(p.replay and p.reuse for p in plan)
    assert len({p.idempotency_key for p in plan}) == 2000
    assert all(p.body["rail"] == "EXTERNAL" and p.body["dest_external_ref"] for p in plan)


def test_los_montos_van_como_cadena_decimal_de_dos_cifras():
    for p in make_plan(n=50):
        assert p.body["amount"].count(".") == 1 and len(p.body["amount"].split(".")[1]) == 2
        assert Decimal("1.00") <= Decimal(p.body["amount"]) <= Decimal("10.00")


def test_el_reparto_no_supera_el_presupuesto_de_ninguna_cuenta():
    amounts = [Decimal("10.00")] * 100
    avail = {"a": Decimal("500"), "b": Decimal("500"), "c": Decimal("500")}
    chosen = allocate_sources(avail, amounts)
    for acct in avail:
        assert sum(a for a, c in zip(amounts, chosen, strict=True) if c == acct) <= avail[acct] * Decimal("0.9")


def test_sin_saldo_suficiente_se_avisa_en_vez_de_fallar_por_saldo():
    with pytest.raises(InsufficientBudget):
        allocate_sources({"a": Decimal("50")}, [Decimal("10.00")] * 10)


def test_ids_de_cuentas_semilla_coinciden_con_el_sql():
    # Valor de select md5('verabank-seed:account:1')::uuid en PostgreSQL
    assert seed_account_id(1) == "dccd6995-ec89-042c-3ebc-0a86fc644c87"


# ---------------------------------------------------------------- ejecución e informe

def test_caso_feliz_pasa_con_failed_cerca_del_reject():
    plan = make_plan(n=200)
    report = run(FakeApi(fail_every=5), plan, reject=0.2)       # 20 % FAILED
    assert report.created == 200
    assert (report.replays_ok, report.replays_expected) == (10, 10)
    assert (report.reuse_ok, report.reuse_expected) == (2, 2)
    assert report.states["FAILED"] == 40 and report.states["COMPLETED"] == 160
    assert evaluate(report) == []
    assert "OK" in format_report(report, [])


def test_failed_a_tres_puntos_o_mas_del_reject_falla():
    report = run(FakeApi(fail_every=0), make_plan(n=200), reject=0.2)   # 0 % FAILED frente a 20 %
    assert any("puntos del REJECT" in p for p in evaluate(report))
    report = run(FakeApi(fail_every=5), make_plan(n=200), reject=0.0)   # 20 % FAILED frente a 0 %
    assert any("puntos del REJECT" in p for p in evaluate(report))


def test_failed_dentro_de_la_tolerancia_pasa():
    report = run(FakeApi(fail_every=0), make_plan(n=200), reject=0.02)  # 0 % frente a 2 %
    assert evaluate(report) == []


def test_transferencias_que_no_llegan_a_estado_final_hacen_fallar():
    report = run(FakeApi(stuck=3), make_plan(n=100), reject=0.0, timeout=5)
    assert len(report.non_final) == 3
    assert any("sin estado final" in p for p in evaluate(report))
    assert "FALLO" in format_report(report, evaluate(report))


def test_espera_varias_vueltas_hasta_el_estado_final():
    report = run(FakeApi(polls_to_final=3), make_plan(n=50), reject=0.0)
    assert report.non_final == [] and evaluate(report) == []


def test_replay_sin_cabecera_idempotent_replayed_falla():
    report = run(FakeApi(replay_header=False), make_plan(n=200), reject=0.0)
    assert report.replays_ok == 0
    assert any("reenvíos idempotentes" in p for p in evaluate(report))


def test_reuso_de_clave_sin_422_falla():
    report = run(FakeApi(break_reuse=True), make_plan(n=200), reject=0.0)
    assert any("reutilizaciones de clave" in p for p in evaluate(report))


def test_una_creacion_sin_201_hace_fallar():
    class Rejecting(FakeApi):
        def post_transfer(self, key, body):
            if body["dest_external_ref"] == "EXT-000003":
                return 422, {}, {"type": "insufficient-funds"}
            return super().post_transfer(key, body)

    report = run(Rejecting(), make_plan(n=50, dup=0, reuse=0), reject=0.0)
    problems = evaluate(report)
    assert report.created == 49 and any("sin 201" in p for p in problems)


def test_rejected_inesperado_hace_fallar():
    report = Report(n=10, reject_rate=0.0, created=10)
    report.states.update({"COMPLETED": 9, "REJECTED": 1})
    assert any("REJECTED" in p for p in evaluate(report))


def test_la_excepcion_de_red_no_tumba_el_informe():
    class Boom(FakeApi):
        def post_transfer(self, key, body):
            raise ConnectionError("x")

    report = run(Boom(), make_plan(n=20), reject=0.0)
    assert report.created == 0 and evaluate(report)


# ---------------------------------------------------------------- CLI

def test_chaos_exige_la_clave_en_el_entorno(monkeypatch):
    monkeypatch.delenv("VERABANK_API_KEYS", raising=False)
    with pytest.raises(SystemExit):
        chaos_cli.api_key_from_env()
    monkeypatch.setenv("VERABANK_API_KEYS", "web:abc123,mobile:def")
    assert chaos_cli.api_key_from_env() == "abc123"


def test_chaos_defaults_del_enunciado():
    from verabank_sim import cli

    args = cli.build_parser().parse_args(["chaos"])
    assert (args.n, args.concurrency, args.timeout) == (2000, 20, 180.0)
    assert (args.replay_rate, args.reuse_rate) == (0.05, 0.01)


def test_run_devuelve_codigo_de_salida_segun_el_informe(monkeypatch):
    from verabank_sim import cli

    monkeypatch.setenv("VERABANK_API_KEYS", "web:k")
    fake = FakeApi(fail_every=5)

    class Adapter:
        def __init__(self, *a, **k): ...
        post_transfer = staticmethod(lambda *a: fake.post_transfer(*a))
        get_transfer = staticmethod(lambda *a: fake.get_transfer(*a))
        get_account = staticmethod(lambda *a: fake.get_account(*a))
        close = staticmethod(lambda: None)

    monkeypatch.setattr(chaos_cli, "HttpApi", Adapter)
    ok = cli.build_parser().parse_args(["chaos", "--n", "100", "--reject-rate", "0.2", "--seed", "3"])
    assert chaos_cli.run(ok) == 0
    fake.fail_every = 0                       # ahora 0 % FAILED frente al 20 % pedido
    bad = cli.build_parser().parse_args(["chaos", "--n", "100", "--reject-rate", "0.2", "--seed", "4"])
    assert chaos_cli.run(bad) == 1
