"""Pruebas del generador AML (sin servicios): determinismo, verdad de terreno y rangos declarados."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator, FormatChecker

from verabank_sim.amlgen.cli import DEFAULT_CONFIG, build

ROOT = Path(__file__).resolve().parents[2]
CFG = yaml.safe_load(DEFAULT_CONFIG.read_text())
T = Decimal(str(CFG["threshold"]))
HOME = CFG["background"]["home_country"]


def _jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def _ts(t: dict) -> datetime:
    return datetime.fromisoformat(t["occurred_at"])


@pytest.fixture(scope="module")
def run(tmp_path_factory: pytest.TempPathFactory) -> dict:
    out = tmp_path_factory.mktemp("aml")
    manifest = build(DEFAULT_CONFIG, 42, out)
    transfers = _jsonl(out / "transfers.jsonl")
    return {
        "out": out,
        "manifest": manifest,
        "transfers": transfers,
        "by_id": {t["transfer_id"]: t for t in transfers},
        "truth": _jsonl(out / "ground_truth.jsonl"),
    }


def _instances(run: dict, typology: str) -> list[tuple[dict, list[dict]]]:
    return [(g, [run["by_id"][i] for i in g["transfer_ids"]]) for g in run["truth"] if g["typology"] == typology]


def test_determinista(run: dict, tmp_path: Path) -> None:
    build(DEFAULT_CONFIG, 42, tmp_path)
    for name in ("transfers.jsonl", "ground_truth.jsonl", "manifest.json"):
        assert (tmp_path / name).read_bytes() == (run["out"] / name).read_bytes(), name


def test_otra_semilla_cambia_la_salida(run: dict, tmp_path: Path) -> None:
    build(DEFAULT_CONFIG, 7, tmp_path)
    assert (tmp_path / "transfers.jsonl").read_bytes() != (run["out"] / "transfers.jsonl").read_bytes()


def test_sim_epoch_coincide_con_ml_config() -> None:
    assert CFG["sim_epoch"] == yaml.safe_load((ROOT / "ml" / "config.yaml").read_text())["sim_epoch"]


def test_transfers_sin_verdad_de_terreno(run: dict) -> None:
    allowed = {"transfer_id", "source_account_id", "dest_account_id", "dest_external_ref", "dest_country",
               "rail", "transfer_type", "amount", "currency", "occurred_at"}
    for t in run["transfers"]:
        assert set(t) <= allowed
    raw = (run["out"] / "transfers.jsonl").read_text().lower()
    for word in ("typology", "instance", "ground", "truth", "structuring", "fan_in", "fast_pass", "country_risk"):
        assert word not in raw
    assert len({t["transfer_id"] for t in run["transfers"]}) == len(run["transfers"])


def test_ids_de_la_verdad_existen(run: dict) -> None:
    assert run["truth"]
    for g in run["truth"]:
        assert g["transfer_ids"] and g["accounts"]
        assert all(i in run["by_id"] for i in g["transfer_ids"])
    ids = [i for g in run["truth"] for i in g["transfer_ids"]]
    assert len(ids) == len(set(ids)), "una transferencia pertenece a una sola instancia"
    assert len({g["instance_id"] for g in run["truth"]}) == len(run["truth"])


def test_manifest_coherente(run: dict) -> None:
    import hashlib

    m = run["manifest"]
    assert m["seed"] == 42
    assert m["transfers"]["total"] == len(run["transfers"])
    for name, digest in m["sha256"].items():
        assert hashlib.sha256((run["out"] / name).read_bytes()).hexdigest() == digest
    for typ, p in CFG["typologies"].items():
        assert m["instances"][typ] == p["instances"]
    assert json.loads((run["out"] / "manifest.json").read_text()) == m


def test_structuring_en_rango(run: dict) -> None:
    p = CFG["typologies"]["STRUCTURING"]
    for g, ts in _instances(run, "STRUCTURING"):
        assert p["transfers"][0] <= len(ts) <= p["transfers"][1]
        assert len({t["source_account_id"] for t in ts}) == 1
        for t in ts:
            pct = Decimal(t["amount"]) / T
            assert Decimal(str(p["amount_pct"][0])) <= pct <= Decimal(str(p["amount_pct"][1]))
        span = max(map(_ts, ts)) - min(map(_ts, ts))
        assert timedelta(days=p["span_days"][0]) <= span <= timedelta(days=p["span_days"][1])


def test_country_risk_en_rango(run: dict) -> None:
    p = CFG["typologies"]["COUNTRY_RISK"]
    listed = set(CFG["risk_countries"]["high"]) | set(CFG["risk_countries"]["watch"])
    for g, ts in _instances(run, "COUNTRY_RISK"):
        assert p["transfers"][0] <= len(ts) <= p["transfers"][1]
        assert {t["dest_country"] for t in ts} <= listed and len({t["dest_country"] for t in ts}) == 1
        assert all(t["rail"] == "EXTERNAL" for t in ts)
        assert all(p["amount_range"][0] <= float(t["amount"]) <= p["amount_range"][1] for t in ts)
    # el fondo legítimo no toca países de riesgo
    in_truth = {i for g in run["truth"] for i in g["transfer_ids"]}
    assert not [t for t in run["transfers"] if t["dest_country"] in listed and t["transfer_id"] not in in_truth]


def test_fan_in_en_rango(run: dict) -> None:
    p = CFG["typologies"]["FAN_IN"]
    for g, ts in _instances(run, "FAN_IN"):
        assert p["senders"][0] <= len(ts) <= p["senders"][1]
        assert len({t["source_account_id"] for t in ts}) == len(ts), "emisores distintos"
        assert len({t["dest_account_id"] for t in ts}) == 1
        assert max(map(_ts, ts)) - min(map(_ts, ts)) < timedelta(hours=p["window_hours"])


def test_fast_pass_en_rango(run: dict) -> None:
    p = CFG["typologies"]["FAST_PASS"]
    for g, (t_in, t_out) in _instances(run, "FAST_PASS"):
        assert t_in["dest_account_id"] == t_out["source_account_id"]
        assert p["amount_range"][0] <= float(t_in["amount"]) <= p["amount_range"][1]
        assert Decimal(t_out["amount"]) / Decimal(t_in["amount"]) >= Decimal("0.90")
        assert Decimal(t_out["amount"]) <= Decimal(t_in["amount"])
        assert timedelta(0) < _ts(t_out) - _ts(t_in) < timedelta(hours=2)


def test_cobertura_estimada(run: dict) -> None:
    cov = run["manifest"]["rule_coverage"]
    assert 0 < cov["STRUCTURING"]["pct"] < 100
    assert cov["FAST_PASS"]["pct"] == 0 and cov["FAST_PASS"]["rule"] is None
    assert cov["FAN_IN"]["in_scope"] is False
    assert cov["FAN_IN"]["instances"] == CFG["typologies"]["FAN_IN"]["instances"]


def test_ruido_cercano_al_umbral(run: dict) -> None:
    in_truth = {i for g in run["truth"] for i in g["transfer_ids"]}
    legit = [t for t in run["transfers"] if t["transfer_id"] not in in_truth]
    lo, hi = (T * Decimal(str(x)) for x in CFG["background"]["near_threshold_pct"])
    near = sum(1 for t in legit if lo <= Decimal(t["amount"]) <= hi)
    share = near / len(legit)
    target = CFG["background"]["near_threshold_share"]
    sigma = (target * (1 - target) / len(legit)) ** 0.5
    assert abs(share - target) <= 4 * sigma + 0.001, share
    assert 0 < share < 0.05


def test_cuerpo_valido_contra_openapi(run: dict) -> None:
    import yaml as _yaml

    spec = _yaml.safe_load((ROOT / "contracts" / "openapi.yaml").read_text())
    schema = {"$ref": "#/components/schemas/TransferRequest", "components": spec["components"]}
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    body_keys = set(spec["components"]["schemas"]["TransferRequest"]["properties"])
    for t in run["transfers"]:
        body = {k: v for k, v in t.items() if k != "transfer_id"}
        errors = list(validator.iter_errors(body))
        assert not errors, (t, errors[0].message)
        assert set(body) <= body_keys
