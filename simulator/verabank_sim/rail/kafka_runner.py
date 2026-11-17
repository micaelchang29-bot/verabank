"""Adaptador Kafka delgado: lee ``rail.commands.v1``, publica en ``rail.replies.v1`` y el DLT."""

from __future__ import annotations

import logging
import signal
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from confluent_kafka import Consumer, KafkaError, KafkaException, Producer

from verabank_sim import crash
from verabank_sim.rail import messages as m
from verabank_sim.rail.heartbeat import Heartbeat
from verabank_sim.rail.processor import Publish, RailProcessor, Source

log = logging.getLogger("verabank_sim.rail")

STATS_INTERVAL_S = 10.0
MAX_POLL_S = 0.2
PRODUCE_TIMEOUT_S = 60.0   # cola del productor llena (BufferError) durante más que esto: fallo, no espera eterna
FLUSH_TIMEOUT_S = 30.0

# Errores de commit que son parte de un rebalanceo: el mensaje se reentregará y el motor es idempotente
# (una instrucción ya decidida no se decide otra vez), así que no merecen detener el simulador.
_TRANSIENT_COMMIT = {
    getattr(KafkaError, name)
    for name in ("REBALANCE_IN_PROGRESS", "ILLEGAL_GENERATION", "UNKNOWN_MEMBER_ID", "_WAIT_COORD",
                 "COORDINATOR_LOAD_IN_PROGRESS", "NOT_COORDINATOR")
    if hasattr(KafkaError, name)
}


@dataclass(frozen=True)
class KafkaSettings:
    bootstrap_servers: str
    group_id: str = "rail-simulator"
    offset_reset: str = "earliest"
    ready_file: Path | None = None
    heartbeat_file: Path | None = None


class RailRunner:
    def __init__(self, processor: RailProcessor, settings: KafkaSettings) -> None:
        self.processor = processor
        self.settings = settings
        self._stop = threading.Event()
        self._ready = False
        self._delivery_failures = 0
        self.heartbeat = Heartbeat(settings.heartbeat_file)
        self.produce_timeout_s = PRODUCE_TIMEOUT_S
        self.consumer = Consumer(
            {
                "error_cb": self._on_client_error,
                "bootstrap.servers": settings.bootstrap_servers,
                "group.id": settings.group_id,
                "enable.auto.commit": False,
                "auto.offset.reset": settings.offset_reset,
                "allow.auto.create.topics": False,  # no crea tópicos
                "enable.partition.eof": False,
            }
        )
        self.producer = Producer(
            {
                "bootstrap.servers": settings.bootstrap_servers,
                "client.id": "rail-simulator",
                "error_cb": self._on_client_error,
                "enable.idempotence": True,
                "acks": "all",
            }
        )

    # ---- ciclo de vida ----------------------------------------------------------------

    def stop(self, *_: object) -> None:
        self._stop.set()

    def run(self) -> int:
        crash.install()
        signal.signal(signal.SIGINT, self.stop)
        signal.signal(signal.SIGTERM, self.stop)
        ready_file = self.settings.ready_file
        if ready_file:
            ready_file.unlink(missing_ok=True)  # un archivo viejo no debe contar como READY
        pending = self.processor.engine.recover()
        if pending:
            log.info("reprogramados %d envíos pendientes desde el estado", pending)
        self.consumer.subscribe([m.COMMANDS_TOPIC], on_assign=self._on_assign, on_revoke=self._on_revoke,
                                on_lost=self._on_lost)
        self.heartbeat.start()
        exit_code = 0
        next_stats = time.monotonic() + STATS_INTERVAL_S
        try:
            while not self._stop.is_set():
                message = self.consumer.poll(self._poll_timeout())
                if message is not None:
                    self._handle(message)
                self._publish_all(self.processor.drain_due())
                self.producer.poll(0)
                self._check_delivery()
                if time.monotonic() >= next_stats:
                    log.info("contadores: %s", self.processor.engine.counters.summary())
                    next_stats = time.monotonic() + STATS_INTERVAL_S
                # Solo se llega aquí si el consumo y los envíos de esta vuelta terminaron.
                self.heartbeat.beat({"counters": self.processor.engine.counters.summary()})
        except Exception:  # noqa: BLE001 - cualquier fallo se registra con traza y termina con código 1
            log.exception("fallo fatal del simulador; se detiene")
            exit_code = crash.EXIT_CODE
        finally:
            self._shutdown()
        return exit_code

    def _shutdown(self) -> None:
        log.info("cerrando: vaciando el productor y confirmando offsets")
        steps = (
            ("vaciar el productor", lambda: self._flush(10)),
            ("confirmar offsets", self._commit_on_close),
            ("cerrar el consumidor", self.consumer.close),
            ("cerrar el estado", self.processor.engine.store.close),
        )
        for name, step in steps:   # un paso que falla no impide los siguientes (sobre todo, salir del grupo)
            try:
                step()
            except Exception:  # noqa: BLE001
                log.exception("no se pudo %s al cerrar", name)
        log.info("contadores finales: %s", self.processor.engine.counters.summary())

    def _commit_on_close(self) -> None:
        try:
            self.consumer.commit(asynchronous=False)
        except KafkaException as exc:
            if exc.args[0].code() != KafkaError._NO_OFFSET:
                log.warning("no se pudo confirmar offsets al cerrar: %s", exc)

    def _flush(self, timeout_s: float) -> None:
        remaining = self.producer.flush(timeout_s)
        if remaining:
            raise RuntimeError(f"quedaron {remaining} mensajes sin entregar tras {timeout_s:.0f} s")

    # ---- piezas -----------------------------------------------------------------------

    def _poll_timeout(self) -> float:
        due = self.processor.engine.next_due_at()
        if due is None:
            return MAX_POLL_S
        return max(0.0, min(MAX_POLL_S, due - self.processor.engine.clock()))

    def _on_assign(self, _consumer: Consumer, partitions: list) -> None:
        log.info("asignadas %d particiones de %s", len(partitions), m.COMMANDS_TOPIC)
        if self._ready:
            return
        self._ready = True
        log.info("READY grupo=%s topico=%s", self.settings.group_id, m.COMMANDS_TOPIC)
        if self.settings.ready_file:
            self.settings.ready_file.parent.mkdir(parents=True, exist_ok=True)
            self.settings.ready_file.write_text("READY\n", encoding="utf-8")

    def _on_revoke(self, _consumer: Consumer, partitions: list) -> None:
        log.warning("rebalanceo: revocadas %d particiones", len(partitions))

    def _on_lost(self, _consumer: Consumer, partitions: list) -> None:
        log.warning("rebalanceo: perdidas %d particiones (sin confirmar offsets)", len(partitions))

    @staticmethod
    def _on_client_error(err: KafkaError) -> None:
        log.warning("error del cliente de Kafka: %s", err)

    def _handle(self, message) -> None:
        if message.error():
            if message.error().fatal():
                raise KafkaException(message.error())
            log.warning("error del consumidor: %s", message.error())
            return
        source = Source(message.topic(), message.partition(), message.offset())
        publishes = self.processor.process(message.key(), message.value(), message.headers(), source)
        for publish in publishes:
            if publish.topic == m.DLT_TOPIC:
                log.warning("comando inválido a %s (%s): %s", m.DLT_TOPIC, source, _header(publish, "dlt_cause"))
        self._publish_all(publishes)
        # El estado ya está en SQLite (el motor lo guarda antes de devolver): ahora se
        # entregan las respuestas y solo entonces se confirma el offset.
        if publishes:
            self._flush(FLUSH_TIMEOUT_S)   # si no se entregaron, no se confirma el offset: falla en vez de seguir
            self._check_delivery()
        try:
            self.consumer.commit(message=message, asynchronous=False)
        except KafkaException as exc:
            if exc.args[0].code() not in _TRANSIENT_COMMIT:
                raise
            log.warning("commit omitido por un rebalanceo (%s); el mensaje se reentregará", exc.args[0])

    def _publish_all(self, publishes: list[Publish]) -> None:
        for publish in publishes:
            kwargs = {"partition": publish.partition} if publish.partition is not None else {}
            deadline = time.monotonic() + self.produce_timeout_s
            while True:
                try:
                    self.producer.produce(
                        publish.topic,
                        key=publish.key,
                        value=publish.value,
                        headers=publish.headers,
                        on_delivery=self._on_delivery,
                        **kwargs,
                    )
                    break
                except BufferError:
                    if time.monotonic() >= deadline:
                        raise RuntimeError(
                            f"la cola del productor sigue llena tras {self.produce_timeout_s:.0f} s "
                            f"(broker inaccesible o lento): {len(self.producer)} mensajes en cola") from None
                    log.warning("cola del productor llena; se espera a que se vacíe")
                    self.producer.poll(0.5)

    def _on_delivery(self, err, msg) -> None:
        if err is not None:
            self._delivery_failures += 1
            log.error("falló la entrega a %s: %s", msg.topic(), err)

    def _check_delivery(self) -> None:
        if self._delivery_failures:
            raise RuntimeError(f"{self._delivery_failures} entregas fallaron; se detiene sin confirmar")


def _header(publish: Publish, name: str) -> str:
    return next((v.decode(errors="replace") for k, v in publish.headers if k == name), "")
