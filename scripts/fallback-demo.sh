#!/usr/bin/env bash
# make fallback-demo (ADR-04): con risk-scoring parado, 500 transferencias del replay deben decidirse por FALLBACK;
# al reiniciarlo, el circuito cierra y una tanda corta vuelve a MODEL. Necesita make up.
# Las filas del replay son nuevas (START_ROW, por defecto 20000, justo después de make replay N=20000), porque la
# Idempotency-Key es determinista y repetir filas devolvería lo ya decidido.
. "$(dirname "$0")/lib.sh"

START_ROW="${START_ROW:-20000}"
RECOVERY_ROWS="${RECOVERY_ROWS:-20}"
RECOVERY_PAUSE="${RECOVERY_PAUSE:-5}"
RECOVERY_TRIES="${RECOVERY_TRIES:-12}"
TPS="${TPS:-50}"

count_by_source() {   # $1: instante UTC de inicio; imprime "FUENTE=n" por línea, ordenadas
  psql_transfer -At -F = -c "SELECT coalesce(decision_source, 'NULL'), count(*) FROM core.transfers
                              WHERE client_id <> 'sim-seed' AND created_at >= '$1' GROUP BY 1 ORDER BY 1"
}

echo "== 1. parando risk-scoring"
"${COMPOSE_ALL[@]}" stop risk-scoring >/dev/null
t0="$(date -u +%FT%T.%6NZ)"

echo "== 2. replay de 500 transferencias nuevas (filas desde $START_ROW)"
scripts/sim.sh replay --n 500 --tps "$TPS" --start-row "$START_ROW" --no-labels --out ../data/replay/fallback-demo

echo "== 3. verificación SQL: las 500 deben ser FALLBACK"
got="$(count_by_source "$t0" | tr '\n' ' ')"
echo "decision_source de las transferencias creadas desde $t0: ${got:-ninguna}"
if [[ "$got" != "FALLBACK=500 " ]]; then
  echo "FALLO: se esperaba exactamente FALLBACK=500" >&2
  exit 1
fi

echo "== 4. reiniciando risk-scoring"
t_restart="$(date +%s)"
restart_utc="$(date -u +%FT%T.%6NZ)"
"${COMPOSE_ALL[@]}" up -d --wait risk-scoring >/dev/null
echo "risk-scoring listo (readiness) en $(($(date +%s) - t_restart)) s"

# El circuito sigue abierto hasta vencer open-duration (10 s) y solo cierra si sus llamadas de prueba salen bien.
# Se miden tandas cortas hasta que una sale entera con MODEL; así queda medido cuánto tarda en recuperarse.
echo "== 5. tandas de $RECOVERY_ROWS filas cada ${RECOVERY_PAUSE}s hasta que una salga entera con MODEL (máx. $RECOVERY_TRIES)"
for attempt in $(seq 1 "$RECOVERY_TRIES"); do
  t1="$(date -u +%FT%T.%6NZ)"
  scripts/sim.sh replay --n "$RECOVERY_ROWS" --tps "$TPS" --start-row "$((START_ROW + 500 + attempt * RECOVERY_ROWS))" \
    --no-warmup --no-labels --out ../data/replay/fallback-demo-recovery >/dev/null 2>&1
  got="$(count_by_source "$t1" | tr '\n' ' ')"
  echo "tanda $attempt a los $(($(date +%s) - t_restart)) s del reinicio: ${got:-ninguna}"
  if [[ "$got" == "MODEL=$RECOVERY_ROWS " ]]; then
    first="$(psql_transfer -At -c "SELECT round(extract(epoch FROM min(created_at) - '$restart_utc'::timestamptz)::numeric, 1)
                                   FROM core.transfers WHERE decision_source = 'MODEL' AND created_at >= '$restart_utc'")"
    echo "primera decisión MODEL: ${first} s después de pedir el reinicio (incluye la carga del CSV de cada tanda)"
    echo "fallback-demo correcto: 500 FALLBACK con el scoring parado y MODEL de nuevo a los $(($(date +%s) - t_restart)) s del reinicio"
    exit 0
  fi
  sleep "$RECOVERY_PAUSE"
done
echo "FALLO: el scoring no volvió a MODEL en $RECOVERY_TRIES tandas" >&2
exit 1
