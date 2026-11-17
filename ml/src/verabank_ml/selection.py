"""Criterio de selección de modelo (docs/diseno.md §5d), calculado por código.

El boosting se sirve solo si, en `valid`: (1) mejora el PR-AUC en >= min_pr_auc_gain, (2) con intervalos
que no se solapan, (3) baja el costo y (4) su inferencia p95 es < max_inference_p95_ms. Si no, gana la
regresión logística. Si el criterio de intervalos no se puede aplicar (menos de `min_blocks` bloques) se
marca INDETERMINADO. Una condición en FAIL decide por sí sola (la conjunción ya es falsa); sin FAIL, un
INDETERMINADO deja la decisión pendiente.
"""

PASS, FAIL, INDETERMINADO = "PASS", "FAIL", "INDETERMINADO"
_TOLERANCE = 1e-12  # 0,42 - 0,40 = 0,01999… en coma flotante: el borde >= 0,02 debe incluirse


def select_model(
    logreg: dict, xgb: dict, latency_p95_ms: float, intervals: dict, cfg: dict
) -> dict:
    """`logreg` y `xgb` son los resultados de `evaluate_partition` en valid del mejor de cada familia.

    `intervals[familia][block_steps]` = {"n_blocks", "metrics": {"pr_auc": {"low", "high"}, ...}}.
    """
    rules = cfg["selection"]
    main = str(cfg["bootstrap"]["block_steps"])
    gain = xgb["pr_auc"] - logreg["pr_auc"]
    criteria = [
        {
            "name": "pr_auc_gain",
            "rule": f"PR-AUC(xgb) - PR-AUC(logreg) >= {rules['min_pr_auc_gain']}",
            "value": gain,
            "result": PASS if gain >= rules["min_pr_auc_gain"] - _TOLERANCE else FAIL,
        }
    ]
    lr_ci, xgb_ci = intervals["logreg"][main], intervals["xgb"][main]
    n_blocks = lr_ci["n_blocks"]
    interval_value = {
        "n_blocks_valid": n_blocks,
        "block_steps": int(main),
        "logreg_pr_auc_interval": [
            lr_ci["metrics"]["pr_auc"]["low"],
            lr_ci["metrics"]["pr_auc"]["high"],
        ],
        "xgb_pr_auc_interval": [
            xgb_ci["metrics"]["pr_auc"]["low"],
            xgb_ci["metrics"]["pr_auc"]["high"],
        ],
    }
    if n_blocks < cfg["bootstrap"]["min_blocks"]:
        result = INDETERMINADO
    elif xgb_ci["metrics"]["pr_auc"]["low"] > lr_ci["metrics"]["pr_auc"]["high"]:
        result = PASS
    else:
        result = FAIL
    criteria.append(
        {
            "name": "pr_auc_intervals_do_not_overlap",
            "rule": f"low(xgb) > high(logreg) con bloques de {main} pasos y al menos "
            f"{cfg['bootstrap']['min_blocks']} bloques en valid",
            "value": interval_value,
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
    results = [c["result"] for c in criteria]
    if FAIL in results:
        winner, status = "logreg", "DECIDIDO"
        reason = "el boosting no se sirve: falla " + ", ".join(
            c["name"] for c in criteria if c["result"] == FAIL
        )
    elif INDETERMINADO in results:
        winner, status = None, "PENDIENTE"
        reason = "criterio(s) indeterminado(s): " + ", ".join(
            c["name"] for c in criteria if c["result"] == INDETERMINADO
        )
    else:
        winner, status = "xgb", "DECIDIDO"
        reason = "el boosting cumple los cuatro criterios"
    return {"status": status, "winner": winner, "reason": reason, "criteria": criteria}
