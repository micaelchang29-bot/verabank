"""Integración con Kafka real (``make test-rail-it``, es decir ``pytest -m integration``).

Requiere el broker del perfil core con sus tópicos (``make up-core``). Si no está disponible la
prueba FALLA: una integración omitida en silencio no prueba nada.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest
from confluent_kafka import Consumer, Producer, TopicPartition
from confluent_kafka.admin import AdminClient

from verabank_sim.rail import messages as m
from verabank_sim.rail.cli import default_bootstrap

pytestmark = pytest.mark.integration

SIM_DIR = Path(__file__).resolve().parents[1]
TIMEOUT_S = 45


@pytest.fixture(scope="module")
def bootstrap() -> str:
    servers = default_bootstrap()
    try:
        topics = AdminClient({"bootstrap.servers": servers}).list_topics(timeout=10).topics
    except Exception as exc:  # noqa: BLE001
        pytest.fail(f"Kafka no responde en {servers} (¿make up-core?): {exc}")
    missing = {m.COMMANDS_TOPIC, m.REPLIES_TOPIC, m.DLT_TOPIC} - set(topics)
    if missing:
        pytest.fail(f"faltan los tópicos {sorted(missing)} en {servers} (¿make up-core?)")
    return servers


@pytest.fixture(scope="module")
def rail(bootstrap: str, tmp_path_factory: pytest.TempPathFactory):
    tmp = tmp_path_factory.mktemp("rail")
    ready, log_path = tmp / "ready", tmp / "rail.log"
    group_id = f"rail-it-{uuid.uuid4()}"  # no interfiere con el grupo real
    with log_path.open("wb") as log:
        proc = subprocess.Popen(
            [
                sys.executable, "-m", "verabank_sim", "rail",
                "--bootstrap-servers", bootstrap,
                "--group-id", group_id,
                "--offset-reset", "latest",
                "--state-db", str(tmp / "rail.db"),
                "--ready-file", str(ready),
                "--delay-ms", "20:50", "--reject-rate", "0",
            ],
            cwd=SIM_DIR, stdout=log, stderr=subprocess.STDOUT,
        )
        try:
            deadline = time.monotonic() + 60
            while not ready.exists():
                if proc.poll() is not None or time.monotonic() > deadline:
                    pytest.fail(f"el rail no quedó READY:\n{log_path.read_text(errors='replace')[-3000:]}")
                time.sleep(0.2)
            yield proc
        finally:
            proc.terminate()  # SIGTERM: cierre ordenado
            try:
                assert proc.wait(timeout=20) == 0, log_path.read_text(errors="replace")[-3000:]
            except subprocess.TimeoutExpired:
                proc.kill()
                pytest.fail("el rail no cerró con SIGTERM")
            finally:
                for future in AdminClient({"bootstrap.servers": bootstrap}).delete_consumer_groups([group_id]).values():
                    try:
                        future.result(timeout=10)
                    except Exception:  # noqa: BLE001  # limpieza de mejor esfuerzo
                        pass


class Reader:
    """Lee un tópico desde su final actual (sin depender de offsets de grupo)."""

    def __init__(self, bootstrap: str, topic: str) -> None:
        self.consumer = Consumer({"bootstrap.servers": bootstrap, "group.id": f"it-reader-{uuid.uuid4()}",
                                  "enable.auto.commit": False})
        meta = self.consumer.list_topics(topic, timeout=10).topics[topic]
        ends = []
        for partition in meta.partitions:
            _, high = self.consumer.get_watermark_offsets(TopicPartition(topic, partition), timeout=10)
            ends.append(TopicPartition(topic, partition, high))
        self.consumer.assign(ends)

    def wait_for(self, transfer_id: str, *, resend=None):
        deadline, last_send = time.monotonic() + TIMEOUT_S, 0.0
        while time.monotonic() < deadline:
            if resend and time.monotonic() - last_send > 3:
                resend()
                last_send = time.monotonic()
            msg = self.consumer.poll(0.5)
            if msg is not None and not msg.error() and msg.key() and msg.key().decode() == transfer_id:
                return msg
        pytest.fail(f"no llegó ningún registro de {transfer_id} en {TIMEOUT_S} s")

    def close(self) -> None:
        self.consumer.close()


def instruction(transfer_id: str) -> tuple[dict, str]:
    correlation_id = str(uuid.uuid4())
    msg = m.build_message(
        event_type=m.CREDIT_INSTRUCTION, transfer_id=transfer_id,
        payload={"dest_external_ref": "ACME-001", "dest_country": "PE", "amount": "1250.00", "currency": "PEN"},
        event_id=str(uuid.uuid4()), correlation_id=correlation_id,
        occurred_at="2026-11-16T10:00:00.000Z", emitted_at=time.time(),
    )
    return msg.envelope, correlation_id


def test_instruccion_en_respuesta_en_replies(bootstrap: str, rail, schemas: m.Schemas) -> None:
    transfer_id = str(uuid.uuid4())
    envelope, correlation_id = instruction(transfer_id)
    reader = Reader(bootstrap, m.REPLIES_TOPIC)
    producer = Producer({"bootstrap.servers": bootstrap, "enable.idempotence": True})

    def send() -> None:
        # La instrucción es idempotente: reenviarla mientras el grupo termina de asignarse no cambia el resultado.
        producer.produce(m.COMMANDS_TOPIC, key=transfer_id, value=json.dumps(envelope))
        producer.flush(10)

    try:
        reply = reader.wait_for(transfer_id, resend=send)
    finally:
        reader.close()
    body = json.loads(reply.value())
    schemas.validate(body, allowed_types=m.REPLY_TYPES)
    assert body["event_type"] == m.CREDIT_CONFIRMED
    assert body["correlation_id"] == correlation_id and body["aggregate_id"] == transfer_id
    assert body["payload"]["rail_ref"]
    headers = {k: v.decode() for k, v in reply.headers()}
    assert headers == {"event_id": body["event_id"], "event_type": m.CREDIT_CONFIRMED,
                       "correlation_id": correlation_id}
    assert body["event_id"] == m.deterministic_event_id(transfer_id, m.CREDIT_CONFIRMED)


def test_comando_invalido_llega_al_dlt(bootstrap: str, rail) -> None:
    transfer_id = str(uuid.uuid4())
    reader = Reader(bootstrap, m.DLT_TOPIC)
    producer = Producer({"bootstrap.servers": bootstrap, "enable.idempotence": True})

    def send() -> None:
        producer.produce(m.COMMANDS_TOPIC, key=transfer_id, value=b'{"no": "es un sobre"}')
        producer.flush(10)

    try:
        record = reader.wait_for(transfer_id, resend=send)
    finally:
        reader.close()
    headers = {k: v.decode() for k, v in record.headers()}
    assert "sobre inválido" in headers["dlt_cause"]
    assert record.value() == b'{"no": "es un sobre"}'
