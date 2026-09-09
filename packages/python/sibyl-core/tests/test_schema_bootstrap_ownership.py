"""Bootstrap owns resets and version advancement while current schemas stay read-only."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from sibyl_core.backends.surreal import schema
from sibyl_core.backends.surreal.schema_ownership import (
    SchemaOwnershipLost,
    try_acquire_schema_ownership,
)
from sibyl_core.backends.surreal.schema_version import GRAPH_SCHEMA_CURRENT_VERSION
from tests.test_reflection_identity import runtime as runtime


async def test_current_bootstrap_does_not_claim_or_mutate(runtime, monkeypatch):
    acquire = AsyncMock(side_effect=AssertionError("current schema claimed ownership"))
    monkeypatch.setattr(schema, "try_acquire_schema_ownership", acquire)
    original = runtime.client.execute_query
    statements = []

    async def read_only(statement, **params):
        statements.append(statement)
        assert statement.lstrip().startswith("SELECT")
        return await original(statement, **params)

    monkeypatch.setattr(runtime.client, "execute_query", read_only)
    await schema.bootstrap_schema(runtime.client)
    assert statements
    acquire.assert_not_awaited()


async def test_lost_bootstrap_cannot_advance_version_and_successor_recovers(runtime, monkeypatch):
    original = runtime.client.execute_query
    await original("UPDATE schema_version:graph SET version = 22;")
    injected = False

    async def expire_before_version(statement, **params):
        nonlocal injected
        if (
            "UPSERT schema_version:graph" in statement
            and params.get("version") == 23
            and not injected
        ):
            injected = True
            await original("UPDATE schema_lease:graph SET deadline = time::now() - 1s;")
        return await original(statement, **params)

    monkeypatch.setattr(runtime.client, "execute_query", expire_before_version)
    with pytest.raises(SchemaOwnershipLost):
        await schema.bootstrap_schema(runtime.client)
    assert injected
    assert (await original("SELECT version FROM schema_version:graph;"))[0]["version"] == 22
    await schema.bootstrap_schema(runtime.client)
    assert (await original("SELECT version FROM schema_version:graph;"))[0][
        "version"
    ] == GRAPH_SCHEMA_CURRENT_VERSION


async def test_reset_waits_for_live_owner_and_preserves_lease_table(runtime, monkeypatch):
    original = runtime.client.execute_query
    incumbent = await try_acquire_schema_ownership(original)
    assert incumbent is not None
    waiting = asyncio.Event()

    async def observe_claim(execute, **kwargs):
        result = await try_acquire_schema_ownership(execute, **kwargs)
        if result is None:
            waiting.set()
        return result

    monkeypatch.setattr(schema, "try_acquire_schema_ownership", observe_claim)
    reset = asyncio.create_task(schema.bootstrap_schema(runtime.client, reset=True))
    try:
        await asyncio.wait_for(waiting.wait(), 2)
        assert not reset.done()
        row = (await original("SELECT owner FROM schema_lease:graph;"))[0]
        assert row["owner"] == incumbent.owner
        await incumbent.release()

        await reset
        row = (await original("SELECT owner FROM schema_lease:graph;"))[0]
        assert row["owner"] != incumbent.owner
        assert (await original("SELECT version FROM schema_version:graph;"))[0][
            "version"
        ] == GRAPH_SCHEMA_CURRENT_VERSION
    finally:
        if not reset.done():
            reset.cancel()
        await asyncio.gather(reset, return_exceptions=True)
        await incumbent.release()


async def test_bootstrap_resumes_interrupted_embedding_rebuild(runtime, monkeypatch):
    original = runtime.client.execute_query
    interrupted = False

    async def fail_second_index(statement, **params):
        nonlocal interrupted
        if (
            "REMOVE INDEX" in statement
            and "idx_relates_fact_embedding" in statement
            and not interrupted
        ):
            interrupted = True
            raise RuntimeError("injected second index failure")
        return await original(statement, **params)

    monkeypatch.setattr(runtime.client, "execute_query", fail_second_index)
    with pytest.raises(RuntimeError, match="injected second index failure"):
        await schema.rebuild_embedding_indexes_for_dimension(runtime.client, dimension=8)
    assert interrupted
    intent = (await original("SELECT embedding_rebuild_dimension FROM schema_version:graph;"))[0]
    assert intent["embedding_rebuild_dimension"] == 8
    await schema.bootstrap_schema(runtime.client)
    info = await original("INFO FOR TABLE entity;")
    assert f"DIMENSION {schema.EMBEDDING_DIM}" in info["indexes"]["idx_entity_embedding"]
    intent = (await original("SELECT embedding_rebuild_dimension FROM schema_version:graph;"))[0]
    assert intent.get("embedding_rebuild_dimension") is None


@pytest.mark.parametrize("version", [22, 23])
@pytest.mark.parametrize("lease_table_exists", [False, True])
async def test_bootstrap_upgrades_version_table_without_rebuild_intent(
    runtime, version, lease_table_exists
):
    original = runtime.client.execute_query
    await original("REMOVE FIELD embedding_rebuild_dimension ON schema_version;")
    await original("UPDATE schema_version:graph SET version = $version;", version=version)
    if not lease_table_exists:
        await original("REMOVE TABLE schema_lease;")
    before = await original("INFO FOR TABLE schema_version;")
    assert "embedding_rebuild_dimension" not in before["fields"]
    await schema.bootstrap_schema(runtime.client)
    rows = await original("SELECT version, embedding_dimension FROM schema_version:graph;")
    assert rows[0]["version"] == schema.GRAPH_SCHEMA_CURRENT_VERSION
    assert rows[0]["embedding_dimension"] == schema.EMBEDDING_DIM


async def test_renewal_loss_cancels_bootstrap_and_preserves_error(runtime, monkeypatch):
    entered = asyncio.Event()
    cancelled = asyncio.Event()
    original = runtime.client.execute_query

    async def lose_ownership(ownership):
        await entered.wait()
        await original("UPDATE schema_lease:graph SET deadline = time::now() - 1s;")
        await ownership.heartbeat()

    monkeypatch.setattr(schema, "_store_supports_concurrent_rebuild", lambda url: True)
    monkeypatch.setattr(schema, "_renew_schema_ownership", lose_ownership)
    with pytest.raises(SchemaOwnershipLost):
        async with schema._graph_schema_ownership(runtime.client):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
    assert cancelled.is_set()


@pytest.mark.parametrize("operation", ["read", "mutation"])
async def test_embedded_bootstrap_continues_after_long_operation(runtime, monkeypatch, operation):
    async def short_claim(execute, **kwargs):
        return await try_acquire_schema_ownership(execute, lease_seconds=1, **kwargs)

    monkeypatch.setattr(schema, "try_acquire_schema_ownership", short_claim)
    async with schema._graph_schema_ownership(runtime.client) as ownership:
        if operation == "mutation":
            await ownership.mutate("SLEEP 1500ms; CREATE embedded_renewal:first;")
        else:
            await ownership.read("SLEEP 1500ms; RETURN 1;")
        await ownership.mutate("CREATE embedded_renewal:complete;")
    assert await runtime.client.execute_query("SELECT VALUE id FROM embedded_renewal:complete;")


@pytest.mark.parametrize("slow_read", ["version", "count", "page"])
async def test_embedded_migration_renews_all_owned_reads(runtime, monkeypatch, slow_read):
    original = runtime.client.execute_query
    await original("UPDATE schema_version:graph SET version = 22;")
    for table in schema.REMOVED_GRAPH_OBJECTS:
        await original(f"DEFINE TABLE IF NOT EXISTS {table} SCHEMALESS;")
    claimed = False
    delayed = False
    prefix = {
        "version": "SELECT version FROM schema_version",
        "count": "SELECT count() AS count FROM",
        "page": "SELECT id, uuid FROM entity WITH INDEX",
    }[slow_read]

    async def slow_owned_read(statement, **params):
        nonlocal claimed, delayed
        if "$sibyl_schema_claimed" in statement:
            result = await original(statement, **params)
            claimed = True
            return result
        if claimed and not delayed and statement.lstrip().startswith(prefix):
            delayed = True
            first, _, remaining = statement.partition(";")
            statement = f"{first}; SLEEP 1500ms; {remaining}"
        return await original(statement, **params)

    async def short_claim(execute, **kwargs):
        return await try_acquire_schema_ownership(execute, lease_seconds=1, **kwargs)

    monkeypatch.setattr(runtime.client, "execute_query", slow_owned_read)
    monkeypatch.setattr(schema, "try_acquire_schema_ownership", short_claim)
    await schema.bootstrap_schema(runtime.client)
    assert delayed
    assert await original("SELECT VALUE version FROM schema_version:graph;") == [
        GRAPH_SCHEMA_CURRENT_VERSION
    ]
