"""Cuentas a sembrar para un replay (D10): una por nombre de origen, con el saldo del conjunto A irrelevante."""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

from verabank_sim.load.seed_sql import SeedAccount
from verabank_sim.replay import mapping as m


def replay_accounts(rows: Sequence[m.Row]) -> list[SeedAccount]:
    """Saldo sembrado = ``max(oldbalanceOrg, suma de los montos del origen en el replay)``.

    ``oldbalanceOrg`` es el de la primera fila del origen en el replay. El conjunto A de features no usa saldos: el saldo
    solo debe alcanzar para que ninguna transferencia falle por fondos insuficientes.
    """
    first: dict[str, Decimal] = {}
    spent: dict[str, Decimal] = {}
    for row in rows:
        first.setdefault(row.name_orig, row.old_balance_org)
        spent[row.name_orig] = spent.get(row.name_orig, Decimal(0)) + row.amount
    accounts = []
    for name in sorted(first):
        index = m.customer_index(name)
        accounts.append(SeedAccount(m.account_id(name), m.customer_id(index), m.segment(index),
                                    max(first[name], spent[name]).quantize(m.CENT)))
    return accounts
