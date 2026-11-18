"""Subcomando ``load-seed`` (make load-seed): cuentas de la prueba de carga T-17 (D10).

``--accounts`` orígenes con saldo alto y ``--destinations`` destinos internos, sembrados con el mecanismo de la fase 1
(una transferencia ``INTERNAL COMPLETED`` desde ``CLEARING`` con su par de asientos; nunca ``balance`` directo), así
que ``make reconcile`` sigue en 0. Los identificadores son ``md5('verabank-load:src:<i>')::uuid`` y
``md5('verabank-load:dst:<j>')::uuid``: ``perf/transfers.js`` los calcula igual. Idempotente.
"""

from __future__ import annotations

import argparse
import hashlib
import uuid
from decimal import Decimal

from verabank_sim.load import seed_sql
from verabank_sim.load.db import transfer_service_psql


def seed_uuid(label: str) -> str:
    """``md5(label)::uuid`` de PostgreSQL."""
    return str(uuid.UUID(hashlib.md5(label.encode()).hexdigest()))


def segment(i: int) -> str:
    slot = i % 30
    return "RETAIL" if slot < 18 else "PREMIUM" if slot < 26 else "PYME"


def plan(accounts: int, destinations: int, balance: Decimal) -> list[seed_sql.SeedAccount]:
    out = [seed_sql.SeedAccount(seed_uuid(f"verabank-load:src:{i}"), seed_uuid(f"verabank-load:cust:src:{i}"),
                                segment(i), balance) for i in range(accounts)]
    out += [seed_sql.SeedAccount(seed_uuid(f"verabank-load:dst:{j}"), seed_uuid(f"verabank-load:cust:dst:{j}"),
                                 segment(j), Decimal("0.00")) for j in range(destinations)]
    return out


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--accounts", type=int, default=5000, help="cuentas origen (5000)")
    parser.add_argument("--destinations", type=int, default=2000, help="destinos internos (2000)")
    parser.add_argument("--balance", type=Decimal, default=Decimal("1000000.00"), help="saldo de cada origen")


def run(args: argparse.Namespace) -> int:
    result = seed_sql.seed(transfer_service_psql(), plan(args.accounts, args.destinations, args.balance))
    print(f"load-seed: {result.accounts} cuentas ({args.accounts} orígenes, {args.destinations} destinos); "
          f"{result.funded} financiadas ahora por {result.amount}")
    return 0
