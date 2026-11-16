"""Despachador de subcomandos: ``python -m verabank_sim <subcomando> ...``.

Para añadir un subcomando (``chaos``, ``replay``, ``aml``) basta con registrarlo en
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
