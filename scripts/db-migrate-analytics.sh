#!/usr/bin/env bash
# make migrate-analytics: aplica db/migrations/analytics con Flyway y el rol de arranque (POSTGRES_USER), con
# historial en flyway_analytics (db/README.md). La vista de analytics cruza core y risk, así que va después de las
# migraciones de ambos; los permisos de ml_job (make db-grants) van después de esta. Idempotente.
. "$(dirname "$0")/db-lib.sh"

require_env POSTGRES_USER POSTGRES_PASSWORD
network="${VERABANK_DB_NETWORK:-verabank_default}"
host="${VERABANK_DB_HOST:-postgres}"
FLYWAY_PASSWORD="$POSTGRES_PASSWORD" docker run --rm --network "$network" -e FLYWAY_PASSWORD \
  -v "$ROOT/db/migrations/analytics:/flyway/sql/analytics:ro" "$FLYWAY_IMAGE" \
  -url="jdbc:postgresql://${host}:5432/${DB_NAME}" -user="$POSTGRES_USER" \
  -schemas=flyway_analytics -defaultSchema=flyway_analytics -locations=filesystem:/flyway/sql/analytics \
  -connectRetries=10 -cleanDisabled=true migrate
