#!/usr/bin/env bash
# make migrate-risk: aplica db/migrations/risk con Flyway y el rol verabank_risk, con la configuración
# de db/README.md (historial en flyway_risk). Es lo que hará risk-service al arrancar; mientras no exista
# (fase 4) lo ejecutamos con la imagen oficial de Flyway. Idempotente: Flyway no repite versiones aplicadas.
. "$(dirname "$0")/db-lib.sh"

require_env VERABANK_RISK_PASSWORD
network="${VERABANK_DB_NETWORK:-verabank_default}"
host="${VERABANK_DB_HOST:-postgres}"
# La contraseña viaja por entorno (-e sin valor), no por la línea de comandos.
FLYWAY_PASSWORD="$VERABANK_RISK_PASSWORD" docker run --rm --network "$network" -e FLYWAY_PASSWORD \
  -v "$ROOT/db/migrations/risk:/flyway/sql/risk:ro" "$FLYWAY_IMAGE" \
  -url="jdbc:postgresql://${host}:5432/${DB_NAME}" -user=verabank_risk \
  -schemas=flyway_risk -defaultSchema=flyway_risk -locations=filesystem:/flyway/sql/risk \
  -connectRetries=10 -cleanDisabled=true migrate
