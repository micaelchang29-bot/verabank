"""T-13 (lado Python): artefactos versionados de ml/artifacts/. Sin datos ni servicios: corren en la CI."""

import hashlib
import json
import re

import numpy as np
import onnxruntime as ort
import pytest

from verabank_ml import golden
from verabank_ml.features import CONTRACT_PATH, REPO_ROOT, load_config, load_contract
from verabank_ml.onnx_export import INPUT_NAME, predict_positive_proba
from verabank_ml.reference import reference_features

ARTIFACTS = REPO_ROOT / "ml" / "artifacts"
PROHIBITED = ["newbalanceOrig", "oldbalanceDest", "newbalanceDest", "isFlaggedFraud"]
ORIGINAL_ACCOUNT = re.compile(
    r"[CM]\d{6,}"
)  # nombres de cuenta de PaySim, p. ej. C1305486145, M1979787155
ALIAS = re.compile(r"a[1-9]\d*")
SCORE_TOLERANCE = 1e-5


def sha256(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture(scope="module")
def thresholds() -> dict:
    return json.loads((ARTIFACTS / "thresholds.json").read_text())


@pytest.fixture(scope="module")
def manifest() -> dict:
    return json.loads((ARTIFACTS / "manifest.json").read_text())


@pytest.fixture(scope="module")
def cases() -> list[dict]:
    lines = (ARTIFACTS / "golden.jsonl").read_text().splitlines()
    return [json.loads(line) for line in lines]


@pytest.fixture(scope="module")
def cfg() -> dict:
    return load_config()


def decision_for(score: float, thresholds: dict) -> str:
    rounded = round(score, 5)
    if rounded >= thresholds["t_block"]:
        return "BLOCK"
    return "REVIEW" if rounded >= thresholds["t_review"] else "APPROVE"


# --- T-13 ------------------------------------------------------------------------------------------


def test_onnx_runtime_reproduces_every_golden_score(cases, thresholds):
    order = thresholds["feature_order"]
    X = np.array([[c["features"][name] for name in order] for c in cases], dtype=np.float32)
    scores = np.array([c["score"] for c in cases])
    actual = predict_positive_proba((ARTIFACTS / "model.onnx").read_bytes(), X)
    worst = np.abs(actual - scores).max()
    assert len(cases) == 1000
    assert worst < SCORE_TOLERANCE, f"diferencia máxima {worst:.3e}"


def test_features_recomputed_from_history_match_golden(cases, thresholds, cfg):
    contract = load_contract()
    categories = next(f for f in contract["features"] if f["name"] == "type")["categories"]
    epoch = golden.parse_epoch(cfg["sim_epoch"])
    order = thresholds["feature_order"]
    for case in cases:
        rows, request_index = golden.case_rows(case, epoch)
        (recomputed,) = reference_features(
            rows, "A", cfg["types"], categories, targets=[request_index]
        )
        expected = case["features"]
        assert list(expected) == order, case["case_id"]
        actual = np.array([recomputed[name] for name in order], dtype=np.float64)
        np.testing.assert_allclose(
            actual,
            [expected[name] for name in order],
            rtol=1e-9,
            atol=1e-9,
            err_msg=case["case_id"],
        )


def test_stored_decision_follows_the_rounded_score_rule(cases, thresholds):
    for case in cases:
        assert case["decision"] == decision_for(case["score"], thresholds), case["case_id"]


def test_boundary_cases_fall_on_the_expected_side(cases, thresholds, cfg):
    distance = cfg["golden"]["boundary_distance"]
    seen_crossing = False
    for case in cases:
        for name in ("t_review", "t_block"):
            t = thresholds[name]
            if abs(case["score"] - t) >= distance:
                continue
            above = round(case["score"], 5) >= t
            order = ["APPROVE", "REVIEW", "BLOCK"]
            reached = order.index(case["decision"]) >= (1 if name == "t_review" else 2)
            assert reached == above, (case["case_id"], name)
            seen_crossing |= case["score"] < t <= round(case["score"], 5)
    assert seen_crossing, "ningún caso en que el redondeo a 5 decimales cambie el lado del umbral"
    near = [
        c
        for c in cases
        if any(abs(c["score"] - thresholds[n]) < distance for n in ("t_review", "t_block"))
    ]
    assert any(c["score"] < thresholds["t_block"] <= round(c["score"], 5) for c in near)
    assert {round(c["score"], 5) >= thresholds["t_block"] for c in near} == {True, False}


# --- composición y estructura ----------------------------------------------------------------------


def test_golden_composition(cases, cfg):
    strata = [c["stratum"] for c in cases]
    gcfg = cfg["golden"]
    assert strata.count("fraud") == gcfg["fraud"]
    assert strata.count("above_review") == gcfg["above_review"]
    assert 0 < strata.count("boundary") <= gcfg["boundary"]
    assert len(cases) == gcfg["total"]
    assert len({c["case_id"] for c in cases}) == len(cases)


def test_history_is_earlier_allowed_and_bounded(cases, cfg):
    max_history = cfg["golden"]["max_history"]
    for case in cases:
        request = case["request"]
        assert request["type"] in cfg["types"]
        assert len(case["history"]) <= max_history
        times = [row["occurred_at"] for row in case["history"]]
        assert times == sorted(times)
        for row in case["history"]:
            assert (
                row["occurred_at"] < request["occurred_at"]
            )  # ISO 8601 UTC: el orden de texto es el temporal
            assert row["type"] in cfg["types"]
            assert row["source"] == request["source"] or row["dest"] == request["dest"], case[
                "case_id"
            ]


def test_accounts_are_opaque_local_aliases(cases):
    for case in cases:
        names = [case["request"]["source"], case["request"]["dest"]]
        for row in case["history"]:
            names += [row["source"], row["dest"]]
        assert all(ALIAS.fullmatch(n) for n in names), case["case_id"]
        numbers = sorted({int(n[1:]) for n in names})
        assert numbers == list(range(1, len(numbers) + 1)), (
            "alias no contiguos en " + case["case_id"]
        )


def test_no_original_account_names_or_prohibited_columns(cfg):
    for name in ("golden.jsonl", "thresholds.json", "manifest.json", "README.md"):
        text = (ARTIFACTS / name).read_text()
        assert not ORIGINAL_ACCOUNT.search(text), f"nombre de cuenta original en {name}"
    for name in ("golden.jsonl", "thresholds.json", "manifest.json"):
        text = (ARTIFACTS / name).read_text()
        for column in PROHIBITED:
            assert column not in text, f"{column} en {name}"
    report = (REPO_ROOT / "ml" / "reports" / "golden_selection.json").read_text()
    assert not ORIGINAL_ACCOUNT.search(report)


# --- hashes y manifiesto ---------------------------------------------------------------------------


def test_model_sha256_in_thresholds_matches_the_file(thresholds):
    assert thresholds["model_sha256"] == sha256(ARTIFACTS / "model.onnx")


def test_manifest_matches_the_files(manifest):
    for name, entry in manifest["artifacts"].items():
        assert entry["sha256"] == sha256(ARTIFACTS / name), name
    assert manifest["artifacts"]["golden.jsonl"]["cases"] == 1000
    assert manifest["inputs"]["contracts/features.yaml"] == sha256(CONTRACT_PATH)


def test_manifest_records_the_real_onnx_opsets_ir_and_runtime_version(manifest):
    import onnx

    assert manifest["onnxruntime_version"] == ort.__version__
    for name in ("model.onnx", "shadow.onnx"):
        model = onnx.load(str(ARTIFACTS / name))
        entry = manifest["artifacts"][name]
        assert entry["ir_version"] == model.ir_version, name
        declared = {(o["domain"], o["version"]) for o in entry["opset_imports"]}
        assert declared == {(o.domain or "ai.onnx", o.version) for o in model.opset_import}, name
        assert len(entry["opset_imports"]) == len(model.opset_import), name


def test_manifest_and_thresholds_agree_on_the_champion(manifest, thresholds):
    champion = manifest["models"]["champion"]
    assert thresholds["model_version"] == f"{champion['registered_model']}/{champion['version']}"
    assert manifest["model_io"]["input_name"] == INPUT_NAME
    assert manifest["model_io"]["positive_class_column"] == 1
    assert manifest["models"]["challenger"]["registered_model"] != champion["registered_model"]


def test_thresholds_contract_is_complete(thresholds):
    assert thresholds["rounding_decimals"] == 5
    assert thresholds["t_review"] <= thresholds["t_block"]
    assert thresholds["feature_order"][0] == "amount" and len(thresholds["feature_order"]) == 17
    for key in ("t_review", "t_block"):
        assert round(thresholds[key], 5) == thresholds[key]


def test_onnx_graphs_are_deterministic_and_have_the_declared_io():
    for name, family in (("model.onnx", "xgb"), ("shadow.onnx", "logreg")):
        session = ort.InferenceSession(str(ARTIFACTS / name), providers=["CPUExecutionProvider"])
        (input_,) = session.get_inputs()
        assert input_.name == INPUT_NAME and input_.shape[1] == 17
        assert [o.name for o in session.get_outputs()] == ["label", "probabilities"]
        import onnx

        assert onnx.load(str(ARTIFACTS / name)).graph.name == f"verabank_fraud_{family}"


def test_shadow_model_scores_the_golden_features(cases, thresholds):
    X = np.array(
        [[c["features"][n] for n in thresholds["feature_order"]] for c in cases], dtype=np.float32
    )
    probabilities = predict_positive_proba((ARTIFACTS / "shadow.onnx").read_bytes(), X)
    assert probabilities.shape == (1000,)
    assert ((probabilities >= 0) & (probabilities <= 1)).all()
