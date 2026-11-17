"""MLflow local (SQLite) del experimento `verabank-fraud`."""

import json
from pathlib import Path

import mlflow
from mlflow import MlflowClient

from verabank_ml.features import REPO_ROOT


def setup(cfg: dict) -> MlflowClient:
    db = (REPO_ROOT / cfg["mlflow"]["tracking_db"]).resolve()
    mlflow.set_tracking_uri(f"sqlite:///{db}")
    client = MlflowClient()
    name = cfg["mlflow"]["experiment"]
    if client.get_experiment_by_name(name) is None:
        client.create_experiment(
            name, artifact_location=(REPO_ROOT / "ml" / "mlruns").resolve().as_uri()
        )
    mlflow.set_experiment(name)
    return client


def dump_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    def default(value):
        if hasattr(value, "item"):
            return value.item()
        raise TypeError(f"no serializable: {type(value)}")

    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=default) + "\n")
