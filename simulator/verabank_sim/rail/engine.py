"""Motor puro del rail: sin Kafka, con reloj y fuente aleatoria inyectables."""

from __future__ import annotations

import heapq
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from random import Random
from typing import Literal

from verabank_sim.rail import messages as m
from verabank_sim.rail.store import Credit, SqliteStore

REASON_CODES = ("ACCOUNT_CLOSED", "INVALID_BENEFICIARY", "BENEFICIARY_BANK_UNAVAILABLE", "COMPLIANCE_HOLD")


@dataclass(frozen=True)
class RailConfig:
    reject_rate: float = 0.0
    delay_min_ms: int = 20
    delay_max_ms: int = 200
    dup_rate: float = 0.0
    dup_mode: Literal["same-id", "new-id"] = "same-id"
    silent_rate: float = 0.0
    lost_rate: float = 0.0
    seed: int = 42

    def __post_init__(self) -> None:
        for name in ("reject_rate", "dup_rate", "silent_rate", "lost_rate"):
            if not 0.0 <= getattr(self, name) <= 1.0:
                raise ValueError(f"{name} debe estar entre 0 y 1")
        if not 0 <= self.delay_min_ms <= self.delay_max_ms:
            raise ValueError("--delay-ms exige 0 <= MIN <= MAX")
        if self.dup_mode not in ("same-id", "new-id"):
            raise ValueError("dup_mode debe ser same-id o new-id")


@dataclass(frozen=True)
class Command:
    """Un comando ya validado."""

    event_type: str
    transfer_id: str
    correlation_id: str
    occurred_at: str


@dataclass
class Counters:
    received: int = 0
    confirmed: int = 0
    rejected: int = 0
    silenced: int = 0
    lost: int = 0
    duplicated: int = 0
    queries: int = 0
    cancels: int = 0

    def summary(self) -> str:
        return (
            f"recibidas={self.received} confirmadas={self.confirmed} rechazadas={self.rejected} "
            f"silenciadas={self.silenced} perdidas={self.lost} duplicadas={self.duplicated} "
            f"consultas={self.queries} cancelaciones={self.cancels}"
        )


@dataclass
class _Decision:
    lost: bool
    outcome: str
    detail: str
    silent: bool
    delay_s: float
    dup: bool


@dataclass
class RailEngine:
    store: SqliteStore
    config: RailConfig = field(default_factory=RailConfig)
    clock: Callable[[], float] = time.time  # epoch en segundos; las pruebas inyectan uno falso
    rng_factory: Callable[[str], Random] = Random
    counters: Counters = field(default_factory=Counters)

    def __post_init__(self) -> None:
        self._heap: list[tuple[float, str]] = []

    # ---- arranque ---------------------------------------------------------------------

    def recover(self) -> int:
        """Reprograma desde el almacén los envíos pendientes; devuelve cuántos."""
        pending = self.store.pending()
        self._heap = [(c.due_at or 0.0, c.transfer_id) for c in pending]
        heapq.heapify(self._heap)
        return len(pending)

    # ---- comandos ---------------------------------------------------------------------

    def handle(self, command: Command) -> list[m.Message]:
        """Aplica un comando validado y devuelve las respuestas inmediatas."""
        self.counters.received += 1
        if command.event_type == m.CREDIT_INSTRUCTION:
            return self._instruction(command)
        if command.event_type == m.CREDIT_STATUS_QUERY:
            self.counters.queries += 1
            return [self._status(command, self._query_state(self.store.get(command.transfer_id)))]
        if command.event_type == m.CREDIT_CANCEL:
            self.counters.cancels += 1
            return [self._cancel(command)]
        raise ValueError(f"comando no soportado: {command.event_type}")

    def _instruction(self, command: Command) -> list[m.Message]:
        credit = self.store.get(command.transfer_id)
        if credit is not None:
            # Ya decidida (o cancelada): no se decide otra vez.
            if credit.reply_emitted and not credit.cancelled:
                self.counters.duplicated += 1
                return [self._result_message(credit)]
            return []
        decision = self._decide(command.transfer_id)
        if decision.lost:
            self.counters.lost += 1
            return []
        credit = Credit(
            transfer_id=command.transfer_id,
            correlation_id=command.correlation_id,
            occurred_at=command.occurred_at,
            outcome=decision.outcome,
            detail=decision.detail,
            due_at=self.clock() + decision.delay_s,
            silent=decision.silent,
            dup=decision.dup,
        )
        self.store.save(credit)
        if decision.outcome == "CONFIRMED":
            self.counters.confirmed += 1
        else:
            self.counters.rejected += 1
        if decision.silent:
            self.counters.silenced += 1
        else:
            heapq.heappush(self._heap, (credit.due_at, credit.transfer_id))
        return []

    def _cancel(self, command: Command) -> m.Message:
        credit = self.store.get(command.transfer_id)
        if credit is None:
            # Nunca llegó o se perdió: la marca hace que una instrucción tardía se ignore.
            self.store.save(
                Credit(command.transfer_id, command.correlation_id, command.occurred_at, cancelled=True)
            )
            return self._status(command, "CANCELLED")
        state = self._query_state(credit)
        if state in ("UNKNOWN", "CANCELLED"):
            if not credit.cancelled:
                credit.cancelled = True
                self.store.save(credit)
            return self._status(command, "CANCELLED")
        return self._status(command, state)  # ya venció: no se puede cancelar

    def _query_state(self, credit: Credit | None) -> str:
        if credit is None:
            return "UNKNOWN"
        if credit.cancelled:
            return "CANCELLED"
        if credit.outcome is not None and credit.due_at is not None and self.clock() >= credit.due_at:
            return credit.outcome  # una respuesta silenciada también cuenta como vencida
        return "UNKNOWN"

    def _status(self, command: Command, state: str) -> m.Message:
        return m.build_message(
            event_type=m.CREDIT_STATUS,
            transfer_id=command.transfer_id,
            payload={"state": state},
            event_id=str(uuid.uuid4()),
            correlation_id=command.correlation_id,
            occurred_at=command.occurred_at,
            emitted_at=self.clock(),
        )

    # ---- envíos programados -----------------------------------------------------------

    def next_due_at(self) -> float | None:
        return self._heap[0][0] if self._heap else None

    def due(self) -> list[m.Message]:
        """Respuestas cuyo retraso ya pasó; las marca como emitidas."""
        out: list[m.Message] = []
        now = self.clock()
        while self._heap and self._heap[0][0] <= now:
            _, transfer_id = heapq.heappop(self._heap)
            credit = self.store.get(transfer_id)
            if credit is None or credit.cancelled or credit.silent or credit.reply_emitted:
                continue  # cancelada, silenciada o ya enviada
            credit.reply_emitted = True
            self.store.save(credit)
            first = self._result_message(credit)
            out.append(first)
            if credit.dup:
                self.counters.duplicated += 1
                if self.config.dup_mode == "same-id":
                    out.append(first)
                else:
                    out.append(self._result_message(credit, event_id=str(uuid.uuid4())))
        return out

    def _result_message(self, credit: Credit, event_id: str | None = None) -> m.Message:
        if credit.outcome == "CONFIRMED":
            event_type, payload = m.CREDIT_CONFIRMED, {"rail_ref": credit.detail}
        else:
            event_type, payload = m.CREDIT_REJECTED, {"reason_code": credit.detail}
        return m.build_message(
            event_type=event_type,
            transfer_id=credit.transfer_id,
            payload=payload,
            event_id=event_id or m.deterministic_event_id(credit.transfer_id, event_type),
            correlation_id=credit.correlation_id,
            occurred_at=credit.occurred_at,
            emitted_at=self.clock(),
        )

    # ---- decisión ---------------------------------------------------------------------

    def _decide(self, transfer_id: str) -> _Decision:
        """Todas las decisiones salen de Random(f"{seed}:{transfer_id}"), siempre en el mismo orden."""
        cfg = self.config
        rng = self.rng_factory(f"{cfg.seed}:{transfer_id}")
        lost = rng.random() < cfg.lost_rate
        rejected = rng.random() < cfg.reject_rate
        silent = rng.random() < cfg.silent_rate
        delay_s = rng.uniform(cfg.delay_min_ms, cfg.delay_max_ms) / 1000.0
        dup = rng.random() < cfg.dup_rate
        reason = rng.choice(REASON_CODES)
        rail_ref = f"RAIL-{rng.randrange(10**10):010d}"
        return _Decision(
            lost=lost,
            outcome="REJECTED" if rejected else "CONFIRMED",
            detail=reason if rejected else rail_ref,
            silent=silent,
            delay_s=delay_s,
            dup=dup,
        )
