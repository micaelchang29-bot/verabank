"""``clock-guard``: aborta si la base tiene transferencias fuera del rango simulado (``sim_epoch`` a ``sim_epoch`` + 744 h).

``make smoke``, ``make demo-idempotency`` y la carga T-17 envían ``occurred_at`` de hoy; en la misma base que un replay
descuadran el PSI (ventanas vacías), la madurez de las etiquetas y los disparadores del reentrenamiento (ADR-13, D2).
Mira ``risk.scoring_log`` con el rol ``ml_job`` (no lee ``core``) y descarta las filas sintéticas de k6 (``perf-``),
que ya excluyen los demás jobs. ``--force`` la salta.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta

from verabank_ml.features import load_config

RANGE = timedelta(hours=744)

QUERY = """
SELECT count(*), min(occurred_at), max(occurred_at) FROM risk.scoring_log
WHERE source_ref NOT LIKE 'perf-%%' AND (occurred_at < %s OR occurred_at > %s)
"""


class OutsideSimulatedClock(RuntimeError):
    pass


def check(conn, epoch: datetime, force: bool = False) -> None:
    """Lanza ``OutsideSimulatedClock`` si hay filas fuera de ``[epoch, epoch + 744 h]`` y no se pidió ``force``."""
    n, first, last = conn.execute(QUERY, (epoch, epoch + RANGE)).fetchone()
    if n and not force:
        raise OutsideSimulatedClock(
            f"la base tiene {n:,} transferencias con occurred_at fuera del rango simulado "
            f"[{epoch:%Y-%m-%d %H:%M}, {epoch + RANGE:%Y-%m-%d %H:%M}] UTC (de {first:%Y-%m-%d %H:%M} a {last:%Y-%m-%d %H:%M}): "
            "las de make smoke, make demo-idempotency o make load-transfers usan el reloj real y descuadran el PSI, "
            "la madurez de las etiquetas y los disparadores. Esas corridas requieren su propia base "
            "(make reset-data CONFIRM=yes); con --force se salta esta guarda.")


def clock_guard_command(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m verabank_ml clock-guard", description=__doc__.split("\n")[0])
    parser.add_argument("--force", action="store_true", help="salta la guarda")
    args = parser.parse_args(argv)
    import psycopg

    from verabank_ml.monitor.psi import dsn_from_env, parse_epoch

    epoch = parse_epoch(load_config()["sim_epoch"])
    with psycopg.connect(dsn_from_env(), autocommit=True) as conn:
        try:
            check(conn, epoch, args.force)
        except OutsideSimulatedClock as exc:
            print(f"ABORTADO: {exc}", file=sys.stderr)
            return 3
    print("reloj simulado: todas las transferencias están dentro del rango" + (" (--force: sin comprobar)" if args.force else ""))
    return 0
