"""Schema preparation shares work within an organization without blocking others."""

import asyncio
import gc
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4
from weakref import WeakValueDictionary

import pytest

from sibyl_core.services import graph_client


@pytest.fixture(autouse=True)
def isolated_preparation_state(monkeypatch):
    monkeypatch.setattr(graph_client, "_prepared_groups", set())
    monkeypatch.setattr(graph_client, "_prepare_locks", WeakValueDictionary())


@pytest.mark.parametrize(
    "urls",
    [("ws://schema-test/rpc", "ws://schema-test/rpc"), ("surrealkv://one", "surrealkv://two")],
)
async def test_slow_schema_preparation_does_not_block_another_org(monkeypatch, urls):
    started = asyncio.Event()
    release = asyncio.Event()
    other_started = asyncio.Event()

    async def bootstrap(client):
        if client.group_id == "slow":
            started.set()
            await release.wait()
        else:
            other_started.set()

    monkeypatch.setattr(graph_client, "bootstrap_schema", bootstrap)
    slow = asyncio.create_task(
        graph_client.prepare_graph_schema(SimpleNamespace(group_id="slow", _url=urls[0]))
    )
    await started.wait()
    other = asyncio.create_task(
        graph_client.prepare_graph_schema(SimpleNamespace(group_id="other", _url=urls[1]))
    )
    try:
        await asyncio.wait_for(other_started.wait(), 1)
        await other
        assert not slow.done()
        assert graph_client._prepared_groups == {"other"}
        gc.collect()
        assert len(graph_client._prepare_locks) == 1
    finally:
        release.set()
        await asyncio.gather(slow, other)


async def test_same_org_shares_preparation_and_failure_can_retry(monkeypatch):
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def bootstrap(client):
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()

    monkeypatch.setattr(graph_client, "bootstrap_schema", bootstrap)
    client = SimpleNamespace(group_id="same", _url="ws://schema-test/rpc")
    first = asyncio.create_task(graph_client.prepare_graph_schema(client))
    await started.wait()
    others = [asyncio.create_task(graph_client.prepare_graph_schema(client)) for _ in range(3)]
    await asyncio.sleep(0)
    assert calls == 1
    assert all(not task.done() for task in others)
    release.set()
    await asyncio.gather(first, *others)
    assert calls == 1

    graph_client.mark_graph_schema_dirty("same")
    failing = AsyncMock(side_effect=RuntimeError("migration failed"))
    monkeypatch.setattr(graph_client, "bootstrap_schema", failing)
    with pytest.raises(RuntimeError, match="migration failed"):
        await graph_client.prepare_graph_schema(client)
    assert "same" not in graph_client._prepared_groups
    success = AsyncMock()
    monkeypatch.setattr(graph_client, "bootstrap_schema", success)
    await graph_client.prepare_graph_schema(client)
    success.assert_awaited_once_with(client)


async def test_shared_embedded_store_prepares_both_namespaces(tmp_path):
    url = "surrealkv://" + str(tmp_path / "schema-store")
    clients = [
        graph_client.SurrealGraphClient(group_id="schema" + uuid4().hex, url=url) for _ in range(2)
    ]
    try:
        await asyncio.gather(*(graph_client.prepare_graph_schema(client) for client in clients))
        for index, client in enumerate(clients):
            await client.execute_query("CREATE schema_probe:one SET value=$value;", value=index)
        for index, client in enumerate(clients):
            assert await client.execute_query("SELECT VALUE value FROM schema_probe;") == [index]
    finally:
        await asyncio.gather(*(client.close() for client in clients))
