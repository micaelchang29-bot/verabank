"""Subcomando ``aml``: envía ``data/aml/transfers.jsonl`` por la API respetando ``occurred_at`` y el orden."""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from verabank_sim.aml import run as aml_run
from verabank_sim.load.api import HttpApi, api_credentials_from_env
from verabank_sim.load.db import REPO_ROOT, transfer_service_psql
from verabank_sim.replay.cli import _positive_float, _positive_int

log = logging.getLogger("verabank_sim.aml")
DEFAULT_IN = REPO_ROOT / "data" / "aml" / "transfers.jsonl"
MAP_NAME = "service_run.jsonl"


def add_arguments(parser: argparse.ArgumentParser) -> None:
    import os

    parser.add_argument("--in", dest="input", type=Path, default=DEFAULT_IN, help="transfers.jsonl de amlgen")
    parser.add_argument("--tps", type=_positive_float, default=50.0, help="solicitudes por segundo dentro de una hora (50)")
    parser.add_argument("--approve-reviews", action="store_true",
                        help="aprueba por la API las que queden en IN_REVIEW (comentario aml-eval)")
    parser.add_argument("--concurrency", type=_positive_int, default=20, help="solicitudes en vuelo como máximo (20)")
    parser.add_argument("--no-seed", action="store_true", help="no siembra las cuentas (ya existen con saldo)")
    parser.add_argument("--final-timeout", type=float, default=60.0, help="segundos de espera a que no queden PROCESSING (60)")
    parser.add_argument("--base-url", default=f"http://localhost:{os.environ.get('SERVER_PORT', '8081')}",
                        help="URL de transfer-service")
    parser.add_argument("--map", type=Path, default=None,
                        help=f"mapa id del generador → id del servicio (junto al archivo de entrada: {MAP_NAME})")


def run(args: argparse.Namespace) -> int:
    transfers = aml_run.read_transfers(args.input)
    channel, key = api_credentials_from_env()
    options = aml_run.Options(tps=args.tps, approve_reviews=args.approve_reviews, seed=not args.no_seed,
                              concurrency=args.concurrency, final_timeout_s=args.final_timeout)
    out_map = args.map or args.input.parent / MAP_NAME
    log.info("aml: %d transferencias de %s", len(transfers), args.input)
    api = HttpApi(args.base_url, key, args.concurrency)
    try:
        summary = aml_run.run(transfers, options, api, transfer_service_psql(), channel, out_map)
    finally:
        api.close()
    print(aml_run.format_summary(summary))
    log.info("mapa de la corrida en %s", out_map)
    return 0
