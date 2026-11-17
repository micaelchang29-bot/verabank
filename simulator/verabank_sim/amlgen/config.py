"""Carga de ``amlgen.yaml`` con la lista real de países de riesgo de ``db/seed/country_risk.csv``.

Los países de nivel alto salen del CSV de la lista del GAFI/FATF (la misma que carga ``make load-country-risk``),
no de códigos ilustrativos. El resto de la configuración se usa tal cual.
"""

from __future__ import annotations

import csv
import hashlib
import re
from pathlib import Path
from typing import Any

import yaml

ALPHA_2 = re.compile(r"^[A-Z]{2}$")


def read_risk_countries(csv_path: Path, level: str) -> tuple[list[str], str, str]:
    """(códigos del nivel pedido en el orden del CSV, ``list_date``, SHA-256 del CSV)."""
    raw = csv_path.read_bytes()
    rows = list(csv.DictReader(raw.decode("utf-8").splitlines()))
    dates = {r["list_date"] for r in rows}
    if len(dates) != 1:
        raise ValueError(f"{csv_path}: la lista mezcla fechas distintas: {sorted(dates)}")
    codes = [r["country"] for r in rows if r["level"] == level]
    bad = [c for c in codes if not ALPHA_2.match(c)]
    if bad or len(codes) != len(set(codes)) or not codes:
        raise ValueError(f"{csv_path}: códigos de nivel {level} inválidos, repetidos o ausentes: {bad or codes}")
    return codes, dates.pop(), hashlib.sha256(raw).hexdigest()


def load_config(config_path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """(configuración con ``risk_countries.high`` resuelto, procedencia de la lista para el manifiesto)."""
    cfg = yaml.safe_load(config_path.read_bytes())
    risk = cfg["risk_countries"]
    csv_path = (config_path.parent / risk["high_from"]).resolve()
    high, list_date, digest = read_risk_countries(csv_path, risk.get("high_level", "HIGH"))
    risk["high"] = high
    provenance = {
        "source": risk["high_from"],
        "level": risk.get("high_level", "HIGH"),
        "list_date": list_date,
        "csv_sha256": digest,
        "high": high,
        "watch": list(risk["watch"]),
    }
    return cfg, provenance
