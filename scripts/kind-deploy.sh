#!/usr/bin/env bash
# make kind-deploy (ADR-13, D12): construye las imágenes, las carga en kind (sin registro), crea el Secret desde .env,
# aplica los manifiestos y espera a que los Jobs terminen y todo esté Ready. Aborta si Compose está corriendo: los dos
# a la vez no caben en la memoria de la laptop (§1.2, §8).
. "$(dirname "$0")/lib.sh"
. "$(dirname "$0")/kind-lib.sh"
KIND="$(verified_kind)"; need kubectl; need docker

if [[ -n "$("${COMPOSE_ALL[@]}" --profile bi ps -q 2>/dev/null)" ]]; then
  echo "Compose está corriendo: deténlo antes (make down, conserva los datos) y repite make kind-deploy" >&2
  exit 1
fi
"$KIND" get clusters 2>/dev/null | grep -qx "$KIND_CLUSTER" || { echo "no existe el clúster $KIND_CLUSTER: make kind-up" >&2; exit 1; }

echo "== imágenes"
"${COMPOSE_ALL[@]}" build risk-scoring transfer-service rail-sim
for image in "${KIND_IMAGES[@]}"; do
  "$KIND" load docker-image --name "$KIND_CLUSTER" "$image"
done

echo "== Secret verabank-env desde .env (los valores no se imprimen)"
for key in "${KIND_SECRET_KEYS[@]}"; do
  [[ -n "${!key:-}" ]] || { echo "falta $key en .env" >&2; exit 2; }
done
# Los valores van en un archivo de entorno por un descriptor (sustitución de proceso), nunca en la línea de comandos;
# el YAML resultante pasa por la entrada estándar de kubectl apply.
kc create secret generic verabank-env --dry-run=client -o yaml \
  --from-env-file=<(for key in "${KIND_SECRET_KEYS[@]}"; do printf '%s=%s\n' "$key" "${!key}"; done) \
  | kc apply -f - >/dev/null

echo "== manifiestos (los Jobs se recrean: son idempotentes)"
kc delete job db-migrate bootstrap --ignore-not-found >/dev/null
manifests | kc apply -f -

echo "== esperando PostgreSQL, Kafka y los Jobs"
kc rollout status statefulset/postgres --timeout=5m
kc rollout status statefulset/kafka --timeout=5m
kc wait --for=condition=complete job/db-migrate --timeout=10m
kc wait --for=condition=complete job/bootstrap --timeout=10m
echo "== esperando los servicios"
for d in transfer-service risk-scoring risk-stream rail-sim metabase; do
  kc rollout status "deployment/$d" --timeout=10m
done
kc get pods -o wide
echo "transfer-service: http://localhost:18080   Metabase: http://localhost:13000"
