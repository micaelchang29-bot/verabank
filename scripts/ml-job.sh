#!/usr/bin/env bash
# Ejecuta un subcomando de ml/ que habla con la base (psi, reasons, retrain…) con el entorno de .env. El job se
# conecta por 127.0.0.1:POSTGRES_PORT con el rol ml_job (ML_JOB_PASSWORD); no recibe otras credenciales de la base.
# Uso: scripts/ml-job.sh <subcomando> [opciones]
. "$(dirname "$0")/lib.sh"

export MLFLOW_DISABLE_AGENT_HINT=1
exec env -u POSTGRES_PASSWORD -u VERABANK_TRANSFER_PASSWORD -u VERABANK_RISK_PASSWORD -u VERABANK_BI_PASSWORD \
  -u RISK_SVC_PASSWORD -u SIM_LOADER_PASSWORD -u METABASE_DB_PASSWORD -u METABASE_ADMIN_PASSWORD -u VERABANK_API_KEYS \
  uv run --project "$ROOT/ml" --frozen python -m verabank_ml "$@"
