"""Escenarios de features de contracts/fixtures/ (T-13). Sin datos ni servicios: solo ml/ y contracts/."""

import json
import re
from datetime import datetime

import numpy as np
import pandas as pd
import pytest

from verabank_ml import golden
from verabank_ml.cli import main
from verabank_ml.features import REPO_ROOT, build_table, load_config, load_contract
from verabank_ml.fixtures import OUT_PATH, build_fixtures, render
from verabank_ml.onnx_export import predict_positive_proba

ARTIFACTS = REPO_ROOT / "ml" / "artifacts"
FIXTURES_DIR = REPO_ROOT / "contracts" / "fixtures"
PROHIBITED = ["newbalanceOrig", "oldbalanceDest", "newbalanceDest", "isFlaggedFraud"]
PAYSIM_ACCOUNT = re.compile(r"^[CM]\d{6,}")
REF = re.compile(r"(src|dst)-\d{2,}")
DECIMAL = re.compile(r"\d+(\.\d+)?")
ISO_UTC_ON_THE_HOUR = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:00:00Z")
FEATURE_TOLERANCE = 1e-9
SCORE_TOLERANCE = 1e-5


@pytest.fixture(scope="module")
def fixtures() -> dict:
    return json.loads(OUT_PATH.read_text())


@pytest.fixture(scope="module")
def thresholds() -> dict:
    return json.loads((ARTIFACTS / "thresholds.json").read_text())


@pytest.fixture(scope="module")
def cfg() -> dict:
    return load_config()


def all_events(fixtures):
    for scenario in fixtures["scenarios"]:
        for index, event in enumerate(scenario["events"]):
            yield scenario, index, event


def scenario_by_name(fixtures, name):
    return next(s for s in fixtures["scenarios"] if s["name"] == name)


# --- estructura ------------------------------------------------------------------------------------


def test_fixtures_describe_the_served_model(fixtures, thresholds):
    assert fixtures["feature_order"] == thresholds["feature_order"]
    assert fixtures["model_version"] == thresholds["model_version"]
    assert fixtures["model_sha256"] == thresholds["model_sha256"]


def test_there_are_at_least_twelve_small_scenarios(fixtures):
    scenarios = fixtures["scenarios"]
    assert len(scenarios) >= 12
    assert len({s["name"] for s in scenarios}) == len(scenarios)
    for scenario in scenarios:
        assert 1 <= len(scenario["events"]) <= 12, scenario["name"]


def test_events_have_the_documented_shape(fixtures, cfg):
    epoch = golden.parse_epoch(cfg["sim_epoch"])
    for scenario in fixtures["scenarios"]:
        times = [e["occurred_at"] for e in scenario["events"]]
        assert times == sorted(times), scenario["name"]  # ISO 8601 UTC: el texto ordena como el tiempo
        for event in scenario["events"]:
            assert set(event) == {
                "source_ref",
                "dest_ref",
                "transfer_type",
                "amount",
                "occurred_at",
                "expected_features",
                "expected_score",
            }
            assert REF.fullmatch(event["source_ref"]) and event["source_ref"].startswith("src-")
            assert REF.fullmatch(event["dest_ref"]) and event["dest_ref"].startswith("dst-")
            assert event["transfer_type"] in cfg["types"]
            assert isinstance(event["amount"], str) and DECIMAL.fullmatch(event["amount"])
            assert ISO_UTC_ON_THE_HOUR.fullmatch(event["occurred_at"]), event["occurred_at"]
            instant = datetime.fromisoformat(event["occurred_at"])
            assert instant >= epoch
            assert list(event["expected_features"]) == fixtures["feature_order"]


def test_scenarios_cover_the_requested_cases(fixtures, cfg):
    types = {e["transfer_type"] for _, _, e in all_events(fixtures)}
    assert types == set(cfg["types"])
    hour_pairs = {
        (round(e["expected_features"]["hour_sin"], 9), round(e["expected_features"]["hour_cos"], 9))
        for _, _, e in all_events(fixtures)
    }
    assert len(hour_pairs) >= 12  # horas del día distintas
    amounts = [float(e["amount"]) for _, _, e in all_events(fixtures)]
    assert min(amounts) <= 0.01 and max(amounts) >= 1e9
    features = [e["expected_features"] for _, _, e in all_events(fixtures)]
    assert max(f["dst_distinct_src_24h"] for f in features) >= 3
    assert max(f["dst_age_hours"] for f in features) >= 300


# --- respuestas conocidas, escritas a mano ---------------------------------------------------------


def column(fixtures, scenario, name):
    return [e["expected_features"][name] for e in scenario_by_name(fixtures, scenario)["events"]]


def test_window_edges_s_minus_1_s_minus_24_and_s_minus_25(fixtures):
    # Eventos en las horas 0, 24, 25, 26 (otro origen), 49 y 50 del par src-01 -> dst-01.
    assert column(fixtures, "window_24h_edges_s24_s25", "src_out_count_24h") == [0, 1, 1, 0, 1, 1]
    assert column(fixtures, "window_24h_edges_s24_s25", "dst_in_count_24h") == [0, 1, 1, 2, 2, 2]
    assert column(fixtures, "src_one_hour_window", "src_out_count_1h") == [0, 1, 0]
    assert column(fixtures, "src_one_hour_window", "src_out_count_24h") == [0, 1, 2]


def test_events_of_the_same_step_do_not_see_each_other(fixtures):
    scenario = "same_step_events_do_not_see_each_other"
    assert column(fixtures, scenario, "dst_in_count_24h") == [0, 0, 0, 3]
    assert column(fixtures, scenario, "dst_seen_before") == [0, 0, 0, 1]
    assert column(fixtures, scenario, "is_new_beneficiary") == [1, 1, 1, 0]


def test_destination_age_and_distinct_sources(fixtures):
    assert column(fixtures, "dst_age_hours", "dst_age_hours") == [0, 5, 100, 300, 600]
    assert column(fixtures, "dst_distinct_sources_24h", "dst_distinct_src_24h") == [
        0,
        1,
        2,
        3,
        3,
        3,
        0,
    ]
    assert column(fixtures, "merchant_payments_are_history", "dst_seen_before") == [0, 1, 1]
    assert column(fixtures, "merchant_payments_are_history", "is_new_beneficiary") == [1, 1, 0]


# --- paridad con la implementación vectorizada -----------------------------------------------------


def test_expected_features_match_the_vectorized_implementation(fixtures, cfg, thresholds):
    """No basta con reference.py: features.py (la del entrenamiento) debe dar lo mismo sobre los mismos eventos."""
    epoch = golden.parse_epoch(cfg["sim_epoch"])
    contract = load_contract()
    order = thresholds["feature_order"]
    for scenario in fixtures["scenarios"]:
        events = scenario["events"]
        raw = pd.DataFrame(
            {
                "step": [
                    int((datetime.fromisoformat(e["occurred_at"]) - epoch).total_seconds() // 3600) + 1
                    for e in events
                ],
                "type": [e["transfer_type"] for e in events],
                "amount": [float(e["amount"]) for e in events],
                "nameOrig": [e["source_ref"] for e in events],
                "nameDest": [e["dest_ref"] for e in events],
                "isFraud": 0,
            }
        )
        table = build_table(raw, "A", cfg, contract)
        assert table.feature_columns == order
        expected = np.array([[e["expected_features"][n] for n in order] for e in events])
        np.testing.assert_allclose(
            table.frame[order].to_numpy(),
            expected,
            rtol=FEATURE_TOLERANCE,
            atol=FEATURE_TOLERANCE,
            err_msg=scenario["name"],
        )


def test_expected_score_matches_onnx_runtime(fixtures, thresholds):
    model = (ARTIFACTS / "model.onnx").read_bytes()
    order = thresholds["feature_order"]
    for scenario in fixtures["scenarios"]:
        events = scenario["events"]
        X = np.array([[e["expected_features"][n] for n in order] for e in events], dtype=np.float32)
        actual = predict_positive_proba(model, X)
        expected = np.array([e["expected_score"] for e in events])
        np.testing.assert_allclose(actual, expected, rtol=0, atol=SCORE_TOLERANCE, err_msg=scenario["name"])
        assert ((expected >= 0) & (expected <= 1)).all()


# --- determinismo y limpieza -----------------------------------------------------------------------


def test_fixtures_are_deterministic_and_the_file_is_up_to_date():
    first, second = render(build_fixtures()), render(build_fixtures())
    assert first == second
    assert OUT_PATH.read_text() == first, "ejecuta make ml-fixtures: el archivo está desactualizado"


def test_fixtures_command_is_deterministic(tmp_path):
    out_a, out_b = tmp_path / "a" / "f.json", tmp_path / "b" / "f.json"
    assert main(["fixtures", "--out", str(out_a)]) == 0
    assert main(["fixtures", "--out", str(out_b)]) == 0
    assert out_a.read_bytes() == out_b.read_bytes() == OUT_PATH.read_bytes()


def test_no_paysim_accounts_or_prohibited_columns_in_artifacts_and_fixtures():
    files = [p for d in (ARTIFACTS, FIXTURES_DIR) for p in d.rglob("*") if p.suffix in (".json", ".jsonl", ".md")]
    assert any(p.parent == FIXTURES_DIR for p in files)
    for path in files:
        text = path.read_text()
        label = str(path.relative_to(REPO_ROOT))
        assert not re.search(r"[CM]\d{6,}", text), f"nombre de cuenta de PaySim en {label}"
        if path.suffix != ".md":
            for column_name in PROHIBITED:
                assert column_name not in text, f"{column_name} en {label}"
    for _, _, event in all_events(json.loads(OUT_PATH.read_text())):
        assert not PAYSIM_ACCOUNT.match(event["source_ref"]) and not PAYSIM_ACCOUNT.match(event["dest_ref"])


# --- el README es el contrato que implementa el servicio Java --------------------------------------


def test_readme_contract_section_lists_the_columns_in_order(thresholds):
    text = (REPO_ROOT / "ml" / "README.md").read_text()
    start = text.index("## Contrato con el servicio de scoring")
    section = text[start : text.index("\n## ", start + 1)]
    cells = re.findall(r"^\| (\d+) \| `(\w+)` \| (?:(\d+) \| `(\w+)` )?\|", section, flags=re.MULTILINE)
    listed = {}
    for left_index, left, right_index, right in cells:
        listed[int(left_index)] = left
        if right_index:
            listed[int(right_index)] = right
    assert [listed[i] for i in range(len(listed))] == thresholds["feature_order"]
    for required in ("ROUND_HALF_EVEN", "golden.py:32", "[t - w, t)", "(step - 1) h", "% 24"):
        assert required in section, required
