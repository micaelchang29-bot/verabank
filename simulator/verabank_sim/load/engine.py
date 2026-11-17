"""Motor de carga con barrera por grupo (D11).

Los grupos (un paso de PaySim, una hora de ``data/aml``) se procesan en orden: no se inicia ningún trabajo del grupo
``g + 1`` hasta que todos los del grupo ``g`` han terminado. Dentro de un grupo el ritmo es de ``tps`` solicitudes por
segundo, con a lo sumo ``concurrency`` en vuelo.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Outcome:
    job: Any
    value: Any = None
    error: BaseException | None = None


def _guard(work: Callable[[Any], Any], job: Any) -> Outcome:
    try:
        return Outcome(job, value=work(job))
    except Exception as exc:  # una solicitud fallida no detiene la corrida: se cuenta en el resumen
        return Outcome(job, error=exc)


def run_groups(
    groups: Iterable[tuple[Any, list[Any]]],
    work: Callable[[Any], Any],
    *,
    tps: float,
    concurrency: int,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> list[Outcome]:
    """Ejecuta ``work`` sobre cada trabajo; devuelve los resultados en el orden de los trabajos."""
    if tps <= 0 or concurrency < 1:
        raise ValueError("tps debe ser positivo y concurrency, al menos 1")
    interval = 1.0 / tps
    slots = threading.BoundedSemaphore(concurrency)
    results: list[Outcome] = []
    next_slot = clock()
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        for _, jobs in groups:
            futures: list[Future[Outcome]] = []
            for job in jobs:
                wait = next_slot - clock()
                if wait > 0:
                    sleep(wait)
                next_slot = max(next_slot, clock()) + interval
                slots.acquire()   # concurrencia acotada: si el servicio va lento, el envío espera
                future = pool.submit(_guard, work, job)
                future.add_done_callback(lambda _f: slots.release())
                futures.append(future)
            results.extend(f.result() for f in futures)   # barrera: el grupo siguiente espera a este
    return results
