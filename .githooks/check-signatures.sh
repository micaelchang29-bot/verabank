#!/usr/bin/env bash
# Revisión de firmas y pies de herramientas (CONTRIBUTING.md). Vive en .githooks/ porque contiene
# las marcas literales: --tree excluye este directorio y, si no, se detectaría a sí mismo (D11).
#
#   check-signatures.sh --range <rango>    mensajes de commit del rango (p. ej. origin/main..HEAD)
#   check-signatures.sh --tree [dir]       archivos versionados del árbol (sin .git ni .githooks)
#   check-signatures.sh --env VARIABLE     texto de una variable de entorno (descripción de un PR)
#   check-signatures.sh --stdin            texto por la entrada estándar
# Sale con 1 y lista dónde si encuentra alguna marca; con 0 si no. Varias opciones se pueden combinar.
set -euo pipefail

MSG_RE='co-authored-by:|generated with|-session:'   # las de los mensajes (igual que .githooks/commit-msg)
FILE_RE='co-authored-by|generated with'             # las del árbol y los textos

found=0
report() { echo "firma o pie de herramienta en $1" >&2; found=1; }

check_text() {   # $1: etiqueta; texto por stdin
  local hits
  hits=$(grep -niE "$FILE_RE|$MSG_RE" || true)
  [[ -z "$hits" ]] || report "$1: $(head -n 3 <<<"$hits" | tr '\n' ' ')"
}

[[ $# -gt 0 ]] || { sed -n '2,10p' "$0" >&2; exit 2; }
while [[ $# -gt 0 ]]; do
  case "$1" in
    --range)
      range="$2"; shift 2
      while read -r sha; do
        [[ -n "$sha" ]] || continue
        git log -1 --format=%B "$sha" | grep -qiE "$MSG_RE" && report "el mensaje del commit ${sha:0:12}"
      done < <(git rev-list "$range")
      ;;
    --tree)
      dir="${2:-.}"; [[ $# -ge 2 && "$2" != --* ]] && shift 2 || shift 1
      # Sin .git ni .githooks. En un repositorio, solo los archivos versionados (no los entornos .venv ni lo ignorado).
      if git -C "$dir" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
        hits=$(cd "$dir" && git ls-files -z | grep -zvE '^\.githooks/' \
               | xargs -0r grep -nilE "$FILE_RE" -- || true)
      else
        hits=$(grep -rnilE "$FILE_RE" --exclude-dir=.git --exclude-dir=.githooks "$dir" || true)
      fi
      [[ -z "$hits" ]] || report "los archivos: $(tr '\n' ' ' <<<"$hits")"
      ;;
    --env)
      var="$2"; shift 2
      check_text "la variable $var" <<<"${!var-}"
      ;;
    --stdin)
      shift; check_text "la entrada estándar"
      ;;
    *) echo "opción desconocida: $1" >&2; exit 2 ;;
  esac
done
[[ "$found" == 0 ]] && echo "sin firmas ni pies de herramientas"
exit "$found"
