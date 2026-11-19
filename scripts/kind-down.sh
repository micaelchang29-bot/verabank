#!/usr/bin/env bash
# make kind-down CONFIRM=yes: borra el clúster kind y sus volúmenes (datos de PostgreSQL y Kafka del clúster).
. "$(dirname "$0")/lib.sh"
. "$(dirname "$0")/kind-lib.sh"
need kind
if [[ "${CONFIRM:-}" != yes ]]; then
  echo "kind-down borra el clúster ${KIND_CLUSTER} y sus datos. Repite con: make kind-down CONFIRM=yes" >&2
  exit 1
fi
kind delete cluster --name "$KIND_CLUSTER"
