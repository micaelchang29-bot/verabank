"""Subcomando ``rail``: banderas, variables de entorno y arranque."""

from __future__ import annotations

import argparse
import logging
import os
import re
from pathlib import Path

from verabank_sim.rail.engine import RailConfig, RailEngine
from verabank_sim.rail.kafka_runner import KafkaSettings, RailRunner
from verabank_sim.rail.messages import Schemas, default_contracts_dir
from verabank_sim.rail.processor import RailProcessor
from verabank_sim.rail.store import SqliteStore

log = logging.getLogger("verabank_sim.rail")

DEFAULT_STATE_DB = Path(__file__).resolve().parents[2] / ".state" / "rail.db"


def default_bootstrap() -> str:
    """Listener externo del broker: KAFKA_BOOTSTRAP_SERVERS, o localhost:KAFKA_EXTERNAL_PORT (.env)."""
    explicit = os.environ.get("KAFKA_BOOTSTRAP_SERVERS")
    if explicit:
        return explicit
    port = os.environ.get("KAFKA_EXTERNAL_PORT")
    env_file = Path(__file__).resolve().parents[3] / ".env"
    if not port and env_file.is_file():
        found = re.search(r"^KAFKA_EXTERNAL_PORT=(\d+)\s*$", env_file.read_text(encoding="utf-8"), re.MULTILINE)
        port = found.group(1) if found else None
    return f"localhost:{port or 29092}"


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def parse_delay(text: str) -> tuple[int, int]:
    match = re.fullmatch(r"(\d+):(\d+)", text.strip())
    if not match:
        raise argparse.ArgumentTypeError(f"se esperaba MIN:MAX en milisegundos, no {text!r}")
    low, high = int(match.group(1)), int(match.group(2))
    if low > high:
        raise argparse.ArgumentTypeError(f"MIN ({low}) no puede superar MAX ({high})")
    return low, high


def _rate(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"no es un número: {text!r}") from None
    if not 0.0 <= value <= 1.0:
        raise argparse.ArgumentTypeError(f"debe estar entre 0 y 1: {text}")
    return value


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--reject-rate", type=_rate, default=_env("RAIL_REJECT_RATE", "0.0"),
                        help="probabilidad de CreditRejected en vez de CreditConfirmed (env RAIL_REJECT_RATE, 0.0)")
    parser.add_argument("--delay-ms", type=parse_delay, default=_env("RAIL_DELAY_MS", "20:200"),
                        metavar="MIN:MAX", help="retraso uniforme de la respuesta (env RAIL_DELAY_MS, 20:200)")
    parser.add_argument("--dup-rate", type=_rate, default=_env("RAIL_DUP_RATE", "0.0"),
                        help="probabilidad de emitir la respuesta dos veces (env RAIL_DUP_RATE, 0.0)")
    parser.add_argument("--dup-mode", choices=("same-id", "new-id"), default=_env("RAIL_DUP_MODE", "same-id"),
                        help="same-id repite el mensaje; new-id repite el payload con otro event_id (env RAIL_DUP_MODE)")
    parser.add_argument("--silent-rate", type=_rate, default=_env("RAIL_SILENT_RATE", "0.0"),
                        help="probabilidad de no emitir la respuesta (env RAIL_SILENT_RATE, 0.0)")
    parser.add_argument("--lost-rate", type=_rate, default=_env("RAIL_LOST_RATE", "0.0"),
                        help="probabilidad de ignorar la instrucción (env RAIL_LOST_RATE, 0.0)")
    parser.add_argument("--seed", type=int, default=_env("RAIL_SEED", "42"),
                        help="semilla de las decisiones (env RAIL_SEED, 42)")
    parser.add_argument("--state-db", type=Path, default=Path(_env("RAIL_STATE_DB", str(DEFAULT_STATE_DB))),
                        help="base SQLite del estado (env RAIL_STATE_DB, simulator/.state/rail.db)")
    parser.add_argument("--ready-file", type=Path, default=None,
                        help="archivo que se crea al quedar suscrito (junto con la línea READY)")
    parser.add_argument("--bootstrap-servers", default=None,
                        help="listener externo de Kafka (env KAFKA_BOOTSTRAP_SERVERS; si no, localhost:KAFKA_EXTERNAL_PORT de .env)")
    parser.add_argument("--group-id", default=_env("RAIL_GROUP_ID", "rail-simulator"),
                        help="grupo de consumo (env RAIL_GROUP_ID, rail-simulator)")
    parser.add_argument("--offset-reset", choices=("earliest", "latest"), default="earliest",
                        help="dónde empezar si el grupo no tiene offset confirmado (earliest)")
    parser.add_argument("--contracts-dir", type=Path, default=None,
                        help="directorio con los esquemas (env VERABANK_CONTRACTS_DIR, contracts/events)")


def run(args: argparse.Namespace) -> int:
    config = RailConfig(
        reject_rate=args.reject_rate,
        delay_min_ms=args.delay_ms[0],
        delay_max_ms=args.delay_ms[1],
        dup_rate=args.dup_rate,
        dup_mode=args.dup_mode,
        silent_rate=args.silent_rate,
        lost_rate=args.lost_rate,
        seed=args.seed,
    )
    schemas = Schemas(args.contracts_dir or default_contracts_dir())
    store = SqliteStore(args.state_db)
    processor = RailProcessor(RailEngine(store, config), schemas)
    bootstrap = args.bootstrap_servers or default_bootstrap()
    log.info("rail: %s bootstrap=%s estado=%s", config, bootstrap, args.state_db)
    runner = RailRunner(
        processor,
        KafkaSettings(bootstrap, args.group_id, args.offset_reset, args.ready_file),
    )
    return runner.run()
