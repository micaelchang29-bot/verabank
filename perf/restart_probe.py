"""4.G(c): carga baja contra POST /api/v1/transfers mientras se reinicia PostgreSQL; línea de tiempo de respuestas.

Envía ``--rate`` solicitudes/s durante ``--duration`` s (cuentas de make load-seed, canal loadtest) y, a los
``--restart-at`` s, ejecuta ``--restart-cmd``. Imprime un JSON con: errores (respuestas fuera de 200/201/422 o sin
respuesta) mientras cae, primer y último error relativos al reinicio, y el tiempo hasta normalizar (desde el reinicio
hasta el inicio de 10 s seguidos sin errores). Solo biblioteca estándar.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime


def seed_uuid(label: str) -> str:
    return str(uuid.UUID(hashlib.md5(label.encode()).hexdigest()))


def post(base: str, key: str) -> int:
    body = {"source_account_id": seed_uuid(f"verabank-load:src:{random.randrange(5000)}"), "rail": "INTERNAL",
            "dest_account_id": seed_uuid(f"verabank-load:dst:{random.randrange(2000)}"), "transfer_type": "PAYMENT",
            "amount": f"{random.uniform(10, 100):.2f}", "currency": "PEN", "dest_country": "PE",
            "occurred_at": datetime.now(UTC).isoformat()}
    req = urllib.request.Request(f"{base}/api/v1/transfers", data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "X-API-Key": key,
                                          "Idempotency-Key": str(uuid.uuid4())})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except (urllib.error.URLError, TimeoutError, ConnectionError):
        return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--base-url", default="http://localhost:8081")
    p.add_argument("--rate", type=float, default=5.0)
    p.add_argument("--duration", type=float, default=90.0)
    p.add_argument("--restart-at", type=float, default=30.0)
    p.add_argument("--restart-cmd", required=True)
    args = p.parse_args()
    key = os.environ["API_KEY"]
    events: list[tuple[float, int]] = []
    lock = threading.Lock()
    start = time.monotonic()
    restart = {"t": None, "done": None}

    def do_restart():
        time.sleep(args.restart_at)
        restart["t"] = time.monotonic() - start
        subprocess.run(args.restart_cmd, shell=True, check=False, capture_output=True)
        restart["done"] = time.monotonic() - start

    threading.Thread(target=do_restart, daemon=True).start()

    def one():
        t = time.monotonic() - start
        code = post(args.base_url, key)
        with lock:
            events.append((t, code))

    with ThreadPoolExecutor(max_workers=32) as pool:
        n = 0
        while time.monotonic() - start < args.duration:
            pool.submit(one)
            n += 1
            time.sleep(max(0.0, start + n / args.rate - time.monotonic()))
    events.sort()
    bad = [(t, c) for t, c in events if c not in (200, 201, 422)]
    r = restart["t"] or 0.0
    normal_from = None
    for t, _ in [(r, 0), *[(t, c) for t, c in events if t >= r]]:
        window = [c for u, c in events if t <= u < t + 10]
        if window and all(c in (200, 201, 422) for c in window) and t + 10 <= events[-1][0]:
            normal_from = t
            break
    by_code: dict[str, int] = {}
    for _, c in events:
        by_code[str(c)] = by_code.get(str(c), 0) + 1
    print(json.dumps({
        "requests": len(events), "by_status": dict(sorted(by_code.items())), "errors": len(bad),
        "restart_started_s": round(r, 2), "restart_command_returned_s": round(restart["done"] or 0, 2),
        "first_error_after_restart_s": round(bad[0][0] - r, 2) if bad else None,
        "last_error_after_restart_s": round(bad[-1][0] - r, 2) if bad else None,
        "time_to_normal_s": round(normal_from - r, 2) if normal_from is not None else None,
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
