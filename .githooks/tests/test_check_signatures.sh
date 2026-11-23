#!/usr/bin/env bash
# Pruebas de .githooks/check-signatures.sh con repositorios temporales. Las marcas se arman por partes en tiempo de
# ejecución y NUNCA se crea un commit con firma: la detección en mensajes se prueba por --stdin y --env, que usan el
# mismo comparador que --range; --range se prueba con commits limpios y con un pie que no es firma.
set -uo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
SCRIPT="$HERE/check-signatures.sh"
pass=0; fail=0
ok() { if "$@" >/dev/null 2>&1; then pass=$((pass + 1)); else fail=$((fail + 1)); echo "FALLA (esperaba 0): $*"; fi; }
ko() { if "$@" >/dev/null 2>&1; then fail=$((fail + 1)); echo "FALLA (esperaba ≠ 0): $*"; else pass=$((pass + 1)); fi; }

CO="Co-""Authored-""By: Alguien <a@example.com>"
GEN="Generated ""with una herramienta"
SES="Claude""-session: abc"

tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
repo="$tmp/repo"; git init -q "$repo"; cd "$repo"
git config user.email t@example.com; git config user.name t; git config commit.gpgsign false
git config core.hooksPath /dev/null
echo a > a.txt; git add a.txt; git commit -qm "feat: primero"
base=$(git rev-parse HEAD)
echo b > b.txt; git add b.txt; git commit -qm "fix: segundo

Refs: #12
Reviewed-by: una persona"
ok "$SCRIPT" --range "$base..HEAD"
ok "$SCRIPT" --range HEAD
ok "$SCRIPT" --tree "$repo"

printf 'texto\n%s\n' "$CO" > "$repo/notas.md"
ok "$SCRIPT" --tree "$repo"                          # sin versionar: no cuenta
git add notas.md                                     # en el índice, sin commit
ko "$SCRIPT" --tree "$repo"
git rm -q --cached notas.md; rm "$repo/notas.md"
mkdir -p "$tmp/plano"; printf '%s\n' "$GEN" > "$tmp/plano/x.txt"
ko "$SCRIPT" --tree "$tmp/plano"                     # fuera de un repositorio: grep -r
mkdir -p "$repo/.githooks"; printf '%s\n' "$GEN" > "$repo/.githooks/regla"   # .githooks queda excluido
ok "$SCRIPT" --tree "$repo"

for mark in "$CO" "$GEN" "$SES"; do
  ko "$SCRIPT" --stdin <<<"feat: algo

$mark"
  PR_BODY="Descripción del PR

$mark" ko "$SCRIPT" --env PR_BODY
done
PR_BODY="Descripción sin marcas; generada a mano" ok "$SCRIPT" --env PR_BODY
ok "$SCRIPT" --stdin <<<"docs: co-autoría y sesiones explicadas sin marcas"
ko "$SCRIPT"
ko "$SCRIPT" --opcion-que-no-existe

# Los mensajes de --range usan la misma expresión que el hook commit-msg del repositorio.
grep -qF "$(grep -o "'[^']*'" "$HERE/commit-msg" | head -1 | tr -d "'")" "$SCRIPT" && pass=$((pass + 1)) || { fail=$((fail + 1)); echo "FALLA: el patrón de mensajes no es el de commit-msg"; }

echo "check-signatures: $pass correctas, $fail fallidas"
[[ "$fail" == 0 ]]
