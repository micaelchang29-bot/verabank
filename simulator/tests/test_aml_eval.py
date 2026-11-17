"""``aml-eval``: caso pequeño con respuesta conocida (sin servicios)."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from verabank_sim.aml import eval_cli
from verabank_sim.aml.evaluate import evaluate, render

TRUTH = [
    {"typology": "STRUCTURING", "instance_id": "i0", "accounts": [], "transfer_ids": ["t1", "t2", "t3"]},
    {"typology": "STRUCTURING", "instance_id": "i1", "accounts": [], "transfer_ids": ["t4", "t5", "t6"]},
    {"typology": "COUNTRY_RISK", "instance_id": "i2", "accounts": [], "transfer_ids": ["t7"]},
    {"typology": "COUNTRY_RISK", "instance_id": "i3", "accounts": [], "transfer_ids": ["t8"]},
    {"typology": "FAN_IN", "instance_id": "i4", "accounts": [], "transfer_ids": ["t9", "t10"]},
    {"typology": "FAST_PASS", "instance_id": "i5", "accounts": [], "transfer_ids": ["t11", "t12"]},
]
MANIFEST = {
    "seed": 42, "transfers": {"total": 1000},
    "rule_coverage": {"STRUCTURING": {"pct": 50.0}, "COUNTRY_RISK": {"pct": 50.0}, "FAN_IN": {"pct": 100.0},
                      "FAST_PASS": {"pct": 0.0}},
}
LEGIT = ["l1", "l2"]
RUN_MAP = [{"transfer_id": t, "service_transfer_id": f"s-{t}", "outcome": "COMPLETED"}
           for t in [f"t{i}" for i in range(1, 13)] + LEGIT]
for entry in RUN_MAP:
    if entry["transfer_id"] == "t4":
        entry["outcome"] = "BLOCKED"
    if entry["transfer_id"] == "t11":
        entry["outcome"] = "NOT_COMPLETED"
    if entry["transfer_id"] == "t12":
        entry["outcome"] = "APPROVED_IN_REVIEW"
ALERTS = [
    {"rule_code": "STRUCTURING", "transfer_ids": ["s-t1", "s-t2"]},       # detecta i0
    {"rule_code": "STRUCTURING", "transfer_ids": ["s-l1"]},               # legítima: baja la precisión
    {"rule_code": "COUNTRY_RISK", "transfer_ids": ["s-t7"]},              # detecta i2
    {"rule_code": "COUNTRY_RISK", "transfer_ids": ["s-t8"]},              # detecta i3: recall 100 % > techo 50 %
    {"rule_code": "THRESHOLD", "transfer_ids": ["s-t9"]},                 # toca la instancia i4
    {"rule_code": "THRESHOLD", "transfer_ids": ["s-l2"]},                 # no toca ninguna
    {"rule_code": "STRUCTURING", "transfer_ids": ["ajena-1", "ajena-2"]}, # no toca data/aml: no cuenta (D12)
    {"rule_code": "STRUCTURING", "transfer_ids": ["s-t12"]},              # una regla de otra tipología no suma a la suya
]


def report():
    return evaluate(TRUTH, MANIFEST, RUN_MAP, ALERTS)


def test_cifras_del_caso_pequeno() -> None:
    r = report()
    assert (r["alerts_total"], r["alerts_touching_dataset"], r["alerts_excluded"]) == (8, 7, 1)
    assert r["alerts_per_1000_transfers"] == 7.0
    assert r["rules"]["STRUCTURING"] == {"alerts": 3, "touching_own_typology": 1, "precision_pct": 33.33}
    assert r["rules"]["COUNTRY_RISK"] == {"alerts": 2, "touching_own_typology": 2, "precision_pct": 100.0}
    assert r["rules"]["THRESHOLD"] == {"alerts": 2, "touching_any_instance": 1, "share_pct": 50.0}
    s, c = r["typologies"]["STRUCTURING"], r["typologies"]["COUNTRY_RISK"]
    assert (s["instances"], s["detected"], s["recall_pct"], s["ceiling_pct"], s["status"]) == (2, 1, 50.0, 50.0, "consistente")
    assert (c["instances"], c["detected"], c["recall_pct"], c["ceiling_pct"], c["status"]) == (2, 2, 100.0, 50.0, "inconsistente")


def test_fan_in_y_fast_pass_son_no_cubiertas_aunque_haya_alertas_que_las_toquen() -> None:
    t = report()["typologies"]
    for typ in ("FAN_IN", "FAST_PASS"):
        assert t[typ]["covered"] is False and t[typ]["detected"] == 0 and t[typ]["recall_pct"] == 0.0
        assert t[typ]["status"] == "no cubierta"
    assert t["FAN_IN"]["instances"] == 1 and t["FAST_PASS"]["instances"] == 1


def test_instancias_con_transferencias_rechazadas_o_sin_completar_van_aparte() -> None:
    t = report()["typologies"]
    assert t["STRUCTURING"]["with_blocked_transfer"] == 1 and t["STRUCTURING"]["with_incomplete_transfer"] == 0
    assert t["FAST_PASS"]["with_blocked_transfer"] == 0 and t["FAST_PASS"]["with_incomplete_transfer"] == 1
    assert t["COUNTRY_RISK"]["with_blocked_transfer"] == 0


def test_el_informe_es_determinista_y_solo_lleva_cifras_agregadas() -> None:
    first, second = render(report()), render(evaluate(TRUTH, MANIFEST, RUN_MAP, list(ALERTS)))
    assert first == second
    assert "inconsistente" in first and "no cubierta" in first and "33,33 %" in first
    assert "`COUNTRY_RISK` | 2 | 2 | 100,00 % | 50,00 % | inconsistente" in first
    assert not re.search(r"[0-9a-f]{8}-[0-9a-f]{4}", first) and "s-t1" not in first and "i0" not in first


def test_las_transferencias_sin_id_de_servicio_se_cuentan_y_no_rompen() -> None:
    run_map = RUN_MAP + [{"transfer_id": "t99", "service_transfer_id": None, "outcome": "NOT_COMPLETED"}]
    assert evaluate(TRUTH, MANIFEST, run_map, ALERTS)["transfers_unmapped"] == 1


def test_sin_alertas_el_recall_es_cero_y_nada_es_inconsistente() -> None:
    r = evaluate(TRUTH, MANIFEST, RUN_MAP, [])
    assert r["alerts_total"] == 0 and r["alerts_per_1000_transfers"] == 0.0
    assert {t["recall_pct"] for t in r["typologies"].values()} == {0.0}
    assert "inconsistente" not in {t["status"] for t in r["typologies"].values()}
    assert "(sin alertas)" in render(r)


class FakePsql:
    def query(self, sql: str) -> list[list[str]]:
        assert "risk.aml_alerts" in sql
        return [["STRUCTURING", "a,b"], ["THRESHOLD", "c"], ["COUNTRY_RISK", ""]]


def test_las_alertas_se_leen_con_el_rol_de_solo_lectura() -> None:
    assert eval_cli.read_alerts(FakePsql()) == [
        {"rule_code": "STRUCTURING", "transfer_ids": ["a", "b"]},
        {"rule_code": "THRESHOLD", "transfer_ids": ["c"]},
        {"rule_code": "COUNTRY_RISK", "transfer_ids": []}]


def test_el_comando_escribe_el_informe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import argparse

    truth = tmp_path / "ground_truth.jsonl"
    truth.write_text("".join(json.dumps(g) + "\n" for g in TRUTH))
    (tmp_path / "service_run.jsonl").write_text("".join(json.dumps(m) + "\n" for m in RUN_MAP))
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(MANIFEST))

    class Alerts:
        def query(self, sql: str) -> list[list[str]]:
            return [[a["rule_code"], ",".join(a["transfer_ids"])] for a in ALERTS]

    monkeypatch.setattr(eval_cli, "read_only_psql", lambda: Alerts())
    out = tmp_path / "docs" / "resultados" / "aml-eval.md"
    parser = argparse.ArgumentParser()
    eval_cli.add_arguments(parser)
    args = parser.parse_args(["--truth", str(truth), "--manifest", str(manifest), "--out", str(out)])
    assert eval_cli.run(args) == 0
    assert out.read_text() == render(report())
