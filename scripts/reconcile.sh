#!/usr/bin/env bash
# make reconcile: las dos consultas de conciliación de docs/diseno.md §7 (db/reconcile.sql).
# Falla si alguna devuelve algo distinto de 0.
. "$(dirname "$0")/lib.sh"

mapfile -t counts < <(psql_transfer -tA < db/reconcile.sql)
if [[ "${#counts[@]}" -ne 2 ]]; then
  echo "salida inesperada de db/reconcile.sql: ${counts[*]-}" >&2
  exit 1
fi
echo "cuentas_descuadradas  = ${counts[0]}"
echo "retenciones_huerfanas = ${counts[1]}"
if [[ "${counts[0]}" != 0 || "${counts[1]}" != 0 ]]; then
  echo "FALLO: la conciliación debe dar 0 y 0" >&2
  exit 1
fi
echo "OK: conciliación en cero"
