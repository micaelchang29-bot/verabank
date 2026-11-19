"""Calentamiento dirigido de historia (ADR-13, reabre 4/D1).

Los 24 pasos de calentamiento cubren las ventanas de 1 h y 24 h, pero ``dst_seen_before`` y ``dst_age_hours`` dependen de
la **primera** aparición del destino en toda la historia. Para cada destino ``PS-<nombre>`` del lote que ya apareció en
PaySim antes de ``primer_paso - 24`` (solo los tipos de ``config.yaml``, sin ``CASH_IN``) se carga UNA fila en
``risk.scoring_log``, en el paso de su primera aparición: ``model_version = 'warmup'``, ``features = '{}'``, ``score = 0``,
``decision = 'APPROVE'`` y el mismo mapeo de ``source_ref`` y ``dest_ref`` (D10). Reproduce ambas features sin cargar los
millones de filas de PaySim y, al quedar fuera de las ventanas de 24 h, no altera las demás. ``transfer_id`` es un
``uuid5`` del destino con un espacio de nombres propio, y la carga es idempotente. Solo se cargan destinos sin una fila
igual o anterior en ``scoring_log``, de modo que las tandas encadenadas no duplican.
"""

from __future__ import annotations

import csv
import re
import uuid
from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from verabank_sim.replay import mapping as m, warmup

HISTORY_NS = uuid.UUID("7d4f0a52-3b1e-5e8c-9a26-c41b8f6d0e37")   # espacio de nombres propio de las filas de historia
SAFE_REF = re.compile(r"^PS-[A-Za-z0-9_.-]+$")
LOOKUP_BATCH = 5000


def history_id(name_dest: str) -> str:
    return str(uuid.uuid5(HISTORY_NS, f"history:{name_dest}"))


def history_record(row: m.Row, epoch: datetime) -> list[str]:
    """Una fila de ``scoring_log``: como la de calentamiento, pero con el ``transfer_id`` de historia del destino."""
    record = warmup.warmup_record(row, epoch)
    record[0] = history_id(row.name_dest)
    return record


def first_appearances(path: Path, *, types: Collection[str], dests: Collection[str], before_step: int,
                      factor: Decimal = Decimal(1)) -> list[m.Row]:
    """Primera aparición en PaySim de cada destino de ``dests`` en un paso ``< before_step``, por ``(step, row_id)``.

    Cuenta cualquier fila de los tipos permitidos (como el entrenamiento, D6 de la fase 3), con o sin monto positivo.
    """
    allowed, wanted = set(types), set(dests)
    found: dict[str, m.Row] = {}
    with path.open(newline="", encoding="utf-8") as handle:
        for row_id, rec in enumerate(csv.DictReader(handle)):
            step = int(rec["step"])
            if step >= before_step:
                break
            name = rec["nameDest"]
            if rec["type"] not in allowed or name not in wanted or name in found:
                continue
            found[name] = m.Row(row_id, step, rec["type"], m.scale_amount(rec["amount"], factor), rec["nameOrig"],
                                Decimal(rec["oldbalanceOrg"]), name, rec["isFraud"] == "1")
    return sorted(found.values(), key=lambda r: (r.step, r.row_id))


@dataclass(frozen=True)
class Plan:
    batch_dests: int          # destinos distintos del lote
    with_history: int         # los que ya aparecieron en PaySim antes de primer_paso - 24
    already_loaded: int       # de esos, los que ya tienen en scoring_log una fila igual o anterior
    rows: list[m.Row]         # filas de historia a cargar

    def line(self, anchor: int, warmup_steps: int = 24) -> str:
        return (f"historia: {self.batch_dests} destinos en el lote; {self.with_history} con historia previa en PaySim "
                f"(antes del paso {anchor - warmup_steps}); {self.already_loaded} ya cargados; "
                f"{len(self.rows)} filas de historia a cargar")


def earliest_loaded(reader, names: Sequence[str]) -> dict[str, str]:
    """Primera ``occurred_at`` (``YYYY-MM-DDTHH:MM:SSZ``, UTC) por destino ya presente en ``scoring_log``."""
    out: dict[str, str] = {}
    for i in range(0, len(names), LOOKUP_BATCH):
        chunk = names[i:i + LOOKUP_BATCH]
        if not all(SAFE_REF.match(n) for n in chunk):
            raise ValueError("referencia de destino con caracteres no admitidos")
        listing = ", ".join(f"'{n}'" for n in chunk)
        sql = ("SELECT dest_ref, to_char(min(occurred_at) AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"') "
               f"FROM risk.scoring_log WHERE dest_ref = ANY(ARRAY[{listing}]) GROUP BY 1;")
        out.update({r[0]: r[1] for r in reader.query(sql)})
    return out


def plan(candidates: Iterable[m.Row], batch_rows: Iterable[m.Row], epoch: datetime, reader) -> Plan:
    """Qué filas de historia faltan: las de ``candidates`` cuyo destino no tiene ya una fila igual o anterior."""
    candidates = list(candidates)
    batch_dests = len({r.name_dest for r in batch_rows})
    loaded = earliest_loaded(reader, [m.dest_ref(r) for r in candidates]) if reader is not None and candidates else {}
    todo = [r for r in candidates
            if m.dest_ref(r) not in loaded or loaded[m.dest_ref(r)] > m.occurred_at(epoch, r.step)]
    return Plan(batch_dests, len(candidates), len(candidates) - len(todo), todo)


def load(psql, rows: Sequence[m.Row], epoch: datetime) -> tuple[int, str]:
    """``COPY`` con el rol ``sim_loader``; si choca con filas ya cargadas, ``INSERT ... ON CONFLICT DO NOTHING``."""
    return warmup.load(psql, rows, epoch, record=history_record)
