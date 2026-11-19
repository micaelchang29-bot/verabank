"""Resumen de una o varias fases del diagnóstico del rail (data/perf/diag/<etiqueta>*): carga, cola y PostgreSQL."""
import csv, json, re, sys
from datetime import datetime
from pathlib import Path

D = Path(__file__).resolve().parents[1] / "data" / "perf" / "diag"


def summary(label: str) -> dict:
    rows = [r for r in csv.DictReader(open(D / f"{label}.csv")) if r["xact_commit"]]
    t = lambda r: datetime.fromisoformat(r["ts"].replace("Z", "+00:00"))  # noqa: E731
    mx = lambda k: max(int(r[k] or 0) for r in rows)  # noqa: E731
    n = min(36, len(rows) - 1)                       # ~6 min de k6 (calentamiento + carga) a 10 s por muestra
    a, b = rows[1], rows[n]
    dt = (t(b) - t(a)).total_seconds() or 1
    rate = lambda k: (int(b[k]) - int(a[k])) / dt  # noqa: E731
    log = (D / f"{label}-load.log").read_text()
    p95 = re.search(r"p95 de carga: ([\d.]+) ms", log)
    settle = re.search(r"sin transferencias abiertas \(([\d.]+) s", log)
    stm = (D / f"{label}-statements.txt").read_text().splitlines()
    lock = next((l for l in stm if "pg_advisory_xact_lock" in l), "")
    outbox = (D / f"{label}-outbox.txt").read_text()
    csq = re.search(r"CreditStatusQuery\s*\|\s*(\d+)\s*\|\s*(\d+)", outbox)
    cpu = {}
    for line in (D / f"{label}-cpu.txt").read_text().splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and parts[1].endswith("%"):
            cpu.setdefault(parts[0].replace("verabank-", "").replace("-1", ""), []).append(float(parts[1][:-1]))
    return {
        "p95_ms": float(p95.group(1)) if p95 else None,
        "settle_s": float(settle.group(1)) if settle else None,
        "lag_max": mx("lag_total"), "lag_por_particion": [mx("lag_p0"), mx("lag_p1"), mx("lag_p2")],
        "transferencias_s": round(rate("transfers"), 1), "replies_s": round(rate("reply_offsets"), 1),
        "commits_s": round(rate("xact_commit")), "auditoria_s": round(rate("audit_rows"), 1),
        "espera_815001_max": mx("adv815001_waiting"), "pg_activas_max": mx("pg_active"),
        "lock_815001_media_ms": float(lock.split("|")[2]) if lock else None,
        "credit_status_query": int(csq.group(1)) if csq else 0,
        "cpu_media": {k: round(sum(v) / len(v), 1) for k, v in sorted(cpu.items())},
    }


if __name__ == "__main__":
    out = {label: summary(label) for label in sys.argv[1:]}
    print(json.dumps(out, indent=1, ensure_ascii=False))
