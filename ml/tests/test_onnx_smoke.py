"""Humo de conversión a ONNX (ADR-05): sin datos ni servicios, con datos sintéticos."""

import numpy as np
import pytest
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler
from xgboost import XGBClassifier

from verabank_ml.onnx_export import (
    predict_positive_proba,
    sklearn_pipeline_to_onnx,
    xgboost_to_onnx,
)

N_ROWS = 20_000
N_FEATURES = 17
POSITIVE_RATE = 0.01
SEED = 20240607
TOLERANCE = 1e-5


@pytest.fixture(scope="module")
def dataset() -> tuple[np.ndarray, np.ndarray]:
    rng = np.random.default_rng(SEED)
    X = rng.normal(size=(N_ROWS, N_FEATURES))
    y = np.zeros(N_ROWS, dtype=int)
    y[rng.choice(N_ROWS, size=int(N_ROWS * POSITIVE_RATE), replace=False)] = 1
    # Desplaza los positivos para que haya señal que aprender.
    X[y == 1, :5] += 1.5
    return X.astype(np.float32), y


def test_logistic_pipeline_matches_predict_proba(dataset):
    X, y = dataset
    pipeline = Pipeline(
        [
            ("scaler", StandardScaler()),
            ("clf", LogisticRegression(max_iter=1000, random_state=SEED)),
        ]
    ).fit(X, y)
    onnx_model = sklearn_pipeline_to_onnx(pipeline, N_FEATURES)
    expected = pipeline.predict_proba(X)[:, 1]
    actual = predict_positive_proba(onnx_model.SerializeToString(), X)
    assert actual.shape == expected.shape == (N_ROWS,)
    np.testing.assert_allclose(actual, expected, rtol=0, atol=TOLERANCE)


def test_xgboost_matches_predict_proba(dataset):
    X, y = dataset
    model = XGBClassifier(
        n_estimators=100, max_depth=4, tree_method="hist", random_state=SEED, n_jobs=1
    ).fit(X, y)
    onnx_model = xgboost_to_onnx(model, N_FEATURES)
    expected = model.predict_proba(X)[:, 1]
    actual = predict_positive_proba(onnx_model.SerializeToString(), X)
    assert actual.shape == expected.shape == (N_ROWS,)
    np.testing.assert_allclose(actual, expected, rtol=0, atol=TOLERANCE)
