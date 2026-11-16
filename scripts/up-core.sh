#!/usr/bin/env bash
# make up-core: PostgreSQL y Kafka (perfil core), tópicos, transfer-service en el puerto
# SERVER_PORT (8081) con sus migraciones, y la semilla local.
. "$(dirname "$0")/lib.sh"

first_api_key >/dev/null

echo "== compose (perfil core)"
"${COMPOSE[@]}" up -d --wait postgres kafka
"${COMPOSE[@]}" run --rm kafka-init >/dev/null
echo "tópicos listos"

echo "== compilando transfer-service"
(cd "$SERVICE_DIR" && ./mvnw -B -q -DskipTests package)

if service_running; then
  echo "== deteniendo la instancia anterior (pid $(cat "$PID_FILE"))"
  "$ROOT/scripts/stop-service.sh"
fi
if (exec 3<>"/dev/tcp/127.0.0.1/${SERVER_PORT}") 2>/dev/null; then
  echo "el puerto ${SERVER_PORT} ya está en uso; detén ese proceso o usa SERVER_PORT=<otro>" >&2
  exit 1
fi

echo "== arrancando transfer-service en el puerto ${SERVER_PORT}"
mkdir -p "$RUN_DIR"
# Los secretos van por entorno, no por argumentos (no aparecen en ps).
# Solo las credenciales del rol del servicio: las del superusuario y otros roles se quitan.
env -u POSTGRES_PASSWORD -u VERABANK_RISK_PASSWORD -u VERABANK_BI_PASSWORD \
POSTGRES_HOST=localhost \
KAFKA_BOOTSTRAP_SERVERS="localhost:${KAFKA_EXTERNAL_PORT:-29092}" \
VERABANK_MIGRATIONS_DIR="$ROOT/db/migrations" \
VERABANK_CONTRACTS_DIR="$ROOT/contracts" \
  nohup java -jar "$SERVICE_DIR/target/transfer-service.jar" >"$LOG_FILE" 2>&1 &
echo $! >"$PID_FILE"

for _ in $(seq 1 90); do
  if curl -fsS "${BASE_URL}/actuator/health/readiness" >/dev/null 2>&1; then
    echo "transfer-service listo: ${BASE_URL} (log: ${LOG_FILE#"$ROOT"/})"
    echo "== semilla local"
    db/seed/load.sh
    exit 0
  fi
  if ! service_running; then
    echo "transfer-service terminó al arrancar; últimas líneas del log:" >&2
    tail -n 40 "$LOG_FILE" >&2
    exit 1
  fi
  sleep 1
done
echo "transfer-service no respondió en 90 s; últimas líneas del log:" >&2
tail -n 40 "$LOG_FILE" >&2
exit 1
