"""Muestreo de métricas de transfer-service durante la carga (T-17): saga.stuck, outbox.lag y fallback del scoring.

Sondea los endpoints de Actuator cada ``--interval`` segundos hasta recibir SIGTERM/SIGINT (o hasta ``--duration``) y
escribe un CSV en ``data/perf/``; al terminar imprime los máximos. Solo biblioteca estándar.

    python3 perf/sample_metrics.py --base-url http://localhost:8081 --out data/perf/metrics.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import signal
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

METRICS = {
    "saga_stuck": ("saga.stuck", "VALUE"),
    "outbox_pending": ("outbox.pending", "VALUE"),
    "outbox_oldest_age_s": ("outbox.oldest.age", "VALUE"),
    "outbox_lag_max_s": ("outbox.lag", "MAX"),
    "outbox_lag_count": ("outbox.lag", "COUNT"),
    "outbox_lag_total_s": ("outbox.lag", "TOTAL_TIME"),
    "fallback_total": ("scoring.fallback.total", "COUNT"),
    "circuit_state": ("scoring.circuit.state", "VALUE"),
}
COLUMNS = ["ts", *METRICS, "outbox_lag_mean_s_interval"]


def read_metric(base_url: str, name: str, statistic: str, timeout: float = 2.0) -> float | None:
    """Valor de una estadística de ``/actuator/metrics/<name>``; None si no existe (aún sin observaciones) o falla."""
    try:
        with urllib.request.urlopen(f"{base_url}/actuator/metrics/{name}", timeout=timeout) as r:
            payload = json.load(r)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ConnectionError):
        return None
    for m in payload.get("measurements", []):
        if m.get("statistic") == statistic:
            return float(m["value"])
    return None


def sample(base_url: str, previous: dict | None) -> dict:
    row = {"ts": datetime.now(UTC).isoformat(timespec="seconds")}
    for column, (name, stat) in METRICS.items():
        row[column] = read_metric(base_url, name, stat)
    mean = None
    if previous and row["outbox_lag_count"] is not None and previous.get("outbox_lag_count") is not None:
        dn = row["outbox_lag_count"] - previous["outbox_lag_count"]
        dt = row["outbox_lag_total_s"] - previous["outbox_lag_total_s"]
        mean = dt / dn if dn > 0 else None
    row["outbox_lag_mean_s_interval"] = mean
    return row


def maxima(rows: list[dict]) -> dict:
    out = {}
    for column in ("saga_stuck", "outbox_pending", "outbox_oldest_age_s", "outbox_lag_max_s", "outbox_lag_mean_s_interval"):
        values = [r[column] for r in rows if r.get(column) is not None]
        out[column] = max(values) if values else None
    first = next((r["fallback_total"] for r in rows if r.get("fallback_total") is not None), 0.0)
    last = next((r["fallback_total"] for r in reversed(rows) if r.get("fallback_total") is not None), 0.0)
    out["fallback_during_run"] = (last or 0.0) - (first or 0.0)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--base-url", default="http://localhost:8081")
    parser.add_argument("--out", type=Path, default=Path("data/perf/metrics.csv"))
    parser.add_argument("--interval", type=float, default=5.0)
    parser.add_argument("--duration", type=float, default=None, help="segundos; sin valor, hasta SIGTERM")
    args = parser.parse_args(argv)
    stop = {"now": False}
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.update(now=True))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    rows: list[dict] = []
    deadline = None if args.duration is None else time.monotonic() + args.duration
    with args.out.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        writer.writeheader()
        while not stop["now"] and (deadline is None or time.monotonic() < deadline):
            row = sample(args.base_url, rows[-1] if rows else None)
            rows.append(row)
            writer.writerow(row)
            handle.flush()
            end = time.monotonic() + args.interval
            while not stop["now"] and time.monotonic() < end:
                time.sleep(0.2)
    print(f"métricas: {len(rows)} muestras en {args.out}; máximos {json.dumps(maxima(rows))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
