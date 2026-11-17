"""Subcomando ``amlgen``: transferencias AML sintéticas con verdad de terreno en archivo aparte."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
from pathlib import Path
from typing import Any

import yaml

from verabank_sim.amlgen.coverage import rule_coverage
from verabank_sim.amlgen.generate import TYPOLOGIES, generate

log = logging.getLogger("verabank_sim.amlgen")
DEFAULT_CONFIG = Path(__file__).resolve().parents[2] / "amlgen.yaml"


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--seed", type=int, default=42, help="semilla (misma semilla, mismos archivos)")
    parser.add_argument("--out", type=Path, default=Path("data/aml"), help="directorio de salida")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="parámetros (amlgen.yaml)")


def _dumps(obj: Any) -> bytes:
    return (json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":")) + "\n").encode()


def build(config_path: Path, seed: int, out: Path) -> dict[str, Any]:
    raw = config_path.read_bytes()
    cfg = yaml.safe_load(raw)
    gen = generate(cfg, seed)
    out.mkdir(parents=True, exist_ok=True)
    files = {
        "transfers.jsonl": b"".join(_dumps(t) for t in gen.transfers),
        "ground_truth.jsonl": b"".join(_dumps(g) for g in gen.truth),
    }
    for name, data in files.items():
        (out / name).write_bytes(data)
    in_typ = {tid for g in gen.truth for tid in g["transfer_ids"]}
    manifest = {
        "seed": seed,
        "config_sha256": hashlib.sha256(raw).hexdigest(),
        "sim_epoch": cfg["sim_epoch"],
        "days": cfg["days"],
        "accounts": cfg["accounts"],
        "transfers": {
            "total": len(gen.transfers),
            "legitimate": len(gen.transfers) - len(in_typ),
            "legitimate_near_threshold": gen.near_threshold_legit,
            "in_typologies": len(in_typ),
        },
        "instances": {typ: sum(1 for g in gen.truth if g["typology"] == typ) for typ in TYPOLOGIES},
        "sha256": {name: hashlib.sha256(data).hexdigest() for name, data in files.items()},
        "rule_coverage": rule_coverage(cfg, gen.transfers, gen.truth),
    }
    (out / "manifest.json").write_bytes(json.dumps(manifest, indent=2, sort_keys=True).encode() + b"\n")
    return manifest


def run(args: argparse.Namespace) -> int:
    manifest = build(args.config, args.seed, args.out)
    log.info("amlgen: %d transferencias en %s (semilla %d)", manifest["transfers"]["total"], args.out, args.seed)
    return 0
