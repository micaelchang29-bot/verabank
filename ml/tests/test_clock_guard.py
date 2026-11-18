"""Guarda del reloj simulado: smoke, demo-idempotency y la carga usan el reloj real y descuadran PSI y reentrenamiento."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from verabank_ml import clock_guard, retrain
from verabank_ml.features import load_config

EPOCH = datetime(2026, 1, 1, tzinfo=UTC)


def insert(pg, occurred_at, source_ref="s", n=1):
    with pg.connect("admin") as conn, conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO risk.scoring_log (transfer_id, source_ref, dest_ref, amount, occurred_at, features, score, "
            "decision, model_version) VALUES (%s, %s, 'd', 1, %s, '{}', 0.1, 'APPROVE', 'm/1')",
            [(uuid.uuid4(), source_ref, occurred_at) for _ in range(n)])


def replay_rows(pg):
    insert(pg, EPOCH, n=2)                                            # paso 1
    insert(pg, EPOCH + timedelta(hours=743, minutes=59), n=2)         # dentro del paso 744
    insert(pg, EPOCH + timedelta(hours=744))                          # el límite del rango, cerrado
    insert(pg, EPOCH + timedelta(days=60), "perf-src-0001", n=3)      # k6: sintéticas, ya excluidas por todos los jobs


def check(pg, force=False):
    with pg.connect() as conn:
        clock_guard.check(conn, EPOCH, force)


def test_replay_only_does_not_abort(pg):
    pg.reset()
    replay_rows(pg)
    check(pg)


def test_an_empty_base_does_not_abort(pg):
    pg.reset()
    check(pg)


@pytest.mark.parametrize("label, when", [("hoy (smoke, carga)", datetime.now(UTC)),
                                         ("después de 744 h", EPOCH + timedelta(hours=744, seconds=1)),
                                         ("futuro lejano", EPOCH + timedelta(days=3650)),
                                         ("antes de sim_epoch", EPOCH - timedelta(seconds=1))])
def test_a_row_outside_the_simulated_range_aborts_with_a_clear_message(pg, label, when):
    pg.reset()
    replay_rows(pg)
    insert(pg, when)
    with pytest.raises(clock_guard.OutsideSimulatedClock) as info:
        check(pg)
    message = str(info.value)
    assert "1 transferencias" in message and "fuera del rango simulado" in message
    assert "make smoke" in message and "su propia base" in message and "--force" in message


def test_force_skips_the_guard(pg):
    pg.reset()
    insert(pg, datetime.now(UTC), n=5)
    check(pg, force=True)


def test_command_exit_codes(pg, monkeypatch, capsys):
    monkeypatch.setenv("VERABANK_ML_DSN", pg.dsn())
    pg.reset()
    replay_rows(pg)
    assert clock_guard.clock_guard_command([]) == 0
    insert(pg, datetime.now(UTC))
    assert clock_guard.clock_guard_command([]) == 3
    assert "ABORTADO" in capsys.readouterr().err
    assert clock_guard.clock_guard_command(["--force"]) == 0


def stub_context(monkeypatch):
    monkeypatch.setattr(retrain, "champion_context",
                        lambda: (load_config(), None, None, {"feature_order": []}, None, None))
    monkeypatch.setattr(retrain, "read_labeled", lambda conn: (_ for _ in ()).throw(retrain.Refused("pasó la guarda")))


@pytest.mark.parametrize("argv", [[], ["--check"]])
def test_retrain_aborts_before_reading_labels_and_force_skips_it(pg, monkeypatch, capsys, argv):
    pg.reset()
    replay_rows(pg)
    insert(pg, datetime.now(UTC))
    monkeypatch.setenv("VERABANK_ML_DSN", pg.dsn())
    stub_context(monkeypatch)
    assert retrain.retrain_command(argv) == 3
    err = capsys.readouterr().err
    assert "fuera del rango simulado" in err and "pasó la guarda" not in err
    assert retrain.retrain_command([*argv, "--force"]) == 3                  # la guarda se salta: falla más adelante
    assert "pasó la guarda" in capsys.readouterr().err


def test_retrain_with_only_replay_rows_reaches_the_labels(pg, monkeypatch, capsys):
    pg.reset()
    replay_rows(pg)
    monkeypatch.setenv("VERABANK_ML_DSN", pg.dsn())
    stub_context(monkeypatch)
    assert retrain.retrain_command(["--check"]) == 3
    assert "pasó la guarda" in capsys.readouterr().err


def test_the_guard_script_is_wired_into_bi_seed_and_the_demo():
    root = Path(clock_guard.__file__).resolve().parents[3]
    makefile = (root / "Makefile").read_text()
    seed = makefile.split("\nbi-seed:")[1].split("\n\n")[0]
    assert "scripts/ml-job.sh clock-guard" in seed and seed.index("ml-job.sh clock-guard") < seed.index("sim.sh replay")
    demo = (root / "scripts/drift-demo.sh").read_text()
    assert "ml-job.sh clock-guard" in demo and demo.index("ml-job.sh clock-guard") < demo.index("sim.sh replay")
