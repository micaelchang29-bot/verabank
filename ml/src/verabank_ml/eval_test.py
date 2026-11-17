"""ml-eval-test: evaluación única en `test` del campeón con umbrales congelados.

La partición test se lee solo aquí. Una segunda ejecución se niega salvo con --force-test, que deja la
etiqueta `test_reevaluated=true` en MLflow.
"""

import argparse
import itertools
import json
import sys
from pathlib import Path

import mlflow
import mlflow.sklearn
import mlflow.xgboost
import numpy as np
import pandas as pd

from verabank_ml import metrics, modeling, tracking
from verabank_ml.bootstrap import bootstrap_metrics
from verabank_ml.features import REPO_ROOT, load_config

REPORTS = REPO_ROOT / "ml" / "reports"
THRESHOLDS_PATH = REPO_ROOT / "ml" / "artifacts" / "thresholds.json"
TEST_REPORT_PATH = REPORTS / "test_evaluation.json"
AMOUNT_BANDS = [0.0, 1e3, 1e4, 1e5, 1e6, np.inf]  # tramos fijos de monto (décadas)
TEST_STAGE_TAG = "test-eval"


class TestAlreadyEvaluated(RuntimeError):
    """`test` ya se evaluó: repetirlo exige --force-test."""

    __test__ = False  # no es una clase de pruebas para pytest


def check_test_guard(report_path: Path, tracked_runs: int, force: bool) -> None:
    """Se niega si ya existe el informe de test o una ejecución de test en MLflow, salvo con force."""
    if (Path(report_path).exists() or tracked_runs > 0) and not force:
        raise TestAlreadyEvaluated(
            "test ya se evaluó (informe o ejecución de MLflow existentes). La partición test se toca una "
            "sola vez; para repetirla de forma explícita usa --force-test."
        )


def group_rates(
    frame: pd.DataFrame, scores: np.ndarray, t_review: float, t_block: float, keys
) -> list[dict]:
    """FPR de bloqueo y de revisión-o-bloqueo, y recall, por grupo (`keys` es una serie alineada con frame)."""
    decisions = metrics.decide(scores, t_review, t_block)
    y = frame["is_fraud"].to_numpy()
    rows = []
    for group in pd.unique(keys):
        mask = np.asarray(keys == group)
        legit, fraud = mask & (y == 0), mask & (y == 1)
        n_legit, n_fraud = int(legit.sum()), int(fraud.sum())
        blocked = int((legit & (decisions == metrics.BLOCK)).sum())
        flagged = int((legit & (decisions != metrics.APPROVE)).sum())
        rows.append(
            {
                "group": str(group),
                "n_legit": n_legit,
                "n_fraud": n_fraud,
                "fpr_block": blocked / n_legit if n_legit else None,
                "fpr_review_or_block": flagged / n_legit if n_legit else None,
                "recall_review_or_block": float(
                    (fraud & (decisions != metrics.APPROVE)).sum() / n_fraud
                )
                if n_fraud
                else None,
            }
        )
    return sorted(rows, key=lambda r: r["group"])


def amount_band(amount: pd.Series) -> pd.Series:
    labels = [f"[{lo:g}, {hi:g})" for lo, hi in itertools.pairwise(AMOUNT_BANDS)]
    return pd.cut(amount, bins=AMOUNT_BANDS, labels=labels, right=False).astype(str)


def evaluate_test(
    probabilities, frame: pd.DataFrame, train_destinations: set, thresholds: dict, cfg: dict
) -> dict:
    """Todas las métricas de test con umbrales congelados (`thresholds` es el contenido de thresholds.json)."""
    scores = metrics.round_scores(probabilities, cfg["rounding_decimals"])
    t_review, t_block = thresholds["t_review"], thresholds["t_block"]
    overall = modeling.frozen_evaluation(probabilities, frame, cfg, thresholds)
    unseen_mask = ~frame["dst_id"].isin(train_destinations).to_numpy()
    unseen = modeling.frozen_evaluation(
        probabilities[unseen_mask], frame[unseen_mask], cfg, thresholds
    )
    unseen["share_of_rows"] = float(unseen_mask.mean())
    no_model = overall["no_model_cost"]
    ops = {k: v for k, v in thresholds["operating_points"].items()}
    intervals = {}
    for block_steps in (
        cfg["bootstrap"]["block_steps"],
        cfg["bootstrap"]["sensitivity_block_steps"],
    ):
        intervals[str(block_steps)] = bootstrap_metrics(
            probabilities, scores, frame["is_fraud"].to_numpy(), frame["amount"].to_numpy(),
            frame["step"].to_numpy(), cfg["split"]["test"][0], block_steps, ops, t_review, t_block,
            cfg["costs"], cfg["bootstrap"]["n_resamples"], cfg["seed"], cfg["bootstrap"]["confidence"],
        )  # fmt: skip
    return {
        "frozen_thresholds": {"t_review": t_review, "t_block": t_block, "operating_points": ops},
        "overall": overall,
        "unseen_destinations": unseen,
        "intervals": intervals,
        "fpr_by_type": group_rates(frame, scores, t_review, t_block, frame["type"]),
        "fpr_by_amount_band": group_rates(
            frame, scores, t_review, t_block, amount_band(frame["amount"])
        ),
        "vs_no_model": {
            "no_model_cost": no_model,
            "model_cost": overall["decision"]["cost"],
            "cost_reduction": 1 - overall["decision"]["cost"] / no_model if no_model else None,
            "no_model_recall": 0.0,
            "model_review_or_block_recall": overall["decision"]["review_or_block_recall"],
        },
    }


def load_champion(model_version: str):
    name = model_version.split("/")[0]
    uri = f"models:/{name}@champion"
    return (mlflow.xgboost if name.endswith("xgb") else mlflow.sklearn).load_model(uri)


def eval_test_command(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m verabank_ml eval-test")
    parser.add_argument("--force-test", action="store_true", help="repite la evaluación sobre test")
    args = parser.parse_args(argv)

    cfg = load_config()
    if not THRESHOLDS_PATH.exists():
        print(
            "falta ml/artifacts/thresholds.json: la selección está pendiente o falta make ml-train",
            file=sys.stderr,
        )
        return 2
    client = tracking.setup(cfg)
    experiment = client.get_experiment_by_name(cfg["mlflow"]["experiment"])
    previous = mlflow.search_runs(
        experiment_ids=[experiment.experiment_id], filter_string=f"tags.stage = '{TEST_STAGE_TAG}'"
    )
    try:
        check_test_guard(TEST_REPORT_PATH, len(previous), args.force_test)
    except TestAlreadyEvaluated as error:
        print(f"ml-eval-test: {error}", file=sys.stderr)
        return 3

    thresholds = json.loads(THRESHOLDS_PATH.read_text())
    cols = thresholds["feature_order"]
    if cols != modeling.feature_order(thresholds["feature_set"]):
        print(
            "el orden de features de thresholds.json no coincide con features.yaml", file=sys.stderr
        )
        return 2
    model = load_champion(thresholds["model_version"])
    test = modeling.load_features(thresholds["feature_set"], ("test",), allow_test=True)
    train_dst = set(
        modeling.load_features(
            thresholds["feature_set"], ("train",), columns=["dst_id", "partition"]
        )["dst_id"]
    )
    probabilities = modeling.positive_proba(model, modeling.matrix(test, cols))
    result = evaluate_test(probabilities, test, train_dst, thresholds, cfg)
    result["model_version"] = thresholds["model_version"]
    result["test_reevaluated"] = bool(
        args.force_test and (len(previous) > 0 or TEST_REPORT_PATH.exists())
    )
    result["provenance"] = modeling.provenance()
    tracking.dump_json(TEST_REPORT_PATH, result)

    with mlflow.start_run(run_name="eval-test"):
        mlflow.set_tags({"stage": TEST_STAGE_TAG, "model_version": thresholds["model_version"]})
        if result["test_reevaluated"]:
            mlflow.set_tag("test_reevaluated", "true")
        mlflow.log_metrics(
            modeling.flat_metrics(
                {**result["overall"], "thresholds": result["overall"]["thresholds"]}
            )
        )
        mlflow.log_artifact(str(TEST_REPORT_PATH))
    overall = result["overall"]
    print(f"test: PR-AUC {overall['pr_auc']:.4f}, costo {overall['decision']['cost']:,.0f} "
          f"(sin modelo {overall['no_model_cost']:,.0f}); escrito {TEST_REPORT_PATH}")  # fmt: skip
    return 0
