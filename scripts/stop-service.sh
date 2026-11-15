#!/usr/bin/env bash
# Detiene la instancia de transfer-service que arrancó make up-core.
. "$(dirname "$0")/lib.sh"

if ! service_running; then
  rm -f "$PID_FILE"
  echo "transfer-service no está corriendo"
  exit 0
fi
pid="$(cat "$PID_FILE")"
kill "$pid"
for _ in $(seq 1 30); do
  kill -0 "$pid" 2>/dev/null || { rm -f "$PID_FILE"; echo "transfer-service detenido"; exit 0; }
  sleep 1
done
echo "transfer-service (pid $pid) no se detuvo en 30 s" >&2
exit 1
