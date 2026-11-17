#!/usr/bin/env bash
# make seed-risk: inserta la cuenta CLEARING en risk.aml_exempt_accounts. Idempotente.
# risk no puede leer core: el id sale de la semilla de la fase 1 (db/seed/01_core_seed.sql), que lo fija
# como md5('verabank-seed:clearing')::uuid; se lee de ahí y se calcula aquí.
. "$(dirname "$0")/db-lib.sh"

key="$(grep -oE "md5\('verabank-seed:clearing[^']*'\)::uuid" db/seed/01_core_seed.sql | head -n 1 \
  | sed -E "s/^md5\('([^']*)'\).*/\1/")"
[[ -n "$key" ]] || { echo "no encuentro el id de CLEARING en db/seed/01_core_seed.sql" >&2; exit 1; }
hash="$(printf '%s' "$key" | md5sum | cut -c1-32)"
clearing_id="${hash:0:8}-${hash:8:4}-${hash:12:4}-${hash:16:4}-${hash:20:12}"

psql_as risk_svc RISK_SVC_PASSWORD -q -v clearing_id="$clearing_id" <<'SQL'
INSERT INTO risk.aml_exempt_accounts (account_id, reason)
VALUES (:'clearing_id', 'Cuenta técnica de compensación (CLEARING): no genera alertas AML')
ON CONFLICT (account_id) DO NOTHING;
SQL
echo "CLEARING ($clearing_id) exenta de reglas AML"
