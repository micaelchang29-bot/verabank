"""Esquema de PaySim en data/raw/ (marcador `data`: no corre en la CI; ver make data)."""

from pathlib import Path

import pandas as pd
import pytest

from verabank_ml.paysim import COLUMNS, EXPECTED_ROWS

CSV = Path(__file__).resolve().parents[2] / "data/raw/paysim.csv"

pytestmark = pytest.mark.data


@pytest.fixture(scope="module")
def paysim() -> pd.DataFrame:
    if not CSV.exists():
        pytest.fail(f"falta {CSV}: ejecuta make data")
    return pd.read_csv(CSV)


def test_columns_and_rows(paysim):
    assert list(paysim.columns) == COLUMNS
    assert len(paysim) == EXPECTED_ROWS


def test_dtypes(paysim):
    assert pd.api.types.is_integer_dtype(paysim["step"])
    for col in ("amount", "oldbalanceOrg", "newbalanceOrig", "oldbalanceDest", "newbalanceDest"):
        assert pd.api.types.is_float_dtype(paysim[col]), col
    for col in ("isFraud", "isFlaggedFraud"):
        assert pd.api.types.is_integer_dtype(paysim[col]), col
        assert set(paysim[col].unique()) <= {0, 1}
    for col in ("type", "nameOrig", "nameDest"):
        assert not pd.api.types.is_numeric_dtype(paysim[col]), col
    assert set(paysim["type"].unique()) == {"PAYMENT", "TRANSFER", "CASH_OUT", "DEBIT", "CASH_IN"}
