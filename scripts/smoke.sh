#!/usr/bin/env bash
# make smoke [KIND=1] — humo de extremo a extremo (CI compose-smoke y kind). Uso: scripts/smoke.sh [url-transfer-service]
#   1. espera a que transfer-service, risk-scoring y risk-stream estén listos (readiness)
#   2. transferencia INTERNAL y EXTERNAL que terminan en COMPLETED
#   3. repetición con la misma clave: 200 e Idempotent-Replayed: true
#   4. decision_source = MODEL con la model_version del champion de ml/artifacts/manifest.json
#   5. una transferencia de 10.000 genera la alerta THRESHOLD, visible en GET /api/v1/aml/alerts
#   6. make reconcile en 0 y 0
# Con KIND=1 apunta al clúster: transfer-service en localhost:18080, la base y los servicios internos por kubectl.
. "$(dirname "$0")/lib.sh"

KIND="${KIND:-0}"
if [[ "$KIND" == 1 ]]; then
  URL="${1:-http://localhost:18080}"
else
  URL="${1:-$BASE_URL}"
fi
SCORING_URL="${SCORING_URL:-http://localhost:${RISK_SCORING_PORT:-8083}}"
STREAM_URL="${STREAM_URL:-http://localhost:${RISK_STREAM_PORT:-8082}}"
TIMEOUT="${SMOKE_TIMEOUT:-180}"
API_KEY="$(first_api_key)"
TMP="$(mktemp -d)"
pf_pids=()
trap 'rm -rf "$TMP"; for p in "${pf_pids[@]}"; do kill "$p" 2>/dev/null || true; done' EXIT
(umask 077; printf 'X-API-Key: %s\n' "$API_KEY" >"$TMP/auth")   # la clave nunca en la línea de comandos

fail() { echo "SMOKE FALLÓ: $*" >&2; exit 1; }
step() { echo "== $*"; }

if [[ "$KIND" == 1 ]]; then
  kubectl port-forward svc/risk-scoring 18083:8081 >/dev/null 2>&1 & pf_pids+=($!)
  kubectl port-forward svc/risk-stream 18082:8082 >/dev/null 2>&1 & pf_pids+=($!)
  SCORING_URL=http://localhost:18083; STREAM_URL=http://localhost:18082
  sql() { kubectl exec -i statefulset/postgres -- sh -c 'PGPASSWORD="$VERABANK_TRANSFER_PASSWORD" psql -X -q -h 127.0.0.1 -U verabank_transfer -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 -tA'; }
else
  sql() { psql_transfer -tA; }
fi

wait_ready() {   # $1 nombre, $2 url
  local deadline=$((SECONDS + TIMEOUT))
  until curl -fsS "$2" >/dev/null 2>&1; do
    (( SECONDS < deadline )) || fail "$1 no quedó listo en ${TIMEOUT} s ($2)"
    sleep 2
  done
  echo "$1 listo"
}

post() {   # $1 archivo de salida, $2 clave, $3 cuerpo → código HTTP
  curl -sS -o "$TMP/$1.body" -D "$TMP/$1.headers" -w '%{http_code}' -X POST "$URL/api/v1/transfers" \
    -H 'Content-Type: application/json' -H @"$TMP/auth" -H "Idempotency-Key: $2" --data "$3"
}
field() { python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get(sys.argv[2], ""))' "$TMP/$1.body" "$2"; }
status_of() { curl -fsS -H @"$TMP/auth" "$URL/api/v1/transfers/$1" | python3 -c 'import json,sys; print(json.load(sys.stdin)["status"])'; }
body() {   # $1 rail, $2 monto, $3 origen, $4 destino (cuenta o referencia), $5 tipo
  local dest
  if [[ "$1" == INTERNAL ]]; then dest="\"dest_account_id\":\"$4\""; else dest="\"dest_external_ref\":\"$4\""; fi
  printf '{"source_account_id":"%s",%s,"dest_country":"PE","rail":"%s","transfer_type":"%s","amount":"%s","currency":"PEN","occurred_at":"%s"}' \
    "$3" "$dest" "$1" "$5" "$2" "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
}
until_final() {   # $1 transfer_id, $2 estado esperado; aprueba una IN_REVIEW
  local deadline=$((SECONDS + TIMEOUT)) s
  while :; do
    s="$(status_of "$1")"
    if [[ "$s" == IN_REVIEW ]]; then
      curl -fsS -o /dev/null -X POST -H @"$TMP/auth" -H 'Content-Type: application/json' \
        --data '{"decision":"APPROVE","comment":"smoke"}' "$URL/api/v1/transfers/$1/review" || true
    fi
    [[ "$s" == "$2" ]] && return 0
    [[ "$s" == REJECTED || "$s" == FAILED ]] && fail "transferencia $1 terminó en $s (se esperaba $2)"
    (( SECONDS < deadline )) || fail "transferencia $1 sigue en $s tras ${TIMEOUT} s"
    sleep 1
  done
}
uuid() { cat /proc/sys/kernel/random/uuid; }

step "readiness"
wait_ready transfer-service "$URL/actuator/health/readiness"
wait_ready risk-scoring "$SCORING_URL/q/health/ready"
wait_ready risk-stream "$STREAM_URL/q/health/ready"

SRC="$(seed_uuid account:26)"      # PREMIUM de la semilla (saldo ≥ 20.000)
DST="$(seed_uuid account:25)"

step "transferencia INTERNAL"
KEY="$(uuid)"; B="$(body INTERNAL 25.00 "$SRC" "$DST" PAYMENT)"
code="$(post internal "$KEY" "$B")"; [[ "$code" == 201 ]] || fail "INTERNAL: HTTP $code $(cat "$TMP/internal.body")"
TID="$(field internal transfer_id)"; until_final "$TID" COMPLETED; echo "INTERNAL $TID COMPLETED"

step "repetición con la misma clave"
code="$(post replay "$KEY" "$B")"
replayed="$(grep -i '^Idempotent-Replayed:' "$TMP/replay.headers" | tr -d '\r' | sed 's/^[^:]*: *//')"
[[ "$code" == 200 && "$replayed" == true && "$(field replay transfer_id)" == "$TID" ]] \
  || fail "repetición: HTTP $code, Idempotent-Replayed=$replayed"
echo "200 con Idempotent-Replayed: true"

step "decision_source = MODEL con la versión del champion"
expected="$(python3 -c 'import json; m=json.load(open("ml/artifacts/manifest.json"))["models"]["champion"]; print(m["registered_model"] + "/" + m["version"])')"
got="$(echo "SELECT decision_source || ' ' || model_version FROM core.transfers WHERE transfer_id = '$TID';" | sql)"
[[ "$got" == "MODEL $expected" ]] || fail "se esperaba MODEL $expected y la transferencia tiene «$got»"
echo "MODEL $expected"

step "transferencia EXTERNAL"
code="$(post external "$(uuid)" "$(body EXTERNAL 30.00 "$SRC" "SMOKE-EXT-$(uuid | cut -c1-8)" PAYMENT)")"
[[ "$code" == 201 ]] || fail "EXTERNAL: HTTP $code $(cat "$TMP/external.body")"
ETID="$(field external transfer_id)"; until_final "$ETID" COMPLETED; echo "EXTERNAL $ETID COMPLETED"

step "alerta THRESHOLD (10.000)"
code="$(post big "$(uuid)" "$(body INTERNAL 10000.00 "$SRC" "$DST" PAYMENT)")"
[[ "$code" == 201 ]] || fail "10.000: HTTP $code $(cat "$TMP/big.body")"
BTID="$(field big transfer_id)"; until_final "$BTID" COMPLETED
deadline=$((SECONDS + TIMEOUT))
until python3 scripts/find_alert.py "$STREAM_URL" "$TMP/auth" THRESHOLD "$BTID"; do
  (( SECONDS < deadline )) || fail "no apareció la alerta THRESHOLD de $BTID en GET /api/v1/aml/alerts"
  sleep 2
done
echo "alerta THRESHOLD de $BTID visible"

step "conciliación"
if [[ "$KIND" == 1 ]]; then
  mapfile -t counts < <(sql < db/reconcile.sql)
  [[ "${counts[0]-}" == 0 && "${counts[1]-}" == 0 ]] || fail "conciliación: ${counts[*]-}"
  echo "0 y 0"
else
  scripts/reconcile.sh
fi
echo "OK: smoke en verde"
