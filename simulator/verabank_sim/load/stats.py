"""Estadísticos de latencia con percentiles por rango más cercano (deterministas, sin interpolación)."""

from __future__ import annotations

import math
from collections.abc import Iterable


def percentile(values: list[float], p: float) -> float:
    """Percentil ``p`` (0 a 100) por rango más cercano de una lista ya ordenada."""
    if not values:
        raise ValueError("sin valores")
    rank = max(1, math.ceil(p / 100.0 * len(values)))
    return values[min(rank, len(values)) - 1]


def describe(values: Iterable[float]) -> dict[str, float | int | None]:
    ordered = sorted(values)
    if not ordered:
        return {"n": 0, "p50": None, "p95": None, "p99": None}
    return {"n": len(ordered), **{f"p{p}": round(percentile(ordered, p), 3) for p in (50, 95, 99)}}
