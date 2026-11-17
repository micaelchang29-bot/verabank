"""Split temporal por `step` (docs/diseno.md §5c): train, valid, test y gap.

Las filas `gap` (los huecos entre particiones y cualquier `step` fuera de los cortes) no se usan para
entrenar ni evaluar, pero sí cuentan como historia en el cálculo de features.
"""

import numpy as np

PARTITIONS = ("train", "valid", "test")
GAP = "gap"


def validate_split(split: dict) -> None:
    """Los cortes deben ser intervalos ordenados y separados por exactamente `gap_steps` pasos."""
    bounds = [tuple(split[name]) for name in PARTITIONS]
    for name, (start, end) in zip(PARTITIONS, bounds, strict=True):
        if start > end:
            raise ValueError(f"corte {name} invertido: {start} > {end}")
    for (name, (_, end)), (nxt, (start, _)) in zip(
        zip(PARTITIONS, bounds, strict=True),
        zip(PARTITIONS[1:], bounds[1:], strict=True),
        strict=False,
    ):
        hole = start - end - 1
        if hole != split["gap_steps"]:
            raise ValueError(
                f"hueco entre {name} y {nxt}: {hole} pasos, config.split.gap_steps = {split['gap_steps']}"
            )


def assign_partition(step: np.ndarray, split: dict) -> np.ndarray:
    """Etiqueta cada `step` con train, valid, test o gap según los cortes (intervalos cerrados)."""
    validate_split(split)
    step = np.asarray(step)
    conditions = [(step >= split[name][0]) & (step <= split[name][1]) for name in PARTITIONS]
    return np.select(conditions, list(PARTITIONS), default=GAP)
