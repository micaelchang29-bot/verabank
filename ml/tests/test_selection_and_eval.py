"""Selección de modelo (§5d), guarda de ml-eval-test y evaluación en test con datos sintéticos."""

import json

import numpy as np
import pandas as pd
import pytest

from verabank_ml import eval_test, modeling
from verabank_ml.eval_test import TestAlreadyEvaluated, check_test_guard, evaluate_test, group_rates
from verabank_ml.features import load_config
from verabank_ml.selection import FAIL, PASS, select_model

CFG = {
    "selection": {"min_pr_auc_gain": 0.02, "max_inference_p95_ms": 10},
    "bootstrap": {"block_steps": 24, "sensitivity_block_steps": 6, "min_blocks": 10},
}


def model_result(pr_auc, cost):
    return {"pr_auc": pr_auc, "decision": {"cost": cost}}


def interval(n_blocks, low, high):
    return {"n_blocks": n_blocks, "metrics": {"pr_auc": {"low": low, "high": high}}}


def select(lr_pr=0.40, xgb_pr=0.60, lr_cost=100.0, xgb_cost=90.0, p95=1.0, n24=20, n6=60,
           lr24=(0.3, 0.5), xgb24=(0.55, 0.65), lr6=(0.3, 0.5), xgb6=(0.55, 0.65)):  # fmt: skip
    intervals = {
        "logreg": {"24": interval(n24, *lr24), "6": interval(n6, *lr6)},
        "xgb": {"24": interval(n24, *xgb24), "6": interval(n6, *xgb6)},
    }
    return select_model(
        model_result(lr_pr, lr_cost), model_result(xgb_pr, xgb_cost), p95, intervals, CFG
    )


def results(selection):
    return {c["name"]: c["result"] for c in selection["criteria"]}


def test_xgb_wins_when_all_four_criteria_pass():
    sel = select()
    assert sel["winner"] == "xgb" and sel["status"] == "DECIDIDO"
    assert set(results(sel).values()) == {PASS}


@pytest.mark.parametrize(
    ("kwargs", "failing"),
    [
        ({"xgb_pr": 0.41}, "pr_auc_gain"),
        ({"xgb_cost": 100.0}, "cost_lower"),
        ({"xgb_cost": 120.0}, "cost_lower"),
        ({"p95": 10.0}, "inference_p95"),
    ],
)
def test_logreg_wins_when_one_criterion_fails(kwargs, failing):
    sel = select(**kwargs)
    assert sel["winner"] == "logreg" and sel["status"] == "DECIDIDO"
    assert results(sel)[failing] == FAIL


def test_gain_boundary_is_inclusive():
    assert results(select(lr_pr=0.40, xgb_pr=0.42))["pr_auc_gain"] == PASS
    assert results(select(lr_pr=0.40, xgb_pr=0.4199))["pr_auc_gain"] == FAIL


OVERLAP = (0.45, 0.65)  # con lr (0.3, 0.5) se solapa; (0.55, 0.65) no


def test_intervals_same_result_in_both_sizes_without_overlap_pass():
    sel = select(xgb24=(0.55, 0.65), xgb6=(0.56, 0.66))
    assert results(sel)["pr_auc_intervals_do_not_overlap"] == PASS
    assert sel["winner"] == "xgb"


def test_intervals_overlapping_in_both_sizes_fail():
    sel = select(xgb24=OVERLAP, xgb6=OVERLAP)
    assert results(sel)["pr_auc_intervals_do_not_overlap"] == FAIL
    assert sel["winner"] == "logreg"


@pytest.mark.parametrize("kwargs", [{"xgb24": OVERLAP}, {"xgb6": OVERLAP}])
def test_intervals_that_differ_between_sizes_fail(kwargs):
    sel = select(**kwargs)
    criterion = next(c for c in sel["criteria"] if c["name"] == "pr_auc_intervals_do_not_overlap")
    assert criterion["result"] == FAIL
    assert criterion["value"]["differs_between_block_sizes"] is True
    assert sel["winner"] == "logreg"


def test_few_blocks_warn_but_do_not_stop_or_make_it_indeterminate():
    sel = select(n24=6, n6=21)
    assert results(sel)["pr_auc_intervals_do_not_overlap"] == PASS
    assert sel["winner"] == "xgb" and sel["status"] == "DECIDIDO"
    assert len(sel["warnings"]) == 1 and "6 bloques de 24 pasos" in sel["warnings"][0]
    assert select()["warnings"] == []


def test_both_sizes_with_few_blocks_give_two_warnings():
    assert len(select(n24=6, n6=8)["warnings"]) == 2


# --- guarda de ml-eval-test ------------------------------------------------------------------------


def test_guard_allows_the_first_evaluation(tmp_path):
    check_test_guard(tmp_path / "test_evaluation.json", tracked_runs=0, force=False)


def test_guard_refuses_a_second_evaluation_by_report_file(tmp_path):
    report = tmp_path / "test_evaluation.json"
    report.write_text("{}")
    with pytest.raises(TestAlreadyEvaluated, match="force-test"):
        check_test_guard(report, tracked_runs=0, force=False)


def test_guard_refuses_when_mlflow_already_has_a_test_run(tmp_path):
    with pytest.raises(TestAlreadyEvaluated):
        check_test_guard(tmp_path / "missing.json", tracked_runs=1, force=False)


def test_guard_allows_force(tmp_path):
    report = tmp_path / "test_evaluation.json"
    report.write_text("{}")
    check_test_guard(report, tracked_runs=3, force=True)


def test_training_cannot_read_the_test_partition():
    with pytest.raises(PermissionError, match="ml-eval-test"):
        modeling.load_features("A", ("train", "test"))


def test_eval_test_cli_refuses_without_thresholds(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(eval_test, "THRESHOLDS_PATH", tmp_path / "thresholds.json")
    assert eval_test.eval_test_command([]) == 2
    assert "thresholds.json" in capsys.readouterr().err


# --- evaluación en test con umbrales congelados ----------------------------------------------------


def synthetic_test(n=4000, seed=5):
    rng = np.random.default_rng(seed)
    y = (rng.random(n) < 0.05).astype(int)
    frame = pd.DataFrame(
        {
            "step": rng.integers(575, 745, n),
            "type": rng.choice(["TRANSFER", "CASH_OUT", "PAYMENT"], n),
            "amount": np.round(rng.lognormal(7, 2, n), 2),
            "is_fraud": y,
            "dst_id": [f"C{i}" for i in rng.integers(0, 300, n)],
        }
    )
    probabilities = np.clip(rng.normal(0.2 + 0.5 * y, 0.2), 0, 1)
    return frame, probabilities


THRESHOLDS = {
    "t_review": 0.5,
    "t_block": 0.8,
    "operating_points": {"fpr_0.005": 0.9, "fpr_0.001": 0.95},
}


def test_evaluate_test_uses_frozen_thresholds_and_unseen_destinations():
    frame, probabilities = synthetic_test()
    cfg = load_config()
    train_destinations = {f"C{i}" for i in range(150)}
    out = evaluate_test(probabilities, frame, train_destinations, THRESHOLDS, cfg)
    assert out["frozen_thresholds"]["t_block"] == 0.8
    assert out["overall"]["thresholds"] == {"t_review": 0.5, "t_block": 0.8}
    assert out["overall"]["operating_points"]["fpr_0.005"]["threshold"] == 0.9
    unseen = ~frame["dst_id"].isin(train_destinations)
    assert out["unseen_destinations"]["n_rows"] == int(unseen.sum())
    assert out["unseen_destinations"]["n_fraud"] == int(frame.loc[unseen, "is_fraud"].sum())
    assert out["vs_no_model"]["no_model_cost"] == pytest.approx(
        frame.loc[frame["is_fraud"] == 1, "amount"].sum()
    )
    assert out["intervals"]["24"]["n_blocks"] == 8 and out["intervals"]["6"]["n_blocks"] == 29
    assert {r["group"] for r in out["fpr_by_type"]} == {"TRANSFER", "CASH_OUT", "PAYMENT"}
    json.dumps(out, default=float)  # serializable


def test_group_rates_by_hand():
    frame = pd.DataFrame({"is_fraud": [0, 0, 0, 0, 1, 1], "type": ["A", "A", "A", "B", "A", "B"]})
    scores = np.array([0.9, 0.6, 0.1, 0.9, 0.95, 0.2])
    rows = {
        r["group"]: r
        for r in group_rates(frame, scores, t_review=0.5, t_block=0.9, keys=frame["type"])
    }
    # A: legítimas 0.9 (BLOCK), 0.6 (REVIEW), 0.1 → 3 legítimas, 1 bloqueada, 2 marcadas; fraude 0.95 detectado
    assert rows["A"]["n_legit"] == 3 and rows["A"]["fpr_block"] == pytest.approx(1 / 3)
    assert rows["A"]["fpr_review_or_block"] == pytest.approx(2 / 3)
    assert rows["A"]["recall_review_or_block"] == 1.0
    # B: legítima 0.9 (BLOCK); fraude 0.2 aprobado
    assert rows["B"]["fpr_block"] == 1.0 and rows["B"]["recall_review_or_block"] == 0.0
