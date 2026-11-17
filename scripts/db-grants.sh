#!/usr/bin/env bash
# make db-grants: permisos de risk_svc, sim_loader y BI sobre risk y audit (db/init/grants.sql).
# Idempotente. Requiere las migraciones de core, audit y risk y los roles de ejecución ya creados.
. "$(dirname "$0")/db-lib.sh"

psql_admin -q < db/init/grants.sql
echo "permisos de risk_svc, sim_loader y BI aplicados"
