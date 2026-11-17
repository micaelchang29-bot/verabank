"""Casos golden (T-13): selección estratificada y construcción de `golden.jsonl` desde `valid`.

Cada caso lleva la solicitud, su historia (todas las filas anteriores del mismo origen o del mismo destino,
de los tipos permitidos) y las features y el score esperados, para que otra implementación recalcule las
features desde `history`. Los nombres de cuenta de PaySim se sustituyen por alias locales (`a1`, `a2`, …).
"""

from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
from scipy.stats import chi2_contingency

from verabank_ml.features import _cum_lookup

DECISIONS = ("APPROVE", "REVIEW", "BLOCK")


def history_counts(step, orig_code, dst_code) -> np.ndarray:
    """Filas anteriores (step estrictamente menor) del mismo origen o del mismo destino, para cada fila."""
    ones = np.ones(len(step), dtype=np.int64)
    q = step - 1
    pair_ids = pd.factorize(orig_code.astype(np.int64) * (int(dst_code.max()) + 1) + dst_code)[0]
    by_src = _cum_lookup(orig_code, step, ones, orig_code, q)
    by_dst = _cum_lookup(dst_code, step, ones, dst_code, q)
    by_both = _cum_lookup(pair_ids, step, ones, pair_ids, q)
    return by_src + by_dst - by_both  # las filas del mismo par (origen y destino) cuentan una vez


def decide_score(score: float, t_review: float, t_block: float) -> str:
    """Regla de servicio: `round(score, 5) >= umbral` (BLOCK, REVIEW o APPROVE)."""
    rounded = round(score, 5)
    if rounded >= t_block:
        return "BLOCK"
    return "REVIEW" if rounded >= t_review else "APPROVE"


def _allocate(counts: dict[str, int], total: int) -> dict[str, int]:
    """Reparto proporcional por tipo con el método del mayor resto."""
    n = sum(counts.values())
    exact = {k: total * v / n for k, v in counts.items()}
    alloc = {k: int(v) for k, v in exact.items()}
    for key in sorted(exact, key=lambda k: (-(exact[k] - alloc[k]), k))[
        : total - sum(alloc.values())
    ]:
        alloc[key] += 1
    return alloc


def choose_cases(valid: pd.DataFrame, thresholds: dict, gcfg: dict, seed: int) -> pd.DataFrame:
    """Elige los casos de `valid` (columnas row_id, type, is_fraud, score, n_history) y marca su estrato.

    Orden de elección: boundary (hasta 4 · boundary_per_side casos), fraud, above_review y el resto legítimas estratificadas por tipo.
    """
    rng = np.random.default_rng(seed)
    pool = valid[valid["n_history"] <= gcfg["max_history"]].reset_index(drop=True)
    pool["rounded"] = np.round(pool["score"], 5)
    t_review, t_block = thresholds["t_review"], thresholds["t_block"]
    chosen: dict[int, str] = {}

    # boundary: por umbral y lado (según el score redondeado), los `boundary_per_side` scores distintos más
    # cercanos al umbral; un caso por score (los empates se sortean con la semilla)
    boundary_of: dict[int, str] = {}
    for name, threshold in (("t_review", t_review), ("t_block", t_block)):
        for above in (False, True):
            side = pool[(pool["rounded"] >= threshold) == above]
            side = side[~side.index.isin(chosen)]
            order = np.lexsort((rng.random(len(side)), (side["score"] - threshold).abs().to_numpy()))
            seen: set[float] = set()
            for idx in side.index[order]:
                score = float(side.at[idx, "score"])
                if score in seen:
                    continue
                seen.add(score)
                chosen[idx] = "boundary"
                boundary_of[idx] = name
                if len(seen) == gcfg["boundary_per_side"]:
                    break

    def draw(mask, n, stratum):
        candidates = pool.index[mask & ~pool.index.isin(chosen)]
        if len(candidates) < n:
            raise ValueError(f"estrato {stratum}: {len(candidates)} candidatos para {n} casos")
        for idx in rng.choice(candidates, size=n, replace=False):
            chosen[int(idx)] = stratum

    draw((pool["is_fraud"] == 1).to_numpy(), gcfg["fraud"], "fraud")
    draw(
        ((pool["is_fraud"] == 0) & (pool["rounded"] >= t_review)).to_numpy(),
        gcfg["above_review"],
        "above_review",
    )
    remaining = gcfg["total"] - len(chosen)
    legit = pool[(pool["is_fraud"] == 0) & ~pool.index.isin(chosen)]
    for type_, n in _allocate(legit["type"].value_counts().to_dict(), remaining).items():
        draw(((pool["is_fraud"] == 0) & (pool["type"] == type_)).to_numpy(), n, "random")
    selected = pool.loc[list(chosen)].copy()
    selected["stratum"] = [chosen[i] for i in selected.index]
    selected["boundary_of"] = [boundary_of.get(i) for i in selected.index]
    return selected.sort_values("row_id").reset_index(drop=True)


def exclusion_report(valid: pd.DataFrame, max_history: int) -> dict:
    """Cuántos candidatos se excluyen por historia larga y si eso sesga la muestra por tipo o clase."""
    kept = valid["n_history"] <= max_history
    report = {
        "max_history": max_history,
        "n_candidates": len(valid),
        "n_excluded": int((~kept).sum()),
        "history_length_quantiles": {
            str(q): float(valid["n_history"].quantile(q)) for q in (0.5, 0.9, 0.99, 1.0)
        },
    }
    for name, column in (
        ("by_type", valid["type"]),
        ("by_class", valid["is_fraud"].map({0: "legitimate", 1: "fraud"})),
    ):
        table = pd.crosstab(column, kept).reindex(columns=[True, False], fill_value=0)
        rows = {}
        for key, row in table.iterrows():
            total = int(row.sum())
            rows[str(key)] = {
                "candidates": total,
                "excluded": int(row[False]),
                "excluded_share": float(row[False] / total) if total else None,
            }
        p_value = (
            float(chi2_contingency(table.to_numpy())[1])
            if table.shape[0] > 1
            and (table.sum(axis=1) > 0).all()
            and table.to_numpy().sum(axis=0).min() > 0
            else None
        )
        report[name] = {
            "rows": rows,
            "chi2_p_value": p_value,
            "biased_at_5_percent": bool(p_value is not None and p_value < 0.05),
        }
    return report


def occurred_at(step: int, epoch: datetime) -> str:
    return (epoch + timedelta(hours=int(step) - 1)).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_epoch(text: str) -> datetime:
    return datetime.fromisoformat(text).astimezone(UTC)


def build_case(
    case_id, request_row, history_rows, features: dict, score, decision, stratum, epoch
) -> dict:
    """Un caso: nombres de cuenta sustituidos por alias que conservan las igualdades dentro del caso."""
    aliases: dict[str, str] = {}

    def alias(name: str) -> str:
        return aliases.setdefault(name, f"a{len(aliases) + 1}")

    def row(r) -> dict:
        return {
            "occurred_at": occurred_at(r["step"], epoch),
            "type": str(r["type"]),
            "amount": float(r["amount"]),
            "source": alias(r["src_id"]),
            "dest": alias(r["dst_id"]),
        }

    request = row(request_row)
    history = [row(r) for r in history_rows]
    return {
        "case_id": case_id,
        "stratum": stratum,
        "request": request,
        "history": history,
        "features": {k: float(v) for k, v in features.items()},
        "score": float(score),
        "decision": decision,
    }


def case_rows(case: dict, epoch: datetime) -> tuple[list[dict], int]:
    """Filas (history + solicitud, esta al final) con la forma que consume `reference.reference_features`."""
    rows = []
    for r in [*case["history"], case["request"]]:
        step = int((parse_epoch(r["occurred_at"]) - epoch).total_seconds() // 3600) + 1
        rows.append(
            {
                "step": step,
                "type": r["type"],
                "amount": r["amount"],
                "nameOrig": r["source"],
                "nameDest": r["dest"],
            }
        )
    return rows, len(rows) - 1
