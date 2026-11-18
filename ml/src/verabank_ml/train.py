"""ml-train: modelos del conjunto A, métricas, umbrales, selección y alias. Solo usa train y valid."""

import argparse
import sys
import time

import mlflow
import mlflow.sklearn
import mlflow.xgboost
import numpy as np

from verabank_ml import modeling, tracking
from verabank_ml.bootstrap import bootstrap_metrics
from verabank_ml.features import CONFIG_PATH, REPO_ROOT, load_config
from verabank_ml.latency import measure_latency, onnx_parity
from verabank_ml.onnx_export import sklearn_pipeline_to_onnx, xgboost_to_onnx
from verabank_ml.selection import select_model

REPORTS = REPO_ROOT / "ml" / "reports"
ARTIFACTS = REPO_ROOT / "ml" / "artifacts"
SELECTION_PATH = REPORTS / "selection.json"
VALID_METRICS_PATH = REPORTS / "valid_metrics.json"
THRESHOLDS_PATH = ARTIFACTS / "thresholds.json"
FAMILIES = ("logreg", "xgb")
PARITY_ROWS = 5000


def intervals_for(result: dict, probabilities, valid, cfg: dict) -> dict:
    """Intervalos por bootstrap de bloques con el tamaño principal y el de sensibilidad."""
    scores = np.round(probabilities, cfg["rounding_decimals"])
    ops = {name: p["threshold"] for name, p in result["operating_points"].items()}
    out = {}
    for block_steps in (
        cfg["bootstrap"]["block_steps"],
        cfg["bootstrap"]["sensitivity_block_steps"],
    ):
        out[str(block_steps)] = bootstrap_metrics(
            probabilities, scores, valid["is_fraud"].to_numpy(), valid["amount"].to_numpy(),
            valid["step"].to_numpy(), cfg["split"]["valid"][0], block_steps, ops,
            result["thresholds"]["t_review"], result["thresholds"]["t_block"], cfg["costs"],
            cfg["bootstrap"]["n_resamples"], cfg["seed"], cfg["bootstrap"]["confidence"],
        )  # fmt: skip
    return out


def fmt(x, digits=4):
    return f"{x:.{digits}f}"


def print_table(rows: list[dict]) -> None:
    print(
        f"{'configuración':<28} {'PR-AUC':>7} {'R@0.5%':>7} {'R@0.1%':>7} {'costo':>14} {'t_block':>8} {'t_review':>8}"
    )
    for r in rows:
        op = r["operating_points"]
        print(
            f"{r['label']:<28} {fmt(r['pr_auc']):>7} {fmt(op['fpr_0.005']['recall']):>7} "
            f"{fmt(op['fpr_0.001']['recall']):>7} {r['decision']['cost']:>14,.0f} "
            f"{fmt(r['thresholds']['t_block'], 5):>8} {fmt(r['thresholds']['t_review'], 5):>8}"
        )


def train_command(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m verabank_ml train")
    parser.add_argument(
        "--winner",
        choices=FAMILIES,
        help="override manual de la selección (queda registrado como manual)",
    )
    args = parser.parse_args(argv)

    started = time.perf_counter()
    cfg = load_config()
    prov = modeling.provenance()
    cols = modeling.feature_order("A")
    train = modeling.load_features("A", ("train",))
    valid = modeling.load_features("A", ("valid",))
    Xtr, ytr = modeling.matrix(train, cols), train["is_fraud"].to_numpy()
    Xva, yva = modeling.matrix(valid, cols), valid["is_fraud"].to_numpy()
    print(f"train {len(ytr):,} filas ({ytr.sum():,} fraudes); valid {len(yva):,} ({yva.sum():,})")

    client = tracking.setup(cfg)
    best: dict[str, dict] = {}
    all_rows: dict[str, list] = {f: [] for f in FAMILIES}

    def child(family: str, label: str, params: dict, fit):
        with mlflow.start_run(run_name=label, nested=True) as run:
            mlflow.set_tags({"family": family, "feature_set": "A", **prov})
            mlflow.log_params(params)
            mlflow.log_artifact(str(CONFIG_PATH))
            fit_started = time.perf_counter()
            model, extra = fit()
            probabilities = modeling.positive_proba(model, Xva)
            result = modeling.evaluate_partition(probabilities, valid, cfg)
            mlflow.log_metrics(modeling.flat_metrics(result))
            mlflow.log_metric("fit_seconds", time.perf_counter() - fit_started)
            if extra:
                mlflow.log_params(extra)
            result.update(label=label, params={**params, **extra}, run_id=run.info.run_id)
        all_rows[family].append(result)
        if family not in best or result["pr_auc"] > best[family]["result"]["pr_auc"]:
            best[family] = {"result": result, "model": model, "probabilities": probabilities}

    with mlflow.start_run(run_name="ml-train"):
        mlflow.set_tags({"stage": "train-valid", "feature_set": "A", **prov})
        for C in cfg["models"]["logreg"]["C"]:

            def fit_lr(C=C):
                return make_fit_logreg(cfg, C, Xtr, ytr), {}

            child("logreg", f"logreg C={C:g}", {"model": "logreg", "C": C}, fit_lr)
            print(
                f"  {all_rows['logreg'][-1]['label']}: PR-AUC {all_rows['logreg'][-1]['pr_auc']:.4f}",
                flush=True,
            )
        for depth in cfg["models"]["xgb"]["max_depth"]:
            for spw in cfg["models"]["xgb"]["scale_pos_weight"]:

                def fit_xgb(depth=depth, spw=spw):
                    model = modeling.make_xgb(cfg, depth, spw)
                    model.fit(Xtr, ytr, eval_set=[(Xva, yva)], verbose=False)
                    return model, {"best_iteration": int(model.best_iteration)}

                child("xgb", f"xgb depth={depth} spw={spw}",
                      {"model": "xgb", "max_depth": depth, "scale_pos_weight": spw}, fit_xgb)  # fmt: skip
                last = all_rows["xgb"][-1]
                print(
                    f"  {last['label']}: PR-AUC {last['pr_auc']:.4f} (árboles {last['params']['best_iteration']})",
                    flush=True,
                )

        # Mejor de cada familia: modelo final, intervalos, latencia, paridad y registro.
        finals, intervals, latencies, parities, versions = {}, {}, {}, {}, {}
        for family in FAMILIES:
            entry = best[family]
            params = entry["result"]["params"]
            if family == "xgb":
                final = modeling.make_xgb(cfg, params["max_depth"], params["scale_pos_weight"],
                                          n_estimators=params["best_iteration"] + 1, early_stopping=False)  # fmt: skip
                final.fit(Xtr, ytr, verbose=False)
                onnx_model = xgboost_to_onnx(final, len(cols))
            else:
                final = entry["model"]
                onnx_model = sklearn_pipeline_to_onnx(final, len(cols))
            probabilities = modeling.positive_proba(final, Xva)
            drift = float(np.abs(probabilities - entry["probabilities"]).max())
            result = modeling.evaluate_partition(probabilities, valid, cfg)
            result.update(
                label=entry["result"]["label"], params=params, run_id=entry["result"]["run_id"]
            )
            finals[family] = (final, result, probabilities)
            intervals[family] = intervals_for(result, probabilities, valid, cfg)
            payload = onnx_model.SerializeToString()
            latencies[family] = measure_latency(
                payload, Xva, cfg["latency"]["calls"], cfg["latency"]["warmup"]
            )
            parities[family] = {
                "max_abs_diff_onnx_vs_predict_proba": onnx_parity(payload, Xva[:PARITY_ROWS], probabilities[:PARITY_ROWS]),
                "refit_vs_early_stopped_max_abs_diff": drift,
            }  # fmt: skip
            name = cfg["mlflow"]["registered_models"][family]
            with mlflow.start_run(run_id=result["run_id"], nested=True):
                logger = mlflow.xgboost if family == "xgb" else mlflow.sklearn
                kwargs = {"xgb_model": final} if family == "xgb" else {"sk_model": final}
                info = logger.log_model(name="model", registered_model_name=name, **kwargs)
                versions[family] = {
                    "name": name,
                    "version": str(info.registered_model_version),
                    "run_id": result["run_id"],
                }
                mlflow.log_dict(intervals[family], "bootstrap_intervals.json")
                mlflow.log_dict(latencies[family], "latency.json")
            print(f"mejor {family}: {result['label']} -> {name} v{versions[family]['version']}")

        selection = select_model(
            finals["logreg"][1], finals["xgb"][1], latencies["xgb"]["p95_ms"], intervals, cfg
        )
        selection.update(
            manual_decision=None, models=versions, best_configs={f: finals[f][1]["params"] for f in FAMILIES},
            intervals=intervals, latency=latencies, parity=parities, provenance=prov,
            valid={f: {k: v for k, v in finals[f][1].items() if k != "run_id"} for f in FAMILIES},
        )  # fmt: skip
        if args.winner:
            selection["manual_decision"] = {"winner": args.winner, "reason": "decisión del usuario"}
            selection["winner"] = args.winner
        tracking.dump_json(SELECTION_PATH, selection)
        tracking.dump_json(
            VALID_METRICS_PATH,
            {
                "configs": {
                    f: [{k: v for k, v in r.items()} for r in all_rows[f]] for f in FAMILIES
                },
                "provenance": prov,
            },
        )
        mlflow.log_artifact(str(SELECTION_PATH))

        report_selection(selection, finals, intervals, cfg)
        write_thresholds(selection["winner"], selection, finals, cols, cfg, client)
    print(f"\nml-train terminado en {time.perf_counter() - started:.0f} s")
    return 0


def make_fit_logreg(cfg, C, Xtr, ytr):
    model = modeling.make_logreg(cfg, C)
    model.fit(Xtr, ytr)
    return model


def report_selection(selection, finals, intervals, cfg) -> None:
    print("\n== mejores configuraciones en valid")
    print_table([finals[f][1] for f in FAMILIES])
    print("\n== intervalos de PR-AUC (bootstrap de bloques)")
    for family in FAMILIES:
        for block, info in intervals[family].items():
            m = info["metrics"]["pr_auc"]
            print(
                f"  {family:<7} bloques de {block:>2} pasos ({info['n_blocks']:>2} bloques): [{m['low']:.4f}, {m['high']:.4f}]"
            )
    print("\n== selección")
    for c in selection["criteria"]:
        print(f"  {c['result']:<14} {c['name']}: {c['value']}")
    for warning in selection["warnings"]:
        print(f"  ADVERTENCIA: {warning}")
    print(f"  -> {selection['status']}: ganador {selection['winner']} ({selection['reason']})")


def write_thresholds(winner, selection, finals, cols, cfg, client) -> None:
    result = finals[winner][1]
    other = "logreg" if winner == "xgb" else "xgb"
    info = selection["models"][winner]
    for family, alias in ((winner, "champion"), (other, "challenger")):
        entry = selection["models"][family]
        client.set_registered_model_alias(entry["name"], alias, entry["version"])
    payload = {
        "model_version": f"{info['name']}/{info['version']}",
        "model_family": winner,
        "feature_set": "A",
        "feature_order": cols,
        "t_review": result["thresholds"]["t_review"],
        "t_block": result["thresholds"]["t_block"],
        "rounding_decimals": cfg["rounding_decimals"],
        "decision_rule": "score = round(p, 5); BLOCK si score >= t_block; si no, REVIEW si score >= t_review; si no, APPROVE",
        "operating_points": {n: p["threshold"] for n, p in result["operating_points"].items()},
        "validation_metrics": {
            "pr_auc": result["pr_auc"],
            "operating_points": result["operating_points"],
            "decision": result["decision"],
            "no_model_cost": result["no_model_cost"],
            "n_blocks_valid_24": selection["intervals"][winner][str(cfg["bootstrap"]["block_steps"])]["n_blocks"],
        },
        "selection": {"status": selection["status"], "manual_decision": selection["manual_decision"]},
        "provenance": selection["provenance"],
    }  # fmt: skip
    tracking.dump_json(THRESHOLDS_PATH, payload)
    client.log_artifact(info["run_id"], str(THRESHOLDS_PATH))   # D8: ml-export lee los umbrales del run del champion
    print(f"alias champion -> {info['name']} v{info['version']}; thresholds.json escrito y adjunto al run")


if __name__ == "__main__":
    sys.exit(train_command(sys.argv[1:]))
