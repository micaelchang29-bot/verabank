"""Razones de fraude (5.E, §5e, D6): explain para ambos modelos, orden de las razones y job sobre PostgreSQL."""

from __future__ import annotations

import json
import uuid

import numpy as np
import pytest
import xgboost as xgb
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler
from xgboost import XGBClassifier

from verabank_ml import reasons

NAMES = ["a", "b", "c", "d"]


@pytest.fixture(scope="module")
def logreg():
    X = np.array([[0, 0, 1, 2], [2, 4, 1, 0], [4, 8, 1, 4], [6, 4, 3, 2]], dtype=float)
    y = np.array([0, 0, 1, 1])
    return Pipeline([("scaler", StandardScaler()), ("lr", LogisticRegression(C=1.0))]).fit(X, y)


@pytest.fixture(scope="module")
def booster():
    rng = np.random.default_rng(0)
    X = rng.normal(size=(800, 4)).astype(np.float32)
    y = ((X[:, 0] - 0.7 * X[:, 2] + rng.normal(0, 0.3, 800)) > 0.8).astype(int)
    return XGBClassifier(n_estimators=30, max_depth=3, tree_method="hist", random_state=0).fit(X, y), X


def test_logistic_contributions_by_hand(logreg):
    scaler, lr = logreg[0], logreg[-1]
    # Medias y desviaciones de entrenamiento: a 3, √5; b 4, √8; c 1,5, √0,75; d 2, √2.
    assert scaler.mean_ == pytest.approx([3, 4, 1.5, 2])
    assert scaler.scale_ == pytest.approx([5 ** 0.5, 8 ** 0.5, 0.75 ** 0.5, 2 ** 0.5])
    x = np.array([[5.0, 0.0, 3.0, 2.0]])
    z = np.array([2 / 5 ** 0.5, -4 / 8 ** 0.5, 1.5 / 0.75 ** 0.5, 0.0])
    hand = z * lr.coef_.ravel()
    got = reasons.explain(logreg, x)[0]
    assert got == pytest.approx(hand, abs=1e-6)
    assert got[3] == 0.0                                       # en la media: contribución nula
    # La suma de contribuciones más el intercepto es el margen del modelo.
    assert got.sum() + lr.intercept_[0] == pytest.approx(logreg.decision_function(x)[0], abs=1e-6)


def test_xgboost_shap_is_additive_to_the_margin(booster):
    model, X = booster
    contrib = reasons.explain(model, X[:50])
    margin = model.get_booster().predict(xgb.DMatrix(X[:50]), output_margin=True)
    assert np.abs(contrib.sum(axis=1) + reasons.expected_value(model) - margin).max() < 1e-5


def test_three_distinct_reasons_ordered_by_absolute_contribution(booster):
    model, X = booster
    for row in reasons.top_reasons(reasons.explain(model, X[:100]), NAMES):
        assert [r[0] for r in row] == [1, 2, 3]
        assert len({r[1] for r in row}) == 3
        mags = [abs(r[2]) for r in row]
        assert mags == sorted(mags, reverse=True)


def test_a_large_negative_contribution_ranks_first_with_its_sign():
    rows = reasons.top_reasons(np.array([[0.2, -0.9, 0.5, 0.05]]), NAMES)
    assert rows == [[(1, "b", -0.9), (2, "c", 0.5), (3, "a", 0.2)]]


def test_ties_are_broken_by_feature_name():
    assert reasons.top_reasons(np.array([[0.3, -0.3, 0.3, 0.1]]), ["d", "c", "b", "a"]) == [
        [(1, "b", 0.3), (2, "c", -0.3), (3, "d", 0.3)]]


# --- job sobre PostgreSQL ------------------------------------------------------------------------------------------


def insert(pg, rows):
    with pg.connect("admin") as conn, conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO risk.scoring_log (transfer_id, source_ref, dest_ref, amount, occurred_at, features, score, "
            "decision, model_version) VALUES (%s, 's', 'd', 1, now(), %s, 0.5, %s, %s)",
            [(tid, json.dumps(dict(zip(NAMES, x, strict=True))), d, v) for tid, x, d, v in rows])


def stored(pg):
    with pg.connect("admin") as conn:
        return conn.execute("SELECT transfer_id, rank, feature, contribution, model_version FROM analytics.fraud_reasons "
                            "ORDER BY 1, 2").fetchall()


def test_job_explains_only_model_review_and_block_and_is_idempotent(pg, booster):
    model, X = booster
    pg.reset()
    ids = {k: uuid.uuid4() for k in ("review", "block", "approve", "warmup", "other")}
    insert(pg, [(ids["review"], X[0].tolist(), "REVIEW", "m/1"), (ids["block"], X[1].tolist(), "BLOCK", "m/1"),
                (ids["approve"], X[2].tolist(), "APPROVE", "m/1"), (ids["warmup"], X[3].tolist(), "REVIEW", "warmup"),
                (ids["other"], X[4].tolist(), "BLOCK", "m/0")])
    with pg.connect() as conn:                                  # rol ml_job
        first = reasons.run(conn, model, "m/1", NAMES)
    assert first == {"explained": {"BLOCK": 1, "REVIEW": 1}, "other_model_skipped": 1}
    rows = stored(pg)
    assert {r[0] for r in rows} == {ids["review"], ids["block"]} and len(rows) == 6
    expected = reasons.top_reasons(reasons.explain(model, X[:2]), NAMES)
    assert [(r[1], r[2]) for r in rows if r[0] == ids["review"]] == [(k, f) for k, f, _ in expected[0]]
    assert all(r[4] == "m/1" for r in rows)
    with pg.connect() as conn:
        assert reasons.run(conn, model, "m/1", NAMES)["explained"] == {}
    assert stored(pg) == rows


def test_limit_and_a_new_model_version(pg, booster):
    model, X = booster
    pg.reset()
    insert(pg, [(uuid.uuid4(), X[i].tolist(), "REVIEW", "m/2") for i in range(7)])
    with pg.connect() as conn:
        assert reasons.run(conn, model, "m/2", NAMES, limit=3)["explained"] == {"REVIEW": 3}
        assert reasons.run(conn, model, "m/2", NAMES)["explained"] == {"REVIEW": 4}
    assert len(stored(pg)) == 21


def test_ml_job_cannot_read_core_or_write_scoring_log(pg):
    import psycopg

    with pg.connect() as conn:
        for sql in ("SELECT * FROM core.accounts", "UPDATE risk.scoring_log SET score = 0"):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(sql)
