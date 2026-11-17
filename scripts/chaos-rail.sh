#!/usr/bin/env bash
# make chaos-rail: rail simulado con caos + carga de transferencias EXTERNAL + comprobaciones.
# Parámetros por entorno (los fija el Makefile): REJECT DUP DUP_MODE SILENT LOST DELAY N.
# No imprime ni registra claves de API.
. "$(dirname "$0")/lib.sh"

: "${REJECT:=0}" "${DUP:=0}" "${DUP_MODE:=same-id}" "${SILENT:=0.02}" "${LOST:=0.01}" "${DELAY:=20:300}" "${N:=2000}"
CONCURRENCY="${CONCURRENCY:-20}"
CHAOS_TIMEOUT="${CHAOS_TIMEOUT:-180}"

SIM_DIR="$ROOT/simulator"
STATE_DIR="$SIM_DIR/.state"
READY_FILE="$STATE_DIR/chaos-rail.ready"
SIM_LOG="$STATE_DIR/chaos-rail-sim.log"
SIM_PID=""
mkdir -p "$STATE_DIR"

stop_sim() {
  [[ -n "$SIM_PID" ]] || return 0
  if kill -0 "$SIM_PID" 2>/dev/null; then
    kill -TERM -- "-$SIM_PID" 2>/dev/null || kill -TERM "$SIM_PID" 2>/dev/null || true   # grupo propio (setsid)
    for _ in $(seq 1 20); do kill -0 "$SIM_PID" 2>/dev/null || break; sleep 0.5; done
    kill -KILL -- "-$SIM_PID" 2>/dev/null || true
    echo "rail-sim detenido"
  fi
  SIM_PID=""
}
trap stop_sim EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

echo "== servicio con tiempos acelerados (SAGA_RAIL_REPLY_TIMEOUT=5s, SAGA_POLL_BACKOFF_BASE=2s)"
SAGA_RAIL_REPLY_TIMEOUT=5s SAGA_POLL_BACKOFF_BASE=2s "$ROOT/scripts/up-core.sh"

echo "== rail-sim: reject=$REJECT dup=$DUP ($DUP_MODE) silent=$SILENT lost=$LOST delay=$DELAY"
rm -f "$READY_FILE" "$STATE_DIR/chaos-rail.db"*
(cd "$SIM_DIR" && uv sync --frozen -q)
(cd "$SIM_DIR" && exec setsid uv run --no-sync python -m verabank_sim rail \
  --reject-rate "$REJECT" --dup-rate "$DUP" --dup-mode "$DUP_MODE" --silent-rate "$SILENT" \
  --lost-rate "$LOST" --delay-ms "$DELAY" --state-db "$STATE_DIR/chaos-rail.db" \
  --ready-file "$READY_FILE") >"$SIM_LOG" 2>&1 &
SIM_PID=$!
for _ in $(seq 1 120); do
  [[ -e "$READY_FILE" ]] && break
  if ! kill -0 "$SIM_PID" 2>/dev/null; then
    echo "rail-sim terminó al arrancar; últimas líneas de ${SIM_LOG#"$ROOT"/}:" >&2
    tail -n 30 "$SIM_LOG" >&2
    SIM_PID=""
    exit 1
  fi
  sleep 0.5
done
[[ -e "$READY_FILE" ]] || { echo "rail-sim no quedó listo en 60 s" >&2; tail -n 30 "$SIM_LOG" >&2; exit 1; }
echo "rail-sim listo (pid $SIM_PID)"

rc=0
echo "== chaos: n=$N concurrencia=$CONCURRENCY timeout=${CHAOS_TIMEOUT}s"
(cd "$SIM_DIR" && uv run --no-sync python -m verabank_sim chaos --n "$N" --concurrency "$CONCURRENCY" \
  --timeout "$CHAOS_TIMEOUT" --reject-rate "$REJECT" --lost-rate "$LOST") || { echo "FALLO: chaos" >&2; rc=1; }

stop_sim

echo "== make reconcile"
"$ROOT/scripts/reconcile.sh" || rc=1
echo "== make check-events"
"$ROOT/scripts/check-events.sh" || rc=1
echo "== db/checks/chaos.sql"
"$ROOT/scripts/chaos-checks.sh" || rc=1

if [[ "$rc" == 0 ]]; then
  echo "OK: chaos-rail sin hallazgos"
else
  echo "FALLO: chaos-rail tiene hallazgos (ver arriba)" >&2
fi
echo "nota: transfer-service sigue corriendo con tiempos acelerados; make up-core lo reinicia con los de siempre"
exit "$rc"
