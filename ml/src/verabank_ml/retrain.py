"""Reentrenamiento con un comando, disparadores, promoción y rollback (HU-10, §5f; D7 y D8).

- ``retrain --reason drift|recall|schedule|manual``: entrena la familia del champion con ``risk.scoring_log`` ⨝
  ``risk.fraud_labels`` (rol ``ml_job``): solo filas maduras (≥ ``min_mature_steps`` pasos antes del máximo
  ``occurred_at``) y sin ``warmup``; features del JSONB en el orden del contrato. Split temporal interno: el último
  ``valid_fraction`` de los pasos para validación, con un hueco de ``gap_steps``. Mismas reglas que el entrenamiento
  offline: pesos de clase, sin remuestreo y umbrales fijados solo en la validación. Registra el run en MLflow, le
  adjunta su ``thresholds.json``, le asigna el alias ``challenger``, exporta ``ml/artifacts/challenger/`` (``model.onnx``
  y su ``thresholds.json``), recalcula ``golden_challenger.jsonl`` sobre los casos de ``golden.jsonl`` y actualiza el
  manifiesto; escribe ``ml/reports/retrain-report.md``.
- ``retrain --check``: qué disparadores se cumplen (alerta de drift vigente, caída de recall, días desde el corte).
- ``promote --confirm``: con las filas con ``shadow_score`` y etiqueta madura, exige que el challenger sea el que corrió
  en sombra, ≥ ``min_shadow_steps`` pasos, costo ≥ 2 % menor y recall no peor (cada modelo con sus umbrales). Mueve los
  alias, guarda ``previous_champion`` en una etiqueta, ejecuta ``ml-export`` y ``ml-psi-ref``.
- ``rollback --confirm``: vuelve al ``previous_champion`` y reexporta.

Parámetros en ``ml/retrain.yaml`` (supuestos). Desajuste heredado de la fase 4 (4/D1): el calentamiento del replay deja
menos historia que el entrenamiento offline, así que ``dst_seen_before``, ``dst_age_hours`` e ``is_new_beneficiary``
del ``scoring_log`` no se distribuyen como en PaySim completo; el informe lo declara.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import yaml
from mlflow.exceptions import MlflowException

from verabank_ml import clock_guard, metrics, modeling
from verabank_ml.features import REPO_ROOT, load_config

RETRAIN_CONFIG = REPO_ROOT / "ml" / "retrain.yaml"
ARTIFACTS = REPO_ROOT / "ml" / "artifacts"
REPORT_PATH = REPO_ROOT / "ml" / "reports" / "retrain-report.md"
CHALLENGER_DIR = ARTIFACTS / "challenger"
PREVIOUS_TAG = "previous_champion"
STEP = timedelta(hours=1)


class Refused(RuntimeError):
    """El comando se niega con un motivo claro (datos insuficientes, criterio no cumplido…)."""


def load_retrain_config(path: Path = RETRAIN_CONFIG) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def parse_epoch(text: str) -> datetime:
    return datetime.fromisoformat(text).astimezone(UTC)


def step_of(epoch: datetime, occurred_at) -> np.ndarray:
    """``occurred_at = sim_epoch + (step - 1) h`` ⇒ step."""
    ts = pd.to_datetime(pd.Series(occurred_at), utc=True)
    return (((ts - pd.Timestamp(epoch)) // pd.Timedelta(hours=1)) + 1).to_numpy(np.int64)


# --- conjunto ---------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Dataset:
    train: pd.DataFrame
    valid: pd.DataFrame
    info: dict


def to_frame(rows: pd.DataFrame, names: list[str], epoch: datetime) -> pd.DataFrame:
    """Filas de scoring_log (con ``features`` como dict) → columnas de features en el orden del contrato."""
    feats = pd.DataFrame([{k: float(f[k]) for k in names} for f in rows["features"]], index=rows.index)
    # ``amount`` también es una feature: manda la del JSONB (la que vio el modelo).
    out = pd.concat([rows.drop(columns=["features", *[c for c in names if c in rows.columns]]), feats], axis=1)
    out["step"] = step_of(epoch, out["occurred_at"])
    out["is_fraud"] = out["is_fraud"].astype(int)
    return out


def build_dataset(rows: pd.DataFrame, max_occurred_at, names: list[str], epoch: datetime, rcfg: dict) -> Dataset:
    """``rows``: scoring_log ⨝ fraud_labels (``transfer_id, occurred_at, features, is_fraud, model_version, …``).

    ``max_occurred_at`` es el máximo de todo ``scoring_log`` sin ``warmup`` (también filas sin etiqueta todavía).
    """
    cfg = rcfg["retrain"]
    real = rows[rows["model_version"] != "warmup"]
    frame = to_frame(real, names, epoch) if len(real) else pd.DataFrame(columns=[*rows.columns, *names, "step"])
    max_step = int(step_of(epoch, [max_occurred_at])[0])
    mature = frame[frame["step"] <= max_step - cfg["min_mature_steps"]]
    mature = mature.sort_values(["step", "transfer_id"], kind="mergesort").reset_index(drop=True)
    if mature.empty:
        raise Refused("no hay filas maduras con etiqueta en risk.scoring_log")
    lo, hi = int(mature["step"].min()), int(mature["step"].max())
    n_valid = max(1, round(cfg["valid_fraction"] * (hi - lo + 1)))
    valid_start = hi - n_valid + 1
    train_end = valid_start - 1 - cfg["gap_steps"]
    train = mature[mature["step"] <= train_end].reset_index(drop=True)
    valid = mature[mature["step"] >= valid_start].reset_index(drop=True)
    info = {"rows_total": len(rows), "rows_warmup_excluded": int(len(rows) - len(real)),
            "rows_immature_excluded": int(len(frame) - len(mature)), "max_step": max_step,
            "mature_steps": [lo, hi], "train_steps": [lo, train_end], "gap_steps": [train_end + 1, valid_start - 1],
            "valid_steps": [valid_start, hi], "train_rows": len(train), "valid_rows": len(valid),
            "train_frauds": int(train["is_fraud"].sum()), "valid_frauds": int(valid["is_fraud"].sum())}
    if info["train_frauds"] < cfg["min_frauds_train"] or info["valid_frauds"] < cfg["min_frauds_valid"]:
        raise Refused(f"datos insuficientes: {info['train_frauds']} fraudes en entrenamiento (mínimo "
                      f"{cfg['min_frauds_train']}) y {info['valid_frauds']} en validación (mínimo {cfg['min_frauds_valid']}); "
                      f"pasos maduros {lo}–{hi}")
    return Dataset(train, valid, info)


# --- entrenamiento ----------------------------------------------------------------------------------------------


def fit_family(family: str, params: dict, cfg: dict, Xtr, ytr, Xva, yva):
    """La familia del champion con sus hiperparámetros; XGBoost con parada temprana y reajuste, como ml-train."""
    if family == "xgb":
        probe = modeling.make_xgb(cfg, int(params["max_depth"]), float(params["scale_pos_weight"]))
        probe.fit(Xtr, ytr, eval_set=[(Xva, yva)], verbose=False)
        best = int(probe.best_iteration)
        model = modeling.make_xgb(cfg, int(params["max_depth"]), float(params["scale_pos_weight"]),
                                  n_estimators=best + 1, early_stopping=False)
        model.fit(Xtr, ytr, verbose=False)
        return model, {**params, "best_iteration": best}
    model = modeling.make_logreg(cfg, float(params["C"]))
    model.fit(Xtr, ytr)
    return model, params


def thresholds_payload(name: str, version: str, family: str, names: list[str], result: dict, cfg: dict,
                       model_sha: str, info: dict, reason: str) -> dict:
    return {
        "model_version": f"{name}/{version}", "model_sha256": model_sha, "model_family": family, "feature_set": "A",
        "feature_order": names, "t_review": result["thresholds"]["t_review"], "t_block": result["thresholds"]["t_block"],
        "rounding_decimals": cfg["rounding_decimals"],
        "decision_rule": "score = round(p, 5); BLOCK si score >= t_block; si no, REVIEW si score >= t_review; si no, APPROVE",
        "operating_points": {n: p["threshold"] for n, p in result["operating_points"].items()},
        "validation_metrics": {"pr_auc": result["pr_auc"], "operating_points": result["operating_points"],
                               "decision": result["decision"], "no_model_cost": result["no_model_cost"]},
        "retrain": {"reason": reason, "source": "risk.scoring_log ⨝ risk.fraud_labels", **info},
    }


def golden_challenger_lines(model) -> tuple[str, np.ndarray, np.ndarray]:
    """Score del challenger sobre los casos de golden.jsonl (las mismas features float32 que recibe el servicio)."""
    cases = [json.loads(line) for line in (ARTIFACTS / "golden.jsonl").read_text().splitlines() if line]
    order = json.loads((ARTIFACTS / "thresholds.json").read_text())["feature_order"]
    X = np.array([[c["features"][k] for k in order] for c in cases], dtype=np.float32)
    scores = modeling.positive_proba(model, X)
    text = "".join(json.dumps({"case_id": c["case_id"], "score": float(v)}) + "\n" for c, v in zip(cases, scores, strict=True))
    return text, X, scores


def export_challenger(family: str, name: str, version: str, model, thresholds: dict) -> dict:
    from verabank_ml import tracking
    from verabank_ml.export import onnx_header, sha256_bytes, to_onnx_bytes
    from verabank_ml.latency import onnx_parity

    payload = to_onnx_bytes(family, model, len(thresholds["feature_order"]))
    CHALLENGER_DIR.mkdir(parents=True, exist_ok=True)
    (CHALLENGER_DIR / "model.onnx").write_bytes(payload)
    thresholds = {**thresholds, "model_sha256": sha256_bytes(payload)}
    tracking.dump_json(CHALLENGER_DIR / "thresholds.json", thresholds)
    golden_text, X, scores = golden_challenger_lines(model)
    parity = onnx_parity(payload, X, scores)
    if parity >= 1e-5:
        raise RuntimeError(f"paridad ONNX del challenger {parity:.3e} >= 1e-5")
    (ARTIFACTS / "golden_challenger.jsonl").write_text(golden_text)
    manifest = json.loads((ARTIFACTS / "manifest.json").read_text())
    manifest["artifacts"]["challenger/model.onnx"] = {"sha256": sha256_bytes(payload), **onnx_header(payload)}
    manifest["artifacts"]["challenger/thresholds.json"] = {"sha256": sha256_bytes((CHALLENGER_DIR / "thresholds.json").read_bytes())}
    manifest["artifacts"]["golden_challenger.jsonl"] = {"sha256": sha256_bytes(golden_text.encode()), "cases": len(scores)}
    manifest["models"]["challenger"] = {"registered_model": name, "version": version, "family": family}
    manifest["golden"]["challenger_onnx_vs_score_max_abs_diff"] = parity
    tracking.dump_json(ARTIFACTS / "manifest.json", manifest)
    return thresholds


# --- disparadores ----------------------------------------------------------------------------------------------


def recall(decisions: np.ndarray, y: np.ndarray) -> float | None:
    """Recall de REVIEW o BLOCK (lo que no se aprueba), como ``review_or_block_recall`` de validación."""
    frauds = y == 1
    return None if not frauds.any() else float((decisions[frauds] != metrics.APPROVE).mean())


def evaluate_triggers(*, drift_alerts: list[str], champion_recall: float | None, validation_recall: float,
                      max_occurred_at: datetime, cutoff: datetime, tcfg: dict) -> list[dict]:
    # En puntos, redondeado a 9 decimales: 0,478125 − 0,428125 da 5,000000000000004 en coma flotante.
    drop = None if champion_recall is None else round(100 * (validation_recall - champion_recall), 9)
    days = (max_occurred_at - cutoff) / timedelta(days=1)
    return [
        {"trigger": "drift", "fired": bool(drift_alerts),
         "detail": f"alertas en la ventana más reciente: {', '.join(drift_alerts) or 'ninguna'}"},
        {"trigger": "recall", "fired": drop is not None and drop > tcfg["recall_drop_points"],
         "detail": ("sin fraudes maduros del champion" if drop is None else
                    f"recall {champion_recall:.4f} frente a {validation_recall:.4f} de validación: "
                    f"caída de {drop:.2f} puntos (umbral {tcfg['recall_drop_points']})")},
        {"trigger": "schedule", "fired": days > tcfg["max_days_since_cutoff"],
         "detail": f"{days:.2f} días simulados desde el corte de entrenamiento (umbral {tcfg['max_days_since_cutoff']})"},
    ]


# --- promoción --------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Decision:
    ok: bool
    reason: str
    figures: dict


def promotion_decision(shadow: pd.DataFrame, champion_t: dict, challenger_t: dict, challenger_scores: np.ndarray | None,
                       pcfg: dict, costs: dict) -> Decision:
    """``shadow``: filas maduras con etiqueta y ``shadow_score`` (``score``, ``shadow_score``, ``amount``, ``is_fraud``,
    ``step``). ``challenger_scores``: el challenger recalculado sobre sus features, para comprobar que es el de la sombra."""
    steps = int(shadow["step"].nunique()) if len(shadow) else 0
    figures: dict[str, Any] = {"shadow_rows": len(shadow), "shadow_steps": steps}
    if steps < pcfg["min_shadow_steps"]:
        return Decision(False, f"solo {steps} pasos en sombra con etiqueta madura (mínimo {pcfg['min_shadow_steps']})", figures)
    if challenger_scores is not None:
        match = float((np.abs(np.round(challenger_scores, 5) - shadow["shadow_score"].to_numpy(float)) <= 1e-5).mean())
        figures["shadow_match"] = match
        if match < pcfg["shadow_match_min"]:
            return Decision(False, f"el shadow_score registrado no es de este challenger: solo el {100 * match:.2f} % "
                                   "coincide (¿se reconstruyó risk-scoring con ml/artifacts/challenger/?)", figures)
    y = shadow["is_fraud"].to_numpy(int)
    amount = shadow["amount"].to_numpy(float)
    d_ch = metrics.decide(shadow["score"].to_numpy(float), champion_t["t_review"], champion_t["t_block"])
    d_cl = metrics.decide(shadow["shadow_score"].to_numpy(float), challenger_t["t_review"], challenger_t["t_block"])
    c_ch, c_cl = metrics.cost(d_ch, y, amount, costs), metrics.cost(d_cl, y, amount, costs)
    r_ch, r_cl = recall(d_ch, y), recall(d_cl, y)
    improvement = (c_ch - c_cl) / c_ch if c_ch > 0 else (0.0 if c_cl == c_ch else -math.inf)
    figures.update(champion_cost=c_ch, challenger_cost=c_cl, cost_improvement=improvement,
                   champion_recall=r_ch, challenger_recall=r_cl, frauds=int(y.sum()))
    cost_ok = improvement >= pcfg["min_cost_improvement"] - 1e-12
    recall_ok = (r_ch is None and r_cl is None) or (r_ch is not None and r_cl is not None and r_cl >= r_ch - 1e-12)
    if cost_ok and recall_ok:
        return Decision(True, f"costo {100 * improvement:.2f} % menor y recall {r_cl} ≥ {r_ch}", figures)
    why = []
    if not cost_ok:
        why.append(f"el costo baja {100 * improvement:.2f} % (mínimo {100 * pcfg['min_cost_improvement']:.0f} %)")
    if not recall_ok:
        why.append(f"el recall empeora ({r_cl} < {r_ch})")
    return Decision(False, "; ".join(why), figures)


# --- alias en MLflow --------------------------------------------------------------------------------------------


class Registry:
    """Alias de los modelos registrados de config.yaml (``fraud-logreg``, ``fraud-xgb``). Un alias vive en uno solo."""

    def __init__(self, client, cfg: dict) -> None:
        self.client, self.names = client, list(cfg["mlflow"]["registered_models"].values())

    def get(self, alias: str) -> tuple[str, str] | None:
        found = []
        for name in self.names:
            try:
                found.append((name, str(self.client.get_model_version_by_alias(name, alias).version)))
            except MlflowException:   # ese modelo no tiene el alias
                continue
        if len(found) > 1:
            raise RuntimeError(f"el alias {alias} está en varios modelos registrados: {found}")
        return found[0] if found else None

    def set(self, alias: str, name: str, version: str) -> None:
        for other in self.names:
            if other != name and self.get_in(other, alias):
                self.client.delete_registered_model_alias(other, alias)
        self.client.set_registered_model_alias(name, alias, version)

    def get_in(self, name: str, alias: str) -> bool:
        try:
            self.client.get_model_version_by_alias(name, alias)
            return True
        except MlflowException:
            return False

    def tag(self, key: str) -> str | None:
        for name in self.names:
            value = self.client.get_registered_model(name).tags.get(key)
            if value:
                return value
        return None

    def set_tag(self, key: str, value: str) -> None:
        for name in self.names:
            self.client.set_registered_model_tag(name, key, value)


def promote(registry: Registry, decision: Decision, reexport: Callable[[], None]) -> dict:
    if not decision.ok:
        raise Refused(f"no se promueve: {decision.reason}")
    champion, challenger = registry.get("champion"), registry.get("challenger")
    if champion is None or challenger is None or champion == challenger:
        raise Refused("hacen falta un champion y un challenger distintos")
    registry.set_tag(PREVIOUS_TAG, "/".join(champion))
    registry.set("champion", *challenger)
    registry.set("challenger", *champion)
    reexport()
    return {"champion": "/".join(challenger), "previous_champion": "/".join(champion)}


def rollback(registry: Registry, reexport: Callable[[], None]) -> dict:
    previous = registry.tag(PREVIOUS_TAG)
    current = registry.get("champion")
    if not previous or current is None:
        raise Refused("no hay previous_champion registrado: nada que deshacer")
    name, version = previous.rsplit("/", 1)
    registry.set_tag(PREVIOUS_TAG, "/".join(current))
    registry.set("champion", name, version)
    registry.set("challenger", *current)
    reexport()
    return {"champion": previous, "previous_champion": "/".join(current)}


def reexport_all() -> None:
    """ml-export y ml-psi-ref (las dos referencias del PSI dependen del champion; la de valid hereda el top-5 de train)."""
    from verabank_ml.export import export_command
    from verabank_ml.monitor.reference import psi_reference_command

    if export_command([]) != 0 or psi_reference_command([]) != 0 or psi_reference_command(["--partition", "valid"]) != 0:
        raise RuntimeError("la reexportación falló")


# --- base de datos ----------------------------------------------------------------------------------------------

LABELED = """
SELECT s.transfer_id, s.occurred_at, s.features, s.amount::float8 AS amount, s.score::float8 AS score,
       s.shadow_score::float8 AS shadow_score, s.decision, s.model_version, l.is_fraud
FROM risk.scoring_log s JOIN risk.fraud_labels l ON l.transfer_id = s.transfer_id
WHERE s.model_version <> 'warmup' AND s.source_ref NOT LIKE 'perf-%'
"""
MAX_REAL = "SELECT max(occurred_at) FROM risk.scoring_log WHERE model_version <> 'warmup' AND source_ref NOT LIKE 'perf-%'"
LATEST_ALERTS = """
SELECT DISTINCT feature FROM analytics.drift_metrics d
WHERE alert AND window_start = (SELECT max(window_start) FROM analytics.drift_metrics WHERE reference = d.reference)
ORDER BY feature
"""


def read_labeled(conn) -> tuple[pd.DataFrame, datetime]:
    cur = conn.execute(LABELED)
    rows = pd.DataFrame(cur.fetchall(), columns=[c.name for c in cur.description])
    max_at = conn.execute(MAX_REAL).fetchone()[0]
    if max_at is None:
        raise Refused("risk.scoring_log no tiene filas reales")
    return rows, max_at


def mature_rows(rows: pd.DataFrame, max_at: datetime, epoch: datetime, min_mature: int) -> pd.DataFrame:
    if rows.empty:
        return rows.assign(step=pd.Series(dtype=int))
    rows = rows.assign(step=step_of(epoch, rows["occurred_at"]))
    return rows[rows["step"] <= int(step_of(epoch, [max_at])[0]) - min_mature]


# --- informe ----------------------------------------------------------------------------------------------------


def fmt(v, digits=4):
    return "—" if v is None else (f"{v:,.2f}" if abs(v) >= 100 else f"{v:.{digits}f}")


def report(info: dict, champion: dict, challenger: dict, names: tuple[str, str], reason: str) -> str:
    rows = [("PR-AUC", "pr_auc"), ("recall a FPR 0,5 %", ("operating_points", "fpr_0.005", "recall")),
            ("recall a FPR 0,1 %", ("operating_points", "fpr_0.001", "recall")),
            ("recall de REVIEW o BLOCK", ("decision", "review_or_block_recall")),
            ("tasa de revisión", ("decision", "review_rate")), ("FPR de bloqueo", ("decision", "block_fpr")),
            ("costo", ("decision", "cost")), ("costo sin modelo", "no_model_cost")]

    def get(d, path):
        for k in (path,) if isinstance(path, str) else path:
            d = d.get(k) if isinstance(d, dict) else None
        return d

    table = "\n".join(f"| {label} | {fmt(get(champion, p))} | {fmt(get(challenger, p))} |" for label, p in rows)
    return f"""# Reentrenamiento: champion frente a challenger

Generado por `make retrain` (motivo: `{reason}`). Ambos modelos se evalúan sobre **la misma** validación interna, cada uno
con sus umbrales: el champion con los de `thresholds.json` (fijados offline) y el challenger con los suyos, fijados en
esta validación (por eso su costo es optimista frente al del champion: la comparación que decide es la de `promote`,
en sombra).

## Datos (`risk.scoring_log` ⨝ `risk.fraud_labels`, rol `ml_job`)

| Concepto | Valor |
|---|---|
| filas con etiqueta | {info['rows_total']:,} |
| excluidas por `warmup` | {info['rows_warmup_excluded']:,} |
| excluidas por etiqueta inmadura (< {info.get('min_mature_steps', 48)} pasos del máximo, paso {info['max_step']}) | {info['rows_immature_excluded']:,} |
| entrenamiento: pasos | {info['train_steps'][0]}–{info['train_steps'][1]} ({info['train_rows']:,} filas, {info['train_frauds']:,} fraudes) |
| hueco | {info['gap_steps'][0]}–{info['gap_steps'][1]} |
| validación: pasos | {info['valid_steps'][0]}–{info['valid_steps'][1]} ({info['valid_rows']:,} filas, {info['valid_frauds']:,} fraudes) |

## Métricas en la validación interna

| Métrica | champion `{names[0]}` | challenger `{names[1]}` |
|---|---|---|
{table}

## Límites declarados

- **Historia acortada (heredado de 4/D1).** El replay carga solo 24 pasos de calentamiento, así que `dst_seen_before`,
  `dst_age_hours` e `is_new_beneficiary` de `scoring_log` ven menos historia que en el entrenamiento offline: el
  challenger aprende esa distribución, no la de PaySim completo.
- La partición de prueba de PaySim es la fuente del replay: este reentrenamiento entrena con ella (lo que el sistema
  vio en producción simulada); el champion offline nunca la usó.
- Volumen pequeño frente al offline; los costos unitarios son los supuestos de `config.yaml`.
"""


# --- comandos ---------------------------------------------------------------------------------------------------


def _connect():
    import psycopg

    from verabank_ml.monitor.psi import dsn_from_env

    return psycopg.connect(dsn_from_env(), autocommit=True)


def champion_context():
    from verabank_ml import tracking
    from verabank_ml.export import champion_thresholds, load_alias, run_thresholds

    cfg = load_config()
    client = tracking.setup(cfg)
    champion = load_alias(client, cfg, "champion")
    return cfg, client, champion, champion_thresholds(client, champion), run_thresholds, load_alias


def retrain_command(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m verabank_ml retrain", description="reentrenamiento (D7)")
    parser.add_argument("--reason", choices=["drift", "recall", "schedule", "manual"], default="manual")
    parser.add_argument("--check", action="store_true", help="solo evalúa los disparadores")
    parser.add_argument("--force", action="store_true", help="salta la guarda del reloj simulado (clock-guard)")
    args = parser.parse_args(argv)
    rcfg = load_retrain_config()
    cfg, client, champion, champ_t, _, _ = champion_context()
    epoch = parse_epoch(cfg["sim_epoch"])
    names = champ_t["feature_order"]
    try:
        with _connect() as conn:
            try:
                clock_guard.check(conn, epoch, args.force)
            except clock_guard.OutsideSimulatedClock as exc:
                raise Refused(str(exc)) from exc
            rows, max_at = read_labeled(conn)
            alerts = [r[0] for r in conn.execute(LATEST_ALERTS).fetchall()]
        if args.check:
            mature = mature_rows(rows, max_at, epoch, rcfg["retrain"]["min_mature_steps"])
            mine = mature[mature["model_version"] == champ_t["model_version"]]
            decisions = metrics.decide(mine["score"].to_numpy(float), champ_t["t_review"], champ_t["t_block"])
            champ_recall = recall(decisions, mine["is_fraud"].to_numpy(int))
            cutoff = epoch + timedelta(hours=cfg["split"]["train"][1])
            triggers = evaluate_triggers(
                drift_alerts=alerts, champion_recall=champ_recall,
                validation_recall=champ_t["validation_metrics"]["decision"]["review_or_block_recall"],
                max_occurred_at=max_at, cutoff=cutoff, tcfg=rcfg["triggers"])
            for t in triggers:
                print(f"{'SE CUMPLE' if t['fired'] else 'no se cumple':<12} {t['trigger']:<9} {t['detail']}")
            fired = [t["trigger"] for t in triggers if t["fired"]]
            print(f"disparadores cumplidos: {', '.join(fired) or 'ninguno'}"
                  + (f" → make retrain REASON={fired[0]}" if fired else ""))
            return 0
        data = build_dataset(rows, max_at, names, epoch, rcfg)
    except Refused as exc:
        print(f"retrain se niega: {exc}", file=sys.stderr)
        return 3
    return _train_and_register(cfg, client, champion, champ_t, data, args.reason, rcfg)


def _train_and_register(cfg, client, champion, champ_t, data: Dataset, reason: str, rcfg: dict) -> int:
    import mlflow
    import mlflow.sklearn
    import mlflow.xgboost

    from verabank_ml import tracking

    family, name, version, champ_model = champion
    names = champ_t["feature_order"]
    params = {k: v for k, v in client.get_run(client.get_model_version(name, version).run_id).data.params.items()
              if k in ("max_depth", "scale_pos_weight", "C")}
    Xtr, ytr = modeling.matrix(data.train, names), data.train["is_fraud"].to_numpy()
    Xva, yva = modeling.matrix(data.valid, names), data.valid["is_fraud"].to_numpy()
    model, used = fit_family(family, params, cfg, Xtr, ytr, Xva, yva)
    proba = modeling.positive_proba(model, Xva)
    result = modeling.evaluate_partition(proba, data.valid, cfg)
    champ_eval = modeling.frozen_evaluation(modeling.positive_proba(champ_model, Xva), data.valid, cfg, champ_t)
    info = {**data.info, "min_mature_steps": rcfg["retrain"]["min_mature_steps"]}
    with mlflow.start_run(run_name=f"retrain ({reason})") as run:
        mlflow.set_tags({"family": family, "feature_set": "A", "stage": "retrain", "reason": reason, **modeling.provenance()})
        mlflow.log_params({**used, "model": family, "source": "scoring_log"})
        mlflow.log_metrics(modeling.flat_metrics(result))
        mlflow.log_dict(info, "retrain_dataset.json")
        logger = mlflow.xgboost if family == "xgb" else mlflow.sklearn
        kwargs = {"xgb_model": model} if family == "xgb" else {"sk_model": model}
        new_version = str(logger.log_model(name="model", registered_model_name=name, **kwargs).registered_model_version)
        thresholds = thresholds_payload(name, new_version, family, names, result, cfg, "", info, reason)
        thresholds = export_challenger(family, name, new_version, model, thresholds)
        mlflow.log_artifact(str(CHALLENGER_DIR / "thresholds.json"))   # D8: los umbrales viajan con el run
        run_id = run.info.run_id
    Registry(client, cfg).set("challenger", name, new_version)
    REPORT_PATH.write_text(report(info, champ_eval, result, (f"{name}/{version}", f"{name}/{new_version}"), reason))
    tracking.dump_json(REPORT_PATH.with_suffix(".json"), {"dataset": info, "champion": champ_eval, "challenger": result})
    print(f"challenger {name}/{new_version} (run {run_id[:8]}): PR-AUC {result['pr_auc']:.4f} frente a "
          f"{champ_eval['pr_auc']:.4f} del champion en la misma validación; informe en {REPORT_PATH.relative_to(REPO_ROOT)}")
    print("Para evaluarlo en sombra: reconstruir y reiniciar risk-scoring (docker compose build risk-scoring), "
          f"reproducir ≥ {rcfg['promotion']['min_shadow_steps']} pasos y esperar a que maduren sus etiquetas; después make promote.")
    return 0


def promote_command(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m verabank_ml promote")
    parser.add_argument("--confirm", action="store_true", help="obligatorio: mueve los alias y reexporta")
    args = parser.parse_args(argv)
    if not args.confirm:
        print("promote mueve el alias champion: repite con --confirm (make promote)", file=sys.stderr)
        return 2
    rcfg = load_retrain_config()
    cfg, client, _champion, champ_t, run_thresholds, load_alias = champion_context()
    challenger = load_alias(client, cfg, "challenger")
    chall_t = run_thresholds(client, challenger[1], challenger[2])
    if chall_t is None:
        print(f"promote se niega: el run del challenger {challenger[1]}/{challenger[2]} no tiene thresholds.json "
              "(solo se promueve un challenger de make retrain)", file=sys.stderr)
        return 3
    epoch = parse_epoch(cfg["sim_epoch"])
    with _connect() as conn:
        rows, max_at = read_labeled(conn)
    mature = mature_rows(rows, max_at, epoch, rcfg["retrain"]["min_mature_steps"])
    shadow = mature[mature["shadow_score"].notna() & (mature["model_version"] == champ_t["model_version"])]
    X = np.array([[float(f[k]) for k in chall_t["feature_order"]] for f in shadow["features"]], dtype=np.float32)
    scores = modeling.positive_proba(challenger[3], X) if len(shadow) else np.empty(0)
    decision = promotion_decision(shadow, champ_t, chall_t, scores, rcfg["promotion"], cfg["costs"])
    print(f"criterio de promoción: {json.dumps(decision.figures, default=float, ensure_ascii=False)}")
    try:
        out = promote(Registry(client, cfg), decision, reexport_all)
    except Refused as exc:
        print(f"promote se niega: {exc}", file=sys.stderr)
        return 3
    print(f"champion → {out['champion']} (previous_champion {out['previous_champion']}); ml-export y ml-psi-ref hechos.")
    print("Reconstruir y reiniciar risk-scoring para servirlo: docker compose build risk-scoring && make up")
    return 0


def rollback_command(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m verabank_ml rollback")
    parser.add_argument("--confirm", action="store_true")
    args = parser.parse_args(argv)
    if not args.confirm:
        print("rollback mueve el alias champion: repite con --confirm (make rollback)", file=sys.stderr)
        return 2
    from verabank_ml import tracking

    cfg = load_config()
    try:
        out = rollback(Registry(tracking.setup(cfg), cfg), reexport_all)
    except Refused as exc:
        print(f"rollback se niega: {exc}", file=sys.stderr)
        return 3
    print(f"champion → {out['champion']} (el anterior, {out['previous_champion']}, queda de challenger); reexportado.")
    return 0
