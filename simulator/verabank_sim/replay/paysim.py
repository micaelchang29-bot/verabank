"""Lectura de PaySim para el replay: partición de prueba, tipos de ``ml/config.yaml`` y calentamiento de 24 pasos."""

from __future__ import annotations

import csv
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from verabank_sim.replay.mapping import Row, scale_amount

WARMUP_STEPS = 24


@dataclass(frozen=True)
class Selection:
    rows: list[Row]               # las N filas del replay, por (step, row_id)
    warmup: list[Row]             # filas de los 24 pasos previos al primero
    start_step: int | None
    skipped_zero_amount: int      # filas de la partición cuyo monto, tras el factor, no es positivo


def select_rows(path: Path, *, types: Sequence[str], test_steps: tuple[int, int], n: int, start_row: int = 0,
                factor: Decimal = Decimal(1), warmup_steps: int = WARMUP_STEPS) -> Selection:
    """Las ``n`` primeras filas de la partición de prueba (desde ``start_row``) y su calentamiento.

    PaySim viene ordenado por ``step`` y ``row_id`` es la posición en el archivo, así que el orden
    ``(step, row_id)`` es el de lectura (se comprueba). ``CASH_IN`` y cualquier tipo fuera de ``types`` quedan fuera.
    Las filas con monto no positivo no son transferencias válidas: no cuentan para ``n`` ni para ``start_row``.
    """
    lo, hi = test_steps
    allowed = set(types)
    rows: list[Row] = []
    window: deque[Row] = deque()
    warmup: list[Row] = []
    skipped = 0
    seen = 0
    last_step = -1
    start_step: int | None = None
    with path.open(newline="", encoding="utf-8") as handle:
        for row_id, rec in enumerate(csv.DictReader(handle)):
            step = int(rec["step"])
            if step < last_step:
                raise ValueError(f"{path}: el CSV no está ordenado por step (fila {row_id})")
            last_step = step
            if rec["type"] not in allowed:
                continue
            amount = scale_amount(rec["amount"], factor)
            in_test = lo <= step <= hi
            if amount <= 0:
                skipped += in_test
                continue
            row = Row(row_id, step, rec["type"], amount, rec["nameOrig"], Decimal(rec["oldbalanceOrg"]),
                      rec["nameDest"], rec["isFraud"] == "1")
            if in_test:
                if seen >= start_row:
                    if not rows:
                        start_step = step
                        warmup = [r for r in window if step - warmup_steps <= r.step < step]
                    rows.append(row)
                    if len(rows) == n:
                        break
                seen += 1
            window.append(row)
            while window and window[0].step < step - warmup_steps:
                window.popleft()
    return Selection(rows, warmup, start_step, skipped)
