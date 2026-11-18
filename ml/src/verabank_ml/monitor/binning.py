"""Binning y PSI comunes a la referencia (entrenamiento) y a las ventanas actuales (D1, §5f).

Un mismo código para ambos lados: ``fit_bins`` decide los bins con los valores de entrenamiento y ``proportions``
reparte cualquier muestra en esos bins.

- Más de 10 valores distintos: 10 bins por deciles con bordes únicos. Los bordes son los cuantiles 0,1 … 0,9; los
  repetidos se fusionan. Bin ``i`` = ``(borde[i-1], borde[i]]`` con los extremos abiertos a ±∞.
- Hasta 10 valores distintos: un bin por valor más un bin «otro» (valores no vistos en entrenamiento).
- Un solo bin (feature constante, o todos los cuantiles iguales): ``degenerate`` y PSI 0.
- Proporciones acotadas inferiormente a 1e-4 y **sin renormalizar**; PSI = Σ (a − e)·ln(a / e).

Los valores se redondean a 9 decimales antes de binarizar: el servicio (Java) y el entrenamiento (Python) calculan
``hour_sin``/``hour_cos`` con diferencias en el último bit, y un valor justo en un borde cambiaría de bin.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

N_DECILES = 10
MAX_CATEGORICAL = 10
FLOOR = 1e-4
DECIMALS = 9


@dataclass(frozen=True)
class Bins:
    kind: str                       # "quantile" | "categorical"
    edges: tuple[float, ...] = ()   # bordes interiores (quantile)
    values: tuple[float, ...] = ()  # valores (categorical); el bin «otro» va al final

    @property
    def n_bins(self) -> int:
        return len(self.edges) + 1 if self.kind == "quantile" else len(self.values) + 1

    @property
    def degenerate(self) -> bool:
        # Categórica de un solo valor: el bin «otro» está vacío en entrenamiento; cuenta como un solo bin.
        return (self.n_bins if self.kind == "quantile" else len(self.values)) <= 1

    def to_json(self) -> dict:
        out = {"kind": self.kind, "n_bins": self.n_bins, "degenerate": self.degenerate}
        if self.kind == "quantile":
            out["edges"] = list(self.edges)
        else:
            out["values"] = list(self.values)
        return out

    @classmethod
    def from_json(cls, data: dict) -> Bins:
        return cls(data["kind"], tuple(data.get("edges", ())), tuple(data.get("values", ())))


def clean(values) -> np.ndarray:
    return np.round(np.asarray(values, dtype=np.float64), DECIMALS)


def fit_bins(train_values) -> Bins:
    x = clean(train_values)
    if x.size == 0:
        raise ValueError("sin valores de entrenamiento")
    distinct = np.unique(x)
    if distinct.size <= MAX_CATEGORICAL:
        return Bins("categorical", values=tuple(float(v) for v in distinct))
    qs = np.quantile(x, np.arange(1, N_DECILES) / N_DECILES, method="linear")
    edges = np.unique(np.round(qs, DECIMALS))
    return Bins("quantile", edges=tuple(float(e) for e in edges))


def assign(bins: Bins, values) -> np.ndarray:
    """Índice de bin de cada valor."""
    x = clean(values)
    if bins.kind == "quantile":
        return np.searchsorted(np.asarray(bins.edges, dtype=np.float64), x, side="left")
    known = np.asarray(bins.values, dtype=np.float64)
    pos = np.searchsorted(known, x)
    hit = (pos < known.size) & (known[np.minimum(pos, known.size - 1)] == x)
    return np.where(hit, pos, known.size)                        # «otro» = último bin


def proportions(bins: Bins, values) -> np.ndarray:
    """Proporción por bin, acotada a ≥ FLOOR y sin renormalizar."""
    idx = assign(bins, values)
    n = idx.size
    counts = np.bincount(idx, minlength=bins.n_bins).astype(np.float64)
    raw = counts / n if n else np.zeros(bins.n_bins)
    return np.maximum(raw, FLOOR)


def psi(expected, actual) -> float:
    e = np.maximum(np.asarray(expected, dtype=np.float64), FLOOR)
    a = np.maximum(np.asarray(actual, dtype=np.float64), FLOOR)
    if e.shape != a.shape:
        raise ValueError("expected y actual con distinto número de bins")
    return float(np.sum((a - e) * np.log(a / e)))
