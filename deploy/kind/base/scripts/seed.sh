#!/bin/sh
# Semillas del clúster kind (Job bootstrap), equivalentes a las de make up: db/seed/01_core_seed.sql con conciliación
# en 0 (db/seed/load.sh), CLEARING exenta de AML (scripts/seed-risk.sh), la lista de países de riesgo con
# valid_from = list_date (primera carga de scripts/load-country-risk.sh) y la base de Metabase con su rol
# (db/scripts/create-metabase-db.sh). Idempotente. Las contraseñas llegan por entorno (Secret verabank-env).
set -eu

as() (   # as ROL VARIABLE [args]: psql con ese rol y la contraseña de esa variable; SQL por la entrada estándar
  role="$1"; pw="$2"; shift 2
  eval "PGPASSWORD=\"\${$pw}\""
  export PGPASSWORD
  exec psql -X -q -h "$PGHOST" -U "$role" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 "$@"
)

echo "== esperando los permisos de db-migrate"
until as "$POSTGRES_USER" POSTGRES_PASSWORD -tA -c \
  "SELECT has_table_privilege('risk_svc', 'risk.country_risk', 'INSERT') AND has_table_privilege('ml_job', 'analytics.drift_metrics', 'INSERT')" \
  2>/dev/null | grep -qx t; do
  sleep 3
done

echo "== semilla de core y conciliación"
as verabank_transfer VERABANK_TRANSFER_PASSWORD < /seed/01_core_seed.sql
counts="$(as verabank_transfer VERABANK_TRANSFER_PASSWORD -tA < /seed/reconcile.sql | tr '\n' ' ')"
[ "$counts" = "0 0 " ] || { echo "conciliación distinta de 0 y 0: $counts" >&2; exit 1; }

echo "== CLEARING exenta de reglas AML"
as risk_svc RISK_SVC_PASSWORD <<'SQL'
INSERT INTO risk.aml_exempt_accounts (account_id, reason)
VALUES (md5('verabank-seed:clearing')::uuid, 'Cuenta técnica de compensación (CLEARING): no genera alertas AML')
ON CONFLICT (account_id) DO NOTHING;
SQL

echo "== países de riesgo"
values="$(awk -F, '
  NR == 1 { if ($0 != "country,level,source,list_date") { print "cabecera inesperada" > "/dev/stderr"; exit 1 }; next }
  $0 == "" { next }
  NF != 4 || $1 !~ /^[A-Z][A-Z]$/ || $2 !~ /^(HIGH|WATCH)$/ || $4 !~ /^[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]$/ { print "línea " NR " inválida" > "/dev/stderr"; exit 1 }
  { gsub(/\047/, "\047\047", $3); printf "%s(\047%s\047, \047%s\047, \047%s\047, DATE \047%s\047)", (n++ ? ",\n" : ""), $1, $2, $3, $4 }
' /seed/country_risk.csv)"
as risk_svc RISK_SVC_PASSWORD <<SQL
INSERT INTO risk.country_risk (country, level, source, list_date, valid_from)
SELECT country, level, source, list_date, list_date FROM (VALUES
${values}
) AS v(country, level, source, list_date)
ON CONFLICT (country, valid_from) DO NOTHING;
SQL

echo "== base de aplicación de Metabase"
as "$POSTGRES_USER" POSTGRES_PASSWORD -v pw="$METABASE_DB_PASSWORD" <<'SQL'
SELECT 'CREATE ROLE metabase_app LOGIN' WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'metabase_app')
\gexec
ALTER ROLE metabase_app PASSWORD :'pw';
SELECT 'CREATE DATABASE metabase OWNER metabase_app' WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'metabase')
\gexec
REVOKE ALL ON DATABASE metabase FROM PUBLIC;
GRANT CONNECT, TEMPORARY ON DATABASE metabase TO metabase_app;
SQL
echo "semillas listas"
