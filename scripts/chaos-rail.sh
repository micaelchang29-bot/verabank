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
HEARTBEAT_FILE="$STATE_DIR/chaos-rail.heartbeat"
# Un log por corrida, con marca de tiempo; no se sobrescribe y se conservan los últimos LOG_KEEP de cada tipo.
LOG_DIR="$STATE_DIR/logs"
LOG_KEEP="${LOG_KEEP:-10}"
RUN_ID="$(date +%Y%m%d-%H%M%S)-$$"
SIM_LOG="$LOG_DIR/rail-sim-$RUN_ID.log"
RUN_LOG="$LOG_DIR/chaos-rail-$RUN_ID.log"
HEARTBEAT_MAX_SILENCE="${HEARTBEAT_MAX_SILENCE:-30}"
SIM_PID=""
CHAOS_PID=""
WATCH_PID=""
mkdir -p "$STATE_DIR" "$LOG_DIR"
for prefix in rail-sim chaos-rail; do
  # shellcheck disable=SC2012
  ls -1t "$LOG_DIR/$prefix-"*.log 2>/dev/null | tail -n +"$((LOG_KEEP))" | xargs -r rm -f -- || true
done
exec > >(tee -a "$RUN_LOG") 2>&1
echo "== logs de esta corrida: ${RUN_LOG#"$ROOT"/} y ${SIM_LOG#"$ROOT"/}"

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
stop_helpers() {
  for pid in "$CHAOS_PID" "$WATCH_PID"; do
    [[ -n "$pid" ]] && kill -TERM -- "-$pid" 2>/dev/null || { [[ -n "$pid" ]] && kill -TERM "$pid" 2>/dev/null; } || true
  done
}
trap 'stop_helpers; stop_sim' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

echo "== servicio con tiempos acelerados (SAGA_RAIL_REPLY_TIMEOUT=5s, SAGA_POLL_BACKOFF_BASE=2s)"
SAGA_RAIL_REPLY_TIMEOUT=5s SAGA_POLL_BACKOFF_BASE=2s "$ROOT/scripts/up-core.sh"

echo "== rail-sim: reject=$REJECT dup=$DUP ($DUP_MODE) silent=$SILENT lost=$LOST delay=$DELAY"
rm -f "$READY_FILE" "$HEARTBEAT_FILE" "$STATE_DIR/chaos-rail.db"*
(cd "$SIM_DIR" && uv sync --frozen -q)
(cd "$SIM_DIR" && exec setsid uv run --no-sync python -m verabank_sim rail \
  --reject-rate "$REJECT" --dup-rate "$DUP" --dup-mode "$DUP_MODE" --silent-rate "$SILENT" \
  --lost-rate "$LOST" --delay-ms "$DELAY" --state-db "$STATE_DIR/chaos-rail.db" \
  --ready-file "$READY_FILE" --heartbeat-file "$HEARTBEAT_FILE") >"$SIM_LOG" 2>&1 &
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
echo "== chaos: n=$N concurrencia=$CONCURRENCY timeout=${CHAOS_TIMEOUT}s (latido del rail vigilado cada 5 s, máx ${HEARTBEAT_MAX_SILENCE} s)"
# chaos y la vigilancia corren a la vez: si el rail se cae o deja de latir, la vigilancia lo dice con las
# trazas y detiene chaos en vez de esperar al --timeout para descubrirlo.
(cd "$SIM_DIR" && exec setsid uv run --no-sync python -m verabank_sim chaos --n "$N" --concurrency "$CONCURRENCY" \
  --timeout "$CHAOS_TIMEOUT" --reject-rate "$REJECT" --lost-rate "$LOST") &
CHAOS_PID=$!
(cd "$SIM_DIR" && exec uv run --no-sync python -m verabank_sim watch --sim-pid "$SIM_PID" --target-pid "$CHAOS_PID" \
  --heartbeat-file "$HEARTBEAT_FILE" --log "$SIM_LOG" --max-silence "$HEARTBEAT_MAX_SILENCE" --interval 5) &
WATCH_PID=$!
chaos_rc=0; wait "$CHAOS_PID" || chaos_rc=$?
CHAOS_PID=""
watch_rc=0; wait "$WATCH_PID" || watch_rc=$?
WATCH_PID=""
if [[ "$watch_rc" == 3 ]]; then
  echo "FALLO: simulador del rail caído o bloqueado durante chaos (trazas en ${SIM_LOG#"$ROOT"/})" >&2
  rc=1
elif [[ "$chaos_rc" != 0 ]]; then
  echo "FALLO: chaos" >&2; rc=1
elif [[ "$watch_rc" != 0 ]]; then
  echo "FALLO: la vigilancia del latido terminó con código $watch_rc" >&2; rc=1
fi

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
