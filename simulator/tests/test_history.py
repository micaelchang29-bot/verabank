"""Calentamiento dirigido de historia (ADR-13): una fila por destino del lote en su primera aparición de PaySim."""

from __future__ import annotations

import csv
import importlib.util
import io
import re
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from tests.test_replay import EPOCH, FakeApi, FakeLoader, FakePublisher, FakeState, TEST, TYPES, write_csv
from verabank_sim.replay import history, mapping as m, warmup
from verabank_sim.replay.paysim import select_rows
from verabank_sim.replay.run import Options, Services, run

ROOT = Path(__file__).resolve().parents[2]
ANCHOR = 577                      # primer paso del lote: el calentamiento de 24 pasos es 553 a 576, la historia es < 553

# (step, type, amount, orig, old_balance, dest, fraud)
ROWS = [
    (10, "TRANSFER", "5.00", "A1", "100", "D1", 0),     # primera aparición de D1 (paso 10)
    (12, "CASH_IN", "7.00", "A2", "100", "D2", 0),      # CASH_IN: no cuenta como aparición de D2
    (20, "PAYMENT", "9.00", "A3", "100", "D2", 0),      # primera aparición de D2 (paso 20)
    (30, "CASH_OUT", "0.00", "A4", "100", "D3", 0),     # monto 0: el entrenamiento la cuenta; primera de D3 (paso 30)
    (40, "TRANSFER", "1.00", "A5", "100", "D1", 0),     # segunda aparición de D1: no genera otra fila
    (552, "TRANSFER", "2.00", "A6", "100", "D4", 0),    # D4 aparece en el paso 552 = 553 - 1: tiene historia
    (553, "TRANSFER", "3.00", "A7", "100", "D5", 0),    # D5 aparece dentro del calentamiento: sin fila de historia
    (560, "CASH_IN", "1.00", "A8", "100", "D6", 0),     # D6 solo como CASH_IN antes del lote: sin historia
    (577, "TRANSFER", "10.00", "A1", "1000", "D1", 0),  # lote (pasos 577 y 578)
    (577, "TRANSFER", "11.00", "A9", "1000", "D2", 0),
    (577, "PAYMENT", "12.00", "A9", "1000", "D3", 0),
    (577, "TRANSFER", "13.00", "A9", "1000", "D4", 0),
    (578, "TRANSFER", "14.00", "A9", "1000", "D5", 0),
    (578, "TRANSFER", "15.00", "A9", "1000", "D6", 0),
    (578, "TRANSFER", "16.00", "A9", "1000", "D7", 0),  # destino nuevo: sin historia
    (579, "TRANSFER", "17.00", "A9", "1000", "D1", 1),  # fuera del lote de dos pasos
]


@pytest.fixture
def paysim(tmp_path: Path) -> Path:
    return write_csv(tmp_path / "paysim.csv", ROWS)


def batch(paysim: Path, first_step: int = ANCHOR, steps: int = 2):
    return select_rows(paysim, types=TYPES, test_steps=(TEST[0], TEST[1]), steps=steps, first_step=first_step)


def candidates(paysim: Path, sel, anchor: int = ANCHOR) -> list[m.Row]:
    return history.first_appearances(paysim, types=TYPES, dests={r.name_dest for r in sel.rows}, before_step=anchor - 24)


def test_only_batch_destinations_with_prior_history_get_a_row_at_their_first_appearance(paysim: Path) -> None:
    sel = batch(paysim)
    rows = candidates(paysim, sel)
    assert [(r.name_dest, r.step, r.name_orig, r.type) for r in rows] == [
        ("D1", 10, "A1", "TRANSFER"), ("D2", 20, "A3", "PAYMENT"), ("D3", 30, "A4", "CASH_OUT"), ("D4", 552, "A6", "TRANSFER")]
    names = {r.name_dest for r in rows}
    assert not names & {"D5", "D6", "D7"}                     # D5 en el calentamiento, D6 solo CASH_IN, D7 nuevo
    assert "CASH_IN" not in {r.type for r in rows}
    assert all(r.step < ANCHOR - 24 for r in rows)


def test_a_destination_outside_the_batch_is_never_loaded(paysim: Path) -> None:
    rows = history.first_appearances(paysim, types=TYPES, dests={"D1"}, before_step=ANCHOR - 24)
    assert [r.name_dest for r in rows] == ["D1"]


def test_cash_in_only_destinations_have_no_history(paysim: Path) -> None:
    assert history.first_appearances(paysim, types=TYPES, dests={"D6"}, before_step=ANCHOR) == []


def test_the_record_is_a_warmup_row_at_the_first_appearance_with_its_own_deterministic_id(paysim: Path) -> None:
    sel = batch(paysim)
    d2 = next(r for r in candidates(paysim, sel) if r.name_dest == "D2")
    record = history.history_record(d2, EPOCH)
    assert record[1:] == [m.account_id("A3"), "PS-D2", "9.00", m.occurred_at(EPOCH, 20), "{}", "0", "APPROVE", "warmup"]
    assert record[0] == history.history_id("D2") == history.history_record(d2, EPOCH)[0]
    assert record[0] != warmup.warmup_id(d2) and history.history_id("D2") != history.history_id("D3")
    assert m.occurred_at(EPOCH, 20) < m.occurred_at(EPOCH, ANCHOR - 24)


def test_amount_factor_scales_the_history_amount_like_the_warmup(paysim: Path) -> None:
    rows = history.first_appearances(paysim, types=TYPES, dests={"D2"}, before_step=ANCHOR, factor=Decimal(3))
    assert rows[0].amount == Decimal("27.00")


class FakeScoringLog:
    """Un ``scoring_log`` mínimo: ``sim_loader`` inserta con COPY (o INSERT) y el rol de lectura consulta."""

    def __init__(self) -> None:
        self.rows: dict[str, tuple[str, str]] = {}        # transfer_id -> (dest_ref, occurred_at)
        self.copies = 0

    def add(self, transfer_id: str, dest_ref: str, occurred_at: str) -> None:
        self.rows.setdefault(transfer_id, (dest_ref, occurred_at))     # ON CONFLICT DO NOTHING

    def run(self, stdin: str, *args: str, timeout_s: float = 0) -> str:
        if args[:1] == ("-c",):
            self.copies += 1
            for rec in csv.reader(io.StringIO(stdin)):
                if rec[0] in self.rows:
                    raise RuntimeError('ERROR: duplicate key value violates unique constraint "scoring_log_pkey"')
            for rec in csv.reader(io.StringIO(stdin)):
                self.add(rec[0], rec[2], rec[4])
        else:
            for tid, dest, at in re.findall(r"\('([0-9a-f-]{36})', '[^']*', '([^']*)', '[^']*', '([^']*)'", stdin):
                self.add(tid, dest, at)
        return ""

    def query(self, sql: str) -> list[list[str]]:
        if sql.startswith("SELECT count(*)"):                   # filas reales previas (warmup.real_rows_before): ninguna
            return [["0"]]
        names = set(re.findall(r"'(PS-[^']+)'", sql))
        first: dict[str, str] = {}
        for dest, at in self.rows.values():
            if dest in names and (dest not in first or at < first[dest]):
                first[dest] = at
        return [[d, at] for d, at in first.items()]


def plan_for(paysim: Path, log: FakeScoringLog, first_step: int = ANCHOR, steps: int = 2) -> history.Plan:
    sel = batch(paysim, first_step, steps)
    return history.plan(candidates(paysim, sel, first_step), sel.rows, EPOCH, log)


def test_plan_counts_and_load_is_idempotent(paysim: Path) -> None:
    log = FakeScoringLog()
    first = plan_for(paysim, log)
    assert (first.batch_dests, first.with_history, first.already_loaded, len(first.rows)) == (7, 4, 0, 4)
    assert first.line(ANCHOR) == ("historia: 7 destinos en el lote; 4 con historia previa en PaySim (antes del paso 553); "
                                  "0 ya cargados; 4 filas de historia a cargar")
    assert history.load(log, first.rows, EPOCH) == (4, "copy") and len(log.rows) == 4
    again = plan_for(paysim, log)
    assert (again.with_history, again.already_loaded, again.rows) == (4, 4, [])
    assert history.load(log, first.rows, EPOCH) == (4, "insert") and len(log.rows) == 4     # reintento: ON CONFLICT DO NOTHING
    assert log.copies == 2


def test_chained_batches_do_not_duplicate_history(paysim: Path, tmp_path: Path) -> None:
    log = FakeScoringLog()
    plan = plan_for(paysim, log)
    history.load(log, plan.rows, EPOCH)
    for r in batch(paysim).rows:                         # la tanda 577-578 ya quedó puntuada
        log.add(f"real-{r.row_id}", m.dest_ref(r), m.occurred_at(EPOCH, r.step))
    chained = plan_for(paysim, log, first_step=579, steps=1)       # D1 vuelve en la tanda siguiente
    assert chained.with_history >= 1 and chained.rows == [] and chained.already_loaded == chained.with_history


def test_a_destination_whose_only_earlier_rows_are_later_than_its_first_appearance_still_gets_history(paysim: Path) -> None:
    log = FakeScoringLog()
    log.add("w", "PS-D2", m.occurred_at(EPOCH, 560))               # solo una fila de calentamiento, posterior al paso 20
    plan = plan_for(paysim, log)
    assert "PS-D2" in {m.dest_ref(r) for r in plan.rows} and plan.already_loaded == 0


def test_history_rows_stay_outside_the_one_and_twenty_four_hour_windows(paysim: Path) -> None:
    sel = batch(paysim)
    plan = history.plan(candidates(paysim, sel), sel.rows, EPOCH, None)
    for h in plan.rows:
        at = datetime.fromisoformat(m.occurred_at(EPOCH, h.step).replace("Z", "+00:00"))
        for r in sel.rows:
            t = datetime.fromisoformat(m.occurred_at(EPOCH, r.step).replace("Z", "+00:00"))
            assert at < t - timedelta(hours=24)


# --- ejecución: --no-history-warmup y el resumen -----------------------------------------------------------------


def services(log: FakeLoader | FakeScoringLog, reader=None) -> Services:
    return Services(api=FakeApi(), state_psql=FakeState(), loader_psql=log, publisher=FakePublisher(), client_id="web",
                    reader_psql=reader)


def with_history(paysim: Path):
    sel = batch(paysim)
    from dataclasses import replace
    return replace(sel, history=candidates(paysim, sel))


def test_run_loads_the_history_before_sending_and_reports_it(paysim: Path, capsys) -> None:
    log = FakeScoringLog()
    summary = run(Options(tps=1000, final_timeout_s=1), with_history(paysim), EPOCH, services(log, reader=log), None)
    assert summary["history_rows"] == 4 and summary["history_method"] == "copy" and summary["history_with_prior"] == 4
    assert summary["history_dests"] == 7 and summary["history_already_loaded"] == 0
    assert "4 filas de historia a cargar" in capsys.readouterr().out
    assert {dest for dest, _ in log.rows.values() if dest in {"PS-D1", "PS-D2", "PS-D3", "PS-D4"}} == {
        "PS-D1", "PS-D2", "PS-D3", "PS-D4"}
    rerun = run(Options(tps=1000, final_timeout_s=1), with_history(paysim), EPOCH, services(log, reader=log), None)
    assert rerun["history_rows"] == 0 and rerun["history_already_loaded"] == 4


def test_no_history_warmup_loads_nothing(paysim: Path) -> None:
    log = FakeScoringLog()
    summary = run(Options(tps=1000, history_warmup=False, warmup=False, final_timeout_s=1), with_history(paysim), EPOCH,
                  services(log, reader=log), None)
    assert summary["history_rows"] == 0 and summary["history_method"] is None and log.rows == {} and log.copies == 0
    loader = FakeLoader()
    run(Options(tps=1000, history_warmup=False, final_timeout_s=1), with_history(paysim), EPOCH, services(loader), None)
    assert all("history" not in str(c) for c in loader.calls) and len(loader.calls) == 1      # solo el calentamiento de 24 pasos


# --- paridad con la implementación de referencia -----------------------------------------------------------------


def load_reference():
    spec = importlib.util.spec_from_file_location("ml_reference", ROOT / "ml/src/verabank_ml/reference.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.reference_features


def service_view(log: list[tuple[str, datetime]], dest: str, t: datetime) -> tuple[float, float]:
    """``dst_seen_before`` y ``dst_age_hours`` como los calcula risk-service (FeatureQueries y FeatureComputer)."""
    seen = [at for d, at in log if d == dest and at < t]
    if not seen:
        return 0.0, 0.0
    first = min(seen)
    hours = lambda x: x.replace(minute=0, second=0, microsecond=0)  # noqa: E731
    return 1.0, float(int((hours(t) - hours(first)).total_seconds() // 3600))


def parity_log(paysim: Path, sel, with_history_rows: bool) -> list[tuple[str, datetime]]:
    parse = lambda step: datetime.fromisoformat(m.occurred_at(EPOCH, step).replace("Z", "+00:00"))  # noqa: E731
    log = [(m.dest_ref(r), parse(r.step)) for r in sel.warmup]
    if with_history_rows:
        log += [(m.dest_ref(r), parse(r.step)) for r in candidates(paysim, sel)]
    return log


@pytest.mark.parametrize("steps", [2, 3])
def test_service_features_match_the_reference_implementation_with_history_and_warmup(paysim: Path, steps: int) -> None:
    sel = batch(paysim, steps=steps)
    raw = [{"step": int(s), "type": t, "amount": float(a), "nameOrig": o, "oldbalanceOrg": float(b), "nameDest": d}
           for s, t, a, o, b, d, _ in ROWS]
    batch_ids = {r.row_id for r in sel.rows}
    expected = {f["row_id"]: f for f in load_reference()(raw, "A", TYPES, ["TRANSFER", "CASH_OUT", "PAYMENT", "DEBIT"],
                                                          targets=sorted(batch_ids))}
    log = parity_log(paysim, sel, True)
    for r in sel.rows:
        t = datetime.fromisoformat(m.occurred_at(EPOCH, r.step).replace("Z", "+00:00"))
        seen, age = service_view(log, m.dest_ref(r), t)
        assert (seen, age) == (expected[r.row_id]["dst_seen_before"], float(expected[r.row_id]["dst_age_hours"])), r
        log.append((m.dest_ref(r), t))                       # la fila queda en el log al puntuarla


def test_without_the_history_rows_the_parity_is_lost(paysim: Path) -> None:
    """El desajuste de 4/D1: sin historia, D1 a D4 parecen nuevos."""
    sel = batch(paysim)
    t = datetime.fromisoformat(m.occurred_at(EPOCH, 577).replace("Z", "+00:00"))
    assert service_view(parity_log(paysim, sel, False), "PS-D1", t) == (0.0, 0.0)
    assert service_view(parity_log(paysim, sel, True), "PS-D1", t) == (1.0, 567.0)
