"""Plan y ejecución del caos. El cliente HTTP es un protocolo mínimo para poder sustituirlo en pruebas."""

from __future__ import annotations

import hashlib
import random
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Protocol

from verabank_sim.chaos.report import FINAL_STATES, Report

# Cuentas semilla con saldo holgado: PREMIUM (19 a 26) y PYME (27 a 30); las RETAIL (1 a 18) no.
SOURCE_SEED_INDEXES = range(19, 31)
MIN_AMOUNT = Decimal("1.00")
MAX_AMOUNT = Decimal("10.00")
# Fracción del saldo disponible que se permite comprometer por cuenta (margen frente a restos de otras corridas).
BUDGET_FRACTION = Decimal("0.9")


class Api(Protocol):
    """Lo que el caos necesita de la API: ``(status, headers en minúsculas, cuerpo JSON o None)``."""

    def post_transfer(self, idempotency_key: str, body: dict[str, Any]) -> tuple[int, dict[str, str], Any]: ...

    def get_transfer(self, transfer_id: str) -> tuple[int, dict[str, str], Any]: ...

    def get_account(self, account_id: str) -> tuple[int, dict[str, str], Any]: ...


class InsufficientBudget(Exception):
    """Las cuentas de origen no cubren el total pedido: hace falta más saldo (script de semilla adicional)."""


def seed_account_id(index: int) -> str:
    """``md5('verabank-seed:account:<i>')::uuid``, igual que db/seed y scripts/lib.sh."""
    return str(uuid.UUID(hashlib.md5(f"verabank-seed:account:{index}".encode()).hexdigest()))


@dataclass(frozen=True)
class Planned:
    index: int
    idempotency_key: str
    body: dict[str, Any]
    replay: bool = False       # se reenvía idéntica
    reuse: bool = False        # se reutiliza la clave con otro cuerpo


def allocate_sources(available: dict[str, Decimal], amounts: list[Decimal]) -> list[str]:
    """Reparte las solicitudes por cuentas sin superar ``BUDGET_FRACTION`` del disponible de ninguna.

    Supone el peor caso (todas se completan). Reparte por turnos entre las cuentas con presupuesto.
    """
    budget = {acct: avail * BUDGET_FRACTION for acct, avail in available.items()}
    order = sorted(budget)
    chosen: list[str] = []
    cursor = 0
    for amount in amounts:
        for step in range(len(order)):
            acct = order[(cursor + step) % len(order)]
            if budget[acct] >= amount:
                budget[acct] -= amount
                chosen.append(acct)
                cursor = (cursor + step + 1) % len(order)
                break
        else:
            raise InsufficientBudget(
                f"las cuentas de origen no alcanzan para {len(amounts)} transferencias de hasta {MAX_AMOUNT}: "
                "añade una semilla adicional (transferencia desde CLEARING con sus asientos) o reduce --n")
    return chosen


def make_amounts(n: int, rng: random.Random) -> list[Decimal]:
    low, high = int(MIN_AMOUNT * 100), int(MAX_AMOUNT * 100)
    return [Decimal(rng.randint(low, high)) / 100 for _ in range(n)]


def build_plan(n: int, dup_rate: float, reuse_rate: float, sources: list[str], amounts: list[Decimal],
               rng: random.Random, now: datetime) -> list[Planned]:
    """N solicitudes; ``round(n·dup_rate)`` se reenvían idénticas y ``round(n·reuse_rate)`` reutilizan la clave."""
    n_replay, n_reuse = round(n * dup_rate), round(n * reuse_rate)
    picks = rng.sample(range(n), min(n, n_replay + n_reuse))
    replays, reuses = set(picks[:n_replay]), set(picks[n_replay:])
    occurred_at = now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    plan = []
    for i in range(n):
        body = {
            "source_account_id": sources[i],
            "dest_external_ref": f"EXT-{i:06d}",
            "dest_country": "PE",
            "rail": "EXTERNAL",
            "transfer_type": "TRANSFER",
            "amount": f"{amounts[i]:.2f}",
            "currency": "PEN",
            "occurred_at": occurred_at,
        }
        plan.append(Planned(i, str(uuid.UUID(int=rng.getrandbits(128), version=4)), body, i in replays, i in reuses))
    return plan


def available_balances(api: Api) -> dict[str, Decimal]:
    out: dict[str, Decimal] = {}
    for index in SOURCE_SEED_INDEXES:
        acct = seed_account_id(index)
        status, _, body = api.get_account(acct)
        if status != 200:
            raise RuntimeError(f"GET cuenta semilla {index}: HTTP {status} (¿semilla cargada?)")
        out[acct] = Decimal(body["available"])
    return out


def _problem_type(body: Any) -> str:
    raw = body.get("type", "") if isinstance(body, dict) else ""
    return str(raw).rsplit("/", 1)[-1]


def _create(api: Api, item: Planned, report: Report, ids: dict[int, str]) -> None:
    try:
        status, _, body = api.post_transfer(item.idempotency_key, item.body)
    except Exception as exc:  # noqa: BLE001
        report.create_errors.append(f"#{item.index}: {type(exc).__name__}")
        return
    if status != 201 or not isinstance(body, dict) or "transfer_id" not in body:
        report.create_errors.append(f"#{item.index}: HTTP {status} {_problem_type(body)}".rstrip())
        return
    report.created += 1
    ids[item.index] = body["transfer_id"]
    # Los reenvíos y reusos van después de la respuesta original, así que la transferencia ya existe.
    if item.replay:
        _check_replay(api, item, body["transfer_id"], report)
    elif item.reuse:
        _check_reuse(api, item, report)


def _check_replay(api: Api, item: Planned, transfer_id: str, report: Report) -> None:
    try:
        status, headers, body = api.post_transfer(item.idempotency_key, item.body)
    except Exception as exc:  # noqa: BLE001
        report.replay_errors.append(f"#{item.index}: {type(exc).__name__}")
        return
    same = isinstance(body, dict) and body.get("transfer_id") == transfer_id
    if status == 200 and headers.get("idempotent-replayed") == "true" and same:
        report.replays_ok += 1
    else:
        report.replay_errors.append(
            f"#{item.index}: HTTP {status}, Idempotent-Replayed={headers.get('idempotent-replayed')!r}, "
            f"mismo id={same}")


def _check_reuse(api: Api, item: Planned, report: Report) -> None:
    other = dict(item.body)
    other["amount"] = f"{Decimal(item.body['amount']) + Decimal('0.01'):.2f}"
    try:
        status, _, body = api.post_transfer(item.idempotency_key, other)
    except Exception as exc:  # noqa: BLE001
        report.reuse_errors.append(f"#{item.index}: {type(exc).__name__}")
        return
    if status == 422 and _problem_type(body) == "idempotency-key-reuse":
        report.reuse_ok += 1
    else:
        report.reuse_errors.append(f"#{item.index}: HTTP {status} {_problem_type(body)}".rstrip())


def _poll(api: Api, transfer_id: str) -> tuple[str, str]:
    """('final', estado) | ('open', estado) | ('error', motivo)."""
    try:
        status, _, body = api.get_transfer(transfer_id)
    except Exception as exc:  # noqa: BLE001
        return "error", type(exc).__name__
    if status != 200 or not isinstance(body, dict):
        return "error", f"HTTP {status}"
    state = str(body.get("status"))
    return ("final" if state in FINAL_STATES else "open"), state


def run_chaos(api: Api, plan: list[Planned], reject_rate: float, concurrency: int, timeout_s: float,
              poll_interval_s: float = 1.0, clock: Callable[[], float] = time.monotonic,
              sleep: Callable[[float], None] = time.sleep) -> Report:
    report = Report(n=len(plan), reject_rate=reject_rate)
    report.replays_expected = sum(1 for p in plan if p.replay)
    report.reuse_expected = sum(1 for p in plan if p.reuse)
    started = clock()
    ids: dict[int, str] = {}
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        list(pool.map(lambda item: _create(api, item, report, ids), plan))
        pending = set(ids.values())
        while pending:
            tids = sorted(pending)
            for tid, (kind, detail) in zip(tids, pool.map(lambda t: _poll(api, t), tids), strict=True):
                if kind == "final":
                    report.states[detail] += 1
                    pending.discard(tid)
                elif kind == "error":
                    report.poll_errors.append(f"{tid}: {detail}")
            if not pending or clock() - started >= timeout_s:
                break
            sleep(poll_interval_s)
    report.non_final = sorted(pending)
    report.elapsed_s = clock() - started
    return report
