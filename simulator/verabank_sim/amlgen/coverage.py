"""Estimación independiente de qué instancias dispararían las reglas de §5a.

Es una implementación mínima y separada de la del servicio: solo lee las transferencias ya generadas (tal
como las vería el sistema) y la verdad de terreno. Una instancia cuenta como cubierta si alguna alerta de la
regla incluye al menos una de sus transferencias.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any


def _prep(transfers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{**t, "_t": datetime.fromisoformat(t["occurred_at"]), "_a": Decimal(t["amount"])} for t in transfers]


def _structuring(rows: list[dict[str, Any]], threshold: Decimal, r: dict[str, Any]) -> set[str]:
    lo, hi = (threshold * Decimal(str(x)) for x in r["pct_range"])
    window = timedelta(hours=r["window_hours"])
    by_src: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for t in rows:
        if lo <= t["_a"] <= hi:
            by_src[t["source_account_id"]].append(t)
    hit: set[str] = set()
    for items in by_src.values():
        items.sort(key=lambda t: t["_t"])
        for j, end in enumerate(items):
            win = [t for t in items[: j + 1] if end["_t"] - t["_t"] <= window]
            if len(win) >= r["min_count"] and sum(t["_a"] for t in win) >= Decimal(str(r["min_sum"])):
                hit.update(t["transfer_id"] for t in win)
    return hit


def _country(rows: list[dict[str, Any]], countries: dict[str, list[str]], r: dict[str, Any]) -> set[str]:
    high, watch = set(countries["high"]), set(countries["watch"])
    return {
        t["transfer_id"] for t in rows
        if (t["dest_country"] in high and t["_a"] >= Decimal(str(r["high_min_amount"])))
        or (t["dest_country"] in watch and t["_a"] >= Decimal(str(r["watch_min_amount"])))
    }


def _fan_in(rows: list[dict[str, Any]], r: dict[str, Any]) -> set[str]:
    window = timedelta(hours=r["window_hours"])
    by_dst: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for t in rows:
        if t.get("dest_account_id"):
            by_dst[t["dest_account_id"]].append(t)
    hit: set[str] = set()
    for items in by_dst.values():
        if len(items) < r["min_senders"]:
            continue
        items.sort(key=lambda t: t["_t"])
        for j, end in enumerate(items):
            win = [t for t in items[: j + 1] if end["_t"] - t["_t"] <= window]
            senders = {t["source_account_id"] for t in win}
            if len(senders) >= r["min_senders"] and sum(t["_a"] for t in win) >= Decimal(str(r["min_sum"])):
                hit.update(t["transfer_id"] for t in win)
    return hit


def rule_coverage(cfg: dict[str, Any], transfers: list[dict[str, Any]], truth: list[dict[str, Any]]) -> dict[str, Any]:
    rows = _prep(transfers)
    rules = cfg["rules"]
    alerted = {
        "STRUCTURING": _structuring(rows, Decimal(str(cfg["threshold"])), rules["STRUCTURING"]),
        "COUNTRY_RISK": _country(rows, cfg["risk_countries"], rules["COUNTRY_RISK"]),
        "FAN_IN": _fan_in(rows, rules["FAN_IN"]),
    }
    notes = {
        "FAN_IN": "fuera de alcance del corte actual; se calcula igual",
        "FAST_PASS": "sin regla en la tabla de §5a: tipología sin cobertura",
    }
    out: dict[str, Any] = {}
    for typ in ("STRUCTURING", "COUNTRY_RISK", "FAN_IN", "FAST_PASS"):
        inst = [g for g in truth if g["typology"] == typ]
        hits = alerted.get(typ, set())
        covered = sum(1 for g in inst if hits.intersection(g["transfer_ids"]))
        entry: dict[str, Any] = {
            "rule": typ if typ in alerted else None,
            "in_scope": typ != "FAN_IN" and typ in alerted,
            "instances": len(inst),
            "covered": covered,
            "pct": round(100.0 * covered / len(inst), 2) if inst else 0.0,
        }
        if typ in notes:
            entry["note"] = notes[typ]
        out[typ] = entry
    return out
