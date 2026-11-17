"""Informe del caos y decisión de éxito. Lógica pura: no toca red ni reloj."""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field

FINAL_STATES = frozenset({"COMPLETED", "FAILED", "REJECTED"})
# El % de FAILED esperado es REJECT·(1−LOST)+LOST: los rechazos se reparten entre las instrucciones que
# llegan al rail y cada instrucción perdida acaba cancelada (FAILED). La holgura es la mayor entre un piso
# de 1 punto y 4 desviaciones típicas de una binomial de N transferencias.
FAILED_FLOOR_POINTS = 1.0
FAILED_SIGMAS = 4.0


def expected_failed(reject_rate: float, lost_rate: float) -> float:
    """Fracción esperada de FAILED."""
    return reject_rate * (1.0 - lost_rate) + lost_rate


def failed_tolerance_points(n: int, p: float) -> float:
    """Holgura (puntos porcentuales) alrededor del esperado: máx(1 punto, 4·√(p(1−p)/N))."""
    if n < 1:
        return FAILED_FLOOR_POINTS
    return max(FAILED_FLOOR_POINTS, 100.0 * FAILED_SIGMAS * math.sqrt(p * (1.0 - p) / n))


@dataclass
class Report:
    n: int
    reject_rate: float
    lost_rate: float = 0.0
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
    expected = expected_failed(report.reject_rate, report.lost_rate)
    tolerance = failed_tolerance_points(report.n, expected)
    gap = abs(report.failed_pct - expected * 100.0)
    if gap > tolerance:
        problems.append(f"FAILED {report.failed_pct:.2f}% se aleja {gap:.2f} puntos del esperado "
                        f"{expected * 100:.2f}% (REJECT {report.reject_rate * 100:.2f}%, LOST "
                        f"{report.lost_rate * 100:.2f}%); el máximo es {tolerance:.2f} puntos")
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
    expected = expected_failed(report.reject_rate, report.lost_rate)
    lines.append(f"FAILED: {report.failed_pct:.2f}% (esperado {expected * 100:.2f}% = REJECT "
                 f"{report.reject_rate * 100:.2f}%·(1−LOST) + LOST {report.lost_rate * 100:.2f}%, "
                 f"tolerancia ±{failed_tolerance_points(report.n, expected):.2f} puntos)")
    lines.append(f"duración: {report.elapsed_s:.1f} s")
    if problems:
        lines.append("FALLO:")
        lines.extend(f"  - {p}" for p in problems)
    else:
        lines.append("OK: chaos sin hallazgos")
    return "\n".join(lines)
