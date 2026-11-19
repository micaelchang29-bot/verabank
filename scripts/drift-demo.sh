#!/usr/bin/env bash
# make drift-demo CONFIRM=yes (D3, D4): DESTRUCTIVO, parte de una base limpia.
#   reset-data, up; guarda del reloj; tanda base 577–624 y settle; psi contra `valid` (la referencia que genera alertas);
#   tanda de deriva 625–696 (72 pasos) con montos × 3 y settle; psi; el veredicto (`drift-verdict`). Sale con 0 solo si,
#   con `valid`, la base no alertó y la deriva sí (ventanas 625–648 y 649–672 completas). Además calcula el PSI contra
#   `train` (informativo, nunca alerta) y lo imprime como hallazgo del cambio de régimen cerca del paso 400. Si no se
#   cumple, imprime las tablas y sale con 1 (STOP de D3: no se tocan umbral, ventanas, referencias ni top-5).
. "$(dirname "$0")/lib.sh"

tps="${DEMO_TPS:-100}"
if [[ "${CONFIRM:-}" != "yes" ]]; then
  echo "drift-demo borra los datos de Compose (reset-data). Repite con: make drift-demo CONFIRM=yes" >&2
  exit 1
fi
bi() { "${COMPOSE[@]}" exec -T postgres sh -c 'PGPASSWORD="$VERABANK_BI_PASSWORD" psql -X -q -h 127.0.0.1 -U verabank_bi -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 "$@"' psql "$@"; }
tables() {
  echo "== PSI por feature y ventana, referencia $1"
  bi -c "SELECT feature, string_agg(to_char(window_start, 'MM-DD') || ' ' || to_char(psi, 'FM990.0000') || CASE WHEN insufficient THEN ' (insuf.)' WHEN alert THEN ' ALERTA' ELSE '' END, ' | ' ORDER BY window_start) AS ventanas, bool_or(is_top_feature) AS top FROM analytics.drift_metrics WHERE reference = '$1' GROUP BY feature ORDER BY bool_or(is_top_feature) DESC, feature"
}
# El veredicto (`drift-verdict`) cuenta las alertas de `valid` por la ventana que ocupan, no por cuándo se calcularon: la
# ventana 601–624 de la base solo se completa con la tanda de deriva.

psi_both() {
  scripts/ml-job.sh psi --reference valid
  scripts/ml-job.sh psi --reference train
}

CONFIRM=yes scripts/reset-data.sh
scripts/up.sh
scripts/ml-job.sh clock-guard ${FORCE:+--force}
echo "== tanda base: pasos 577–624"
scripts/sim.sh replay --steps 48 --first-step 577 --tps "$tps"
scripts/sim.sh settle
psi_both
tables valid
echo "== tanda de deriva: pasos 625–696, montos × 3"
scripts/sim.sh replay --steps 72 --first-step 625 --amount-factor 3 --tps "$tps"
scripts/sim.sh settle
psi_both
tables valid
echo "== HALLAZGO (sin criterio de aceptación): la misma base contra la referencia de entrenamiento"
tables train
echo "== SELECT reference, feature, psi FROM analytics.drift_metrics WHERE alert ORDER BY reference, psi DESC"
bi -c "SELECT reference, feature, psi, window_start FROM analytics.drift_metrics WHERE alert ORDER BY reference DESC, psi DESC"
scripts/ml-job.sh drift-verdict
