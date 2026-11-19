#!/usr/bin/env bash
# make kind-up (ADR-13, D12): comprueba el SHA-256 del binario kind contra el del release oficial ANTES de ejecutarlo y
# crea el clúster con deploy/kind/cluster.yaml (imagen del nodo fijada por digest). No instala kind.
. "$(dirname "$0")/lib.sh"
. "$(dirname "$0")/kind-lib.sh"
KIND="$(verified_kind)"
echo "kind ${KIND_VERSION} verificado (${KIND})"
if "$KIND" get clusters 2>/dev/null | grep -qx "$KIND_CLUSTER"; then
  echo "el clúster ${KIND_CLUSTER} ya existe"
else
  "$KIND" create cluster --name "$KIND_CLUSTER" --config "$ROOT/deploy/kind/cluster.yaml"
fi
