"""Despachador de subcomandos: `python -m verabank_ml <subcomando>`.

Para añadir un subcomando, registra una función `(argv: list[str]) -> int` en COMMANDS.
"""

import sys
from collections.abc import Callable, Sequence

from verabank_ml.paysim import check_paysim

COMMANDS: dict[str, Callable[[list[str]], int]] = {
    "check-paysim": check_paysim,
}


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help"):
        names = ", ".join(sorted(COMMANDS)) or "(ninguno todavía)"
        print(f"uso: python -m verabank_ml <subcomando> [opciones]\nsubcomandos: {names}")
        return 0 if args else 2
    handler = COMMANDS.get(args[0])
    if handler is None:
        print(f"subcomando desconocido: {args[0]}", file=sys.stderr)
        return 2
    return handler(args[1:])
