"""Bootstrap por bloques de pasos (un bloque = `block_steps` pasos consecutivos de la partición).

Remuestrea bloques con reemplazo (multinomial, semilla fija): cada remuestreo es un vector de
multiplicidades por bloque. Las métricas con umbrales fijos se obtienen de agregados por bloque; el
PR-AUC, de una versión ponderada y ordenada una sola vez.
"""

import numpy as np


def block_ids(step: np.ndarray, start: int, block_steps: int) -> tuple[np.ndarray, int]:
    """Bloque de cada fila (contando desde `start`) y número de bloques (el último puede ser parcial)."""
    ids = (np.asarray(step) - start) // block_steps
    return ids.astype(np.int64), int(ids.max()) + 1


def resample_counts(n_blocks: int, n_resamples: int, seed: int) -> np.ndarray:
    """Multiplicidades [n_resamples, n_blocks]: n_blocks sorteos con reemplazo por remuestreo."""
    rng = np.random.default_rng(seed)
    return rng.multinomial(n_blocks, np.full(n_blocks, 1.0 / n_blocks), size=n_resamples)


def block_aggregates(columns: np.ndarray, ids: np.ndarray, n_blocks: int) -> np.ndarray:
    """Suma por bloque de cada columna: [n_blocks, k]."""
    columns = np.asarray(columns, dtype=np.float64)
    if columns.ndim == 1:
        columns = columns[:, None]
    return np.stack(
        [
            np.bincount(ids, weights=columns[:, j], minlength=n_blocks)
            for j in range(columns.shape[1])
        ],
        axis=1,
    )


class WeightedPRAUC:
    """PR-AUC (average precision) con multiplicidades por bloque, idéntica a la de scikit-learn."""

    def __init__(self, scores: np.ndarray, y: np.ndarray, ids: np.ndarray):
        _, group = np.unique(
            -np.asarray(scores, dtype=np.float64), return_inverse=True
        )  # 0 = score más alto
        self.group = group
        self.n_groups = int(group.max()) + 1
        self.y = np.asarray(y, dtype=np.float64)
        self.ids = ids

    def __call__(self, multiplicity: np.ndarray) -> float:
        w = multiplicity[self.ids].astype(np.float64)
        tp = np.cumsum(np.bincount(self.group, weights=w * self.y, minlength=self.n_groups))
        fp = np.cumsum(np.bincount(self.group, weights=w * (1 - self.y), minlength=self.n_groups))
        total_tp = tp[-1]
        if total_tp == 0:
            return float("nan")
        precision = tp / np.maximum(tp + fp, 1e-12)
        recall = tp / total_tp
        return float(np.sum(np.diff(np.concatenate(([0.0], recall))) * precision))


def percentile_interval(samples: np.ndarray, confidence: float) -> tuple[float, float]:
    samples = np.asarray(samples, dtype=np.float64)
    samples = samples[~np.isnan(samples)]
    alpha = (1 - confidence) / 2
    low, high = np.quantile(samples, [alpha, 1 - alpha])
    return float(low), float(high)


def bootstrap_pr_auc(scores, y, step, start, block_steps, n_resamples, seed, confidence) -> dict:
    ids, n_blocks = block_ids(step, start, block_steps)
    counts = resample_counts(n_blocks, n_resamples, seed + block_steps)
    metric = WeightedPRAUC(scores, y, ids)
    samples = np.array([metric(c) for c in counts])
    low, high = percentile_interval(samples, confidence)
    return {"n_blocks": n_blocks, "block_steps": block_steps, "low": low, "high": high}


def bootstrap_metrics(
    probabilities, scores, y, amount, step, start, block_steps, operating_thresholds: dict,
    t_review, t_block, costs: dict, n_resamples: int, seed: int, confidence: float,
) -> dict:  # fmt: skip
    """Intervalos por bootstrap de bloques de PR-AUC, recall (y ponderado) a los umbrales de FPR fija y costo.

    Los umbrales se congelan antes (no se reestiman en cada remuestreo). Devuelve
    {"n_blocks", "block_steps", "metrics": {nombre: {"low", "high"}}}.
    """
    from verabank_ml.metrics import APPROVE, BLOCK, REVIEW, decide

    y = np.asarray(y)
    amount = np.asarray(amount, dtype=np.float64)
    pos, neg = (y == 1).astype(np.float64), (y == 0).astype(np.float64)
    columns = {"pos": pos, "neg": neg, "pos_amount": pos * amount}
    for name, threshold in operating_thresholds.items():
        flagged = (np.asarray(scores) >= threshold).astype(np.float64)
        columns[f"{name}:pos"] = flagged * pos
        columns[f"{name}:neg"] = flagged * neg
        columns[f"{name}:pos_amount"] = flagged * pos * amount
    decisions = decide(scores, t_review, t_block)
    columns["approved_fraud_amount"] = (decisions == APPROVE) * pos * amount
    columns["n_review"] = (decisions == REVIEW).astype(np.float64)
    columns["blocked_legit"] = ((decisions == BLOCK) & (y == 0)).astype(np.float64)
    names = list(columns)

    ids, n_blocks = block_ids(step, start, block_steps)
    counts = resample_counts(n_blocks, n_resamples, seed + block_steps)
    totals = counts @ block_aggregates(np.column_stack([columns[n] for n in names]), ids, n_blocks)
    col = {n: totals[:, i] for i, n in enumerate(names)}
    samples = {}
    for name in operating_thresholds:
        samples[f"recall@{name}"] = col[f"{name}:pos"] / col["pos"]
        samples[f"fpr@{name}"] = col[f"{name}:neg"] / col["neg"]
        samples[f"recall_weighted@{name}"] = col[f"{name}:pos_amount"] / col["pos_amount"]
    samples["cost"] = (
        col["approved_fraud_amount"]
        + costs["review"] * col["n_review"]
        + costs["block_false_positive"] * col["blocked_legit"]
    )
    metric = WeightedPRAUC(probabilities, y, ids)
    samples["pr_auc"] = np.array([metric(c) for c in counts])
    out = {}
    for name, values in samples.items():
        low, high = percentile_interval(values, confidence)
        out[name] = {"low": low, "high": high}
    return {"n_blocks": n_blocks, "block_steps": block_steps, "metrics": out}
