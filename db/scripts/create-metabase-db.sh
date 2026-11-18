#!/usr/bin/env bash
# make bi-init: base de aplicación de Metabase (D9) en la misma instancia de PostgreSQL. Idempotente: crea el rol
# metabase_app (o actualiza su contraseña) y la base metabase con ese dueño; nadie más se conecta a ella. metabase_app
# no tiene CONNECT sobre la base de negocio (roles.sql revoca la de PUBLIC): los datos los lee Metabase con verabank_bi.
# La contraseña llega de .env (METABASE_DB_PASSWORD) por entorno; no se imprime.
. "$(dirname "$0")/../../scripts/db-lib.sh"

require_env METABASE_DB_PASSWORD
db_exec METABASE_DB_PASSWORD -- sh -c 'exec psql -X -q -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 -v pw="$METABASE_DB_PASSWORD" -f -' <<'SQL'
SELECT 'CREATE ROLE metabase_app LOGIN' WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'metabase_app')
\gexec
ALTER ROLE metabase_app PASSWORD :'pw';
SELECT 'CREATE DATABASE metabase OWNER metabase_app' WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'metabase')
\gexec
REVOKE ALL ON DATABASE metabase FROM PUBLIC;
GRANT CONNECT, TEMPORARY ON DATABASE metabase TO metabase_app;
SQL
echo "base metabase y rol metabase_app listos"
