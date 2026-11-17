#!/usr/bin/env bash
# Descarga PaySim (sin cuenta; el HEAD da 404, se usa GET) a data/raw/paysim.csv.
# Idempotente: no descarga si el CSV existe y coincide con el SHA-256 de ml/data.sha256.
# La primera vez valida el CSV (11 columnas, 6.362.620 filas) y escribe el hash.
set -euo pipefail
cd "$(dirname "$0")/.."

URL="https://www.kaggle.com/api/v1/datasets/download/ealaxi/paysim1"
RAW=data/raw
CSV=$RAW/paysim.csv
HASH_FILE=ml/data.sha256

sha() { sha256sum "$1" | cut -d' ' -f1; }

if [[ -f $CSV && -f $HASH_FILE ]]; then
  if [[ "$(sha "$CSV")" == "$(tr -d '[:space:]' < "$HASH_FILE")" ]]; then
    echo "data: $CSV ya está y coincide con $HASH_FILE; no se descarga"
    exit 0
  fi
  echo "data: $CSV no coincide con $HASH_FILE" >&2
  exit 1
fi

if [[ ! -f $CSV ]]; then
  mkdir -p "$RAW/paysim"
  curl -fL -o "$RAW/paysim1.zip" "$URL"
  if [[ "$(head -c 2 "$RAW/paysim1.zip")" != "PK" ]]; then
    echo "data: la respuesta no es un zip (¿redirige a un inicio de sesión?); se detiene" >&2
    exit 1
  fi
  unzip -o "$RAW/paysim1.zip" -d "$RAW/paysim"
  csvs=("$RAW"/paysim/*.csv)
  if [[ ${#csvs[@]} -ne 1 ]]; then
    echo "data: se esperaba un único CSV en el zip y hay ${#csvs[@]}" >&2
    exit 1
  fi
  mv "${csvs[0]}" "$CSV"
  rmdir "$RAW/paysim"
fi

uv run --project ml --frozen python -m verabank_ml check-paysim "$CSV"
sha "$CSV" > "$HASH_FILE"
echo "data: hash escrito en $HASH_FILE"
