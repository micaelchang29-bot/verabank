"""Robustez del simulador: latido, trazas con SIGUSR1, salida ante excepciones y vigilancia del latido."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import textwrap
import time
import uuid

import pytest
from confluent_kafka import KafkaError, KafkaException

from tests.conftest import FakeClock, command
from verabank_sim import cli
from verabank_sim.rail import messages as m
from verabank_sim.rail import kafka_runner
from verabank_sim.rail.heartbeat import Heartbeat
from verabank_sim.rail.kafka_runner import KafkaSettings, RailRunner
from verabank_sim.rail.processor import RailProcessor
from verabank_sim.watch import DEAD, OK, STALLED, Watcher, tail

# ---------------------------------------------------------------- latido


def test_el_latido_se_escribe_al_arrancar_y_solo_cada_intervalo(tmp_path):
    clock = FakeClock(100.0)
    hb = Heartbeat(tmp_path / "hb" / "rail.hb", interval_s=5.0, clock=clock, wall=lambda: 1234.5)
    hb.start()
    first = json.loads((tmp_path / "hb" / "rail.hb").read_text())
    assert first["ts"] == 1234.5 and first["pid"] == os.getpid()
    assert not hb.beat()                       # 0 s: aún no toca
    clock.advance(4.9)
    assert not hb.beat()
    clock.advance(0.2)
    assert hb.beat({"counters": "x"})          # 5,1 s
    assert json.loads((tmp_path / "hb" / "rail.hb").read_text())["counters"] == "x"
    assert not (tmp_path / "hb" / "rail.hb.tmp").exists()


def test_start_borra_un_latido_viejo_y_sin_archivo_no_hace_nada(tmp_path):
    path = tmp_path / "rail.hb"
    path.write_text("viejo")
    os.utime(path, (1, 1))
    Heartbeat(path).start()
    assert path.stat().st_mtime > 1_000_000
    none = Heartbeat(None)
    none.start()
    assert none.beat(force=True) is False


# ---------------------------------------------------------------- hilos, excepciones y SIGUSR1

_CHILD = textwrap.dedent(
    """
    import logging, sys, threading, time
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    from verabank_sim import crash
    crash.install()
    mode = sys.argv[1]
    if mode == "thread":
        def boom():
            raise ValueError("falla en un hilo")
        threading.Thread(target=boom, name="envios").start()
        time.sleep(30)
    elif mode == "main":
        raise RuntimeError("falla en el principal")
    else:
        def busy():
            while True:
                time.sleep(0.05)
        threading.Thread(target=busy, name="hilo-ocupado", daemon=True).start()
        print("LISTO", flush=True)
        time.sleep(30)
    """
)


def _spawn(mode: str) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-c", _CHILD, mode], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, env={**os.environ, "PYTHONPATH": os.getcwd()})


@pytest.mark.parametrize(("mode", "needle"), [("thread", "falla en un hilo"), ("main", "falla en el principal")])
def test_una_excepcion_no_capturada_se_registra_con_traza_y_sale_con_codigo_1(mode, needle):
    proc = _spawn(mode)
    _, err = proc.communicate(timeout=20)
    assert proc.returncode == 1
    assert needle in err and "Traceback" in err


@pytest.mark.skipif(not hasattr(signal, "SIGUSR1"), reason="solo POSIX")
def test_sigusr1_vuelca_las_trazas_de_todos_los_hilos_sin_matar_el_proceso():
    proc = _spawn("wait")
    assert proc.stdout.readline().strip() == "LISTO"
    proc.send_signal(signal.SIGUSR1)
    time.sleep(1.0)
    assert proc.poll() is None, "SIGUSR1 no debe matar el proceso"
    proc.terminate()
    _, err = proc.communicate(timeout=20)
    assert "Thread 0x" in err and "busy" in err and "most recent call first" in err


# ---------------------------------------------------------------- bucle del runner con fakes


@pytest.fixture
def runner(make_engine, schemas, tmp_path):
    processor = RailProcessor(make_engine(delay_min_ms=1, delay_max_ms=1), schemas)
    r = RailRunner(processor, KafkaSettings("localhost:1", heartbeat_file=tmp_path / "rail.hb"))
    r.consumer.close()
    return r


class FakeMessage:
    def __init__(self, value: bytes, offset: int = 0) -> None:
        self._value, self._offset = value, offset

    def error(self):
        return None

    def topic(self):
        return m.COMMANDS_TOPIC

    def partition(self):
        return 0

    def offset(self):
        return self._offset

    def key(self):
        return b"k"

    def value(self):
        return self._value

    def headers(self):
        return []


class FakeConsumer:
    def __init__(self, messages=(), commit_error: KafkaException | None = None) -> None:
        self.messages = list(messages)
        self.commit_error = commit_error
        self.commits = 0
        self.closed = False

    def subscribe(self, *a, **k):
        pass

    def poll(self, timeout):
        return self.messages.pop(0) if self.messages else None

    def commit(self, *a, **k):
        self.commits += 1
        if self.commit_error:
            raise self.commit_error

    def close(self):
        self.closed = True


class FakeProducer:
    def __init__(self, buffer_full: bool = False, flush_left: int = 0) -> None:
        self.buffer_full, self.flush_left, self.sent = buffer_full, flush_left, []

    def produce(self, topic, **kw):
        if self.buffer_full:
            raise BufferError("cola llena")
        self.sent.append((topic, kw))

    def poll(self, t):
        pass

    def flush(self, t):
        return self.flush_left

    def __len__(self):
        return 1000


def _instruction() -> bytes:
    cmd = command(m.CREDIT_INSTRUCTION)
    envelope = m.build_message(event_type=m.CREDIT_INSTRUCTION, transfer_id=cmd.transfer_id,
                               payload={"transfer_id": cmd.transfer_id, "amount": "10.00", "currency": "PEN",
                                        "dest_country": "PE", "dest_external_ref": "EXT-1"},
                               event_id=str(uuid.uuid4()), correlation_id=cmd.correlation_id,
                               occurred_at="2026-11-16T10:00:00Z", emitted_at=1.0).envelope
    return json.dumps(envelope).encode()


def test_la_cola_llena_del_productor_falla_con_mensaje_en_vez_de_esperar_para_siempre(runner):
    runner.producer = FakeProducer(buffer_full=True)
    runner.produce_timeout_s = 0.3
    msg = runner.processor.process(None, _instruction(), [], kafka_runner.Source("t", 0, 0))
    assert msg == []   # una instrucción no responde de inmediato: se prueba con una consulta
    query = m.build_message(event_type=m.CREDIT_STATUS_QUERY, transfer_id=str(uuid.uuid4()),
                            payload={"transfer_id": str(uuid.uuid4())}, event_id=str(uuid.uuid4()),
                            correlation_id=str(uuid.uuid4()), occurred_at="2026-11-16T10:00:00Z", emitted_at=1.0)
    publishes = [runner.processor._reply(m.build_message(
        event_type=m.CREDIT_STATUS, transfer_id=query.envelope["payload"]["transfer_id"], payload={"state": "UNKNOWN"},
        event_id=str(uuid.uuid4()), correlation_id=str(uuid.uuid4()), occurred_at="2026-11-16T10:00:00Z",
        emitted_at=1.0))]
    start = time.monotonic()
    with pytest.raises(RuntimeError, match="cola del productor sigue llena"):
        runner._publish_all(publishes)
    assert time.monotonic() - start < 5


def test_un_flush_que_deja_mensajes_sin_entregar_no_confirma_el_offset(runner):
    runner.consumer = FakeConsumer([FakeMessage(_query_bytes())])
    runner.producer = FakeProducer(flush_left=3)
    with pytest.raises(RuntimeError, match="sin entregar"):
        runner._handle(runner.consumer.poll(0))
    assert runner.consumer.commits == 0


def _query_bytes() -> bytes:
    tid = str(uuid.uuid4())
    return json.dumps(m.build_message(
        event_type=m.CREDIT_STATUS_QUERY, transfer_id=tid, payload={"transfer_id": tid}, event_id=str(uuid.uuid4()),
        correlation_id=str(uuid.uuid4()), occurred_at="2026-11-16T10:00:00Z", emitted_at=1.0).envelope).encode()


def _kafka_error(code: int) -> KafkaException:
    return KafkaException(KafkaError(code))


def test_un_commit_fallido_por_rebalanceo_se_tolera_y_otro_error_detiene(runner):
    producer = FakeProducer()
    runner.producer = producer
    runner.consumer = FakeConsumer(commit_error=_kafka_error(KafkaError.REBALANCE_IN_PROGRESS))
    runner._handle(FakeMessage(_query_bytes()))          # no lanza: el mensaje se reentregará
    assert runner.consumer.commits == 1 and producer.sent
    runner.consumer = FakeConsumer(commit_error=_kafka_error(KafkaError._FATAL))
    with pytest.raises(KafkaException):
        runner._handle(FakeMessage(_query_bytes()))


def test_una_excepcion_en_el_bucle_se_registra_con_traza_cierra_limpio_y_devuelve_1(runner, caplog):
    consumer = FakeConsumer()

    def boom(timeout):
        raise RuntimeError("poll roto")

    consumer.poll = boom
    runner.consumer = consumer
    runner.producer = FakeProducer()
    with caplog.at_level("ERROR"):
        assert runner.run() == 1
    assert consumer.closed, "debe salir del grupo aunque falle"
    assert any("fallo fatal" in r.getMessage() and r.exc_info for r in caplog.records)


def test_el_bucle_escribe_el_latido_mientras_avanza(runner, tmp_path):
    consumer = FakeConsumer()
    polls = {"n": 0}

    def poll(timeout):
        polls["n"] += 1
        if polls["n"] >= 3:
            runner.stop()
        return None

    consumer.poll = poll
    runner.consumer = consumer
    runner.producer = FakeProducer()
    assert runner.run() == 0
    assert json.loads((tmp_path / "rail.hb").read_text())["pid"] == os.getpid()


def test_rail_acepta_heartbeat_file():
    args = cli.build_parser().parse_args(["rail", "--heartbeat-file", "/tmp/x.hb"])
    assert str(args.heartbeat_file) == "/tmp/x.hb"
    assert cli.build_parser().parse_args(["rail"]).heartbeat_file is None


# ---------------------------------------------------------------- vigilancia con un latido falso


def _watcher(tmp_path, alive=True, max_silence=30.0):
    hb = tmp_path / "rail.hb"
    now = {"t": 10_000.0}
    w = Watcher(hb, sim_pid=4242, max_silence_s=max_silence, clock=lambda: now["t"], alive=lambda pid: alive)
    return w, hb, now


def _beat(hb, at):
    hb.write_text("{}")
    os.utime(hb, (at, at))


def test_con_latido_reciente_el_simulador_esta_sano(tmp_path):
    w, hb, now = _watcher(tmp_path)
    _beat(hb, now["t"] - 4)
    assert w.check().state == OK


def test_mas_de_30_segundos_sin_latido_es_bloqueo(tmp_path):
    w, hb, now = _watcher(tmp_path)
    _beat(hb, now["t"] - 29)
    assert w.check().state == OK
    _beat(hb, now["t"] - 31)
    verdict = w.check()
    assert verdict.state == STALLED and "31" in verdict.detail


def test_un_latido_que_se_detiene_se_detecta_al_pasar_el_tiempo(tmp_path):
    w, hb, now = _watcher(tmp_path)
    for _ in range(5):                      # el simulador late cada 5 s
        _beat(hb, now["t"])
        now["t"] += 5
        assert w.check().healthy
    now["t"] += 26                          # y deja de latir: 5 + 26 = 31 s
    assert w.check().state == STALLED


def test_proceso_muerto_se_distingue_del_bloqueo(tmp_path):
    w, hb, now = _watcher(tmp_path, alive=False)
    _beat(hb, now["t"])
    assert w.check().state == DEAD


def test_sin_latido_hay_gracia_y_despues_falla(tmp_path):
    w, _hb, now = _watcher(tmp_path)
    assert w.check().state == OK
    now["t"] += 31
    assert w.check().state == STALLED


def test_tail_devuelve_las_ultimas_lineas(tmp_path):
    log = tmp_path / "sim.log"
    log.write_text("\n".join(f"l{i}" for i in range(100)) + "\n")
    assert tail(log, 30) == [f"l{i}" for i in range(70, 100)]
    assert "no se pudo leer" in tail(tmp_path / "no-existe", 3)[0]


def test_watch_sale_con_3_envia_sigusr1_y_detiene_chaos(tmp_path):
    """Extremo a extremo con procesos reales: un 'simulador' que no late y un 'chaos' que espera."""
    sim = subprocess.Popen([sys.executable, "-c", textwrap.dedent(
        """
        import faulthandler, signal, sys, time
        faulthandler.register(signal.SIGUSR1, file=sys.stderr, all_threads=True)
        time.sleep(60)
        """)], stderr=open(tmp_path / "sim.log", "w"), start_new_session=True)
    chaos = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
    hb = tmp_path / "rail.hb"
    hb.write_text("{}")
    os.utime(hb, (time.time() - 100, time.time() - 100))   # latido viejo: bloqueado
    try:
        time.sleep(0.5)   # que faulthandler esté registrado
        out = subprocess.run(
            [sys.executable, "-m", "verabank_sim", "watch", "--sim-pid", str(sim.pid), "--target-pid", str(chaos.pid),
             "--heartbeat-file", str(hb), "--log", str(tmp_path / "sim.log"), "--max-silence", "30", "--interval", "0.2"],
            capture_output=True, text=True, timeout=60, env={**os.environ, "PYTHONPATH": os.getcwd()})
        assert out.returncode == 3
        assert "bloqueado" in out.stderr and "últimas 30 líneas" in out.stderr
        assert "most recent call first" in (tmp_path / "sim.log").read_text(), "SIGUSR1 debía volcar las trazas en el log"
        chaos.wait(timeout=10)
        assert chaos.returncode is not None, "chaos debía detenerse"
        assert sim.poll() is None, "el simulador sigue vivo tras SIGUSR1"
    finally:
        for p in (sim, chaos):
            if p.poll() is None:
                p.kill()
