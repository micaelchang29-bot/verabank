#!/usr/bin/env bash
# make load-transfers DURATION=5m|30m PEAK=0|1 (T-17, D10). Necesita make up y el canal loadtest en VERABANK_API_KEYS.
#   1. load-seed (ACCOUNTS orígenes con saldo alto y 2.000 destinos internos, por asientos desde CLEARING)
#   2. rail simulado con REJECT (0.1): 10 % de rechazos
#   3. k6 (perf/transfers.js) en paralelo con perf/sample_metrics.py y docker stats cada 30 s
#   4. settle --approve-reviews, reconcile-full y verify-audit
# Salidas en data/perf/: transfers-summary.json, transfers-k6.log, metrics.csv, load-docker-stats.txt. Corren todas las
# etapas aunque una falle; sale con 1 si alguna falló. Al terminar el rail vuelve a RAIL_REJECT_RATE=0.
. "$(dirname "$0")/lib.sh"

K6_IMAGE="${K6_IMAGE:-grafana/k6:1.4.2}"
DURATION="${DURATION:-5m}"
PEAK="${PEAK:-0}"
ACCOUNTS="${ACCOUNTS:-5000}"
REJECT="${LOAD_REJECT:-0.1}"
OUT="$ROOT/data/perf"
mkdir -p "$OUT"

LOADTEST_KEY=""
IFS=',' read -ra entries <<<"${VERABANK_API_KEYS:-}"
for e in "${entries[@]}"; do [[ "${e%%:*}" == loadtest ]] && LOADTEST_KEY="${e#*:}"; done
if [[ -z "$LOADTEST_KEY" ]]; then
  echo "falta el canal loadtest en VERABANK_API_KEYS (.env): añade ,loadtest:<clave de 16+ caracteres> y repite make up" >&2
  exit 2
fi
for s in transfer-service risk-scoring risk-stream; do
  "${COMPOSE_ALL[@]}" ps --status running --services 2>/dev/null | grep -qx "$s" || { echo "$s no está corriendo: make up" >&2; exit 1; }
done

status=0
step() { echo "== $*"; }
restore_rail() { RAIL_REJECT_RATE=0 "${COMPOSE_ALL[@]}" up -d --no-deps rail-sim >/dev/null 2>&1 || true; }
trap 'kill "${sampler:-}" "${stats:-}" 2>/dev/null || true; restore_rail' EXIT

step "load-seed: ${ACCOUNTS} orígenes y 2.000 destinos"
scripts/sim.sh load-seed --accounts "$ACCOUNTS" --destinations 2000 || status=1
step "rail simulado con REJECT=${REJECT}"
RAIL_REJECT_RATE="$REJECT" "${COMPOSE_ALL[@]}" up -d --no-deps --wait rail-sim

step "k6: RATE=${RATE:-50} DURATION=${DURATION} PEAK=${PEAK} (60 s de calentamiento sin umbrales)"
python3 perf/sample_metrics.py --base-url "http://localhost:${SERVER_PORT}" --out "$OUT/metrics.csv" &
sampler=$!
: >"$OUT/load-docker-stats.txt"
( while sleep 30; do
    echo "# $(date -u +%FT%TZ)"
    docker stats --no-stream --format '{{.Name}}\t{{.MemUsage}}\t{{.MemPerc}}\t{{.CPUPerc}}' $("${COMPOSE_ALL[@]}" ps -q)
  done >>"$OUT/load-docker-stats.txt" ) &
stats=$!
set +e
API_KEY="$LOADTEST_KEY" docker run --rm --network verabank_default -u "$(id -u):$(id -g)" -e API_KEY \
  -v "$ROOT/perf:/scripts:ro" -v "$OUT:/out" -e BASE_URL=http://transfer-service:8081 -e DURATION="$DURATION" \
  -e PEAK="$PEAK" -e RATE="${RATE:-50}" -e ACCOUNTS="$ACCOUNTS" "$K6_IMAGE" run --quiet /scripts/transfers.js 2>&1 | tee "$OUT/transfers-k6.log"
k6_status=${PIPESTATUS[0]}
set -e
kill -TERM "$sampler" 2>/dev/null; wait "$sampler" 2>/dev/null || true
kill "$stats" 2>/dev/null || true
[[ "$k6_status" == 0 ]] || { echo "k6 terminó con $k6_status (umbrales no cumplidos o error)" >&2; status=1; }

step "settle --approve-reviews"
scripts/sim.sh settle --approve-reviews --timeout "${SETTLE_TIMEOUT:-15m}" || status=1
step "reconcile-full"
scripts/reconcile-full.sh || status=1
step "verify-audit"
scripts/verify-audit.sh || status=1
echo "memoria máxima por muestra (docker stats, suma de contenedores):"
python3 - "$OUT/load-docker-stats.txt" <<'PY' || true
import re, sys
units = {"B": 1, "KiB": 1024, "MiB": 1024**2, "GiB": 1024**3}
peak, current = 0.0, 0.0
for line in open(sys.argv[1]):
    if line.startswith("#"):
        peak, current = max(peak, current), 0.0
        continue
    m = re.match(r"\S+\t([\d.]+)(\w+) /", line)
    if m:
        current += float(m.group(1)) * units[m.group(2)]
print(f"  {max(peak, current) / 1024**3:.2f} GiB (tope §1.2: 8 GB)")
PY
exit "$status"
