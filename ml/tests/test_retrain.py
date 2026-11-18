"""Reentrenamiento, disparadores, promoción y rollback (5.F, D7 y D8). Sin datos de PaySim."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
import pytest
from mlflow.exceptions import MlflowException

from verabank_ml import export, metrics, modeling, retrain
from verabank_ml.features import load_config

EPOCH = datetime(2026, 1, 1, tzinfo=UTC)
NAMES = ["amount", "x", "y"]
RCFG = retrain.load_retrain_config()
CFG = load_config()


def at(step: int) -> datetime:
    return EPOCH + timedelta(hours=step - 1)


def labeled_rows(steps, per_step=20, fraud_every=5, seed=0, version="m/1") -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    rows = []
    for s in steps:
        for i in range(per_step):
            fraud = i % fraud_every == 0
            rows.append({"transfer_id": str(uuid.UUID(int=s * 1000 + i)), "occurred_at": at(s),
                         "features": {"amount": float(rng.lognormal(5, 1) * (3 if fraud else 1)), "x": float(rng.normal(fraud, 1)),
                                      "y": float(s)},
                         "amount": 0.0, "score": 0.1, "shadow_score": None, "decision": "APPROVE",
                         "model_version": version, "is_fraud": fraud})
    return pd.DataFrame(rows)


# --- conjunto -----------------------------------------------------------------------------------------------------


def test_dataset_excludes_warmup_and_immature_labels_and_keeps_feature_order():
    rows = pd.concat([labeled_rows(range(577, 721)), labeled_rows([600, 601], version="warmup", seed=1)])
    data = retrain.build_dataset(rows, at(720), NAMES, EPOCH, RCFG)
    info = data.info
    assert info["rows_warmup_excluded"] == 40
    assert info["max_step"] == 720 and info["mature_steps"] == [577, 672]          # 720 − 48
    assert info["rows_immature_excluded"] == 48 * 20
    for frame in (data.train, data.valid):
        assert frame["step"].max() <= 672 and set(frame["model_version"]) == {"m/1"}
        assert list(frame.columns[-len(NAMES) - 1:-1]) == NAMES or set(NAMES) <= set(frame.columns)
        assert modeling.matrix(frame, NAMES).shape[1] == 3
    # El monto que usa el modelo es la feature del JSONB, no la columna de la bitácora.
    assert (data.train["amount"] > 0).all()


def test_temporal_split_uses_no_future_rows_and_a_24_step_gap():
    data = retrain.build_dataset(labeled_rows(range(577, 721)), at(720), NAMES, EPOCH, RCFG)
    info = data.info
    # 96 pasos maduros (577–672): validación = últimos 24 (649–672); hueco 625–648; entrenamiento 577–624.
    assert info["valid_steps"] == [649, 672] and info["gap_steps"] == [625, 648] and info["train_steps"] == [577, 624]
    assert data.train["step"].max() < data.valid["step"].min() - 24
    assert data.valid["step"].min() - data.train["step"].max() - 1 == 24


def test_dataset_and_training_are_deterministic():
    rows = labeled_rows(range(577, 721))
    a = retrain.build_dataset(rows.sample(frac=1, random_state=1), at(720), NAMES, EPOCH, RCFG)
    b = retrain.build_dataset(rows.sample(frac=1, random_state=2), at(720), NAMES, EPOCH, RCFG)
    pd.testing.assert_frame_equal(a.train, b.train)
    pd.testing.assert_frame_equal(a.valid, b.valid)
    args = (modeling.matrix(a.train, NAMES), a.train["is_fraud"].to_numpy(), modeling.matrix(a.valid, NAMES),
            a.valid["is_fraud"].to_numpy())
    params = {"max_depth": "3", "scale_pos_weight": "30"}
    m1, _ = retrain.fit_family("xgb", params, CFG, *args)
    m2, _ = retrain.fit_family("xgb", params, CFG, *args)
    assert np.array_equal(m1.predict_proba(args[2]), m2.predict_proba(args[2]))


def test_refuses_below_the_minimum_frauds():
    rows = labeled_rows(range(577, 721), fraud_every=1000)           # un fraude por paso
    with pytest.raises(retrain.Refused, match="datos insuficientes"):
        retrain.build_dataset(rows, at(720), NAMES, EPOCH, RCFG)


def test_refuses_without_mature_rows():
    with pytest.raises(retrain.Refused, match="maduras"):
        retrain.build_dataset(labeled_rows(range(700, 721)), at(720), NAMES, EPOCH, RCFG)


# --- disparadores -------------------------------------------------------------------------------------------------


def triggers(**kw):
    base = {"drift_alerts": [], "champion_recall": 0.48, "validation_recall": 0.478125, "max_occurred_at": at(700),
                "cutoff": EPOCH + timedelta(hours=400), "tcfg": RCFG["triggers"]}
    return {t["trigger"]: t["fired"] for t in retrain.evaluate_triggers(**{**base, **kw})}


def test_triggers_by_hand():
    assert triggers() == {"drift": False, "recall": False, "schedule": False}
    assert triggers(drift_alerts=["score"])["drift"]
    # 0,478125 − 0,428 = 5,0125 puntos > 5; 0,428125 → exactamente 5, no.
    assert triggers(champion_recall=0.428)["recall"]
    assert not triggers(champion_recall=0.428125)["recall"]
    assert not triggers(champion_recall=None)["recall"]
    # (paso 1121 − 1) h − 400 h = 720 h = 30 días: no; un paso más, sí.
    assert not triggers(max_occurred_at=at(1121))["schedule"]
    assert triggers(max_occurred_at=at(1122))["schedule"]


def test_recall_counts_review_and_block():
    d = np.array([metrics.APPROVE, metrics.REVIEW, metrics.BLOCK, metrics.APPROVE])
    assert retrain.recall(d, np.array([1, 1, 1, 0])) == pytest.approx(2 / 3)
    assert retrain.recall(d, np.zeros(4, int)) is None


# --- promoción ----------------------------------------------------------------------------------------------------

CHAMP_T = {"t_review": 0.5, "t_block": 0.9}
CHALL_T = {"t_review": 0.5, "t_block": 0.9}


def shadow_frame(champion_cost_rows: int, challenger_cost_rows: int, steps: int = 24, recall_drop: bool = False):
    """Costo = 3·n(REVIEW) (sin fraude aprobado ni bloqueos): cada modelo manda a revisión ``*_cost_rows`` legítimas."""
    n = max(champion_cost_rows, challenger_cost_rows) + 10
    rows = []
    for i in range(n):
        rows.append({"step": 600 + i % steps, "amount": 100.0, "is_fraud": 0,
                     "score": 0.6 if i < champion_cost_rows else 0.1,
                     "shadow_score": 0.6 if i < challenger_cost_rows else 0.1})
    rows.append({"step": 600, "amount": 1000.0, "is_fraud": 1, "score": 0.95, "shadow_score": 0.1 if recall_drop else 0.95})
    return pd.DataFrame(rows)


def decide(frame, scores=None):
    return retrain.promotion_decision(frame, CHAMP_T, CHALL_T, scores, RCFG["promotion"], CFG["costs"])


def test_exactly_two_percent_cheaper_passes_and_1_99_does_not():
    ok = decide(shadow_frame(10000, 9800))
    assert ok.ok and ok.figures["cost_improvement"] == pytest.approx(0.02)
    no = decide(shadow_frame(10000, 9801))
    assert not no.ok and no.figures["cost_improvement"] == pytest.approx(0.0199) and "costo" in no.reason


def test_equal_recall_passes_and_lower_recall_does_not():
    assert decide(shadow_frame(100, 50)).ok
    worse = decide(shadow_frame(100, 50, recall_drop=True))
    assert not worse.ok and "recall" in worse.reason


def test_refuses_without_enough_shadow_steps():
    d = decide(shadow_frame(100, 50, steps=23))
    assert not d.ok and d.figures["shadow_steps"] == 23 and "pasos en sombra" in d.reason


def test_refuses_when_the_shadow_scores_are_not_from_this_challenger():
    frame = shadow_frame(100, 50)
    d = decide(frame, scores=frame["shadow_score"].to_numpy() + 0.01)
    assert not d.ok and "no es de este challenger" in d.reason
    assert decide(frame, scores=frame["shadow_score"].to_numpy() + 1e-7).ok


class FakeClient:
    """Alias y etiquetas de MLflow en memoria."""

    def __init__(self, aliases: dict[tuple[str, str], str]):
        self.aliases, self.tags = dict(aliases), {}

    def get_model_version_by_alias(self, name, alias):
        if (name, alias) not in self.aliases:
            raise MlflowException(f"{name}@{alias} no existe")
        return type("V", (), {"version": self.aliases[(name, alias)]})

    def set_registered_model_alias(self, name, alias, version):
        self.aliases[(name, alias)] = str(version)

    def delete_registered_model_alias(self, name, alias):
        del self.aliases[(name, alias)]

    def get_registered_model(self, name):
        return type("M", (), {"tags": {k[1]: v for k, v in self.tags.items() if k[0] == name}})

    def set_registered_model_tag(self, name, key, value):
        self.tags[(name, key)] = value


def registry(**aliases):
    # Tras make retrain: el challenger reentrenado es fraud-xgb/7 (el alias ya no está en fraud-logreg).
    start = {("fraud-xgb", "champion"): "6", ("fraud-xgb", "challenger"): "7"}
    client = FakeClient(start)
    return retrain.Registry(client, CFG), client


def test_promote_moves_the_aliases_records_previous_and_reexports():
    reg, client = registry()
    exports = []
    out = retrain.promote(reg, retrain.Decision(True, "ok", {}), lambda: exports.append(dict(client.aliases)))
    assert out == {"champion": "fraud-xgb/7", "previous_champion": "fraud-xgb/6"}
    assert client.aliases == {("fraud-xgb", "champion"): "7", ("fraud-xgb", "challenger"): "6"}
    assert reg.tag(retrain.PREVIOUS_TAG) == "fraud-xgb/6"
    assert exports == [client.aliases]                     # una reexportación, con los alias ya movidos


def test_a_refused_promotion_changes_nothing():
    reg, client = registry()
    before = dict(client.aliases)
    with pytest.raises(retrain.Refused):
        retrain.promote(reg, retrain.Decision(False, "el costo baja 1 %", {}), lambda: pytest.fail("no debe exportar"))
    assert client.aliases == before and not client.tags


def test_rollback_restores_the_previous_state_and_reexports():
    reg, _client = registry()
    retrain.promote(reg, retrain.Decision(True, "ok", {}), lambda: None)
    exports = []
    out = retrain.rollback(reg, lambda: exports.append(True))
    assert out["champion"] == "fraud-xgb/6" and exports == [True]
    assert reg.get("champion") == ("fraud-xgb", "6") and reg.get("challenger") == ("fraud-xgb", "7")


def test_an_alias_in_two_registered_models_is_an_error():
    reg = retrain.Registry(FakeClient({("fraud-xgb", "challenger"): "7", ("fraud-logreg", "challenger"): "6"}), CFG)
    with pytest.raises(RuntimeError, match="varios modelos"):
        reg.get("challenger")


def test_setting_an_alias_removes_it_from_the_other_registered_model():
    client = FakeClient({("fraud-logreg", "challenger"): "6", ("fraud-xgb", "champion"): "6"})
    retrain.Registry(client, CFG).set("challenger", "fraud-xgb", "7")
    assert client.aliases == {("fraud-xgb", "champion"): "6", ("fraud-xgb", "challenger"): "7"}


def test_rollback_without_history_refuses():
    reg, _ = registry()
    with pytest.raises(retrain.Refused, match="previous_champion"):
        retrain.rollback(reg, lambda: pytest.fail("no debe exportar"))


# --- exportación idéntica (D8, punto 4) ---------------------------------------------------------------------------


class NoArtifactClient:
    """El run del champion actual (entrenado antes de la fase 5) no tiene thresholds.json adjunto."""

    def get_model_version(self, name, version):
        return type("V", (), {"run_id": "r1"})

    def list_artifacts(self, run_id):
        return [type("A", (), {"path": "model"}), type("A", (), {"path": "bootstrap_intervals.json"})]


def rewritten(thresholds: dict, model_sha: str) -> bytes:
    """Lo que hace ml-export con los umbrales: inserta model_sha256 tras model_version."""
    updated = {}
    for key, value in thresholds.items():
        if key != "model_sha256":
            updated[key] = value
        if key == "model_version":
            updated["model_sha256"] = model_sha
    return (json.dumps(updated, indent=2, ensure_ascii=False) + "\n").encode()


def test_export_reads_the_current_champion_thresholds_byte_for_byte():
    current = json.loads((export.THRESHOLDS_PATH).read_text())
    name, version = current["model_version"].split("/")
    resolved = export.champion_thresholds(NoArtifactClient(), ("xgb", name, version, None))
    assert rewritten(resolved, current["model_sha256"]) == export.THRESHOLDS_PATH.read_bytes()


def test_export_refuses_a_champion_whose_thresholds_are_missing():
    with pytest.raises(RuntimeError, match="no tiene thresholds.json"):
        export.champion_thresholds(NoArtifactClient(), ("xgb", "fraud-xgb", "99", None))


def test_export_prefers_the_thresholds_attached_to_the_run(tmp_path):
    attached = {"model_version": "fraud-xgb/7", "t_review": 0.7}
    (tmp_path / "thresholds.json").write_text(json.dumps(attached))

    class WithArtifact(NoArtifactClient):
        def list_artifacts(self, run_id):
            return [type("A", (), {"path": "thresholds.json"})]

        def download_artifacts(self, run_id, path):
            return str(tmp_path / path)

    assert export.champion_thresholds(WithArtifact(), ("xgb", "fraud-xgb", "7", None)) == attached
