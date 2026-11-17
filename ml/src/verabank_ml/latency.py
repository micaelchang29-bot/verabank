"""Latencia de inferencia con ONNX Runtime: CPU, un hilo, una fila por llamada."""

import time

import numpy as np

from verabank_ml.onnx_export import INPUT_NAME, load_session, predict_positive_proba


def measure_latency(onnx_bytes: bytes, rows: np.ndarray, calls: int, warmup: int) -> dict:
    """Tiempo de `session.run` con una fila (1, n_features) por llamada, tras `warmup` llamadas."""
    session = load_session(onnx_bytes)
    rows = np.ascontiguousarray(rows, dtype=np.float32)
    singles = [rows[i % len(rows)][None, :] for i in range(calls + warmup)]
    for row in singles[:warmup]:
        session.run(None, {INPUT_NAME: row})
    samples = np.empty(calls)
    for i, row in enumerate(singles[warmup:]):
        start = time.perf_counter_ns()
        session.run(None, {INPUT_NAME: row})
        samples[i] = (time.perf_counter_ns() - start) / 1e6
    return {
        "calls": calls,
        "p50_ms": float(np.percentile(samples, 50)),
        "p95_ms": float(np.percentile(samples, 95)),
        "p99_ms": float(np.percentile(samples, 99)),
        "max_ms": float(samples.max()),
    }


def onnx_parity(onnx_bytes: bytes, rows: np.ndarray, expected: np.ndarray) -> float:
    """Diferencia máxima absoluta entre ONNX Runtime y `predict_proba` (informativa; la paridad es de 3.E)."""
    return float(np.abs(predict_positive_proba(onnx_bytes, rows) - expected).max())
