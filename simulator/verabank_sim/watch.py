"""Subcomando ``watch``: vigila el latido del simulador mientras ``chaos`` espera.

Lo usa ``make chaos-rail``. Cada ``--interval`` segundos comprueba que el simulador vive y que su latido
(``--heartbeat-file``) no tiene más de ``--max-silence`` segundos. Si no, envía SIGUSR1 al simulador (que
vuelca las trazas de todos sus hilos en su log sin morir), imprime las últimas líneas del log, detiene
``chaos`` y termina con código 3. La lógica de decisión (``Watcher``) es pura y se prueba con un latido falso.
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

EXIT_STALLED = 3
OK, DEAD, STALLED = "ok", "dead", "stalled"


@dataclass(frozen=True)
class Verdict:
    state: str
    detail: str = ""

    @property
    def healthy(self) -> bool:
        return self.state == OK


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # Un zombi (terminó y nadie hizo wait) cuenta como muerto.
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as f:
            return f.read().rsplit(")", 1)[1].split()[0] != "Z"
    except (OSError, IndexError):
        return True


class Watcher:
    """Decide si el simulador está sano. ``clock`` y ``alive`` se inyectan para probarlo."""

    def __init__(self, heartbeat_file: Path, sim_pid: int, max_silence_s: float = 30.0,
                 clock: Callable[[], float] = time.time,
                 alive: Callable[[int], bool] = pid_alive) -> None:
        self.heartbeat_file = heartbeat_file
        self.sim_pid = sim_pid
        self.max_silence_s = max_silence_s
        self._clock = clock
        self._alive = alive
        self._started = clock()

    def heartbeat_age(self) -> float | None:
        try:
            return self._clock() - self.heartbeat_file.stat().st_mtime
        except FileNotFoundError:
            return None

    def check(self) -> Verdict:
        if not self._alive(self.sim_pid):
            return Verdict(DEAD, f"el proceso del simulador (pid {self.sim_pid}) ya no existe")
        age = self.heartbeat_age()
        if age is None:   # sin latido todavía: la gracia se cuenta desde que empezó la vigilancia
            age = self._clock() - self._started
            if age > self.max_silence_s:
                return Verdict(STALLED, f"el simulador nunca escribió el latido en {age:.0f} s")
            return Verdict(OK)
        if age > self.max_silence_s:
            return Verdict(STALLED, f"sin latido desde hace {age:.0f} s (máximo {self.max_silence_s:.0f} s)")
        return Verdict(OK)


def tail(path: Path, lines: int) -> list[str]:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return list(deque((line.rstrip("\n") for line in f), maxlen=lines))
    except OSError as exc:
        return [f"(no se pudo leer {path}: {exc})"]


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--sim-pid", type=int, required=True, help="pid del simulador (rail)")
    parser.add_argument("--target-pid", type=int, required=True,
                        help="pid de `chaos`: se vigila mientras viva y se detiene si el simulador falla")
    parser.add_argument("--heartbeat-file", type=Path, required=True)
    parser.add_argument("--log", type=Path, required=True, help="log del simulador (donde caen las trazas)")
    parser.add_argument("--max-silence", type=float, default=30.0, help="segundos sin latido tolerados (30)")
    parser.add_argument("--interval", type=float, default=5.0, help="cada cuántos segundos se comprueba (5)")


def report_failure(verdict: Verdict, args: argparse.Namespace, traces_wait_s: float = 2.0) -> None:
    kind = "caído" if verdict.state == DEAD else "bloqueado"
    print(f"FALLO: simulador del rail {kind}: {verdict.detail}", file=sys.stderr, flush=True)
    if verdict.state == STALLED and pid_alive(args.sim_pid):
        os.kill(args.sim_pid, signal.SIGUSR1)   # faulthandler vuelca las trazas de todos los hilos en el log
        time.sleep(traces_wait_s)
        print(f"(trazas de todos los hilos volcadas con SIGUSR1 en {args.log})", file=sys.stderr, flush=True)
    print(f"-- últimas 30 líneas de {args.log}:", file=sys.stderr)
    for line in tail(args.log, 30):
        print(f"   {line}", file=sys.stderr)
    sys.stderr.flush()


def run(args: argparse.Namespace) -> int:
    watcher = Watcher(args.heartbeat_file, args.sim_pid, args.max_silence)
    while pid_alive(args.target_pid):
        verdict = watcher.check()
        if not verdict.healthy:
            report_failure(verdict, args)
            try:
                os.killpg(args.target_pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                try:
                    os.kill(args.target_pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            return EXIT_STALLED
        time.sleep(args.interval)
    return 0
