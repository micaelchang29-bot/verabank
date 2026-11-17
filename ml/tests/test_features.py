"""Features punto-en-el-tiempo (T-14), sin datos: sintéticos y ejemplos calculados a mano."""

import math

import numpy as np
import pandas as pd
import pytest
from conftest import ALL_TYPES, CONFIG, N_ACCOUNTS, random_rows

from verabank_ml.features import (
    BALANCE_COLUMNS,
    LeakageError,
    build_table,
    check_matrix_columns,
    feature_columns,
)
from verabank_ml.reference import reference_features

SETS = ["A", "B"]


def features_of(raw, feature_set, contract):
    table = build_table(raw, feature_set, CONFIG, contract)
    return table.frame.set_index("row_id")[table.feature_columns]


def alter(raw: pd.DataFrame, mask: np.ndarray, rng: np.random.Generator) -> pd.DataFrame:
    """Cambia montos, cuentas, tipos, saldos y etiquetas de las filas marcadas (no el paso)."""
    out = raw.copy()
    n = int(mask.sum())
    out.loc[mask, "amount"] = np.round(rng.lognormal(4, 1.5, n), 2)
    out.loc[mask, "type"] = rng.choice(ALL_TYPES, n)
    out.loc[mask, "nameOrig"] = [f"C{i}" for i in rng.integers(0, N_ACCOUNTS, n)]
    out.loc[mask, "nameDest"] = [f"C{i}" for i in rng.integers(0, N_ACCOUNTS, n)]
    out.loc[mask, "oldbalanceOrg"] = np.round(rng.lognormal(5, 2, n), 2)
    out.loc[mask, "isFraud"] = rng.integers(0, 2, n)
    return out


# --- T-14 ------------------------------------------------------------------------------------------


@pytest.mark.parametrize("feature_set", SETS)
@pytest.mark.parametrize("k", [1, 2, 5, 13, 25, 26, 40, 59, 60])
def test_t14_altering_the_future_does_not_change_the_past(raw, contract, feature_set, k):
    """Se alteran las filas con step > k y la mitad de las de step == k.

    Cambian: ninguna feature de las filas con step < k ni de las filas intactas del paso k.
    """
    rng = np.random.default_rng(100 + k)
    at_k = (raw["step"] == k).to_numpy()
    mask = (raw["step"] > k).to_numpy() | (at_k & (rng.random(len(raw)) < 0.5))
    altered = alter(raw, mask, rng)
    untouched = ~mask & (raw["step"] <= k).to_numpy()
    before = features_of(raw, feature_set, contract)
    after = features_of(altered, feature_set, contract)
    ids = [i for i in before.index[np.isin(before.index, np.flatnonzero(untouched))]]
    assert ids, "la prueba no compara ninguna fila"
    pd.testing.assert_frame_equal(before.loc[ids], after.loc[ids], check_exact=True)


@pytest.mark.parametrize("feature_set", SETS)
def test_t14_rows_of_the_same_step_do_not_see_each_other(raw, contract, feature_set):
    """Alterar todas las demás filas del mismo paso no cambia las features de una fila."""
    rng = np.random.default_rng(11)
    before = features_of(raw, feature_set, contract)
    checked = 0
    for row_id in rng.choice(raw.index[raw["type"] != "CASH_IN"], 25, replace=False):
        same_step = (raw["step"] == raw.at[row_id, "step"]).to_numpy()
        others = same_step & (raw.index != row_id)
        after = features_of(alter(raw, others, rng), feature_set, contract)
        pd.testing.assert_series_equal(
            before.loc[row_id], after.loc[row_id], check_exact=True, check_names=False
        )
        checked += int(others.sum())
    assert checked > 100


@pytest.mark.parametrize("feature_set", SETS)
def test_t14_labels_do_not_enter(raw, contract, feature_set):
    """Barajar is_fraud deja las features idénticas."""
    shuffled = raw.copy()
    shuffled["isFraud"] = np.random.default_rng(3).permutation(raw["isFraud"].to_numpy())
    assert not shuffled["isFraud"].equals(raw["isFraud"])
    pd.testing.assert_frame_equal(
        features_of(raw, feature_set, contract),
        features_of(shuffled, feature_set, contract),
        check_exact=True,
    )


@pytest.mark.parametrize("feature_set", SETS)
def test_t14_vectorized_matches_slow_reference(contract, feature_set):
    """2.000 filas aleatorias con claves repetidas: la vectorizada coincide con la de referencia."""
    raw = random_rows(np.random.default_rng(2024), 2000)
    categories = next(f for f in contract["features"] if f["name"] == "type")["categories"]
    expected = pd.DataFrame(
        reference_features(raw.to_dict("records"), feature_set, CONFIG["types"], categories)
    ).set_index("row_id")
    actual = features_of(raw, feature_set, contract)
    assert list(actual.columns) == list(expected.columns)
    assert actual.index.equals(expected.index)
    pd.testing.assert_frame_equal(actual, expected.astype(float), rtol=1e-9, atol=1e-9)
    # La muestra ejercita las ventanas: hay valores distintos de cero en todas las de historial.
    for column in [
        "dst_in_count_24h",
        "dst_distinct_src_24h",
        "src_out_count_1h",
        "src_out_count_24h",
    ]:
        assert (actual[column] > 0).any(), column
    assert (actual["is_new_beneficiary"] == 0).any() and (actual["dst_seen_before"] == 1).any()


# --- ejemplos con valores calculados a mano --------------------------------------------------------

#            step  orig dest  amount type
HAND_ROWS = [
    (1, "A", "X", 10, "TRANSFER"),  # 0
    (2, "B", "X", 20, "CASH_OUT"),  # 1
    (2, "C", "X", 5, "TRANSFER"),  # 2
    (2, "A", "Y", 7, "PAYMENT"),  # 3
    (4, "A", "X", 3, "TRANSFER"),  # 4
    (25, "D", "X", 1, "TRANSFER"),  # 5
    (26, "E", "X", 2, "TRANSFER"),  # 6
    (27, "A", "X", 4, "TRANSFER"),  # 7
    (2, "Z", "X", 1000, "CASH_IN"),  # 8: fuera de config.types, no es historia
]
# dst_in_count_24h, dst_in_sum_24h, dst_distinct_src_24h, dst_seen_before, dst_age_hours,
# src_out_count_1h, src_out_count_24h, src_out_sum_24h, is_new_beneficiary
HAND_EXPECTED = {
    0: (0, 0, 0, 0, 0, 0, 0, 0, 1),  # primera fila del dataset
    1: (1, 10, 1, 1, 1, 0, 0, 0, 1),  # ve el paso 1 (= s-1); no ve la fila 2 (mismo paso)
    2: (1, 10, 1, 1, 1, 0, 0, 0, 1),  # igual que la 1: las filas del mismo paso no se ven
    3: (0, 0, 0, 0, 0, 1, 1, 10, 1),  # destino Y nuevo; A envió en el paso 1 (= s-1)
    4: (3, 35, 3, 1, 3, 0, 2, 17, 0),  # pasos 1 y 2 en ventana; A ya envió a X; 1 h = paso 3, vacío
    5: (
        4,
        38,
        3,
        1,
        24,
        0,
        0,
        0,
        1,
    ),  # s-24 = 1: el paso 1 entra; A cuenta una vez como origen distinto
    6: (4, 29, 4, 1, 25, 0, 0, 0, 1),  # s-25 = 1: el paso 1 sale de la ventana
    7: (3, 6, 3, 1, 26, 0, 1, 3, 0),  # s-24 = 3: el paso 2 sale; A solo cuenta el paso 4
}
HAND_HOURS = {  # step -> (hour_sin, hour_cos) con hora = (step - 1) % 24
    1: (0.0, 1.0),
    2: (0.25881904510252074, 0.9659258262890683),
    4: (0.7071067811865476, 0.7071067811865476),
    25: (0.0, 1.0),
    26: (0.25881904510252074, 0.9659258262890683),
    27: (0.5, 0.8660254037844387),
}
HISTORY_COLUMNS = [
    "dst_in_count_24h",
    "dst_in_sum_24h",
    "dst_distinct_src_24h",
    "dst_seen_before",
    "dst_age_hours",
    "src_out_count_1h",
    "src_out_count_24h",
    "src_out_sum_24h",
    "is_new_beneficiary",
]


@pytest.fixture
def hand_raw() -> pd.DataFrame:
    frame = pd.DataFrame(HAND_ROWS, columns=["step", "nameOrig", "nameDest", "amount", "type"])
    frame["oldbalanceOrg"] = [100.0, 0.0, 50.0, 7.0, 3.0, 1.0, 2.0, 8.0, 0.0]
    frame["isFraud"] = 0
    return frame


def test_hand_history_features(hand_raw, contract):
    table = build_table(hand_raw, "A", CONFIG, contract)
    assert table.dropped_by_type == {"CASH_IN": 1}
    feats = table.frame.set_index("row_id")
    assert list(feats.index) == list(range(8))
    for row_id, expected in HAND_EXPECTED.items():
        actual = tuple(feats.loc[row_id, HISTORY_COLUMNS])
        assert actual == pytest.approx(expected, abs=1e-9), f"fila {row_id}"


def test_hand_request_features(hand_raw, contract):
    feats = build_table(hand_raw, "A", CONFIG, contract).frame.set_index("row_id")
    assert feats.loc[0, "amount"] == 10.0
    assert feats.loc[0, "log_amount"] == pytest.approx(2.3978952727983707)  # ln(11)
    assert feats.loc[1, "log_amount"] == pytest.approx(math.log(21))
    one_hot = ["type_TRANSFER", "type_CASH_OUT", "type_PAYMENT", "type_DEBIT"]
    assert feats.loc[0, one_hot].tolist() == [1, 0, 0, 0]
    assert feats.loc[1, one_hot].tolist() == [0, 1, 0, 0]
    assert feats.loc[3, one_hot].tolist() == [0, 0, 1, 0]
    for row_id, step in [(0, 1), (1, 2), (4, 4), (5, 25), (6, 26), (7, 27)]:
        sin, cos = HAND_HOURS[step]
        assert feats.loc[row_id, ["hour_sin", "hour_cos"]].tolist() == pytest.approx([sin, cos])


def test_hand_set_b_balance_columns(hand_raw, contract):
    feats = build_table(hand_raw, "B", CONFIG, contract).frame.set_index("row_id")
    assert feats.loc[0, "src_balance_before"] == 100.0
    assert feats.loc[0, "amount_to_balance"] == pytest.approx(0.1)
    assert feats.loc[2, "amount_to_balance"] == pytest.approx(0.1)  # 5 / 50
    assert feats.loc[1, "amount_to_balance"] == 0.0  # saldo 0: relleno pendiente de contrato


# --- columnas y guarda -----------------------------------------------------------------------------


def test_columns_follow_contract_order(raw, contract):
    a = build_table(raw, "A", CONFIG, contract)
    b = build_table(raw, "B", CONFIG, contract)
    assert len(a.feature_columns) == 17
    assert b.feature_columns == a.feature_columns + ["src_balance_before", "amount_to_balance"]
    assert not set(a.frame.columns) & BALANCE_COLUMNS
    assert not set(a.frame.columns) & set(contract["prohibited_inputs"])
    assert not (a.frame[a.feature_columns].isna().any()).any()
    assert feature_columns("A", contract)[:2] == ["amount", "log_amount"]


@pytest.mark.parametrize(
    "column", ["newbalanceOrig", "oldbalanceDest", "newbalanceDest", "isFlaggedFraud"]
)
@pytest.mark.parametrize("feature_set", SETS)
def test_guard_rejects_prohibited_inputs(contract, feature_set, column):
    with pytest.raises(LeakageError, match=column):
        check_matrix_columns(["amount", column], feature_set, contract)


@pytest.mark.parametrize("column", sorted(BALANCE_COLUMNS))
def test_guard_rejects_any_balance_in_set_a(contract, column):
    with pytest.raises(LeakageError):
        check_matrix_columns(["amount", column], "A", contract)


def test_guard_allows_balances_only_in_set_b(contract):
    check_matrix_columns(["amount", "src_balance_before", "amount_to_balance"], "B", contract)
    check_matrix_columns(feature_columns("A", contract), "A", contract)


def test_guard_runs_inside_build_table(raw, contract, monkeypatch):
    """Si la lista de features incluyera una columna prohibida, build_table lanza error."""
    from verabank_ml import features

    real = features.feature_columns
    monkeypatch.setattr(features, "feature_columns", lambda s, c: real(s, c) + ["newbalanceDest"])
    with pytest.raises(LeakageError):
        build_table(raw, "A", CONFIG, contract)
