"""Cliente HTTP de transfer-service para las cargas (replay y aml). La clave de API no se imprime."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any

import httpx


@dataclass(frozen=True)
class Reply:
    status: int
    headers: dict[str, str]
    body: Any
    elapsed_ms: float


def api_credentials_from_env() -> tuple[str, str]:
    """(canal, clave) de la primera entrada de VERABANK_API_KEYS (``canal:clave,...``). El canal es el client_id."""
    first = os.environ.get("VERABANK_API_KEYS", "").split(",")[0]
    channel, sep, key = first.partition(":")
    if not sep or not key or not channel:
        raise SystemExit("VERABANK_API_KEYS no está definido en el entorno (formato canal:clave)")
    return channel, key


class HttpApi:
    def __init__(self, base_url: str, api_key: str, concurrency: int, *, timeout_s: float = 30.0,
                 transport: httpx.BaseTransport | None = None) -> None:
        self._client = httpx.Client(
            base_url=base_url,
            headers={"X-API-Key": api_key},
            timeout=timeout_s,
            transport=transport,
            limits=httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency),
        )

    @staticmethod
    def _reply(response: httpx.Response, started: float) -> Reply:
        try:
            body = response.json()
        except ValueError:
            body = None
        return Reply(response.status_code, {k.lower(): v for k, v in response.headers.items()}, body,
                     (time.perf_counter() - started) * 1000.0)

    def post_transfer(self, idempotency_key: str, body: dict[str, Any]) -> Reply:
        started = time.perf_counter()
        return self._reply(self._client.post("/api/v1/transfers", json=body,
                                             headers={"Idempotency-Key": idempotency_key}), started)

    def review(self, transfer_id: str, decision: str, comment: str) -> Reply:
        started = time.perf_counter()
        return self._reply(self._client.post(f"/api/v1/transfers/{transfer_id}/review",
                                             json={"decision": decision, "comment": comment}), started)

    def get_transfer(self, transfer_id: str) -> Reply:
        started = time.perf_counter()
        return self._reply(self._client.get(f"/api/v1/transfers/{transfer_id}"), started)

    def close(self) -> None:
        self._client.close()
