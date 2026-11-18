"""Ejemplos de contracts/examples/ contra el sobre y el payload de su tipo, y los 14 event_type de docs/diseno.md §3.3.

- Cada tipo de §3.3 tiene esquema (events/<Tipo>.v1.schema.json) y ejemplo (examples/<Tipo>.v1.example.json).
- Cada ejemplo valida contra events/envelope.v1.json (con formatos uuid y date-time) y su payload contra el esquema
  del tipo que declara `event_type`.
- TransferScored: el ejemplo MODEL lleva `score`; el de FALLBACK no; y un TransferScored MODEL sin `score` debe fallar.
Uso: python contracts/validate_examples.py (necesita jsonschema, que instala check-jsonschema).
"""

import glob
import json
import os
import re
import sys
from copy import deepcopy

from jsonschema import Draft202012Validator, FormatChecker

HERE = os.path.dirname(os.path.abspath(__file__))
DESIGN = os.path.join(HERE, "..", "docs", "diseno.md")


def load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def design_event_types():
    text = open(DESIGN, encoding="utf-8").read()
    section = text[text.index("### 3.3"):text.index("### 3.4")]
    rows = re.findall(r"^\| [a-z.]+ \| (.+?) \|", section, re.M)      # columna event_type de la tabla
    return sorted({t for row in rows for t in re.findall(r"`([A-Z][A-Za-z]+)`", row)})


def validator(schema):
    return Draft202012Validator(schema, format_checker=FormatChecker())


def errors_of(example, envelope, payloads):
    errs = [e.message for e in validator(envelope).iter_errors(example)]
    schema = payloads.get(example.get("event_type"))
    if schema is None:
        return errs + [f"event_type sin esquema: {example.get('event_type')}"]
    return errs + [e.message for e in validator(schema).iter_errors(example.get("payload", {}))]


def main():
    envelope = load(os.path.join(HERE, "events", "envelope.v1.json"))
    payloads = {os.path.basename(p).split(".")[0]: load(p) for p in glob.glob(os.path.join(HERE, "events", "*.v1.schema.json"))}
    types = design_event_types()
    problems = []
    if len(types) != 14:
        problems.append(f"§3.3 debe listar 14 event_type y se leyeron {len(types)}: {types}")
    for t in types:
        if t not in payloads:
            problems.append(f"{t}: falta events/{t}.v1.schema.json")
        if not os.path.exists(os.path.join(HERE, "examples", f"{t}.v1.example.json")):
            problems.append(f"{t}: falta examples/{t}.v1.example.json")
    extra = set(payloads) - set(types)
    if extra:
        problems.append(f"esquemas sin event_type en §3.3: {sorted(extra)}")
    examples = sorted(glob.glob(os.path.join(HERE, "examples", "*.json")))
    for path in examples:
        for e in errors_of(load(path), envelope, payloads):
            problems.append(f"{os.path.basename(path)}: {e}")
    model = load(os.path.join(HERE, "examples", "TransferScored.v1.example.json"))
    fallback = load(os.path.join(HERE, "examples", "TransferScored.v1.fallback.example.json"))
    if model["payload"].get("decision_source") != "MODEL" or "score" not in model["payload"]:
        problems.append("TransferScored.v1.example.json debe ser MODEL con score")
    if fallback["payload"].get("decision_source") != "FALLBACK" or "score" in fallback["payload"]:
        problems.append("TransferScored.v1.fallback.example.json debe ser FALLBACK sin score")
    broken = deepcopy(model)
    del broken["payload"]["score"]
    if not errors_of(broken, envelope, payloads):
        problems.append("un TransferScored MODEL sin score no debería validar")
    if problems:
        print("\n".join(problems), file=sys.stderr)
        return 1
    print(f"{len(types)} event_type de §3.3 con esquema y ejemplo; {len(examples)} ejemplos válidos "
          "(TransferScored con score si MODEL, sin score si FALLBACK)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
