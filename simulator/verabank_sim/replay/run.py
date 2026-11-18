"""Orquesta un replay: siembra, calentamiento, envío con barrera por paso, estado final, etiquetas y resumen."""

from __future__ import annotations

import json
import logging
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from verabank_sim.load import finalstate, seed_sql
from verabank_sim.load.engine import run_groups
from verabank_sim.load.stats import describe
from verabank_sim.replay import labels, mapping as m, warmup
from verabank_sim.replay.accounts import replay_accounts
from verabank_sim.replay.paysim import Selection

log = logging.getLogger("verabank_sim.replay")


@dataclass(frozen=True)
class Options:
    n: int = 20000
    tps: float = 50.0
    amount_factor: Decimal = Decimal(1)
    start_row: int = 0
    warmup: bool = True
    labels: bool = True
    seed: bool = True
    concurrency: int = 20
    final_timeout_s: float = 60.0
    steps: int | None = None          # modo por pasos (--steps); None = modo por filas (--n)
    first_step: int | None = None


@dataclass(frozen=True)
class Job:
    row: m.Row
    key: str
    body: dict[str, str]


@dataclass
class Services:
    api: Any                       # post_transfer(key, body) -> Reply
    state_psql: Any                # rol de transfer-service: siembra y lectura del estado final
    loader_psql: Any               # rol sim_loader: calentamiento
    publisher: Any | None          # publish(messages) -> int
    client_id: str
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    now: Callable[[], float] = time.time
    extra: dict[str, Any] = field(default_factory=dict)
    reader_psql: Any = None        # rol de solo lectura (BI): ¿hay filas reales antes de la tanda?


def build_jobs(rows: list[m.Row], epoch: datetime) -> list[tuple[int, list[Job]]]:
    """Un grupo por paso, en orden. Todas las filas de un paso comparten ``occurred_at`` (D10)."""
    groups: dict[int, list[Job]] = {}
    for row in rows:
        groups.setdefault(row.step, []).append(Job(row, m.idempotency_key(row), m.transfer_body(row, epoch)))
    return sorted(groups.items())


def summarize(options: Options, selection: Selection, outcomes: list, final: dict[str, finalstate.FinalRow],
              elapsed_s: float, extra: dict[str, Any]) -> dict[str, Any]:
    http: Counter[str] = Counter()
    client_ms: list[float] = []
    for o in outcomes:
        if o.error is not None:
            http["error"] += 1
        else:
            http[str(o.value.status)] += 1
            client_ms.append(o.value.elapsed_ms)
    rows = list(final.values())
    return {
        "amount_factor": str(options.amount_factor),
        "sent": len(outcomes),
        "rows": len(selection.rows),
        "start_step": selection.start_step,
        "skipped_zero_amount": selection.skipped_zero_amount,
        "by_http_status": dict(sorted(http.items())),
        "by_fraud_decision": dict(sorted(Counter(r.fraud_decision or "NONE" for r in rows).items())),
        "by_decision_source": dict(sorted(Counter(r.decision_source or "NONE" for r in rows).items())),
        "by_final_status": dict(sorted(Counter(r.status for r in rows).items())),
        "elapsed_s": round(elapsed_s, 3),
        "tps_achieved": round(len(outcomes) / elapsed_s, 2) if elapsed_s > 0 else None,
        "client_latency_ms": describe(client_ms),
        "scoring_latency_ms": describe(r.scoring_latency_ms for r in rows if r.scoring_latency_ms is not None),
        **extra,
    }


def run(options: Options, selection: Selection, epoch: datetime, services: Services, out_dir: Path | None) -> dict[str, Any]:
    extra: dict[str, Any] = {"warmup_rows": 0, "warmup_method": None, "warmup_skipped_real_rows": 0,
                             "seeded_accounts": 0, "funded_accounts": 0, "labels_published": 0,
                             "steps": options.steps, "first_step": options.first_step}
    if options.seed and selection.rows:
        result = seed_sql.seed(services.state_psql, replay_accounts(selection.rows))
        extra["seeded_accounts"], extra["funded_accounts"] = result.accounts, result.funded
        log.info("cuentas: %d pedidas, %d financiadas (%s)", result.accounts, result.funded, result.amount)
    if options.warmup and selection.warmup:
        # Tandas encadenadas: si la tanda anterior ya dejó filas reales en los 24 pasos previos, esas son la historia;
        # el calentamiento no se carga (tampoco podría pisarlas: sus ids son otros y la carga no actualiza nada).
        anchor = options.first_step or selection.start_step
        real = (warmup.real_rows_before(services.reader_psql, epoch, anchor)
                if services.reader_psql is not None and anchor is not None else 0)
        if real:
            extra["warmup_skipped_real_rows"] = real
            extra["warmup_method"] = "skipped"
            log.info("calentamiento: no se carga, ya hay %d filas reales en los 24 pasos previos al %d", real, anchor)
        else:
            loaded, method = warmup.load(services.loader_psql, selection.warmup, epoch)
            extra["warmup_rows"], extra["warmup_method"] = loaded, method
            log.info("calentamiento: %d filas (%s)", loaded, method)

    groups = build_jobs(selection.rows, epoch)
    started = services.clock()
    outcomes = run_groups(groups, lambda job: services.api.post_transfer(job.key, job.body), tps=options.tps,
                          concurrency=options.concurrency, clock=services.clock, sleep=services.sleep)
    elapsed = services.clock() - started

    jobs = [job for _, group in groups for job in group]
    final = finalstate.wait_for_final(services.state_psql, services.client_id, [j.key for j in jobs],
                                      timeout_s=options.final_timeout_s, clock=services.clock, sleep=services.sleep)
    if options.labels and services.publisher is not None:
        items = [(final[j.key].transfer_id, j.row.is_fraud, j.body["occurred_at"]) for j in jobs if j.key in final]
        extra["labels_published"] = services.publisher.publish(labels.build_all(items, emitted_at=services.now()))
    summary = summarize(options, selection, outcomes, final, elapsed, extra)
    if out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    return summary


def format_summary(summary: dict[str, Any]) -> str:
    def lat(name: str) -> str:
        d = summary[name]
        return "sin datos" if not d["n"] else f"p50 {d['p50']} ms, p95 {d['p95']} ms, p99 {d['p99']} ms (n={d['n']})"

    return "\n".join([
        f"enviadas: {summary['sent']} en {summary['elapsed_s']} s ({summary['tps_achieved']} TPS alcanzados)",
        f"por código HTTP: {summary['by_http_status']}",
        f"por fraud_decision: {summary['by_fraud_decision']}",
        f"por decision_source: {summary['by_decision_source']}",
        f"por estado final: {summary['by_final_status']}",
        f"latencia de cliente de POST /transfers: {lat('client_latency_ms')}",
        f"scoring_latency_ms: {lat('scoring_latency_ms')}",
        f"calentamiento: {summary['warmup_rows']} filas ({summary['warmup_method']}); "
        f"cuentas sembradas: {summary['seeded_accounts']} "
        f"({summary['funded_accounts']} financiadas); etiquetas publicadas: {summary['labels_published']}",
    ])
