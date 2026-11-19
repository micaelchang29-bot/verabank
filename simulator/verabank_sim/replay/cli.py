"""Subcomando ``replay``: reproduce la partición de prueba de PaySim contra transfer-service (D1, D10, D11)."""

from __future__ import annotations

import argparse
from dataclasses import replace
import logging
import os
from decimal import Decimal, InvalidOperation
from pathlib import Path

import yaml

from verabank_sim.load.api import HttpApi, api_credentials_from_env
from verabank_sim.load.db import REPO_ROOT, read_only_psql, sim_loader_psql, transfer_service_psql
from verabank_sim.replay import history as history_mod, mapping as m
from verabank_sim.replay.labels import KafkaPublisher
from verabank_sim.replay.paysim import WARMUP_STEPS, select_rows
from verabank_sim.replay import run as orchestrator
from verabank_sim.replay.run import Options, Services, format_summary

log = logging.getLogger("verabank_sim.replay")
DEFAULT_N = 20000
DEFAULT_FIRST_STEP = 577   # inicio de la ventana de PSI 24 (pasos 577 a 600)


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
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--n", type=_positive_int, default=DEFAULT_N, help="filas a reproducir (20000); excluyente con --steps")
    mode.add_argument("--steps", type=_positive_int, default=None,
                      help="alternativa a --n: todas las filas de K pasos consecutivos desde --first-step")
    parser.add_argument("--first-step", type=_positive_int, default=None,
                        help="primer paso del modo --steps (577: inicio de una ventana de PSI de 24 pasos)")
    parser.add_argument("--tps", type=_positive_float, default=50.0, help="solicitudes por segundo dentro de un paso (50)")
    parser.add_argument("--amount-factor", type=_factor, default=Decimal(1),
                        help="multiplica los montos antes de la clave de idempotencia (1)")
    parser.add_argument("--start-row", type=int, default=0, help="fila de la partición de prueba por la que empezar (0)")
    parser.add_argument("--concurrency", type=_positive_int, default=20, help="solicitudes en vuelo como máximo (20)")
    parser.add_argument("--no-warmup", action="store_true", help="no carga las 24 horas previas en risk.scoring_log")
    parser.add_argument("--no-history-warmup", action="store_true",
                        help="no carga la fila de primera aparición de cada destino del lote (dst_seen_before y dst_age_hours)")
    parser.add_argument("--no-labels", action="store_true", help="no publica FraudLabelConfirmed al terminar")
    parser.add_argument("--no-seed", action="store_true", help="no siembra las cuentas (ya existen con saldo)")
    parser.add_argument("--final-timeout", type=float, default=60.0, help="segundos de espera a que no queden PROCESSING (60)")
    parser.add_argument("--paysim", type=Path, default=REPO_ROOT / "data" / "raw" / "paysim.csv", help="CSV de PaySim")
    parser.add_argument("--ml-config", type=Path, default=REPO_ROOT / "ml" / "config.yaml", help="ml/config.yaml")
    parser.add_argument("--base-url", default=f"http://localhost:{port}", help="URL de transfer-service")
    parser.add_argument("--bootstrap", default=f"localhost:{kafka}", help="Kafka (para las etiquetas)")
    parser.add_argument("--out", type=Path, default=REPO_ROOT / "data" / "replay", help="directorio del resumen")


def resolve_mode(args: argparse.Namespace) -> tuple[int | None, int | None, int | None]:
    """(n, steps, first_step). ``--first-step`` solo vale con ``--steps``; ``--steps`` no se combina con ``--start-row``."""
    if args.steps is None:
        if args.first_step is not None:
            raise SystemExit("--first-step solo se usa con --steps")
        return args.n, None, None
    if args.start_row:
        raise SystemExit("--steps no se combina con --start-row")
    return None, args.steps, args.first_step or DEFAULT_FIRST_STEP


def plan_line(rows: int, steps: tuple[int, int], tps: float) -> str:
    seconds = rows / tps
    return (f"replay: {rows} filas, pasos {steps[0]} a {steps[1]}; duración estimada a {tps:g} TPS: "
            f"{int(seconds // 60)} min {int(seconds % 60)} s (más la espera de la barrera por paso)")


def run(args: argparse.Namespace) -> int:
    cfg = yaml.safe_load(args.ml_config.read_text(encoding="utf-8"))
    epoch = m.parse_epoch(cfg["sim_epoch"])
    test_steps = tuple(cfg["split"]["test"])
    n, steps, first_step = resolve_mode(args)
    selection = select_rows(args.paysim, types=cfg["types"], test_steps=(test_steps[0], test_steps[1]), n=n,
                            start_row=args.start_row, factor=args.amount_factor, steps=steps, first_step=first_step)
    if not selection.rows:
        log.error("no hay filas que reproducir con esa selección")
        return 2
    if not args.no_history_warmup:
        anchor = first_step or selection.start_step
        selection = replace(selection, history=history_mod.first_appearances(
            args.paysim, types=cfg["types"], dests={r.name_dest for r in selection.rows},
            before_step=anchor - WARMUP_STEPS, factor=args.amount_factor))
    print(plan_line(len(selection.rows), (selection.rows[0].step, selection.rows[-1].step), args.tps), flush=True)
    log.info("replay: %d filas, pasos %d a %d, %d filas de calentamiento", len(selection.rows), selection.rows[0].step,
             selection.rows[-1].step, len(selection.warmup))
    channel, key = api_credentials_from_env()
    options = Options(n=n or len(selection.rows), tps=args.tps, amount_factor=args.amount_factor,
                      start_row=args.start_row, warmup=not args.no_warmup,
                      history_warmup=not args.no_history_warmup, labels=not args.no_labels,
                      seed=not args.no_seed, concurrency=args.concurrency, final_timeout_s=args.final_timeout,
                      steps=steps, first_step=first_step)
    api = HttpApi(args.base_url, key, args.concurrency)
    try:
        services = Services(api=api, state_psql=transfer_service_psql(), loader_psql=sim_loader_psql(),
                            publisher=KafkaPublisher(args.bootstrap) if options.labels else None, client_id=channel,
                            reader_psql=read_only_psql())
        summary = orchestrator.run(options, selection, epoch, services, args.out)
    finally:
        api.close()
    print(format_summary(summary))
    log.info("resumen en %s", args.out / "summary.json")
    return 0

