#!/usr/bin/env bash
# make db-grants: permisos de risk_svc, sim_loader, ml_job y BI sobre risk, audit y analytics (db/init/grants.sql).
# Idempotente. Requiere las migraciones de core, audit, risk y analytics y los roles de ejecución ya creados.
. "$(dirname "$0")/db-lib.sh"

psql_admin -q < db/init/grants.sql
echo "permisos de risk_svc, sim_loader, ml_job y BI aplicados"
