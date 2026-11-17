"""Siembra masiva de cuentas del simulador (simulator/verabank_sim/load/seed_sql.py) contra PostgreSQL real.

El módulo del simulador usa solo la biblioteca estándar, así que se carga por ruta: este proyecto no depende de él.
Comprueba lo que importa del manual 2.2: los saldos entran como asientos desde CLEARING, la conciliación sigue en 0 y
0, no se emite ningún evento ni se toca risk.scoring_log, y repetir la siembra no cambia nada.
"""

import importlib.util
import sys
import uuid
from decimal import Decimal

import pytest
from conftest import ADMIN, ROOT

SPEC = importlib.util.spec_from_file_location("sim_seed_sql", ROOT / "simulator/verabank_sim/load/seed_sql.py")
seed_sql = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = seed_sql   # los dataclasses con anotaciones diferidas lo necesitan
SPEC.loader.exec_module(seed_sql)

RECONCILE = (ROOT / "db/reconcile.sql").read_text()


class PgPsql:
    """Ejecuta el SQL de la siembra con el rol de transfer-service, como lo hace psql en el contenedor."""

    def __init__(self, db):
        self.db = db

    def run(self, stdin, *args, timeout_s=900.0):
        lines = []
        with self.db.connect("verabank_transfer") as conn:
            cur = conn.execute(stdin)
            while True:
                if cur.description:
                    lines += ["|".join(str(c) for c in row) for row in cur.fetchall()]
                if not cur.nextset():
                    break
        return "\n".join(lines) + "\n"


def account(n, balance, customer=None):
    return seed_sql.SeedAccount(
        account_id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"sim-test:account:{n}")),
        customer_id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"sim-test:customer:{customer if customer is not None else n}")),
        segment=("RETAIL", "PREMIUM", "PYME")[n % 3],
        balance=Decimal(balance),
    )


def reconcile(db):
    with db.connect("verabank_transfer") as conn:
        cur = conn.execute(RECONCILE)
        first = cur.fetchone()[0]
        cur.nextset()
        return first, cur.fetchone()[0]


def balance(db, acc):
    with db.connect(ADMIN) as conn:
        return conn.execute("SELECT balance, held, version FROM core.accounts WHERE account_id = %s", (acc.account_id,)).fetchone()


@pytest.fixture(scope="module")
def db(provisioned_db):
    return provisioned_db


def test_balances_enter_as_entries_from_clearing_and_reconcile_stays_at_zero(db):
    accounts = [account(1, "1500.50"), account(2, "20.00", customer=1), account(3, "0"), account(4, "98765432.10")]
    result = seed_sql.seed(PgPsql(db), accounts, batch=3)   # dos lotes
    assert (result.accounts, result.funded, result.amount) == (4, 3, Decimal("98766952.60"))
    assert [balance(db, a)[0] for a in accounts] == [Decimal("1500.50"), Decimal("20.00"), 0, Decimal("98765432.10")]
    assert reconcile(db) == (0, 0)
    with db.connect(ADMIN) as conn:
        # cada cuenta financiada tiene su par D/C y el saldo de CLEARING baja por la suma acreditada
        assert conn.execute("SELECT count(*) FROM core.transactions WHERE direction = 'D' AND account_id = md5('verabank-seed:clearing')::uuid").fetchone()[0] >= 3
        clearing = conn.execute("SELECT balance FROM core.accounts WHERE account_id = md5('verabank-seed:clearing')::uuid").fetchone()[0]
        assert clearing == -Decimal("98766952.60")
        # las cuentas 1 y 2 comparten cliente; la cuenta sin saldo no tiene asientos
        assert conn.execute("SELECT count(DISTINCT customer_id) FROM core.accounts WHERE account_id = ANY(%s::uuid[])",
                            ([accounts[0].account_id, accounts[1].account_id],)).fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM core.transactions WHERE account_id = %s", (accounts[2].account_id,)).fetchone()[0] == 0


def test_seeding_emits_no_events_and_never_touches_the_scoring_log(db):
    with db.connect(ADMIN) as conn:
        outbox = conn.execute("SELECT count(*) FROM core.outbox").fetchone()[0]
        scored = conn.execute("SELECT count(*) FROM risk.scoring_log").fetchone()[0]
    seed_sql.seed(PgPsql(db), [account(10, "75.25"), account(11, "10.00")])
    with db.connect(ADMIN) as conn:
        assert conn.execute("SELECT count(*) FROM core.outbox").fetchone()[0] == outbox
        assert conn.execute("SELECT count(*) FROM risk.scoring_log").fetchone()[0] == scored
        assert conn.execute("SELECT count(*), min(status) FROM core.transfers WHERE client_id = 'sim-seed'").fetchone()[1] == "COMPLETED"


def test_seeding_twice_changes_nothing_and_a_larger_balance_adds_only_the_difference(db):
    accounts = [account(20, "100.00"), account(21, "50.00")]
    seed_sql.seed(PgPsql(db), accounts)
    before = [balance(db, a) for a in accounts]
    again = seed_sql.seed(PgPsql(db), accounts)
    assert (again.funded, again.amount) == (0, 0)
    assert [balance(db, a) for a in accounts] == before
    bigger = [account(20, "130.00"), account(21, "50.00")]
    topup = seed_sql.seed(PgPsql(db), bigger)
    assert (topup.funded, topup.amount) == (1, Decimal("30.00"))
    assert balance(db, bigger[0])[0] == Decimal("130.00") and balance(db, bigger[1])[0] == Decimal("50.00")
    assert reconcile(db) == (0, 0)


def test_a_balance_already_above_the_wanted_one_is_left_alone(db):
    acc = account(30, "200.00")
    seed_sql.seed(PgPsql(db), [acc])
    with db.connect(ADMIN) as conn:
        conn.execute("SELECT 1")   # el saldo gastado lo mueve el servicio; aquí basta con pedir menos
    smaller = account(30, "50.00")
    assert seed_sql.seed(PgPsql(db), [smaller]).funded == 0
    assert balance(db, acc)[0] == Decimal("200.00")
