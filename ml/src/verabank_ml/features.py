"""Features de fraude de `contracts/features.yaml`, punto-en-el-tiempo y vectorizadas.

Semántica (detalle y rellenos en ml/README.md, sección «Features»):
- La fila del paso `s` solo ve filas con `step` estrictamente menor; las del mismo paso no se ven.
- Ventana de 1 h = paso s-1; ventana de 24 h = pasos s-24 a s-1 (cerradas por la izquierda).
- La historia es todo el dataset anterior (de cualquier partición) de los tipos de `config.types`;
  cuenta intentos, no solo transferencias completadas (ADR-07). Ninguna feature usa etiquetas.

Técnica: agregados por clave y paso con sumas acumuladas y búsqueda binaria (sin bucles por fila).
Para cada clave k, `C(k, x)` es la suma de los eventos de k con `step <= x`; una ventana [s-w, s-1] vale
`C(k, s-1) - C(k, s-w-1)`. Los orígenes distintos de una ventana se obtienen con una diferencia de
cobertura por par (destino, origen): cada evento cubre las consultas de los pasos e+1 a e+24.
"""

import argparse
import resource
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from verabank_ml.split import assign_partition, validate_split

REPO_ROOT = Path(__file__).resolve().parents[3]
CONTRACT_PATH = REPO_ROOT / "contracts" / "features.yaml"
CONFIG_PATH = REPO_ROOT / "ml" / "config.yaml"
RAW_PATH = REPO_ROOT / "data" / "raw" / "paysim.csv"
OUT_DIR = REPO_ROOT / "data" / "features"

FEATURE_SETS = ("A", "B")
# Columnas de saldo: ninguna puede llegar a la matriz del conjunto A (el B las usa a propósito).
BALANCE_COLUMNS = frozenset(
    {
        "oldbalanceOrg",
        "newbalanceOrig",
        "oldbalanceDest",
        "newbalanceDest",
        "src_balance_before",
        "amount_to_balance",
    }
)
# Columnas que acompañan a la matriz para análisis; nunca entran a ella.
METADATA_COLUMNS = ["row_id", "step", "type", "is_fraud", "partition", "src_id", "dst_id"]
# Lo único que se lee del CSV: las columnas prohibidas ni siquiera se cargan.
RAW_USECOLS = {
    "A": ["step", "type", "amount", "nameOrig", "nameDest", "isFraud"],
    "B": ["step", "type", "amount", "nameOrig", "nameDest", "isFraud", "oldbalanceOrg"],
}
HOURS_PER_DAY = 24
# Las claves compuestas `clave * _M + paso + _OFF` caben en int64 para claves < 2**43.
_M = 1 << 20
_OFF = 1 << 10
MAX_STEP = _M - 4 * _OFF


class LeakageError(ValueError):
    """Una columna prohibida o de saldo (en el conjunto A) llegó a la matriz de features."""


def load_contract(path: Path = CONTRACT_PATH) -> dict:
    return yaml.safe_load(Path(path).read_text())


def load_config(path: Path = CONFIG_PATH) -> dict:
    return yaml.safe_load(Path(path).read_text())


def feature_columns(feature_set: str, contract: dict) -> list[str]:
    """Columnas de la matriz en el orden de features.yaml; `type` se expande a type_<categoría>."""
    if feature_set not in FEATURE_SETS:
        raise ValueError(f"conjunto desconocido: {feature_set!r} (A o B)")
    columns: list[str] = []
    for feature in contract["features"]:
        if feature["set"] == "B" and feature_set == "A":
            continue
        if feature["type"] == "categorical":
            columns += [f"{feature['name']}_{category}" for category in feature["categories"]]
        else:
            columns.append(feature["name"])
    return columns


def check_matrix_columns(columns, feature_set: str, contract: dict) -> None:
    """Guarda de fuga: error si aparece una entrada prohibida o, en el conjunto A, una columna de saldo."""
    present = set(columns)
    bad = present & set(contract["prohibited_inputs"])
    if feature_set == "A":
        bad |= present & BALANCE_COLUMNS
    if bad:
        raise LeakageError(
            f"columnas prohibidas en la matriz del conjunto {feature_set}: {sorted(bad)}"
        )


# --- sumas acumuladas por clave y paso -------------------------------------------------------------


def _check_steps(step: np.ndarray) -> None:
    if step.size and (step.min() < 0 or step.max() >= MAX_STEP):
        raise ValueError(f"step fuera de [0, {MAX_STEP})")


def _cum_lookup(ev_key, ev_step, ev_val, q_key, q_x) -> np.ndarray:
    """Para cada consulta, suma de `ev_val` de los eventos con su clave y `step <= q_x`."""
    comb = ev_key.astype(np.int64) * _M + ev_step + _OFF
    order = np.argsort(comb, kind="stable")
    comb = comb[order]
    cum = np.concatenate(
        (np.zeros(1, dtype=ev_val.dtype), np.cumsum(ev_val[order], dtype=ev_val.dtype))
    )
    base = q_key.astype(np.int64) * _M
    start = np.searchsorted(comb, base, side="left")
    hi = np.searchsorted(comb, base + q_x + _OFF, side="right")
    return cum[hi] - cum[start]


def _window(ev_key, ev_step, ev_val, q_key, q_step, width) -> np.ndarray:
    """Suma de `ev_val` de los eventos de la clave con step en [s-width, s-1]."""
    hi = _cum_lookup(ev_key, ev_step, ev_val, q_key, q_step - 1)
    lo = _cum_lookup(ev_key, ev_step, ev_val, q_key, q_step - width - 1)
    return hi - lo


def _amount_values(amount: np.ndarray) -> tuple[np.ndarray, float]:
    """Montos en centavos enteros si son exactos (sumas exactas e independientes del orden)."""
    cents = np.rint(amount * 100)
    if np.all(np.abs(amount * 100 - cents) < 1e-6):
        return cents.astype(np.int64), 100.0
    return amount.astype(np.longdouble), 1.0


def _distinct_sources(step, dst_code, pair_code, n_dst) -> np.ndarray:
    """Orígenes distintos que enviaron al destino de cada fila en [s-24, s-1]."""
    # Pares compactos (< nº de filas) para que `par * _M + paso` quepa en int64.
    pair_ids, pair_values = pd.factorize(pair_code)
    comb = np.unique(pair_ids.astype(np.int64) * _M + step)  # eventos únicos (par, paso), ordenados
    pair = comb // _M
    ev_step = comb % _M
    same = np.zeros(comb.size, dtype=bool)
    same[1:] = pair[1:] == pair[:-1]
    prev = np.where(same, np.roll(ev_step, 1), -(10**9))
    # Un evento en e cubre las consultas e+1..e+24; el tramo nuevo empieza tras la cobertura previa.
    start = np.maximum(ev_step + 1, prev + HOURS_PER_DAY + 1)
    end = ev_step + HOURS_PER_DAY
    dst = np.asarray(pair_values)[pair] % n_dst
    diff_key = np.concatenate((dst, dst))
    diff_step = np.concatenate((start, end + 1))
    diff_val = np.concatenate((np.ones(dst.size, np.int64), -np.ones(dst.size, np.int64)))
    return _cum_lookup(diff_key, diff_step, diff_val, dst_code, step)


def _first_step(step: np.ndarray, code: np.ndarray) -> np.ndarray:
    return pd.Series(step).groupby(code).transform("min").to_numpy()


# --- features --------------------------------------------------------------------------------------


@dataclass
class FeatureTable:
    frame: pd.DataFrame
    dropped_by_type: dict[str, int]
    feature_columns: list[str]


def _history_features(step, amount, orig_code, dst_code) -> dict[str, np.ndarray]:
    values, scale = _amount_values(amount)
    n_dst = int(dst_code.max()) + 1 if dst_code.size else 1
    pair_code = orig_code.astype(np.int64) * n_dst + dst_code
    ones = np.ones(step.size, dtype=np.int64)

    def window_sum(key, width, val):
        return _window(key, step, val, key, step, width)

    dst_sum = window_sum(dst_code, HOURS_PER_DAY, values)
    src_sum = window_sum(orig_code, HOURS_PER_DAY, values)
    dst_first = _first_step(step, dst_code)
    pair_first = _first_step(step, pair_code)
    seen = dst_first < step
    return {
        "dst_in_count_24h": window_sum(dst_code, HOURS_PER_DAY, ones),
        "dst_in_sum_24h": dst_sum.astype(np.float64) / scale,
        "dst_distinct_src_24h": _distinct_sources(step, dst_code, pair_code, n_dst),
        "dst_seen_before": seen,
        "dst_age_hours": np.where(seen, step - dst_first, 0),
        "src_out_count_1h": window_sum(orig_code, 1, ones),
        "src_out_count_24h": window_sum(orig_code, HOURS_PER_DAY, ones),
        "src_out_sum_24h": src_sum.astype(np.float64) / scale,
        "is_new_beneficiary": pair_first >= step,
    }


def build_table(raw: pd.DataFrame, feature_set: str, config: dict, contract: dict | None = None):
    """Matriz de features y columnas de análisis para el conjunto A o B.

    `raw` tiene las columnas de PaySim (ver RAW_USECOLS); `row_id` es la posición de cada fila en `raw`.
    """
    contract = contract or load_contract()
    columns = feature_columns(feature_set, contract)
    check_matrix_columns(columns, feature_set, contract)
    types = list(config["types"])
    validate_split(config["split"])

    keep = raw["type"].isin(types).to_numpy()
    dropped = raw.loc[~keep, "type"].value_counts().to_dict()
    d = raw.loc[keep]
    step = d["step"].to_numpy(np.int64)
    _check_steps(step)
    amount = d["amount"].to_numpy(np.float64)
    orig_code = pd.factorize(d["nameOrig"])[0].astype(np.int64)
    dst_code = pd.factorize(d["nameDest"])[0].astype(np.int64)

    feats: dict[str, np.ndarray] = {"amount": amount, "log_amount": np.log1p(amount)}
    for category in next(f for f in contract["features"] if f["name"] == "type")["categories"]:
        feats[f"type_{category}"] = (d["type"].to_numpy() == category).astype(np.float64)
    angle = 2 * np.pi * ((step - 1) % HOURS_PER_DAY) / HOURS_PER_DAY
    feats["hour_sin"] = np.sin(angle)
    feats["hour_cos"] = np.cos(angle)
    feats.update(_history_features(step, amount, orig_code, dst_code))
    if feature_set == "B":
        balance = d["oldbalanceOrg"].to_numpy(np.float64)
        feats["src_balance_before"] = balance
        # Pendiente de contrato: con saldo 0 el cociente no existe; se usa 0.0.
        feats["amount_to_balance"] = np.divide(
            amount, balance, out=np.zeros_like(amount), where=balance > 0
        )

    matrix = pd.DataFrame({name: np.asarray(feats[name], dtype=np.float64) for name in columns})
    check_matrix_columns(matrix.columns, feature_set, contract)
    meta = pd.DataFrame(
        {
            "row_id": np.flatnonzero(keep),
            "step": step,
            "type": d["type"].astype(str).to_numpy(),
            "is_fraud": d["isFraud"].to_numpy().astype(np.int8),
            "partition": assign_partition(step, config["split"]),
            "src_id": d["nameOrig"].astype(str).to_numpy(),
            "dst_id": d["nameDest"].astype(str).to_numpy(),
        }
    )
    return FeatureTable(pd.concat([meta, matrix], axis=1), dropped, columns)


def read_raw(path: Path, feature_set: str) -> pd.DataFrame:
    """Lee solo las columnas necesarias: las prohibidas nunca se cargan."""
    return pd.read_csv(path, usecols=RAW_USECOLS[feature_set])


def features_command(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m verabank_ml features")
    parser.add_argument("--set", dest="feature_set", required=True, choices=FEATURE_SETS)
    parser.add_argument("--raw", type=Path, default=RAW_PATH)
    parser.add_argument("--out", type=Path, default=OUT_DIR)
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    args = parser.parse_args(argv)

    started = time.perf_counter()
    raw = read_raw(args.raw, args.feature_set)
    table = build_table(raw, args.feature_set, load_config(args.config))
    args.out.mkdir(parents=True, exist_ok=True)
    target = args.out / f"features_{args.feature_set}.parquet"
    table.frame.to_parquet(target, index=False)
    elapsed = time.perf_counter() - started
    peak_gb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024 / 1024  # KiB en Linux
    frame = table.frame
    print(
        f"conjunto {args.feature_set}: {len(frame):,} filas, {len(table.feature_columns)} features -> {target}"
    )
    print(f"filas descartadas por tipo: {table.dropped_by_type}")
    print(frame.groupby("partition")["is_fraud"].agg(filas="size", fraudes="sum").to_string())
    print(f"tiempo {elapsed:.0f} s, memoria máxima {peak_gb:.1f} GB", file=sys.stderr)
    return 0
