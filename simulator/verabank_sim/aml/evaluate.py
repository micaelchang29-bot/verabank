"""Evaluación de las reglas AML contra la verdad de terreno de ``amlgen`` (D12).

Reglas: solo cuentan las alertas que tocan transferencias de ``data/aml/``; ``STRUCTURING`` y ``COUNTRY_RISK`` se miden
contra su tipología homónima; ``THRESHOLD`` se informa aparte; ``FAN_IN`` y ``FAST_PASS`` no están cubiertas por
diseño (línea de corte, docs/diseno.md §8). Si el recall supera el techo de cobertura del generador, se marca «inconsistente»: las
implementaciones de las reglas discrepan y se investiga, no se «arregla».

El informe es determinista y solo lleva cifras agregadas (ningún identificador de cuenta ni de transferencia).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from typing import Any

MEASURED = {"STRUCTURING": "STRUCTURING", "COUNTRY_RISK": "COUNTRY_RISK"}   # regla -> tipología
NOT_COVERED = ("FAN_IN", "FAST_PASS")
TYPOLOGIES = ("STRUCTURING", "COUNTRY_RISK", "FAN_IN", "FAST_PASS")
BLOCKED, NOT_COMPLETED = "BLOCKED", "NOT_COMPLETED"


def _pct(part: int, whole: int) -> float:
    return round(100.0 * part / whole, 2) if whole else 0.0


def evaluate(truth: Sequence[dict[str, Any]], manifest: dict[str, Any], run_map: Sequence[dict[str, Any]],
             alerts: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """``alerts``: ``{"rule_code": str, "transfer_ids": [id del servicio, ...]}``. Devuelve las cifras del informe."""
    to_generator = {r["service_transfer_id"]: r["transfer_id"] for r in run_map if r.get("service_transfer_id")}
    outcome = {r["transfer_id"]: r["outcome"] for r in run_map}
    instance_of: dict[str, list[tuple[str, int]]] = defaultdict(list)   # id del generador -> [(tipología, n.º de instancia)]
    for n, g in enumerate(truth):
        for tid in g["transfer_ids"]:
            instance_of[tid].append((g["typology"], n))

    touching: list[tuple[str, set[str]]] = []
    total_alerts = 0
    for a in alerts:
        total_alerts += 1
        touched = {to_generator[i] for i in a["transfer_ids"] if i in to_generator}
        if touched:
            touching.append((a["rule_code"], touched))
    by_rule: dict[str, list[set[str]]] = defaultdict(list)
    for rule, touched in touching:
        by_rule[rule].append(touched)

    rules: dict[str, Any] = {}
    for rule in sorted(by_rule):
        sets = by_rule[rule]
        if rule in MEASURED:
            hits = sum(1 for s in sets if any(t == MEASURED[rule] for tid in s for t, _ in instance_of[tid]))
            rules[rule] = {"alerts": len(sets), "touching_own_typology": hits, "precision_pct": _pct(hits, len(sets))}
        else:
            hits = sum(1 for s in sets if any(instance_of[tid] for tid in s))
            rules[rule] = {"alerts": len(sets), "touching_any_instance": hits, "share_pct": _pct(hits, len(sets))}

    typologies: dict[str, Any] = {}
    for typ in TYPOLOGIES:
        instances = [(n, g) for n, g in enumerate(truth) if g["typology"] == typ]
        entry: dict[str, Any] = {"instances": len(instances)}
        covered_by = [s for s in by_rule.get(typ, [])] if typ in MEASURED else []
        detected = sum(1 for _, g in instances if any(set(g["transfer_ids"]) & s for s in covered_by))
        ceiling = manifest["rule_coverage"][typ]["pct"]
        if typ in NOT_COVERED:
            entry.update({"covered": False, "detected": 0, "recall_pct": 0.0, "ceiling_pct": None,
                          "status": "no cubierta"})
        else:
            recall = _pct(detected, len(instances))
            entry.update({"covered": True, "detected": detected, "recall_pct": recall, "ceiling_pct": ceiling,
                          "status": "inconsistente" if recall > ceiling else "consistente"})
        blocked = sum(1 for _, g in instances if any(outcome.get(t) == BLOCKED for t in g["transfer_ids"]))
        incomplete = sum(1 for _, g in instances if any(outcome.get(t) == NOT_COMPLETED for t in g["transfer_ids"]))
        entry.update({"with_blocked_transfer": blocked, "with_incomplete_transfer": incomplete})
        typologies[typ] = entry

    transfers_total = manifest["transfers"]["total"]
    return {
        "seed": manifest["seed"],
        "transfers": transfers_total,
        "alerts_total": total_alerts,
        "alerts_touching_dataset": len(touching),
        "alerts_excluded": total_alerts - len(touching),
        "alerts_per_1000_transfers": round(1000.0 * len(touching) / transfers_total, 2) if transfers_total else 0.0,
        "transfers_unmapped": sum(1 for r in run_map if not r.get("service_transfer_id")),
        "rules": rules,
        "typologies": typologies,
    }


def _fmt(value: float) -> str:
    return f"{value:.2f}".replace(".", ",")


def render(report: dict[str, Any]) -> str:
    r = report
    lines = [
        "# Evaluación de las reglas AML",
        "",
        "Contra la verdad de terreno de `amlgen` (D12). Solo cuentan las alertas que tocan transferencias de `data/aml/`; "
        "`BLOCK` y `REVIEW` se informan aparte. No se ajustó ninguna regla ni parámetro del generador para mejorar estas cifras.",
        "",
        f"- Semilla del generador: {r['seed']}",
        f"- Transferencias de `data/aml/`: {r['transfers']}",
        f"- Alertas en `risk.aml_alerts`: {r['alerts_total']}; tocan estas transferencias: {r['alerts_touching_dataset']}; "
        f"excluidas por no tocarlas: {r['alerts_excluded']}",
        f"- Alertas por cada 1.000 transferencias: {_fmt(r['alerts_per_1000_transfers'])}",
        f"- Transferencias del archivo sin id de servicio (no creadas): {r['transfers_unmapped']}",
        "",
        "## Reglas",
        "",
        "| Regla | Alertas | Tocan una instancia de su tipología | Precisión |",
        "|---|---:|---:|---:|",
    ]
    for rule in ("STRUCTURING", "COUNTRY_RISK"):
        d = r["rules"].get(rule, {"alerts": 0, "touching_own_typology": 0, "precision_pct": 0.0})
        lines.append(f"| `{rule}` | {d['alerts']} | {d['touching_own_typology']} | {_fmt(d['precision_pct'])} % |")
    lines += ["", "### `THRESHOLD` y otras reglas (solo conteo)", "",
              "| Regla | Alertas | Tocan alguna instancia | Proporción |", "|---|---:|---:|---:|"]
    others = {k: v for k, v in r["rules"].items() if k not in MEASURED}
    if not others:
        lines.append("| (sin alertas) | 0 | 0 | 0,00 % |")
    for rule, d in others.items():
        lines.append(f"| `{rule}` | {d['alerts']} | {d['touching_any_instance']} | {_fmt(d['share_pct'])} % |")
    lines += ["", "## Tipologías", "",
              "| Tipología | Instancias | Detectadas | Recall | Techo de cobertura | Estado |", "|---|---:|---:|---:|---:|---|"]
    for typ in TYPOLOGIES:
        t = r["typologies"][typ]
        if t["covered"]:
            lines.append(f"| `{typ}` | {t['instances']} | {t['detected']} | {_fmt(t['recall_pct'])} % | "
                         f"{_fmt(t['ceiling_pct'])} % | {t['status']} |")
        else:
            lines.append(f"| `{typ}` | {t['instances']} | 0 | 0,00 % (por diseño) | — | no cubierta |")
    lines += ["",
              "`FAN_IN` (fuera del corte de alcance) y `FAST_PASS` (sin regla en §5a) no están cubiertas: recall 0 por diseño "
              "(línea de corte del alcance, `docs/diseno.md` §8). Si el recall de una tipología supera su techo, queda «inconsistente»: la implementación de la "
              "regla y la copia mínima del generador discrepan, y hay que investigarlo.",
              "", "## Instancias afectadas por transferencias rechazadas o sin completar (aparte)", "",
              "| Tipología | Con alguna rechazada por `BLOCK` | Con alguna sin completar |", "|---|---:|---:|"]
    for typ in TYPOLOGIES:
        t = r["typologies"][typ]
        lines.append(f"| `{typ}` | {t['with_blocked_transfer']} | {t['with_incomplete_transfer']} |")
    lines.append("")
    return "\n".join(lines)
