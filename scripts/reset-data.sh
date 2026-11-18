#!/usr/bin/env bash
# make reset-data CONFIRM=yes: DESTRUCTIVO. Detiene todo el Compose y borra sus volúmenes (PostgreSQL, Kafka, estado
# del rail y Metabase): `docker compose down -v --remove-orphans`. Sin CONFIRM=yes no ejecuta nada: imprime qué
# volúmenes borraría y sale con 1. Después, `make up` parte de una base limpia.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
PROJECT=verabank

mapfile -t volumes < <(docker volume ls -q --filter "label=com.docker.compose.project=${PROJECT}")
if [[ "${CONFIRM:-}" != "yes" ]]; then
  echo "reset-data es destructivo y no se ejecutó. Borraría ${#volumes[@]} volúmenes de Compose:" >&2
  for v in "${volumes[@]}"; do echo "  $v" >&2; done
  echo "Repite con: make reset-data CONFIRM=yes" >&2
  exit 1
fi

if [[ -f transfer-service/target/run/transfer-service.pid ]] && kill -0 "$(cat transfer-service/target/run/transfer-service.pid)" 2>/dev/null; then
  scripts/stop-service.sh   # el transfer-service del host (make up-core) quedaría apuntando a una base borrada
fi
# down no necesita las contraseñas, pero Compose interpola el archivo entero: valores ficticios solo aquí.
VERABANK_API_KEYS="${VERABANK_API_KEYS:-unused}" RISK_SVC_PASSWORD="${RISK_SVC_PASSWORD:-unused}" \
SIM_LOADER_PASSWORD="${SIM_LOADER_PASSWORD:-unused}" ML_JOB_PASSWORD="${ML_JOB_PASSWORD:-unused}" \
  docker compose --profile core --profile risk --profile sim --profile bi down -v --remove-orphans
echo "datos borrados: ${#volumes[@]} volúmenes"
