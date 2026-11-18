"""Despachador de subcomandos: ``python -m verabank_sim <subcomando> ...``.

Para añadir un subcomando (``replay``, ``aml``) basta con registrarlo en
``COMMANDS`` con un módulo que exponga ``add_arguments(parser)`` y ``run(args) -> int``.
"""

from __future__ import annotations

import argparse
import importlib
import logging
import sys
from collections.abc import Sequence

# nombre -> (módulo, descripción)
COMMANDS: dict[str, tuple[str, str]] = {
    "rail": ("verabank_sim.rail.cli", "rail interbancario simulado (consume rail.commands.v1)"),
    "chaos": ("verabank_sim.chaos.cli", "carga de transferencias EXTERNAL con reenvíos y reusos de clave"),
    "amlgen": ("verabank_sim.amlgen.cli", "generador AML con verdad de terreno (data/aml)"),
    "replay": ("verabank_sim.replay.cli", "reproduce la partición de prueba de PaySim por la API (D1, D10, D11)"),
    "aml": ("verabank_sim.aml.cli", "envía data/aml/transfers.jsonl por la API y aprueba las revisiones (D12)"),
    "aml-eval": ("verabank_sim.aml.eval_cli", "evalúa las alertas AML contra la verdad de terreno de amlgen (D12)"),
    "load-seed": ("verabank_sim.load.loadseed", "cuentas de la prueba de carga T-17 (orígenes con saldo y destinos internos)"),
    "settle": ("verabank_sim.settle", "espera a que no queden transferencias abiertas; aprueba las revisiones si se pide"),
    "watch": ("verabank_sim.watch", "vigila el latido del simulador mientras chaos espera (lo usa make chaos-rail)"),
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m verabank_sim", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", metavar="<subcomando>", required=True)
    for name, (module_name, help_text) in COMMANDS.items():
        module = importlib.import_module(module_name)
        module.add_arguments(sub.add_parser(name, help=help_text, description=help_text))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    module = importlib.import_module(COMMANDS[args.command][0])
    return module.run(args)
