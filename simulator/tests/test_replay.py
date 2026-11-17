"""Replay de PaySim (sin servicios): mapeo, selección, calentamiento, saldos, factor, etiquetas y orquestación."""

from __future__ import annotations

import csv
import json
import re
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator

from verabank_sim.load.api import Reply
from verabank_sim.replay import labels, mapping as m, warmup
from verabank_sim.replay.accounts import replay_accounts
from verabank_sim.replay.paysim import select_rows
from verabank_sim.replay.run import Options, Services, build_jobs, format_summary, run

ROOT = Path(__file__).resolve().parents[2]
ML = yaml.safe_load((ROOT / "ml" / "config.yaml").read_text())
EPOCH = m.parse_epoch(ML["sim_epoch"])
TYPES = ML["types"]
TEST = tuple(ML["split"]["test"])
HEADER = ["step", "type", "amount", "nameOrig", "oldbalanceOrg", "newbalanceOrig", "nameDest", "oldbalanceDest",
          "newbalanceDest", "isFraud", "isFlaggedFraud"]


def write_csv(path: Path, rows: list[tuple]) -> Path:
    """rows: (step, type, amount, orig, old_balance, dest, fraud)"""
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(HEADER)
        for step, typ, amount, orig, old, dest, fraud in rows:
            w.writerow([step, typ, amount, orig, old, 0, dest, 0, 0, fraud, 0])
    return path


@pytest.fixture
def paysim(tmp_path: Path) -> Path:
    return write_csv(tmp_path / "paysim.csv", [
        (540, "TRANSFER", "999.00", "C0", "5000", "M0", 0),          # antes de la ventana de calentamiento
        (552, "TRANSFER", "50.00", "C1", "100", "C9", 0),            # calentamiento (575 - 24 = 551 a 574)
        (560, "CASH_IN", "70.00", "C2", "100", "C9", 0),             # CASH_IN: nunca
        (574, "PAYMENT", "20.00", "C1", "100", "M1", 0),             # calentamiento
        (575, "TRANSFER", "100.00", "C1", "1000", "C7", 0),          # 1.ª fila del replay
        (575, "CASH_IN", "30.00", "C3", "10", "C8", 0),              # CASH_IN dentro de la partición
        (575, "CASH_OUT", "250.50", "C2", "10", "C8", 1),
        (576, "PAYMENT", "80.00", "C1", "1000", "M2", 0),
        (576, "DEBIT", "0.00", "C4", "10", "C8", 0),                 # monto 0: no es transferencia
        (577, "TRANSFER", "300.25", "C5", "100", "C7", 1),
        (744, "TRANSFER", "10.00", "C6", "100", "C7", 0),
        (745, "TRANSFER", "11.00", "C6", "100", "C7", 0),            # fuera de la partición de prueba
    ])


def selection(path: Path, **kw):
    return select_rows(path, types=TYPES, test_steps=TEST, n=kw.pop("n", 100), **kw)


# --- mapeo (D10) ------------------------------------------------------------------------------------------------------


def test_mapeo_determinista_y_cuentas_por_nombre() -> None:
    assert m.account_id("C123") == m.account_id("C123") != m.account_id("C124")
    assert re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-5[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}", m.account_id("C123"))   # uuid5
    assert 0 <= m.customer_index("C123") < 30_000 and m.customer_index("C123") == m.customer_index("C123")
    assert {m.segment(i) for i in range(30)} == {"RETAIL", "PREMIUM", "PYME"}
    assert [m.segment(i) for i in range(30)].count("RETAIL") == 18


def test_solicitud_externa_con_referencia_de_paysim_y_monto_como_cadena(paysim: Path) -> None:
    first = selection(paysim).rows[0]
    body = m.transfer_body(first, EPOCH)
    assert body == {"source_account_id": m.account_id("C1"), "dest_external_ref": "PS-C7", "dest_country": "PE",
                    "rail": "EXTERNAL", "transfer_type": "TRANSFER", "amount": "100.00", "currency": "PEN",
                    "occurred_at": "2026-01-24T22:00:00Z"}
    assert isinstance(body["amount"], str) and re.fullmatch(r"[0-9]{1,16}(\.[0-9]{1,2})?", body["amount"])
    assert "dest_account_id" not in body


def test_occurred_at_es_epoch_mas_step_menos_1_horas_y_esta_alineado_a_la_hora() -> None:
    assert m.occurred_at(EPOCH, 1) == "2026-01-01T00:00:00Z"
    assert m.occurred_at(EPOCH, 25) == "2026-01-02T00:00:00Z"
    assert m.occurred_at(EPOCH, 575).endswith(":00:00Z")
    assert datetime.fromisoformat(m.occurred_at(EPOCH, 575)).tzinfo == UTC


def test_todas_las_filas_de_un_paso_comparten_occurred_at(paysim: Path) -> None:
    groups = dict(build_jobs(selection(paysim).rows, EPOCH))
    assert {j.body["occurred_at"] for j in groups[575]} == {m.occurred_at(EPOCH, 575)}
    assert len(groups[575]) == 2 and {len({j.body["occurred_at"] for j in g}) for g in groups.values()} == {1}


# --- selección ---------------------------------------------------------------------------------------------------------


def test_solo_partición_de_prueba_solo_tipos_configurados_y_sin_cash_in(paysim: Path) -> None:
    sel = selection(paysim)
    assert [r.step for r in sel.rows] == [575, 575, 576, 577, 744]
    assert {r.type for r in sel.rows} <= set(TYPES) and "CASH_IN" not in {r.type for r in sel.rows}
    assert all(TEST[0] <= r.step <= TEST[1] for r in sel.rows)
    assert [r.row_id for r in sel.rows] == sorted(r.row_id for r in sel.rows)
    assert sel.skipped_zero_amount == 1 and sel.start_step == 575


def test_las_n_primeras_desde_start_row(paysim: Path) -> None:
    assert [r.name_orig for r in selection(paysim, n=2).rows] == ["C1", "C2"]
    later = selection(paysim, n=2, start_row=2)
    assert [r.name_orig for r in later.rows] == ["C1", "C5"] and later.start_step == 576


def test_calentamiento_son_las_filas_de_los_24_pasos_previos_sin_cash_in(paysim: Path) -> None:
    sel = selection(paysim)
    assert [(r.step, r.name_orig) for r in sel.warmup] == [(552, "C1"), (574, "C1")]
    assert all(sel.start_step - 24 <= r.step < sel.start_step for r in sel.warmup)
    assert "CASH_IN" not in {r.type for r in sel.warmup}
    # con start_row > 0 la ventana se recoge antes del primer paso reproducido
    later = selection(paysim, start_row=3)   # empieza en la fila de C5 (paso 577): el calentamiento son los 24 pasos previos
    assert [(r.step, r.name_orig) for r in later.warmup] == [(574, "C1"), (575, "C1"), (575, "C2"), (576, "C1")]


def test_el_calentamiento_usa_el_mismo_mapeo_y_el_formato_de_copy(paysim: Path) -> None:
    sel = selection(paysim)
    first = warmup.warmup_record(sel.warmup[0], EPOCH)
    assert first[1] == m.account_id("C1") and first[2] == "PS-C9" and first[3] == "50.00"
    assert first[4] == m.occurred_at(EPOCH, 552) and first[5:] == ["{}", "0", "APPROVE", "warmup"]
    lines = warmup.copy_data(sel.warmup, EPOCH).splitlines()
    assert len(lines) == 2 and lines[0].endswith(",{},0,APPROVE,warmup")
    assert warmup.warmup_id(sel.warmup[0]) == first[0]


class FakeLoader:
    def __init__(self, fail_copy: bool = False) -> None:
        self.calls: list[tuple[str, tuple[str, ...]]] = []
        self.fail_copy = fail_copy

    def run(self, stdin: str, *args: str, timeout_s: float = 0) -> str:
        self.calls.append((stdin, args))
        if self.fail_copy and args[:1] == ("-c",):
            raise RuntimeError('ERROR: duplicate key value violates unique constraint "scoring_log_pkey"')
        return ""


def test_el_calentamiento_carga_con_copy_y_si_ya_estaba_cae_a_insert_on_conflict(paysim: Path) -> None:
    rows = selection(paysim).warmup
    ok = FakeLoader()
    assert warmup.load(ok, rows, EPOCH) == (2, "copy") and ok.calls[0][1][0] == "-c" and "FROM STDIN" in ok.calls[0][1][1]
    dup = FakeLoader(fail_copy=True)
    assert warmup.load(dup, rows, EPOCH) == (2, "insert")
    assert "ON CONFLICT (transfer_id) DO NOTHING" in dup.calls[1][0] and "'warmup'" in dup.calls[1][0]
    other = FakeLoader()
    other.fail_copy = False
    assert warmup.load(other, [], EPOCH) == (0, "copy") and not other.calls

    class Boom(FakeLoader):
        def run(self, stdin: str, *args: str, timeout_s: float = 0) -> str:
            raise RuntimeError("permission denied")

    with pytest.raises(RuntimeError, match="permission denied"):
        warmup.load(Boom(), rows, EPOCH)


# --- saldos y factor --------------------------------------------------------------------------------------------------


def test_saldo_sembrado_es_el_maximo_entre_oldbalance_y_la_suma_de_montos(paysim: Path) -> None:
    rows = selection(paysim).rows
    by_account = {a.account_id: a.balance for a in replay_accounts(rows)}
    assert by_account[m.account_id("C1")] == Decimal("1000.00")    # oldbalanceOrg 1000 > 100 + 80
    assert by_account[m.account_id("C5")] == Decimal("300.25")     # oldbalanceOrg 100 < 300,25
    assert by_account[m.account_id("C2")] == Decimal("250.50")
    assert len(by_account) == len({r.name_orig for r in rows})


def test_amount_factor_multiplica_los_montos_y_el_saldo_necesario(paysim: Path) -> None:
    base = selection(paysim)
    big = selection(paysim, factor=Decimal("3"))
    assert [r.amount for r in big.rows] == [r.amount * 3 for r in base.rows]
    by_account = {a.account_id: a.balance for a in replay_accounts(big.rows)}
    assert by_account[m.account_id("C1")] == Decimal("1000.00")    # 300 + 240 = 540 < 1000
    assert by_account[m.account_id("C5")] == Decimal("900.75")
    tiny = selection(paysim, factor=Decimal("0.005"))   # 100,00 → 0,50; 250,50 → 1,25 (mitad al par); 300,25 → 1,50
    assert [r.amount for r in tiny.rows] == [Decimal("0.50"), Decimal("1.25"), Decimal("0.40"), Decimal("1.50"), Decimal("0.05")]


def test_el_factor_se_aplica_antes_de_la_clave_de_idempotencia(paysim: Path) -> None:
    a = selection(paysim).rows[0]
    again = selection(paysim).rows[0]
    other = selection(paysim, factor=Decimal("2")).rows[0]
    assert m.idempotency_key(a) == m.idempotency_key(again) != m.idempotency_key(other)
    assert m.amount_text(other.amount) == "200.00"
    # una fila de monto cero tras el factor deja de ser candidata
    assert all(r.amount > 0 for r in selection(paysim, factor=Decimal("0.0001")).rows)


def test_csv_fuera_de_orden_se_rechaza(tmp_path: Path) -> None:
    bad = write_csv(tmp_path / "bad.csv", [(580, "TRANSFER", "1.00", "C1", "1", "C2", 0), (579, "TRANSFER", "1.00", "C1", "1", "C2", 0)])
    with pytest.raises(ValueError, match="no está ordenado"):
        selection(bad)


# --- etiquetas ----------------------------------------------------------------------------------------------------------


def test_las_etiquetas_llevan_occurred_at_mas_24_horas_y_cumplen_el_esquema() -> None:
    tid = "5c0f2b0e-7f43-4c58-9a57-3e2b1f9d6a10"
    msg = labels.build(tid, True, "2026-01-24T22:00:00Z", emitted_at=1_700_000_000.0)
    assert msg.envelope["occurred_at"] == "2026-01-25T22:00:00Z"
    assert msg.envelope["event_type"] == "FraudLabelConfirmed" and msg.envelope["aggregate_id"] == tid
    assert msg.envelope["payload"] == {"transfer_id": tid, "is_fraud": True, "label_source": "SIMULATOR"}
    assert msg.key == tid and ("event_type", b"FraudLabelConfirmed") in msg.headers()
    contracts = ROOT / "contracts" / "events"
    Draft202012Validator(json.loads((contracts / "envelope.v1.json").read_text())).validate(msg.envelope)
    Draft202012Validator(json.loads((contracts / "FraudLabelConfirmed.v1.schema.json").read_text())).validate(msg.envelope["payload"])
    again = labels.build(tid, True, "2026-01-24T22:00:00Z", emitted_at=1.0)
    assert (again.event_id, again.envelope["correlation_id"]) == (msg.event_id, msg.envelope["correlation_id"])
    assert labels.build(tid, False, "2026-01-24T22:00:00Z", emitted_at=1.0).envelope["payload"]["is_fraud"] is False


# --- orquestación --------------------------------------------------------------------------------------------------------


class FakeApi:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def post_transfer(self, key: str, body: dict) -> Reply:
        self.calls.append((key, body))
        return Reply(201 if len(self.calls) % 2 else 200, {}, {"status": "COMPLETED"}, 5.0 + len(self.calls))


class FakeState:
    """Rol de transfer-service: la siembra devuelve su resumen y la consulta final, una fila por clave."""

    def __init__(self) -> None:
        self.statements: list[str] = []

    def run(self, stdin: str, *args: str, timeout_s: float = 0) -> str:
        self.statements.append(stdin)
        return "3|6|3|1550.75\n"

    def query(self, sql: str) -> list[list[str]]:
        keys = re.findall(r"'([0-9a-f-]{36})'", sql)
        return [[k, f"{i:08d}-0000-4000-8000-000000000000", "COMPLETED" if i % 2 else "REJECTED",
                 "APPROVE" if i % 2 else "BLOCK", "MODEL" if i % 3 else "FALLBACK", str(10 + i), ""]
                for i, k in enumerate(keys)]


class FakePublisher:
    def __init__(self) -> None:
        self.messages = []

    def publish(self, messages) -> int:
        self.messages = list(messages)
        return len(self.messages)


def test_replay_completo_con_servicios_simulados(paysim: Path, tmp_path: Path) -> None:
    sel = selection(paysim)
    api, state, loader, publisher = FakeApi(), FakeState(), FakeLoader(), FakePublisher()
    services = Services(api=api, state_psql=state, loader_psql=loader, publisher=publisher, client_id="web",
                        now=lambda: 1_700_000_000.0)
    summary = run(Options(tps=1000, concurrency=4, final_timeout_s=1), sel, EPOCH, services, tmp_path / "out")

    assert summary["sent"] == 5 and summary["by_http_status"] == {"200": 2, "201": 3}
    assert sum(summary["by_final_status"].values()) == 5 and set(summary["by_fraud_decision"]) == {"APPROVE", "BLOCK"}
    assert set(summary["by_decision_source"]) == {"MODEL", "FALLBACK"}
    assert summary["client_latency_ms"]["n"] == 5 and summary["scoring_latency_ms"]["n"] == 5
    assert summary["warmup_rows"] == 2 and summary["warmup_method"] == "copy" and summary["seeded_accounts"] == 4
    assert summary["labels_published"] == 5 and len(publisher.messages) == 5
    assert all(msg.envelope["payload"]["label_source"] == "SIMULATOR" for msg in publisher.messages)
    assert json.loads((tmp_path / "out" / "summary.json").read_text()) == summary
    assert [b["occurred_at"] for _, b in api.calls][:2] == [m.occurred_at(EPOCH, 575)] * 2   # pasos en orden
    assert len({k for k, _ in api.calls}) == 5
    text = format_summary(summary)
    assert "enviadas: 5" in text and "p50" in text and "TPS alcanzados" in text


def test_sin_calentamiento_sin_etiquetas_y_sin_siembra(paysim: Path, tmp_path: Path) -> None:
    api, state, loader, publisher = FakeApi(), FakeState(), FakeLoader(), FakePublisher()
    services = Services(api=api, state_psql=state, loader_psql=loader, publisher=publisher, client_id="web")
    summary = run(Options(tps=1000, warmup=False, labels=False, seed=False, final_timeout_s=1), selection(paysim), EPOCH,
                  services, None)
    assert summary["warmup_rows"] == 0 and not loader.calls and summary["labels_published"] == 0
    assert not publisher.messages and summary["seeded_accounts"] == 0 and not state.statements


def test_un_error_de_red_se_cuenta_y_no_detiene_el_replay(paysim: Path) -> None:
    class Flaky(FakeApi):
        def post_transfer(self, key: str, body: dict) -> Reply:
            if len(self.calls) == 1:
                self.calls.append((key, body))
                raise ConnectionError("reset")
            return super().post_transfer(key, body)

    services = Services(api=Flaky(), state_psql=FakeState(), loader_psql=FakeLoader(), publisher=None, client_id="web")
    summary = run(Options(tps=1000, warmup=False, labels=False, seed=False, final_timeout_s=1), selection(paysim), EPOCH,
                  services, None)
    assert summary["by_http_status"].get("error") == 1 and summary["sent"] == 5
