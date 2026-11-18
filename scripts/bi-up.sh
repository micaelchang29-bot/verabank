#!/usr/bin/env bash
# make bi-up: Metabase (perfil bi), su base de aplicación (make bi-init) y bi/setup_metabase.py, que crea por API la
# conexión de solo lectura (verabank_bi), la colección VeraBank, las 7 tarjetas y el dashboard. Idempotente.
# make bi-url (este script con --url): solo imprime las URL.
. "$(dirname "$0")/lib.sh"

url="http://localhost:${METABASE_PORT:-3000}"
if [[ "${1:-}" == "--url" ]]; then
  echo "Metabase: ${url}"
  curl -fsS "${url}/api/health" >/dev/null 2>&1 || echo "(Metabase no está en marcha: make bi-up)"
  if [[ -f data/bi/dashboard_url.txt ]]; then echo "Dashboard: $(cat data/bi/dashboard_url.txt)"; else echo "Dashboard: aún no creado (make bi-up)"; fi
  exit 0
fi

if [[ "${1:-}" == "--check" ]]; then
  METABASE_URL="$url" exec uv run --project bi --frozen python bi/check_cards.py
fi

for v in METABASE_DB_PASSWORD METABASE_ADMIN_EMAIL METABASE_ADMIN_PASSWORD VERABANK_BI_PASSWORD; do
  [[ -n "${!v:-}" ]] || { echo "falta $v en .env (ver .env.example)" >&2; exit 2; }
done
"${COMPOSE_ALL[@]}" --profile bi up -d --wait postgres
db/scripts/create-metabase-db.sh
echo "== Metabase (perfil bi); el primer arranque tarda un par de minutos"
"${COMPOSE_ALL[@]}" --profile bi up -d --wait metabase
echo "== bi/setup_metabase.py"
METABASE_URL="$url" uv run --project bi --frozen python bi/setup_metabase.py
