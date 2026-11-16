from __future__ import annotations

import json
import uuid

import pytest

from tests.conftest import FakeClock
from verabank_sim.rail import messages as m
from verabank_sim.rail.processor import RailProcessor, Source

SRC = Source(m.COMMANDS_TOPIC, 2, 17)


def envelope(event_type: str, tid: str | None = None, **payload: str) -> dict:
    tid = tid or str(uuid.uuid4())
    body = {"dest_external_ref": "X-1", "dest_country": "PE", "amount": "10.00", "currency": "PEN"}
    return m.build_message(
        event_type=event_type, transfer_id=tid, payload=body if event_type == m.CREDIT_INSTRUCTION else payload,
        event_id=str(uuid.uuid4()), correlation_id=str(uuid.uuid4()),
        occurred_at="2026-11-16T10:00:00.000Z", emitted_at=1_700_000_000.0,
    ).envelope


@pytest.fixture
def processor(make_engine, schemas) -> RailProcessor:
    return RailProcessor(make_engine(delay_min_ms=10, delay_max_ms=10), schemas)


def feed(processor, value: bytes | None, key: bytes | None = b"k", headers=None):
    return processor.process(key, value, headers, SRC)


def test_instruccion_valida_responde_en_replies_con_clave_y_cabeceras(processor, clock):
    env = envelope(m.CREDIT_INSTRUCTION)
    assert feed(processor, json.dumps(env).encode()) == []
    clock.advance(1)
    (pub,) = processor.drain_due()
    assert pub.topic == m.REPLIES_TOPIC and pub.partition is None
    assert pub.key == env["payload"]["transfer_id"].encode()
    out = json.loads(pub.value)
    assert out["correlation_id"] == env["correlation_id"]
    assert dict(pub.headers)["event_type"] == out["event_type"].encode()


def test_consulta_responde_de_inmediato(processor):
    env = envelope(m.CREDIT_STATUS_QUERY)
    (pub,) = feed(processor, json.dumps(env).encode())
    assert pub.topic == m.REPLIES_TOPIC
    assert json.loads(pub.value)["payload"]["state"] == "UNKNOWN"


@pytest.mark.parametrize(
    ("value", "cause"),
    [
        (b"no es json", "JSON"),
        (b"", "vacío"),
        (None, "vacío"),
        (b"[]", "objeto"),
        (json.dumps({"event_id": "x"}).encode(), "sobre inválido"),
        (json.dumps(envelope(m.CREDIT_CONFIRMED, amount="1")).encode(), "no admitido"),
        (b"\xff\xfe", "JSON"),
    ],
)
def test_comando_invalido_va_al_dlt_con_la_causa(processor, value, cause):
    (pub,) = feed(processor, value, headers=[("event_type", b"X"), ("nulo", None)])
    assert pub.topic == m.DLT_TOPIC == "rail.commands.v1.dlt"
    assert pub.partition == 0
    assert pub.value == (value or b"") and pub.key == b"k"
    headers = dict(pub.headers)
    assert cause in headers["dlt_cause"].decode()
    assert headers["dlt_source"] == b"rail.commands.v1:2@17"
    assert headers["event_type"] == b"X" and headers["nulo"] == b""  # conserva las originales
    assert processor.invalid == 1
    assert processor.engine.counters.received == 0  # el motor no lo vio


def test_payload_invalido_va_al_dlt_y_no_deja_estado(processor):
    env = envelope(m.CREDIT_INSTRUCTION)
    env["payload"]["amount"] = "abc"
    (pub,) = feed(processor, json.dumps(env).encode())
    assert pub.topic == m.DLT_TOPIC
    assert "payload de CreditInstruction inválido" in dict(pub.headers)["dlt_cause"].decode()
    assert processor.engine.store.get(env["payload"]["transfer_id"]) is None


def test_un_defecto_en_la_respuesta_no_se_envia_al_dlt(processor, monkeypatch):
    monkeypatch.setattr(m.Schemas, "validate",
                        lambda self, e, *, allowed_types: (_ for _ in ()).throw(m.InvalidMessage("x"))
                        if allowed_types == m.REPLY_TYPES else None)
    with pytest.raises(m.InvalidMessage):
        feed(processor, json.dumps(envelope(m.CREDIT_STATUS_QUERY)).encode())
