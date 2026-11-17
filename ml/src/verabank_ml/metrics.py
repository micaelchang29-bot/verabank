"""Métricas de decisión del modelo de fraude (docs/diseno.md §5c).

Los scores se redondean a `DECIMALS` decimales antes de compararlos con un umbral, igual que el servicio;
la decisión es `score >= umbral`. Las métricas de decisión (FPR, tasa de revisión, costo) usan scores
redondeados; el PR-AUC es una métrica de ranking y usa las probabilidades sin redondear.

Decisión: BLOCK si score >= t_block; si no, REVIEW si score >= t_review; si no, APPROVE.
Costo: C = Σ monto(fraude APPROVE) + c_review · n(REVIEW) + c_block_fp · n(legítimas BLOCK).
(Un fraude en REVIEW o BLOCK no suma monto: se supone que el analista o el bloqueo lo detienen.)
"""

import numpy as np
from sklearn.metrics import average_precision_score

DECIMALS = 5
APPROVE, REVIEW, BLOCK = 0, 1, 2
_EPS = 1e-12


def round_scores(scores: np.ndarray, decimals: int = DECIMALS) -> np.ndarray:
    return np.round(np.asarray(scores, dtype=np.float64), decimals)


def pr_auc(y: np.ndarray, probabilities: np.ndarray) -> float:
    return float(average_precision_score(y, probabilities))


def _above_all(scores: np.ndarray, decimals: int = DECIMALS) -> float:
    """Un umbral que ningún score alcanza (nada se marca)."""
    return float(np.round(scores.max() + 10.0**-decimals, decimals))


def fpr_threshold(scores, y, max_fpr: float, decimals: int = DECIMALS) -> float:
    """Menor score observado t con FPR(t) = P(score >= t | legítima) <= max_fpr.

    Si ni el score máximo cumple (empates en lo alto), devuelve un umbral que nadie alcanza.
    """
    scores = np.asarray(scores, dtype=np.float64)
    negatives = np.sort(scores[np.asarray(y) == 0])
    candidates = np.unique(scores)
    flagged = len(negatives) - np.searchsorted(negatives, candidates, side="left")
    feasible = flagged <= max_fpr * len(negatives) + _EPS
    if feasible.any():
        return float(candidates[np.argmax(feasible)])
    return _above_all(scores, decimals)


def choose_t_block(scores, y, block_fpr_max: float, decimals: int = DECIMALS) -> float:
    """t_block: el menor umbral con FPR de bloqueo <= block_fpr_max."""
    return fpr_threshold(scores, y, block_fpr_max, decimals)


def decide(scores: np.ndarray, t_review: float, t_block: float) -> np.ndarray:
    scores = np.asarray(scores)
    return np.where(scores >= t_block, BLOCK, np.where(scores >= t_review, REVIEW, APPROVE))


def cost(decisions, y, amount, costs: dict) -> float:
    """Costo de negocio C con los costos unitarios de `costs` (review, block_false_positive)."""
    decisions, y, amount = (
        np.asarray(decisions),
        np.asarray(y),
        np.asarray(amount, dtype=np.float64),
    )
    approved_fraud = float(amount[(decisions == APPROVE) & (y == 1)].sum())
    n_review = int((decisions == REVIEW).sum())
    n_blocked_legit = int(((decisions == BLOCK) & (y == 0)).sum())
    return (
        approved_fraud
        + costs["review"] * n_review
        + costs["block_false_positive"] * n_blocked_legit
    )


def choose_t_review(
    scores, y, amount, t_block: float, costs: dict, review_rate_max: float
) -> float:
    """t_review: minimiza C con tasa de revisión <= review_rate_max y t_review <= t_block.

    La tasa de revisión es n(REVIEW) / N. Los candidatos son los scores observados <= t_block (y t_block,
    que equivale a no revisar). Con costos iguales gana el mayor umbral (menos revisiones).
    """
    scores = np.asarray(scores, dtype=np.float64)
    y, amount = np.asarray(y), np.asarray(amount, dtype=np.float64)
    values, inverse = np.unique(scores, return_inverse=True)
    n_rows = np.bincount(inverse)
    fraud_amount = np.bincount(inverse, weights=amount * (y == 1))
    below = np.concatenate(([0], np.cumsum(n_rows)))  # filas con score < values[i], en below[i]
    fraud_below = np.concatenate(([0.0], np.cumsum(fraud_amount)))
    blocked = values >= t_block
    n_blocked_legit_total = float(np.bincount(inverse, weights=(y == 0))[blocked].sum())
    cut = int(
        np.searchsorted(values, t_block, side="left")
    )  # índice de la primera banda de bloqueo
    candidates = np.append(np.arange(cut), cut)  # el último representa t_review == t_block
    t_candidates = np.append(values[:cut], t_block)
    n_review = below[cut] - below[candidates]
    approved_fraud = fraud_below[candidates]
    total = (
        approved_fraud
        + costs["review"] * n_review
        + costs["block_false_positive"] * n_blocked_legit_total
    )
    feasible = n_review <= review_rate_max * len(scores) + _EPS
    total = np.where(feasible, total, np.inf)
    best = total.min()
    return float(t_candidates[np.flatnonzero(total <= best + _EPS * max(1.0, abs(best)))[-1]])


def operating_point(scores, y, amount, threshold: float) -> dict:
    """FPR real, recall y recall ponderado por monto con `score >= threshold`."""
    scores, y = np.asarray(scores), np.asarray(y)
    amount = np.asarray(amount, dtype=np.float64)
    flagged = scores >= threshold
    pos, neg = y == 1, y == 0
    return {
        "threshold": float(threshold),
        "fpr": float((flagged & neg).sum() / max(neg.sum(), 1)),
        "recall": float((flagged & pos).sum() / max(pos.sum(), 1)),
        "recall_weighted": float(amount[flagged & pos].sum() / max(amount[pos].sum(), 1e-12)),
    }


def decision_summary(scores, y, amount, t_review: float, t_block: float, costs: dict) -> dict:
    """Costo, tasas y FPR de bloqueo con los umbrales dados."""
    decisions = decide(scores, t_review, t_block)
    y = np.asarray(y)
    n_neg = max(int((y == 0).sum()), 1)
    return {
        "cost": cost(decisions, y, amount, costs),
        "review_rate": float((decisions == REVIEW).mean()),
        "block_rate": float((decisions == BLOCK).mean()),
        "block_fpr": float(((decisions == BLOCK) & (y == 0)).sum() / n_neg),
        "block_recall": float(((decisions == BLOCK) & (y == 1)).sum() / max((y == 1).sum(), 1)),
        "review_or_block_recall": float(
            ((decisions != APPROVE) & (y == 1)).sum() / max((y == 1).sum(), 1)
        ),
        "n_review": int((decisions == REVIEW).sum()),
        "n_blocked_legit": int(((decisions == BLOCK) & (y == 0)).sum()),
        "approved_fraud_amount": float(
            np.asarray(amount, dtype=np.float64)[(decisions == APPROVE) & (y == 1)].sum()
        ),
    }


def cost_sensitivity(scores, y, amount, t_review, t_block, costs: dict) -> dict:
    """Costo con los umbrales fijos y ambos costos unitarios multiplicados por (1 ± sensibilidad)."""
    decisions = decide(scores, t_review, t_block)
    out = {}
    for fr in (1 - costs["sensitivity"], 1.0, 1 + costs["sensitivity"]):
        for fb in (1 - costs["sensitivity"], 1.0, 1 + costs["sensitivity"]):
            unit = {
                "review": costs["review"] * fr,
                "block_false_positive": costs["block_false_positive"] * fb,
            }
            out[f"review x{fr:g}, block_fp x{fb:g}"] = cost(decisions, y, amount, unit)
    return out


def no_model_cost(y, amount) -> float:
    """Costo de aprobarlo todo: el monto de todos los fraudes."""
    return float(np.asarray(amount, dtype=np.float64)[np.asarray(y) == 1].sum())
