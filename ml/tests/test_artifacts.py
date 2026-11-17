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
    per_side = cfg["golden"]["boundary_per_side"]
    boundary = [c for c in cases if c["stratum"] == "boundary"]
    order = ["APPROVE", "REVIEW", "BLOCK"]
    crossings = 0
    for name, level in (("t_review", 1), ("t_block", 2)):
        t = thresholds[name]
        # cada caso de frontera se asigna al umbral más cercano (los dos umbrales están muy separados)
        own = [c for c in boundary if abs(c["score"] - t) == min(abs(c["score"] - thresholds[n]) for n in ("t_review", "t_block"))]
        above = [c for c in own if round(c["score"], 5) >= t]
        below = [c for c in own if round(c["score"], 5) < t]
        assert 1 <= len(above) <= per_side and 1 <= len(below) <= per_side, name
        assert len({c["score"] for c in own}) == len(own), "scores distintos"
        for c in above:
            assert order.index(c["decision"]) >= level, (c["case_id"], name)
        for c in below:
            assert order.index(c["decision"]) < level, (c["case_id"], name)
        crossings += sum(1 for c in own if c["score"] < t <= round(c["score"], 5))
    assert crossings >= 1, "ningún caso en que el redondeo a 5 decimales cambie el lado del umbral"


# --- composición y estructura ----------------------------------------------------------------------


def test_golden_composition(cases, cfg):
    strata = [c["stratum"] for c in cases]
    gcfg = cfg["golden"]
    assert strata.count("fraud") == gcfg["fraud"]
    assert strata.count("above_review") == gcfg["above_review"]
    assert 1 <= strata.count("boundary") <= 4 * gcfg["boundary_per_side"]
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
    texts = sorted(p for p in ARTIFACTS.rglob("*") if p.suffix in (".json", ".jsonl", ".md"))
    assert {p.name for p in texts} >= {"golden_challenger.jsonl", "golden.jsonl", "manifest.json"}
    for path in texts:
        name = str(path.relative_to(ARTIFACTS))
        text = path.read_text()
        assert not ORIGINAL_ACCOUNT.search(text), f"nombre de cuenta original en {name}"
        if path.suffix != ".md":
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
    for name in ("model.onnx", "challenger/model.onnx"):
        model = onnx.load(str(ARTIFACTS / name))
        entry = manifest["artifacts"][name]
        assert entry["ir_version"] == model.ir_version, name
        declared = {(o["domain"], o["version"]) for o in entry["opset_imports"]}
        assert declared == {(o.domain or "ai.onnx", o.version) for o in model.opset_import}, name
        assert len(entry["opset_imports"]) == len(model.opset_import), name


def test_thresholds_and_manifest_share_the_provenance(manifest, thresholds):
    """Mismo `config.yaml` (el vigente) y el mismo commit de entrenamiento, sin árbol sucio."""
    config_hash = sha256(REPO_ROOT / "ml" / "config.yaml")
    prov = thresholds["provenance"]
    assert prov["config_yaml_sha256"] == manifest["inputs"]["ml/config.yaml"] == config_hash
    assert prov["features_yaml_sha256"] == manifest["inputs"]["contracts/features.yaml"]
    assert prov["csv_sha256"] == manifest["inputs"]["paysim.csv"]
    assert manifest["git_commit"] == prov["git_commit"]
    assert re.fullmatch(r"[0-9a-f]{40}", prov["git_commit"]), prov["git_commit"]  # sin «-dirty»


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
    for name, family in (("model.onnx", "xgb"), ("challenger/model.onnx", "logreg")):
        session = ort.InferenceSession(str(ARTIFACTS / name), providers=["CPUExecutionProvider"])
        (input_,) = session.get_inputs()
        assert input_.name == INPUT_NAME and input_.shape[1] == 17
        assert [o.name for o in session.get_outputs()] == ["label", "probabilities"]
        import onnx

        assert onnx.load(str(ARTIFACTS / name)).graph.name == f"verabank_fraud_{family}"


def challenger_cases() -> list[dict]:
    lines = (ARTIFACTS / "golden_challenger.jsonl").read_text().splitlines()
    return [json.loads(line) for line in lines]


def test_challenger_matches_golden_challenger_scores(cases, thresholds):
    """Paridad del challenger: ONNX Runtime reproduce el score float64 de golden_challenger.jsonl."""
    scores = challenger_cases()
    assert [c["case_id"] for c in scores] == [c["case_id"] for c in cases]
    assert all(set(c) == {"case_id", "score"} for c in scores)  # sin features ni decisión
    X = np.array(
        [[c["features"][n] for n in thresholds["feature_order"]] for c in cases], dtype=np.float32
    )
    actual = predict_positive_proba((ARTIFACTS / "challenger" / "model.onnx").read_bytes(), X)
    expected = np.array([c["score"] for c in scores])
    assert np.abs(actual - expected).max() < SCORE_TOLERANCE
    assert ((actual >= 0) & (actual <= 1)).all()


def test_challenger_has_the_same_io_contract_as_the_champion(thresholds):
    def signature(path):
        session = ort.InferenceSession(str(path), providers=["CPUExecutionProvider"])
        return (
            [(i.name, i.type, i.shape) for i in session.get_inputs()],
            [o.name for o in session.get_outputs()],
        )

    champion = signature(ARTIFACTS / "model.onnx")
    challenger = signature(ARTIFACTS / "challenger" / "model.onnx")
    assert challenger == champion
    assert champion[0][0][2][1] == len(thresholds["feature_order"])  # mismo feature_order: n y nombre


def test_challenger_has_no_thresholds_and_is_listed_in_the_manifest(manifest):
    assert not (ARTIFACTS / "challenger" / "thresholds.json").exists()
    entry = manifest["artifacts"]["golden_challenger.jsonl"]
    assert entry["cases"] == 1000
    assert manifest["artifacts"]["challenger/model.onnx"]["sha256"]
    challenger = manifest["models"]["challenger"]
    assert challenger["registered_model"] and challenger["version"]
