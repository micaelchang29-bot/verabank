#!/usr/bin/env bash
# make db-roles: crea (o actualiza la contraseña de) risk_svc y sim_loader. Idempotente; superusuario.
# Los volúmenes nuevos los reciben de db/init/initdb.d/20-roles-runtime.sh; este script los lleva a los existentes.
. "$(dirname "$0")/db-lib.sh"

require_env RISK_SVC_PASSWORD SIM_LOADER_PASSWORD
db_exec RISK_SVC_PASSWORD SIM_LOADER_PASSWORD -- sh -c \
  'exec psql -X -q -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 -v db="$POSTGRES_DB" -v risk_svc_pw="$RISK_SVC_PASSWORD" -v sim_loader_pw="$SIM_LOADER_PASSWORD" -f -' \
  < db/init/roles_runtime.sql
echo "roles risk_svc y sim_loader listos"
