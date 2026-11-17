"""§5b, puntos 4 y 5: el escalador va dentro de un Pipeline ajustado solo con train y no hay remuestreo."""

import importlib.metadata
import importlib.util

import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from verabank_ml.features import REPO_ROOT, load_config
from verabank_ml.modeling import make_logreg

ML = REPO_ROOT / "ml"


def test_make_logreg_is_a_pipeline_with_the_scaler_first():
    model = make_logreg(load_config(), C=1.0)
    assert isinstance(model, Pipeline)
    assert [type(step).__name__ for _, step in model.steps] == ["StandardScaler", "LogisticRegression"]
    assert isinstance(model.steps[0][1], StandardScaler)
    assert isinstance(model.steps[-1][1], LogisticRegression)
    assert model.steps[-1][1].class_weight == "balanced"  # pesos de clase, no remuestreo


def test_the_scaler_is_fitted_only_with_the_rows_given_to_fit():
    rng = np.random.default_rng(0)
    train = rng.normal(0.0, 1.0, (500, 3))
    later = rng.normal(50.0, 1.0, (500, 3))  # otra partición con otra escala: no debe influir
    y = (rng.random(500) < 0.2).astype(int)
    model = make_logreg(load_config(), C=1.0).fit(train, y)
    scaler = model.steps[0][1]
    np.testing.assert_allclose(scaler.mean_, train.mean(axis=0))
    assert not np.allclose(scaler.mean_, np.vstack([train, later]).mean(axis=0))


def test_imblearn_is_neither_installed_nor_declared():
    assert importlib.util.find_spec("imblearn") is None
    installed = {d.metadata["Name"].lower() for d in importlib.metadata.distributions()}
    assert "imbalanced-learn" not in installed and "imblearn" not in installed
    for name in ("pyproject.toml", "requirements.txt", "uv.lock"):
        text = (ML / name).read_text().lower()
        assert "imbalanced-learn" not in text and "imblearn" not in text, name
