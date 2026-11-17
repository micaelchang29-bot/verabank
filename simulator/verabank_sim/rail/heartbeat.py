"""Latido del simulador: un archivo que se actualiza solo mientras el bucle principal avanza.

Que el proceso viva no basta: el latido lo escribe el propio bucle de consumo y envíos, de modo que
un ``poll``, un ``produce`` o un ``commit`` que no vuelve deja de actualizarlo y quien vigila lo nota.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from pathlib import Path

HEARTBEAT_INTERVAL_S = 5.0


class Heartbeat:
    def __init__(self, path: Path | None, interval_s: float = HEARTBEAT_INTERVAL_S,
                 clock: Callable[[], float] = time.monotonic,
                 wall: Callable[[], float] = time.time) -> None:
        self.path = path
        self.interval_s = interval_s
        self._clock = clock
        self._wall = wall
        self._last: float | None = None

    def start(self) -> None:
        """Borra un latido viejo y escribe el primero."""
        if self.path is None:
            return
        self.path.unlink(missing_ok=True)
        self.beat(force=True)

    def beat(self, info: dict | None = None, force: bool = False) -> bool:
        """Escribe el latido si pasó el intervalo (o ``force``); devuelve si lo escribió."""
        if self.path is None:
            return False
        now = self._clock()
        if not force and self._last is not None and now - self._last < self.interval_s:
            return False
        self._last = now
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps({"ts": self._wall(), "pid": os.getpid(), **(info or {})}) + "\n",
                       encoding="utf-8")
        os.replace(tmp, self.path)   # atómico: quien lee nunca ve un archivo a medias
        return True
