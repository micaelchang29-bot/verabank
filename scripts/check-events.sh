#!/usr/bin/env bash
# make check-events: cada transferencia COMPLETED tiene su TransferCompleted, y cada FAILED su
# TransferFailed con compensated = true, en transfer.events.v1. Se excluyen las de la semilla
# (client_id 'seed'), que db/seed carga por SQL sin pasar por el servicio y por eso no emiten eventos.
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
echo "SELECT transfer_id FROM core.transfers WHERE status = 'FAILED' AND client_id <> 'seed' ORDER BY 1" \
  | psql_transfer -tA | sort -u >"$TMP/failed"

"${COMPOSE[@]}" exec -T kafka /opt/kafka/bin/kafka-console-consumer.sh \
  --bootstrap-server localhost:9092 --topic transfer.events.v1 --from-beginning --timeout-ms 10000 \
  2>/dev/null >"$TMP/events" || true
# Un mensaje por línea. Los ids publicados de un tipo de evento (opcionalmente con un filtro extra).
published_ids() {   # $1: event_type; $2: expresión adicional que debe cumplir la línea (opcional)
  grep -E "\"event_type\" ?: ?\"$1\"" "$TMP/events" | grep -E "${2:-.}" \
    | grep -oE '"aggregate_id" ?: ?"[0-9a-f-]{36}"' | grep -oE '[0-9a-f-]{36}' | sort -u || true
}
published_ids TransferCompleted >"$TMP/published"
published_ids TransferFailed '"compensated" ?: ?true' >"$TMP/published_failed"
published_ids TransferFailed >"$TMP/any_failed"

total="$(wc -l <"$TMP/completed")"
missing="$(comm -23 "$TMP/completed" "$TMP/published" | wc -l)"
echo "transferencias completadas por el servicio: $total"
echo "con TransferCompleted en Kafka:             $((total - missing))"
if [[ "$missing" -ne 0 ]]; then
  echo "FALLO: sin evento en Kafka:" >&2
  comm -23 "$TMP/completed" "$TMP/published" >&2
  exit 1
fi

failed_total="$(wc -l <"$TMP/failed")"
failed_missing="$(comm -23 "$TMP/failed" "$TMP/published_failed" | wc -l)"
echo "transferencias fallidas por el servicio:    $failed_total"
echo "con TransferFailed compensated=true:        $((failed_total - failed_missing))"
if [[ "$failed_missing" -ne 0 ]]; then
  echo "FALLO: sin TransferFailed con compensated=true (los que tienen el evento con compensated distinto de true se marcan *):" >&2
  comm -23 "$TMP/failed" "$TMP/published_failed" | while read -r id; do
    grep -qx "$id" "$TMP/any_failed" && echo "* $id" >&2 || echo "  $id" >&2
  done
  exit 1
fi
echo "OK: cada transferencia completada y cada fallida tiene su evento en Kafka"
