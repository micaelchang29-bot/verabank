"""Etiquetas de fraude (``FraudLabelConfirmed``) en ``fraud.labels.v1`` al terminar el replay.

``occurred_at`` es el de la transferencia más 24 h simuladas (el retraso de §1.3): tiempo de negocio, no se espera.
``event_id`` y ``correlation_id`` son deterministas, así que repetir el replay no duplica etiquetas (inbox).
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Sequence
from datetime import datetime, timedelta

from verabank_sim.rail import messages as rail
from verabank_sim.replay.mapping import NS

TOPIC = "fraud.labels.v1"
EVENT_TYPE = "FraudLabelConfirmed"
LABEL_SOURCE = "SIMULATOR"
LABEL_DELAY = timedelta(hours=24)


def label_time(occurred_at: str) -> str:
    moment = datetime.fromisoformat(occurred_at.replace("Z", "+00:00")) + LABEL_DELAY
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def build(transfer_id: str, is_fraud: bool, occurred_at: str, *, emitted_at: float) -> rail.Message:
    return rail.build_message(
        event_type=EVENT_TYPE,
        transfer_id=transfer_id,
        payload={"is_fraud": is_fraud, "label_source": LABEL_SOURCE},
        event_id=rail.deterministic_event_id(transfer_id, EVENT_TYPE),
        correlation_id=str(uuid.uuid5(NS, f"label:{transfer_id}")),
        occurred_at=label_time(occurred_at),
        emitted_at=emitted_at,
    )


def build_all(items: Sequence[tuple[str, bool, str]], *, emitted_at: float | None = None) -> list[rail.Message]:
    stamp = time.time() if emitted_at is None else emitted_at
    return [build(tid, fraud, at, emitted_at=stamp) for tid, fraud, at in items]


class KafkaPublisher:
    """Publica con ``confluent_kafka``; falla si algún mensaje no se entrega."""

    def __init__(self, bootstrap_servers: str) -> None:
        from confluent_kafka import Producer

        self._failures: list[str] = []
        self._producer = Producer({"bootstrap.servers": bootstrap_servers, "enable.idempotence": True,
                                   "acks": "all", "linger.ms": 20})

    def _delivered(self, error: object, _message: object) -> None:
        if error is not None:
            self._failures.append(str(error))

    def publish(self, messages: Sequence[rail.Message]) -> int:
        for message in messages:
            while True:
                try:
                    self._producer.produce(TOPIC, key=message.key, value=message.value(), headers=message.headers(),
                                           on_delivery=self._delivered)
                    break
                except BufferError:
                    self._producer.poll(0.2)
            self._producer.poll(0)
        remaining = self._producer.flush(60)
        if remaining or self._failures:
            raise RuntimeError(f"{remaining} mensajes sin entregar; fallos: {self._failures[:3]}")
        return len(messages)
