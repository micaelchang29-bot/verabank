"""Calentamiento de ``risk.scoring_log`` (D1): las filas de los 24 pasos previos, con el rol ``sim_loader`` y ``COPY``.

``model_version = 'warmup'``, ``features = '{}'``, ``score = 0`` y ``decision = 'APPROVE'``. Con ellas las ventanas
de 1 h y 24 h quedan correctas; ``dst_seen_before``, ``dst_age_hours`` e ``is_new_beneficiary`` verán menos historia
que en el entrenamiento (desajuste aceptado y documentado en simulator/README.md).
"""

from __future__ import annotations

import csv
import io
import uuid
from collections.abc import Sequence
from datetime import datetime, timedelta

from verabank_sim.replay import mapping as m

COLUMNS = "transfer_id, source_ref, dest_ref, amount, occurred_at, features, score, decision, model_version"
COPY_SQL = f"COPY risk.scoring_log ({COLUMNS}) FROM STDIN WITH (FORMAT csv)"
INSERT_BATCH = 2000


def warmup_id(row: m.Row) -> str:
    return str(uuid.uuid5(m.NS, f"warmup:{row.row_id}:{m.amount_text(row.amount)}"))


def warmup_record(row: m.Row, epoch: datetime) -> list[str]:
    return [warmup_id(row), m.account_id(row.name_orig), m.dest_ref(row), m.amount_text(row.amount),
            m.occurred_at(epoch, row.step), "{}", "0", "APPROVE", "warmup"]


def copy_data(rows: Sequence[m.Row], epoch: datetime) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    for row in rows:
        writer.writerow(warmup_record(row, epoch))
    return buffer.getvalue()


def insert_sql(rows: Sequence[m.Row], epoch: datetime) -> str:
    """Alternativa con ``ON CONFLICT DO NOTHING`` para cuando un ``COPY`` choca con filas ya cargadas.

    Sin destino de conflicto a propósito: ``ON CONFLICT (transfer_id)`` exige ``SELECT`` sobre la tabla y ``sim_loader``
    solo tiene ``INSERT`` (db/init/grants.sql).
    """
    values = ",\n".join(
        "(" + ", ".join(f"'{v}'" for v in warmup_record(r, epoch)[:5]) + ", '{}'::jsonb, 0, 'APPROVE', 'warmup')"
        for r in rows)
    return (f"INSERT INTO risk.scoring_log ({COLUMNS})\nVALUES\n{values}\nON CONFLICT DO NOTHING;\n")


def real_rows_sql(epoch: datetime, first_step: int, steps: int = 24) -> str:
    """Filas reales (no ``warmup``) de ``risk.scoring_log`` en los ``steps`` pasos previos a ``first_step``."""
    start = epoch + timedelta(hours=first_step - 1 - steps)
    end = epoch + timedelta(hours=first_step - 1)
    return ("SELECT count(*) FROM risk.scoring_log WHERE model_version <> 'warmup' "
            f"AND occurred_at >= '{start.isoformat()}' AND occurred_at < '{end.isoformat()}';")


def real_rows_before(psql, epoch: datetime, first_step: int, steps: int = 24) -> int:
    """Lo lee el rol de solo lectura de BI: ``sim_loader`` no tiene ``SELECT``."""
    return int(psql.query(real_rows_sql(epoch, first_step, steps))[0][0])


def load(psql, rows: Sequence[m.Row], epoch: datetime) -> tuple[int, str]:
    """Carga el calentamiento. Devuelve (filas, método): ``copy`` o ``insert`` si hubo filas ya cargadas."""
    if not rows:
        return 0, "copy"
    try:
        psql.run(copy_data(rows, epoch), "-c", COPY_SQL)
        return len(rows), "copy"
    except Exception as exc:   # un reintento del mismo replay choca con la clave primaria
        if "duplicate key" not in str(exc):
            raise
    for i in range(0, len(rows), INSERT_BATCH):
        psql.run(insert_sql(rows[i:i + INSERT_BATCH], epoch), "-f", "-")
    return len(rows), "insert"
