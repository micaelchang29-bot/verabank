"""La alerta de drift solo depende de las filas ``valid`` (ADR-13): veredicto de la demo, retrain --check y BI."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from verabank_ml import retrain
from verabank_ml.monitor import verdict

EPOCH = datetime(2026, 1, 1, tzinfo=UTC)
ROOT = Path(__file__).resolve().parents[2]


def start(k: int) -> datetime:
    return EPOCH + timedelta(hours=24 * k)


def put(pg, k: int, reference: str, *, alert: bool = False, feature: str = "score", psi: float = 0.5,
        insufficient: bool = False) -> None:
    with pg.connect("admin") as conn:
        conn.execute(
            "INSERT INTO analytics.drift_metrics (window_start, window_end, feature, psi, n_current, n_bins, degenerate, "
            "insufficient, is_top_feature, alert, model_version, reference) VALUES (%s, %s, %s, %s, 500, 10, false, %s, "
            "true, %s, 'm/1', %s) ON CONFLICT DO NOTHING",
            (start(k), start(k + 1), feature, psi, insufficient, alert, reference))


def full_demo(pg, *, base_alert: bool = False, drift_alert: bool = True, windows=(24, 25, 26, 27)) -> None:
    """Las cuatro ventanas de la demo con ``valid``: 577–600, 601–624 (base) y 625–648, 649–672 (deriva)."""
    pg.reset()
    for k in windows:
        is_drift = k >= 26
        put(pg, k, "valid", alert=(k == 25 and base_alert) or (k == 27 and is_drift and drift_alert),
            psi=0.4 if is_drift else 0.02)


def verdict_of(pg):
    with pg.connect() as conn:
        return verdict.verdict(conn, EPOCH)


def test_base_quiet_and_drift_alerting_passes(pg):
    full_demo(pg)
    v = verdict_of(pg)
    assert (v.base_alerts, v.drift_alerts, v.missing_windows, v.ok, v.reason()) == (0, 1, [], True, None)


def test_the_demo_fails_if_the_base_alerts(pg):
    full_demo(pg, base_alert=True)
    v = verdict_of(pg)
    assert v.base_alerts == 1 and not v.ok and "base ya alerta contra valid" in v.reason()


def test_the_demo_fails_if_the_drift_does_not_alert(pg):
    full_demo(pg, drift_alert=False)
    v = verdict_of(pg)
    assert v.drift_alerts == 0 and not v.ok and v.reason() == "la deriva no alertó"


@pytest.mark.parametrize("missing", [26, 27, 24])
def test_the_demo_fails_if_a_window_is_not_complete(pg, missing):
    full_demo(pg, windows=tuple(k for k in (24, 25, 26, 27) if k != missing))
    v = verdict_of(pg)
    assert v.missing_windows == [missing] and not v.ok and "sin calcular" in v.reason()


def test_insufficient_rows_do_not_count_as_a_complete_window(pg):
    full_demo(pg)
    with pg.connect("admin") as conn:
        conn.execute("UPDATE analytics.drift_metrics SET insufficient = true WHERE window_start = %s", (start(27),))
    assert verdict_of(pg).missing_windows == [27]


def test_train_rows_never_count_for_the_verdict(pg):
    """Una base que alerta (o una deriva que alertaría) solo contra train no cambia el veredicto."""
    full_demo(pg, drift_alert=False)
    for k in (24, 25, 26, 27):
        put(pg, k, "train", alert=True)                    # ni siquiera debería existir una fila train con alert
    v = verdict_of(pg)
    assert (v.base_alerts, v.drift_alerts) == (0, 0) and not v.ok
    full_demo(pg)
    for k in (24, 25):
        put(pg, k, "train", alert=True)
    assert verdict_of(pg).base_alerts == 0 and verdict_of(pg).ok


def test_the_verdict_command_exit_codes(pg, monkeypatch, capsys):
    monkeypatch.setenv("VERABANK_ML_DSN", pg.dsn())
    full_demo(pg)
    assert verdict.drift_verdict_command([]) == 0 and "OK: con la referencia valid" in capsys.readouterr().out
    full_demo(pg, drift_alert=False)
    assert verdict.drift_verdict_command([]) == 1 and "la deriva no alertó" in capsys.readouterr().err
    full_demo(pg, base_alert=True)
    assert verdict.drift_verdict_command([]) == 1 and "STOP (D3)" in capsys.readouterr().err


# --- retrain --check y BI leen la referencia correcta -------------------------------------------------------------


def latest_alerts(pg):
    with pg.connect() as conn:
        return [r[0] for r in conn.execute(retrain.LATEST_ALERTS).fetchall()]


def test_retrain_check_reads_the_latest_valid_window_only(pg):
    pg.reset()
    put(pg, 26, "valid", alert=True, feature="score")
    put(pg, 26, "valid", alert=True, feature="log_amount")
    put(pg, 27, "train", alert=True, feature="dst_seen_before")        # más reciente, pero de train: no cuenta
    assert latest_alerts(pg) == ["log_amount", "score"]
    put(pg, 27, "valid", alert=False, feature="score")                  # la última ventana de valid ya no alerta
    assert latest_alerts(pg) == []
    pg.reset()
    put(pg, 27, "train", alert=True)
    assert latest_alerts(pg) == []


def extra_a() -> str:
    text = (ROOT / "docs" / "kpis-extra.sql").read_text()
    return text.split("-- EXTRA B")[0].split("\n", 1)[1]


def test_bi_extra_a_reads_valid_alerts_only(pg):
    pg.reset()
    put(pg, 26, "valid", alert=True, feature="score", psi=0.4)
    put(pg, 27, "valid", alert=True, feature="score", psi=0.6)
    put(pg, 27, "train", alert=True, feature="log_amount", psi=0.9)    # más alta y más reciente, pero de train
    put(pg, 20, "valid", alert=True, feature="old", psi=0.7)           # fuera de las dos últimas ventanas de valid
    with pg.connect() as conn:
        rows = conn.execute(extra_a()).fetchall()
    assert [(r[2], float(r[3])) for r in rows] == [("score", 0.6), ("score", 0.4)]
    pg.reset()
    put(pg, 27, "train", alert=True)
    with pg.connect() as conn:
        assert conn.execute(extra_a()).fetchall() == []


def test_the_demo_script_runs_the_72_step_drift_and_ends_with_the_verdict():
    script = (ROOT / "scripts" / "drift-demo.sh").read_text()
    assert "replay --steps 48 --first-step 577" in script
    assert "replay --steps 72 --first-step 625 --amount-factor 3" in script
    assert "ml-job.sh psi --reference valid" in script and "ml-job.sh psi --reference train" in script
    assert script.rstrip().splitlines()[-1] == "scripts/ml-job.sh drift-verdict"          # su código de salida es el de la demo
