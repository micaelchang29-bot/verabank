"""PSI (5.D): binning, fórmula, ventanas, alertas, job sobre PostgreSQL y referencia (D1, D2)."""

from __future__ import annotations

import hashlib
import json
import math
import uuid
from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from verabank_ml.monitor import binning, psi, reference

EPOCH = datetime(2026, 1, 1, tzinfo=UTC)
ARTIFACTS = reference.ARTIFACTS


def at_step(step: int) -> datetime:
    return EPOCH + timedelta(hours=step - 1)


# --- PSI y binning con ejemplos a mano ---------------------------------------------------------------------------


def test_identical_distributions_give_zero():
    assert binning.psi([0.2, 0.3, 0.5], [0.2, 0.3, 0.5]) == 0.0


def test_known_shift():
    # (0,75 − 0,5)·ln(1,5) + (0,25 − 0,5)·ln(0,5)
    expected = 0.25 * math.log(1.5) + 0.25 * math.log(2)
    assert binning.psi([0.5, 0.5], [0.75, 0.25]) == pytest.approx(expected, abs=1e-12)
    assert expected == pytest.approx(0.274653, abs=1e-6)


def test_empty_bin_is_floored_at_1e4_without_renormalising():
    bins = binning.fit_bins([0, 1] * 50)
    actual = binning.proportions(bins, [0] * 10)
    assert list(actual) == [1.0, 1e-4, 1e-4]           # 0, 1, «otro»: suma > 1, sin renormalizar
    e = binning.proportions(bins, [0, 1] * 50)
    hand = (1 - 0.5) * math.log(1 / 0.5) + (1e-4 - 0.5) * math.log(1e-4 / 0.5) + 0.0
    assert binning.psi(e, actual) == pytest.approx(hand, abs=1e-12)


def test_repeated_quantile_edges_are_merged():
    train = [0.0] * 80 + list(range(1, 21))             # 21 valores distintos: deciles
    bins = binning.fit_bins(train)
    assert bins.kind == "quantile"
    # Cuantil lineal: posición 99·q en los 100 valores ordenados (80 ceros, luego 1..20). q = 0,1…0,7 → 0;
    # q = 0,8 → posición 79,2 → 0 + 0,2·(1 − 0) = 0,2; q = 0,9 → 89,1 → 10 + 0,1·(11 − 10) = 10,1.
    assert bins.edges == (0.0, 0.2, 10.1)
    assert bins.n_bins == 4 and not bins.degenerate


def test_quantile_edges_by_hand():
    train = list(range(1, 12))                           # 1..11: cuantil q = 1 + 10·q
    bins = binning.fit_bins(train)
    assert bins.edges == tuple(float(1 + q) for q in range(1, 10))
    # (borde[i-1], borde[i]]: 2 va al bin 0, 2,0000001 al bin 1, 11 al último.
    assert list(binning.assign(bins, [1, 2, 2.0000001, 10, 10.5, 11])) == [0, 0, 1, 8, 9, 9]


def test_constant_feature_is_degenerate_and_scores_zero():
    bins = binning.fit_bins([3.0] * 50)
    assert bins.degenerate
    ref = {"top_features": [], "features": {"c": {**bins.to_json(), "expected": list(binning.proportions(bins, [3.0] * 50))}},
           "score": quantile_spec(np.linspace(0, 1, 100))}
    metrics = psi.compute_window(ref, EPOCH, EPOCH + psi.WINDOW, [0.5] * 300, [{"c": 9.0}] * 300, "m/1")
    c = next(m for m in metrics if m.feature == "c")
    assert c.degenerate and c.psi == 0.0


def test_few_values_with_an_unseen_value_go_to_other():
    bins = binning.fit_bins([0, 1, 2, 2, 1, 0])
    assert bins.kind == "categorical" and bins.values == (0.0, 1.0, 2.0) and bins.n_bins == 4
    assert list(binning.assign(bins, [0, 2, 5, -1])) == [0, 2, 3, 3]
    assert list(binning.proportions(bins, [5, 5, 0, 1])) == [0.25, 0.25, 1e-4, 0.5]


def test_values_are_rounded_before_binning():
    """El seno de la hora en Java puede diferir en el último bit: cae en el mismo bin."""
    bins = binning.fit_bins(np.linspace(-1, 1, 100))
    edge = bins.edges[3]
    assert binning.assign(bins, [edge + 1e-15])[0] == binning.assign(bins, [edge])[0]


# --- ventanas y alertas --------------------------------------------------------------------------------------------


def test_window_alignment_at_576_577_and_600_601():
    assert psi.window_index(EPOCH, at_step(576)) == 23
    assert psi.window_index(EPOCH, at_step(577)) == 24
    assert psi.window_index(EPOCH, at_step(600)) == 24
    assert psi.window_index(EPOCH, at_step(601)) == 25
    assert psi.window_steps(24) == (577, 600)
    assert psi.window_bounds(EPOCH, 24) == (at_step(577), at_step(601))


def quantile_spec(values) -> dict:
    bins = binning.fit_bins(values)
    return {**bins.to_json(), "expected": [float(p) for p in binning.proportions(bins, values)]}


@pytest.fixture
def ref():
    rng = np.random.default_rng(7)
    train = {"a": rng.normal(0, 1, 5000), "b": rng.normal(0, 1, 5000), "kind": rng.integers(0, 3, 5000).astype(float)}
    return {"top_features": ["a", "kind"], "features": {k: quantile_spec(v) for k, v in train.items()},
            "score": quantile_spec(rng.beta(1, 20, 5000))}


def window(ref, k: int, shift: float, n: int = 400, seed: int = 0):
    """Una ventana: ``shift`` desplaza a, b y el score (0 = igual que entrenamiento)."""
    rng = np.random.default_rng(seed + k)
    feats = [{"a": float(x + shift), "b": float(y + shift), "kind": float(z)}
             for x, y, z in zip(rng.normal(0, 1, n), rng.normal(0, 1, n), rng.integers(0, 3, n), strict=True)]
    scores = list(np.clip(rng.beta(1, 20, n) + shift / 4, 0, 1))
    return (k, scores, feats, "m/1")


def alerts(done) -> set[tuple[int, str]]:
    return {(k, m.feature) for k, ms in done.items() for m in ms if m.alert}


def test_a_single_window_over_the_threshold_does_not_alert(ref):
    done = psi.evaluate(ref, EPOCH, [window(ref, 24, 0), window(ref, 25, 3), window(ref, 26, 0)], {})
    by = {m.feature: m for m in done[25]}
    assert by["a"].psi > 0.25 and by["score"].psi > 0.25
    assert alerts(done) == set()


def test_two_contiguous_windows_alert_only_score_and_top_features(ref):
    done = psi.evaluate(ref, EPOCH, [window(ref, 24, 0), window(ref, 25, 3), window(ref, 26, 3)], {})
    by = {m.feature: m for m in done[26]}
    assert by["b"].psi > 0.25 and not by["b"].alert and not by["b"].is_top_feature   # fuera del top: sin alerta
    assert alerts(done) == {(26, "a"), (26, "score")}                                 # kind no se desplazó


def test_two_high_windows_separated_by_a_low_one_do_not_alert(ref):
    done = psi.evaluate(ref, EPOCH, [window(ref, 24, 3), window(ref, 25, 0), window(ref, 26, 3)], {})
    assert alerts(done) == set()


def test_the_previous_window_can_come_from_the_table(ref):
    first = psi.evaluate(ref, EPOCH, [window(ref, 25, 3)], {})
    done = psi.evaluate(ref, EPOCH, [window(ref, 26, 3)], {25: {m.feature: m for m in first[25]}})
    assert alerts(done) == {(26, "a"), (26, "score")}


def test_under_200_rows_is_insufficient_and_never_alerts(ref):
    done = psi.evaluate(ref, EPOCH, [window(ref, 25, 3, n=199), window(ref, 26, 3)], {})
    assert all(m.insufficient and m.n_current == 199 for m in done[25])
    assert not any(m.insufficient for m in done[26])
    assert alerts(done) == set()                          # la anterior es insuficiente
    done = psi.evaluate(ref, EPOCH, [window(ref, 25, 3), window(ref, 26, 3, n=150)], {})
    assert alerts(done) == set()


# --- job sobre PostgreSQL ------------------------------------------------------------------------------------------


def insert_rows(pg, rows):
    with pg.connect("admin") as conn, conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO risk.scoring_log (transfer_id, source_ref, dest_ref, amount, occurred_at, features, score, "
            "decision, model_version) VALUES (%s, 's', 'd', 1, %s, %s, %s, 'APPROVE', %s)",
            [(uuid.uuid4(), at, json.dumps(f), round(s, 5), v) for at, f, s, v in rows])


def rows_for(ref, k: int, shift: float, n: int = 400, version="m/1", seed=0):
    _, scores, feats, _ = window(ref, k, shift, n, seed)
    start = psi.window_bounds(EPOCH, k)[0]
    return [(start + timedelta(hours=i % 24), f, s, version) for i, (f, s) in enumerate(zip(feats, scores, strict=True))]


def independent_psi(spec: dict, values) -> float:
    """Implementación de la prueba: numpy puro, sin el código del job."""
    x = np.round(np.asarray(values, dtype=float), 9)
    if spec["kind"] == "quantile":
        idx = np.digitize(x, np.array(spec["edges"]), right=True)
    else:
        lookup = {v: i for i, v in enumerate(spec["values"])}
        idx = np.array([lookup.get(v, len(spec["values"])) for v in x])
    a = np.maximum(np.bincount(idx, minlength=spec["n_bins"]) / len(x), 1e-4)
    e = np.maximum(np.array(spec["expected"]), 1e-4)
    return float(np.sum((a - e) * np.log(a / e)))


def stored(pg):
    with pg.connect("admin") as conn:
        return conn.execute("SELECT window_start, feature, psi, n_current, n_bins, degenerate, insufficient, "
                            "is_top_feature, alert, model_version FROM analytics.drift_metrics ORDER BY 1, 2").fetchall()


def test_job_matches_an_independent_implementation_and_skips_warmup_and_incomplete(pg, ref):
    pg.reset()
    w24 = rows_for(ref, 24, 0.7, seed=1)
    insert_rows(pg, w24 + rows_for(ref, 25, 0.7, seed=2) + rows_for(ref, 26, 0, n=50, seed=3))
    warm = [(at_step(580), {"a": 99.0, "b": 99.0, "kind": 9.0}, 0.99, "warmup")] * 0
    insert_rows(pg, [(at_step(580 + i % 20), {"a": 50.0, "b": 50.0, "kind": 7.0}, 0.9, "warmup") for i in range(300)] + warm)
    with pg.connect() as conn:                           # rol ml_job
        done = psi.run(conn, ref, EPOCH)
    assert sorted(done) == [24, 25]                      # la 26 (con el máximo) está incompleta
    by = {m.feature: m for m in done[24]}
    assert by["score"].n_current == 400                  # las 300 de warmup no cuentan
    for name, spec in [*ref["features"].items(), ("score", ref["score"])]:
        values = [s for _, _, s, _ in w24] if name == "score" else [f[name] for _, f, _, _ in w24]
        expected = independent_psi(spec, np.round(values, 5) if name == "score" else values)
        assert by[name].psi == pytest.approx(expected, abs=1e-9), name
    db = {(r[0], r[1]): float(r[2]) for r in stored(pg)}
    assert db[(psi.window_bounds(EPOCH, 24)[0], "a")] == pytest.approx(by["a"].psi, abs=1e-6)
    assert alerts(done) == {(25, "a"), (25, "score")}


def test_k6_synthetic_rows_are_excluded(pg, ref):
    """make perf-scoring escribe filas con source_ref «perf-…» y sin transferencia (docs/resultados/fase-4.md)."""
    pg.reset()
    insert_rows(pg, rows_for(ref, 24, 0) + rows_for(ref, 25, 0, n=5))
    with pg.connect("admin") as conn:
        conn.execute("INSERT INTO risk.scoring_log (transfer_id, source_ref, dest_ref, amount, occurred_at, features, "
                     "score, decision, model_version) SELECT gen_random_uuid(), 'perf-src-0001', 'perf-dst-0001', 1, %s, "
                     "%s, 0.9, 'BLOCK', 'm/1' FROM generate_series(1, 500)",
                     (at_step(2000), json.dumps({"a": 9.0, "b": 9.0, "kind": 1.0})))
        conn.execute("INSERT INTO risk.scoring_log (transfer_id, source_ref, dest_ref, amount, occurred_at, features, "
                     "score, decision, model_version) SELECT gen_random_uuid(), 'perf-src-0002', 'd', 1, %s, %s, 0.9, "
                     "'BLOCK', 'm/1' FROM generate_series(1, 300)", (at_step(590), json.dumps({"a": 9.0, "b": 9.0, "kind": 1.0})))
    with pg.connect() as conn:
        done = psi.run(conn, ref, EPOCH)
    assert sorted(done) == [24]                         # sin las de k6 el máximo está en la 25: solo la 24 completa
    assert next(m for m in done[24] if m.feature == "score").n_current == 400


def test_job_is_idempotent_and_recompute_rewrites_the_same_rows(pg, ref):
    pg.reset()
    insert_rows(pg, rows_for(ref, 24, 0) + rows_for(ref, 25, 1) + rows_for(ref, 26, 0, n=10))
    with pg.connect() as conn:
        first = psi.run(conn, ref, EPOCH)
        before = stored(pg)
        again = psi.run(conn, ref, EPOCH)
        assert again == {}                                # nada pendiente
        assert stored(pg) == before
        psi.run(conn, ref, EPOCH, recompute=True)
    assert stored(pg) == before and len(before) == 2 * (len(ref["features"]) + 1) and sorted(first) == [24, 25]


def test_a_new_window_reads_the_previous_one_from_the_table(pg, ref):
    pg.reset()
    insert_rows(pg, rows_for(ref, 24, 3, seed=5) + rows_for(ref, 25, 0, n=5))
    with pg.connect() as conn:
        assert alerts(psi.run(conn, ref, EPOCH)) == set()
    insert_rows(pg, rows_for(ref, 25, 3, seed=6) + rows_for(ref, 26, 0, n=5))
    with pg.connect() as conn:
        done = psi.run(conn, ref, EPOCH)
    assert sorted(done) == [25] and alerts(done) == {(25, "a"), (25, "score")}


@pytest.fixture
def shifted_ref():
    """Una referencia calibrada sobre datos desplazados 3 (el estado de a, b y score de ``window(..., 3)``)."""
    rng = np.random.default_rng(8)
    train = {"a": rng.normal(3, 1, 5000), "b": rng.normal(3, 1, 5000), "kind": rng.integers(0, 3, 5000).astype(float)}
    return {"top_features": ["a", "kind"], "features": {k: quantile_spec(v) for k, v in train.items()},
            "score": quantile_spec(np.clip(rng.beta(1, 20, 5000) + 0.75, 0, 1))}


def reference_rows(pg):
    with pg.connect("admin") as conn:
        return conn.execute("SELECT reference, count(*), count(*) FILTER (WHERE alert) FROM analytics.drift_metrics "
                            "GROUP BY 1 ORDER BY 1").fetchall()


def test_rows_of_both_references_live_together(pg, ref, shifted_ref):
    pg.reset()
    insert_rows(pg, rows_for(ref, 24, 3, seed=5) + rows_for(ref, 25, 3, seed=6) + rows_for(ref, 26, 0, n=5))
    train = {**ref, "partition": "train"}
    valid = {**shifted_ref, "partition": "valid"}
    with pg.connect() as conn:
        done_train = psi.run(conn, train, EPOCH)
        done_valid = psi.run(conn, valid, EPOCH)
        assert psi.run(conn, train, EPOCH) == {} and psi.run(conn, valid, EPOCH) == {}   # cada una tiene sus ventanas
    n = len(ref["features"]) + 1
    assert reference_rows(pg) == [("train", 2 * n, 2), ("valid", 2 * n, 0)]
    assert {m.reference for ms in done_train.values() for m in ms} == {"train"}
    assert {m.reference for ms in done_valid.values() for m in ms} == {"valid"}
    assert alerts(done_train) == {(25, "a"), (25, "score")} and alerts(done_valid) == set()
    with pg.connect() as conn:
        # La misma ventana y feature con dos referencias son filas distintas con PSI distinto.
        rows = conn.execute("SELECT reference, psi FROM analytics.drift_metrics WHERE feature = 'a' AND window_start = %s "
                            "ORDER BY 1", (psi.window_bounds(EPOCH, 24)[0],)).fetchall()
    assert rows[0][0] == "train" and rows[1][0] == "valid" and float(rows[0][1]) > 0.25 > float(rows[1][1])


@pytest.mark.parametrize("hi_name, lo_name", [("train", "valid"), ("valid", "train")])
def test_the_alert_reads_the_previous_window_of_the_same_reference_only(pg, ref, shifted_ref, hi_name, lo_name):
    """Con ``hi`` la ventana 24 ya estaba alta; con ``lo`` (calibrada sobre esos datos) no: ``lo`` no puede alertar en la 25."""
    pg.reset()
    hi, lo = {**ref, "partition": hi_name}, {**shifted_ref, "partition": lo_name}
    insert_rows(pg, rows_for(ref, 24, 3, seed=5) + rows_for(ref, 25, 0, n=5))
    with pg.connect() as conn:
        psi.run(conn, lo, EPOCH)                      # lo primero, hi después: las filas de hi son las últimas de la tabla
        psi.run(conn, hi, EPOCH)
    insert_rows(pg, rows_for(ref, 25, 6, seed=7) + rows_for(ref, 26, 0, n=5))
    with pg.connect() as conn:
        done_lo = psi.run(conn, lo, EPOCH)
        done_hi = psi.run(conn, hi, EPOCH)
    assert {m.feature: m.psi for m in done_lo[25]}["a"] > 0.25            # alta en la 25, pero la anterior de lo era baja
    assert alerts(done_lo) == set()
    assert alerts(done_hi) == {(25, "a"), (25, "score")}
    assert reference_rows(pg)[0][0] == "train" and {r[0]: r[2] for r in reference_rows(pg)}[lo_name] == 0


def test_psi_command_writes_the_chosen_reference(pg, ref, shifted_ref, tmp_path, monkeypatch, capsys):
    pg.reset()
    paths = {"train": tmp_path / "t.json", "valid": tmp_path / "v.json"}
    paths["train"].write_text(json.dumps({**ref, "partition": "train"}))
    paths["valid"].write_text(json.dumps({**shifted_ref, "partition": "valid"}))
    insert_rows(pg, rows_for(ref, 24, 3) + rows_for(ref, 25, 0, n=5))
    monkeypatch.setattr(psi, "REFERENCE_PATHS", paths)
    monkeypatch.setenv("VERABANK_ML_DSN", pg.dsn())
    assert psi.psi_command([]) == 0                                    # por defecto: train
    assert [r[0] for r in reference_rows(pg)] == ["train"]
    assert psi.psi_command(["--reference", "valid"]) == 0
    assert [r[0] for r in reference_rows(pg)] == ["train", "valid"]
    assert "referencia: valid (v.json)" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        psi.psi_command(["--reference", "test"])
    paths["valid"].unlink()
    with pytest.raises(SystemExit, match="falta v.json"):
        psi.psi_command(["--reference", "valid"])


def test_ml_job_cannot_read_core(pg):
    import psycopg

    with pg.connect() as conn, pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute("SELECT * FROM core.accounts")


# --- referencia ----------------------------------------------------------------------------------------------------


class Model:
    def predict_proba(self, X):
        p = 1 / (1 + np.exp(-(X[:, 0] - 2)))
        return np.column_stack([1 - p, p])

    class _Step:
        coef_ = np.array([[0.5, -2.0, 0.1]])

    def __getitem__(self, i):
        return self._Step()


def synthetic_train(n=3000, seed=3):
    rng = np.random.default_rng(seed)
    return pd.DataFrame({"step": rng.integers(1, 401, n), "partition": "train", "x": rng.normal(0, 1, n),
                         "y": rng.integers(0, 4, n).astype(float), "z": np.zeros(n)})


def test_reference_is_deterministic_and_aggregate_only():
    thresholds = {"feature_order": ["x", "y", "z"], "model_version": "m/1", "model_sha256": "abc", "feature_set": "A"}
    a = reference.build_reference(synthetic_train(), Model(), "logreg", thresholds, seed=42)
    b = reference.build_reference(synthetic_train(), Model(), "logreg", thresholds, seed=42)
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)
    assert a["top_features"] == ["y", "x", "z"] and a["features"]["z"]["degenerate"]
    assert a["features"]["y"]["kind"] == "categorical" and a["features"]["x"]["n_bins"] == 10
    assert set(a) >= {"model_sha256", "top_features", "features", "score"}
    assert "rows" not in json.dumps(a) and "occurred_at" not in json.dumps(a)


def test_top_features_break_ties_by_name():
    assert reference.top_features({"b": 1.0, "a": 1.0, "c": 3.0, "d": 0.5, "e": 0.1, "f": 0.0}) == ["c", "a", "b", "d", "e"]


def synthetic_valid(n=2000, seed=11):
    """Otro régimen: x desplazada y más dispersa, y con otra mezcla."""
    rng = np.random.default_rng(seed)
    return pd.DataFrame({"step": rng.integers(425, 551, n), "partition": "valid", "x": rng.normal(1.5, 2.0, n),
                         "y": rng.integers(0, 3, n).astype(float), "z": np.zeros(n)})


def independent_bins(values):
    """Bins y proporciones con Python puro (cuantil lineal a mano, bisect), sin el código de binning."""
    from bisect import bisect_left

    x = sorted(round(float(v), 9) for v in values)
    n = len(x)
    if len(set(x)) <= 10:
        keys = sorted(set(x))
        idx = [keys.index(v) for v in x]
        k = len(keys) + 1
    else:
        edges = []
        for i in range(1, 10):
            pos = (n - 1) * i / 10
            lo = int(pos)
            hi = min(lo + 1, n - 1)
            edges.append(round(x[lo] + (x[hi] - x[lo]) * (pos - lo), 9))
        keys = sorted(set(edges))
        idx = [bisect_left(keys, v) for v in x]
        k = len(keys) + 1
    return keys, [max(idx.count(i) / n, 1e-4) for i in range(k)]


THRESHOLDS = {"feature_order": ["x", "y", "z"], "model_version": "m/1", "model_sha256": "abc", "feature_set": "A"}


def test_validation_reference_bins_match_an_independent_implementation():
    train = reference.build_reference(synthetic_train(), Model(), "logreg", THRESHOLDS, seed=42)
    valid_df = synthetic_valid()
    valid = reference.build_reference(valid_df, Model(), "logreg", THRESHOLDS, seed=42, partition="valid", inherited=train)
    assert valid["partition"] == "valid" and valid["valid_steps"] == [int(valid_df["step"].min()), int(valid_df["step"].max())]
    assert valid["n_valid"] == len(valid_df) and "train_steps" not in valid
    for name in ("x", "y", "z"):
        keys, expected = independent_bins(valid_df[name])
        spec = valid["features"][name]
        assert list(spec.get("edges", spec.get("values"))) == pytest.approx(keys, abs=2e-9), name   # el 9.º decimal puede empatar
        assert spec["expected"] == pytest.approx(expected, abs=1e-12), name
        if name != "z":                                    # z es constante en ambas
            assert spec.get("edges", spec.get("values")) != train["features"][name].get("edges", train["features"][name].get("values")), name
    score = np.round(Model().predict_proba(valid_df[["x", "y", "z"]].to_numpy())[:, 1], 5)
    keys, expected = independent_bins(score)
    assert list(valid["score"]["edges"]) == pytest.approx(keys, abs=2e-9) and valid["score"]["expected"] == pytest.approx(expected, abs=1e-12)
    assert valid["top_features"] == train["top_features"] and valid["importance"] == train["importance"]


def test_validation_reference_is_deterministic_and_aggregate_only():
    train = reference.build_reference(synthetic_train(), Model(), "logreg", THRESHOLDS, seed=42)
    a = reference.build_reference(synthetic_valid(), Model(), "logreg", THRESHOLDS, seed=42, partition="valid", inherited=train)
    b = reference.build_reference(synthetic_valid(), Model(), "logreg", THRESHOLDS, seed=42, partition="valid", inherited=train)
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)
    assert "rows" not in json.dumps(a) and "occurred_at" not in json.dumps(a)


def test_each_partition_writes_its_own_file_and_manifest_entry(tmp_path, monkeypatch):
    artifacts = tmp_path
    (artifacts / "manifest.json").write_text(json.dumps({"artifacts": {}}))
    monkeypatch.setattr(reference, "REFERENCE_PATHS", {"train": artifacts / "psi_reference.json",
                                                      "valid": artifacts / "psi_reference_valid.json"})
    monkeypatch.setattr(reference, "MANIFEST_PATH", artifacts / "manifest.json")
    train = reference.build_reference(synthetic_train(), Model(), "logreg", THRESHOLDS, seed=42)
    valid = reference.build_reference(synthetic_valid(), Model(), "logreg", THRESHOLDS, seed=42, partition="valid", inherited=train)
    digests = [reference.write_reference(train), reference.write_reference(valid)]
    manifest = json.loads((artifacts / "manifest.json").read_text())["artifacts"]
    assert {k: v["sha256"] for k, v in manifest.items()} == dict(zip(["psi_reference.json", "psi_reference_valid.json"], digests, strict=True))
    assert reference.write_reference(valid) == digests[1]                      # segunda vez: idéntico
    assert json.loads((artifacts / "psi_reference_valid.json").read_text())["partition"] == "valid"


@pytest.mark.parametrize("partition, argv", [("train", []), ("train", ["--partition", "train"]),
                                             ("valid", ["--partition", "valid"])])
def test_psi_reference_command_reads_only_its_partition(monkeypatch, partition, argv):
    seen = {}

    def fake_load(feature_set, partitions, columns=None, allow_test=False):
        seen["partitions"] = partitions
        raise RuntimeError("detener aquí")

    monkeypatch.setattr(reference.modeling, "load_features", fake_load)
    monkeypatch.setattr(reference.tracking, "setup", lambda cfg: None)
    monkeypatch.setattr(reference, "THRESHOLDS_PATH", ARTIFACTS / "thresholds.json")
    thresholds = json.loads((ARTIFACTS / "thresholds.json").read_text())
    monkeypatch.setattr("verabank_ml.export.load_alias",
                        lambda client, cfg, alias: ("xgb", *thresholds["model_version"].split("/"), None))
    with pytest.raises(RuntimeError, match="detener aquí"):
        reference.psi_reference_command(argv)
    assert seen["partitions"] == (partition,)


def test_psi_reference_command_rejects_an_unknown_partition():
    with pytest.raises(SystemExit):
        reference.psi_reference_command(["--partition", "test"])


def test_artifact_reference_belongs_to_the_current_champion_and_is_in_the_manifest():
    """Si cambia el champion hay que regenerar psi_reference.json (make ml-psi-ref)."""
    ref = json.loads((ARTIFACTS / "psi_reference.json").read_text())
    thresholds = json.loads((ARTIFACTS / "thresholds.json").read_text())
    manifest = json.loads((ARTIFACTS / "manifest.json").read_text())
    assert ref["model_sha256"] == thresholds["model_sha256"] and ref["model_version"] == thresholds["model_version"]
    entry = manifest["artifacts"]["psi_reference.json"]
    assert entry["sha256"] == hashlib.sha256((ARTIFACTS / "psi_reference.json").read_bytes()).hexdigest()
    assert entry["model_sha256"] == thresholds["model_sha256"]
    assert list(ref["features"]) == thresholds["feature_order"] and len(ref["top_features"]) == 5
    assert ref["partition"] == "train" and ref["train_steps"][1] <= 400
    for spec in [*ref["features"].values(), ref["score"]]:
        assert len(spec["expected"]) == spec["n_bins"] and min(spec["expected"]) >= 1e-4


def test_validation_reference_is_in_the_manifest_next_to_the_training_one():
    train = json.loads((ARTIFACTS / "psi_reference.json").read_text())
    valid = json.loads((ARTIFACTS / "psi_reference_valid.json").read_text())
    thresholds = json.loads((ARTIFACTS / "thresholds.json").read_text())
    manifest = json.loads((ARTIFACTS / "manifest.json").read_text())["artifacts"]
    for name in ("psi_reference.json", "psi_reference_valid.json"):
        entry = manifest[name]
        assert entry["sha256"] == hashlib.sha256((ARTIFACTS / name).read_bytes()).hexdigest(), name
        assert entry["model_sha256"] == thresholds["model_sha256"], name
    assert manifest["psi_reference.json"]["sha256"] != manifest["psi_reference_valid.json"]["sha256"]
    assert valid["partition"] == "valid" and 425 <= valid["valid_steps"][0] and valid["valid_steps"][1] <= 550
    assert "train_steps" not in valid and "n_train" not in valid
    assert valid["model_sha256"] == train["model_sha256"] and valid["top_features"] == train["top_features"]
    assert list(valid["features"]) == list(train["features"])
    assert valid["score"]["expected"] != train["score"]["expected"]       # otra partición, otros bins
    for spec in [*valid["features"].values(), valid["score"]]:
        assert len(spec["expected"]) == spec["n_bins"] and min(spec["expected"]) >= 1e-4


# --- humo de la CI (D11) ----------------------------------------------------------------------------------------


def test_ci_smoke_runs_the_pipeline_on_a_synthetic_sample(tmp_path):
    from verabank_ml import smoke

    sample = smoke.synthetic_paysim(200, seed=1)
    assert list(sample.columns) == smoke.COLUMNS and sample["step"].is_monotonic_increasing
    result = smoke.run(20_000, tmp_path)
    assert set(result["models"]) == {"logreg", "xgb"}
    for m in result["models"].values():
        assert m["onnx_parity"] < 1e-5 and 0 <= m["t_review"] <= m["t_block"] <= 1
    assert (tmp_path / "xgb.onnx").exists() and not (tmp_path / "manifest.json").exists()
