"""Estado del rail en SQLite (un solo proceso)."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, fields
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS credits (
    transfer_id    TEXT PRIMARY KEY,
    outcome        TEXT,              -- CONFIRMED | REJECTED; NULL si solo hay una cancelación
    detail         TEXT,              -- rail_ref (CONFIRMED) o reason_code (REJECTED)
    due_at         REAL,              -- epoch en el que vence el resultado
    silent         INTEGER NOT NULL DEFAULT 0,
    cancelled      INTEGER NOT NULL DEFAULT 0,
    reply_emitted  INTEGER NOT NULL DEFAULT 0,
    dup            INTEGER NOT NULL DEFAULT 0,
    correlation_id TEXT NOT NULL,
    occurred_at    TEXT NOT NULL
)
"""


@dataclass
class Credit:
    transfer_id: str
    correlation_id: str
    occurred_at: str
    outcome: str | None = None
    detail: str | None = None
    due_at: float | None = None
    silent: bool = False
    cancelled: bool = False
    reply_emitted: bool = False
    dup: bool = False


_COLUMNS = [f.name for f in fields(Credit)]


class SqliteStore:
    def __init__(self, path: str | Path) -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path))
        self._db.row_factory = sqlite3.Row
        if str(path) != ":memory:":
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=FULL")  # el offset se confirma después
        self._db.execute(_SCHEMA)
        self._db.commit()

    def get(self, transfer_id: str) -> Credit | None:
        row = self._db.execute("SELECT * FROM credits WHERE transfer_id = ?", (transfer_id,)).fetchone()
        return _to_credit(row) if row else None

    def save(self, credit: Credit) -> None:
        values = [int(v) if isinstance(v, bool) else v for v in (getattr(credit, c) for c in _COLUMNS)]
        placeholders = ", ".join("?" for _ in _COLUMNS)
        self._db.execute(f"INSERT OR REPLACE INTO credits ({', '.join(_COLUMNS)}) VALUES ({placeholders})", values)
        self._db.commit()

    def pending(self) -> list[Credit]:
        """Resultados decididos cuya respuesta aún debe emitirse."""
        rows = self._db.execute(
            "SELECT * FROM credits WHERE outcome IS NOT NULL AND silent = 0 AND cancelled = 0 "
            "AND reply_emitted = 0 ORDER BY due_at"
        ).fetchall()
        return [_to_credit(r) for r in rows]

    def close(self) -> None:
        self._db.close()


def _to_credit(row: sqlite3.Row) -> Credit:
    data = dict(row)
    for flag in ("silent", "cancelled", "reply_emitted", "dup"):
        data[flag] = bool(data[flag])
    return Credit(**data)
