#!/bin/bash
# Crea los tópicos de docs/diseno.md §3.1 (replicación 1: laptop de un solo broker).
# Idempotente: --if-not-exists no modifica tópicos ya existentes.
set -euo pipefail

BOOTSTRAP="${BOOTSTRAP_SERVER:-kafka:9092}"
KAFKA_TOPICS=/opt/kafka/bin/kafka-topics.sh

DAY=86400000
# nombre:particiones:retención_ms
TOPICS=(
  "transfer.events.v1:6:$((7 * DAY))"
  "rail.commands.v1:3:$((1 * DAY))"
  "rail.replies.v1:3:$((1 * DAY))"
  "fraud.labels.v1:3:$((30 * DAY))"
  "aml.alerts.v1:3:$((30 * DAY))"
)
# Tópicos de entrada: los que consume algún servicio. Cada uno tiene su .dlt
# (1 partición, 14 días) para los mensajes que fallan tras 3 reintentos.
# aml.alerts.v1 no tiene consumidor todavía (integraciones futuras): sin .dlt.
INPUT_TOPICS=(transfer.events.v1 rail.commands.v1 rail.replies.v1 fraud.labels.v1)
for t in "${INPUT_TOPICS[@]}"; do
  TOPICS+=("${t}.dlt:1:$((14 * DAY))")
done

for spec in "${TOPICS[@]}"; do
  IFS=: read -r name partitions retention <<<"$spec"
  echo "creando ${name} (particiones=${partitions}, retention.ms=${retention})"
  "$KAFKA_TOPICS" --bootstrap-server "$BOOTSTRAP" --create --if-not-exists \
    --topic "$name" --partitions "$partitions" --replication-factor 1 \
    --config "retention.ms=${retention}"
done

echo "--- tópicos creados"
"$KAFKA_TOPICS" --bootstrap-server "$BOOTSTRAP" --describe | grep -E '^Topic:'
