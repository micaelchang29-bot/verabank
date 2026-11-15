#!/usr/bin/env bash
# Carga la semilla local y concilia. Requiere el perfil core levantado y las migraciones de
# core aplicadas. Uso: db/seed/load.sh
set -euo pipefail

cd "$(dirname "$0")/../.."

run_sql() {   # $1: archivo SQL; el psql corre dentro del contenedor, con el rol de transfer-service
  docker compose --profile core exec -T postgres sh -c \
    'PGPASSWORD="$VERABANK_TRANSFER_PASSWORD" psql -h 127.0.0.1 -U verabank_transfer \
       -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 -f -' < "$1"
}

echo "== semilla"
run_sql db/seed/01_core_seed.sql
echo "== conciliación (ambas deben ser 0)"
run_sql db/reconcile.sql
