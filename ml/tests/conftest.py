"""Datos sintéticos con la forma de PaySim para las pruebas de features."""

import numpy as np
import pandas as pd
import pytest

from verabank_ml.features import load_contract

CONFIG = {
    "types": ["TRANSFER", "CASH_OUT", "PAYMENT", "DEBIT"],
    "split": {
        "train": [1, 20],
        "valid": [26, 40],
        "test": [46, 60],
        "gap_steps": 5,
    },
}
ALL_TYPES = ["TRANSFER", "CASH_OUT", "PAYMENT", "DEBIT", "CASH_IN"]
N_STEPS = 60
N_ACCOUNTS = 40


def random_rows(rng: np.random.Generator, n: int, steps: np.ndarray | None = None) -> pd.DataFrame:
    """Filas aleatorias con pocas cuentas, para que las claves se repitan mucho."""
    return pd.DataFrame(
        {
            "step": rng.integers(1, N_STEPS + 1, n) if steps is None else steps,
            "type": rng.choice(ALL_TYPES, n),
            "amount": np.round(rng.lognormal(4, 1.5, n), 2),
            "nameOrig": [f"C{i}" for i in rng.integers(0, N_ACCOUNTS, n)],
            "nameDest": [f"C{i}" for i in rng.integers(0, N_ACCOUNTS, n)],
            "oldbalanceOrg": np.round(rng.lognormal(5, 2, n), 2) * (rng.random(n) > 0.2),
            "isFraud": rng.integers(0, 2, n),
        }
    )


@pytest.fixture(scope="session")
def contract() -> dict:
    return load_contract()


@pytest.fixture
def raw() -> pd.DataFrame:
    """2.000 filas sin ordenar por paso."""
    return random_rows(np.random.default_rng(7), 2000)
