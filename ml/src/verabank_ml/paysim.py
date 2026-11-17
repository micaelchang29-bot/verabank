"""Esquema esperado de PaySim y su comprobación."""

import sys
from pathlib import Path

import pandas as pd

PAYSIM_PATH = Path("data/raw/paysim.csv")
COLUMNS = [
    "step", "type", "amount", "nameOrig", "oldbalanceOrg", "newbalanceOrig",
    "nameDest", "oldbalanceDest", "newbalanceDest", "isFraud", "isFlaggedFraud",
]  # fmt: skip
EXPECTED_ROWS = 6_362_620


def check_paysim(argv: list[str]) -> int:
    path = Path(argv[0]) if argv else PAYSIM_PATH
    df = pd.read_csv(path)
    columns = list(df.columns)
    if columns != COLUMNS or len(df) != EXPECTED_ROWS:
        print(
            f"PaySim inesperado: {len(df)} filas (esperadas {EXPECTED_ROWS}), "
            f"columnas {columns} (esperadas {COLUMNS})",
            file=sys.stderr,
        )
        return 1
    print(f"PaySim correcto: {len(df)} filas, {len(columns)} columnas")
    return 0
