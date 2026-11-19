"""feature-parity: empareja filas del replay con PaySim y compara features guardadas con las de entrenamiento."""

from __future__ import annotations

import argparse
import re
import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from tests.test_replay import EPOCH, TYPES, TEST, write_csv
from verabank_sim import parity
from verabank_sim.replay import mapping as m

ROWS = [
    (575, "TRANSFER", "100.00", "A1", "1000", "D1", 0),     # row_id 0
    (575, "CASH_IN", "30.00", "A2", "10", "D2", 0),          # row_id 1: CASH_IN, nunca
    (576, "PAYMENT", "20.00", "A3", "10", "D3", 0),          # row_id 2
    (576, "PAYMENT", "20.00", "A3", "10", "D3", 0),          # row_id 3: idéntica a la 2 (ambigua)
    (577, "TRANSFER", "7.50", "A4", "10", "D1", 1),          # row_id 4
]
STEP_FEATURES = {"dst_seen_before": 1.0, "dst_age_hours": 5.0, "hour_sin": 0.25}


def scored(row_id: int, **override: float) -> parity.Scored:
    step, _, amount, orig, _, dest, _ = ROWS[row_id]
    return parity.Scored(f"t{row_id}", m.account_id(orig), f"PS-{dest}", f"{float(amount):.2f}", step,
                         {**STEP_FEATURES, **override})


@pytest.fixture
def paysim(tmp_path: Path) -> Path:
    return write_csv(tmp_path / "paysim.csv", ROWS)


def test_index_skips_cash_in_and_other_steps_and_flags_duplicates(paysim: Path) -> None:
    index = parity.paysim_index(paysim, types=TYPES, steps={575, 576})
    assert index[(575, m.account_id("A1"), "PS-D1", "100.00")] == [0]
    assert index[(576, m.account_id("A3"), "PS-D3", "20.00")] == [2, 3]
    assert all(key[0] in (575, 576) for key in index) and not any("PS-D2" == key[2] for key in index)


def test_match_separates_unmatched_and_ambiguous(paysim: Path) -> None:
    index = parity.paysim_index(paysim, types=TYPES, steps={575, 576, 577})
    other = parity.Scored("x", m.account_id("A9"), "PS-D9", "1.00", 577, {})
    found, unmatched, ambiguous = parity.match([scored(0), scored(2), scored(4), other], index)
    assert sorted(found) == [0, 4] and unmatched == 1 and ambiguous == 1


def test_compare_reports_percent_and_magnitude_per_feature() -> None:
    found = {0: scored(0), 1: scored(4, dst_age_hours=8.0), 2: scored(2, dst_seen_before=0.0, hour_sin=0.25 + 1e-12)}
    training = {i: dict(STEP_FEATURES) for i in found}
    diffs = {d.feature: d for d in parity.compare(found, training, list(STEP_FEATURES))}
    assert (diffs["dst_age_hours"].compared, diffs["dst_age_hours"].differing) == (3, 1)
    assert diffs["dst_age_hours"].max_abs == 3.0 and diffs["dst_age_hours"].mean_abs == 3.0
    assert diffs["dst_age_hours"].percent == pytest.approx(100 / 3)
    assert diffs["dst_seen_before"].differing == 1 and diffs["dst_seen_before"].max_abs == 1.0
    assert diffs["hour_sin"].differing == 0                      # 1e-12 queda dentro de la tolerancia de 1e-9
    strict = {d.feature: d for d in parity.compare(found, training, ["hour_sin"], tolerance=1e-13)}
    assert strict["hour_sin"].differing == 1


def test_sample_sql_excludes_warmup_k6_and_rows_without_features_and_is_deterministic() -> None:
    sql = parity.sample_sql(5000)
    assert "model_version <> 'warmup'" in sql and "NOT LIKE 'perf-%'" in sql and "features <> '{}'" in sql
    assert "ORDER BY md5(transfer_id::text) LIMIT 5000" in sql


class Reader:
    def __init__(self, lines: list[str]) -> None:
        self.lines, self.calls = lines, []

    def run(self, stdin: str, *args: str, timeout_s: float = 0) -> str:
        self.calls.append((stdin, args))
        return "\n".join(self.lines) + "\n"


def line(s: parity.Scored, step: int | None = None) -> str:
    at = m.occurred_at(EPOCH, step or s.step)
    return parity.SEP.join([s.transfer_id, s.source_ref, s.dest_ref, s.amount, at, json.dumps(s.features)])


def test_end_to_end_with_a_small_parquet(paysim: Path, tmp_path: Path, monkeypatch, capsys) -> None:
    names = list(STEP_FEATURES)
    training = pa.table({"row_id": pa.array([0, 1, 2, 3, 4], pa.int64()),
                         **{n: pa.array([STEP_FEATURES[n]] * 5) for n in names}})
    pq.write_table(training, tmp_path / "features.parquet")
    reader = Reader([line(scored(0)), line(scored(4, dst_age_hours=9.0)), line(scored(2))])
    monkeypatch.setattr(parity, "read_only_psql", lambda: reader)
    args = argparse.Namespace(sample=5000, tolerance=1e-9, paysim=paysim, features=tmp_path / "features.parquet",
                              ml_config=Path(__file__).resolve().parents[2] / "ml" / "config.yaml")
    assert parity.run(args) == 0
    out = capsys.readouterr().out
    assert "3 filas muestreadas; 2 con pareja en PaySim, 0 sin pareja, 1 ambiguas" in out
    assert re.search(r"dst_age_hours\s+2\s+1\s+50\.000\s+4\b", out) and re.search(r"dst_seen_before\s+2\s+0\s+0\.000", out)
    assert reader.calls[0][1][:2] == ("-At", "-F")


def test_no_scored_rows_or_no_matches_is_an_error_not_a_pass(paysim: Path, tmp_path: Path, monkeypatch, capsys) -> None:
    args = argparse.Namespace(sample=10, tolerance=1e-9, paysim=paysim, features=tmp_path / "f.parquet",
                              ml_config=Path(__file__).resolve().parents[2] / "ml" / "config.yaml")
    monkeypatch.setattr(parity, "read_only_psql", lambda: Reader([]))
    assert parity.run(args) == 2
    other = parity.Scored("x", m.account_id("A9"), "PS-D9", "1.00", 577, {"dst_age_hours": 1.0})
    monkeypatch.setattr(parity, "read_only_psql", lambda: Reader([line(other)]))
    assert parity.run(args) == 2 and "ninguna" in capsys.readouterr().out


def test_the_ml_types_are_those_of_the_config() -> None:
    assert "CASH_IN" not in TYPES and TEST[0] <= 577 <= TEST[1]
