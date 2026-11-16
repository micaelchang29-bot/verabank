"""Subcomando ``chaos``: banderas, cliente httpx y código de salida."""

from __future__ import annotations

import argparse
import logging
import os
import random
from datetime import UTC, datetime
from typing import Any

import httpx

from verabank_sim.chaos.report import evaluate, format_report
from verabank_sim.chaos.runner import (
    InsufficientBudget,
    allocate_sources,
    available_balances,
    build_plan,
    make_amounts,
    run_chaos,
)
from verabank_sim.rail.cli import _rate

log = logging.getLogger("verabank_sim.chaos")


class HttpApi:
    """Adaptador de httpx a ``runner.Api``. La clave de API vive solo en la cabecera por defecto del cliente."""

    def __init__(self, base_url: str, api_key: str, concurrency: int, request_timeout_s: float = 30.0) -> None:
        self._client = httpx.Client(
            base_url=base_url,
            headers={"X-API-Key": api_key},
            timeout=request_timeout_s,
            limits=httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency),
        )

    @staticmethod
    def _unpack(response: httpx.Response) -> tuple[int, dict[str, str], Any]:
        try:
            body = response.json()
        except ValueError:
            body = None
        return response.status_code, {k.lower(): v for k, v in response.headers.items()}, body

    def post_transfer(self, idempotency_key: str, body: dict[str, Any]) -> tuple[int, dict[str, str], Any]:
        return self._unpack(self._client.post("/api/v1/transfers", json=body,
                                              headers={"Idempotency-Key": idempotency_key}))

    def get_transfer(self, transfer_id: str) -> tuple[int, dict[str, str], Any]:
        return self._unpack(self._client.get(f"/api/v1/transfers/{transfer_id}"))

    def get_account(self, account_id: str) -> tuple[int, dict[str, str], Any]:
        return self._unpack(self._client.get(f"/api/v1/accounts/{account_id}"))

    def close(self) -> None:
        self._client.close()


def api_key_from_env() -> str:
    """Primera clave de VERABANK_API_KEYS (``canal:clave,...``). Nunca se imprime."""
    first = os.environ.get("VERABANK_API_KEYS", "").split(",")[0]
    _, sep, key = first.partition(":")
    if not sep or not key:
        raise SystemExit("VERABANK_API_KEYS no está definido en el entorno (formato canal:clave)")
    return key


def add_arguments(parser: argparse.ArgumentParser) -> None:
    port = os.environ.get("SERVER_PORT", "8081")
    parser.add_argument("--n", type=int, default=2000, help="transferencias EXTERNAL a crear (2000)")
    parser.add_argument("--concurrency", type=int, default=20, help="solicitudes en paralelo (20)")
    parser.add_argument("--timeout", type=float, default=180.0,
                        help="segundos máximos, desde el inicio, hasta que todas estén en estado final (180)")
    parser.add_argument("--base-url", default=f"http://localhost:{port}",
                        help="URL de transfer-service (http://localhost:$SERVER_PORT)")
    parser.add_argument("--reject-rate", type=_rate, default=0.0,
                        help="REJECT pedido al rail: el % de FAILED debe quedar a menos de 3 puntos (0.0)")
    parser.add_argument("--replay-rate", type=_rate, default=0.05,
                        help="fracción que se reenvía idéntica: 200 + Idempotent-Replayed (0.05)")
    parser.add_argument("--reuse-rate", type=_rate, default=0.01,
                        help="fracción que reutiliza la clave con otro cuerpo: 422 idempotency-key-reuse (0.01)")
    parser.add_argument("--seed", type=int, default=None, help="semilla del plan (por defecto, aleatoria)")


def run(args: argparse.Namespace) -> int:
    if args.n < 1 or args.concurrency < 1 or args.timeout <= 0:
        raise SystemExit("--n, --concurrency y --timeout deben ser positivos")
    if args.replay_rate + args.reuse_rate > 1.0:
        raise SystemExit("--replay-rate más --reuse-rate no puede superar 1")
    logging.getLogger("httpx").setLevel(logging.WARNING)   # una línea INFO por solicitud ahogaría el informe
    seed = args.seed if args.seed is not None else random.SystemRandom().randrange(2**32)
    rng = random.Random(seed)
    api = HttpApi(args.base_url, api_key_from_env(), args.concurrency)
    try:
        amounts = make_amounts(args.n, rng)
        try:
            sources = allocate_sources(available_balances(api), amounts)
        except InsufficientBudget as exc:
            log.error("%s", exc)
            return 2
        plan = build_plan(args.n, args.replay_rate, args.reuse_rate, sources, amounts, rng, datetime.now(UTC))
        log.info("chaos: n=%d concurrencia=%d timeout=%ss reject=%s semilla=%d",
                 args.n, args.concurrency, args.timeout, args.reject_rate, seed)
        report = run_chaos(api, plan, args.reject_rate, args.concurrency, args.timeout)
    finally:
        api.close()
    problems = evaluate(report)
    print(format_report(report, problems), flush=True)
    return 1 if problems else 0
