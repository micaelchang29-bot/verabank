#!/usr/bin/env bash
# make drift-demo CONFIRM=yes (D3, D4): DESTRUCTIVO, parte de una base limpia.
#   reset-data, up; guarda del reloj; tanda base 577–624 y settle; psi contra la referencia de validación (no debe
#   alertar); tanda de deriva 625–672 con montos × 3 y settle; psi; las alertas. Sale con 0 solo si, con `valid`, la base
#   no alertó y la deriva sí. Además calcula el PSI contra `train` y lo imprime como hallazgo del cambio de régimen cerca
#   del paso 400 (sin criterio de aceptación). Si no se cumple, imprime las tablas y sale con 1 (STOP de D3: no se
#   tocan umbral, ventanas, referencias ni top-5).
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
# Alertas por tramo, según la ventana en que caen (no según cuándo se calcularon): la ventana 600 de la base solo se
# completa con la tanda de deriva. epoch + 576 h = inicio del paso 577; + 624 h = inicio del 625; + 672 h = fin del 672.
epoch="$(sed -n 's/^sim_epoch: *"\(.*\)".*/\1/p' ml/config.yaml)"
alerts_between() { bi -tA -c "SELECT count(*) FROM analytics.drift_metrics WHERE alert AND reference = 'valid' AND window_start >= timestamptz '${epoch}' + interval '$1 hours' AND window_start < timestamptz '${epoch}' + interval '$2 hours'"; }

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
echo "== tanda de deriva: pasos 625–672, montos × 3"
scripts/sim.sh replay --steps 48 --first-step 625 --amount-factor 3 --tps "$tps"
scripts/sim.sh settle
psi_both
tables valid
echo "== HALLAZGO (sin criterio de aceptación): la misma base contra la referencia de entrenamiento"
tables train
echo "== SELECT reference, feature, psi FROM analytics.drift_metrics WHERE alert ORDER BY reference, psi DESC"
bi -c "SELECT reference, feature, psi, window_start FROM analytics.drift_metrics WHERE alert ORDER BY reference DESC, psi DESC"
base_alerts=$(alerts_between 576 624)
final_alerts=$(alerts_between 624 672)
echo "alertas con la referencia valid en ventanas de la base (577–624): ${base_alerts}; en ventanas de la deriva (625–672): ${final_alerts}"
if [[ "$base_alerts" != 0 ]]; then
  echo "STOP (D3): la tanda base ya alerta (${base_alerts} filas). No se cambian umbral, ventanas, referencias ni top-5." >&2
  exit 1
fi
if [[ "$final_alerts" == 0 ]]; then
  echo "STOP (D3): la deriva no alertó. No se cambian umbral, ventanas, referencias ni top-5." >&2
  exit 1
fi
echo "OK: con la referencia valid la base no alertó y la deriva sí (${final_alerts} filas con alert)"
