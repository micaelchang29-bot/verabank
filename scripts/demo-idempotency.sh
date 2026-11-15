#!/usr/bin/env bash
# make demo-idempotency: dos POST con la misma Idempotency-Key y el mismo cuerpo.
# Esperado: 201 y luego 200 con Idempotent-Replayed: true (ADR-03). Falla si no.
. "$(dirname "$0")/lib.sh"

API_KEY="$(first_api_key)"
SOURCE="$(seed_uuid account:1)"
DEST="$(seed_uuid account:2)"
KEY="$(cat /proc/sys/kernel/random/uuid)"
BODY=$(printf '{"source_account_id":"%s","dest_account_id":"%s","dest_country":"PE","rail":"INTERNAL","transfer_type":"TRANSFER","amount":"1.00","currency":"PEN","occurred_at":"%s"}' \
  "$SOURCE" "$DEST" "$(date -u +%Y-%m-%dT%H:%M:%SZ)")
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
# La clave va en un archivo de cabeceras (curl -H @archivo), no en la línea de comandos.
(umask 077; printf 'X-API-Key: %s\n' "$API_KEY" >"$TMP/auth")

post() {   # $1: número de intento
  curl -sS -o "$TMP/body$1" -D "$TMP/headers$1" -w '%{http_code}' \
    -X POST "${BASE_URL}/api/v1/transfers" \
    -H 'Content-Type: application/json' \
    -H @"$TMP/auth" \
    -H "Idempotency-Key: ${KEY}" \
    --data "$BODY"
}
replayed() { grep -i '^Idempotent-Replayed:' "$TMP/headers$1" | tr -d '\r' | sed 's/^[^:]*: *//'; }
field() { grep -o "\"$2\":\"[^\"]*\"" "$TMP/body$1" | head -1 | cut -d'"' -f4; }

echo "Idempotency-Key: $KEY"
s1="$(post 1)"
echo "POST 1 -> HTTP $s1  transfer_id=$(field 1 transfer_id)  status=$(field 1 status)"
s2="$(post 2)"
echo "POST 2 -> HTTP $s2  transfer_id=$(field 2 transfer_id)  status=$(field 2 status)  Idempotent-Replayed: $(replayed 2)"

if [[ "$s1" == 201 && "$s2" == 200 && "$(replayed 2)" == true && "$(field 1 transfer_id)" == "$(field 2 transfer_id)" ]]; then
  echo "OK: 201 y luego 200 con Idempotent-Replayed: true, misma transferencia"
else
  echo "FALLO: se esperaba 201 y luego 200 con Idempotent-Replayed: true" >&2
  cat "$TMP/body1" "$TMP/body2" >&2; echo >&2
  exit 1
fi
