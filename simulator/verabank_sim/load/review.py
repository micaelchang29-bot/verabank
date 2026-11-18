"""Decisión del analista por la API (``POST /api/v1/transfers/{id}/review``), común a ``aml`` y ``settle``."""

from __future__ import annotations

from typing import Any

APPROVED = 200
NOT_PENDING = 409   # review-not-pending: ya no estaba en revisión (otro la decidió, o un intento anterior)


def approve(api: Any, transfer_id: str, comment: str) -> int:
    """Aprueba una transferencia ``IN_REVIEW`` con ese comentario; devuelve el código HTTP de la API."""
    return api.review(transfer_id, "APPROVE", comment).status
