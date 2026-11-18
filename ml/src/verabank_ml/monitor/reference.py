"""``psi-reference`` (make ml-psi-ref): referencia del PSI desde la partición de entrenamiento (D1).

Lee **solo** ``partition = 'train'`` de ``data/features/features_A.parquet`` y escribe ``ml/artifacts/psi_reference.json``
con agregados (bins, proporciones esperadas, ``top_features``), nunca filas ni marcas de tiempo: dos ejecuciones con
los mismos datos y el mismo champion dan el mismo archivo byte a byte. Registra su hash en ``manifest.json``.

- Score de referencia: el del champion (alias ``champion`` de MLflow) sobre entrenamiento, redondeado a 5 decimales
  como el que guarda ``risk.scoring_log``.
- ``top_features``: las 5 de mayor SHAP medio absoluto (TreeSHAP exacto de XGBoost, ``pred_contribs``) sobre una
  muestra determinista de entrenamiento si sirve XGBoost; ``|coef · desviación|`` si sirve la logística (con el
  ``StandardScaler`` del pipeline es ``|coef|`` del espacio estandarizado). Desempate por nombre.
"""

from __future__ import annotations

import hashlib
import json

import numpy as np

from verabank_ml import modeling, tracking
from verabank_ml.features import REPO_ROOT, load_config
from verabank_ml.monitor import binning

ARTIFACTS = REPO_ROOT / "ml" / "artifacts"
REFERENCE_PATH = ARTIFACTS / "psi_reference.json"
MANIFEST_PATH = ARTIFACTS / "manifest.json"
THRESHOLDS_PATH = ARTIFACTS / "thresholds.json"
MANIFEST_KEY = "psi_reference.json"
TOP_N = 5
SHAP_SAMPLE = 200_000
IMPORTANCE_DECIMALS = 9


def top_features(importance: dict[str, float], n: int = TOP_N) -> list[str]:
    return [k for k, _ in sorted(importance.items(), key=lambda kv: (-kv[1], kv[0]))[:n]]


def xgb_importance(model, X: np.ndarray, names: list[str], seed: int, sample: int = SHAP_SAMPLE) -> dict[str, float]:
    import xgboost as xgb

    if X.shape[0] > sample:
        rows = np.sort(np.random.default_rng(seed).choice(X.shape[0], sample, replace=False))
        X = X[rows]
    contribs = model.get_booster().predict(xgb.DMatrix(X, feature_names=names), pred_contribs=True)
    mean_abs = np.abs(contribs[:, :-1]).mean(axis=0)          # la última columna es el valor base
    return {k: round(float(v), IMPORTANCE_DECIMALS) for k, v in zip(names, mean_abs, strict=True)}


def logreg_importance(pipeline, names: list[str]) -> dict[str, float]:
    coef = pipeline[-1].coef_.ravel()                          # sobre features estandarizadas: coef·desv
    return {k: round(float(abs(v)), IMPORTANCE_DECIMALS) for k, v in zip(names, coef, strict=True)}


def build_reference(train, model, family: str, thresholds: dict, seed: int) -> dict:
    cols = thresholds["feature_order"]
    X = modeling.matrix(train, cols)
    score = np.round(modeling.positive_proba(model, X), 5)
    importance = (xgb_importance(model, X, cols, seed) if family == "xgb" else logreg_importance(model, cols))
    features = {}
    for name in cols:
        bins = binning.fit_bins(train[name].to_numpy())
        features[name] = {**bins.to_json(), "expected": [float(p) for p in binning.proportions(bins, train[name].to_numpy())]}
    score_bins = binning.fit_bins(score)
    steps = train["step"].to_numpy()
    return {
        "version": 1,
        "model_version": thresholds["model_version"],
        "model_sha256": thresholds["model_sha256"],
        "model_family": family,
        "feature_set": thresholds["feature_set"],
        "partition": "train",
        "train_steps": [int(steps.min()), int(steps.max())],
        "n_train": len(train),
        "method": {"bins": "deciles con bordes únicos; <= 10 valores: uno por valor + otro", "floor": binning.FLOOR,
                   "decimals": binning.DECIMALS, "psi": "sum((a - e) * ln(a / e))",
                   "top_features": "mean |SHAP| (xgb, muestra de entrenamiento)" if family == "xgb" else "|coef * std|",
                   "shap_sample": min(SHAP_SAMPLE, len(train)) if family == "xgb" else None},
        "top_features": top_features(importance),
        "importance": importance,
        "features": features,
        "score": {**score_bins.to_json(), "expected": [float(p) for p in binning.proportions(score_bins, score)]},
    }


def write_reference(reference: dict) -> str:
    payload = (json.dumps(reference, indent=2, ensure_ascii=False) + "\n").encode()
    REFERENCE_PATH.write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    manifest = json.loads(MANIFEST_PATH.read_text())
    manifest["artifacts"][MANIFEST_KEY] = {"sha256": digest, "model_sha256": reference["model_sha256"]}
    tracking.dump_json(MANIFEST_PATH, manifest)
    return digest


def psi_reference_command(argv: list[str]) -> int:
    del argv
    from verabank_ml.export import load_alias

    cfg = load_config()
    thresholds = json.loads(THRESHOLDS_PATH.read_text())
    client = tracking.setup(cfg)
    family, name, version, model = load_alias(client, cfg, "champion")
    if f"{name}/{version}" != thresholds["model_version"]:
        raise RuntimeError(f"el champion {name}/{version} no es {thresholds['model_version']}: ejecuta make ml-export")
    train = modeling.load_features(thresholds["feature_set"], ("train",),
                                   columns=["step", "partition", *thresholds["feature_order"]])
    if set(train["partition"].unique()) != {"train"}:
        raise RuntimeError("la referencia solo usa la partición train")
    reference = build_reference(train, model, family, thresholds, cfg["seed"])
    digest = write_reference(reference)
    print(f"psi_reference.json: {len(train):,} filas de train (pasos {reference['train_steps']}), "
          f"top_features {reference['top_features']}, sha256 {digest[:12]}…")
    return 0
