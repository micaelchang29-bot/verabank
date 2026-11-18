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


def select_rows(path: Path, *, types: Sequence[str], test_steps: tuple[int, int], n: int | None = None,
                start_row: int = 0, factor: Decimal = Decimal(1), warmup_steps: int = WARMUP_STEPS,
                steps: int | None = None, first_step: int | None = None) -> Selection:
    """Las filas del replay y su calentamiento.

    Dos modos excluyentes: ``n`` (las ``n`` primeras filas de la partición de prueba desde ``start_row``) o ``steps``
    (todas las filas de ``steps`` pasos consecutivos de la partición desde ``first_step``). El calentamiento son las
    filas de los ``warmup_steps`` pasos previos al primero (en el modo por pasos, previos a ``first_step``).

    PaySim viene ordenado por ``step`` y ``row_id`` es la posición en el archivo, así que el orden
    ``(step, row_id)`` es el de lectura (se comprueba). ``CASH_IN`` y cualquier tipo fuera de ``types`` quedan fuera.
    Las filas con monto no positivo no son transferencias válidas: no cuentan para ``n`` ni para ``start_row``.
    """
    lo, hi = test_steps
    if (n is None) == (steps is None):
        raise ValueError("indica n o steps, no ambos")
    if steps is not None:
        if steps < 1 or start_row:
            raise ValueError("steps debe ser al menos 1 y no se combina con start_row")
        first_step = lo if first_step is None else first_step
        if not lo <= first_step <= hi:
            raise ValueError(f"first_step {first_step} fuera de la partición de prueba {lo}-{hi}")
        lo, hi = first_step, min(hi, first_step + steps - 1)
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
            if step > hi:
                break
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
                        anchor = step if steps is None else lo
                        warmup = [r for r in window if anchor - warmup_steps <= r.step < anchor]
                    rows.append(row)
                    if n is not None and len(rows) == n:
                        break
                seen += 1
            window.append(row)
            while window and window[0].step < step - warmup_steps:
                window.popleft()
    return Selection(rows, warmup, start_step, skipped)
