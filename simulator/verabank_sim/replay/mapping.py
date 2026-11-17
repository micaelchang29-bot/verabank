"""Mapeo de una fila de PaySim a una solicitud de transfer-service (D10) y a una fila de calentamiento (D1).

Todo es determinista: una cuenta por nombre de PaySim (``uuid5``), cliente por hash entre 30.000 identificadores
sintéticos, destino siempre ``EXTERNAL`` con ``dest_external_ref = "PS-<nombre>"``, y el tiempo de negocio
``sim_epoch + (step - 1) h``, igual para todas las filas del paso.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal

NS = uuid.UUID("2b0c3b8e-6a0d-5c1e-8f3a-9d1e4c7a5b21")   # espacio de nombres de uuid5 del replay
CUSTOMERS = 30_000
CURRENCY = "PEN"
DEST_COUNTRY = "PE"
CENT = Decimal("0.01")


@dataclass(frozen=True, slots=True)
class Row:
    """Una fila de PaySim: solo las columnas que el replay necesita."""

    row_id: int            # posición en el CSV, como ``row_id`` de ml/
    step: int
    type: str
    amount: Decimal        # ya con ``--amount-factor`` y a centavos
    name_orig: str
    old_balance_org: Decimal
    name_dest: str
    is_fraud: bool


def parse_epoch(text: str) -> datetime:
    return datetime.fromisoformat(text).astimezone(UTC)


def scale_amount(raw: str | Decimal, factor: Decimal) -> Decimal:
    """Monto de PaySim por el factor, a centavos (mitad al par). El factor se aplica antes de la clave de idempotencia."""
    return (Decimal(str(raw)) * factor).quantize(CENT, rounding=ROUND_HALF_EVEN)


def amount_text(amount: Decimal) -> str:
    return f"{amount:.2f}"


def account_id(name: str) -> str:
    return str(uuid.uuid5(NS, f"account:{name}"))


def customer_index(name: str) -> int:
    return int.from_bytes(hashlib.sha256(name.encode()).digest()[:8], "big") % CUSTOMERS


def customer_id(index: int) -> str:
    return str(uuid.uuid5(NS, f"customer:{index}"))


def segment(index: int) -> str:
    """Mismo reparto que db/seed: 18 RETAIL, 8 PREMIUM y 4 PYME de cada 30."""
    slot = index % 30
    return "RETAIL" if slot < 18 else "PREMIUM" if slot < 26 else "PYME"


def occurred_at(epoch: datetime, step: int) -> str:
    """``sim_epoch + (step - 1) h``, alineado a la hora (ml/README.md, contrato de tiempo)."""
    return (epoch + timedelta(hours=step - 1)).strftime("%Y-%m-%dT%H:%M:%SZ")


def idempotency_key(row: Row) -> str:
    """Determinista por fila y monto final: repetir el replay devuelve 200 y otro factor no choca con el original."""
    return str(uuid.uuid5(NS, f"replay:{row.row_id}:{amount_text(row.amount)}"))


def dest_ref(row: Row) -> str:
    return f"PS-{row.name_dest}"


def transfer_body(row: Row, epoch: datetime) -> dict[str, str]:
    return {
        "source_account_id": account_id(row.name_orig),
        "dest_external_ref": dest_ref(row),
        "dest_country": DEST_COUNTRY,
        "rail": "EXTERNAL",
        "transfer_type": row.type,
        "amount": amount_text(row.amount),
        "currency": CURRENCY,
        "occurred_at": occurred_at(epoch, row.step),
    }
