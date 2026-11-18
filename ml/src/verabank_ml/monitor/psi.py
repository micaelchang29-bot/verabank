"""``psi`` (make psi): PSI por ventana de 24 pasos y por feature, más el del score, en ``analytics.drift_metrics`` (D2).

- Ventana ``k`` = pasos ``[24k+1, 24k+24]`` desde ``sim_epoch``: ``[epoch + 24k h, epoch + 24(k+1) h)``. La 24 es 577–600.
- Solo filas reales de ``risk.scoring_log``: ``model_version <> 'warmup'`` y sin las sintéticas de k6 (``perf-``). Una ventana está completa si hay filas con
  ``occurred_at >= window_end``; las incompletas no se calculan.
- Menos de 200 filas: ``insufficient``, sin alerta. Feature degenerada: PSI 0.
- ``alert`` solo para ``score`` y las ``top_features`` de la referencia, con PSI > 0,25 en la ventana **y** en la
  anterior contigua (leída de la tabla o calculada en esta misma corrida). Las demás se registran sin alerta.
- Calcula las ventanas completas pendientes (``--recompute``: todas) y escribe con upsert. Rol ``ml_job``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from verabank_ml.features import load_config
from verabank_ml.monitor import binning
from verabank_ml.monitor.reference import REFERENCE_PATH

WINDOW_STEPS = 24
MIN_ROWS = 200
ALERT_PSI = 0.25
SCORE = "score"
WINDOW = timedelta(hours=WINDOW_STEPS)


@dataclass(frozen=True)
class Metric:
    window_start: datetime
    window_end: datetime
    feature: str
    psi: float
    n_current: int
    n_bins: int
    degenerate: bool
    insufficient: bool
    is_top_feature: bool
    alert: bool
    model_version: str


def parse_epoch(text: str) -> datetime:
    return datetime.fromisoformat(text).astimezone(UTC)


def window_index(epoch: datetime, occurred_at: datetime) -> int:
    return int((occurred_at - epoch) // WINDOW)


def window_bounds(epoch: datetime, k: int) -> tuple[datetime, datetime]:
    start = epoch + k * WINDOW
    return start, start + WINDOW


def window_steps(k: int) -> tuple[int, int]:
    return WINDOW_STEPS * k + 1, WINDOW_STEPS * k + WINDOW_STEPS


def monitored(reference: dict) -> set[str]:
    return {SCORE, *reference["top_features"]}


def compute_window(reference: dict, start: datetime, end: datetime, scores: Sequence[float],
                   features: Sequence[dict], model_version: str) -> list[Metric]:
    """Métricas de una ventana, sin la alerta (la decide ``apply_alerts`` con la ventana anterior)."""
    n = len(scores)
    top = monitored(reference)
    out = []
    specs = [(name, spec, [f[name] for f in features]) for name, spec in reference["features"].items()]
    specs.append((SCORE, reference["score"], list(scores)))
    for name, spec, values in specs:
        bins = binning.Bins.from_json(spec)
        value = 0.0 if bins.degenerate or n == 0 else binning.psi(spec["expected"], binning.proportions(bins, values))
        out.append(Metric(start, end, name, value, n, bins.n_bins, bins.degenerate, n < MIN_ROWS,
                          name in top, False, model_version))
    return out


def over(m: Metric | None) -> bool:
    return m is not None and not m.insufficient and m.psi > ALERT_PSI


def apply_alerts(current: list[Metric], previous: dict[str, Metric]) -> list[Metric]:
    """``previous``: las métricas de la ventana anterior contigua, por feature (vacío si no existe)."""
    return [Metric(**{**m.__dict__, "alert": m.is_top_feature and over(m) and over(previous.get(m.feature))})
            for m in current]


def evaluate(reference: dict, epoch: datetime, windows: Iterable[tuple[int, list[float], list[dict], str]],
             stored: dict[int, dict[str, Metric]]) -> dict[int, list[Metric]]:
    """Calcula las ventanas pedidas en orden; ``stored`` son las ya guardadas (para la contigua anterior)."""
    done: dict[int, list[Metric]] = {}
    for k, scores, feats, version in sorted(windows, key=lambda w: w[0]):
        start, end = window_bounds(epoch, k)
        prev = {m.feature: m for m in done[k - 1]} if k - 1 in done else stored.get(k - 1, {})
        done[k] = apply_alerts(compute_window(reference, start, end, scores, feats, version), prev)
    return done


# --- base de datos ----------------------------------------------------------------------------------------------


def dsn_from_env() -> str:
    if os.environ.get("VERABANK_ML_DSN"):
        return os.environ["VERABANK_ML_DSN"]
    password = os.environ.get("ML_JOB_PASSWORD")
    if not password:
        raise SystemExit("falta ML_JOB_PASSWORD en el entorno (.env)")
    from psycopg.conninfo import make_conninfo

    return make_conninfo(host=os.environ.get("POSTGRES_HOST", "127.0.0.1"), port=os.environ.get("POSTGRES_PORT", "5432"),
                         dbname=os.environ.get("POSTGRES_DB", "verabank"), user="ml_job", password=password,
                         connect_timeout=10)


# Filas reales: sin el calentamiento del replay (warmup) ni las sintéticas de make perf-scoring, que van directo a
# risk-scoring con source_ref «perf-…» y sin transferencia (docs/resultados/fase-4.md). Lo comparten los tres jobs.
REAL = "model_version <> 'warmup' AND source_ref NOT LIKE 'perf-%%'"
UPSERT = """
INSERT INTO analytics.drift_metrics (window_start, window_end, feature, psi, n_current, n_bins, degenerate,
                                     insufficient, is_top_feature, alert, model_version, computed_at)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
ON CONFLICT (window_start, feature) DO UPDATE SET
  window_end = EXCLUDED.window_end, psi = EXCLUDED.psi, n_current = EXCLUDED.n_current, n_bins = EXCLUDED.n_bins,
  degenerate = EXCLUDED.degenerate, insufficient = EXCLUDED.insufficient, is_top_feature = EXCLUDED.is_top_feature,
  alert = EXCLUDED.alert, model_version = EXCLUDED.model_version, computed_at = now()
"""


def complete_windows(conn, epoch: datetime) -> list[int]:
    lo, hi = conn.execute(f"SELECT min(occurred_at), max(occurred_at) FROM risk.scoring_log WHERE {REAL}").fetchone()
    if lo is None:
        return []
    first, last = window_index(epoch, lo), window_index(epoch, hi)
    # La ventana k está completa si hay filas con occurred_at >= su final: la última, que contiene al máximo, no.
    return list(range(first, last))


def load_window(conn, epoch: datetime, k: int) -> tuple[list[float], list[dict], str]:
    start, end = window_bounds(epoch, k)
    rows = conn.execute(
        f"SELECT score, features, model_version FROM risk.scoring_log WHERE {REAL} "
        "AND occurred_at >= %s AND occurred_at < %s", (start, end)).fetchall()
    versions = Counter(r[2] for r in rows)
    version = versions.most_common(1)[0][0] if versions else "none"
    return [float(r[0]) for r in rows], [r[1] for r in rows], version


def stored_metrics(conn, epoch: datetime) -> dict[int, dict[str, Metric]]:
    out: dict[int, dict[str, Metric]] = {}
    for row in conn.execute(
            "SELECT window_start, window_end, feature, psi, n_current, n_bins, degenerate, insufficient, "
            "is_top_feature, alert, model_version FROM analytics.drift_metrics").fetchall():
        m = Metric(row[0], row[1], row[2], float(row[3]), *row[4:])
        out.setdefault(window_index(epoch, m.window_start), {})[m.feature] = m
    return out


def write(conn, metrics: Iterable[Metric]) -> int:
    rows = [(m.window_start, m.window_end, m.feature, m.psi, m.n_current, m.n_bins, m.degenerate, m.insufficient,
             m.is_top_feature, m.alert, m.model_version) for m in metrics]
    with conn.cursor() as cur:
        cur.executemany(UPSERT, rows)
    return len(rows)


def run(conn, reference: dict, epoch: datetime, recompute: bool = False) -> dict[int, list[Metric]]:
    stored = stored_metrics(conn, epoch)
    pending = [k for k in complete_windows(conn, epoch) if recompute or k not in stored]
    windows = [(k, *load_window(conn, epoch, k)) for k in pending]
    done = evaluate(reference, epoch, windows, stored)
    with conn.transaction():
        write(conn, (m for k in sorted(done) for m in done[k]))
    return done


def summary_table(done: dict[int, list[Metric]], reference: dict) -> str:
    if not done:
        return "sin ventanas completas pendientes"
    top = [SCORE, *reference["top_features"]]
    lines = [f"{'ventana':>7} {'pasos':>9} {'filas':>7}  " + " ".join(f"{t[:14]:>14}" for t in top) + "  alertas"]
    for k in sorted(done):
        by = {m.feature: m for m in done[k]}
        a, b = window_steps(k)
        n = by[SCORE].n_current
        flags = [m.feature for m in done[k] if m.alert]
        cells = " ".join(f"{by[t].psi:>14.4f}" for t in top)
        note = "insuficiente" if by[SCORE].insufficient else (", ".join(flags) or "-")
        lines.append(f"{k:>7} {a:>4}-{b:<4} {n:>7}  {cells}  {note}")
    return "\n".join(lines)


def psi_command(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m verabank_ml psi", description=__doc__.split("\n")[0])
    parser.add_argument("--recompute", action="store_true", help="recalcula todas las ventanas completas")
    parser.add_argument("--fail-on-alert", action="store_true", help="sale con 2 si alguna ventana calculada alerta")
    args = parser.parse_args(argv)
    import psycopg

    reference = json.loads(REFERENCE_PATH.read_text())
    epoch = parse_epoch(load_config()["sim_epoch"])
    with psycopg.connect(dsn_from_env(), autocommit=True) as conn:
        done = run(conn, reference, epoch, recompute=args.recompute)
    print(summary_table(done, reference))
    alerts = sorted({(window_steps(k)[0], m.feature) for k, ms in done.items() for m in ms if m.alert})
    print(f"ventanas calculadas: {len(done)}; alertas: {len(alerts)} {alerts if alerts else ''}".rstrip())
    return 2 if alerts and args.fail_on_alert else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(psi_command(sys.argv[1:]))
