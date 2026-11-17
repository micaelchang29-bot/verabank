"""Datos, modelos y evaluación en `valid` del modelo de fraude (conjuntos A y B)."""

import hashlib
import subprocess
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler
from xgboost import XGBClassifier

from verabank_ml import metrics
from verabank_ml.features import (
    CONFIG_PATH,
    CONTRACT_PATH,
    OUT_DIR,
    REPO_ROOT,
    feature_columns,
    load_contract,
)

META_COLUMNS = ["row_id", "step", "type", "is_fraud", "partition", "src_id", "dst_id"]
DATA_HASH_PATH = REPO_ROOT / "ml" / "data.sha256"
TEST = "test"


def load_features(
    feature_set: str,
    partitions: tuple[str, ...],
    columns: list[str] | None = None,
    allow_test: bool = False,
) -> pd.DataFrame:
    """Lee solo las particiones pedidas. `test` solo se puede leer con `allow_test=True` (ml-eval-test)."""
    if TEST in partitions and not allow_test:
        raise PermissionError("la partición test solo la lee make ml-eval-test")
    path = OUT_DIR / f"features_{feature_set}.parquet"
    if not path.exists():
        raise FileNotFoundError(f"falta {path}: ejecuta make ml-features")
    return pd.read_parquet(path, columns=columns, filters=[("partition", "in", list(partitions))])


def matrix(frame: pd.DataFrame, columns: list[str]) -> np.ndarray:
    """float32 en el orden de las features: es lo que recibirá el servicio."""
    return frame[columns].to_numpy(dtype=np.float32)


def sha256_of(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def provenance() -> dict:
    """Commit de Git y hashes de features.yaml, del CSV y de config.yaml."""

    def git(*args):
        return subprocess.run(
            ["git", *args], cwd=REPO_ROOT, check=False, capture_output=True, text=True
        ).stdout.strip()

    commit = git("rev-parse", "HEAD") or "desconocido"
    if git("status", "--porcelain", "--", "ml", "contracts"):
        commit += "-dirty"
    return {
        "git_commit": commit,
        "features_yaml_sha256": sha256_of(CONTRACT_PATH),
        "csv_sha256": DATA_HASH_PATH.read_text().strip()
        if DATA_HASH_PATH.exists()
        else "desconocido",
        "config_yaml_sha256": sha256_of(CONFIG_PATH),
    }


# --- modelos ---------------------------------------------------------------------------------------


def make_logreg(cfg: dict, C: float) -> Pipeline:
    params = cfg["models"]["logreg"]
    return Pipeline(
        [
            ("scaler", StandardScaler()),
            (
                "clf",
                LogisticRegression(
                    C=C,
                    class_weight="balanced",
                    max_iter=params["max_iter"],
                    random_state=cfg["seed"],
                ),
            ),
        ]
    )


def make_xgb(
    cfg: dict, max_depth: int, scale_pos_weight: float, n_estimators=None, early_stopping=True
):
    params = cfg["models"]["xgb"]
    return XGBClassifier(
        tree_method="hist",
        eval_metric="aucpr",
        n_estimators=n_estimators or params["n_estimators"],
        learning_rate=params["learning_rate"],
        max_depth=max_depth,
        scale_pos_weight=scale_pos_weight,
        early_stopping_rounds=params["early_stopping_rounds"] if early_stopping else None,
        random_state=cfg["seed"],
        n_jobs=params["n_jobs"],
    )


def positive_proba(model, X: np.ndarray) -> np.ndarray:
    return model.predict_proba(X)[:, 1].astype(np.float64)


# --- evaluación en valid ---------------------------------------------------------------------------


def fpr_key(target: float) -> str:
    return f"fpr_{target:g}"


def evaluate_partition(probabilities: np.ndarray, frame: pd.DataFrame, cfg: dict) -> dict:
    """Métricas de la sección 2 y umbrales de la sección 4, todos fijados en esta partición."""
    y = frame["is_fraud"].to_numpy()
    amount = frame["amount"].to_numpy(dtype=np.float64)
    scores = metrics.round_scores(probabilities, cfg["rounding_decimals"])
    out: dict = {"pr_auc": metrics.pr_auc(y, probabilities), "operating_points": {}}
    for target in cfg["fpr_targets"]:
        threshold = metrics.fpr_threshold(scores, y, target, cfg["rounding_decimals"])
        out["operating_points"][fpr_key(target)] = metrics.operating_point(
            scores, y, amount, threshold
        )
    t_block = metrics.choose_t_block(scores, y, cfg["block_fpr_max"], cfg["rounding_decimals"])
    t_review = metrics.choose_t_review(
        scores, y, amount, t_block, cfg["costs"], cfg["review_rate_max"]
    )
    out["thresholds"] = {"t_review": float(t_review), "t_block": float(t_block)}
    out["decision"] = metrics.decision_summary(scores, y, amount, t_review, t_block, cfg["costs"])
    out["cost_sensitivity"] = metrics.cost_sensitivity(
        scores, y, amount, t_review, t_block, cfg["costs"]
    )
    out["no_model_cost"] = metrics.no_model_cost(y, amount)
    return out


def flat_metrics(result: dict) -> dict[str, float]:
    """Métricas planas para MLflow."""
    flat = {"pr_auc": result["pr_auc"], "cost": result["decision"]["cost"]}
    for name, point in result["operating_points"].items():
        flat[f"recall_{name}"] = point["recall"]
        flat[f"fpr_actual_{name}"] = point["fpr"]
        flat[f"recall_weighted_{name}"] = point["recall_weighted"]
        flat[f"threshold_{name}"] = point["threshold"]
    flat["t_review"] = result["thresholds"]["t_review"]
    flat["t_block"] = result["thresholds"]["t_block"]
    for key in ("review_rate", "block_rate", "block_fpr", "block_recall", "review_or_block_recall"):
        flat[key] = result["decision"][key]
    for name, value in result["cost_sensitivity"].items():
        flat["cost_sens_" + name.replace(" ", "").replace(",", "_")] = value
    flat["no_model_cost"] = result["no_model_cost"]
    return flat


def frozen_evaluation(probabilities, frame, cfg, thresholds: dict) -> dict:
    """Mismas métricas con umbrales ya fijados (en test): no se reestima ninguno."""
    y = frame["is_fraud"].to_numpy()
    amount = frame["amount"].to_numpy(dtype=np.float64)
    scores = metrics.round_scores(probabilities, cfg["rounding_decimals"])
    out: dict = {"pr_auc": metrics.pr_auc(y, probabilities), "operating_points": {}}
    for name, threshold in thresholds["operating_points"].items():
        out["operating_points"][name] = metrics.operating_point(scores, y, amount, threshold)
    t_review, t_block = thresholds["t_review"], thresholds["t_block"]
    out["thresholds"] = {"t_review": t_review, "t_block": t_block}
    out["decision"] = metrics.decision_summary(scores, y, amount, t_review, t_block, cfg["costs"])
    out["cost_sensitivity"] = metrics.cost_sensitivity(
        scores, y, amount, t_review, t_block, cfg["costs"]
    )
    out["no_model_cost"] = metrics.no_model_cost(y, amount)
    out["n_rows"] = len(y)
    out["n_fraud"] = int(y.sum())
    return out


def feature_order(feature_set: str) -> list[str]:
    return feature_columns(feature_set, load_contract())
