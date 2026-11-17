# Funciones comunes de los objetivos de Make. Se carga con: . scripts/lib.sh
# Lee .env (credenciales locales) y define rutas y puertos. Nunca imprime secretos.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [[ ! -f .env ]]; then
  echo "falta .env: cópialo de .env.example y completa los valores" >&2
  exit 2
fi
# El entorno del llamador tiene prioridad sobre .env (p. ej. SERVER_PORT=9090 make up-core).
_saved_port="${SERVER_PORT-}"
set -a
# shellcheck disable=SC1091
. ./.env
set +a
[[ -n "$_saved_port" ]] && SERVER_PORT="$_saved_port"
export SERVER_PORT="${SERVER_PORT:-8081}"

COMPOSE=(docker compose --profile core)
COMPOSE_ALL=(docker compose --profile core --profile risk --profile sim)
SERVICE_DIR="$ROOT/transfer-service"
RUN_DIR="$SERVICE_DIR/target/run"
PID_FILE="$RUN_DIR/transfer-service.pid"
LOG_FILE="$RUN_DIR/transfer-service.log"
BASE_URL="http://localhost:${SERVER_PORT}"

# psql dentro del contenedor, con el rol de transfer-service. Lee SQL de la entrada estándar.
psql_transfer() {
  "${COMPOSE[@]}" exec -T postgres sh -c \
    'PGPASSWORD="$VERABANK_TRANSFER_PASSWORD" psql -X -h 127.0.0.1 -U verabank_transfer -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 "$@"' \
    psql "$@"
}

# Primera clave de VERABANK_API_KEYS ("canal:clave,…"), sin mostrarla.
first_api_key() {
  local first="${VERABANK_API_KEYS%%,*}"
  if [[ -z "$first" || "$first" != *:* ]]; then
    echo "VERABANK_API_KEYS no está definido en .env (formato canal:clave)" >&2
    return 2
  fi
  printf '%s' "${first#*:}"
}

# UUID de una fila de la semilla: md5('verabank-seed:...')::uuid, igual que db/seed.
seed_uuid() {
  local h
  h="$(printf '%s' "verabank-seed:$1" | md5sum | cut -c1-32)"
  printf '%s-%s-%s-%s-%s' "${h:0:8}" "${h:8:4}" "${h:12:4}" "${h:16:4}" "${h:20:12}"
}

service_running() {
  [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null
}
