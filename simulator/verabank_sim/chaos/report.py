"""Informe del caos y decisión de éxito. Lógica pura: no toca red ni reloj."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

FINAL_STATES = frozenset({"COMPLETED", "FAILED", "REJECTED"})
# Tolerancia (en puntos porcentuales) entre el % de FAILED y el REJECT pedido al rail.
FAILED_TOLERANCE_POINTS = 3.0


@dataclass
class Report:
    n: int
    reject_rate: float
    created: int = 0                                  # POST originales con 201
    create_errors: list[str] = field(default_factory=list)
    replays_expected: int = 0
    replays_ok: int = 0
    replay_errors: list[str] = field(default_factory=list)
    reuse_expected: int = 0
    reuse_ok: int = 0
    reuse_errors: list[str] = field(default_factory=list)
    states: Counter[str] = field(default_factory=Counter)   # estado final observado por transferencia
    non_final: list[str] = field(default_factory=list)      # transfer_id sin estado final al vencer el plazo
    poll_errors: list[str] = field(default_factory=list)
    elapsed_s: float = 0.0

    @property
    def failed_pct(self) -> float:
        total = sum(self.states.values()) + len(self.non_final)
        return 100.0 * self.states.get("FAILED", 0) / total if total else 0.0


def evaluate(report: Report) -> list[str]:
    """Motivos de fallo; lista vacía si el caos pasó."""
    problems: list[str] = []
    if report.create_errors:
        problems.append(f"{len(report.create_errors)} creaciones sin 201 (p. ej. {report.create_errors[0]})")
    if report.created != report.n:
        problems.append(f"creadas {report.created} de {report.n}")
    if report.replays_ok != report.replays_expected or report.replay_errors:
        problems.append(f"reenvíos idempotentes correctos {report.replays_ok}/{report.replays_expected}"
                        + (f" (p. ej. {report.replay_errors[0]})" if report.replay_errors else ""))
    if report.reuse_ok != report.reuse_expected or report.reuse_errors:
        problems.append(f"reutilizaciones de clave con 422 correctas {report.reuse_ok}/{report.reuse_expected}"
                        + (f" (p. ej. {report.reuse_errors[0]})" if report.reuse_errors else ""))
    if report.non_final:
        problems.append(f"{len(report.non_final)} transferencias sin estado final al vencer --timeout")
    if report.poll_errors:
        problems.append(f"{len(report.poll_errors)} consultas de estado fallidas (p. ej. {report.poll_errors[0]})")
    unexpected = {s: c for s, c in report.states.items() if s not in FINAL_STATES}
    if unexpected:
        problems.append(f"estados no finales o desconocidos: {dict(unexpected)}")
    if report.states.get("REJECTED", 0):
        problems.append(f"{report.states['REJECTED']} transferencias REJECTED: ninguna debía fallar por saldo ni riesgo")
    gap = abs(report.failed_pct - report.reject_rate * 100.0)
    if gap >= FAILED_TOLERANCE_POINTS:
        problems.append(f"FAILED {report.failed_pct:.2f}% se aleja {gap:.2f} puntos del REJECT pedido "
                        f"({report.reject_rate * 100:.2f}%); el máximo es {FAILED_TOLERANCE_POINTS} puntos")
    return problems


def format_report(report: Report, problems: list[str]) -> str:
    lines = [
        "== informe de chaos",
        f"transferencias pedidas:        {report.n}",
        f"creadas (201):                 {report.created}",
        f"reenvíos idénticos (200 + Idempotent-Replayed + mismo id): {report.replays_ok}/{report.replays_expected}",
        f"clave reutilizada (422 idempotency-key-reuse):             {report.reuse_ok}/{report.reuse_expected}",
        "estado final:",
    ]
    for state in sorted(report.states):
        lines.append(f"  {state:<10} {report.states[state]:>6}")
    lines.append(f"  {'sin final':<10} {len(report.non_final):>6}")
    lines.append(f"FAILED: {report.failed_pct:.2f}% (REJECT pedido {report.reject_rate * 100:.2f}%, "
                 f"tolerancia {FAILED_TOLERANCE_POINTS:.0f} puntos)")
    lines.append(f"duración: {report.elapsed_s:.1f} s")
    if problems:
        lines.append("FALLO:")
        lines.extend(f"  - {p}" for p in problems)
    else:
        lines.append("OK: chaos sin hallazgos")
    return "\n".join(lines)
