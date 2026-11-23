#!/usr/bin/env bash
# Pruebas de scripts/kind_chaos_verdict.py, el veredicto de make kind-chaos. Sin clúster ni red: resúmenes de k6 de
# mentira. El caso de 2.540 FALLBACK de 4.801 es el de una corrida real con el scoring caído (D16), que antes salía con 0.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
fails=0
check() { if eval "$2"; then echo "ok   $1"; else echo "FAIL $1"; fails=$((fails + 1)); fi; }

summary() {   # $1: 201, $2: other
  cat > "$TMP/s.json" <<JSON
{"counts": {"201": $1, "200_replayed": 60, "422_transfer_declined": 40, "422_insufficient_funds": 0,
            "other": $2, "422_other": 0, "200_not_replayed": 0},
 "load_latency_ms": {"p(95)": 24.0, "p(99)": 28.2}}
JSON
}
run() { python3 "$ROOT/scripts/kind_chaos_verdict.py" "$TMP/s.json" "$@" >"$TMP/out" 2>&1; }

summary 4804 0
check "corrida en verde (0 FALLBACK, Ready en 7 s): código 0" 'run 0 4801 7 0'
check "el resumen dice éxito y FALLBACK" 'grep -q "éxito (200, 201, 422) 100.00 %" "$TMP/out" && grep -q "FALLBACK en la ventana: 0 de 4801" "$TMP/out"'
check "2.540 FALLBACK de 4.801 con 100 % de éxito: código 1" '! run 2540 4801 8 0 && grep -q "FALLBACK 52.91 % > 1 %" "$TMP/out"'
check "1 % de FALLBACK justo en el límite: código 0" 'run 48 4800 7 0'
check "el reemplazo no quedó Ready: código 1" '! run 0 4801 "" 0 && grep -q "no quedó Ready" "$TMP/out"'
check "k6 terminó con error (umbrales): código 1" '! run 0 4801 7 99 && grep -q "k6 terminó con 99" "$TMP/out"'
summary 4700 104
check "éxito 97,9 % < 99 %: código 1" '! run 0 4801 7 0 && grep -q "< 99 %" "$TMP/out"'
check "el mínimo de éxito se puede ajustar" 'CHAOS_MIN_SUCCESS_PCT=97 run 0 4801 7 0'
summary 0 0
sed -i 's/"200_replayed": 60, "422_transfer_declined": 40/"200_replayed": 0, "422_transfer_declined": 0/' "$TMP/s.json"
check "sin respuestas: código 1" '! run 0 0 7 0 && grep -q "sin respuestas" "$TMP/out"'
check "kind-chaos.sh sale con el código del veredicto" 'tail -1 "$ROOT/scripts/kind-chaos.sh" | grep -q "^python3 .*kind_chaos_verdict.py"'
[[ $fails == 0 ]] && echo "OK" || { echo "$fails fallos"; exit 1; }
