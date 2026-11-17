"""ml-report: ablación B (solo valid) e informe del modelo. No lee la partición test."""

import json
import sys

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import mlflow
import mlflow.sklearn
import mlflow.xgboost
import numpy as np
import shap

from verabank_ml import modeling, tracking
from verabank_ml.eval_test import TEST_REPORT_PATH, THRESHOLDS_PATH
from verabank_ml.features import REPO_ROOT, load_config
from verabank_ml.train import SELECTION_PATH

REPORTS = REPO_ROOT / "ml" / "reports"
FIGURES = REPORTS / "figures"
ABLATION_PATH = REPORTS / "ablation_b.json"
REPORT_PATH = REPORTS / "model-report.md"
SHAP_ROWS = 20_000
NEAR_ONE = 0.95  # PR-AUC por encima de esto se considera «cerca de 1»


def run_ablation(cfg: dict, best_configs: dict) -> dict:
    """Entrena ambos modelos con el conjunto B (hiperparámetros elegidos con A) y evalúa solo en valid."""
    cols = modeling.feature_order("B")
    train = modeling.load_features("B", ("train",))
    valid = modeling.load_features("B", ("valid",))
    Xtr, ytr = modeling.matrix(train, cols), train["is_fraud"].to_numpy()
    Xva, yva = modeling.matrix(valid, cols), valid["is_fraud"].to_numpy()
    out = {
        "feature_order": cols,
        "hyperparameters_from": "conjunto A (mejor de cada familia en valid)",
    }
    logreg = modeling.make_logreg(cfg, best_configs["logreg"]["C"]).fit(Xtr, ytr)
    out["logreg"] = modeling.evaluate_partition(modeling.positive_proba(logreg, Xva), valid, cfg)
    xp = best_configs["xgb"]
    xgb = modeling.make_xgb(cfg, xp["max_depth"], xp["scale_pos_weight"])
    xgb.fit(Xtr, ytr, eval_set=[(Xva, yva)], verbose=False)
    out["xgb"] = modeling.evaluate_partition(modeling.positive_proba(xgb, Xva), valid, cfg)
    out["xgb"]["best_iteration"] = int(xgb.best_iteration)
    importances = {n: float(v) for n, v in zip(cols, xgb.feature_importances_, strict=True)}
    out["xgb"]["top_gain_importance"] = dict(sorted(importances.items(), key=lambda kv: -kv[1])[:5])
    out["artifact_suspected"] = bool(max(out["logreg"]["pr_auc"], out["xgb"]["pr_auc"]) >= NEAR_ONE)
    return out


def load_registered(entry: dict):
    uri = f"models:/{entry['name']}/{entry['version']}"
    return (mlflow.xgboost if entry["name"].endswith("xgb") else mlflow.sklearn).load_model(uri)


def explain_xgb(selection: dict, valid, cols: list[str]) -> tuple[list[tuple[str, float]], str]:
    model = load_registered(selection["models"]["xgb"])
    rng = np.random.default_rng(load_config()["seed"])
    rows = rng.choice(len(valid), size=min(SHAP_ROWS, len(valid)), replace=False)
    X = modeling.matrix(valid.iloc[rows], cols)
    values = shap.TreeExplainer(model).shap_values(X)
    importance = np.abs(values).mean(axis=0)
    ranked = sorted(zip(cols, importance.astype(float), strict=True), key=lambda kv: -kv[1])
    FIGURES.mkdir(parents=True, exist_ok=True)
    fig, ax = plt.subplots(figsize=(6.4, 4))
    top = ranked[:10][::-1]
    ax.barh([n for n, _ in top], [v for _, v in top])
    ax.set(title=f"SHAP global (media |valor|, {len(X):,} filas de valid)", xlabel="log-odds")
    fig.tight_layout()
    fig.savefig(FIGURES / "shap_global.png", dpi=80)
    plt.close(fig)
    return ranked, "figures/shap_global.png"


def explain_logreg(selection: dict, cols: list[str]) -> list[tuple[str, float]]:
    model = load_registered(selection["models"]["logreg"])
    coefs = model.named_steps["clf"].coef_[
        0
    ]  # por valor estandarizado (el scaler está dentro del Pipeline)
    return sorted(zip(cols, coefs.astype(float), strict=True), key=lambda kv: -abs(kv[1]))


def md_table(headers: list[str], rows: list[list]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    lines += ["| " + " | ".join(str(c) for c in row) + " |" for row in rows]
    return "\n".join(lines)


def ci(interval: dict | None, digits=4) -> str:
    return "–" if not interval else f"[{interval['low']:.{digits}f}, {interval['high']:.{digits}f}]"


def metrics_rows(result: dict, intervals: dict | None, main_block: str) -> list[list]:
    """Filas de una tabla de métricas con su intervalo de bloques principal."""
    m = (intervals or {}).get(main_block, {}).get("metrics", {})
    op = result["operating_points"]
    rows = [["PR-AUC", f"{result['pr_auc']:.4f}", ci(m.get("pr_auc"))]]
    for name, point in op.items():
        target = name.removeprefix("fpr_")
        rows.append(
            [
                f"Recall a FPR {float(target):.1%} (FPR real {point['fpr']:.3%})",
                f"{point['recall']:.4f}",
                ci(m.get(f"recall@{name}")),
            ]
        )
        rows.append(
            [
                f"Recall ponderado por monto a FPR {float(target):.1%}",
                f"{point['recall_weighted']:.4f}",
                ci(m.get(f"recall_weighted@{name}")),
            ]
        )
    d = result["decision"]
    rows.append(["Costo C", f"{d['cost']:,.0f}", ci(m.get("cost"), 0)])
    rows.append(["Costo sin modelo (todo aprobado)", f"{result['no_model_cost']:,.0f}", "–"])
    rows.append(
        ["Tasa de revisión / de bloqueo", f"{d['review_rate']:.3%} / {d['block_rate']:.3%}", "–"]
    )
    rows.append(["FPR de bloqueo", f"{d['block_fpr']:.4%}", "–"])
    rows.append(
        [
            "Recall de bloqueo / de revisión-o-bloqueo",
            f"{d['block_recall']:.4f} / {d['review_or_block_recall']:.4f}",
            "–",
        ]
    )
    return rows  # fmt: skip


def build_report(cfg, selection, ablation, thresholds, test, ranking, served=None) -> str:
    main = str(cfg["bootstrap"]["block_steps"])
    sens = str(cfg["bootstrap"]["sensitivity_block_steps"])
    winner = selection["winner"]
    out = ["# Informe del modelo de fraude", "",
           "Generado por `make ml-report`. Hiperparámetros, umbrales y selección usan solo `valid`; `test` se evalúa una vez con todo congelado.", ""]  # fmt: skip
    # --- modelo servido
    out.append("## Modelo servido")
    manual = selection.get("manual_decision")
    why = f"{selection['reason']}" + (f"; decisión manual: {manual['reason']}" if manual else "")
    # versión servida: la del alias `champion` (si no se da, la de thresholds.json)
    v = served or (thresholds["model_version"] if thresholds else "(sin thresholds.json)")
    tested = test is not None and test["model_version"] != v
    versions = f"`{v}`; evaluado en `test` como `{test['model_version']}`" if tested else f"`{v}`"
    out += [f"**{winner}** ({versions}), conjunto A. Motivo: {why}.", ""]
    if tested:
        explanation = (
            f"`{v}` es un reentrenamiento determinista de `{test['model_version']}` con el mismo `model_sha256` y las "
            "mismas probabilidades en `valid` (ver ADR-11), así que la evaluación de `test` sigue siendo válida."
        )
        out += [explanation, ""]
    for warning in selection.get("warnings", []):
        out += [f"> **Advertencia:** {warning}.", ""]
    out.append(md_table(["Criterio", "Regla", "Valor", "Resultado"],
                        [[c["name"], c["rule"], json.dumps(c["value"], ensure_ascii=False) if isinstance(c["value"], dict) else f"{c['value']:.5g}", c["result"]]
                         for c in selection["criteria"]]))  # fmt: skip
    nb = {f: selection["intervals"][f] for f in ("logreg", "xgb")}
    out += ["", (f"Intervalos de PR-AUC en `valid` con bloques de {main} pasos "
            f"({nb['logreg'][main]['n_blocks']} bloques; el mínimo fiable es {cfg['bootstrap']['min_blocks']}) "
            f"y de {sens} pasos ({nb['logreg'][sens]['n_blocks']} bloques):"), ""]  # fmt: skip
    out.append(md_table(["Modelo", f"Bloques de {main} pasos", f"Bloques de {sens} pasos"],
                        [[f, ci(nb[f][main]["metrics"]["pr_auc"]), ci(nb[f][sens]["metrics"]["pr_auc"])] for f in nb]))  # fmt: skip
    lat = selection["latency"]
    out += ["", "Latencia de inferencia (ONNX Runtime, un hilo, una fila por llamada, "
            f"{lat['xgb']['calls']:,} llamadas): " + "; ".join(f"{f} p95 {lat[f]['p95_ms']:.4f} ms" for f in lat) + ".", ""]  # fmt: skip

    # --- valid
    out += ["## Métricas en valid (pasos 425–550)", ""]
    for family in ("logreg", "xgb"):
        res = selection["valid"][family]
        cfg_text = ", ".join(f"{k}={v}" for k, v in selection["best_configs"][family].items())
        out += [f"### {family} ({cfg_text})", ""]
        out.append(
            md_table(
                ["Métrica", "Valor", f"Intervalo 95 % (bloques de {main})"],
                metrics_rows(res, selection["intervals"][family], main),
            )
        )
        out += [
            "",
            "Sensibilidad del costo (umbrales fijos; costos unitarios ×0,5 / ×1 / ×1,5):",
            "",
        ]
        out.append(md_table(["review × / block_fp ×", "Costo"], [[k.replace("review x", "").replace(", block_fp x", " / "), f"{v:,.0f}"] for k, v in res["cost_sensitivity"].items()]))  # fmt: skip
        out.append("")
    out += [
        "Los umbrales de FPR fija (0,5 % y 0,1 %) se fijan en valid; el FPR real es el de esa partición.",
        "",
    ]

    # --- umbrales
    out += ["## Umbrales (fijados en valid)", ""]
    rows = [
        [
            f,
            f"{selection['valid'][f]['thresholds']['t_block']:.5f}",
            f"{selection['valid'][f]['thresholds']['t_review']:.5f}",
        ]
        for f in ("logreg", "xgb")
    ]
    out.append(md_table(["Modelo", "t_block (FPR de bloqueo ≤ 0,05 %)", "t_review (mín. C, revisión ≤ 0,5 %, ≤ t_block)"], rows))  # fmt: skip
    out += [
        "",
        "Decisión: `score = round(p, 5)`; BLOCK si `score >= t_block`; si no, REVIEW si `score >= t_review`; si no, APPROVE.",
        "",
    ]

    # --- test
    out.append("## Métricas en test (pasos 575–744, evaluado una vez)")
    out.append("")
    if test is None:
        out += ["Aún no se ha ejecutado `make ml-eval-test`.", ""]
    else:
        same = "" if test["model_version"] == v else (
            f"; el modelo servido, `{v}`, tiene los mismos pesos (mismo `model_sha256`), por lo que vale para ambos (ver ADR-11)")
        out += [f"Modelo evaluado: `{test['model_version']}`, umbrales congelados de valid{same}"
                + (" (**reevaluado con --force-test**)" if test.get("test_reevaluated") else "") + ".", ""]  # fmt: skip
        n24 = test["intervals"][main]["n_blocks"]
        out.append(
            md_table(
                ["Métrica", "Valor", f"Intervalo 95 % (bloques de {main})"],
                metrics_rows(test["overall"], test["intervals"], main),
            )
        )
        out += ["", f"La partición test tiene {n24} bloques de {main} pasos"
                + (f" (menos de {cfg['bootstrap']['min_blocks']}: los intervalos son poco fiables)" if n24 < cfg["bootstrap"]["min_blocks"] else "")
                + f" y {test['intervals'][sens]['n_blocks']} de {sens} pasos.", ""]  # fmt: skip
        u = test["unseen_destinations"]
        out += [
            f"### Destinos no vistos en train ({u['share_of_rows']:.1%} de las filas de test; {u['n_fraud']:,} fraudes)",
            "",
        ]
        out.append(md_table(["Métrica", "Valor", "Intervalo"], metrics_rows(u, None, main)))
        out += ["", "### FPR por tipo", ""]
        out.append(md_table(["Tipo", "Legítimas", "Fraudes", "FPR bloqueo", "FPR revisión o bloqueo", "Recall rev.+bloq."],
                            [[r["group"], r["n_legit"], r["n_fraud"], pct(r["fpr_block"]), pct(r["fpr_review_or_block"]), pct(r["recall_review_or_block"], 1)] for r in test["fpr_by_type"]]))  # fmt: skip
        out += ["", "### FPR por tramo de monto", ""]
        out.append(md_table(["Tramo", "Legítimas", "Fraudes", "FPR bloqueo", "FPR revisión o bloqueo", "Recall rev.+bloq."],
                            [[r["group"], r["n_legit"], r["n_fraud"], pct(r["fpr_block"]), pct(r["fpr_review_or_block"]), pct(r["recall_review_or_block"], 1)] for r in test["fpr_by_amount_band"]]))  # fmt: skip
        nm = test["vs_no_model"]
        out += ["", "### Frente a no tener modelo", "",
                (f"Todo aprobado cuesta {nm['no_model_cost']:,.0f} (recall 0). El modelo cuesta {nm['model_cost']:,.0f} "
                f"({nm['cost_reduction']:.1%} menos) y detecta el {nm['model_review_or_block_recall']:.1%} de los fraudes en REVIEW o BLOCK."), ""]  # fmt: skip

    # --- explicabilidad
    out.append("## Explicabilidad")
    for kind, ranked in ranking.items():
        if kind == "xgb":
            out += ["", f"SHAP global del XGBoost (TreeExplainer, {SHAP_ROWS:,} filas de valid):", "",
                    "![SHAP global](figures/shap_global.png)", ""]  # fmt: skip
            out.append(
                md_table(["Feature", "Media \\|SHAP\\|"], [[n, f"{v:.4f}"] for n, v in ranked[:10]])
            )
        else:
            out += [
                "",
                "Regresión logística: coeficiente por valor estandarizado (log-odds por desviación típica):",
                "",
            ]
            out.append(md_table(["Feature", "Coeficiente"], [[n, f"{v:+.4f}"] for n, v in ranked]))
    out.append("")

    # --- ablación
    out += ["## Ablación: conjunto B (con saldo), solo valid", ""]
    rows = []
    for family in ("logreg", "xgb"):
        a, b = selection["valid"][family], ablation[family]
        rows.append(
            [
                family,
                f"{a['pr_auc']:.4f}",
                f"{b['pr_auc']:.4f}",
                f"{a['operating_points']['fpr_0.005']['recall']:.4f}",
                f"{b['operating_points']['fpr_0.005']['recall']:.4f}",
            ]
        )
    out.append(
        md_table(["Modelo", "PR-AUC A", "PR-AUC B", "Recall@0,5 % A", "Recall@0,5 % B"], rows)
    )
    out += [
        "",
        f"Mayores ganancias del XGBoost con B: {ablation['xgb']['top_gain_importance']}.",
        "",
    ]
    if ablation["artifact_suspected"]:
        out += [
            f"El PR-AUC de B es ≥ {NEAR_ONE}: es el artefacto del simulador descrito en §5b (el fraude vacía la cuenta) y B no se sirve."
        ]
    else:
        out += [
            f"El PR-AUC de B no llega a {NEAR_ONE}. Esta es la evidencia; la decisión entre A y B no se toma aquí."
        ]
    out += [""]

    # --- limitaciones
    out += ["## Limitaciones", "",
            "- Datos sintéticos (PaySim): las cifras no se extrapolan a clientes reales.",
            "- El fraude solo ocurre en TRANSFER y CASH_OUT; el modelo no ha visto fraude en PAYMENT ni DEBIT.",
            "- Cambio de régimen en el volumen alrededor del paso 400 (ver `eda.md`): la prevalencia de fraude (sobre las filas de los cuatro tipos del modelo, TRANSFER, CASH_OUT, PAYMENT y DEBIT; sin CASH_IN) pasa de 0,099 % en train a 0,976 % en valid y 1,490 % en test; los umbrales fijados en valid no se trasladan limpiamente.",
            f"- Pocos bloques de pasos: {nb['logreg'][main]['n_blocks']} en valid con bloques de {main} pasos; los intervalos por bootstrap de bloques son poco fiables por debajo de {cfg['bootstrap']['min_blocks']}.",
            "- Casi todos los orígenes aparecen una sola vez: `src_out_*` e `is_new_beneficiary` casi no aportan información en PaySim.",
            "- El costo está dominado por el monto de los fraudes aprobados; los costos unitarios son supuestos (sensibilidad ±50 %).", ""]  # fmt: skip
    return "\n".join(out)


def champion_version(client, thresholds: dict | None) -> str | None:
    """`nombre/versión` del alias `champion` actual; None si no hay thresholds.json ni alias."""
    if thresholds is None:
        return None
    name = thresholds["model_version"].split("/")[0]
    try:
        return f"{name}/{client.get_model_version_by_alias(name, 'champion').version}"
    except mlflow.exceptions.MlflowException:
        return thresholds["model_version"]


def pct(value, digits=3):
    return "–" if value is None else f"{value:.{digits}%}"


def report_command(argv: list[str]) -> int:
    del argv
    cfg = load_config()
    if not SELECTION_PATH.exists():
        print("falta ml/reports/selection.json: ejecuta make ml-train", file=sys.stderr)
        return 2
    selection = json.loads(SELECTION_PATH.read_text())
    client = tracking.setup(cfg)
    ablation = run_ablation(cfg, selection["best_configs"])
    tracking.dump_json(ABLATION_PATH, ablation)
    with mlflow.start_run(run_name="ablation-B"):
        mlflow.set_tags({"stage": "ablation-valid", "feature_set": "B"})
        for family in ("logreg", "xgb"):
            mlflow.log_metrics(
                {f"{family}_{k}": v for k, v in modeling.flat_metrics(ablation[family]).items()}
            )
    thresholds = json.loads(THRESHOLDS_PATH.read_text()) if THRESHOLDS_PATH.exists() else None
    test = json.loads(TEST_REPORT_PATH.read_text()) if TEST_REPORT_PATH.exists() else None
    cols = modeling.feature_order("A")
    valid = modeling.load_features("A", ("valid",))
    winner = selection["winner"]
    ranking = {}
    if winner == "xgb":
        ranking["xgb"], _ = explain_xgb(selection, valid, cols)
    else:
        ranking["logreg"] = explain_logreg(selection, cols)
    served = champion_version(client, thresholds)
    REPORT_PATH.write_text(build_report(cfg, selection, ablation, thresholds, test, ranking, served))
    print(f"escrito {REPORT_PATH} y {ABLATION_PATH}")
    return 0
