"""Criterio de selección de modelo (docs/diseno.md §5d, decisión D3), calculado por código.

El boosting se sirve solo si, en `valid`: (1) mejora el PR-AUC en >= min_pr_auc_gain, (2) con intervalos
que no se solapan, (3) baja el costo y (4) su inferencia p95 es < max_inference_p95_ms.

Criterio de intervalos (D3): se evalúa con bloques de `bootstrap.block_steps` (24) pasos y se contrasta con los
de `sensitivity_block_steps` (6). Pasa solo si no hay solape con ambos tamaños; si el resultado difiere entre
tamaños, cuenta como NO cumplido. Tener menos de `min_blocks` bloques no detiene ni deja indeterminada la
selección: se registra como advertencia en `warnings`.
"""

PASS, FAIL = "PASS", "FAIL"
_TOLERANCE = 1e-12  # 0,42 - 0,40 = 0,01999… en coma flotante: el borde >= 0,02 debe incluirse


def select_model(
    logreg: dict, xgb: dict, latency_p95_ms: float, intervals: dict, cfg: dict
) -> dict:
    """`logreg` y `xgb` son los resultados de `evaluate_partition` en valid del mejor de cada familia.

    `intervals[familia][block_steps]` = {"n_blocks", "metrics": {"pr_auc": {"low", "high"}, ...}}.
    """
    rules = cfg["selection"]
    gain = xgb["pr_auc"] - logreg["pr_auc"]
    criteria = [
        {
            "name": "pr_auc_gain",
            "rule": f"PR-AUC(xgb) - PR-AUC(logreg) >= {rules['min_pr_auc_gain']}",
            "value": gain,
            "result": PASS if gain >= rules["min_pr_auc_gain"] - _TOLERANCE else FAIL,
        }
    ]
    sizes = {
        "main": str(cfg["bootstrap"]["block_steps"]),
        "sensitivity": str(cfg["bootstrap"]["sensitivity_block_steps"]),
    }
    value, no_overlap, warnings = {}, {}, []
    for label, size in sizes.items():
        lr_ci, xgb_ci = intervals["logreg"][size], intervals["xgb"][size]
        lr, xg = lr_ci["metrics"]["pr_auc"], xgb_ci["metrics"]["pr_auc"]
        no_overlap[label] = xg["low"] > lr["high"]
        value[f"block_steps_{size}"] = {
            "n_blocks_valid": lr_ci["n_blocks"],
            "logreg_pr_auc_interval": [lr["low"], lr["high"]],
            "xgb_pr_auc_interval": [xg["low"], xg["high"]],
            "no_overlap": bool(no_overlap[label]),
        }
        if lr_ci["n_blocks"] < cfg["bootstrap"]["min_blocks"]:
            warnings.append(
                f"valid tiene {lr_ci['n_blocks']} bloques de {size} pasos (menos de "
                f"{cfg['bootstrap']['min_blocks']}): los intervalos por bootstrap de bloques son poco fiables"
            )
    if no_overlap["main"] and no_overlap["sensitivity"]:
        result = PASS
    else:
        result = FAIL
        if no_overlap["main"] != no_overlap["sensitivity"]:
            value["differs_between_block_sizes"] = True
    criteria.append(
        {
            "name": "pr_auc_intervals_do_not_overlap",
            "rule": f"low(xgb) > high(logreg) con bloques de {sizes['main']} y de {sizes['sensitivity']} pasos; "
            "si difieren, cuenta como no cumplido",
            "value": value,
            "result": result,
        }
    )
    cost_lr, cost_xgb = logreg["decision"]["cost"], xgb["decision"]["cost"]
    criteria.append(
        {
            "name": "cost_lower",
            "rule": "costo(xgb) < costo(logreg) en valid, cada uno con sus umbrales",
            "value": {"logreg": cost_lr, "xgb": cost_xgb},
            "result": PASS if cost_xgb < cost_lr else FAIL,
        }
    )
    criteria.append(
        {
            "name": "inference_p95",
            "rule": f"p95 de xgb en ONNX Runtime < {rules['max_inference_p95_ms']} ms",
            "value": latency_p95_ms,
            "result": PASS if latency_p95_ms < rules["max_inference_p95_ms"] else FAIL,
        }
    )
    failing = [c["name"] for c in criteria if c["result"] == FAIL]
    if failing:
        winner, reason = "logreg", "el boosting no se sirve: falla " + ", ".join(failing)
    else:
        winner, reason = "xgb", "el boosting cumple los cuatro criterios"
    return {
        "status": "DECIDIDO",
        "winner": winner,
        "reason": reason,
        "warnings": warnings,
        "criteria": criteria,
    }
