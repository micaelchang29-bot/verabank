"""Subcomando ``aml-eval``: evalúa las alertas de ``risk.aml_alerts`` contra la verdad de terreno de ``amlgen``."""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from typing import Any

from verabank_sim.aml.cli import MAP_NAME
from verabank_sim.aml.evaluate import evaluate, render
from verabank_sim.load.db import REPO_ROOT, read_only_psql

log = logging.getLogger("verabank_sim.aml")
ALERTS_SQL = ("SELECT rule_code, array_to_string(transfer_ids, ',') FROM risk.aml_alerts "
              "ORDER BY created_at, alert_id;")


def add_arguments(parser: argparse.ArgumentParser) -> None:
    data = REPO_ROOT / "data" / "aml"
    parser.add_argument("--truth", type=Path, default=data / "ground_truth.jsonl", help="ground_truth.jsonl de amlgen")
    parser.add_argument("--manifest", type=Path, default=data / "manifest.json", help="manifest.json de amlgen")
    parser.add_argument("--run", type=Path, default=None,
                        help=f"mapa de la corrida de `aml` (junto a la verdad de terreno: {MAP_NAME})")
    parser.add_argument("--out", type=Path, default=REPO_ROOT / "docs" / "resultados" / "aml-eval.md", help="informe")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def read_alerts(psql: Any) -> list[dict[str, Any]]:
    """Las alertas con el rol de solo lectura (verabank_bi)."""
    return [{"rule_code": rule, "transfer_ids": [t for t in ids.split(",") if t]} for rule, ids in psql.query(ALERTS_SQL)]


def run(args: argparse.Namespace) -> int:
    run_map = read_jsonl(args.run or args.truth.parent / MAP_NAME)
    report = evaluate(read_jsonl(args.truth), json.loads(args.manifest.read_text(encoding="utf-8")), run_map,
                      read_alerts(read_only_psql()))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(render(report), encoding="utf-8")
    log.info("informe en %s (%d alertas tocan data/aml)", args.out, report["alerts_touching_dataset"])
    return 0
