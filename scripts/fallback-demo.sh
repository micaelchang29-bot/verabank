#!/usr/bin/env bash
# make fallback-demo (ADR-04): con risk-scoring parado, 500 transferencias del replay deben decidirse por FALLBACK;
# al reiniciarlo, el circuito cierra y una tanda corta vuelve a MODEL. Necesita make up.
# Las filas del replay son nuevas (START_ROW, por defecto 20000, justo después de make replay N=20000), porque la
# Idempotency-Key es determinista y repetir filas devolvería lo ya decidido.
. "$(dirname "$0")/lib.sh"

START_ROW="${START_ROW:-20000}"
RECOVERY_ROWS="${RECOVERY_ROWS:-100}"
TPS="${TPS:-50}"

count_by_source() {   # $1: instante UTC de inicio; imprime "FUENTE=n" por línea, ordenadas
  psql_transfer -At -c "SELECT coalesce(decision_source, 'NULL') || '=' || count(*) FROM core.transfers
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
t1="$(date -u +%FT%T.%6NZ)"
"${COMPOSE_ALL[@]}" up -d --wait risk-scoring >/dev/null
echo "risk-scoring listo en $(($(date +%s) - t_restart)) s"
# El circuito sigue abierto hasta que vence open-duration (10 s) y sus llamadas de prueba salen bien.
sleep 12

echo "== 5. tanda corta ($RECOVERY_ROWS filas): debe volver a MODEL"
scripts/sim.sh replay --n "$RECOVERY_ROWS" --tps "$TPS" --start-row "$((START_ROW + 500))" --no-labels \
  --out ../data/replay/fallback-demo-recovery
got="$(count_by_source "$t1" | tr '\n' ' ')"
echo "decision_source desde el reinicio: ${got:-ninguna}"
if [[ "$got" != "MODEL=$RECOVERY_ROWS " ]]; then
  echo "FALLO: se esperaba exactamente MODEL=$RECOVERY_ROWS" >&2
  exit 1
fi
echo "fallback-demo correcto: 500 FALLBACK con el scoring parado y $RECOVERY_ROWS MODEL después del reinicio"
