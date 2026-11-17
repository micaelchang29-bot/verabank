"""Features sobre PaySim real (marcador `data`: no corre en la CI; necesita make data)."""

import numpy as np
import pandas as pd
import pytest

from verabank_ml.features import (
    REPO_ROOT,
    build_table,
    feature_columns,
    load_config,
    load_contract,
    read_raw,
)

pytestmark = pytest.mark.data

CSV = REPO_ROOT / "data/raw/paysim.csv"
EDA = REPO_ROOT / "ml/reports/eda.md"


@pytest.fixture(scope="module")
def table_a():
    if not CSV.exists():
        pytest.fail(f"falta {CSV}: ejecuta make data")
    return build_table(read_raw(CSV, "A"), "A", load_config(), load_contract())


def eda_fraud_by_partition() -> dict[str, int]:
    """Fraudes por partición de la tabla de ml/reports/eda.md (columna fraudes_en_config_types)."""
    found = {}
    for line in EDA.read_text().splitlines():
        cells = [c.strip() for c in line.strip("|").split("|")]
        if cells[0] in ("train", "valid", "test"):
            found[cells[0]] = int(cells[-1].replace(",", ""))
    assert set(found) == {"train", "valid", "test"}
    return found


def test_columns_match_contract(table_a):
    contract = load_contract()
    expected = feature_columns("A", contract)
    names = [f["name"] for f in contract["features"] if f["set"] == "A"]
    assert table_a.feature_columns == expected
    assert [c for c in expected if not c.startswith("type_")] == [n for n in names if n != "type"]
    assert list(table_a.frame.columns) == [
        "row_id",
        "step",
        "type",
        "is_fraud",
        "partition",
        "src_id",
        "dst_id",
        *expected,
    ]


def test_no_unexpected_nan_or_inf(table_a):
    matrix = table_a.frame[table_a.feature_columns].to_numpy()
    assert np.isfinite(matrix).all()


def test_dropped_rows_are_the_types_outside_config(table_a):
    assert table_a.dropped_by_type == {"CASH_IN": 1_399_284}
    assert len(table_a.frame) == 6_362_620 - 1_399_284


def test_fraud_by_partition_matches_eda(table_a):
    config = load_config()
    raw = pd.read_csv(CSV, usecols=["step", "type", "isFraud"])
    raw = raw[raw["type"].isin(config["types"])]
    by_partition = table_a.frame.groupby("partition")["is_fraud"].agg(["size", "sum"])
    eda = eda_fraud_by_partition()
    for name in ("train", "valid", "test"):
        lo, hi = config["split"][name]
        rows = raw[raw["step"].between(lo, hi)]
        assert by_partition.loc[name, "size"] == len(rows)
        assert by_partition.loc[name, "sum"] == rows["isFraud"].sum() == eda[name]
        prevalence = by_partition.loc[name, "sum"] / by_partition.loc[name, "size"]
        assert prevalence == pytest.approx(rows["isFraud"].mean())


def test_gap_rows_are_kept_as_history_but_labelled_gap(table_a):
    frame = table_a.frame
    assert set(frame["partition"].unique()) == {"train", "valid", "test", "gap"}
    assert frame.loc[frame["partition"] == "gap", "step"].between(401, 424).any()
