#!/usr/bin/env bash
# Muestreador del diagnóstico del rail (5.G.2): cada INTERVAL s (15) escribe una fila en data/perf/diag/<etiqueta>.csv
# y la CPU por contenedor en <etiqueta>-cpu.txt, hasta recibir SIGTERM. Solo lectura: Kafka por kafka-consumer-groups y
# kafka-get-offsets dentro del contenedor, PostgreSQL con el rol de arranque por el socket local. Sin secretos en salida.
# Uso: perf/diag_rail.sh <etiqueta> &   (lo detiene el llamador con kill)
. "$(dirname "$0")/../scripts/lib.sh"
label="${1:?etiqueta}"
INTERVAL="${INTERVAL:-15}"
OUT="$ROOT/data/perf/diag"
mkdir -p "$OUT"
csv="$OUT/$label.csv"
echo "ts,lag_p0,lag_p1,lag_p2,lag_total,cmd_offsets,reply_offsets,xact_commit,adv815001_waiting,locks_waiting,saga_running,saga_waiting_reply,saga_in_review,saga_compensating,transfers,audit_rows,pg_active" >"$csv"
kafka() { "${COMPOSE_ALL[@]}" exec -T kafka "$@" 2>/dev/null; }
offsets() { kafka /opt/kafka/bin/kafka-get-offsets.sh --bootstrap-server localhost:9092 --topic "$1" | awk -F: '{s += $3} END {print s + 0}'; }
pg() {
  "${COMPOSE[@]}" exec -T postgres sh -c 'exec psql -X -q -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tA -F,' 2>/dev/null <<'SQL'
SELECT (SELECT xact_commit FROM pg_stat_database WHERE datname = current_database()),
       (SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND objid = 815001 AND NOT granted),
       (SELECT count(*) FROM pg_locks WHERE NOT granted),
       count(*) FILTER (WHERE status = 'RUNNING'), count(*) FILTER (WHERE status = 'WAITING_REPLY'),
       count(*) FILTER (WHERE status = 'IN_REVIEW'), count(*) FILTER (WHERE status = 'COMPENSATING'),
       (SELECT count(*) FROM core.transfers), (SELECT count(*) FROM audit.audit_log),
       (SELECT count(*) FROM pg_stat_activity WHERE state = 'active' AND datname = current_database())
FROM core.saga_state;
SQL
}
trap 'exit 0' TERM INT
while true; do
  lags="$(kafka /opt/kafka/bin/kafka-consumer-groups.sh --bootstrap-server localhost:9092 --describe --group transfer-service \
    | awk '$2 == "rail.replies.v1" {lag[$3] = $6} END {t = 0; for (p = 0; p < 3; p++) {v = (lag[p] == "" || lag[p] == "-") ? 0 : lag[p]; printf "%s,", v; t += v}; printf "%s", t}')"
  echo "$(date -u +%FT%TZ),${lags},$(offsets rail.commands.v1),$(offsets rail.replies.v1),$(pg | tr -d ' ')" >>"$csv"
  { echo "# $(date -u +%FT%TZ)"; docker stats --no-stream --format '{{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}' $("${COMPOSE_ALL[@]}" ps -q); } >>"$OUT/$label-cpu.txt" 2>/dev/null
  sleep "$INTERVAL" & wait $!
done
