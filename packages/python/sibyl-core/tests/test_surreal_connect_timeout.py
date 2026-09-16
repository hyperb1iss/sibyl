"""Connect budgets keep a stalled handshake off the query receipt."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest
from structlog.testing import capture_logs

from sibyl_core.backends.surreal.connection import SurrealConnectTimeout
from sibyl_core.backends.surreal.dedicated_client import (
    DedicatedSurrealClient,
    _connect_timeout_seconds,
)
from sibyl_core.config import core_config


def _install_stalled_handshake(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Make every socket hang inside the handshake, as a stalled ws open does."""
    clients: list[Any] = []

    class FakeAsyncSurreal:
        def __init__(self, url: str) -> None:
            self.url = url
            self.closed = False
            clients.append(self)

        async def signin(self, credentials: dict[str, str]) -> None:
            await asyncio.sleep(30)

        async def use(self, namespace: str, database: str) -> None:
            await asyncio.sleep(30)

        async def query_raw(self, query: str, params: object | None = None) -> object:
            raise AssertionError("no statement may run when the socket never opened")

        async def close(self) -> None:
            self.closed = True

    monkeypatch.setattr("surrealdb.AsyncSurreal", FakeAsyncSurreal)
    return clients


@pytest.fixture
def short_connect_budget(monkeypatch: pytest.MonkeyPatch) -> float:
    budget = 0.05
    monkeypatch.setattr(core_config, "surreal_connect_timeout_seconds", budget)
    return budget


def _remote_client() -> DedicatedSurrealClient:
    return DedicatedSurrealClient(
        url="ws://localhost:8612/rpc",
        username="root",
        password="root",
        namespace="org_connect",
        database="graph",
        client_kind="graph",
        pool_size=1,
    )


@pytest.mark.asyncio
async def test_stalled_handshake_raises_connect_timeout_not_query_timeout(
    monkeypatch: pytest.MonkeyPatch, short_connect_budget: float
) -> None:
    clients = _install_stalled_handshake(monkeypatch)
    client = _remote_client()

    started_at = time.perf_counter()
    with pytest.raises(SurrealConnectTimeout) as failure:
        await client.connect()
    elapsed = time.perf_counter() - started_at

    assert failure.value.attempt == 1
    assert failure.value.timeout_seconds == pytest.approx(short_connect_budget)
    assert failure.value.url_scheme == "ws"
    assert elapsed < short_connect_budget * 10
    assert clients[0].closed is True


@pytest.mark.asyncio
async def test_connect_failure_logs_surreal_connect_failed_with_attempt_and_elapsed(
    monkeypatch: pytest.MonkeyPatch, short_connect_budget: float
) -> None:
    _install_stalled_handshake(monkeypatch)
    client = _remote_client()

    with capture_logs() as entries, pytest.raises(SurrealConnectTimeout):
        await client.connect()

    connect_failures = [entry for entry in entries if entry["event"] == "surreal_connect_failed"]
    assert len(connect_failures) == 1
    failure = connect_failures[0]
    assert failure["log_level"] == "warning"
    assert failure["attempt"] == 1
    assert failure["error_category"] == "connect_timeout"
    assert failure["timeout_seconds"] == pytest.approx(short_connect_budget)
    assert failure["url_scheme"] == "ws"
    assert failure["namespace"] == "org_connect"
    assert isinstance(failure["elapsed_ms"], float)
    assert failure["elapsed_ms"] > 0


@pytest.mark.asyncio
async def test_read_retries_spend_a_bounded_budget_and_name_every_attempt(
    monkeypatch: pytest.MonkeyPatch, short_connect_budget: float
) -> None:
    _install_stalled_handshake(monkeypatch)
    client = _remote_client()

    started_at = time.perf_counter()
    with capture_logs() as entries, pytest.raises(SurrealConnectTimeout):
        await client.execute_query("SELECT * FROM entity;")
    elapsed = time.perf_counter() - started_at

    attempts = [entry["attempt"] for entry in entries if entry["event"] == "surreal_connect_failed"]
    assert attempts == [1, 2, 3]
    # Three attempts at the budget, not three attempts at the websockets
    # default of ten seconds.
    assert elapsed < short_connect_budget * 3 * 10

    receipts = [entry for entry in entries if entry["event"] == "surreal_query_failed"]
    assert len(receipts) == 1
    receipt = receipts[0]
    assert receipt["error_category"] == "connect_timeout"
    assert receipt["error_type"] == "SurrealConnectTimeout"
    assert receipt["connect_timeout_seconds"] == pytest.approx(short_connect_budget)
    assert receipt["connect_attempts"] == 3
    assert receipt["connect_budget_seconds"] == pytest.approx(short_connect_budget * 3)
    assert receipt["retry_count"] == 2
    assert receipt["statement"] == "select"


@pytest.mark.asyncio
async def test_write_preflight_failure_is_also_named_as_a_connect_timeout(
    monkeypatch: pytest.MonkeyPatch, short_connect_budget: float
) -> None:
    _install_stalled_handshake(monkeypatch)
    client = _remote_client()

    with capture_logs() as entries, pytest.raises(SurrealConnectTimeout):
        await client.execute_query("CREATE entity CONTENT {};")

    receipts = [entry for entry in entries if entry["event"] == "surreal_query_failed"]
    assert [receipt["error_category"] for receipt in receipts] == ["connect_timeout"]


def test_embedded_urls_get_no_connect_budget() -> None:
    # A cold SurrealKV directory can take longer to open than a handshake
    # budget, and it is not a socket, so it must stay unbounded.
    assert _connect_timeout_seconds("memory://") is None
    assert _connect_timeout_seconds("surrealkv:///tmp/sibyl") is None
    assert _connect_timeout_seconds("ws://localhost:8612/rpc") == pytest.approx(
        core_config.surreal_connect_timeout_seconds
    )
