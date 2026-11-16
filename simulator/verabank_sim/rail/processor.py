"""Procesamiento de registros de ``rail.commands.v1``: validación, motor y enrutado (sin Kafka)."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field

from verabank_sim.rail import messages as m
from verabank_sim.rail.engine import Command, RailEngine

Headers = list[tuple[str, bytes]]


@dataclass(frozen=True)
class Publish:
    """Un registro a publicar. ``partition`` es ``None`` salvo en el DLT (una partición)."""

    topic: str
    key: bytes | None
    value: bytes
    headers: Headers = field(default_factory=list)
    partition: int | None = None


@dataclass(frozen=True)
class Source:
    topic: str
    partition: int
    offset: int


class RailProcessor:
    def __init__(self, engine: RailEngine, schemas: m.Schemas) -> None:
        self.engine = engine
        self.schemas = schemas
        self.invalid = 0

    def process(
        self,
        key: bytes | None,
        value: bytes | None,
        headers: Sequence[tuple[str, bytes | None]] | None,
        source: Source,
    ) -> list[Publish]:
        """Un comando válido pasa al motor; uno inválido va al DLT con la causa en una cabecera."""
        try:
            command = self._parse(value)
        except m.InvalidMessage as exc:
            return [self._dead_letter(key, value, headers, source, str(exc))]
        return [self._reply(msg) for msg in self.engine.handle(command)]

    def drain_due(self) -> list[Publish]:
        return [self._reply(msg) for msg in self.engine.due()]

    # ---- internos ---------------------------------------------------------------------

    def _parse(self, value: bytes | None) -> Command:
        if not value:
            raise m.InvalidMessage("mensaje vacío")
        try:
            envelope = json.loads(value)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise m.InvalidMessage(f"no es JSON válido: {exc}") from exc
        self.schemas.validate(envelope, allowed_types=m.COMMAND_TYPES)
        return Command(
            event_type=envelope["event_type"],
            transfer_id=envelope["payload"]["transfer_id"],
            correlation_id=envelope["correlation_id"],
            occurred_at=envelope["occurred_at"],
        )

    def _reply(self, message: m.Message) -> Publish:
        # Un error aquí es un defecto del simulador, no del comando: se propaga.
        self.schemas.validate(message.envelope, allowed_types=m.REPLY_TYPES)
        return Publish(
            topic=m.REPLIES_TOPIC,
            key=message.key.encode(),
            value=message.value(),
            headers=message.headers(),
        )

    def _dead_letter(
        self,
        key: bytes | None,
        value: bytes | None,
        headers: Sequence[tuple[str, bytes | None]] | None,
        source: Source,
        cause: str,
    ) -> Publish:
        self.invalid += 1
        kept: Headers = [(k, v if v is not None else b"") for k, v in (headers or [])]
        kept += [
            ("dlt_cause", cause.encode()),
            ("dlt_source", f"{source.topic}:{source.partition}@{source.offset}".encode()),
        ]
        return Publish(m.DLT_TOPIC, key, value or b"", kept, partition=0)
