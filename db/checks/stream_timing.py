"""4.G(a): tiempo de las consultas del rol stream de risk-service frente al statement_timeout de risk_svc.

Ejecuta cada consulta de ``risk-service/src/main/java/.../stream`` (las mismas sentencias, con parámetros reales
tomados de la base) ``--repeat`` veces con ``EXPLAIN (ANALYZE)`` y reporta el máximo de ``Execution Time``. Las
escrituras corren dentro de una transacción que se revierte: no cambia nada. Se conecta como el administrador por
127.0.0.1 (``POSTGRES_USER``/``POSTGRES_PASSWORD`` de .env, por entorno) y lee el ``statement_timeout`` de risk_svc.

    uv run --project db --frozen python db/checks/stream_timing.py
"""

from __future__ import annotations

import os
import re
import sys
import uuid

import psycopg

QUERIES = {
    "country_risk (CountryRiskRule)": (
        "SELECT level FROM risk.country_risk WHERE country = %(country)s AND valid_from <= %(at)s "
        "AND (valid_to IS NULL OR %(at)s < valid_to)", False),
    "ventana de fraccionamiento (StructuringRule)": (
        "SELECT transfer_id, amount, occurred_at FROM risk.transfer_facts WHERE source_account_id = %(account)s "
        "AND occurred_at >= %(from)s AND occurred_at <= %(at)s AND amount >= 7000 AND amount < 10000 "
        "ORDER BY occurred_at, transfer_id", False),
    "proyección transfer_facts (AmlProcessor)": (
        "INSERT INTO risk.transfer_facts (transfer_id, source_account_id, dest_ref, dest_country, transfer_type, amount, "
        "occurred_at, emitted_at, completed_at) VALUES (%(new)s, %(account)s, 'x', 'PE', 'TRANSFER', 1, %(at)s, now(), "
        "now()) ON CONFLICT (transfer_id) DO NOTHING", True),
    "upsert de alerta (AlertRepository)": (
        "INSERT INTO risk.aml_alerts (alert_id, dedupe_key, rule_code, rule_version, rule_params, subject_account_id, "
        "severity, window_start, window_end, total_amount, transfer_ids, trigger_emitted_at) VALUES (%(new)s, %(dedupe)s, "
        "'THRESHOLD', 1, '{}', %(account)s, 'MEDIUM', %(at)s, %(at)s, 1, ARRAY[%(new)s]::uuid[], now()) "
        "ON CONFLICT (dedupe_key) DO UPDATE SET transfer_ids = EXCLUDED.transfer_ids, total_amount = EXCLUDED.total_amount, "
        "window_end = EXCLUDED.window_end WHERE risk.aml_alerts.transfer_ids IS DISTINCT FROM EXCLUDED.transfer_ids "
        "RETURNING alert_id", True),
    "etiqueta (LabelProcessor)": (
        "INSERT INTO risk.fraud_labels (transfer_id, is_fraud, label_source, labeled_at) VALUES (%(label)s, true, "
        "'SIMULATOR', now()) ON CONFLICT (transfer_id) DO UPDATE SET is_fraud = EXCLUDED.is_fraud, "
        "label_source = EXCLUDED.label_source, labeled_at = now() WHERE (risk.fraud_labels.is_fraud, "
        "risk.fraud_labels.label_source) IS DISTINCT FROM (EXCLUDED.is_fraud, EXCLUDED.label_source)", True),
    "inbox (Inbox)": ("INSERT INTO risk.processed_messages (consumer, event_id) VALUES ('timing', %(new)s)", True),
    "cola de alertas (AlertService, GET /aml/alerts)": (
        "SELECT alert_id, rule_code, rule_version, rule_params::text, severity, status, subject_account_id, transfer_ids, "
        "total_amount, window_start, window_end, created_at, closed_at, closed_by FROM risk.aml_alerts "
        "WHERE status = 'OPEN' ORDER BY severity, created_at LIMIT 100", False),
}


def params(conn) -> dict:
    """La cuenta con más hechos (el peor caso de la ventana) y una etiqueta existente."""
    account, at = conn.execute(
        "SELECT source_account_id, max(occurred_at) FROM risk.transfer_facts GROUP BY 1 ORDER BY count(*) DESC LIMIT 1"
    ).fetchone() or (uuid.uuid4(), "2026-01-30")
    label = conn.execute("SELECT transfer_id FROM risk.fraud_labels LIMIT 1").fetchone()
    dedupe = conn.execute("SELECT dedupe_key FROM risk.aml_alerts LIMIT 1").fetchone()
    return {"account": account, "at": at, "from": conn.execute("SELECT %s::timestamptz - interval '24 hours'", (at,)).fetchone()[0],
            "country": "IR", "label": label[0] if label else uuid.uuid4(), "dedupe": dedupe[0] if dedupe else "x",
            "new": uuid.uuid4()}


def main() -> int:
    repeat = int(sys.argv[1]) if len(sys.argv) > 1 else 50
    dsn = psycopg.conninfo.make_conninfo(host="127.0.0.1", port=os.environ.get("POSTGRES_PORT", "5432"),
                                         dbname=os.environ.get("POSTGRES_DB", "verabank"),
                                         user=os.environ["POSTGRES_USER"], password=os.environ["POSTGRES_PASSWORD"])
    with psycopg.connect(dsn) as conn:
        timeout = conn.execute("SELECT coalesce((SELECT split_part(c, '=', 2) FROM pg_db_role_setting s, "
                               "unnest(s.setconfig) c WHERE s.setrole = 'risk_svc'::regrole AND c LIKE 'statement_timeout=%'), "
                               "'(sin valor)')").fetchone()[0]
        p = params(conn)
        conn.rollback()
        worst = 0.0
        print(f"statement_timeout de risk_svc: {timeout}; {repeat} ejecuciones por consulta (EXPLAIN ANALYZE)")
        for name, (sql, writes) in QUERIES.items():
            times = []
            for _ in range(repeat):
                p["new"] = uuid.uuid4()
                plan = conn.execute(f"EXPLAIN (ANALYZE) {sql}", p).fetchall()
                times.append(float(re.search(r"Execution Time: ([\d.]+) ms", plan[-1][0]).group(1)))
                conn.rollback()                      # las escrituras no quedan
            times.sort()
            worst = max(worst, times[-1])
            print(f"  {name:<48} p50 {times[len(times) // 2]:7.3f} ms   máx {times[-1]:7.3f} ms")
        print(f"consulta más lenta: {worst:.3f} ms frente a {timeout}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
