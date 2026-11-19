# Funciones comunes de los objetivos kind-* (ADR-13, D12). Se carga después de scripts/lib.sh.
KIND_CLUSTER="${KIND_CLUSTER:-verabank}"
KIND_CONTEXT="kind-${KIND_CLUSTER}"
KIND_IMAGES=(verabank/transfer-service:dev verabank/risk-service:dev verabank/rail-sim:dev)
# Claves del Secret verabank-env: las mismas variables que Compose pasa a los contenedores.
KIND_SECRET_KEYS=(POSTGRES_DB POSTGRES_USER POSTGRES_PASSWORD VERABANK_TRANSFER_PASSWORD VERABANK_RISK_PASSWORD
  VERABANK_BI_PASSWORD RISK_SVC_PASSWORD SIM_LOADER_PASSWORD ML_JOB_PASSWORD METABASE_DB_PASSWORD KAFKA_CLUSTER_ID
  VERABANK_API_KEYS)

need() { command -v "$1" >/dev/null 2>&1 || { echo "falta $1 en el PATH (manual 0.3)" >&2; exit 2; }; }
kc() { kubectl --context "$KIND_CONTEXT" "$@"; }
manifests() { kubectl kustomize --load-restrictor LoadRestrictionsNone "$ROOT/deploy/kind/overlays/kind"; }
