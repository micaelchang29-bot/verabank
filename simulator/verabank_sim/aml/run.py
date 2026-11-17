"""Carga de ``data/aml/transfers.jsonl`` por la API de transfer-service (D12).

Envía las transferencias en el orden y con el ``occurred_at`` del archivo (barrera por hora de negocio), siembra sus
cuentas con el mismo mecanismo del replay y, con ``--approve-reviews``, aprueba por la API las que quedan en
``IN_REVIEW`` (comentario ``aml-eval``). Escribe el mapa id del generador → id del servicio y el desenlace de cada una,
que ``aml-eval`` necesita para reconocer las alertas que tocan estas transferencias.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from verabank_sim.load import finalstate, seed_sql
from verabank_sim.load.engine import run_groups
from verabank_sim.replay.mapping import CENT, NS

log = logging.getLogger("verabank_sim.aml")
REVIEW_COMMENT = "aml-eval"
COMPLETED, APPROVED, BLOCKED, NOT_COMPLETED = "COMPLETED", "APPROVED_IN_REVIEW", "BLOCKED", "NOT_COMPLETED"


@dataclass(frozen=True)
class Job:
    transfer: dict[str, Any]
    key: str
    body: dict[str, str]


@dataclass(frozen=True)
class Options:
    tps: float = 50.0
    approve_reviews: bool = False
    seed: bool = True
    concurrency: int = 20
    final_timeout_s: float = 60.0


def read_transfers(path: Path) -> list[dict[str, Any]]:
    """Las transferencias del archivo, por ``(occurred_at, transfer_id)`` (el orden en que se envían)."""
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    return sorted(rows, key=lambda t: (t["occurred_at"], t["transfer_id"]))


def idempotency_key(transfer: dict[str, Any]) -> str:
    return str(uuid.uuid5(NS, f"aml:{transfer['transfer_id']}"))


def request_body(t: dict[str, Any]) -> dict[str, str]:
    body = {"source_account_id": t["source_account_id"], "dest_country": t["dest_country"], "rail": t["rail"],
            "transfer_type": t["transfer_type"], "amount": t["amount"], "currency": t["currency"],
            "occurred_at": t["occurred_at"]}
    if t["rail"] == "INTERNAL":
        body["dest_account_id"] = t["dest_account_id"]
    else:
        body["dest_external_ref"] = t["dest_external_ref"]
    return body


def seed_accounts(transfers: list[dict[str, Any]]) -> list[seed_sql.SeedAccount]:
    """Todas las cuentas del archivo. Saldo = suma de lo que la cuenta envía (los abonos que reciba solo suman)."""
    spent: dict[str, Decimal] = {}
    for t in transfers:
        spent[t["source_account_id"]] = spent.get(t["source_account_id"], Decimal(0)) + Decimal(t["amount"])
        if t.get("dest_account_id"):
            spent.setdefault(t["dest_account_id"], Decimal(0))
    return [seed_sql.SeedAccount(a, str(uuid.uuid5(NS, f"aml-customer:{a}")), "RETAIL", spent[a].quantize(CENT))
            for a in sorted(spent)]


def build_groups(transfers: list[dict[str, Any]]) -> list[tuple[str, list[Job]]]:
    """Un grupo por hora de negocio, en orden (barrera: las ventanas de las reglas dependen del orden)."""
    groups: dict[str, list[Job]] = {}
    for t in transfers:
        groups.setdefault(t["occurred_at"][:13], []).append(Job(t, idempotency_key(t), request_body(t)))
    return sorted(groups.items())


def make_worker(api: Any, approve_reviews: bool) -> Callable[[Job], dict[str, Any]]:
    def work(job: Job) -> dict[str, Any]:
        reply = api.post_transfer(job.key, job.body)
        result: dict[str, Any] = {"http": reply.status, "approved": False}
        status = reply.body.get("status") if isinstance(reply.body, dict) else None
        if status == "IN_REVIEW" and approve_reviews:
            tid = reply.body["transfer_id"]
            review = api.review(tid, "APPROVE", REVIEW_COMMENT)
            result["approved"] = review.status == 200
            result["review_http"] = review.status
        return result
    return work


def classify(final: finalstate.FinalRow | None, approved: bool) -> str:
    if final is None:
        return NOT_COMPLETED
    if final.failure_reason == "RISK_BLOCK":
        return BLOCKED
    if final.status == "COMPLETED":
        return APPROVED if approved else COMPLETED
    return NOT_COMPLETED


def run(transfers: list[dict[str, Any]], options: Options, api: Any, state_psql: Any, client_id: str,
        out_map: Path | None, *, clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    seeded = None
    if options.seed:
        seeded = seed_sql.seed(state_psql, seed_accounts(transfers))
        log.info("cuentas: %d pedidas, %d financiadas", seeded.accounts, seeded.funded)
    groups = build_groups(transfers)
    started = clock()
    outcomes = run_groups(groups, make_worker(api, options.approve_reviews), tps=options.tps,
                          concurrency=options.concurrency, clock=clock, sleep=sleep)
    elapsed = clock() - started
    jobs = [j for _, g in groups for j in g]
    final = finalstate.wait_for_final(state_psql, client_id, [j.key for j in jobs], timeout_s=options.final_timeout_s,
                                      clock=clock, sleep=sleep)
    http: Counter[str] = Counter()
    desenlace: Counter[str] = Counter()
    mapping = []
    for job, out in zip(jobs, outcomes, strict=True):
        info = out.value if out.error is None else {"http": "error", "approved": False}
        http[str(info["http"])] += 1
        row = final.get(job.key)
        outcome = classify(row, bool(info["approved"]))
        desenlace[outcome] += 1
        mapping.append({"transfer_id": job.transfer["transfer_id"], "service_transfer_id": row.transfer_id if row else None,
                        "outcome": outcome})
    summary = {
        "sent": len(jobs),
        "elapsed_s": round(elapsed, 3),
        "by_http_status": dict(sorted(http.items())),
        "completed": desenlace[COMPLETED],
        "approved_in_review": desenlace[APPROVED],
        "blocked": desenlace[BLOCKED],
        "not_completed": desenlace[NOT_COMPLETED],
        "by_final_status": dict(sorted(Counter(r.status for r in final.values()).items())),
        "seeded_accounts": seeded.accounts if seeded else 0,
    }
    if out_map is not None:
        out_map.parent.mkdir(parents=True, exist_ok=True)
        out_map.write_text("".join(json.dumps(m, sort_keys=True) + "\n" for m in mapping), encoding="utf-8")
    return summary


def format_summary(s: dict[str, Any]) -> str:
    return "\n".join([
        f"enviadas: {s['sent']} en {s['elapsed_s']} s; por código HTTP: {s['by_http_status']}",
        f"completadas: {s['completed']}",
        f"aprobadas en revisión (comentario {REVIEW_COMMENT}): {s['approved_in_review']}",
        f"rechazadas por BLOCK (aparte, D12): {s['blocked']}",
        f"sin completar: {s['not_completed']}; por estado final: {s['by_final_status']}",
    ])
