"""Paridad de model.onnx frente a predict_proba del champion de MLflow (marcador `data`: necesita make ml-train)."""

import json

import numpy as np
import pytest

from verabank_ml import modeling, tracking
from verabank_ml.export import load_alias
from verabank_ml.features import REPO_ROOT, load_config
from verabank_ml.onnx_export import predict_positive_proba

pytestmark = pytest.mark.data

ARTIFACTS = REPO_ROOT / "ml" / "artifacts"
ROWS = 100_000
TOLERANCE = 1e-5


def test_onnx_matches_predict_proba_on_100k_valid_rows():
    cfg = load_config()
    thresholds = json.loads((ARTIFACTS / "thresholds.json").read_text())
    cols = thresholds["feature_order"]
    client = tracking.setup(cfg)
    _, name, version, model = load_alias(client, cfg, "champion")
    assert f"{name}/{version}" == thresholds["model_version"]
    valid = modeling.load_features(
        thresholds["feature_set"], ("valid",), columns=[*modeling.META_COLUMNS, *cols]
    )
    rows = np.random.default_rng(cfg["seed"]).choice(len(valid), size=ROWS, replace=False)
    X = modeling.matrix(valid.iloc[rows], cols)
    expected = modeling.positive_proba(model, X)
    actual = predict_positive_proba((ARTIFACTS / "model.onnx").read_bytes(), X)
    worst = float(np.abs(actual - expected).max())
    print(f"diferencia máxima ONNX frente a predict_proba en {ROWS:,} filas: {worst:.3e}")
    assert worst < TOLERANCE, f"diferencia máxima {worst:.3e}"
