"""Selección y construcción de casos golden con datos sintéticos."""

from datetime import UTC, datetime

import numpy as np
import pandas as pd
import pytest

from verabank_ml.golden import (
    build_case,
    case_rows,
    choose_cases,
    decide_score,
    exclusion_report,
    history_counts,
    occurred_at,
)

EPOCH = datetime(2026, 1, 1, tzinfo=UTC)
THRESHOLDS = {"t_review": 0.5, "t_block": 0.8}
GCFG = {
    "total": 60,
    "fraud": 10,
    "above_review": 10,
    "boundary": 8,
    "boundary_distance": 0.0005,
    "max_history": 5,
}


def candidates(n=2000, seed=3):
    rng = np.random.default_rng(seed)
    y = (rng.random(n) < 0.05).astype(int)
    score = np.clip(rng.normal(0.25 + 0.5 * y, 0.25), 0, 1)
    # empuja filas a ambos lados de los umbrales, a menos de 0,0005
    near = rng.choice(n, 60, replace=False)
    score[near] = rng.choice([0.5, 0.8], 60) + rng.uniform(-0.0004, 0.0004, 60)
    return pd.DataFrame(
        {
            "row_id": np.arange(n),
            "type": rng.choice(
                ["TRANSFER", "CASH_OUT", "PAYMENT", "DEBIT"], n, p=[0.2, 0.3, 0.45, 0.05]
            ),
            "is_fraud": y,
            "score": score,
            "n_history": rng.integers(0, 9, n),
        }
    )


def test_decide_score_boundaries_use_rounded_score():
    assert decide_score(0.49999, 0.5, 0.8) == "APPROVE"
    assert decide_score(0.499996, 0.5, 0.8) == "REVIEW"  # redondea a 0.5
    assert decide_score(0.8, 0.5, 0.8) == "BLOCK"
    assert decide_score(0.799994, 0.5, 0.8) == "REVIEW"  # redondea a 0.79999


def test_history_counts_match_brute_force():
    rng = np.random.default_rng(1)
    n = 300
    step = rng.integers(1, 30, n)
    orig = rng.integers(0, 12, n)
    dest = rng.integers(0, 12, n)
    expected = [
        sum(1 for j in range(n) if step[j] < step[i] and (orig[j] == orig[i] or dest[j] == dest[i]))
        for i in range(n)
    ]
    assert history_counts(step, orig, dest).tolist() == expected


def test_choose_cases_composition_and_determinism():
    frame = candidates()
    first = choose_cases(frame, THRESHOLDS, GCFG, seed=42)
    again = choose_cases(frame, THRESHOLDS, GCFG, seed=42)
    pd.testing.assert_frame_equal(first, again)
    assert len(first) == GCFG["total"] and first["row_id"].is_unique
    assert (first["n_history"] <= GCFG["max_history"]).all()
    counts = first["stratum"].value_counts()
    assert counts["fraud"] == 10 and counts["above_review"] == 10 and counts["boundary"] == 8
    assert counts["random"] == 32
    legit_extra = first[first["stratum"].isin(["above_review", "random"])]
    assert (legit_extra["is_fraud"] == 0).all()
    assert (first.loc[first["stratum"] == "above_review", "score"].round(5) >= 0.5).all()
    boundary = first[first["stratum"] == "boundary"]
    distance = np.minimum((boundary["score"] - 0.5).abs(), (boundary["score"] - 0.8).abs())
    assert (distance < GCFG["boundary_distance"]).all()
    sides = {(round(s, 5) >= 0.5, round(s, 5) >= 0.8) for s in boundary["score"]}
    assert len(sides) >= 3  # ambos lados de ambos umbrales
    assert not first.sort_values("row_id")["row_id"].diff().dropna().le(0).any()


def test_choose_cases_stratifies_random_legit_by_type():
    first = choose_cases(candidates(20000), THRESHOLDS, {**GCFG, "total": 1000 // 5 + 20}, seed=1)
    random_cases = first[first["stratum"] == "random"]
    shares = random_cases["type"].value_counts(normalize=True)
    assert shares["PAYMENT"] > shares["TRANSFER"] > shares["DEBIT"]


def test_exclusion_report_counts_and_flags_bias():
    frame = candidates()
    report = exclusion_report(frame, max_history=5)
    assert report["n_candidates"] == len(frame)
    assert report["n_excluded"] == int((frame["n_history"] > 5).sum())
    assert sum(r["excluded"] for r in report["by_type"]["rows"].values()) == report["n_excluded"]
    # un sesgo fabricado: todos los fraudes con historia larga
    biased = frame.copy()
    biased.loc[biased["is_fraud"] == 1, "n_history"] = 9
    assert exclusion_report(biased, 5)["by_class"]["biased_at_5_percent"] is True
    assert exclusion_report(frame, 5)["by_class"]["biased_at_5_percent"] is False


def test_occurred_at_is_epoch_plus_step_minus_one_hours():
    assert occurred_at(1, EPOCH) == "2026-01-01T00:00:00Z"
    assert occurred_at(26, EPOCH) == "2026-01-02T01:00:00Z"
    assert occurred_at(744, EPOCH) == "2026-01-31T23:00:00Z"


def test_build_case_aliases_keep_equalities_and_hide_names():
    request = {
        "step": 10,
        "type": "TRANSFER",
        "amount": 5.0,
        "src_id": "C111111111",
        "dst_id": "M222222222",
    }
    history = [
        {
            "step": 3,
            "type": "CASH_OUT",
            "amount": 1.5,
            "src_id": "C333333333",
            "dst_id": "M222222222",
        },
        {
            "step": 4,
            "type": "TRANSFER",
            "amount": 2.5,
            "src_id": "C111111111",
            "dst_id": "C444444444",
        },
    ]
    case = build_case("g0001", request, history, {"amount": 5.0}, 0.9, "BLOCK", "fraud", EPOCH)
    assert (case["request"]["source"], case["request"]["dest"]) == ("a1", "a2")
    assert (case["history"][0]["source"], case["history"][0]["dest"]) == (
        "a3",
        "a2",
    )  # mismo destino
    assert (case["history"][1]["source"], case["history"][1]["dest"]) == (
        "a1",
        "a4",
    )  # mismo origen
    assert "C1111" not in str(case) and "M2222" not in str(case)
    assert case["request"]["occurred_at"] == "2026-01-01T09:00:00Z"
    rows, request_index = case_rows(case, EPOCH)
    assert request_index == 2 and [r["step"] for r in rows] == [3, 4, 10]


@pytest.mark.parametrize("bad", [{"total": 10_000}])
def test_choose_cases_fails_loudly_when_a_stratum_is_short(bad):
    with pytest.raises(ValueError, match="candidatos"):
        choose_cases(candidates(300), THRESHOLDS, {**GCFG, **bad}, seed=1)
