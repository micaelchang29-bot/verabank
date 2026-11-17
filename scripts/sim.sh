#!/usr/bin/env bash
# Ejecuta un subcomando del simulador con el entorno de .env (claves de API, contraseñas de los roles y Kafka).
# Uso: scripts/sim.sh <subcomando> [opciones]   (lo usan make replay, make aml-run y make aml-eval)
. "$(dirname "$0")/lib.sh"

cd "$ROOT/simulator"
exec uv run --frozen python -m verabank_sim "$@"
