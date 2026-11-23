"""Siembra de cuentas con saldo por asientos desde ``CLEARING``, sin escribir saldos de forma directa.

Es el mecanismo de ``db/seed/01_core_seed.sql`` pero por lotes y con ``INSERT ... SELECT`` sobre un ``VALUES``: cada
cuenta con saldo recibe una transferencia ``INTERNAL COMPLETED`` desde ``CLEARING`` con su par de asientos D/C, y los
saldos solo se mueven junto a esos asientos, así que ``make reconcile`` sigue dando 0. No pasa por el scoring ni
deja filas en ``risk.scoring_log``, y como no emite eventos tampoco hay ``TransferCompleted`` que neutralizar.

Idempotente: la clave de cada transferencia semilla es ``md5(cuenta + saldo deseado)``; repetir la siembra no hace
nada, y pedir un saldo mayor añade solo la diferencia. Solo usa la biblioteca estándar.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass
from decimal import Decimal

CLIENT_ID = "sim-seed"
CLEARING = "md5('verabank-seed:clearing')::uuid"   # el id que fija db/seed/01_core_seed.sql
BATCH = 2000


@dataclass(frozen=True)
class SeedAccount:
    account_id: str
    customer_id: str
    segment: str
    balance: Decimal


def _q(value: object) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _batches(items: Sequence[SeedAccount], size: int) -> Iterator[Sequence[SeedAccount]]:
    for i in range(0, len(items), size):
        yield items[i:i + size]


def batch_sql(batch: Sequence[SeedAccount]) -> str:
    """Un lote en una transacción: clientes y cuentas, y luego la transferencia semilla de quien necesite saldo."""
    people = ",\n".join(f"({_q(a.account_id)}::uuid, {_q(a.customer_id)}::uuid, {_q(a.segment)})" for a in batch)
    wanted = ",\n".join(f"({_q(a.account_id)}::uuid, {_q(f'{a.balance:.2f}')}::numeric(18,2))" for a in batch)
    return f"""BEGIN;

INSERT INTO core.accounts (account_id, customer_id, account_type, currency, status)
VALUES ({CLEARING}, NULL, 'CLEARING', 'PEN', 'ACTIVE') ON CONFLICT (account_id) DO NOTHING;

WITH input(account_id, customer_id, segment) AS (VALUES
{people}
),
customers AS (
  INSERT INTO core.customers (customer_id, segment, country)
  SELECT DISTINCT ON (customer_id) customer_id, segment, 'PE' FROM input ORDER BY customer_id
  ON CONFLICT (customer_id) DO NOTHING
  RETURNING 1
)
INSERT INTO core.accounts (account_id, customer_id, account_type, currency, status)
SELECT account_id, customer_id, 'CUSTOMER', 'PEN', 'ACTIVE' FROM input
ON CONFLICT (account_id) DO NOTHING;

WITH input(account_id, balance) AS (VALUES
{wanted}
),
todo AS (                       -- cuentas que necesitan saldo y cuya transferencia semilla aún no existe
  SELECT i.account_id,
         md5('verabank-sim:' || i.account_id::text || ':' || i.balance::text)::uuid AS idempotency_key,
         i.balance - a.balance AS amount,
         row_number() OVER (ORDER BY i.account_id) AS n
  FROM input i JOIN core.accounts a ON a.account_id = i.account_id
  WHERE i.balance > a.balance
    AND NOT EXISTS (SELECT 1 FROM core.transfers t WHERE t.client_id = '{CLIENT_ID}'
                    AND t.idempotency_key = md5('verabank-sim:' || i.account_id::text || ':' || i.balance::text)::uuid)
),
clearing AS (SELECT balance FROM core.accounts WHERE account_id = {CLEARING} FOR UPDATE),
ins_transfers AS (
  INSERT INTO core.transfers (transfer_id, client_id, idempotency_key, request_hash, source_account_id,
                              dest_account_id, dest_country, rail, transfer_type, amount, currency, status,
                              occurred_at, completed_at)
  SELECT md5('verabank-sim:transfer:' || t.idempotency_key::text)::uuid, '{CLIENT_ID}', t.idempotency_key,
         encode(sha256(convert_to('sim-seed|' || t.account_id::text || '|' || t.amount::text, 'UTF8')), 'hex'),
         {CLEARING}, t.account_id, 'PE', 'INTERNAL', 'TRANSFER', t.amount, 'PEN', 'COMPLETED',
         timestamptz '2026-01-01 00:00:00+00', timestamptz '2026-01-01 00:00:00+00'
  FROM todo t
  RETURNING transfer_id
),
ins_entries AS (                -- par D/C por transferencia
  INSERT INTO core.transactions (transfer_id, account_id, direction, entry_kind, amount, balance_after)
  SELECT md5('verabank-sim:transfer:' || t.idempotency_key::text)::uuid, {CLEARING}, 'D', 'TRANSFER', t.amount,
         c.balance - sum(t.amount) OVER (ORDER BY t.n)
  FROM todo t CROSS JOIN clearing c
  UNION ALL
  SELECT md5('verabank-sim:transfer:' || t.idempotency_key::text)::uuid, t.account_id, 'C', 'TRANSFER', t.amount,
         a.balance + t.amount
  FROM todo t JOIN core.accounts a ON a.account_id = t.account_id
  RETURNING entry_id
),
upd_clearing AS (
  UPDATE core.accounts a SET balance = a.balance - s.total, version = a.version + 1
  FROM (SELECT sum(amount) AS total FROM todo) s
  WHERE a.account_id = {CLEARING} AND s.total IS NOT NULL
  RETURNING a.account_id
),
upd_accounts AS (
  UPDATE core.accounts a SET balance = a.balance + t.amount, version = a.version + 1
  FROM todo t WHERE a.account_id = t.account_id
  RETURNING a.account_id
)
SELECT (SELECT count(*) FROM ins_transfers) || '|' || (SELECT count(*) FROM ins_entries) || '|'
       || (SELECT count(*) FROM upd_accounts) || '|' || coalesce((SELECT sum(amount) FROM todo), 0);

COMMIT;
"""


@dataclass(frozen=True)
class SeedResult:
    accounts: int
    funded: int
    amount: Decimal


class Psql:
    """Lo que la siembra necesita de ``db.DockerPsql`` (se sustituye en las pruebas)."""

    def run(self, stdin: str, *psql_args: str, timeout_s: float = 900.0) -> str:  # pragma: no cover - interfaz
        raise NotImplementedError


def seed(psql: Psql, accounts: Iterable[SeedAccount], *, batch: int = BATCH,
         progress: Callable[[int, int], None] | None = None) -> SeedResult:
    """Siembra las cuentas por lotes. Devuelve cuántas se pidieron, cuántas se financiaron y cuánto se acreditó."""
    ordered = sorted(accounts, key=lambda a: a.account_id)
    funded = 0
    total = Decimal(0)
    for done, chunk in enumerate(_batches(ordered, batch), start=1):
        out = psql.run(batch_sql(chunk), "-At", "-f", "-")
        line = next((ln for ln in out.splitlines() if ln.count("|") == 3), None)
        if line is None:
            raise RuntimeError(f"la siembra no devolvió su resumen: {out[:200]!r}")
        transfers, _entries, _updated, amount = line.split("|")
        funded += int(transfers)
        total += Decimal(amount)
        if progress:
            progress(min(done * batch, len(ordered)), len(ordered))
    return SeedResult(len(ordered), funded, total)
