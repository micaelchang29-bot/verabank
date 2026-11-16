#!/usr/bin/env bash
# make chaos-checks: db/checks/chaos.sql con psql, como make reconcile. Falla si alguna comprobación da f.
. "$(dirname "$0")/lib.sh"

mapfile -t rows < <(psql_transfer -tA -F '|' < db/checks/chaos.sql)
if [[ "${#rows[@]}" -ne 5 ]]; then
  echo "salida inesperada de db/checks/chaos.sql (${#rows[@]} filas): ${rows[*]-}" >&2
  exit 1
fi
bad=0
for row in "${rows[@]}"; do
  IFS='|' read -r name value ok <<<"$row"
  printf '%-52s %s\n' "$name" "$value"
  [[ "$ok" == t ]] || { echo "FALLO: $name = $value" >&2; bad=1; }
done
[[ "$bad" == 0 ]] || exit 1
echo "OK: comprobaciones de chaos en regla"
