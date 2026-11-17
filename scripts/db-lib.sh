# Funciones comunes de los scripts de base de datos (roles, migraciones de risk, permisos, semillas).
# Se carga con: . scripts/db-lib.sh
# A diferencia de lib.sh no exige .env: lee .env si existe, pero lo que ya esté en el entorno tiene
# prioridad (la CI y las pruebas lo definen ahí). Nunca imprime secretos.
#
# Entorno: POSTGRES_DB, POSTGRES_USER, RISK_SVC_PASSWORD, SIM_LOADER_PASSWORD, VERABANK_RISK_PASSWORD.
# Por defecto actúan sobre el servicio `postgres` de Compose; con VERABANK_DB_CONTAINER=<nombre> lo
# hacen sobre ese contenedor (pruebas de db/tests), y con VERABANK_DB_NETWORK / VERABANK_DB_HOST
# indican la red y el nombre de host que ve Flyway.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [[ -f .env ]]; then
  while IFS='=' read -r _key _value; do
    [[ "$_key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
    [[ -n "${!_key+x}" ]] || export "$_key=$_value"
  done < .env
fi

DB_NAME="${POSTGRES_DB:-verabank}"
FLYWAY_IMAGE="${FLYWAY_IMAGE:-flyway/flyway:11.7.2}"   # misma versión que transfer-service (Spring Boot 3.5.16)

require_env() {
  local name
  for name in "$@"; do
    [[ -n "${!name:-}" ]] || { echo "falta $name en el entorno (.env)" >&2; return 2; }
  done
}

# db_exec [NOMBRE_DE_VARIABLE...] -- comando...: ejecuta dentro del contenedor de PostgreSQL. Las variables
# nombradas viajan por entorno (sin valor en la línea de comandos).
db_exec() {
  local envs=()
  while [[ "$1" != "--" ]]; do envs+=(-e "$1"); shift; done
  shift
  if [[ -n "${VERABANK_DB_CONTAINER:-}" ]]; then
    docker exec -i "${envs[@]}" "$VERABANK_DB_CONTAINER" "$@"
  else
    docker compose --profile core exec -T "${envs[@]}" postgres "$@"
  fi
}

# psql_admin [args]: superusuario de arranque, por el socket local del contenedor. SQL por la entrada estándar.
psql_admin() {
  db_exec -- sh -c 'exec psql -X -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 "$@"' sh "$@"
}

# psql_as ROL VARIABLE_CON_LA_CONTRASEÑA [args]: psql con ese rol, por TCP dentro del contenedor.
psql_as() {
  local role="$1" pwvar="$2"
  shift 2
  require_env "$pwvar"
  PGPW="${!pwvar}" db_exec PGPW -- sh -c \
    'u=$1; d=$2; shift 2; PGPASSWORD="$PGPW" exec psql -X -h 127.0.0.1 -U "$u" -d "$d" -v ON_ERROR_STOP=1 "$@"' \
    sh "$role" "$DB_NAME" "$@"
}
