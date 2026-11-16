"""Contratos de mensajes del rail: validación contra ``contracts/events/`` y sobre común (§3.2)."""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

COMMANDS_TOPIC = "rail.commands.v1"
REPLIES_TOPIC = "rail.replies.v1"
DLT_TOPIC = "rail.commands.v1.dlt"

CREDIT_INSTRUCTION = "CreditInstruction"
CREDIT_STATUS_QUERY = "CreditStatusQuery"
CREDIT_CANCEL = "CreditCancel"
CREDIT_CONFIRMED = "CreditConfirmed"
CREDIT_REJECTED = "CreditRejected"
CREDIT_STATUS = "CreditStatus"

COMMAND_TYPES = (CREDIT_INSTRUCTION, CREDIT_STATUS_QUERY, CREDIT_CANCEL)
REPLY_TYPES = (CREDIT_CONFIRMED, CREDIT_REJECTED, CREDIT_STATUS)

# Namespace fijo: el event_id de CreditConfirmed/CreditRejected es uuid5(transfer_id y tipo).
EVENT_ID_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "verabank/rail-simulator")


class InvalidMessage(ValueError):
    """El mensaje no es un sobre válido o su payload no cumple el esquema."""


def default_contracts_dir() -> Path:
    override = os.environ.get("VERABANK_CONTRACTS_DIR")
    if override:
        return Path(override)
    # simulator/verabank_sim/rail/messages.py -> raíz del monorepo
    return Path(__file__).resolve().parents[3] / "contracts" / "events"


def _check_date_time(value: object) -> bool:
    if not isinstance(value, str):
        return True
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return False
    return parsed.tzinfo is not None  # RFC 3339 exige zona horaria


def _format_checker() -> FormatChecker:
    # "uuid" ya viene en jsonschema; "date-time" necesita una dependencia extra, así que se
    # comprueba aquí con la biblioteca estándar.
    checker = FormatChecker()
    checker.checks("date-time")(_check_date_time)
    return checker


class Schemas:
    """Validadores del sobre y de cada payload, cargados de ``contracts/events/``."""

    def __init__(self, contracts_dir: Path | None = None) -> None:
        directory = contracts_dir or default_contracts_dir()
        checker = _format_checker()

        def load(name: str) -> Draft202012Validator:
            path = directory / name
            if not path.is_file():
                raise FileNotFoundError(f"falta el esquema {path}")
            schema = json.loads(path.read_text(encoding="utf-8"))
            Draft202012Validator.check_schema(schema)
            return Draft202012Validator(schema, format_checker=checker)

        self._envelope = load("envelope.v1.json")
        self._payloads = {t: load(f"{t}.v1.schema.json") for t in COMMAND_TYPES + REPLY_TYPES}

    def validate(self, envelope: Any, *, allowed_types: tuple[str, ...]) -> None:
        """Valida sobre y payload; lanza ``InvalidMessage`` con la causa."""
        if not isinstance(envelope, dict):
            raise InvalidMessage("el mensaje no es un objeto JSON")
        errors = sorted(self._envelope.iter_errors(envelope), key=lambda e: list(e.path))
        if errors:
            raise InvalidMessage(f"sobre inválido: {_describe(errors[0])}")
        event_type = envelope["event_type"]
        if event_type not in allowed_types:
            raise InvalidMessage(f"event_type no admitido: {event_type!r}")
        errors = sorted(self._payloads[event_type].iter_errors(envelope["payload"]), key=lambda e: list(e.path))
        if errors:
            raise InvalidMessage(f"payload de {event_type} inválido: {_describe(errors[0])}")


def _describe(error: Any) -> str:
    where = "/".join(str(p) for p in error.absolute_path) or "(raíz)"
    return f"{where}: {error.message}"[:300]


@lru_cache(maxsize=1)
def default_schemas() -> Schemas:
    return Schemas()


def deterministic_event_id(transfer_id: str, event_type: str) -> str:
    return str(uuid.uuid5(EVENT_ID_NAMESPACE, f"{transfer_id}:{event_type}"))


def iso(epoch_seconds: float) -> str:
    return datetime.fromtimestamp(epoch_seconds, UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class Message:
    """Un mensaje de salida listo para publicar (clave = transfer_id)."""

    event_id: str
    event_type: str
    transfer_id: str
    correlation_id: str
    envelope: dict[str, Any]

    @property
    def key(self) -> str:
        return self.transfer_id

    def value(self) -> bytes:
        return json.dumps(self.envelope, separators=(",", ":"), ensure_ascii=False).encode("utf-8")

    def headers(self) -> list[tuple[str, bytes]]:
        return [
            ("event_id", self.event_id.encode()),
            ("event_type", self.event_type.encode()),
            ("correlation_id", self.correlation_id.encode()),
        ]


def build_message(
    *,
    event_type: str,
    transfer_id: str,
    payload: dict[str, Any],
    event_id: str,
    correlation_id: str,
    occurred_at: str,
    emitted_at: float,
) -> Message:
    envelope = {
        "event_id": event_id,
        "event_type": event_type,
        "schema_version": 1,
        "aggregate_id": transfer_id,
        "occurred_at": occurred_at,
        "emitted_at": iso(emitted_at),
        "correlation_id": correlation_id,
        "payload": {"transfer_id": transfer_id, **payload},
    }
    return Message(event_id, event_type, transfer_id, correlation_id, envelope)
