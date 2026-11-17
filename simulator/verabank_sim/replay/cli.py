"""Subcomando ``replay``: reproduce la partición de prueba de PaySim contra transfer-service (D1, D10, D11)."""

from __future__ import annotations

import argparse
import logging
import os
from decimal import Decimal, InvalidOperation
from pathlib import Path

import yaml

from verabank_sim.load.api import HttpApi, api_credentials_from_env
from verabank_sim.load.db import REPO_ROOT, sim_loader_psql, transfer_service_psql
from verabank_sim.replay import mapping as m
from verabank_sim.replay.labels import KafkaPublisher
from verabank_sim.replay.paysim import select_rows
from verabank_sim.replay import run as orchestrator
from verabank_sim.replay.run import Options, Services, format_summary

log = logging.getLogger("verabank_sim.replay")


def _positive_int(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError("debe ser al menos 1")
    return value


def _positive_float(text: str) -> float:
    value = float(text)
    if value <= 0:
        raise argparse.ArgumentTypeError("debe ser positivo")
    return value


def _factor(text: str) -> Decimal:
    try:
        value = Decimal(text)
    except InvalidOperation as exc:
        raise argparse.ArgumentTypeError("no es un número") from exc
    if value <= 0:
        raise argparse.ArgumentTypeError("debe ser positivo")
    return value


def add_arguments(parser: argparse.ArgumentParser) -> None:
    port = os.environ.get("SERVER_PORT", "8081")
    kafka = os.environ.get("KAFKA_EXTERNAL_PORT", "29092")
    parser.add_argument("--n", type=_positive_int, default=20000, help="filas a reproducir (20000)")
    parser.add_argument("--tps", type=_positive_float, default=50.0, help="solicitudes por segundo dentro de un paso (50)")
    parser.add_argument("--amount-factor", type=_factor, default=Decimal(1),
                        help="multiplica los montos antes de la clave de idempotencia (1)")
    parser.add_argument("--start-row", type=int, default=0, help="fila de la partición de prueba por la que empezar (0)")
    parser.add_argument("--concurrency", type=_positive_int, default=20, help="solicitudes en vuelo como máximo (20)")
    parser.add_argument("--no-warmup", action="store_true", help="no carga las 24 horas previas en risk.scoring_log")
    parser.add_argument("--no-labels", action="store_true", help="no publica FraudLabelConfirmed al terminar")
    parser.add_argument("--no-seed", action="store_true", help="no siembra las cuentas (ya existen con saldo)")
    parser.add_argument("--final-timeout", type=float, default=60.0, help="segundos de espera a que no queden PROCESSING (60)")
    parser.add_argument("--paysim", type=Path, default=REPO_ROOT / "data" / "raw" / "paysim.csv", help="CSV de PaySim")
    parser.add_argument("--ml-config", type=Path, default=REPO_ROOT / "ml" / "config.yaml", help="ml/config.yaml")
    parser.add_argument("--base-url", default=f"http://localhost:{port}", help="URL de transfer-service")
    parser.add_argument("--bootstrap", default=f"localhost:{kafka}", help="Kafka (para las etiquetas)")
    parser.add_argument("--out", type=Path, default=REPO_ROOT / "data" / "replay", help="directorio del resumen")


def run(args: argparse.Namespace) -> int:
    cfg = yaml.safe_load(args.ml_config.read_text(encoding="utf-8"))
    epoch = m.parse_epoch(cfg["sim_epoch"])
    test_steps = tuple(cfg["split"]["test"])
    selection = select_rows(args.paysim, types=cfg["types"], test_steps=(test_steps[0], test_steps[1]), n=args.n,
                            start_row=args.start_row, factor=args.amount_factor)
    if not selection.rows:
        log.error("no hay filas que reproducir con --start-row %d", args.start_row)
        return 2
    log.info("replay: %d filas, pasos %d a %d, %d filas de calentamiento", len(selection.rows), selection.rows[0].step,
             selection.rows[-1].step, len(selection.warmup))
    channel, key = api_credentials_from_env()
    options = Options(n=args.n, tps=args.tps, amount_factor=args.amount_factor, start_row=args.start_row,
                      warmup=not args.no_warmup, labels=not args.no_labels, seed=not args.no_seed,
                      concurrency=args.concurrency, final_timeout_s=args.final_timeout)
    api = HttpApi(args.base_url, key, args.concurrency)
    try:
        services = Services(api=api, state_psql=transfer_service_psql(), loader_psql=sim_loader_psql(),
                            publisher=KafkaPublisher(args.bootstrap) if options.labels else None, client_id=channel)
        summary = orchestrator.run(options, selection, epoch, services, args.out)
    finally:
        api.close()
    print(format_summary(summary))
    log.info("resumen en %s", args.out / "summary.json")
    return 0

