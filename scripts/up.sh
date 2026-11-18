#!/usr/bin/env bash
# make up: todo el sistema en Compose (perfiles core, risk y sim): PostgreSQL, Kafka, transfer-service con scoring
# remoto, risk-scoring, risk-stream y el rail simulado; con migraciones de risk, permisos y semillas.
# Orden: transfer-service migra core y audit; después risk migra y recibe permisos; solo entonces arranca risk.
. "$(dirname "$0")/lib.sh"

first_api_key >/dev/null

if service_running; then
  echo "== deteniendo el transfer-service del host (make up-core): comparte puerto con el de Compose"
  "$ROOT/scripts/stop-service.sh"
fi

echo "== imágenes"
"${COMPOSE_ALL[@]}" build risk-scoring transfer-service rail-sim

echo "== PostgreSQL, Kafka y tópicos"
"${COMPOSE_ALL[@]}" up -d --wait postgres kafka
"${COMPOSE_ALL[@]}" run --rm kafka-init >/dev/null
echo "== roles de ejecución de risk"
scripts/db-roles.sh

echo "== transfer-service (scoring remoto; migra core y audit)"
"${COMPOSE_ALL[@]}" up -d --wait transfer-service

echo "== migraciones de risk y analytics, permisos y semillas"
scripts/db-migrate-risk.sh
scripts/db-migrate-analytics.sh
scripts/db-grants.sh
db/seed/load.sh
scripts/seed-risk.sh
scripts/load-country-risk.sh

echo "== risk-scoring, risk-stream y rail simulado"
"${COMPOSE_ALL[@]}" up -d --wait risk-scoring risk-stream rail-sim
"${COMPOSE_ALL[@]}" ps --format 'table {{.Service}}\t{{.Status}}'
echo "transfer-service: ${BASE_URL}   risk-scoring: http://localhost:${RISK_SCORING_PORT:-8083}   risk-stream: http://localhost:${RISK_STREAM_PORT:-8082}"
