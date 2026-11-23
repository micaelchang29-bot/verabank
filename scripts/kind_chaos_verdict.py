"""Veredicto de make kind-chaos: imprime el resumen y sale con 1 si no se cumple el objetivo.

Uso: python3 scripts/kind_chaos_verdict.py <transfers-summary.json> <fallback> <puntuadas> <reemplazo_s|""> <k6_status>

Falla si: k6 terminó con error (umbrales de perf/transfers.js), no hubo respuestas, el éxito (200, 201, 422) es menor
que CHAOS_MIN_SUCCESS_PCT (99), el reemplazo del pod no quedó Ready, o la parte decidida por FALLBACK supera
CHAOS_MAX_FALLBACK_PCT (1 %). Con el scoring caído el éxito sigue en 100 % (el fallback responde), así que sin el
límite de FALLBACK la prueba no ve que el pod borrado se haya llevado el scoring.
"""

import json
import os
import sys


def verdict(summary: dict, fallback: int, scored: int, ready_s: str, k6_status: int,
            min_success: float = 99.0, max_fallback: float = 1.0) -> tuple[list[str], list[str]]:
    c = summary["counts"]
    ok = c["201"] + c["200_replayed"] + c["422_transfer_declined"] + c["422_insufficient_funds"]
    bad = c["other"] + c["422_other"] + c["200_not_replayed"]
    total = ok + bad
    lat = summary.get("load_latency_ms", {})
    success = 100 * ok / total if total else 0.0
    fallback_pct = 100 * fallback / scored if scored else 0.0
    lines = [
        f"k6: {total} respuestas; éxito (200, 201, 422) {success:.2f} %; fuera de esos códigos {bad}; "
        f"p95 {lat.get('p(95)', 0):.1f} ms, p99 {lat.get('p(99)', 0):.1f} ms" if total else "k6: sin respuestas",
        f"FALLBACK en la ventana: {fallback} de {scored} ({fallback_pct:.2f} %)",
        f"reemplazo Ready en: {ready_s or 'no quedó Ready en 300'} s",
    ]
    problems = []
    if k6_status != 0:
        problems.append(f"k6 terminó con {k6_status}")
    if not total:
        problems.append("sin respuestas de k6")
    elif success < min_success:
        problems.append(f"éxito {success:.2f} % < {min_success:g} %")
    if not ready_s:
        problems.append("el reemplazo del pod no quedó Ready")
    if fallback_pct > max_fallback:
        problems.append(f"FALLBACK {fallback_pct:.2f} % > {max_fallback:g} %")
    return lines, problems


def main() -> int:
    path, fallback, scored, ready_s, k6_status = sys.argv[1:6]
    with open(path, encoding="utf-8") as f:
        summary = json.load(f)
    lines, problems = verdict(summary, int(fallback), int(scored), ready_s, int(k6_status),
                              float(os.environ.get("CHAOS_MIN_SUCCESS_PCT", "99")),
                              float(os.environ.get("CHAOS_MAX_FALLBACK_PCT", "1")))
    print("\n".join(lines))
    if problems:
        print("CAOS FALLÓ: " + "; ".join(problems), file=sys.stderr)
        return 1
    print("OK: caos en verde")
    return 0


if __name__ == "__main__":
    sys.exit(main())
