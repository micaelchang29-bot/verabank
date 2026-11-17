#!/bin/bash
# Se ejecuta al inicializar el volumen, después de 10-roles.sh. Los volúmenes que ya existen
# reciben estos roles con scripts/db-roles.sh (make db-roles, que make up-core ejecuta).
set -euo pipefail

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  -v db="$POSTGRES_DB" \
  -v risk_svc_pw="$RISK_SVC_PASSWORD" \
  -v sim_loader_pw="$SIM_LOADER_PASSWORD" \
  -f /sql/roles_runtime.sql
