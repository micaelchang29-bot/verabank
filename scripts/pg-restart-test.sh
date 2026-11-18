#!/usr/bin/env bash
# make pg-restart-test (4.G c): 5 solicitudes/s durante 90 s con el canal loadtest; a los 30 s, docker compose restart
# postgres. Informa errores mientras cae, recuperación del pool y tiempo hasta normalizar (perf/restart_probe.py), y
# los reinicios de los servicios. Necesita make up y make load-seed. Después: settle y reconcile.
. "$(dirname "$0")/lib.sh"

key=""
IFS=',' read -ra entries <<<"${VERABANK_API_KEYS:-}"
for e in "${entries[@]}"; do [[ "${e%%:*}" == loadtest ]] && key="${e#*:}"; done
[[ -n "$key" ]] || { echo "falta el canal loadtest en VERABANK_API_KEYS" >&2; exit 2; }
before=$(docker inspect -f '{{.Name}} {{.RestartCount}}' $("${COMPOSE_ALL[@]}" ps -q transfer-service risk-scoring risk-stream))
API_KEY="$key" python3 perf/restart_probe.py --base-url "http://localhost:${SERVER_PORT}" --rate "${RATE:-5}" \
  --duration "${PROBE_DURATION:-90}" --restart-at 30 --restart-cmd "docker compose --profile core restart postgres"
echo "reinicios de contenedores antes / después:"
echo "$before"
docker inspect -f '{{.Name}} {{.RestartCount}} {{.State.Health.Status}}' $("${COMPOSE_ALL[@]}" ps -q transfer-service risk-scoring risk-stream)
