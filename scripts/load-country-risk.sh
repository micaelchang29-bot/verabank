#!/usr/bin/env bash
# make load-country-risk [CSV=ruta]: carga db/seed/country_risk.csv en risk.country_risk con
# valid_from = list_date. El CSV es una instantánea completa (todas las filas con la misma list_date).
# Al cargar una lista más nueva se cierra (valid_to = list_date) toda vigencia anterior, también la de los
# países que salieron de la lista. Idempotente: repetir la misma lista no cambia nada. Rechaza una lista
# más antigua que la vigente. Corre como risk_svc.
#
# VALID_FROM=AAAA-MM-DD (opcional): aplica la lista RETROACTIVAMENTE, con esa fecha como inicio de vigencia en vez de
# list_date ("lista aplicada retroactivamente al periodo simulado"). Existe porque la lista real es de octubre de 2026
# y el generador AML simula enero de 2026: las reglas consultan el nivel vigente en occurred_at (D6) y, sin esto, no
# habría ninguno. Debe ser anterior o igual a list_date. Si esa lista ya estaba cargada con un valid_from posterior, se
# adelanta (UPDATE de valid_from). No es el uso normal: sin la variable, valid_from = list_date.
. "$(dirname "$0")/db-lib.sh"

csv="${1:-db/seed/country_risk.csv}"
valid_from="${VALID_FROM:-}"
[[ -f "$csv" ]] || { echo "no existe $csv" >&2; exit 1; }

# CSV simple (sin comillas ni comas dentro de los campos) -> filas VALUES de SQL, validadas.
values="$(awk -F, '
  NR == 1 { if ($0 != "country,level,source,list_date") { print "cabecera inesperada: " $0 > "/dev/stderr"; exit 1 } ; next }
  NF == 0 || $0 == "" { next }
  NF != 4 { print "línea " NR ": se esperaban 4 campos" > "/dev/stderr"; exit 1 }
  $1 !~ /^[A-Z][A-Z]$/ { print "línea " NR ": país no alfa-2: " $1 > "/dev/stderr"; exit 1 }
  $2 !~ /^(HIGH|WATCH)$/ { print "línea " NR ": nivel inválido: " $2 > "/dev/stderr"; exit 1 }
  $4 !~ /^[0-9]{4}-[0-9]{2}-[0-9]{2}$/ { print "línea " NR ": fecha inválida: " $4 > "/dev/stderr"; exit 1 }
  { gsub(/\047/, "\047\047", $3); printf "%s(\047%s\047, \047%s\047, \047%s\047, DATE \047%s\047)", (n++ ? ",\n" : ""), $1, $2, $3, $4 }
  END { if (n == 0) { print "el CSV no tiene filas" > "/dev/stderr"; exit 1 } }
' "$csv")"

dates="$(awk -F, 'NR > 1 && $0 != "" { print $4 }' "$csv" | sort -u)"
if [[ "$(wc -l <<<"$dates")" -ne 1 ]]; then
  echo "el CSV mezcla fechas de lista distintas: ${dates//$'\n'/, }" >&2
  exit 1
fi
if [[ -n "$valid_from" ]]; then
  [[ "$valid_from" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]] || { echo "VALID_FROM inválida: $valid_from" >&2; exit 1; }
  if [[ "$valid_from" > "$dates" ]]; then
    echo "VALID_FROM ($valid_from) no puede ser posterior a la fecha de la lista ($dates)" >&2
    exit 1
  fi
  echo "AVISO: lista aplicada retroactivamente al periodo simulado (valid_from = $valid_from, lista del $dates)" >&2
fi
vf="${valid_from:-$dates}"
current="$(psql_as risk_svc RISK_SVC_PASSWORD -tA -c 'SELECT coalesce(max(valid_from)::text, $$0001-01-01$$) FROM risk.country_risk')"
if [[ -z "$valid_from" && "$dates" < "$current" ]]; then
  echo "la lista del CSV ($dates) es más antigua que la vigente ($current); no se carga" >&2
  exit 1
fi

psql_as risk_svc RISK_SVC_PASSWORD -q <<SQL
BEGIN;
-- Retroactivo: la misma lista ya cargada con un valid_from posterior se adelanta.
UPDATE risk.country_risk SET valid_from = DATE '${vf}'
 WHERE list_date = DATE '${dates}' AND valid_from > DATE '${vf}';
UPDATE risk.country_risk
   SET valid_to = DATE '${vf}'
 WHERE valid_to IS NULL AND valid_from < DATE '${vf}' AND list_date < DATE '${dates}';
INSERT INTO risk.country_risk (country, level, source, list_date, valid_from)
SELECT country, level, source, list_date, DATE '${vf}'
FROM (VALUES
${values}
) AS v(country, level, source, list_date)
ON CONFLICT (country, valid_from) DO NOTHING;
COMMIT;
SQL
psql_as risk_svc RISK_SVC_PASSWORD -tA -c \
  "SELECT 'vigentes: ' || count(*) FILTER (WHERE level = 'HIGH') || ' HIGH, ' || count(*) FILTER (WHERE level = 'WATCH') || ' WATCH (lista de ${dates})' FROM risk.country_risk WHERE valid_to IS NULL"
