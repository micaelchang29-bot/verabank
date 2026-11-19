#!/usr/bin/env bash
# make kind-chaos (ADR-13, D12): k6 (perf/transfers.js) a 20 TPS contra localhost:18080; un minuto después del
# calentamiento borra un pod de risk-scoring con --grace-period=0 --force. Informa el éxito de k6 (200, 201 y 422),
# cuántas transferencias de la ventana se decidieron por FALLBACK y cuánto tardó el reemplazo en estar Ready.
# Objetivo: éxito >= 99 %. Necesita make kind-deploy y el canal loadtest en VERABANK_API_KEYS.
. "$(dirname "$0")/lib.sh"
. "$(dirname "$0")/kind-lib.sh"
need kubectl; need docker

K6_IMAGE="${K6_IMAGE:-grafana/k6:1.4.2}"
RATE="${CHAOS_RATE:-20}"
DURATION="${CHAOS_DURATION:-3m}"
ACCOUNTS="${CHAOS_ACCOUNTS:-1000}"
OUT="$ROOT/data/perf/kind"
mkdir -p "$OUT"

LOADTEST_KEY=""
IFS=',' read -ra entries <<<"${VERABANK_API_KEYS:-}"
for e in "${entries[@]}"; do [[ "${e%%:*}" == loadtest ]] && LOADTEST_KEY="${e#*:}"; done
[[ -n "$LOADTEST_KEY" ]] || { echo "falta el canal loadtest en VERABANK_API_KEYS (.env)" >&2; exit 2; }

sql() { kc exec -i statefulset/postgres -- sh -c 'PGPASSWORD="$VERABANK_TRANSFER_PASSWORD" psql -X -q -h 127.0.0.1 -U verabank_transfer -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 -tA'; }

echo "== cuentas de la carga (${ACCOUNTS} orígenes, 2.000 destinos)"
VERABANK_KIND=1 scripts/sim.sh load-seed --accounts "$ACCOUNTS" --destinations 2000

started="$(date -u +%FT%T.%3NZ)"
echo "== k6: ${RATE} TPS, 60 s de calentamiento y ${DURATION} medidos (inicio ${started})"
API_KEY="$LOADTEST_KEY" docker run --rm --network host -u "$(id -u):$(id -g)" -e API_KEY \
  -e BASE_URL=http://127.0.0.1:18080 -e RATE="$RATE" -e DURATION="$DURATION" -e ACCOUNTS="$ACCOUNTS" -e VUS=60 \
  -v "$ROOT/perf:/scripts:ro" -v "$OUT:/out" "$K6_IMAGE" run --quiet /scripts/transfers.js >"$OUT/chaos-k6.log" 2>&1 &
k6=$!

sleep 120
victim="$(kc get pods -l app=risk-scoring -o jsonpath='{.items[0].metadata.name}')"
before="$(kc get pods -l app=risk-scoring -o jsonpath='{.items[*].metadata.name}')"
killed_at=$SECONDS
echo "== borrando ${victim} (--grace-period=0 --force)"
kc delete pod "$victim" --grace-period=0 --force >/dev/null 2>&1
ready_s=""
while (( SECONDS - killed_at < 300 )); do
  ready="$(kc get pods -l app=risk-scoring -o jsonpath='{range .items[*]}{.metadata.name}={.status.containerStatuses[0].ready}{"\n"}{end}')"
  replacement="$(grep -v -F -e "$victim" <<<"$ready" | grep '=true' | cut -d= -f1 | grep -vxF -f <(tr ' ' '\n' <<<"$before") || true)"
  if [[ -n "$replacement" ]]; then ready_s=$((SECONDS - killed_at)); break; fi
  sleep 1
done
echo "reemplazo de ${victim} Ready en: ${ready_s:-no quedó Ready en 300} s"
set +e
wait "$k6"; k6_status=$?
ended="$(date -u +%FT%T.%3NZ)"

# Resumen propio de perf/transfers.js (handleSummary): counts por resultado y latencia de la fase medida.
python3 - "$OUT/transfers-summary.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
c = d["counts"]
ok = c["201"] + c["200_replayed"] + c["422_transfer_declined"] + c["422_insufficient_funds"]
bad = c["other"] + c["422_other"] + c["200_not_replayed"]
total = ok + bad
lat = d.get("load_latency_ms", {})
print(f"k6: {total} respuestas; éxito (200, 201, 422) {100 * ok / total:.2f} %; fuera de esos códigos {bad}; "
      f"p95 {lat.get('p(95)', 0):.1f} ms, p99 {lat.get('p(99)', 0):.1f} ms" if total else "k6: sin respuestas")
PY
fallback="$(sql <<SQL
SELECT count(*) FILTER (WHERE decision_source = 'FALLBACK') || ' de ' || count(*)
FROM core.transfers WHERE client_id = 'loadtest' AND created_at >= '${started}' AND created_at < '${ended}';
SQL
)"
echo "FALLBACK en la ventana: ${fallback}"
echo "k6 terminó con ${k6_status}; salida en ${OUT}"
