#!/usr/bin/env bash
# Pruebas de verified_kind (scripts/kind-lib.sh): un binario con otro SHA-256 se rechaza sin ejecutarlo; el hash
# esperado es el del release oficial. Sin red ni clúster.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
fails=0
check() { if eval "$2"; then echo "ok   $1"; else echo "FAIL $1"; fails=$((fails + 1)); fi; }
# shellcheck disable=SC1091
. "$ROOT/scripts/kind-lib.sh"

cat > "$TMP/kind" <<'SH'
#!/bin/sh
touch "$(dirname "$0")/EJECUTADO"
SH
chmod +x "$TMP/kind"
out="$(KIND_BIN="$TMP/kind" verified_kind 2>&1)" && rc=0 || rc=$?
if [[ "$(uname -s)/$(uname -m)" == Linux/x86_64 ]]; then
  check "un binario con otro hash se rechaza (código 3)" '[[ $rc == 3 && "$out" == *"no es el v0.33.0 oficial"* ]]'
else
  check "fuera de Linux amd64 se rechaza (código 2)" '[[ $rc == 2 ]]'
fi
check "el binario rechazado nunca se ejecutó" '[[ ! -e "$TMP/EJECUTADO" ]]'
out="$(KIND_BIN="$TMP/no-existe" verified_kind 2>&1)" && rc=0 || rc=$?
check "sin binario: código 2 y mensaje" '[[ $rc == 2 && "$out" == *"falta kind"* ]]'
check "versión y hash del release oficial" '[[ $KIND_VERSION == v0.33.0 && $KIND_SHA256_LINUX_AMD64 == aee6151561422756b764a4ae28e7f44cda5af5a9eead3cc9985112b1de8d8e0d ]]'
check "kind.yml fija la misma versión y hash" 'grep -q "KIND_VERSION: $KIND_VERSION" "$ROOT/.github/workflows/kind.yml" && grep -q "KIND_SHA256: $KIND_SHA256_LINUX_AMD64" "$ROOT/.github/workflows/kind.yml"'
check "kind.yml verifica el hash antes del chmod" '[[ $(grep -n "sha256sum -c" "$ROOT/.github/workflows/kind.yml" | cut -d: -f1) -lt $(grep -n "chmod +x" "$ROOT/.github/workflows/kind.yml" | cut -d: -f1) ]]'
check "ningún script ejecuta kind sin verified_kind" '! grep -nE "^[^#]*(^|[;&| ])kind (create|delete|load|get)" "$ROOT"/scripts/kind-*.sh "$ROOT/Makefile"'
[[ $fails == 0 ]] && echo "OK" || { echo "$fails fallos"; exit 1; }
