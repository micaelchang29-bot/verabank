"""Los cinco KPIs de docs/kpis.sql (§5g) sobre 12 transferencias con resultados calculados a mano (5.C).

Datos (T0 = 2099-01-01 00:00 UTC; la última transferencia ocurre en T0+100 h, así que son maduras las de antes de
T0+52 h). created_at fijado a dos horas de reloj: H1 (t1–t5) y H2 (t6–t11); t12 no se puntuó.

| t  | seg.    | occ. | monto | estado      | decisión | origen   | versión  | ms  | fraude |
|----|---------|------|-------|-------------|----------|----------|----------|-----|--------|
| 1  | RETAIL  | +1 h | 100   | COMPLETED   | APPROVE  | MODEL    | m/1      | 10  | sí     |
| 2  | RETAIL  | +2 h | 200   | REJECTED    | BLOCK    | MODEL    | m/1      | 20  | sí     |
| 3  | PYME    | +3 h | 300   | COMPLETED   | REVIEW   | MODEL    | m/1      | 30  | sí     |
| 4  | PYME    | +4 h | 50    | COMPLETED   | APPROVE  | MODEL    | m/1      | 40  | no     |
| 5  | RETAIL  | +5 h | 60    | REJECTED    | BLOCK    | MODEL    | m/1      | 50  | no     |
| 6  | RETAIL  | +25h | 400   | COMPLETED   | APPROVE  | FALLBACK | fallback | 120 | no     |
| 7  | PYME    | +26h | 600   | IN_REVIEW   | REVIEW   | FALLBACK | fallback | 121 | no     |
| 8  | RETAIL  | +27h | 700   | FAILED      | APPROVE  | MODEL    | m/2      | 15  | sí     |
| 9  | PYME    | +28h | 80    | COMPLETED   | APPROVE  | MODEL    | m/2      | 25  | no     |
| 10 | RETAIL  | +70h | 90    | COMPLETED   | APPROVE  | MODEL    | m/2      | 35  | sí     |
| 11 | PYME    | +80h | 1000  | REJECTED    | BLOCK    | MODEL    | m/2      | 45  | no     |
| 12 | RETAIL  | +100h| 5     | PENDING     | —        | —        | —        | —   | no     |

Más una transferencia de siembra desde CLEARING (sin cliente: la vista no la ve) y tres alertas AML.
"""

import hashlib
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import pytest
from conftest import ADMIN, ROOT, make_database

T0 = datetime(2099, 1, 1, tzinfo=timezone.utc)
H1 = datetime(2099, 2, 1, 10, 0, tzinfo=timezone.utc)
H2 = datetime(2099, 2, 1, 11, 0, tzinfo=timezone.utc)
ROWS = [  # n, segmento, horas, monto, estado, decisión, origen, versión, latencia, fraude
    (1, "RETAIL", 1, 100, "COMPLETED", "APPROVE", "MODEL", "m/1", 10, True),
    (2, "RETAIL", 2, 200, "REJECTED", "BLOCK", "MODEL", "m/1", 20, True),
    (3, "PYME", 3, 300, "COMPLETED", "REVIEW", "MODEL", "m/1", 30, True),
    (4, "PYME", 4, 50, "COMPLETED", "APPROVE", "MODEL", "m/1", 40, False),
    (5, "RETAIL", 5, 60, "REJECTED", "BLOCK", "MODEL", "m/1", 50, False),
    (6, "RETAIL", 25, 400, "COMPLETED", "APPROVE", "FALLBACK", "fallback", 120, False),
    (7, "PYME", 26, 600, "IN_REVIEW", "REVIEW", "FALLBACK", "fallback", 121, False),
    (8, "RETAIL", 27, 700, "FAILED", "APPROVE", "MODEL", "m/2", 15, True),
    (9, "PYME", 28, 80, "COMPLETED", "APPROVE", "MODEL", "m/2", 25, False),
    (10, "RETAIL", 70, 90, "COMPLETED", "APPROVE", "MODEL", "m/2", 35, True),
    (11, "PYME", 80, 1000, "REJECTED", "BLOCK", "MODEL", "m/2", 45, False),
    (12, "RETAIL", 100, 5, "PENDING", None, None, None, None, False),
]


def uid(label: str) -> uuid.UUID:
    return uuid.UUID(hashlib.md5(f"kpi-test:{label}".encode()).hexdigest())


def kpis() -> dict[int, str]:
    text = (ROOT / "docs/kpis.sql").read_text()
    parts = text.split("-- KPI ")[1:]
    return {int(p.split(":", 1)[0]): p.split("\n", 1)[1] for p in parts}


@pytest.fixture(scope="module")
def kpi_db(tmp_path_factory):
    db = make_database(tmp_path_factory, runtime_roles_at_init=True)
    db.provision(with_roles_script=False)
    accounts = {"RETAIL": uid("acc-retail"), "PYME": uid("acc-pyme")}
    with db.connect("verabank_transfer") as conn:
        for seg, acc in accounts.items():
            conn.execute("INSERT INTO core.customers (customer_id, segment) VALUES (%s, %s)", (uid(f"cust-{seg}"), seg))
            conn.execute("INSERT INTO core.accounts (account_id, customer_id) VALUES (%s, %s)", (acc, uid(f"cust-{seg}")))
        conn.execute("INSERT INTO core.accounts (account_id, account_type) VALUES (%s, 'CLEARING')", (uid("clearing"),))
        for n, seg, hours, amount, status, decision, source, version, latency, _ in ROWS:
            other = accounts["PYME" if seg == "RETAIL" else "RETAIL"]
            conn.execute(
                """INSERT INTO core.transfers (transfer_id, client_id, idempotency_key, request_hash, source_account_id,
                     dest_account_id, dest_country, rail, transfer_type, amount, currency, status, fraud_score,
                     fraud_decision, decision_source, model_version, scoring_latency_ms, occurred_at, created_at)
                   VALUES (%s, 'test', %s, %s, %s, %s, 'PE', 'INTERNAL', 'TRANSFER', %s, 'PEN', %s, NULL, %s, %s, %s, %s,
                     %s, %s)""",
                (uid(f"t{n}"), uuid.uuid4(), "0" * 64, accounts[seg], other, amount, status, decision, source, version,
                 latency, T0 + timedelta(hours=hours), H1 if n <= 5 else H2))  # fmt: skip
        conn.execute(  # siembra desde CLEARING: fuera de la vista; si entrara, KPI 4 lo notaría
            """INSERT INTO core.transfers (transfer_id, client_id, idempotency_key, request_hash, source_account_id,
                 dest_account_id, dest_country, rail, transfer_type, amount, currency, status, fraud_decision,
                 decision_source, model_version, scoring_latency_ms, occurred_at, created_at)
               VALUES (%s, 'seed', %s, %s, %s, %s, 'PE', 'INTERNAL', 'TRANSFER', 9999, 'PEN', 'COMPLETED', 'APPROVE',
                 'MODEL', 'm/1', 1, %s, %s)""",
            (uid("seed"), uuid.uuid4(), "0" * 64, uid("clearing"), accounts["RETAIL"], T0, H1))  # fmt: skip
    with db.connect("risk_svc") as conn:
        for n, *_, fraud in ROWS:
            if n != 12:   # t12 aún sin etiqueta: COALESCE la deja en no fraude
                conn.execute("INSERT INTO risk.fraud_labels (transfer_id, is_fraud, label_source) VALUES (%s, %s, 'SIMULATOR')",
                             (uid(f"t{n}"), fraud))  # fmt: skip
        for i, (rule, latency_s, status) in enumerate([("THRESHOLD", 1, "CLOSED_FALSE_POSITIVE"), ("THRESHOLD", 3, "OPEN"),
                                                       ("STRUCTURING", 2, "CLOSED_CONFIRMED")]):  # fmt: skip
            conn.execute(
                """INSERT INTO risk.aml_alerts (alert_id, dedupe_key, rule_code, rule_version, rule_params,
                     subject_account_id, severity, status, window_start, window_end, total_amount, transfer_ids,
                     trigger_emitted_at, created_at)
                   VALUES (%s, %s, %s, 1, '{}', %s, 'MEDIUM', %s, %s, %s, 10000, ARRAY[%s]::uuid[], %s, %s)""",
                (uid(f"alert{i}"), f"k{i}", rule, accounts["RETAIL"], status, T0, T0, uid("t1"),
                 H1, H1 + timedelta(seconds=latency_s)))  # fmt: skip
    yield db
    db.teardown()


def run(db, n: int) -> list[tuple]:
    with db.connect("verabank_bi") as conn:   # el rol de Metabase
        return conn.execute(kpis()[n]).fetchall()


def test_the_view_hides_the_clearing_seed(kpi_db):
    with kpi_db.connect("verabank_bi") as conn:
        assert conn.execute("SELECT count(*) FROM analytics.v_transfer_outcomes").fetchone() == (12,)
        assert conn.execute("SELECT count(*) FILTER (WHERE label_mature) FROM analytics.v_transfer_outcomes").fetchone() == (9,)


def test_kpi1_detection_by_day(kpi_db):
    assert run(kpi_db, 1) == [
        (datetime(2099, 1, 1, tzinfo=timezone.utc), 3, D("66.67"), D("83.33")),   # t2 y t3 de t1–t3; 500 de 600
        (datetime(2099, 1, 2, tzinfo=timezone.utc), 1, D("0.00"), D("0.00")),     # t8 aprobado
    ]


def test_kpi2_false_positives_and_precision_by_model_version(kpi_db):
    assert sorted(run(kpi_db, 2)) == [
        ("fallback", D("50.000"), D("0.00"), 0),       # t7 a revisión de t6–t7; ninguna marcada era fraude
        ("m/1", D("50.000"), D("66.67"), 1),          # t5 bloqueada de t4–t5; t2, t3 de t2, t3, t5
        ("m/2", D("0.000"), None, 0),                 # maduras t8 y t9, ambas aprobadas
    ]


def test_kpi3_aml_latency_and_false_positives_by_rule(kpi_db):
    rows = run(kpi_db, 3)
    assert [(r[0], r[1]) for r in rows] == [("THRESHOLD", 2), ("STRUCTURING", 1)]
    threshold, structuring = rows
    assert threshold[2:4] == pytest.approx((2.0, 2.9)) and threshold[4] == D("100.0")
    assert structuring[2:4] == pytest.approx((2.0, 2.0)) and structuring[4] == D("0.0")


def test_kpi4_approval_by_segment(kpi_db):
    assert run(kpi_db, 4) == [
        ("PYME", 5, D("40.00"), D("40.00"), D("60.00")),      # t3 t4 t7 t9 t11
        ("RETAIL", 6, D("66.67"), D("0.00"), D("50.00")),     # t1 t2 t5 t6 t8 t10
    ]


def test_kpi5_scoring_latency_and_fallback_by_hour(kpi_db):
    rows = run(kpi_db, 5)
    assert [(r[0], r[1]) for r in rows] == [(H2, 6), (H1, 5)]
    assert rows[0][2] == pytest.approx(120.75) and rows[0][3] == D("33.33")   # [15 25 35 45 120 121]
    assert rows[1][2] == pytest.approx(48.0) and rows[1][3] == D("0.00")      # [10 20 30 40 50]
