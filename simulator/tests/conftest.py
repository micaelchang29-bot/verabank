from __future__ import annotations

import uuid

import pytest

from verabank_sim.rail import messages as m
from verabank_sim.rail.engine import Command, RailConfig, RailEngine
from verabank_sim.rail.store import SqliteStore


class FakeClock:
    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def command(event_type: str = m.CREDIT_INSTRUCTION, transfer_id: str | None = None, **kw: str) -> Command:
    return Command(
        event_type=event_type,
        transfer_id=transfer_id or str(uuid.uuid4()),
        correlation_id=kw.get("correlation_id", str(uuid.uuid4())),
        occurred_at=kw.get("occurred_at", "2026-11-16T10:00:00.000Z"),
    )


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def make_engine(clock: FakeClock):
    def factory(store: SqliteStore | None = None, **cfg: object) -> RailEngine:
        return RailEngine(store or SqliteStore(":memory:"), RailConfig(**cfg), clock=clock)  # type: ignore[arg-type]

    return factory


@pytest.fixture(scope="session")
def schemas() -> m.Schemas:
    return m.Schemas()
