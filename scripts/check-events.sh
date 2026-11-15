#!/usr/bin/env bash
# make check-events: cada transferencia COMPLETED tiene su TransferCompleted en
# transfer.events.v1. Se excluyen las de la semilla (client_id 'seed'), que db/seed carga por
# SQL sin pasar por el servicio y por eso no emiten eventos.
. "$(dirname "$0")/lib.sh"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# Primero se espera a que el relay vacíe el outbox (retraso esperado < 1 s).
for _ in $(seq 1 30); do
  pending="$(echo "SELECT count(*) FROM core.outbox WHERE published_at IS NULL" | psql_transfer -tA)"
  [[ "$pending" == 0 ]] && break
  sleep 1
done
[[ "$pending" == 0 ]] || { echo "FALLO: quedan $pending eventos sin publicar en core.outbox" >&2; exit 1; }

echo "SELECT transfer_id FROM core.transfers WHERE status = 'COMPLETED' AND client_id <> 'seed' ORDER BY 1" \
  | psql_transfer -tA | sort -u >"$TMP/completed"

"${COMPOSE[@]}" exec -T kafka /opt/kafka/bin/kafka-console-consumer.sh \
  --bootstrap-server localhost:9092 --topic transfer.events.v1 --from-beginning --timeout-ms 10000 \
  2>/dev/null >"$TMP/events" || true
grep -E '"event_type" ?: ?"TransferCompleted"' "$TMP/events" \
  | grep -oE '"aggregate_id" ?: ?"[0-9a-f-]{36}"' | grep -oE '[0-9a-f-]{36}' | sort -u >"$TMP/published" || true

total="$(wc -l <"$TMP/completed")"
missing="$(comm -23 "$TMP/completed" "$TMP/published" | wc -l)"
echo "transferencias completadas por el servicio: $total"
echo "con TransferCompleted en Kafka:             $((total - missing))"
if [[ "$missing" -ne 0 ]]; then
  echo "FALLO: sin evento en Kafka:" >&2
  comm -23 "$TMP/completed" "$TMP/published" >&2
  exit 1
fi
echo "OK: cada transferencia completada tiene su evento en Kafka"
