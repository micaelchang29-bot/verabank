"""``feature-parity``: compara las ``features`` guardadas por el servicio con las del entrenamiento (ADR-13, reabre 4/D1).

Toma una muestra determinista de filas del replay ya puntuadas en ``risk.scoring_log`` (rol de solo lectura de BI),
las empareja con su ``row_id`` de PaySim por ``(paso, cuenta de origen, destino, monto)`` y las compara con
``data/features/features_A.parquet`` por ``row_id``. Informa, por feature, el porcentaje de filas que difieren y la
magnitud de la diferencia. Solo tiene sentido con ``--amount-factor 1`` (con otro factor los montos no empatan y esas
filas se cuentan como sin pareja).
"""

from __future__ import annotations

import argparse
import csv
import json
import math
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import yaml

from verabank_sim.load.db import REPO_ROOT, read_only_psql
from verabank_sim.replay import mapping as m

SEP = "\x1f"
DEFAULT_TOLERANCE = 1e-9


@dataclass(frozen=True)
class Scored:
    transfer_id: str
    source_ref: str
    dest_ref: str
    amount: str
    step: int
    features: Mapping[str, float]


def sample_sql(n: int) -> str:
    """Muestra determinista (orden por hash del id) de filas reales con features, sin calentamiento ni filas de k6."""
    return ("SELECT transfer_id, source_ref, dest_ref, amount, to_char(occurred_at AT TIME ZONE 'UTC', "
            "'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"'), features::text FROM risk.scoring_log "
            "WHERE model_version <> 'warmup' AND source_ref NOT LIKE 'perf-%' AND features <> '{}'::jsonb "
            f"ORDER BY md5(transfer_id::text) LIMIT {int(n)};")


def sample(reader, n: int, epoch: datetime) -> list[Scored]:
    out = []
    for line in filter(None, reader.run(sample_sql(n), "-At", "-F", SEP, "-f", "-").splitlines()):
        tid, source, dest, amount, at, features = line.split(SEP)
        hours = (datetime.fromisoformat(at.replace("Z", "+00:00")) - epoch).total_seconds() / 3600
        out.append(Scored(tid, source, dest, f"{Decimal(amount):.2f}", int(hours) + 1, json.loads(features)))
    return out


def paysim_index(path: Path, *, types: Iterable[str], steps: set[int]) -> dict[tuple, list[int]]:
    """``(paso, cuenta de origen, destino, monto) -> [row_id]`` de las filas de esos pasos (como el replay las envía)."""
    allowed, hi = set(types), max(steps) if steps else 0
    index: dict[tuple, list[int]] = defaultdict(list)
    with path.open(newline="", encoding="utf-8") as handle:
        for row_id, rec in enumerate(csv.DictReader(handle)):
            step = int(rec["step"])
            if step > hi:
                break
            if step in steps and rec["type"] in allowed:
                amount = m.scale_amount(rec["amount"], Decimal(1))
                if amount > 0:
                    index[(step, m.account_id(rec["nameOrig"]), f"PS-{rec['nameDest']}", m.amount_text(amount))].append(row_id)
    return index


def match(scored: Sequence[Scored], index: Mapping[tuple, list[int]]) -> tuple[dict[int, Scored], int, int]:
    """(``row_id -> fila``, sin pareja, ambiguas). Una clave con varias filas de PaySim no se puede asignar y se omite."""
    found: dict[int, Scored] = {}
    unmatched = ambiguous = 0
    for s in scored:
        ids = index.get((s.step, s.source_ref, s.dest_ref, s.amount), [])
        if not ids:
            unmatched += 1
        elif len(ids) > 1:
            ambiguous += 1
        else:
            found[ids[0]] = s
    return found, unmatched, ambiguous


def read_training(path: Path, row_ids: Sequence[int], columns: Sequence[str]) -> dict[int, dict[str, float]]:
    import pyarrow.parquet as pq

    table = pq.read_table(path, columns=["row_id", *columns], filters=[("row_id", "in", list(row_ids))])
    data = table.to_pydict()
    return {rid: {c: data[c][i] for c in columns} for i, rid in enumerate(data["row_id"])}


@dataclass(frozen=True)
class FeatureDiff:
    feature: str
    compared: int
    differing: int
    max_abs: float
    mean_abs: float          # sobre las filas que difieren

    @property
    def percent(self) -> float:
        return 100.0 * self.differing / self.compared if self.compared else 0.0


def compare(found: Mapping[int, Scored], training: Mapping[int, Mapping[str, float]], names: Sequence[str],
            tolerance: float = DEFAULT_TOLERANCE) -> list[FeatureDiff]:
    out = []
    for name in names:
        diffs = []
        compared = 0
        for rid, s in found.items():
            if rid not in training or name not in s.features:
                continue
            compared += 1
            a, b = float(s.features[name]), float(training[rid][name])
            if abs(a - b) > tolerance + tolerance * abs(b):
                diffs.append(abs(a - b))
        out.append(FeatureDiff(name, compared, len(diffs), max(diffs, default=0.0),
                               math.fsum(diffs) / len(diffs) if diffs else 0.0))
    return out


def format_report(diffs: Sequence[FeatureDiff], *, sampled: int, matched: int, unmatched: int, ambiguous: int,
                  tolerance: float) -> str:
    head = (f"feature-parity: {sampled} filas muestreadas; {matched} con pareja en PaySim, {unmatched} sin pareja, "
            f"{ambiguous} ambiguas; tolerancia {tolerance:g}")
    lines = [head, f"{'feature':<22} {'filas':>7} {'difieren':>9} {'%':>8} {'dif. máx.':>14} {'dif. media':>14}"]
    lines += [f"{d.feature:<22} {d.compared:>7} {d.differing:>9} {d.percent:>8.3f} {d.max_abs:>14.6g} {d.mean_abs:>14.6g}"
              for d in diffs]
    return "\n".join(lines)


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--sample", type=int, default=5000, help="filas del replay a comparar (5000)")
    parser.add_argument("--tolerance", type=float, default=DEFAULT_TOLERANCE, help="tolerancia absoluta y relativa (1e-9)")
    parser.add_argument("--paysim", type=Path, default=REPO_ROOT / "data" / "raw" / "paysim.csv", help="CSV de PaySim")
    parser.add_argument("--features", type=Path, default=REPO_ROOT / "data" / "features" / "features_A.parquet",
                        help="features de entrenamiento (conjunto A)")
    parser.add_argument("--ml-config", type=Path, default=REPO_ROOT / "ml" / "config.yaml", help="ml/config.yaml")


def run(args: argparse.Namespace) -> int:
    cfg = yaml.safe_load(args.ml_config.read_text(encoding="utf-8"))
    epoch = m.parse_epoch(cfg["sim_epoch"])
    scored = sample(read_only_psql(), args.sample, epoch)
    if not scored:
        print("feature-parity: no hay filas puntuadas del replay en risk.scoring_log")
        return 2
    index = paysim_index(args.paysim, types=cfg["types"], steps={s.step for s in scored})
    found, unmatched, ambiguous = match(scored, index)
    if not found:
        print(f"feature-parity: ninguna de las {len(scored)} filas tiene pareja en PaySim (¿--amount-factor distinto de 1?)")
        return 2
    names = sorted({k for s in found.values() for k in s.features})
    training = read_training(args.features, sorted(found), names)
    print(format_report(compare(found, training, names, args.tolerance), sampled=len(scored), matched=len(found),
                        unmatched=unmatched, ambiguous=ambiguous, tolerance=args.tolerance))
    return 0
