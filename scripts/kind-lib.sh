# Funciones comunes de los objetivos kind-* (ADR-13, D12). Se carga después de scripts/lib.sh.
KIND_CLUSTER="${KIND_CLUSTER:-verabank}"
# Versión y SHA-256 del binario Linux amd64 tal como los publica el release oficial (gh api
# repos/kubernetes-sigs/kind/releases/tags/v0.33.0: «digest» del asset kind-linux-amd64 y kind-linux-amd64.sha256sum).
KIND_VERSION="v0.33.0"
KIND_SHA256_LINUX_AMD64="aee6151561422756b764a4ae28e7f44cda5af5a9eead3cc9985112b1de8d8e0d"
KIND_CONTEXT="kind-${KIND_CLUSTER}"
KIND_IMAGES=(verabank/transfer-service:dev verabank/risk-service:dev verabank/rail-sim:dev)
# Claves del Secret verabank-env: las mismas variables que Compose pasa a los contenedores.
KIND_SECRET_KEYS=(POSTGRES_DB POSTGRES_USER POSTGRES_PASSWORD VERABANK_TRANSFER_PASSWORD VERABANK_RISK_PASSWORD
  VERABANK_BI_PASSWORD RISK_SVC_PASSWORD SIM_LOADER_PASSWORD ML_JOB_PASSWORD METABASE_DB_PASSWORD KAFKA_CLUSTER_ID
  VERABANK_API_KEYS)

# verified_kind: ruta del binario kind tras comprobar su SHA-256, ANTES de ejecutarlo. Aborta si no coincide o si la
# plataforma no es Linux amd64 (no hay hash fijado para otras). KIND_BIN permite indicar la ruta.
verified_kind() {
  local bin="${KIND_BIN:-$(command -v kind || true)}" got
  [[ -n "$bin" && -f "$bin" ]] || { echo "falta kind ${KIND_VERSION} (deploy/kind/README.md)" >&2; return 2; }
  [[ "$(uname -s)/$(uname -m)" == Linux/x86_64 ]] || { echo "solo hay SHA-256 fijado para Linux amd64" >&2; return 2; }
  got="$(sha256sum "$bin" | cut -d' ' -f1)"
  if [[ "$got" != "$KIND_SHA256_LINUX_AMD64" ]]; then
    echo "kind en $bin no es el ${KIND_VERSION} oficial: SHA-256 $got, se esperaba $KIND_SHA256_LINUX_AMD64" >&2
    return 3
  fi
  printf '%s\n' "$bin"
}

need() { command -v "$1" >/dev/null 2>&1 || { echo "falta $1 en el PATH (deploy/kind/README.md)" >&2; exit 2; }; }
kc() { kubectl --context "$KIND_CONTEXT" "$@"; }
manifests() { kubectl kustomize --load-restrictor LoadRestrictionsNone "$ROOT/deploy/kind/overlays/kind"; }
