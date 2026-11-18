#!/usr/bin/env bash
# Valida los contratos: esquemas de eventos (JSON Schema 2020-12), OpenAPI 3.1 y features.yaml.
# Requisitos: python 3.9+ con `pip install check-jsonschema openapi-spec-validator pyyaml`.
set -euo pipefail

cd "$(dirname "$0")"
PY="${PYTHON:-python}"

for mod in check_jsonschema openapi_spec_validator yaml; do
  if ! "$PY" -c "import $mod" 2>/dev/null; then
    echo "falta el módulo '$mod'; instala: $PY -m pip install check-jsonschema openapi-spec-validator pyyaml" >&2
    exit 2
  fi
done

echo "== esquemas de eventos contra el metaesquema JSON Schema 2020-12"
"$PY" -m check_jsonschema --check-metaschema events/*.json

echo "== convenciones: \$schema 2020-12, \$id con prefijo verabank/events/ y 14 payloads"
"$PY" -I - <<'PYEOF'
import glob, json, os, sys
DRAFT = "https://json-schema.org/draft/2020-12/schema"
errores, payloads = [], 0
for path in sorted(glob.glob("events/*.json")):
    nombre = os.path.basename(path)
    with open(path, encoding="utf-8") as f:
        s = json.load(f)
    if s.get("$schema") != DRAFT:
        errores.append(f"{nombre}: $schema debe ser {DRAFT}")
    if s.get("$id") != f"verabank/events/{nombre}":
        errores.append(f"{nombre}: $id debe ser verabank/events/{nombre}")
    if nombre.endswith(".v1.schema.json"):
        payloads += 1
        if s.get("additionalProperties") is False:
            errores.append(f"{nombre}: los payloads admiten propiedades adicionales")
if payloads != 14:
    errores.append(f"se esperaban 14 payloads y hay {payloads}")
if errores:
    print("\n".join(errores), file=sys.stderr)
    sys.exit(1)
print(f"{payloads} payloads y el envelope correctos")
PYEOF

echo "== ejemplos de eventos (sobre + payload) y los 14 event_type de §3.3"
"$PY" -I validate_examples.py

echo "== OpenAPI 3.1"
"$PY" -m openapi_spec_validator openapi.yaml

echo "== features.yaml"
"$PY" -c "import yaml,sys; d=yaml.safe_load(open('features.yaml',encoding='utf-8')); a=[f for f in d['features'] if f['set']=='A']; b=[f for f in d['features'] if f['set']=='B']; assert a and b, 'faltan conjuntos'; assert all({'name','type','window','origin'} <= f.keys() for f in d['features']), 'faltan campos'; print(len(a),'features en A,',len(b),'en B')"

echo "contratos válidos"
