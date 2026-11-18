"""Subcomando ``settle``: espera a que no quede ninguna transferencia abierta y, si se pide, aprueba las revisiones.

Lee ``analytics.v_transfer_outcomes`` con el rol de solo lectura de BI (no lee ``core``). Termina cuando no quedan
``PENDING`` ni ``PROCESSING`` y, con ``--approve-reviews``, tampoco ``IN_REVIEW``: esas se aprueban por la API con el
comentario ``settle`` (la misma llamada que ``aml --approve-reviews``). Sale con 1 si vence el plazo.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from verabank_sim.load import review

log = logging.getLogger("verabank_sim.settle")
COMMENT = "settle"
OPEN = ("PENDING", "PROCESSING")
COUNTS_SQL = "SELECT status, count(*) FROM analytics.v_transfer_outcomes GROUP BY status ORDER BY status;"
REVIEW_SQL = "SELECT transfer_id FROM analytics.v_transfer_outcomes WHERE status = 'IN_REVIEW' ORDER BY transfer_id;"
_DURATION = re.compile(r"^(\d+(?:\.\d+)?)(ms|s|m|h)?$")
_UNITS = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0, None: 1.0}


def duration(text: str) -> float:
    """``15m``, ``5s``, ``1h``, ``500ms`` o segundos sin unidad."""
    match = _DURATION.match(text.strip())
    if not match:
        raise argparse.ArgumentTypeError(f"duración inválida: {text!r} (p. ej. 15m, 5s)")
    value = float(match.group(1)) * _UNITS[match.group(2)]
    if value <= 0:
        raise argparse.ArgumentTypeError("la duración debe ser positiva")
    return value


@dataclass
class Result:
    ok: bool
    counts: dict[str, int]
    approved: int = 0
    already_decided: int = 0
    rounds: int = 0
    elapsed_s: float = 0.0
    errors: list[str] = field(default_factory=list)


def counts(reader: Any) -> dict[str, int]:
    return {status: int(n) for status, n in reader.query(COUNTS_SQL)}


def settle(reader: Any, api: Any | None, *, approve_reviews: bool, timeout_s: float, poll_s: float,
           clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep) -> Result:
    if approve_reviews and api is None:
        raise ValueError("approve_reviews necesita la API")
    started = clock()
    deadline = started + timeout_s
    result = Result(ok=False, counts={})
    tried: set[str] = set()
    while True:
        result.rounds += 1
        result.counts = counts(reader)
        if approve_reviews and result.counts.get("IN_REVIEW"):
            for (tid,) in reader.query(REVIEW_SQL):
                if tid in tried:
                    continue
                tried.add(tid)
                code = review.approve(api, tid, COMMENT)
                if code == review.APPROVED:
                    result.approved += 1
                elif code == review.NOT_PENDING:
                    result.already_decided += 1
                else:
                    result.errors.append(f"{tid}: HTTP {code}")
                    tried.discard(tid)   # se reintenta en la siguiente vuelta
            result.counts = counts(reader)
        waiting = sum(result.counts.get(s, 0) for s in OPEN)
        if approve_reviews:
            waiting += result.counts.get("IN_REVIEW", 0)
        if waiting == 0:
            result.ok = True
            break
        if clock() >= deadline:
            break
        log.info("abiertas: %s", {s: result.counts.get(s, 0) for s in (*OPEN, "IN_REVIEW")})
        sleep(min(poll_s, max(0.0, deadline - clock())))
    result.elapsed_s = round(clock() - started, 3)
    return result


def format_result(r: Result) -> str:
    head = "OK: sin transferencias abiertas" if r.ok else "FALLO: venció el plazo con transferencias abiertas"
    return "\n".join([
        f"{head} ({r.elapsed_s} s, {r.rounds} consultas)",
        f"por estado: {dict(sorted(r.counts.items()))}",
        f"aprobadas en revisión (comentario {COMMENT}): {r.approved}; ya decididas: {r.already_decided}",
        *([f"errores de review: {len(r.errors)} (p. ej. {r.errors[0]})"] if r.errors else []),
    ])


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--approve-reviews", action="store_true",
                        help="aprueba por la API las IN_REVIEW (comentario settle) y espera a que terminen")
    parser.add_argument("--timeout", type=duration, default=duration("15m"), help="plazo total (15m)")
    parser.add_argument("--poll", type=duration, default=duration("5s"), help="intervalo de consulta (5s)")
    parser.add_argument("--base-url", default=f"http://localhost:{os.environ.get('SERVER_PORT', '8081')}",
                        help="URL de transfer-service (solo con --approve-reviews)")


def run(args: argparse.Namespace) -> int:
    from verabank_sim.load.api import HttpApi, api_credentials_from_env
    from verabank_sim.load.db import read_only_psql

    api = None
    if args.approve_reviews:
        _, key = api_credentials_from_env()
        api = HttpApi(args.base_url, key, 4)
    try:
        result = settle(read_only_psql(), api, approve_reviews=args.approve_reviews, timeout_s=args.timeout,
                        poll_s=args.poll)
    finally:
        if api is not None:
            api.close()
    print(format_result(result))
    return 0 if result.ok else 1
