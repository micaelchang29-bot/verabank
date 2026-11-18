#!/usr/bin/env bash
# make verify-audit: recorre audit.audit_log con audit.verify_chain() (T-16). Sale con 1 si la cadena tiene un
# problema (HASH_MISMATCH, PREV_MISMATCH o GAP). Solo lectura; con el rol de arranque, el único que la ejecuta.
# Límite declarado (db/README.md): sin ancla externa no detecta que se trunque el final ni que se reescriba entera.
. "$(dirname "$0")/db-lib.sh"

from="${FROM:-1}"
[[ "$from" =~ ^[0-9]+$ ]] || { echo "FROM debe ser un entero" >&2; exit 2; }
start=$(date +%s%N)
IFS='|' read -r checked bad problem < <(echo "SELECT checked, coalesce(first_bad_id::text, ''), coalesce(problem, '') FROM audit.verify_chain($from)" | psql_admin -tA)
ms=$(( ($(date +%s%N) - start) / 1000000 ))
echo "filas revisadas = ${checked}   (${ms} ms)"
if [[ -n "$bad" ]]; then
  echo "FALLO: cadena de auditoría alterada en audit_id ${bad}: ${problem}" >&2
  exit 1
fi
echo "OK: cadena de auditoría íntegra"
