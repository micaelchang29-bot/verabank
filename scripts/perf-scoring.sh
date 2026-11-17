#!/usr/bin/env bash
# make perf-scoring: k6 (imagen fijada) contra risk-scoring dentro de la red de Compose, sin pasar por transfer-service.
# Variables: DURATION (5m), PEAK (1 añade 100 TPS durante 5 min). Salidas en data/perf/ (ignorado por Git):
# scoring-summary.json (k6), docker-stats.txt (una muestra cada 30 s de docker stats --no-stream) y k6.log.
. "$(dirname "$0")/lib.sh"

K6_IMAGE="${K6_IMAGE:-grafana/k6:1.4.2}"
DURATION="${DURATION:-5m}"
PEAK="${PEAK:-0}"
OUT="$ROOT/data/perf"
mkdir -p "$OUT"
: >"$OUT/docker-stats.txt"

if ! docker compose --profile risk ps --status running --services 2>/dev/null | grep -qx risk-scoring; then
  echo "risk-scoring no está corriendo: make up" >&2
  exit 1
fi

sample_stats() {
  while sleep 30; do
    {
      echo "# $(date -u +%FT%TZ)"
      docker stats --no-stream --format '{{.Name}}\t{{.MemUsage}}\t{{.MemPerc}}\t{{.CPUPerc}}' \
        $(docker compose --profile core --profile risk --profile sim ps -q)
    } >>"$OUT/docker-stats.txt"
  done
}
sample_stats &
sampler=$!
trap 'kill "$sampler" 2>/dev/null || true' EXIT

echo "k6: DURATION=$DURATION PEAK=$PEAK (60 s de calentamiento sin umbrales); resultado en data/perf/"
set +e
docker run --rm --network verabank_default -u "$(id -u):$(id -g)" \
  -v "$ROOT/perf:/scripts:ro" -v "$OUT:/out" \
  -e BASE_URL=http://risk-scoring:8081 -e DURATION="$DURATION" -e PEAK="$PEAK" \
  "$K6_IMAGE" run --summary-export /out/scoring-summary.json /scripts/scoring.js 2>&1 | tee "$OUT/k6.log"
status=${PIPESTATUS[0]}
set -e
exit "$status"
