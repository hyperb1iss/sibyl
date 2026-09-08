"""Background network capacity does not borrow interactive graph sockets."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from sibyl_core.services import graph_client, graph_runtime
from sibyl_core.services.graph_client import SurrealGraphClient


@pytest.mark.parametrize("cancel", [False, True])
async def test_background_pool_saturation_preserves_interactive_capacity(monkeypatch, cancel):
    started = asyncio.Event()
    release = asyncio.Event()
    sockets = []

    class Socket:
        def __init__(self, url):
            self.url = url
            self.closed = False
            sockets.append(self)

        async def signin(self, credentials):
            self.credentials = credentials

        async def use(self, namespace, database):
            self.namespace, self.database = namespace, database

        async def query(self, query, params=None):
            if query == "RETURN 'background';":
                started.set()
                await release.wait()
            return [query]

        async def query_raw(self, query, params=None):
            return {"result": [{"status": "OK", "result": await self.query(query, params)}]}

        async def close(self):
            await asyncio.sleep(0)
            self.closed = True

    monkeypatch.setattr("surrealdb.AsyncSurreal", Socket)
    foreground = SurrealGraphClient(
        group_id="Org-A",
        url="ws://test.invalid/rpc",
        username="synthetic",
        password="test-only",
        namespace_prefix="custom_",
        database="custom_graph",
        pool_size=1,
    )
    monkeypatch.setattr(
        graph_client,
        "settings",
        SimpleNamespace(
            resolved_surreal_url="ws://test.invalid/rpc",
            surreal_username="synthetic",
            surreal_password=SimpleNamespace(get_secret_value=lambda: "test-only"),
            surreal_token=SimpleNamespace(get_secret_value=lambda: ""),
            surreal_namespace_prefix="custom_",
            surreal_database="custom_graph",
            surreal_client_pool_size=lambda kind: 1,
        ),
    )
    monkeypatch.setattr(
        graph_client,
        "get_surreal_graph_client",
        AsyncMock(side_effect=AssertionError("background touched foreground cache")),
    )
    monkeypatch.setattr(graph_runtime, "prepare_graph_schema", AsyncMock())
    background_socket = None

    async def work():
        nonlocal background_socket
        async with graph_runtime.background_graph_runtime("Org-A") as runtime:
            assert runtime.client is not foreground
            assert runtime.client._pool_size == foreground._pool_size
            await runtime.client.execute_query("RETURN 'background';")
            background_socket = sockets[0]

    task = asyncio.create_task(work())
    try:
        await asyncio.wait_for(started.wait(), timeout=2)
        background_socket = sockets[0]
        # The sole background socket is held until release; a shared pool deadlocks here.
        result = await asyncio.wait_for(foreground.execute_query("RETURN 'interactive';"), 2)
        assert result == ["RETURN 'interactive';"]
        assert len(sockets) == 2
        assert all(socket.namespace == "custom_orga" for socket in sockets)
        assert all(socket.database == "custom_graph" for socket in sockets)
        assert all(
            socket.credentials == {"username": "synthetic", "password": "test-only"}
            for socket in sockets
        )
        if cancel:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            release.set()
            await task
        assert background_socket.closed
        assert not sockets[1].closed
        assert await foreground.execute_query("RETURN 'still available';")
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await foreground.close()


async def test_embedded_background_preserves_database_and_client_lifetime(monkeypatch):
    client = SurrealGraphClient(group_id="background-embedded", url="memory://")
    monkeypatch.setattr(graph_client, "settings", SimpleNamespace(resolved_surreal_url="memory://"))
    monkeypatch.setattr(graph_client, "get_surreal_graph_client", AsyncMock(return_value=client))
    monkeypatch.setattr(graph_runtime, "prepare_graph_schema", AsyncMock())
    try:
        await client.execute_query("CREATE sentinel:one SET value = 'retained';")
        async with graph_runtime.background_graph_runtime(client.group_id) as runtime:
            assert runtime.client is client
            assert await runtime.client.execute_query("SELECT VALUE value FROM sentinel:one;") == [
                "retained"
            ]
        assert await client.execute_query("SELECT VALUE value FROM sentinel:one;") == ["retained"]
    finally:
        await client.close()


async def test_overlapping_background_leases_share_pool_until_last_exit(monkeypatch):
    client = SurrealGraphClient(
        group_id="shared-background", url="ws://test.invalid/rpc", pool_size=3
    )
    client.close = AsyncMock()
    factory = Mock(return_value=client)
    monkeypatch.setattr(
        graph_client, "settings", SimpleNamespace(resolved_surreal_url="ws://test.invalid/rpc")
    )
    monkeypatch.setattr(graph_client, "_new_graph_client", factory)
    async with graph_client.background_graph_client(client.group_id) as first:
        async with graph_client.background_graph_client(client.group_id) as second:
            assert first is second is client
            assert second._pool_size == 3
            factory.assert_called_once_with(client.group_id)
        client.close.assert_not_awaited()
    client.close.assert_awaited_once()
    assert client.group_id not in graph_client._background_clients


async def test_repeated_shutdown_cancellation_finishes_socket_teardown(monkeypatch):
    closing = asyncio.Event()
    release = asyncio.Event()
    client = SurrealGraphClient(group_id="cancel-background", url="ws://test.invalid/rpc")

    async def close():
        closing.set()
        await release.wait()

    client.close = AsyncMock(side_effect=close)
    monkeypatch.setattr(
        graph_client, "settings", SimpleNamespace(resolved_surreal_url="ws://test.invalid/rpc")
    )
    monkeypatch.setattr(graph_client, "_new_graph_client", lambda _: client)

    async def work():
        async with graph_client.background_graph_client(client.group_id):
            pass

    task = asyncio.create_task(work())
    try:
        await asyncio.wait_for(closing.wait(), 2)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert client.group_id not in graph_client._background_clients
        client.close.assert_awaited_once()
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


async def test_cancellation_while_release_waits_for_registry_lock_does_not_leak(monkeypatch):
    client = SurrealGraphClient(group_id="cancel-lease-lock", url="ws://test.invalid/rpc")
    client.close = AsyncMock()
    monkeypatch.setattr(
        graph_client, "settings", SimpleNamespace(resolved_surreal_url="ws://test.invalid/rpc")
    )
    monkeypatch.setattr(graph_client, "_new_graph_client", lambda _: client)
    entered = asyncio.Event()
    leave = asyncio.Event()

    async def work():
        async with graph_client.background_graph_client(client.group_id):
            entered.set()
            await leave.wait()

    task = asyncio.create_task(work())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        async with graph_client._background_lock:
            leave.set()
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
        with pytest.raises(asyncio.CancelledError):
            await task
        client.close.assert_awaited_once()
        assert client.group_id not in graph_client._background_clients
    finally:
        leave.set()
        await asyncio.gather(task, return_exceptions=True)
