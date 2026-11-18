#!/usr/bin/env bash
# make reconcile-full (T-17): conciliación de §7 más (a) Σ D <> Σ C por transferencia, (b) transferencias abiertas tras
# el settle y (c) diferencia entre las filas del canal loadtest y lo que k6 dice que las creó: 201 más 422 que crean
# fila (transfer-declined e insufficient-funds: toda solicitud válida deja fila, ADR-03), leídos de
# data/perf/transfers-summary.json. Todas deben dar 0; sale con 1 si no. make reconcile no cambia.
. "$(dirname "$0")/lib.sh"

summary="${SUMMARY:-$ROOT/data/perf/transfers-summary.json}"
declare -A got
while IFS='|' read -r name value; do got[$name]="$value"; done < <(psql_transfer -tA < db/reconcile_full.sql)

fail=0
for k in cuentas_descuadradas retenciones_huerfanas transferencias_con_debe_distinto_de_haber transferencias_abiertas; do
  printf '%-44s = %s\n' "$k" "${got[$k]}"
  [[ "${got[$k]}" == 0 ]] || fail=1
done
if [[ -f "$summary" ]]; then
  expected=$(python3 -c 'import json,sys; c=json.load(open(sys.argv[1]))["counts"]; print(int(c["201"]) + int(c["422_transfer_declined"]) + int(c["422_insufficient_funds"]))' "$summary")
  created=$(python3 -c 'import json,sys; print(int(json.load(open(sys.argv[1]))["counts"]["201"]))' "$summary")
  diff=$(( got[filas_loadtest] - expected ))
  printf '%-44s = %s   (filas %s; k6: 201 = %s, con los 422 que crean fila = %s)\n' "diferencia_k6_frente_a_filas_loadtest" \
    "$diff" "${got[filas_loadtest]}" "$created" "$expected"
  [[ "$diff" == 0 ]] || fail=1
else
  echo "diferencia_k6_frente_a_filas_loadtest = (sin $summary: no hubo carga)"
fi
echo "informativo: compensación de las FAILED p50 / p95 / p99 = ${got[failed_compensacion_p50_p95_p99_s]} (objetivo §1.2: 99 % < 60 s)"
if [[ "$fail" != 0 ]]; then echo "FALLO: reconcile-full debe dar todo 0" >&2; exit 1; fi
echo "OK: reconcile-full en cero"
