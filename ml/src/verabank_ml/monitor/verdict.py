"""``drift-verdict``: veredicto de ``make drift-demo`` sobre ``analytics.drift_metrics`` (ADR-13, D3).

Solo cuenta la referencia que genera alertas (``valid``). Las ventanas se juzgan por la que ocupan, no por cuándo se
calcularon: base = ventanas de los pasos 577–624; deriva = ventanas de los pasos 625–696 (tanda de 72 pasos; la última,
673–696, queda incompleta por la regla de completitud de D2 y no cuenta). Sale con 0 solo si la base no alertó y la
deriva sí, y si las cuatro ventanas (577–600, 601–624, 625–648 y 649–672) están completas (calculadas y con filas suficientes).
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import datetime

from verabank_ml.features import load_config
from verabank_ml.monitor.psi import ALERT_REFERENCE, SCORE, WINDOW, parse_epoch, window_bounds

BASE_WINDOWS = (24, 25)          # pasos 577–600 y 601–624
DRIFT_WINDOWS = (26, 27)         # pasos 625–648 y 649–672 (la 28, 673–696, es la última de la tanda)


@dataclass(frozen=True)
class Verdict:
    base_alerts: int
    drift_alerts: int
    missing_windows: list[int]

    @property
    def ok(self) -> bool:
        return self.base_alerts == 0 and self.drift_alerts > 0 and not self.missing_windows

    def reason(self) -> str | None:
        if self.base_alerts:
            return f"la tanda base ya alerta contra {ALERT_REFERENCE} ({self.base_alerts} filas)"
        if self.missing_windows:
            starts = ", ".join(f"{24 * k + 1}-{24 * k + 24}" for k in self.missing_windows)
            return f"ventanas sin calcular o con filas insuficientes (pasos {starts})"
        if not self.drift_alerts:
            return "la deriva no alertó"
        return None


def _span(epoch: datetime, windows: tuple[int, ...]) -> tuple[datetime, datetime]:
    return window_bounds(epoch, windows[0])[0], window_bounds(epoch, windows[-1])[0] + WINDOW


def verdict(conn, epoch: datetime) -> Verdict:
    def alerts(windows: tuple[int, ...]) -> int:
        lo, hi = _span(epoch, windows)
        return conn.execute("SELECT count(*) FROM analytics.drift_metrics WHERE alert AND reference = %s "
                            "AND window_start >= %s AND window_start < %s", (ALERT_REFERENCE, lo, hi)).fetchone()[0]

    missing = [k for k in (*BASE_WINDOWS, *DRIFT_WINDOWS) if conn.execute(
        "SELECT 1 FROM analytics.drift_metrics WHERE reference = %s AND feature = %s AND NOT insufficient "
        "AND window_start = %s", (ALERT_REFERENCE, SCORE, window_bounds(epoch, k)[0])).fetchone() is None]
    return Verdict(alerts(BASE_WINDOWS), alerts(DRIFT_WINDOWS), missing)


def drift_verdict_command(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m verabank_ml drift-verdict", description=__doc__.split("\n")[0])
    parser.parse_args(argv)
    import psycopg

    from verabank_ml.monitor.psi import dsn_from_env

    epoch = parse_epoch(load_config()["sim_epoch"])
    with psycopg.connect(dsn_from_env(), autocommit=True) as conn:
        v = verdict(conn, epoch)
    print(f"alertas con la referencia {ALERT_REFERENCE} en ventanas de la base (577–624): {v.base_alerts}; "
          f"en ventanas de la deriva (625–672): {v.drift_alerts}")
    reason = v.reason()
    if reason:
        print(f"STOP (D3): {reason}. No se cambian umbral, ventanas, referencias ni top-5.", file=sys.stderr)
        return 1
    print(f"OK: con la referencia {ALERT_REFERENCE} la base no alertó y la deriva sí ({v.drift_alerts} filas con alert)")
    return 0

