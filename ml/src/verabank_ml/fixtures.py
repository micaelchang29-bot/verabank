"""Escenarios de features con respuesta conocida para el servicio de scoring (T-13, `make ml-fixtures`).

Cada escenario es una lista corta de eventos en orden con identificadores inventados (`src-01`, `dst-01`).
Para cada evento se guardan las features esperadas (calculadas con `reference.py`, solo con los eventos de
pasos anteriores) y el score esperado del champion en ONNX Runtime. Sin datos ni servicios: solo usa
`contracts/`, `ml/config.yaml` y `ml/artifacts/`. La salida es determinista.
"""

import argparse
import json
import math
from pathlib import Path

import numpy as np

from verabank_ml import golden
from verabank_ml.features import CONFIG_PATH, CONTRACT_PATH, REPO_ROOT, load_config, load_contract
from verabank_ml.onnx_export import predict_positive_proba
from verabank_ml.reference import reference_features

ARTIFACTS = REPO_ROOT / "ml" / "artifacts"
OUT_PATH = REPO_ROOT / "contracts" / "fixtures" / "feature_scenarios.json"

# Evento: (horas desde sim_epoch, origen, destino, tipo, monto). El paso es horas + 1.
Event = tuple[int, str, str, str, str]

S1, S2, S3, S4, S5 = "src-01", "src-02", "src-03", "src-04", "src-05"
D1, D2, D3, D4, D5, D6, D7 = (f"dst-0{i}" for i in range(1, 8))
T, C, P, D = "TRANSFER", "CASH_OUT", "PAYMENT", "DEBIT"

SCENARIOS: list[tuple[str, str, list[Event]]] = [
    (
        "cold_start_each_type",
        (
            "Cuatro eventos sin historia, uno por transfer_type, con cuentas distintas: todas las features de "
            "historia valen su relleno y type_<categoría> marca el tipo."
        ),
        [
            (0, S1, D1, T, "1250.50"),
            (1, S2, D2, C, "300.00"),
            (2, S3, D3, P, "45.90"),
            (3, S4, D4, D, "12.00"),
        ],
    ),
    (
        "new_then_repeated_destination",
        (
            "Destino nuevo y repetido; beneficiario nuevo frente al par ya visto; otro origen hacia un destino "
            "conocido (dst_seen_before = 1 e is_new_beneficiary = 1)."
        ),
        [
            (0, S1, D1, T, "500.00"),
            (2, S1, D1, T, "500.00"),
            (3, S2, D1, T, "75.25"),
            (4, S1, D2, T, "10.00"),
            (5, S2, D1, C, "20.00"),
        ],
    ),
    (
        "src_one_hour_window",
        "Ventana de 1 h del origen: el evento del paso s-1 cuenta (src_out_count_1h = 1) y el del paso s-2 no.",
        [
            (10, S1, D1, T, "100.00"),
            (11, S1, D2, T, "200.00"),
            (13, S1, D3, T, "50.00"),
        ],
    ),
    (
        "window_24h_edges_s24_s25",
        (
            "Límites de la ventana de 24 h [s-24, s-1]: un evento en s-24 cuenta, uno en s-25 ya no; el de s-1 "
            "cuenta. Un segundo origen hacia el mismo destino da orígenes distintos."
        ),
        [
            (0, S1, D1, T, "100.10"),
            (24, S1, D1, T, "200.20"),
            (25, S1, D1, T, "300.30"),
            (26, S2, D1, T, "40.04"),
            (49, S1, D1, T, "500.50"),
            (50, S1, D1, T, "600.60"),
        ],
    ),
    (
        "same_step_events_do_not_see_each_other",
        (
            "Tres eventos del mismo paso no se ven entre sí (todos con features de arranque en frío); el evento "
            "de la hora siguiente los ve a los tres."
        ),
        [
            (7, S1, D1, T, "100.00"),
            (7, S1, D1, T, "200.00"),
            (7, S2, D1, C, "300.00"),
            (8, S1, D1, T, "50.00"),
        ],
    ),
    (
        "dst_distinct_sources_24h",
        (
            "Varios orígenes hacia el mismo destino: dst_distinct_src_24h cuenta orígenes distintos (un origen "
            "repetido cuenta una vez) y baja cuando sus eventos salen de la ventana."
        ),
        [
            (0, S1, D1, T, "10.00"),
            (1, S2, D1, T, "20.00"),
            (2, S3, D1, T, "30.00"),
            (3, S1, D1, T, "40.00"),
            (25, S4, D1, T, "50.00"),
            (26, S5, D1, T, "60.00"),
            (60, S1, D1, T, "70.00"),
        ],
    ),
    (
        "dst_age_hours",
        "dst_age_hours es la diferencia de pasos con la primera aparición del destino, sin límite de ventana.",
        [
            (0, S1, D1, T, "100.00"),
            (5, S2, D1, T, "100.00"),
            (100, S3, D1, T, "100.00"),
            (300, S1, D1, T, "100.00"),
            (600, S4, D1, T, "100.00"),
        ],
    ),
    (
        "hour_of_day_cycle",
        (
            "Horas del día 0, 3, …, 21 y repeticiones al día siguiente: hour_sin y hour_cos distintos y cíclicos "
            "(hora = (paso - 1) % 24). Cada evento usa cuentas propias."
        ),
        [
            (0, "src-01", "dst-01", T, "100.00"),
            (3, "src-02", "dst-02", T, "100.00"),
            (6, "src-03", "dst-03", T, "100.00"),
            (9, "src-04", "dst-04", T, "100.00"),
            (12, "src-05", "dst-05", T, "100.00"),
            (15, "src-06", "dst-06", T, "100.00"),
            (18, "src-07", "dst-07", T, "100.00"),
            (21, "src-08", "dst-08", T, "100.00"),
            (30, "src-09", "dst-09", T, "100.00"),
            (47, "src-10", "dst-10", T, "100.00"),
            (48, "src-11", "dst-11", T, "100.00"),
        ],
    ),
    (
        "tiny_and_huge_amounts",
        "Montos muy pequeños (0.01) y muy grandes (hasta 9999999999.99): log_amount y las sumas de ventana.",
        [
            (0, S1, D1, T, "0.01"),
            (1, S1, D1, T, "0.01"),
            (2, S2, D1, C, "9999999999.99"),
            (3, S3, D1, T, "100000000.00"),
            (4, S1, D2, P, "0.01"),
        ],
    ),
    (
        "merchant_payments_are_history",
        (
            "Un pago (PAYMENT) cuenta como historia: el destino queda visto y el par deja de ser beneficiario "
            "nuevo para cualquier tipo posterior."
        ),
        [
            (0, S1, D1, P, "89.99"),
            (1, S2, D1, T, "1500.00"),
            (2, S1, D1, T, "20.00"),
        ],
    ),
    (
        "new_beneficiary_pairs",
        "is_new_beneficiary depende del par (origen, destino), no solo del destino.",
        [
            (0, S1, D1, T, "10.00"),
            (1, S1, D2, T, "10.00"),
            (2, S2, D1, T, "10.00"),
            (3, S1, D1, T, "10.00"),
            (4, S2, D2, T, "10.00"),
            (5, S2, D1, T, "10.00"),
        ],
    ),
    (
        "src_burst_sums",
        (
            "Un origen activo hacia destinos distintos: src_out_count_1h, src_out_count_24h y src_out_sum_24h "
            "con montos decimales y la ventana de 24 h moviéndose."
        ),
        [
            (0, S1, D1, T, "10.10"),
            (1, S1, D2, T, "20.20"),
            (1, S1, D3, T, "30.30"),
            (2, S1, D4, T, "40.40"),
            (23, S1, D5, T, "50.50"),
            (24, S1, D6, T, "60.60"),
            (25, S1, D7, T, "70.70"),
            (27, S1, D1, C, "80.80"),
        ],
    ),
    (
        "long_gap_empty_windows",
        (
            "Un hueco de 600 horas: las ventanas quedan vacías, pero el destino y el par siguen vistos y "
            "dst_age_hours es la antigüedad total."
        ),
        [
            (0, S1, D1, T, "250.00"),
            (600, S1, D1, T, "250.00"),
            (601, S1, D1, T, "250.00"),
        ],
    ),
    (
        "all_types_same_pair",
        "Los cuatro tipos entre el mismo par, una vez por hora: el tipo no filtra la historia.",
        [
            (0, S1, D1, T, "100.00"),
            (1, S1, D1, C, "100.00"),
            (2, S1, D1, P, "100.00"),
            (3, S1, D1, D, "100.00"),
            (4, S1, D1, T, "100.00"),
        ],
    ),
]


def _rows(events: list[Event]) -> list[dict]:
    return [
        {"step": hour + 1, "type": kind, "amount": float(amount), "nameOrig": src, "nameDest": dst}
        for hour, src, dst, kind, amount in events
    ]


def scenario_events(events: list[Event], epoch, order: list[str], categories: list[str], types: list[str]):
    """Eventos del escenario con sus features esperadas (solo con eventos de pasos anteriores)."""
    hours = [e[0] for e in events]
    if hours != sorted(hours):
        raise ValueError("los eventos de un escenario van en orden cronológico")
    computed = reference_features(_rows(events), "A", types, categories)
    out = []
    for (hour, src, dst, kind, amount), feats in zip(events, computed, strict=True):
        if set(feats) - {"row_id"} != set(order):
            raise RuntimeError("las features de referencia no coinciden con feature_order")
        out.append(
            {
                "source_ref": src,
                "dest_ref": dst,
                "transfer_type": kind,
                "amount": amount,
                "occurred_at": golden.occurred_at(hour + 1, epoch),
                "expected_features": {name: float(feats[name]) for name in order},
            }
        )
    return out


def build_fixtures() -> dict:
    cfg, contract = load_config(CONFIG_PATH), load_contract(CONTRACT_PATH)
    thresholds = json.loads((ARTIFACTS / "thresholds.json").read_text())
    order = thresholds["feature_order"]
    categories = next(f for f in contract["features"] if f["name"] == "type")["categories"]
    epoch = golden.parse_epoch(cfg["sim_epoch"])
    model = (ARTIFACTS / "model.onnx").read_bytes()

    scenarios = []
    for name, description, events in SCENARIOS:
        built = scenario_events(events, epoch, order, categories, cfg["types"])
        X = np.array([[e["expected_features"][n] for n in order] for e in built], dtype=np.float32)
        for event, score in zip(built, predict_positive_proba(model, X), strict=True):
            if not math.isfinite(score):
                raise RuntimeError(f"score no finito en {name}")
            event["expected_score"] = float(score)
        scenarios.append({"name": name, "description": description, "events": built})
    return {
        "description": (
            "Escenarios de features con respuesta conocida (T-13). Eventos en orden; expected_features usa solo "
            "eventos de pasos anteriores (paso = horas desde sim_epoch + 1); expected_score es la probabilidad "
            "float64 del champion en ONNX Runtime sobre esas features en float32, sin redondear. Se regenera "
            "con make ml-fixtures."
        ),
        "sim_epoch": cfg["sim_epoch"],
        "model_version": thresholds["model_version"],
        "model_sha256": thresholds["model_sha256"],
        "feature_order": order,
        "scenarios": scenarios,
    }


def render(fixtures: dict) -> str:
    return json.dumps(fixtures, indent=2, ensure_ascii=False) + "\n"


def fixtures_command(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m verabank_ml fixtures")
    parser.add_argument("--out", type=Path, default=OUT_PATH)
    args = parser.parse_args(argv)
    fixtures = build_fixtures()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(render(fixtures))
    n_events = sum(len(s["events"]) for s in fixtures["scenarios"])
    print(f"{len(fixtures['scenarios'])} escenarios, {n_events} eventos -> {args.out}")
    return 0
