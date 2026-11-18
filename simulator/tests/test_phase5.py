"""Fase 5 (5.B): replay por pasos, tandas encadenadas, ``settle`` y ``reset-data`` (sin servicios)."""

from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx
import yaml

from verabank_sim import cli
from verabank_sim.load.api import HttpApi, Reply
from verabank_sim.replay import mapping as m, warmup
from verabank_sim.replay.cli import resolve_mode
from verabank_sim.replay.paysim import select_rows
from verabank_sim.replay.run import Options, Services, run
from verabank_sim.settle import duration, settle

from .test_replay import FakePublisher, write_csv

ROOT = Path(__file__).resolve().parents[2]
ML = yaml.safe_load((ROOT / "ml" / "config.yaml").read_text())
EPOCH = m.parse_epoch(ML["sim_epoch"])
TYPES = ML["types"]
TEST = tuple(ML["split"]["test"])
BASE = "http://transfer.test"


@pytest.fixture
def paysim(tmp_path: Path) -> Path:
    rows = []
    for step in (552, 553, 575, 576, 577, 578, 600, 601, 612, 624, 625, 648, 672, 673, 700):
        rows.append((step, "TRANSFER", f"{step}.00", f"C{step}", "1000", f"D{step}", 0))
        rows.append((step, "PAYMENT", f"{step}.50", f"C{step}b", "1000", f"M{step}", int(step == 600)))
    rows.append((577, "CASH_IN", "5.00", "C1", "1", "C2", 0))      # nunca entra
    rows.append((600, "DEBIT", "0.00", "C3", "1", "C4", 0))        # monto 0: no es transferencia
    rows.sort(key=lambda r: r[0])
    return write_csv(tmp_path / "paysim.csv", rows)


def steps_of(sel) -> list[int]:
    return sorted({r.step for r in sel.rows})


# --- --steps y --first-step ------------------------------------------------------------------------------------------


def test_steps_takes_exactly_the_requested_steps_from_the_first_step(paysim: Path) -> None:
    sel = select_rows(paysim, types=TYPES, test_steps=TEST, steps=24, first_step=577)
    assert steps_of(sel) == [577, 578, 600]
    assert len(sel.rows) == 6 and sel.start_step == 577 and sel.skipped_zero_amount == 1
    assert {r.step for r in sel.warmup} == {553, 575, 576}          # pasos 553 a 576, ni 552 ni la tanda
    nxt = select_rows(paysim, types=TYPES, test_steps=TEST, steps=48, first_step=625)
    assert steps_of(nxt) == [625, 648, 672]                         # 625 a 672: el 673 queda fuera
    assert {r.step for r in nxt.warmup} == {601, 612, 624}


def test_steps_defaults_to_the_start_of_the_test_partition_and_stays_inside_it(paysim: Path) -> None:
    assert steps_of(select_rows(paysim, types=TYPES, test_steps=TEST, steps=2)) == [575, 576]
    assert steps_of(select_rows(paysim, types=TYPES, test_steps=TEST, steps=500, first_step=700)) == [700]
    for bad in (dict(steps=1, first_step=574), dict(steps=1, first_step=745), dict(steps=1, start_row=3),
                dict(steps=1, n=5), dict()):
        with pytest.raises(ValueError):
            select_rows(paysim, types=TYPES, test_steps=TEST, **bad)


def test_window_boundaries_576_577_and_600_601(paysim: Path) -> None:
    w24 = select_rows(paysim, types=TYPES, test_steps=TEST, steps=24, first_step=577)
    w25 = select_rows(paysim, types=TYPES, test_steps=TEST, steps=24, first_step=601)
    assert 576 not in steps_of(w24) and 600 in steps_of(w24)
    assert 600 not in steps_of(w25) and 601 in steps_of(w25)


def test_cli_steps_is_exclusive_with_n_and_first_step_needs_steps() -> None:
    parser = cli.build_parser()
    args = parser.parse_args(["replay", "--steps", "3", "--first-step", "577", "--amount-factor", "3"])
    assert resolve_mode(args) == (None, 3, 577) and str(args.amount_factor) == "3"
    assert resolve_mode(parser.parse_args(["replay", "--steps", "48"])) == (None, 48, 577)
    assert resolve_mode(parser.parse_args(["replay"])) == (20000, None, None)
    with pytest.raises(SystemExit):
        parser.parse_args(["replay", "--n", "10", "--steps", "3"])
    for argv in (["replay", "--first-step", "577"], ["replay", "--steps", "3", "--start-row", "5"]):
        with pytest.raises(SystemExit):
            resolve_mode(parser.parse_args(argv))


# --- tandas encadenadas ----------------------------------------------------------------------------------------------


class ScoringLog:
    """``risk.scoring_log`` en memoria: los servicios insertan filas reales, el calentamiento las suyas."""

    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}

    def insert(self, transfer_id: str, occurred_at: str, model_version: str) -> None:
        self.rows.setdefault(transfer_id, {"occurred_at": occurred_at, "model_version": model_version})  # DO NOTHING


class Loader:
    """``sim_loader``: ``COPY`` (o el INSERT de reserva) en el registro en memoria."""

    def __init__(self, log: ScoringLog) -> None:
        self.log, self.calls = log, 0

    def run(self, stdin: str, *args: str, timeout_s: float = 0) -> str:
        self.calls += 1
        for line in stdin.splitlines():
            fields = line.split(",")
            self.log.insert(fields[0], fields[4], fields[-1])
        return ""


class Reader:
    """Rol de BI: cuenta las filas reales en el rango que pide ``warmup.real_rows_sql``."""

    def __init__(self, log: ScoringLog) -> None:
        self.log, self.sql = log, []

    def query(self, sql: str) -> list[list[str]]:
        self.sql.append(sql)
        lo, hi = re.findall(r"'([^']+)'", sql)[1:3]
        n = sum(1 for r in self.log.rows.values() if r["model_version"] != "warmup" and lo <= _iso(r["occurred_at"]) < hi)
        return [[str(n)]]


def _iso(text: str) -> str:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).isoformat()


class Service:
    """transfer-service + risk-scoring: cada POST deja una fila real en ``scoring_log``."""

    def __init__(self, log: ScoringLog) -> None:
        self.log, self.n = log, 0

    def post_transfer(self, key: str, body: dict) -> Reply:
        self.n += 1
        self.log.insert(f"real-{key}", body["occurred_at"], "fraud-xgb/6")
        return Reply(201, {}, {"status": "COMPLETED"}, 1.0)


class State:
    def run(self, stdin: str, *args: str, timeout_s: float = 0) -> str:
        return "0|0|0|0\n"

    def query(self, sql: str) -> list[list[str]]:
        keys = re.findall(r"'([0-9a-f-]{36})'", sql)
        return [[k, f"{i:08d}-0000-4000-8000-000000000000", "COMPLETED", "APPROVE", "MODEL", "5", ""]
                for i, k in enumerate(keys)]


def batch(paysim: Path, log: ScoringLog, first_step: int, steps: int):
    sel = select_rows(paysim, types=TYPES, test_steps=TEST, steps=steps, first_step=first_step)
    loader, reader = Loader(log), Reader(log)
    services = Services(api=Service(log), state_psql=State(), loader_psql=loader, publisher=FakePublisher(),
                        client_id="web", reader_psql=reader)
    summary = run(Options(tps=1000, seed=False, labels=False, final_timeout_s=1, steps=steps, first_step=first_step),
                  sel, EPOCH, services, None)
    return summary, loader, reader


def test_chained_batches_do_not_duplicate_or_overwrite_and_skip_the_second_warmup(paysim: Path) -> None:
    log = ScoringLog()
    first, loader1, _ = batch(paysim, log, 577, 48)
    assert first["warmup_method"] == "copy" and first["warmup_rows"] == 6 and loader1.calls == 1
    snapshot = json.dumps(log.rows, sort_keys=True)
    real_before = {k for k, v in log.rows.items() if v["model_version"] != "warmup"}

    second, loader2, reader2 = batch(paysim, log, 625, 48)
    assert loader2.calls == 0 and second["warmup_rows"] == 0 and second["warmup_method"] == "skipped"
    assert second["warmup_skipped_real_rows"] == 6                   # pasos 601, 612 y 624 de la primera tanda
    assert "'2026-01-26T00:00:00+00:00'" in reader2.sql[0] and "'2026-01-27T00:00:00+00:00'" in reader2.sql[0]
    # Las filas de la primera tanda siguen idénticas; la segunda solo añade.
    after = json.loads(json.dumps(log.rows, sort_keys=True))
    assert {k: after[k] for k in json.loads(snapshot)} == json.loads(snapshot)
    assert real_before < {k for k, v in log.rows.items() if v["model_version"] != "warmup"}
    assert len(log.rows) == 6 + 12 + 6                               # calentamiento + 1.ª tanda + 2.ª tanda


def test_without_real_history_the_warmup_is_loaded_even_in_a_later_batch(paysim: Path) -> None:
    summary, loader, _ = batch(paysim, ScoringLog(), 625, 48)
    assert loader.calls == 1 and summary["warmup_rows"] == 6


def test_the_fallback_insert_never_updates_existing_rows() -> None:
    row = m.Row(1, 600, "TRANSFER", m.scale_amount("10", 1), "C1", m.scale_amount("1", 1), "C2", False)
    sql = warmup.insert_sql([row], EPOCH)
    assert sql.rstrip().endswith("ON CONFLICT DO NOTHING;") and "UPDATE" not in sql.upper()
    assert warmup.warmup_id(row) != m.idempotency_key(row)             # nunca el id de una fila real


def test_real_rows_window_is_the_24_steps_before_the_first() -> None:
    sql = warmup.real_rows_sql(EPOCH, 625)
    start = (EPOCH + timedelta(hours=600)).isoformat()
    end = (EPOCH + timedelta(hours=624)).isoformat()
    assert f">= '{start}'" in sql and f"< '{end}'" in sql and "model_version <> 'warmup'" in sql


# --- settle ----------------------------------------------------------------------------------------------------------


class ViewReader:
    """``analytics.v_transfer_outcomes`` leída por el rol BI: una secuencia de estados por consulta."""

    def __init__(self, states: dict[str, str], advance=None) -> None:
        self.states, self.advance = states, advance

    def query(self, sql: str) -> list[list[str]]:
        if "GROUP BY status" in sql:
            if self.advance:
                self.advance(self.states)
            counts: dict[str, int] = {}
            for s in self.states.values():
                counts[s] = counts.get(s, 0) + 1
            return [[s, str(n)] for s, n in sorted(counts.items())]
        return [[t] for t, s in sorted(self.states.items()) if s == "IN_REVIEW"]


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += s


def finish_one(states: dict[str, str]) -> None:
    for tid, s in states.items():
        if s in ("PENDING", "PROCESSING"):
            states[tid] = "COMPLETED"
            return


def test_settle_ends_when_nothing_is_open() -> None:
    states = {"t1": "PENDING", "t2": "PROCESSING", "t3": "COMPLETED", "t4": "IN_REVIEW"}
    clock = Clock()
    result = settle(ViewReader(states, finish_one), None, approve_reviews=False, timeout_s=60, poll_s=5,
                    clock=clock, sleep=clock.sleep)
    assert result.ok and result.counts == {"COMPLETED": 3, "IN_REVIEW": 1} and result.approved == 0
    assert clock.t == 5.0                     # dos sondeos: el segundo ya no encuentra abiertas


@respx.mock(base_url=BASE)
def test_settle_approves_only_in_review_through_the_api(respx_mock) -> None:
    states = {"a": "IN_REVIEW", "b": "COMPLETED", "c": "REJECTED", "d": "IN_REVIEW", "e": "PROCESSING"}

    def review(request: httpx.Request, tid: str) -> httpx.Response:
        assert json.loads(request.content) == {"decision": "APPROVE", "comment": "settle"}
        assert states[tid] == "IN_REVIEW"
        states[tid] = "PROCESSING"
        return httpx.Response(200, json={"transfer_id": tid, "status": "PROCESSING"})

    route = respx_mock.post(url__regex=r"/api/v1/transfers/(?P<tid>[^/]+)/review").mock(side_effect=review)
    api = HttpApi(BASE, "k" * 24, 2)
    clock = Clock()
    result = settle(ViewReader(states, finish_one), api, approve_reviews=True, timeout_s=120, poll_s=5,
                    clock=clock, sleep=clock.sleep)
    api.close()
    assert result.ok and result.approved == 2 and route.call_count == 2
    assert sorted(c.request.url.path for c in route.calls) == ["/api/v1/transfers/a/review", "/api/v1/transfers/d/review"]
    assert all(c.request.headers["X-API-Key"] == "k" * 24 for c in route.calls)
    assert set(states.values()) == {"COMPLETED", "REJECTED"}


@respx.mock(base_url=BASE)
def test_settle_counts_a_review_already_decided(respx_mock) -> None:
    states = {"a": "IN_REVIEW"}

    def conflict(request: httpx.Request) -> httpx.Response:
        states["a"] = "COMPLETED"           # otro analista la decidió entre la consulta y la llamada
        return httpx.Response(409, json={"type": "review-not-pending"})

    respx_mock.post("/api/v1/transfers/a/review").mock(side_effect=conflict)
    api = HttpApi(BASE, "k" * 24, 1)
    result = settle(ViewReader(states), api, approve_reviews=True, timeout_s=10, poll_s=1)
    api.close()
    assert result.ok and result.approved == 0 and result.already_decided == 1


def test_settle_fails_when_the_deadline_passes() -> None:
    clock = Clock()
    result = settle(ViewReader({"t1": "PROCESSING"}), None, approve_reviews=False, timeout_s=12, poll_s=5,
                    clock=clock, sleep=clock.sleep)
    assert not result.ok and result.counts == {"PROCESSING": 1} and clock.t == 12.0


def test_settle_cli_exit_code_and_durations() -> None:
    assert (duration("15m"), duration("5s"), duration("1h"), duration("500ms"), duration("7")) == (900, 5, 3600, 0.5, 7)
    args = cli.build_parser().parse_args(["settle", "--approve-reviews", "--timeout", "2m", "--poll", "1s"])
    assert args.approve_reviews and args.timeout == 120 and args.poll == 1
    for bad in (["settle", "--timeout", "x"], ["settle", "--poll", "0s"]):
        with pytest.raises(SystemExit):
            cli.build_parser().parse_args(bad)


# --- reset-data ------------------------------------------------------------------------------------------------------


def fake_docker(tmp_path: Path) -> tuple[dict, Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "docker-calls.log"
    script = bin_dir / "docker"
    script.write_text(f"""#!/bin/sh
echo "$@" >> {calls}
if [ "$1" = volume ]; then printf 'verabank_postgres-data\\nverabank_kafka-data\\n'; fi
""")
    script.chmod(0o755)
    env = {"PATH": f"{bin_dir}:{os.environ['PATH']}", "HOME": os.environ.get("HOME", "/tmp")}
    return env, calls


@pytest.mark.parametrize("confirm", [None, "", "no", "YES", "y"])
def test_reset_data_without_confirm_yes_runs_nothing(tmp_path: Path, confirm) -> None:
    env, calls = fake_docker(tmp_path)
    if confirm is not None:
        env["CONFIRM"] = confirm
    done = subprocess.run([str(ROOT / "scripts" / "reset-data.sh")], env=env, capture_output=True, text=True)
    assert done.returncode != 0
    assert "verabank_postgres-data" in done.stderr and "CONFIRM=yes" in done.stderr
    lines = calls.read_text().splitlines()
    assert lines and all(line.startswith("volume ls") for line in lines)   # solo lista; nunca down


def test_reset_data_with_confirm_yes_removes_the_volumes(tmp_path: Path) -> None:
    env, calls = fake_docker(tmp_path)
    env["CONFIRM"] = "yes"
    done = subprocess.run([str(ROOT / "scripts" / "reset-data.sh")], env=env, capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    down = [line for line in calls.read_text().splitlines() if " down " in f" {line} "]
    assert down and "-v" in down[0].split() and "--remove-orphans" in down[0].split()
