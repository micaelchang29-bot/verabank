"""Que un fallo no deje al proceso parado en silencio: trazas de todos los hilos y salida distinta de 0.

- ``SIGUSR1`` vuelca la traza de todos los hilos a stderr sin matar el proceso (``faulthandler``).
- Una excepción no capturada, en el hilo principal o en cualquier otro, se registra con su traza y
  el proceso termina con código 1 en lugar de quedarse sin consumir.
"""

from __future__ import annotations

import faulthandler
import logging
import os
import signal
import sys
import threading

log = logging.getLogger("verabank_sim.crash")

EXIT_CODE = 1


def _die(message: str, exc_info) -> None:
    log.critical("%s", message, exc_info=exc_info)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except Exception:  # noqa: BLE001 - ya se está muriendo
            pass
    os._exit(EXIT_CODE)


def _thread_hook(args: threading.ExceptHookArgs) -> None:
    name = args.thread.name if args.thread else "?"
    _die(f"excepción no capturada en el hilo {name!r}; el proceso termina",
         (args.exc_type, args.exc_value, args.exc_traceback))


def install() -> None:
    """Registra ``faulthandler`` (SIGUSR1 y señales fatales) y los ganchos de excepciones."""
    try:
        faulthandler.enable(file=sys.stderr, all_threads=True)
        if hasattr(signal, "SIGUSR1"):
            faulthandler.register(signal.SIGUSR1, file=sys.stderr, all_threads=True, chain=False)
    except (AttributeError, OSError, ValueError, RuntimeError):
        # stderr sin descriptor de archivo (p. ej. capturado por pytest): sin volcado, el resto sigue.
        log.debug("faulthandler no disponible en este stderr", exc_info=True)
    threading.excepthook = _thread_hook
    sys.excepthook = lambda t, v, tb: _die("excepción no capturada; el proceso termina", (t, v, tb))
