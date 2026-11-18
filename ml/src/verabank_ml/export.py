"""ml-export: ONNX del campeón y del retador, golden.jsonl, golden_challenger.jsonl, thresholds.json y manifest.json.

El retador solo puntúa en sombra: no tiene umbrales ni decisión; `golden_challenger.jsonl` guarda su score
para los mismos casos del golden.
"""

import hashlib
import json
import sys
import time

import mlflow
import mlflow.sklearn
import mlflow.xgboost
import numpy as np
import onnx
import onnxruntime as ort
import pandas as pd

from verabank_ml import golden, modeling, tracking
from verabank_ml.features import CONFIG_PATH, CONTRACT_PATH, REPO_ROOT, load_config
from verabank_ml.latency import onnx_parity
from verabank_ml.onnx_export import (
    INPUT_NAME,
    POSITIVE_CLASS_COLUMN,
    sklearn_pipeline_to_onnx,
    xgboost_to_onnx,
)

ARTIFACTS = REPO_ROOT / "ml" / "artifacts"
REPORTS = REPO_ROOT / "ml" / "reports"
THRESHOLDS_PATH = ARTIFACTS / "thresholds.json"
GOLDEN_PATH = ARTIFACTS / "golden.jsonl"
CHALLENGER_ONNX = "challenger/model.onnx"
GOLDEN_CHALLENGER_PATH = ARTIFACTS / "golden_challenger.jsonl"
MANIFEST_PATH = ARTIFACTS / "manifest.json"
GOLDEN_REPORT_PATH = REPORTS / "golden_selection.json"
PSI_REFERENCE = "psi_reference.json"
HISTORY_PARTITIONS = (
    "train",
    "gap",
    "valid",
)  # el historial de un caso de valid nunca llega a test

MODEL_KEYS = ("registered_model", "version", "family")


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def onnx_header(payload: bytes) -> dict:
    """`opset_imports` e `ir_version` leídos del propio archivo ONNX (no de la constante de exportación)."""
    model = onnx.load_from_string(payload)
    return {
        "opset_imports": [
            {"domain": o.domain or "ai.onnx", "version": o.version} for o in model.opset_import
        ],
        "ir_version": model.ir_version,
    }


def load_alias(client, cfg: dict, alias: str):
    """(familia, nombre, versión, modelo) del modelo registrado con ese alias."""
    for family, name in cfg["mlflow"]["registered_models"].items():
        try:
            version = client.get_model_version_by_alias(name, alias).version
        except mlflow.exceptions.MlflowException:
            continue
        flavor = mlflow.xgboost if family == "xgb" else mlflow.sklearn
        return family, name, str(version), flavor.load_model(f"models:/{name}@{alias}")
    raise RuntimeError(f"ningún modelo registrado tiene el alias {alias}: ejecuta make ml-train")


def io_signature(payload: bytes) -> dict:
    """Entradas y salidas (nombre, tipo, forma) de un ONNX, tal como las ve ONNX Runtime."""
    session = ort.InferenceSession(payload, providers=["CPUExecutionProvider"])
    return {
        "inputs": [(i.name, i.type, i.shape) for i in session.get_inputs()],
        "outputs": [o.name for o in session.get_outputs()],
    }


def check_challenger_contract(client, thresholds: dict, challenger, champion_onnx, challenger_onnx):
    """El retador usa el mismo conjunto y orden de features y las mismas reglas de entrada y salida."""
    version = client.get_model_version_by_alias(challenger[1], "challenger")
    feature_set = client.get_run(version.run_id).data.tags.get("feature_set")
    assert feature_set == thresholds["feature_set"], (
        f"el challenger se entrenó con el conjunto {feature_set!r}, no {thresholds['feature_set']!r}"
    )
    assert modeling.feature_order(feature_set) == thresholds["feature_order"], "feature_order distinto"
    champion_io, challenger_io = io_signature(champion_onnx), io_signature(challenger_onnx)
    assert challenger_io == champion_io, f"entradas o salidas distintas: {challenger_io} != {champion_io}"
    (_, _, shape), = challenger_io["inputs"]
    assert shape[1] == len(thresholds["feature_order"]), "n_features distinto de feature_order"


def to_onnx_bytes(family: str, model, n_features: int) -> bytes:
    proto = (
        xgboost_to_onnx(model, n_features)
        if family == "xgb"
        else sklearn_pipeline_to_onnx(model, n_features)
    )
    # Los conversores ponen un UUID aleatorio en graph.name; es lo único que cambia entre dos conversiones.
    proto.graph.name = f"verabank_fraud_{family}"
    return proto.SerializeToString()


def export_command(argv: list[str]) -> int:
    del argv
    started = time.perf_counter()
    cfg = load_config()
    thresholds = json.loads(THRESHOLDS_PATH.read_text())
    cols = thresholds["feature_order"]
    if cols != modeling.feature_order(thresholds["feature_set"]):
        raise RuntimeError("feature_order de thresholds.json no coincide con features.yaml")
    client = tracking.setup(cfg)
    champion = load_alias(client, cfg, "champion")
    challenger = load_alias(client, cfg, "challenger")
    if f"{champion[1]}/{champion[2]}" != thresholds["model_version"]:
        raise RuntimeError(
            f"el champion {champion[1]}/{champion[2]} no es {thresholds['model_version']}"
        )

    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    onnx_files = {}
    (ARTIFACTS / "shadow.onnx").unlink(missing_ok=True)  # el retador vive ahora en challenger/
    for filename, (family, name, version, model) in (
        ("model.onnx", champion),
        (CHALLENGER_ONNX, challenger),
    ):
        payload = to_onnx_bytes(family, model, len(cols))
        (ARTIFACTS / filename).parent.mkdir(parents=True, exist_ok=True)
        (ARTIFACTS / filename).write_bytes(payload)
        onnx_files[filename] = {
            "sha256": sha256_bytes(payload),
            **onnx_header(payload),
            "registered_model": name,
            "version": version,
            "family": family,
        }
        print(f"{filename}: {name} v{version} ({len(payload):,} bytes)")
    check_challenger_contract(
        client, thresholds, challenger,
        (ARTIFACTS / "model.onnx").read_bytes(), (ARTIFACTS / CHALLENGER_ONNX).read_bytes(),
    )  # fmt: skip

    # --- golden: solo train, gap y valid (nunca test)
    meta = ["row_id", "step", "type", "is_fraud", "partition", "src_id", "dst_id"]
    frame = modeling.load_features(
        thresholds["feature_set"], HISTORY_PARTITIONS, columns=[*meta, *cols]
    ).reset_index(drop=True)
    step = frame["step"].to_numpy(np.int64)
    orig_code = pd.factorize(frame["src_id"])[0].astype(np.int64)
    dst_code = pd.factorize(frame["dst_id"])[0].astype(np.int64)
    in_valid = (frame["partition"] == "valid").to_numpy()
    n_history = golden.history_counts(step, orig_code, dst_code)

    valid = frame[in_valid]
    X = modeling.matrix(valid, cols)
    score = modeling.positive_proba(champion[3], X)
    candidates = pd.DataFrame(
        {"pos": np.flatnonzero(in_valid), "row_id": valid["row_id"].to_numpy(), "type": valid["type"].to_numpy(),
         "is_fraud": valid["is_fraud"].to_numpy(), "score": score, "n_history": n_history[in_valid]}
    )  # fmt: skip
    gcfg = cfg["golden"]
    exclusion = golden.exclusion_report(candidates, gcfg["max_history"])
    selected = golden.choose_cases(candidates, thresholds, gcfg, cfg["seed"])
    epoch = golden.parse_epoch(cfg["sim_epoch"])
    t_review, t_block = thresholds["t_review"], thresholds["t_block"]

    chosen_o, chosen_d = np.unique(orig_code[selected["pos"]]), np.unique(dst_code[selected["pos"]])
    related = np.flatnonzero(np.isin(orig_code, chosen_o) | np.isin(dst_code, chosen_d))
    sub = pd.DataFrame({"pos": related, "o": orig_code[related], "d": dst_code[related]})
    by_src = {k: sub["pos"].to_numpy()[v] for k, v in sub.groupby("o").indices.items()}
    by_dst = {k: sub["pos"].to_numpy()[v] for k, v in sub.groupby("d").indices.items()}
    row_ids = frame["row_id"].to_numpy()

    cases = []
    for number, case in enumerate(selected.itertuples(index=False), start=1):
        pos = int(case.pos)
        s = int(step[pos])
        near = np.union1d(
            by_src.get(orig_code[pos], np.empty(0, np.int64)),
            by_dst.get(dst_code[pos], np.empty(0, np.int64)),
        )
        near = near[step[near] < s]
        near = near[np.lexsort((row_ids[near], step[near]))]
        if len(near) != case.n_history:
            raise RuntimeError(f"historia de {len(near)} filas, esperadas {case.n_history}")
        features = frame.iloc[pos][cols].to_dict()
        decision = golden.decide_score(float(case.score), t_review, t_block)
        cases.append(
            golden.build_case(f"g{number:04d}", frame.iloc[pos], [frame.iloc[i] for i in near], features,
                              case.score, decision, case.stratum, epoch)
        )  # fmt: skip
    GOLDEN_PATH.write_text("".join(json.dumps(c, ensure_ascii=False) + "\n" for c in cases))

    # --- golden del retador: el score float64 del modelo original sobre las mismas features float32
    challenger_scores = modeling.positive_proba(
        challenger[3], modeling.matrix(frame.iloc[selected["pos"].to_numpy()], cols)
    )
    GOLDEN_CHALLENGER_PATH.write_text(
        "".join(
            json.dumps({"case_id": c["case_id"], "score": float(v)}) + "\n"
            for c, v in zip(cases, challenger_scores, strict=True)
        )
    )

    # --- comprobaciones del exportador (las pruebas de CI las repiten sin datos)
    onnx_rows = np.array([[c["features"][k] for k in cols] for c in cases], dtype=np.float32)
    golden_scores = np.array([c["score"] for c in cases])
    parity = onnx_parity((ARTIFACTS / "model.onnx").read_bytes(), onnx_rows, golden_scores)
    print(f"golden: {len(cases)} casos; diferencia máxima ONNX frente a score = {parity:.3e}")
    if parity >= 1e-5:
        raise RuntimeError(f"paridad ONNX {parity:.3e} >= 1e-5")
    challenger_parity = onnx_parity(
        (ARTIFACTS / CHALLENGER_ONNX).read_bytes(), onnx_rows, challenger_scores
    )
    print(f"golden_challenger: diferencia máxima ONNX frente a score = {challenger_parity:.3e}")
    if challenger_parity >= 1e-5:
        raise RuntimeError(f"paridad ONNX del challenger {challenger_parity:.3e} >= 1e-5")

    # --- thresholds.json, manifest.json e informe de selección del golden
    model_sha = onnx_files["model.onnx"]["sha256"]
    updated = {}
    for key, value in thresholds.items():
        if key != "model_sha256":
            updated[key] = value
        if key == "model_version":
            updated["model_sha256"] = model_sha
    tracking.dump_json(THRESHOLDS_PATH, updated)

    strata = pd.Series([c["stratum"] for c in cases]).value_counts().sort_index().to_dict()
    rounded = np.round(score, 5)
    python_rounded = np.array([round(float(v), 5) for v in score])
    boundary = selected[selected["stratum"] == "boundary"]
    boundary_sides = {}
    for name, t in (("t_review", t_review), ("t_block", t_block)):
        own = boundary[boundary["boundary_of"] == name]
        rounded_own = np.round(own["score"], 5)
        boundary_sides[name] = {
            "below": int((rounded_own < t).sum()),
            "at_or_above": int((rounded_own >= t).sum()),
            "rounding_crosses_threshold": int(((own["score"] < t) & (rounded_own >= t)).sum()),
            "max_distance_below": float((t - own["score"][rounded_own < t]).abs().max()) if (rounded_own < t).any() else None,
            "max_distance_at_or_above": float((own["score"][rounded_own >= t] - t).abs().max()) if (rounded_own >= t).any() else None,
        }  # fmt: skip
    report = {
        "exclusion": exclusion,
        "strata": strata,
        "golden_by_type_and_class": {
            f"{t}/{'fraud' if f else 'legitimate'}": int(n)
            for (t, f), n in selected.groupby(["type", "is_fraud"]).size().items()
        },
        "boundary": {"n": len(boundary), "by_threshold": boundary_sides},
        "rounding_numpy_vs_python_disagreements_in_valid": int((rounded != python_rounded).sum()),
        "onnx_vs_score_max_abs_diff_golden": parity,
    }  # fmt: skip
    tracking.dump_json(GOLDEN_REPORT_PATH, report)

    thresholds_bytes = THRESHOLDS_PATH.read_bytes()
    manifest = {
        "artifacts": {
            **{
                k: {f: v[f] for f in ("sha256", "opset_imports", "ir_version")}
                for k, v in onnx_files.items()
            },
            "thresholds.json": {"sha256": sha256_bytes(thresholds_bytes)},
            "golden.jsonl": {"sha256": sha256_bytes(GOLDEN_PATH.read_bytes()), "cases": len(cases)},
            "golden_challenger.jsonl": {
                "sha256": sha256_bytes(GOLDEN_CHALLENGER_PATH.read_bytes()),
                "cases": len(cases),
            },
        },
        "models": {
            "champion": {k: onnx_files["model.onnx"][k] for k in MODEL_KEYS},
            "challenger": {k: onnx_files[CHALLENGER_ONNX][k] for k in MODEL_KEYS},
        },
        "git_commit": thresholds["provenance"]["git_commit"],
        "onnxruntime_version": ort.__version__,
        "model_io": {
            "input_name": INPUT_NAME,
            "input_type": "float32 [None, n_features] en el orden de thresholds.json:feature_order",
            "output_name": "probabilities",
            "positive_class_column": POSITIVE_CLASS_COLUMN,
        },
        "inputs": {
            "contracts/features.yaml": sha256_bytes(CONTRACT_PATH.read_bytes()),
            "ml/config.yaml": sha256_bytes(CONFIG_PATH.read_bytes()),
            "paysim.csv": modeling.DATA_HASH_PATH.read_text().strip(),
        },
        "golden": {
            "strata": strata,
            "onnx_vs_score_max_abs_diff": parity,
            "challenger_onnx_vs_score_max_abs_diff": challenger_parity,
        },
    }
    # La referencia del PSI (make ml-psi-ref) sigue en el manifiesto solo si es del mismo champion; si el champion
    # cambió, desaparece y la prueba de manifiesto exige regenerarla.
    previous = json.loads(MANIFEST_PATH.read_text()) if MANIFEST_PATH.exists() else {}
    psi_entry = previous.get("artifacts", {}).get(PSI_REFERENCE)
    if psi_entry and psi_entry.get("model_sha256") == model_sha and (ARTIFACTS / PSI_REFERENCE).exists():
        manifest["artifacts"][PSI_REFERENCE] = psi_entry
    tracking.dump_json(MANIFEST_PATH, manifest)
    print(f"estratos: {strata}")
    print(f"excluidos por historia > {gcfg['max_history']}: {exclusion['n_excluded']:,} de {exclusion['n_candidates']:,}; "
          f"sesgo por tipo p={exclusion['by_type']['chi2_p_value']}, por clase p={exclusion['by_class']['chi2_p_value']}")  # fmt: skip
    print(f"ml-export terminado en {time.perf_counter() - started:.0f} s", file=sys.stderr)
    return 0
