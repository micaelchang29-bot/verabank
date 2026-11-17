"""Estado final de las transferencias enviadas, leído de ``core.transfers`` con el rol de transfer-service.

La API de lectura (``GET /transfers/{id}``) no devuelve ``decision_source`` ni ``scoring_latency_ms``, y el rol de BI no
lee ``core``; por eso el resumen consulta con el rol de transfer-service, solo con ``SELECT``.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass

NON_FINAL = frozenset({"PENDING", "PROCESSING"})
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
CHUNK = 2000


@dataclass(frozen=True)
class FinalRow:
    idempotency_key: str
    transfer_id: str
    status: str
    fraud_decision: str
    decision_source: str
    scoring_latency_ms: int | None
    failure_reason: str


def _chunks(items: Sequence[str], size: int) -> Iterable[Sequence[str]]:
    for i in range(0, len(items), size):
        yield items[i:i + size]


def fetch(psql, client_id: str, keys: Sequence[str]) -> dict[str, FinalRow]:
    """Filas de ``core.transfers`` de ese ``client_id`` para esas claves de idempotencia, por lotes."""
    bad = [k for k in keys if not UUID_RE.match(k)]
    if bad or "'" in client_id:
        raise ValueError("claves de idempotencia o client_id inválidos")
    out: dict[str, FinalRow] = {}
    for chunk in _chunks(list(keys), CHUNK):
        array = ",".join(f"'{k}'" for k in chunk)
        sql = (
            "SELECT idempotency_key, transfer_id, status, coalesce(fraud_decision, ''), coalesce(decision_source, ''), "
            "coalesce(scoring_latency_ms::text, ''), coalesce(failure_reason, '') FROM core.transfers "
            f"WHERE client_id = '{client_id}' AND idempotency_key = ANY(ARRAY[{array}]::uuid[]);"
        )
        for key, tid, status, decision, source, latency, reason in psql.query(sql):
            out[key] = FinalRow(key, tid, status, decision, source, int(latency) if latency else None, reason)
    return out


def wait_for_final(psql, client_id: str, keys: Sequence[str], *, timeout_s: float = 60.0, poll_s: float = 2.0,
                   clock: Callable[[], float] = time.monotonic,
                   sleep: Callable[[float], None] = time.sleep) -> dict[str, FinalRow]:
    """Espera hasta ``timeout_s`` a que ninguna transferencia siga PENDING o PROCESSING; devuelve lo último leído."""
    deadline = clock() + timeout_s
    result = fetch(psql, client_id, keys)
    while True:
        pending = [k for k, r in result.items() if r.status in NON_FINAL]
        if not pending or clock() >= deadline:
            return result
        sleep(min(poll_s, max(0.0, deadline - clock())))
        result.update(fetch(psql, client_id, pending))
