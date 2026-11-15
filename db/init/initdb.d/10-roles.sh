#!/bin/bash
# Se ejecuta una sola vez, al inicializar el volumen de datos de PostgreSQL.
# Las contraseñas llegan por variables de entorno (.env), no están en el repositorio.
set -euo pipefail

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  -v db="$POSTGRES_DB" \
  -v admin="$POSTGRES_USER" \
  -v transfer_pw="$VERABANK_TRANSFER_PASSWORD" \
  -v risk_pw="$VERABANK_RISK_PASSWORD" \
  -v bi_pw="$VERABANK_BI_PASSWORD" \
  -f /sql/roles.sql
