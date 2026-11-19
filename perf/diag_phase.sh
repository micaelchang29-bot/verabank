#!/usr/bin/env bash
# Una fase del diagnóstico del rail (5.G.2): perf/diag_rail.sh en paralelo con make load-transfers RATE=<tps>
# DURATION=<duración>, y al final los volcados de PostgreSQL (pg_stat_statements si está activo, outbox por tipo de
# mensaje y auditoría por saga, solo de esta fase). Salidas en data/perf/diag/<etiqueta>-*.
# Uso: perf/diag_phase.sh <etiqueta> <tps> <duración>
cd "$(dirname "$0")/.."
label="$1"; rate="$2"; dur="$3"
OUT=data/perf/diag; mkdir -p "$OUT"
admin() { docker compose --profile core exec -T postgres sh -c 'exec psql -X -q -U "$POSTGRES_USER" -d "$POSTGRES_DB" -P pager=off "$@"' sh "$@"; }
admin -tA -c "SELECT pg_stat_statements_reset()" >/dev/null 2>&1 || true
start="$(admin -tA -c "SELECT now()")"; echo "$start" > "$OUT/$label-start.txt"
INTERVAL=10 perf/diag_rail.sh "$label" & sampler=$!
make load-transfers RATE="$rate" DURATION="$dur" > "$OUT/$label-load.log" 2>&1; echo "load exit=$?" >> "$OUT/$label-load.log"
kill "$sampler"; wait "$sampler" 2>/dev/null
cp data/perf/transfers-summary.json "$OUT/$label-k6.json" 2>/dev/null
cp data/perf/metrics.csv "$OUT/$label-metrics.csv" 2>/dev/null
admin -c "SELECT calls, round(total_exec_time::numeric) AS total_ms, round(mean_exec_time::numeric, 2) AS mean_ms, round((blk_write_time + blk_read_time)::numeric) AS io_ms, left(regexp_replace(query, '\s+', ' ', 'g'), 110) AS query FROM pg_stat_statements WHERE dbid = (SELECT oid FROM pg_database WHERE datname = current_database()) ORDER BY total_exec_time DESC LIMIT 15" > "$OUT/$label-statements.txt" 2>&1
admin -c "SELECT event_type, count(*), count(DISTINCT aggregate_id) AS transfers, round(count(*)::numeric / nullif(count(DISTINCT aggregate_id), 0), 2) AS por_transferencia FROM core.outbox WHERE created_at >= '$start' GROUP BY 1 ORDER BY 2 DESC" > "$OUT/$label-outbox.txt"
admin -c "SELECT round(count(*)::numeric / nullif((SELECT count(*) FROM core.saga_state WHERE created_at >= '$start'), 0), 2) AS audit_por_saga, count(*) AS audit_rows, (SELECT count(*) FROM core.saga_state WHERE created_at >= '$start') AS sagas FROM audit.audit_log WHERE occurred_at >= '$start'" > "$OUT/$label-audit.txt"
admin -c "SELECT action, count(*) FROM audit.audit_log WHERE occurred_at >= '$start' GROUP BY 1 ORDER BY 2 DESC LIMIT 15" >> "$OUT/$label-audit.txt"
echo "fase $label terminada"
