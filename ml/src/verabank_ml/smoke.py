"""``smoke`` (CI, D11): humo de extremo a extremo del pipeline sobre una muestra SINTÉTICA con el esquema de PaySim.

No descarga nada ni toca ``ml/artifacts`` ni MLflow: genera ``--rows`` filas (50.000 en la CI) con la forma de PaySim,
escribe el CSV, calcula las features A con el mismo código que ``make ml-features`` (``features.read_raw`` y
``build_table``), entrena los dos modelos con una rejilla reducida (pesos de clase, sin remuestreo), fija ``t_review``
y ``t_block`` solo en validación (``modeling.evaluate_partition``), exporta ambos a ONNX con el mismo conversor que
``ml-export`` (``export.to_onnx_bytes``) y comprueba la paridad ONNX frente a ``predict_proba`` (< 1e-5). Todo en
``--out`` (un directorio temporal si no se da). La muestra prueba el pipeline, no las métricas del modelo.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path

import numpy as np
import pandas as pd

from verabank_ml import modeling
from verabank_ml.features import build_table, load_config, read_raw

COLUMNS = ["step", "type", "amount", "nameOrig", "oldbalanceOrg", "newbalanceOrig", "nameDest", "oldbalanceDest",
           "newbalanceDest", "isFraud", "isFlaggedFraud"]
SPLIT = {"train": [1, 60], "valid": [69, 96], "test": [105, 120], "gap_steps": 8}
TYPES = ["TRANSFER", "CASH_OUT", "PAYMENT", "DEBIT", "CASH_IN"]


def synthetic_paysim(rows: int, seed: int) -> pd.DataFrame:
    """Filas con el esquema y las columnas de PaySim, ordenadas por step. El fraude (≈ 1,5 %) es TRANSFER o CASH_OUT,
    de monto alto y a destinos nuevos, para que los modelos tengan señal."""
    rng = np.random.default_rng(seed)
    steps = np.sort(rng.integers(1, SPLIT["test"][1] + 1, rows))
    kind = rng.choice(TYPES, rows, p=[0.12, 0.33, 0.33, 0.02, 0.20])
    fraud = np.isin(kind, ["TRANSFER", "CASH_OUT"]) & (rng.random(rows) < 0.033)
    amount = np.round(np.where(fraud, rng.lognormal(10.5, 1.0, rows), rng.lognormal(8.5, 1.2, rows)), 2)
    orig = np.where(fraud, [f"C9{i:08d}" for i in rng.integers(0, 10**8, rows)],
                    [f"C1{i:05d}" for i in rng.integers(0, 3000, rows)])
    dest = np.where(fraud, [f"C8{i:08d}" for i in rng.integers(0, 10**8, rows)],
                    [f"M{i:05d}" for i in rng.integers(0, 1500, rows)])
    balance = np.round(rng.lognormal(9, 2, rows), 2)
    return pd.DataFrame({"step": steps, "type": kind, "amount": amount, "nameOrig": orig, "oldbalanceOrg": balance,
                         "newbalanceOrig": np.maximum(balance - amount, 0), "nameDest": dest, "oldbalanceDest": 0.0,
                         "newbalanceDest": 0.0, "isFraud": fraud.astype(int), "isFlaggedFraud": 0})[COLUMNS]


def reduced_config(cfg: dict) -> dict:
    out = json.loads(json.dumps(cfg))
    out["split"] = SPLIT
    out["models"]["logreg"]["C"] = [1.0]
    out["models"]["xgb"].update(max_depth=[3], scale_pos_weight=[30], n_estimators=60, n_jobs=2)
    return out


def run(rows: int, out: Path, seed: int = 42) -> dict:
    from verabank_ml.export import to_onnx_bytes
    from verabank_ml.latency import onnx_parity

    started = time.perf_counter()
    cfg = reduced_config(load_config())
    csv = out / "paysim_synthetic.csv"
    synthetic_paysim(rows, seed).to_csv(csv, index=False)
    table = build_table(read_raw(csv, "A"), "A", cfg).frame
    table.to_parquet(out / "features_A.parquet", index=False)
    cols = modeling.feature_order("A")
    train, valid = table[table["partition"] == "train"], table[table["partition"] == "valid"]
    Xtr, ytr = modeling.matrix(train, cols), train["is_fraud"].to_numpy()
    Xva, yva = modeling.matrix(valid, cols), valid["is_fraud"].to_numpy()
    models = {"logreg": modeling.make_logreg(cfg, 1.0).fit(Xtr, ytr)}
    xgb = modeling.make_xgb(cfg, 3, 30)
    xgb.fit(Xtr, ytr, eval_set=[(Xva, yva)], verbose=False)
    models["xgb"] = xgb
    result: dict = {"rows": rows, "partitions": table["partition"].value_counts().to_dict(),
                    "frauds": {"train": int(ytr.sum()), "valid": int(yva.sum())}, "models": {}}
    for family, model in models.items():
        proba = modeling.positive_proba(model, Xva)
        evaluation = modeling.evaluate_partition(proba, valid, cfg)
        payload = to_onnx_bytes(family, model, len(cols))
        (out / f"{family}.onnx").write_bytes(payload)
        parity = onnx_parity(payload, Xva[:2000], proba[:2000])
        t = evaluation["thresholds"]
        if not 0 <= t["t_review"] <= t["t_block"] <= 1:
            raise RuntimeError(f"{family}: umbrales fuera de orden {t}")
        if parity >= 1e-5:
            raise RuntimeError(f"{family}: paridad ONNX {parity:.3e} >= 1e-5")
        result["models"][family] = {"pr_auc": round(evaluation["pr_auc"], 4), **t, "onnx_parity": parity}
    if min(result["frauds"].values()) < 30:
        raise RuntimeError(f"muestra sin fraudes suficientes: {result['frauds']}")
    result["seconds"] = round(time.perf_counter() - started, 1)
    (out / "smoke.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def smoke_command(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m verabank_ml smoke", description=__doc__.split("\n")[0])
    parser.add_argument("--rows", type=int, default=50_000)
    parser.add_argument("--out", type=Path, default=None, help="directorio de salida (temporal si no se da)")
    args = parser.parse_args(argv)
    if args.out is None:
        with tempfile.TemporaryDirectory(prefix="verabank-ml-smoke-") as tmp:
            result = run(args.rows, Path(tmp))
    else:
        args.out.mkdir(parents=True, exist_ok=True)
        result = run(args.rows, args.out)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(smoke_command(sys.argv[1:]))
