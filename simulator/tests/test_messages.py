from __future__ import annotations

import copy
import json
import uuid

import pytest

from tests.conftest import command
from verabank_sim.rail import messages as m
from verabank_sim.rail.engine import REASON_CODES
from verabank_sim.rail.messages import InvalidMessage


def valid_instruction() -> dict:
    tid = str(uuid.uuid4())
    return m.build_message(
        event_type=m.CREDIT_INSTRUCTION,
        transfer_id=tid,
        payload={"dest_external_ref": "ACME-001", "dest_country": "PE", "amount": "1250.00", "currency": "PEN"},
        event_id=str(uuid.uuid4()),
        correlation_id=str(uuid.uuid4()),
        occurred_at="2026-11-16T10:00:00.000Z",
        emitted_at=1_700_000_000.0,
    ).envelope


def test_todo_mensaje_de_salida_valida_contra_los_esquemas(make_engine, clock, schemas):
    engine = make_engine(reject_rate=0.5, dup_rate=1.0, dup_mode="new-id", delay_min_ms=10, delay_max_ms=10)
    out = []
    for i in range(50):
        tid = str(uuid.UUID(int=i))
        engine.handle(command(m.CREDIT_INSTRUCTION, tid))
        out += engine.handle(command(m.CREDIT_STATUS_QUERY, tid))
        out += engine.handle(command(m.CREDIT_CANCEL, str(uuid.UUID(int=1000 + i))))
    clock.advance(1)
    out += engine.due()
    out += engine.handle(command(m.CREDIT_CANCEL, str(uuid.UUID(int=3))))  # ya venció: resultado real
    assert {x.event_type for x in out} == {m.CREDIT_STATUS, m.CREDIT_CONFIRMED, m.CREDIT_REJECTED}
    for msg in out:
        schemas.validate(json.loads(msg.value()), allowed_types=m.REPLY_TYPES)
        assert {k for k, _ in msg.headers()} == {"event_id", "event_type", "correlation_id"}


def test_reason_codes_validos(schemas):
    for code in REASON_CODES:
        msg = m.build_message(
            event_type=m.CREDIT_REJECTED, transfer_id=str(uuid.uuid4()), payload={"reason_code": code},
            event_id=str(uuid.uuid4()), correlation_id=str(uuid.uuid4()),
            occurred_at="2026-11-16T10:00:00Z", emitted_at=1.0,
        )
        schemas.validate(msg.envelope, allowed_types=m.REPLY_TYPES)


def test_instruccion_valida_pasa(schemas):
    schemas.validate(valid_instruction(), allowed_types=m.COMMAND_TYPES)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda e: e.pop("correlation_id"),
        lambda e: e.update(schema_version=2),
        lambda e: e.update(event_id="no-es-uuid"),
        lambda e: e.update(occurred_at="ayer"),
        lambda e: e.update(extra="x"),
        lambda e: e.update(event_type="Desconocido"),
        lambda e: e.update(event_type=m.CREDIT_CONFIRMED),  # no es un comando
        lambda e: e["payload"].pop("amount"),
        lambda e: e["payload"].update(amount="12.345"),
        lambda e: e["payload"].update(dest_country="peru"),
    ],
)
def test_comando_invalido_se_rechaza(schemas, mutate):
    envelope = copy.deepcopy(valid_instruction())
    mutate(envelope)
    with pytest.raises(InvalidMessage):
        schemas.validate(envelope, allowed_types=m.COMMAND_TYPES)


def test_no_objeto_se_rechaza(schemas):
    with pytest.raises(InvalidMessage):
        schemas.validate([1, 2], allowed_types=m.COMMAND_TYPES)
