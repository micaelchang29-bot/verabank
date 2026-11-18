"""``psi-reference`` (make ml-psi-ref): referencias del PSI desde la partición de entrenamiento y la de validación (D1).

``--partition train`` (por defecto, §5f) lee **solo** ``partition = 'train'`` de ``data/features/features_A.parquet`` y
escribe ``ml/artifacts/psi_reference.json``. ``--partition valid`` lee solo ``valid`` (pasos 425–550, ya en el régimen
posterior al cambio cercano al paso 400) y escribe ``psi_reference_valid.json``, con los mismos ``top_features`` que la de
entrenamiento. Ambas guardan agregados (bins, proporciones esperadas), nunca filas ni marcas de tiempo: dos ejecuciones
con los mismos datos y el mismo champion dan el mismo archivo byte a byte. Registran su hash en ``manifest.json``.

- Score de referencia: el del champion (alias ``champion`` de MLflow) sobre entrenamiento, redondeado a 5 decimales
  como el que guarda ``risk.scoring_log``.
- ``top_features``: las 5 de mayor SHAP medio absoluto (TreeSHAP exacto de XGBoost, ``pred_contribs``) sobre una
  muestra determinista de entrenamiento si sirve XGBoost; ``|coef · desviación|`` si sirve la logística (con el
  ``StandardScaler`` del pipeline es ``|coef|`` del espacio estandarizado). Desempate por nombre.
"""

from __future__ import annotations

import argparse
import hashlib
import json

import numpy as np

from verabank_ml import modeling, tracking
from verabank_ml.features import REPO_ROOT, load_config
from verabank_ml.monitor import binning

ARTIFACTS = REPO_ROOT / "ml" / "artifacts"
REFERENCE_PATHS = {"train": ARTIFACTS / "psi_reference.json", "valid": ARTIFACTS / "psi_reference_valid.json"}
REFERENCE_PATH = REFERENCE_PATHS["train"]
MANIFEST_PATH = ARTIFACTS / "manifest.json"
THRESHOLDS_PATH = ARTIFACTS / "thresholds.json"
MANIFEST_KEYS = {name: path.name for name, path in REFERENCE_PATHS.items()}
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


def build_reference(train, model, family: str, thresholds: dict, seed: int, partition: str = "train",
                    inherited: dict | None = None) -> dict:
    """Referencia de ``train`` (la fuente de ``top_features``) o de ``valid``, que hereda ``inherited`` (la de train)."""
    cols = thresholds["feature_order"]
    X = modeling.matrix(train, cols)
    score = np.round(modeling.positive_proba(model, X), 5)
    if partition == "train":
        importance = xgb_importance(model, X, cols, seed) if family == "xgb" else logreg_importance(model, cols)
        top_method = "mean |SHAP| (xgb, muestra de entrenamiento)" if family == "xgb" else "|coef * std|"
        shap_sample = min(SHAP_SAMPLE, len(train)) if family == "xgb" else None
    else:
        importance = inherited["importance"]
        top_method = "heredadas de la referencia de entrenamiento (psi_reference.json)"
        shap_sample = None
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
        "partition": partition,
        f"{partition}_steps": [int(steps.min()), int(steps.max())],
        f"n_{partition}": len(train),
        "method": {"bins": "deciles con bordes únicos; <= 10 valores: uno por valor + otro", "floor": binning.FLOOR,
                   "decimals": binning.DECIMALS, "psi": "sum((a - e) * ln(a / e))",
                   "top_features": top_method, "shap_sample": shap_sample},
        "top_features": top_features(importance) if partition == "train" else list(inherited["top_features"]),
        "importance": importance,
        "features": features,
        "score": {**score_bins.to_json(), "expected": [float(p) for p in binning.proportions(score_bins, score)]},
    }


def write_reference(reference: dict) -> str:
    partition = reference["partition"]
    payload = (json.dumps(reference, indent=2, ensure_ascii=False) + "\n").encode()
    REFERENCE_PATHS[partition].write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    manifest = json.loads(MANIFEST_PATH.read_text())
    manifest["artifacts"][MANIFEST_KEYS[partition]] = {"sha256": digest, "model_sha256": reference["model_sha256"]}
    tracking.dump_json(MANIFEST_PATH, manifest)
    return digest


def psi_reference_command(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m verabank_ml psi-reference", description=__doc__.split("\n")[0])
    parser.add_argument("--partition", choices=sorted(REFERENCE_PATHS), default="train",
                        help="partición que da los bins y el score de referencia (por defecto train, §5f)")
    args = parser.parse_args(argv)
    partition = args.partition
    from verabank_ml.export import load_alias

    cfg = load_config()
    thresholds = json.loads(THRESHOLDS_PATH.read_text())
    client = tracking.setup(cfg)
    family, name, version, model = load_alias(client, cfg, "champion")
    if f"{name}/{version}" != thresholds["model_version"]:
        raise RuntimeError(f"el champion {name}/{version} no es {thresholds['model_version']}: ejecuta make ml-export")
    inherited = None
    if partition == "valid":
        if not REFERENCE_PATH.exists():
            raise RuntimeError("falta psi_reference.json (train): ejecuta primero make ml-psi-ref --partition train")
        inherited = json.loads(REFERENCE_PATH.read_text())
        if inherited["model_sha256"] != thresholds["model_sha256"]:
            raise RuntimeError("psi_reference.json es de otro champion: regenera primero la de train")
    data = modeling.load_features(thresholds["feature_set"], (partition,),
                                  columns=["step", "partition", *thresholds["feature_order"]])
    if set(data["partition"].unique()) != {partition}:
        raise RuntimeError(f"la referencia {partition} solo usa la partición {partition}")
    lo, hi = cfg["split"][partition]
    if data["step"].min() < lo or data["step"].max() > hi:
        raise RuntimeError(f"la partición {partition} tiene pasos fuera de [{lo}, {hi}]")
    reference = build_reference(data, model, family, thresholds, cfg["seed"], partition, inherited)
    digest = write_reference(reference)
    print(f"{REFERENCE_PATHS[partition].name}: {len(data):,} filas de {partition} (pasos {reference[f'{partition}_steps']}), "
          f"top_features {reference['top_features']}, sha256 {digest[:12]}…")
    return 0
