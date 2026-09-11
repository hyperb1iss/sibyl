from __future__ import annotations

import asyncio
from typing import Any

import pytest

from sibyl_core.backends.surreal import dedicated_client as dedicated_client_module
from sibyl_core.backends.surreal.dedicated_client import DedicatedSurrealClient


class _ConcurrencyTracker:
    def __init__(self) -> None:
        self.in_flight = 0
        self.peak = 0
        self.release = asyncio.Event()


def _install_overlap_surreal(monkeypatch, tracker: _ConcurrencyTracker) -> list[Any]:
    clients: list[Any] = []

    class FakeAsyncSurreal:
        def __init__(self, url: str) -> None:
            self.url = url
            self.closed = False
            clients.append(self)

        async def signin(self, credentials: dict[str, str]) -> None:
            self.credentials = credentials

        async def use(self, namespace: str, database: str) -> None:
            self.namespace = namespace
            self.database = database

        async def query_raw(self, query: str, params: object | None = None) -> dict[str, object]:
            tracker.in_flight += 1
            tracker.peak = max(tracker.peak, tracker.in_flight)
            try:
                await tracker.release.wait()
                return {"result": [{"status": "OK", "result": [{"ok": "yes"}]}]}
            finally:
                tracker.in_flight -= 1

        async def close(self) -> None:
            self.closed = True

    monkeypatch.setattr("surrealdb.AsyncSurreal", FakeAsyncSurreal)
    return clients


def _install_live_surreal(monkeypatch) -> list[Any]:
    clients: list[Any] = []

    class FakeAsyncSurreal:
        def __init__(self, url: str) -> None:
            self.url = url
            self.closed = False
            self.killed: object | None = None
            self.live_args: tuple[str, bool] | None = None
            clients.append(self)

        async def signin(self, credentials: dict[str, str]) -> None:
            self.credentials = credentials

        async def use(self, namespace: str, database: str) -> None:
            self.namespace = namespace
            self.database = database

        async def live(self, table: str, *, diff: bool = False) -> str:
            self.live_args = (table, diff)
            return "live-query-id"

        async def subscribe_live(self, query_uuid: object):
            async def notifications():
                yield {
                    "uuid": "raw-a",
                    "organization_id": "org-1",
                    "query_uuid": query_uuid,
                }

            return notifications()

        async def kill(self, query_uuid: object) -> None:
            self.killed = query_uuid

        async def close(self) -> None:
            self.closed = True

    monkeypatch.setattr("surrealdb.AsyncSurreal", FakeAsyncSurreal)
    return clients


@pytest.mark.asyncio
async def test_two_reads_on_one_client_overlap_in_flight(monkeypatch) -> None:
    tracker = _ConcurrencyTracker()
    clients = _install_overlap_surreal(monkeypatch, tracker)

    client = DedicatedSurrealClient(
        url="ws://localhost:8000/rpc",
        username="root",
        password="root",
        namespace="org_overlap",
        database="graph",
        pool_size=4,
    )

    first = asyncio.create_task(client.execute_query("SELECT * FROM entity;"))
    second = asyncio.create_task(client.execute_query("SELECT * FROM entity;"))

    async def _both_in_flight() -> bool:
        return tracker.in_flight >= 2

    for _ in range(200):
        if await _both_in_flight():
            break
        await asyncio.sleep(0.005)

    assert tracker.in_flight >= 2, "two queries on one client must be in flight at once"

    tracker.release.set()
    results = await asyncio.gather(first, second)

    assert tracker.peak >= 2
    assert results == [[{"ok": "yes"}], [{"ok": "yes"}]]
    assert len(clients) >= 2


@pytest.mark.asyncio
async def test_embedded_url_collapses_to_single_connection(monkeypatch) -> None:
    tracker = _ConcurrencyTracker()
    tracker.release.set()
    clients = _install_overlap_surreal(monkeypatch, tracker)

    client = DedicatedSurrealClient(
        url="memory://",
        namespace="sibyl_content",
        database="content",
    )

    await asyncio.gather(*(client.execute_query("SELECT * FROM crawl_sources;") for _ in range(6)))

    assert len(clients) == 1


@pytest.mark.asyncio
async def test_live_table_subscribes_kills_and_closes_connection(monkeypatch) -> None:
    clients = _install_live_surreal(monkeypatch)
    client = DedicatedSurrealClient(
        url="ws://localhost:8000/rpc",
        username="root",
        password="root",
        namespace="sibyl_content",
        database="content",
    )

    async with client.live_table("raw_captures", diff=True) as notifications:
        seen = [notification async for notification in notifications]

    assert seen == [
        {
            "uuid": "raw-a",
            "organization_id": "org-1",
            "query_uuid": "live-query-id",
        }
    ]
    assert len(clients) == 1
    assert clients[0].live_args == ("raw_captures", True)
    assert clients[0].killed == "live-query-id"
    assert clients[0].closed is True


@pytest.mark.asyncio
async def test_live_table_rejects_non_websocket_urls() -> None:
    client = DedicatedSurrealClient(
        url="memory://",
        namespace="sibyl_content",
        database="content",
    )

    with pytest.raises(RuntimeError, match="WebSocket URL"):
        async with client.live_table("raw_captures"):
            pass


@pytest.mark.asyncio
async def test_close_closes_every_pooled_connection(monkeypatch) -> None:
    tracker = _ConcurrencyTracker()
    tracker.release.set()
    clients = _install_overlap_surreal(monkeypatch, tracker)

    client = DedicatedSurrealClient(
        url="ws://localhost:8000/rpc",
        username="root",
        password="root",
        namespace="org_close",
        database="graph",
        pool_size=3,
    )

    await asyncio.gather(*(client.execute_query("SELECT * FROM entity;") for _ in range(3)))
    assert len(clients) == 3

    await client.close()

    assert all(fake.closed for fake in clients)


@pytest.mark.asyncio
async def test_close_waits_for_in_flight_query(monkeypatch) -> None:
    tracker = _ConcurrencyTracker()
    clients = _install_overlap_surreal(monkeypatch, tracker)

    client = DedicatedSurrealClient(
        url="ws://localhost:8000/rpc",
        username="root",
        password="root",
        namespace="org_close_race",
        database="graph",
        pool_size=3,
    )

    query = asyncio.create_task(client.execute_query("SELECT * FROM entity;"))
    for _ in range(200):
        if tracker.in_flight >= 1:
            break
        await asyncio.sleep(0.005)
    assert tracker.in_flight >= 1

    # close() must not finish, nor close the busy connection, while a query runs.
    close_task = asyncio.create_task(client.close())
    await asyncio.sleep(0.02)
    assert not close_task.done(), "close() must wait for the in-flight query"
    assert not any(fake.closed for fake in clients), "no socket closed mid-query"

    tracker.release.set()
    await query
    await close_task
    assert all(fake.closed for fake in clients)


@pytest.mark.asyncio
async def test_embedded_url_hard_clamps_explicit_pool_size(monkeypatch) -> None:
    tracker = _ConcurrencyTracker()
    tracker.release.set()
    clients = _install_overlap_surreal(monkeypatch, tracker)

    client = DedicatedSurrealClient(
        url="memory://",
        namespace="sibyl_content",
        database="content",
        pool_size=8,
    )

    await asyncio.gather(*(client.execute_query("SELECT * FROM crawl_sources;") for _ in range(6)))

    assert len(clients) == 1


@pytest.mark.asyncio
async def test_execute_query_logs_label_origin_and_param_keys(monkeypatch) -> None:
    tracker = _ConcurrencyTracker()
    tracker.release.set()
    _install_overlap_surreal(monkeypatch, tracker)
    log_calls: list[dict[str, object]] = []

    def fake_log_query(_query: str, **fields: object) -> None:
        log_calls.append(fields)

    monkeypatch.setattr(dedicated_client_module, "log_query", fake_log_query)

    client = DedicatedSurrealClient(
        url="ws://localhost:8000/rpc",
        username="root",
        password="root",
        namespace="org_log_context",
        database="graph",
        pool_size=1,
    )

    await client.execute_query(
        "SELECT * FROM entity WHERE group_id = $group_id;",
        group_id="org_log_context",
        _query_label="entity.search.fulltext",
    )

    assert log_calls
    assert log_calls[0]["param_keys"] == ["group_id"]
    assert log_calls[0]["query_label"] == "entity.search.fulltext"
    assert str(log_calls[0]["query_origin"]).startswith(__name__ + ":")


@pytest.mark.asyncio
async def test_warm_pool_connects_every_pooled_connection(monkeypatch) -> None:
    tracker = _ConcurrencyTracker()
    tracker.release.set()
    clients = _install_overlap_surreal(monkeypatch, tracker)

    client = DedicatedSurrealClient(
        url="ws://localhost:8000/rpc",
        username="root",
        password="root",
        namespace="org_warm",
        database="graph",
        pool_size=3,
    )

    await client.warm_pool()

    assert len(clients) == 3
    assert all(fake.namespace == "org_warm" for fake in clients)


@pytest.mark.asyncio
async def test_ping_drops_transient_failed_connection(monkeypatch) -> None:
    class FakeAsyncSurreal:
        def __init__(self, _url: str) -> None:
            self.closed = False

        async def signin(self, _credentials: dict[str, str]) -> None:
            return None

        async def use(self, _namespace: str, _database: str) -> None:
            return None

        async def query_raw(self, _query: str, _params: object | None = None) -> object:
            raise TimeoutError("timed out during opening handshake")

        async def close(self) -> None:
            self.closed = True

    monkeypatch.setattr("surrealdb.AsyncSurreal", FakeAsyncSurreal)

    client = DedicatedSurrealClient(
        url="ws://localhost:8000/rpc",
        username="root",
        password="root",
        namespace="org_ping",
        database="graph",
        pool_size=1,
    )

    with pytest.raises(TimeoutError):
        await client.ping()

    pooled = await client._available.get()
    assert pooled._client is None
    client._available.put_nowait(pooled)


@pytest.mark.asyncio
async def test_execute_query_retries_server_declared_transaction_conflicts(monkeypatch) -> None:
    calls = 0
    sleeps: list[float] = []

    class FakeAsyncSurreal:
        def __init__(self, _url: str) -> None:
            pass

        async def signin(self, _credentials: dict[str, str]) -> None:
            return None

        async def use(self, _namespace: str, _database: str) -> None:
            return None

        async def query_raw(self, query: str, _params: object | None = None) -> object:
            nonlocal calls
            if query == "RETURN true;":
                return {"result": [{"status": "OK", "result": True}]}
            calls += 1
            if calls == 1:
                raise RuntimeError(
                    "Transaction conflict: Resource busy. This transaction can be retried"
                )
            return {"result": [{"status": "OK", "result": [{"ok": True}]}]}

        async def close(self) -> None:
            return None

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr("surrealdb.AsyncSurreal", FakeAsyncSurreal)
    monkeypatch.setattr(dedicated_client_module.random, "uniform", lambda _low, high: high)
    monkeypatch.setattr(dedicated_client_module.asyncio, "sleep", fake_sleep)
    client = DedicatedSurrealClient(
        url="ws://localhost:8000/rpc",
        username="root",
        password="root",
        namespace="org_conflict",
        database="graph",
        pool_size=1,
    )

    result = await client.execute_query("UPDATE entity SET updated_at = time::now();")

    assert result == [{"ok": True}]
    assert calls == 2
    assert sleeps == [dedicated_client_module._TRANSACTION_CONFLICT_RETRY_BASE_SECONDS]


@pytest.mark.asyncio
async def test_execute_query_does_not_retry_unmarked_transaction_conflicts(monkeypatch) -> None:
    calls = 0

    class FakeAsyncSurreal:
        def __init__(self, _url: str) -> None:
            pass

        async def signin(self, _credentials: dict[str, str]) -> None:
            return None

        async def use(self, _namespace: str, _database: str) -> None:
            return None

        async def query_raw(self, query: str, _params: object | None = None) -> object:
            nonlocal calls
            if query == "RETURN true;":
                return {"result": [{"status": "OK", "result": True}]}
            calls += 1
            raise RuntimeError("Transaction conflict: retry safety is unknown")

        async def close(self) -> None:
            return None

    monkeypatch.setattr("surrealdb.AsyncSurreal", FakeAsyncSurreal)
    client = DedicatedSurrealClient(
        url="ws://localhost:8000/rpc",
        username="root",
        password="root",
        namespace="org_conflict",
        database="graph",
        pool_size=1,
    )

    with pytest.raises(RuntimeError, match="retry safety is unknown"):
        await client.execute_query("UPDATE entity SET updated_at = time::now();")

    assert calls == 1


async def test_schema_renewal_has_capacity_when_graph_pool_is_occupied(monkeypatch):
    tracker = _ConcurrencyTracker()
    clients = _install_overlap_surreal(monkeypatch, tracker)
    client = DedicatedSurrealClient(
        url="ws://localhost:8000/rpc",
        username="qualification",
        password="synthetic",
        namespace="org_renewal",
        database="graph",
        pool_size=1,
    )
    try:
        async with client.schema_lease_executor() as execute_lease:
            work = asyncio.create_task(client.execute_query("RETURN 'long mutation';"))
            renewal = asyncio.create_task(execute_lease("RETURN 'renewal';"))
            try:
                for _ in range(200):
                    if tracker.in_flight == 2:
                        break
                    await asyncio.sleep(0.005)
                assert tracker.in_flight == 2
                assert all(c.namespace == "org_renewal" and c.database == "graph" for c in clients)
            finally:
                tracker.release.set()
                await asyncio.gather(work, renewal)
        assert sum(c.closed for c in clients) == 1
        await client.execute_query("RETURN 'graph still usable';")
    finally:
        tracker.release.set()
        await client.close()
    assert all(c.closed for c in clients)


async def test_embedded_schema_renewal_preserves_the_original_store(monkeypatch):
    tracker = _ConcurrencyTracker()
    tracker.release.set()
    clients = _install_overlap_surreal(monkeypatch, tracker)
    client = DedicatedSurrealClient(url="memory://", namespace="org_embedded", database="graph")
    try:
        async with client.schema_lease_executor() as execute_lease:
            await execute_lease("RETURN 'renewal';")
        await client.execute_query("RETURN 'same store';")
        assert len(clients) == 1 and not clients[0].closed
    finally:
        await client.close()
