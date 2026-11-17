"""Infraestructura común de las cargas: estadísticos, motor con barrera, cliente HTTP, psql y SQL de siembra."""

from __future__ import annotations

import subprocess
import threading
import time
from decimal import Decimal

import httpx
import pytest

from verabank_sim.load import db, finalstate, seed_sql
from verabank_sim.load.api import HttpApi, api_credentials_from_env
from verabank_sim.load.engine import run_groups
from verabank_sim.load.stats import describe, percentile


# --- estadísticos -----------------------------------------------------------------------------------------------------


def test_percentil_por_rango_mas_cercano() -> None:
    values = [float(i) for i in range(1, 101)]
    assert (percentile(values, 50), percentile(values, 95), percentile(values, 99)) == (50.0, 95.0, 99.0)
    assert percentile([7.0], 99) == 7.0
    assert describe([]) == {"n": 0, "p50": None, "p95": None, "p99": None}
    assert describe([3, 1, 2]) == {"n": 3, "p50": 2, "p95": 3, "p99": 3}


# --- barrera por grupo (D11) ------------------------------------------------------------------------------------------


def test_ninguna_solicitud_del_paso_siguiente_empieza_antes_de_que_terminen_las_del_anterior() -> None:
    spans: dict[int, list[tuple[float, float]]] = {}
    lock = threading.Lock()

    def slow(job: tuple[int, int]) -> int:
        start = time.monotonic()
        time.sleep(0.03 if job[1] % 2 else 0.01)   # respuestas lentas y desiguales
        with lock:
            spans.setdefault(job[0], []).append((start, time.monotonic()))
        return job[1]

    groups = [(step, [(step, i) for i in range(6)]) for step in range(5)]
    outcomes = run_groups(groups, slow, tps=1000, concurrency=4)
    assert [o.value for o in outcomes] == [i for _ in range(5) for i in range(6)]
    for step in range(4):
        assert max(end for _, end in spans[step]) <= min(start for start, _ in spans[step + 1])


def test_el_ritmo_respeta_tps_y_la_concurrencia_esta_acotada() -> None:
    in_flight = 0
    peak = 0
    lock = threading.Lock()

    def work(_job: int) -> None:
        nonlocal in_flight, peak
        with lock:
            in_flight += 1
            peak = max(peak, in_flight)
        time.sleep(0.05)
        with lock:
            in_flight -= 1

    started = time.monotonic()
    run_groups([(0, list(range(10)))], work, tps=50, concurrency=3)
    assert peak <= 3
    assert time.monotonic() - started >= 9 / 50   # 10 envíos a 50 TPS: al menos 0,18 s


def test_un_trabajo_que_falla_se_registra_sin_detener_la_corrida() -> None:
    def work(job: int) -> int:
        if job == 1:
            raise RuntimeError("boom")
        return job

    outcomes = run_groups([(0, [0, 1, 2])], work, tps=1000, concurrency=2)
    assert [o.value for o in outcomes] == [0, None, 2]
    assert isinstance(outcomes[1].error, RuntimeError)
    with pytest.raises(ValueError):
        run_groups([], work, tps=0, concurrency=1)


# --- cliente HTTP -----------------------------------------------------------------------------------------------------


def test_cliente_http_envia_clave_de_api_e_idempotencia_y_no_imprime_la_clave() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(201, json={"transfer_id": "t", "status": "COMPLETED"}, headers={"Idempotent-Replayed": "true"})

    api = HttpApi("http://svc", "secret-key", 2, transport=httpx.MockTransport(handler))
    reply = api.post_transfer("k-1", {"amount": "10.00"})
    assert reply.status == 201 and reply.body["status"] == "COMPLETED" and reply.headers["idempotent-replayed"] == "true"
    assert seen[0].headers["x-api-key"] == "secret-key" and seen[0].headers["idempotency-key"] == "k-1"
    api.review("abc", "APPROVE", "aml-eval")
    assert seen[1].url.path == "/api/v1/transfers/abc/review"
    assert b"aml-eval" in seen[1].content
    assert "secret-key" not in repr(reply)
    api.close()


def test_credenciales_desde_el_entorno(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VERABANK_API_KEYS", "web:abc123,mobile:zzz")
    assert api_credentials_from_env() == ("web", "abc123")
    monkeypatch.setenv("VERABANK_API_KEYS", "")
    with pytest.raises(SystemExit):
        api_credentials_from_env()


# --- psql en el contenedor --------------------------------------------------------------------------------------------


def test_la_contrasena_viaja_por_entorno_y_no_por_la_linea_de_comandos(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    def fake_run(cmd, **kwargs):
        captured.update(cmd=cmd, **kwargs)
        return subprocess.CompletedProcess(cmd, 0, stdout="1|2\n3|4\n", stderr="")

    monkeypatch.setenv("SIM_LOADER_PASSWORD", "pw-secreta")
    monkeypatch.delenv("VERABANK_DB_CONTAINER", raising=False)
    monkeypatch.setattr(subprocess, "run", fake_run)
    psql = db.sim_loader_psql()
    assert psql.query("SELECT 1") == [["1", "2"], ["3", "4"]]
    assert "pw-secreta" not in " ".join(captured["cmd"])
    assert captured["env"]["PGPW"] == "pw-secreta" and "-e" in captured["cmd"] and "PGPW" in captured["cmd"]
    assert captured["cmd"][:3] == ["docker", "compose", "--project-directory"] and "sim_loader" in captured["cmd"]
    assert captured["input"] == "SELECT 1"


def test_con_un_contenedor_explicito_usa_docker_exec(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VERABANK_DB_CONTAINER", "mi-postgres")
    cmd = db.read_only_psql().command("-c", "SELECT 1")
    assert cmd[:2] == ["docker", "exec"] and "mi-postgres" in cmd and "verabank_bi" in cmd and cmd[-2:] == ["-c", "SELECT 1"]


def test_sin_contrasena_en_el_entorno_se_detiene(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SIM_LOADER_PASSWORD", raising=False)
    with pytest.raises(SystemExit):
        db.sim_loader_psql().run("SELECT 1")


def test_un_fallo_de_psql_se_informa_con_su_mensaje(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SIM_LOADER_PASSWORD", "x")
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 3, "", "ERROR: duplicate key"))
    with pytest.raises(db.PsqlError, match="duplicate key"):
        db.sim_loader_psql().run("x")


# --- SQL de siembra (la prueba contra PostgreSQL real está en db/tests/test_sim_seed.py) --------------------------------


class RecordingPsql:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def run(self, stdin: str, *args: str, timeout_s: float = 0) -> str:
        self.statements.append(stdin)
        return "2|4|2|150.50\n"


def accounts(n: int) -> list[seed_sql.SeedAccount]:
    return [seed_sql.SeedAccount(f"00000000-0000-4000-8000-{i:012d}", f"11111111-1111-4111-8111-{i:012d}", "RETAIL",
                                 Decimal(i) + Decimal("0.5")) for i in range(n)]


def test_la_siembra_va_por_lotes_y_no_escribe_saldos_directos() -> None:
    psql = RecordingPsql()
    result = seed_sql.seed(psql, accounts(5), batch=2)
    assert len(psql.statements) == 3 and (result.accounts, result.funded, result.amount) == (5, 6, Decimal("451.50"))
    sql = psql.statements[0]
    assert sql.startswith("BEGIN;") and sql.rstrip().endswith("COMMIT;")
    assert "INSERT INTO core.transactions" in sql and "'D'" in sql and "'C'" in sql   # asientos desde CLEARING
    assert "md5('verabank-seed:clearing')::uuid" in sql
    assert "INSERT INTO core.accounts (account_id, customer_id, account_type, currency, status)" in sql
    assert "balance)" not in sql.split("INSERT INTO core.accounts")[1].split("VALUES")[0]   # no se inserta balance
    assert "risk." not in sql and "core.outbox" not in sql   # sin scoring ni eventos


def test_la_siembra_escapa_comillas_y_ordena_las_cuentas() -> None:
    psql = RecordingPsql()
    seed_sql.seed(psql, list(reversed(accounts(3))), batch=10)
    sql = psql.statements[0]
    assert sql.index("000000000000'::uuid") < sql.index("000000000001'::uuid") < sql.index("000000000002'::uuid")
    assert seed_sql._q("o'brien") == "'o''brien'"


def test_si_la_siembra_no_devuelve_su_resumen_falla() -> None:
    class Mute(RecordingPsql):
        def run(self, stdin: str, *args: str, timeout_s: float = 0) -> str:
            return ""

    with pytest.raises(RuntimeError):
        seed_sql.seed(Mute(), accounts(1))


# --- estado final -----------------------------------------------------------------------------------------------------


KEY_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
KEY_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


class ScriptedPsql:
    def __init__(self, *rounds: list[list[str]]) -> None:
        self.rounds = list(rounds)
        self.sql: list[str] = []

    def query(self, sql: str) -> list[list[str]]:
        self.sql.append(sql)
        return self.rounds.pop(0) if len(self.rounds) > 1 else self.rounds[0]


def row(key: str, status: str, decision: str = "APPROVE", source: str = "MODEL", latency: str = "12") -> list[str]:
    return [key, "cccccccc-cccc-4ccc-8ccc-" + key[-12:], status, decision, source, latency, ""]


def test_espera_a_que_no_queden_procesando_y_devuelve_la_ultima_lectura() -> None:
    psql = ScriptedPsql([row(KEY_A, "PROCESSING"), row(KEY_B, "COMPLETED")], [row(KEY_A, "COMPLETED")])
    now = [0.0]
    final = finalstate.wait_for_final(psql, "web", [KEY_A, KEY_B], timeout_s=60, poll_s=2, clock=lambda: now[0],
                                      sleep=lambda s: now.__setitem__(0, now[0] + s))
    assert {k: r.status for k, r in final.items()} == {KEY_A: "COMPLETED", KEY_B: "COMPLETED"}
    assert "client_id = 'web'" in psql.sql[0] and KEY_A in psql.sql[0]
    assert KEY_B not in psql.sql[1]   # solo se vuelve a preguntar por lo que seguía sin terminar


def test_se_rinde_a_los_60_segundos_si_algo_sigue_procesando() -> None:
    psql = ScriptedPsql([row(KEY_A, "PROCESSING")])
    now = [0.0]
    final = finalstate.wait_for_final(psql, "web", [KEY_A], timeout_s=60, poll_s=10, clock=lambda: now[0],
                                      sleep=lambda s: now.__setitem__(0, now[0] + s))
    assert final[KEY_A].status == "PROCESSING" and now[0] >= 60


def test_claves_invalidas_no_llegan_al_sql() -> None:
    with pytest.raises(ValueError):
        finalstate.fetch(ScriptedPsql([]), "web", ["x'; DROP TABLE core.transfers; --"])
    with pytest.raises(ValueError):
        finalstate.fetch(ScriptedPsql([]), "we'b", [KEY_A])
