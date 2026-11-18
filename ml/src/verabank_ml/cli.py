"""Despachador de subcomandos: `python -m verabank_ml <subcomando>`.

Para añadir un subcomando, registra una función `(argv: list[str]) -> int` en COMMANDS.
"""

import sys
from collections.abc import Callable, Sequence

from verabank_ml.eval_test import eval_test_command
from verabank_ml.export import export_command
from verabank_ml.features import features_command
from verabank_ml.fixtures import fixtures_command
from verabank_ml.monitor.psi import psi_command
from verabank_ml.monitor.reference import psi_reference_command
from verabank_ml.paysim import check_paysim
from verabank_ml.reasons import reasons_command
from verabank_ml.report import report_command
from verabank_ml.retrain import promote_command, retrain_command, rollback_command
from verabank_ml.train import train_command

COMMANDS: dict[str, Callable[[list[str]], int]] = {
    "check-paysim": check_paysim,
    "eval-test": eval_test_command,
    "export": export_command,
    "features": features_command,
    "fixtures": fixtures_command,
    "promote": promote_command,
    "psi": psi_command,
    "psi-reference": psi_reference_command,
    "reasons": reasons_command,
    "report": report_command,
    "retrain": retrain_command,
    "rollback": rollback_command,
    "train": train_command,
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
