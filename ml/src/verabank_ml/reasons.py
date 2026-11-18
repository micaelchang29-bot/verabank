"""``reasons`` (make ml-reasons): tres razones por caso ``REVIEW`` o ``BLOCK`` del modelo (§5e, D6).

ONNX no devuelve SHAP; este job por lotes lo calcula desde ``risk.scoring_log.features`` (rol ``ml_job``) y escribe
``analytics.fraud_reasons``:

- Solo casos del modelo: ``decision`` ``REVIEW`` o ``BLOCK`` y ``model_version`` distinta de ``warmup``. El fallback no
  tiene score ni fila en ``scoring_log``. Se explican con el modelo del alias ``champion`` de MLflow, así que solo los
  casos que ese mismo modelo decidió (``model_version`` igual); los demás se cuentan como omitidos.
- XGBoost: SHAP (``shap.TreeExplainer``, en log-odds). Logística: coeficiente × valor estandarizado (el
  ``StandardScaler`` del pipeline).
- Las tres de mayor contribución **absoluta** (desempate por nombre), con su signo en ``contribution``.
- Idempotente: no recalcula los casos que ya tienen razones con la misma ``model_version``.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter

import numpy as np
from sklearn.pipeline import Pipeline

from verabank_ml.features import REPO_ROOT

THRESHOLDS_PATH = REPO_ROOT / "ml" / "artifacts" / "thresholds.json"
N_REASONS = 3
BATCH = 5000


def explain(model, X: np.ndarray) -> np.ndarray:
    """Contribución de cada feature a cada fila (n × k), común a los dos tipos de modelo. Sin base de datos."""
    X = np.asarray(X, dtype=np.float32)
    if isinstance(model, Pipeline):
        scaler, lr = model[0], model[-1]
        z = (X.astype(np.float64) - scaler.mean_) / scaler.scale_
        return z * lr.coef_.ravel()
    import shap

    values = shap.TreeExplainer(model).shap_values(X)
    return np.asarray(values[1] if isinstance(values, list) else values, dtype=np.float64)


def expected_value(model) -> float:
    """Valor base del explicador (margen medio); con la logística, el margen en la media de entrenamiento."""
    if isinstance(model, Pipeline):
        return float(model[-1].intercept_[0])
    # El sesgo de TreeSHAP según XGBoost (columna final de pred_contribs; es igual en toda fila). No se usa
    # shap.TreeExplainer(model).expected_value: con XGBoost 3 ignora base_score hasta la primera llamada a shap_values.
    import xgboost as xgb

    n = model.get_booster().num_features()
    return float(model.get_booster().predict(xgb.DMatrix(np.zeros((1, n), np.float32)), pred_contribs=True)[0, -1])


def top_reasons(contributions: np.ndarray, names: list[str], n: int = N_REASONS) -> list[list[tuple[int, str, float]]]:
    """Por fila: (rank, feature, contribución con signo) de las ``n`` de mayor valor absoluto; desempate por nombre."""
    out = []
    for row in contributions:
        order = sorted(range(len(names)), key=lambda j: (-abs(row[j]), names[j]))[:n]
        out.append([(rank, names[j], float(row[j])) for rank, j in enumerate(order, start=1)])
    return out


# --- base de datos ----------------------------------------------------------------------------------------------

PENDING = """
SELECT s.transfer_id, s.features, s.decision FROM risk.scoring_log s
WHERE s.model_version <> 'warmup' AND s.source_ref NOT LIKE 'perf-%%' AND s.decision IN ('REVIEW', 'BLOCK')
  AND s.model_version = %(mv)s
  AND NOT EXISTS (SELECT 1 FROM analytics.fraud_reasons r WHERE r.transfer_id = s.transfer_id AND r.model_version = %(mv)s)
ORDER BY s.occurred_at, s.transfer_id
LIMIT %(limit)s
"""
OTHER_MODEL = """
SELECT count(*) FROM risk.scoring_log WHERE model_version NOT IN ('warmup', %(mv)s) AND source_ref NOT LIKE 'perf-%%'
  AND decision IN ('REVIEW', 'BLOCK')
"""
UPSERT = """
INSERT INTO analytics.fraud_reasons (transfer_id, rank, feature, contribution, model_version, computed_at)
VALUES (%s, %s, %s, %s, %s, now())
ON CONFLICT (transfer_id, rank) DO UPDATE SET feature = EXCLUDED.feature, contribution = EXCLUDED.contribution,
  model_version = EXCLUDED.model_version, computed_at = now()
"""


def run(conn, model, model_version: str, names: list[str], limit: int | None = None) -> dict:
    done: Counter[str] = Counter()
    remaining = limit
    while remaining is None or remaining > 0:
        size = BATCH if remaining is None else min(BATCH, remaining)
        rows = conn.execute(PENDING, {"mv": model_version, "limit": size}).fetchall()
        if not rows:
            break
        X = np.array([[float(r[1][k]) for k in names] for r in rows], dtype=np.float32)
        reasons = top_reasons(explain(model, X), names)
        params = [(r[0], rank, feature, value, model_version)
                  for r, rr in zip(rows, reasons, strict=True) for rank, feature, value in rr]
        with conn.transaction(), conn.cursor() as cur:
            cur.executemany(UPSERT, params)
        done.update(r[2] for r in rows)
        if remaining is not None:
            remaining -= len(rows)
    skipped = conn.execute(OTHER_MODEL, {"mv": model_version}).fetchone()[0]
    return {"explained": dict(sorted(done.items())), "other_model_skipped": int(skipped)}


def reasons_command(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m verabank_ml reasons", description=__doc__.split("\n")[0])
    parser.add_argument("--limit", type=int, default=None, help="como máximo N casos en esta corrida")
    args = parser.parse_args(argv)
    import psycopg

    from verabank_ml import tracking
    from verabank_ml.export import load_alias
    from verabank_ml.features import load_config
    from verabank_ml.monitor.psi import dsn_from_env

    cfg = load_config()
    thresholds = json.loads(THRESHOLDS_PATH.read_text())
    family, name, version, model = load_alias(tracking.setup(cfg), cfg, "champion")
    model_version = f"{name}/{version}"
    with psycopg.connect(dsn_from_env(), autocommit=True) as conn:
        result = run(conn, model, model_version, thresholds["feature_order"], args.limit)
    method = "SHAP (TreeExplainer)" if family == "xgb" else "coeficiente × valor estandarizado"
    print(f"razones con {model_version} ({method}): casos explicados {result['explained'] or 'ninguno'}; "
          f"de otro modelo, sin explicar: {result['other_model_skipped']}")
    return 0
