"""Métricas de decisión, umbrales y bootstrap por bloques: ejemplos pequeños con respuesta conocida."""

import numpy as np
import pytest
from sklearn.metrics import average_precision_score

from verabank_ml.bootstrap import (
    WeightedPRAUC,
    block_ids,
    bootstrap_metrics,
    bootstrap_pr_auc,
    resample_counts,
)
from verabank_ml.metrics import (
    APPROVE,
    BLOCK,
    REVIEW,
    choose_t_block,
    choose_t_review,
    cost,
    cost_sensitivity,
    decide,
    decision_summary,
    fpr_threshold,
    no_model_cost,
    operating_point,
    round_scores,
)

COSTS = {"review": 3, "block_false_positive": 15, "sensitivity": 0.5}


def test_round_scores_to_five_decimals():
    assert round_scores([0.123456, 0.999996, 0.000004]).tolist() == [0.12346, 1.0, 0.0]


def test_decide_uses_greater_or_equal():
    scores = np.array([0.1, 0.2, 0.3, 0.4, 0.5])
    assert decide(scores, t_review=0.2, t_block=0.4).tolist() == [
        APPROVE,
        REVIEW,
        REVIEW,
        BLOCK,
        BLOCK,
    ]


# Ejemplo a mano: 10 legítimas (y=0) y 3 fraudes (y=1).
#   legítimas:  0.1 0.1 0.2 0.3 0.4 0.5 0.6 0.7 0.8 0.9
#   fraudes:    0.55 0.85 0.95 con montos 100, 200, 400
S = np.array([0.1, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 0.55, 0.85, 0.95])
Y = np.array([0] * 10 + [1] * 3)
AMOUNT = np.array([10.0] * 10 + [100.0, 200.0, 400.0])


def test_operating_point_by_hand():
    # umbral 0.8: marca legítimas 0.8, 0.9 (FPR 2/10) y fraudes 0.85, 0.95 (recall 2/3)
    point = operating_point(S, Y, AMOUNT, 0.8)
    assert point["fpr"] == pytest.approx(0.2)
    assert point["recall"] == pytest.approx(2 / 3)
    assert point["recall_weighted"] == pytest.approx(600 / 700)


def test_fpr_threshold_by_hand():
    # Candidatos: todos los scores observados. FPR(0.85) = FPR(0.9) = 1/10 (solo la legítima 0.9) y
    # FPR(0.8) = 2/10: con tope 0.1 el menor umbral es 0.85 (marca además el fraude 0.85);
    # con tope 0.2 es 0.8; con tope 0 solo cumple 0.95; con tope 1 cualquiera, el menor.
    assert fpr_threshold(S, Y, 0.1) == 0.85
    assert fpr_threshold(S, Y, 0.2) == 0.8
    assert fpr_threshold(S, Y, 0.0) == 0.95  # el fraude 0.95 queda marcado, ninguna legítima
    assert fpr_threshold(S, Y, 1.0) == 0.1


def test_fpr_threshold_with_ties_from_rounding():
    # Tras redondear, 3 legítimas empatan en 0.50000: el FPR salta de 0/4 a 3/4 y no hay umbral intermedio.
    scores = round_scores([0.500001, 0.499999, 0.5000004, 0.1, 0.9, 0.2])
    y = np.array([0, 0, 0, 0, 1, 1])
    assert scores.tolist() == [0.5, 0.5, 0.5, 0.1, 0.9, 0.2]
    # tope 0.25 (1 de 4): 0.1, 0.2 y 0.5 marcan 4, 3 y 3 legítimas, no cumplen; 0.9 marca 0.
    assert fpr_threshold(scores, y, 0.25) == 0.9
    # tope 0.75 (3 de 4): el menor score observado que cumple es 0.2 (marca las 3 empatadas en 0.5)
    t = fpr_threshold(scores, y, 0.75)
    assert t == 0.2
    assert operating_point(scores, y, np.ones(6), t)["fpr"] == 0.75


def test_fpr_threshold_when_top_score_is_a_legit_tie():
    scores = np.array([0.7, 0.7, 0.7, 0.7, 0.2])
    y = np.array([0, 0, 0, 0, 1])
    # ni 0.7 cumple FPR <= 0.5: umbral que nadie alcanza
    assert fpr_threshold(scores, y, 0.5) == pytest.approx(0.70001)
    assert not (scores >= fpr_threshold(scores, y, 0.5)).any()


def test_choose_t_block():
    assert choose_t_block(S, Y, block_fpr_max=0.1) == 0.85


def test_cost_by_hand():
    decisions = decide(S, t_review=0.5, t_block=0.9)
    # BLOCK: legítima 0.9 y fraude 0.95. REVIEW (0.5 <= s < 0.9): legítimas 0.5 0.6 0.7 0.8 y fraudes 0.55 0.85.
    # APPROVE: sin fraudes. C = 0 + 3·(4+2) + 15·1
    assert cost(decisions, Y, AMOUNT, COSTS) == 3 * 6 + 15 * 1
    # Con t_review más alto el fraude 0.55 (monto 100) se aprueba: 100 + 3·(3+1) + 15
    assert cost(decide(S, 0.6, 0.9), Y, AMOUNT, COSTS) == 100 + 3 * 4 + 15


def test_no_model_cost_is_total_fraud_amount():
    assert no_model_cost(Y, AMOUNT) == 700


def test_choose_t_review_unconstrained_picks_cheapest():
    # t_block = 0.9. Revisar el fraude 0.55 (100) cuesta 3 por fila revisada; con tope de tasa holgado
    # hay que bajar a 0.55: revisan 0.55 .. 0.85 → 0.55, 0.6, 0.7, 0.8, 0.85 (5 filas = 15) frente a
    # aprobar el fraude de 100. Bajar a 0.5 añade una legítima (3) sin evitar nada: gana 0.55.
    t_review = choose_t_review(S, Y, AMOUNT, t_block=0.9, costs=COSTS, review_rate_max=1.0)
    assert t_review == 0.55
    assert cost(decide(S, t_review, 0.9), Y, AMOUNT, COSTS) == 3 * 5 + 15


def test_choose_t_review_respects_rate_cap_and_t_block():
    # Sin tope el óptimo es 0.55 (5 filas en REVIEW). Con tope 2/13 solo caben 2: candidatos 0.8 (2 filas:
    # C = 100 + 6 + 15 = 121), 0.85 (1 fila: C = 100 + 3 + 15 = 118) y 0.9 (C = 300 + 15). Gana 0.85.
    t_review = choose_t_review(S, Y, AMOUNT, t_block=0.9, costs=COSTS, review_rate_max=2 / 13)
    assert t_review == 0.85
    assert (decide(S, t_review, 0.9) == REVIEW).sum() == 1
    assert cost(decide(S, t_review, 0.9), Y, AMOUNT, COSTS) == 118
    # con tope 0 la banda de revisión queda vacía: t_review == t_block
    assert choose_t_review(S, Y, AMOUNT, 0.9, COSTS, review_rate_max=0.0) == 0.9


def test_choose_t_review_with_rounding_ties_and_expensive_review():
    # Si revisar es carísimo ya no compensa ni siquiera revisar el fraude de 100: t_review = t_block.
    costs = {"review": 1000, "block_false_positive": 15, "sensitivity": 0.5}
    assert choose_t_review(S, Y, AMOUNT, 0.9, costs, 1.0) == 0.9
    # Empates por redondeo: 0.500001 y 0.499999 son un mismo score (0.5) y no se pueden separar.
    scores = round_scores([0.500001, 0.499999, 0.1, 0.99])
    y = np.array([1, 0, 0, 1])
    amount = np.array([1000.0, 5.0, 5.0, 10.0])
    t = choose_t_review(scores, y, amount, t_block=0.99, costs=COSTS, review_rate_max=1.0)
    assert t == 0.5  # revisar el par empatado (2 filas = 6) evita aprobar el fraude de 1000
    assert (decide(scores, t, 0.99) == REVIEW).sum() == 2


def test_decision_summary_by_hand():
    summary = decision_summary(S, Y, AMOUNT, 0.5, 0.9, COSTS)
    assert summary["cost"] == 3 * 6 + 15
    assert summary["n_review"] == 6 and summary["n_blocked_legit"] == 1
    assert summary["review_rate"] == pytest.approx(6 / 13)
    assert summary["block_fpr"] == pytest.approx(1 / 10)
    assert summary["review_or_block_recall"] == 1.0
    assert summary["approved_fraud_amount"] == 0.0


def test_cost_sensitivity_grid_is_centered_on_nominal():
    grid = cost_sensitivity(S, Y, AMOUNT, 0.5, 0.9, COSTS)
    assert len(grid) == 9
    assert grid["review x1, block_fp x1"] == 3 * 6 + 15
    assert grid["review x0.5, block_fp x0.5"] == 1.5 * 6 + 7.5
    assert grid["review x1.5, block_fp x1.5"] == 4.5 * 6 + 22.5


# --- bootstrap por bloques -------------------------------------------------------------------------


def synthetic(n=6000, seed=1):
    rng = np.random.default_rng(seed)
    step = rng.integers(1, 101, n)
    y = (rng.random(n) < 0.05).astype(int)
    probabilities = np.clip(rng.normal(0.2 + 0.5 * y, 0.2), 0, 1)
    return step, y, probabilities, np.round(rng.lognormal(4, 1, n), 2)


def test_block_ids_count_and_partial_last_block():
    ids, n_blocks = block_ids(np.array([425, 448, 449, 550]), start=425, block_steps=24)
    assert ids.tolist() == [0, 0, 1, 5]
    assert n_blocks == 6  # 126 pasos → 5 bloques completos y uno de 6


def test_weighted_pr_auc_matches_scikit_learn():
    step, y, probabilities, _ = synthetic()
    ids, n_blocks = block_ids(step, 1, 24)
    metric = WeightedPRAUC(probabilities, y, ids)
    ones = np.ones(n_blocks, dtype=int)
    assert metric(ones) == pytest.approx(average_precision_score(y, probabilities), abs=1e-12)
    counts = np.random.default_rng(0).multinomial(n_blocks, np.full(n_blocks, 1 / n_blocks))
    expected = average_precision_score(y, probabilities, sample_weight=counts[ids])
    assert metric(counts) == pytest.approx(expected, abs=1e-12)


def test_resample_counts_are_deterministic_and_sum_to_n_blocks():
    a = resample_counts(8, 50, seed=42)
    b = resample_counts(8, 50, seed=42)
    c = resample_counts(8, 50, seed=43)
    assert np.array_equal(a, b)
    assert not np.array_equal(a, c)
    assert (a.sum(axis=1) == 8).all()


def test_bootstrap_is_deterministic_with_seed():
    step, y, probabilities, _ = synthetic()
    kwargs = dict(  # noqa: C408
        scores=probabilities,
        y=y,
        step=step,
        start=1,
        block_steps=10,
        n_resamples=200,
        seed=42,
        confidence=0.95,
    )
    first = bootstrap_pr_auc(**kwargs)
    assert bootstrap_pr_auc(**kwargs) == first
    assert bootstrap_pr_auc(**{**kwargs, "seed": 7}) != first
    assert first["n_blocks"] == 10 and first["low"] < first["high"]
    point = average_precision_score(y, probabilities)
    assert first["low"] <= point <= first["high"]


def test_bootstrap_metrics_deterministic_and_consistent():
    step, y, probabilities, amount = synthetic()
    scores = round_scores(probabilities)
    t_block = choose_t_block(scores, y, 0.01)
    t_fpr = fpr_threshold(scores, y, 0.05)
    t_review = choose_t_review(scores, y, amount, t_block, COSTS, 0.05)
    kwargs = dict(  # noqa: C408
        probabilities=probabilities, scores=scores, y=y, amount=amount, step=step, start=1, block_steps=10,
        operating_thresholds={"fpr_0.05": t_fpr}, t_review=t_review, t_block=t_block, costs=COSTS,
        n_resamples=300, seed=42, confidence=0.95,
    )  # fmt: skip
    first = bootstrap_metrics(**kwargs)
    assert bootstrap_metrics(**kwargs) == first
    point = operating_point(scores, y, amount, t_fpr)
    interval = first["metrics"]["recall@fpr_0.05"]
    assert interval["low"] <= point["recall"] <= interval["high"]
    nominal = decision_summary(scores, y, amount, t_review, t_block, COSTS)["cost"]
    assert first["metrics"]["cost"]["low"] <= nominal <= first["metrics"]["cost"]["high"]


@pytest.mark.parametrize("seed", range(5))
def test_thresholds_match_brute_force_with_rounding_ties(seed):
    rng = np.random.default_rng(seed)
    n = 400
    y = (rng.random(n) < 0.1).astype(int)
    scores = round_scores(
        np.clip(rng.normal(0.3 + 0.3 * y, 0.25), 0, 1), decimals=2
    )  # muchos empates
    amount = np.round(rng.lognormal(3, 1, n), 2)
    block_fpr_max, rate_max = 0.03, 0.08
    grid = np.unique(scores)

    feasible = [
        t for t in grid if ((scores >= t) & (y == 0)).sum() / (y == 0).sum() <= block_fpr_max
    ]
    t_block = choose_t_block(scores, y, block_fpr_max, decimals=2)
    assert t_block == min(feasible)

    best = None
    for t in [*grid[grid <= t_block], t_block]:
        decisions = decide(scores, t, t_block)
        if (decisions == REVIEW).mean() > rate_max:
            continue
        key = (cost(decisions, y, amount, COSTS), -t)
        best = key if best is None else min(best, key)
    t_review = choose_t_review(scores, y, amount, t_block, COSTS, rate_max)
    assert (cost(decide(scores, t_review, t_block), y, amount, COSTS), -t_review) == pytest.approx(
        best
    )
    assert t_review <= t_block
