"""Per-database index rebuild after the move to SurrealDB 4, inside each database's bootstrap.

The embedded engine never reports a compatibility level, so these tests make
it answer like a 4.x server by patching ``index_engine_of`` where it is read;
everything else (schema bootstrap, lease, REBUILD INDEX, the record) is real.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from uuid import uuid4

import pytest

from sibyl_core.backends.surreal import index_engine, schema
from sibyl_core.backends.surreal.dedicated_client import DedicatedSurrealClient
from sibyl_core.backends.surreal.index_engine import (
    INDEX_ENGINE,
    IndexRebuildFailed,
    ensure_database_indexes,
    holds_sibyl_tables,
    index_engine_of,
    settle_database_indexes,
)
from sibyl_core.backends.surreal.schema_ownership import SchemaOwnershipLost
from sibyl_core.backends.surreal.schema_version import (
    GRAPH_SCHEMA_CURRENT_VERSION,
    apply_schema_migrations,
    ensure_schema_version_table,
)
from sibyl_core.services import graph_client as graph_client_module
from sibyl_core.services.graph import SurrealGraphClient


def _fourx(info: object) -> int:
    return INDEX_ENGINE


@pytest.fixture
def server_4x(monkeypatch: pytest.MonkeyPatch) -> None:
    for module in (index_engine, schema, graph_client_module):
        monkeypatch.setattr(module, "index_engine_of", _fourx)

    async def fourx_server(driver: object) -> int:
        return INDEX_ENGINE

    monkeypatch.setattr(schema, "_server_index_engine", fourx_server)


async def _engine(client: object, name: str = "graph") -> object:
    rows = await client.execute_query(f"SELECT VALUE index_engine FROM schema_version:{name};")
    return rows[0] if rows else None


def _rebuilt(statements: list[str]) -> list[str]:
    return [statement.split("`")[1] for statement in statements]


def _count_rebuilds(monkeypatch: pytest.MonkeyPatch, client: object) -> list[str]:
    seen: list[str] = []
    original = client.execute_query

    async def counting(statement: str, **params: object) -> object:
        if statement.lstrip().startswith("REBUILD INDEX"):
            seen.append(statement)
        return await original(statement, **params)

    monkeypatch.setattr(client, "execute_query", counting)
    return seen


@pytest.fixture
async def converted_graph() -> AsyncIterator[SurrealGraphClient]:
    """A graph database bootstrapped by an engine that never marks: what a 3.x store looks like."""
    client = SurrealGraphClient(group_id=f"conv-{uuid4().hex}", url="memory://")
    try:
        await schema.bootstrap_schema(client)
        assert await _engine(client) is None
        yield client
    finally:
        await client.close()


def test_info_helpers() -> None:
    assert index_engine_of({"compatibility": 3, "tables": {}}) == INDEX_ENGINE
    assert index_engine_of({"tables": {}}) is None
    assert index_engine_of([]) is None
    assert not holds_sibyl_tables({"tables": {"schema_version": "", "schema_lease": ""}})
    assert holds_sibyl_tables({"tables": {"schema_version": "", "entity": ""}})


async def test_3x_and_embedded_leave_every_database_alone(converted_graph, monkeypatch) -> None:
    rebuilds = _count_rebuilds(monkeypatch, converted_graph)

    assert await settle_database_indexes(converted_graph, name="graph", info={"tables": {}}) is None
    await schema.bootstrap_schema(converted_graph)

    assert rebuilds == []
    assert await _engine(converted_graph) is None


async def test_a_graph_written_before_4x_is_rebuilt_then_marked_and_then_left_alone(
    converted_graph, server_4x, monkeypatch
) -> None:
    rebuilds = _count_rebuilds(monkeypatch, converted_graph)

    assert not await schema._graph_schema_is_current(converted_graph)
    await schema.bootstrap_schema(converted_graph)

    assert len(rebuilds) > 20
    assert len(rebuilds) == len(set(rebuilds))
    assert await _engine(converted_graph) == INDEX_ENGINE
    assert await schema._graph_schema_is_current(converted_graph)
    rebuilds.clear()
    await schema.bootstrap_schema(converted_graph)
    assert rebuilds == []


async def test_a_graph_created_on_4x_is_marked_without_a_rebuild(server_4x, monkeypatch) -> None:
    client = SurrealGraphClient(group_id=f"fresh-{uuid4().hex}", url="memory://")
    rebuilds = _count_rebuilds(monkeypatch, client)
    try:
        await schema.bootstrap_schema(client)
        assert rebuilds == []
        assert await _engine(client) == INDEX_ENGINE
    finally:
        await client.close()


async def test_an_interrupted_fresh_bootstrap_is_not_mistaken_for_an_old_one(
    server_4x, monkeypatch
) -> None:
    client = SurrealGraphClient(group_id=f"cut-{uuid4().hex}", url="memory://")
    original = client.execute_query
    rebuilds: list[str] = []

    async def cut_after_version_five(statement: str, **params: object) -> object:
        if statement.lstrip().startswith("REBUILD INDEX"):
            rebuilds.append(statement)
        if "UPSERT schema_version:graph" in statement and params.get("version") == 6:
            raise RuntimeError("killed mid-bootstrap")
        return await original(statement, **params)

    monkeypatch.setattr(client, "execute_query", cut_after_version_five)
    try:
        with pytest.raises(RuntimeError, match="killed mid-bootstrap"):
            await schema.bootstrap_schema(client)
        assert await _engine(client) == INDEX_ENGINE
        monkeypatch.setattr(client, "execute_query", original)
        await schema.bootstrap_schema(client)
        assert rebuilds == []
        rows = await original("SELECT VALUE version FROM schema_version:graph;")
        assert rows == [GRAPH_SCHEMA_CURRENT_VERSION]
    finally:
        await client.close()


async def test_concurrent_bootstraps_of_one_database_rebuild_it_once(
    converted_graph, server_4x, monkeypatch
) -> None:
    rebuilds = _count_rebuilds(monkeypatch, converted_graph)

    await asyncio.gather(
        schema.bootstrap_schema(converted_graph), schema.bootstrap_schema(converted_graph)
    )

    assert rebuilds
    assert len(rebuilds) == len(set(rebuilds))
    assert await _engine(converted_graph) == INDEX_ENGINE


async def test_a_failed_rebuild_fails_the_bootstrap_and_leaves_the_database_unmarked(
    converted_graph, server_4x, monkeypatch
) -> None:
    original = converted_graph.execute_query
    failed = False

    async def fail_one(statement: str, **params: object) -> object:
        nonlocal failed
        if statement.lstrip().startswith("REBUILD INDEX") and not failed:
            failed = True
            raise RuntimeError("Database index `idx` already contains 1.0dec")
        return await original(statement, **params)

    monkeypatch.setattr(converted_graph, "execute_query", fail_one)
    with pytest.raises(IndexRebuildFailed, match="already contains"):
        await schema.bootstrap_schema(converted_graph)
    assert await _engine(converted_graph) is None
    assert not await schema._graph_schema_is_current(converted_graph)

    monkeypatch.setattr(converted_graph, "execute_query", original)
    await schema.bootstrap_schema(converted_graph)
    assert await _engine(converted_graph) == INDEX_ENGINE


async def test_an_index_that_vanishes_or_is_not_built_is_never_marked(
    converted_graph, server_4x, monkeypatch
) -> None:
    original = converted_graph.execute_query

    async def vanish(statement: str, **params: object) -> object:
        result = await original(statement, **params)
        if statement.lstrip().startswith("REBUILD INDEX `idx_entity_updated`"):
            await original("REMOVE INDEX idx_entity_updated ON entity;")
        return result

    monkeypatch.setattr(converted_graph, "execute_query", vanish)
    with pytest.raises(IndexRebuildFailed, match="no longer defined"):
        await schema.bootstrap_schema(converted_graph)
    assert await _engine(converted_graph) is None

    async def errored(statement: str, **params: object) -> object:
        if statement.lstrip().startswith("INFO FOR INDEX"):
            return {"building": {"status": "error", "error": "already contains 1.0dec"}}
        return await original(statement, **params)

    monkeypatch.setattr(converted_graph, "execute_query", errored)
    with pytest.raises(IndexRebuildFailed, match="is error after its rebuild"):
        await schema.bootstrap_schema(converted_graph)
    assert await _engine(converted_graph) is None


async def test_an_owner_that_lost_its_lease_cannot_mark(
    converted_graph, server_4x, monkeypatch
) -> None:
    original = converted_graph.execute_query

    async def expire_before_mark(statement: str, **params: object) -> object:
        if "SET index_engine = $index_engine" in statement:
            await original("UPDATE schema_lease:graph SET deadline = time::now() - 1s;")
        return await original(statement, **params)

    monkeypatch.setattr(converted_graph, "execute_query", expire_before_mark)
    with pytest.raises(SchemaOwnershipLost):
        await schema.bootstrap_schema(converted_graph)
    assert await _engine(converted_graph) is None


async def test_a_read_path_that_skips_schema_preparation_still_waits_for_the_rebuild(
    converted_graph, server_4x, monkeypatch
) -> None:
    """A worker or a read path binding the runtime without ensure_schema gets a rebuilt graph."""
    from sibyl_core.services.graph_runtime import get_surreal_graph_runtime

    rebuilds = _count_rebuilds(monkeypatch, converted_graph)
    monkeypatch.setattr(graph_client_module, "is_embedded_surreal_url", lambda url: False)
    monkeypatch.setattr(graph_client_module, "_new_graph_client", lambda group_id: converted_graph)
    group_id = converted_graph.group_id
    graph_client_module._clients.pop(group_id, None)
    graph_client_module._prepared_groups.discard(group_id)
    graph_client_module._index_ready_groups.discard(group_id)
    try:
        runtime = await get_surreal_graph_runtime(group_id, ensure_schema=False)
        assert runtime.client is converted_graph
        assert rebuilds
        assert await _engine(converted_graph) == INDEX_ENGINE
        rebuilds.clear()
        await get_surreal_graph_runtime(group_id, ensure_schema=False)
        assert rebuilds == []
    finally:
        graph_client_module._clients.pop(group_id, None)
        graph_client_module._index_ready_groups.discard(group_id)


async def test_an_empty_namespace_prefix_changes_nothing(server_4x, monkeypatch) -> None:
    client = SurrealGraphClient(group_id=f"np-{uuid4().hex}", url="memory://", namespace_prefix="")
    try:
        assert client.namespace == client.group_id.replace("-", "")
        await schema.bootstrap_schema(client)
        assert await _engine(client) == INDEX_ENGINE
        await client.execute_query("UPDATE schema_version:graph SET index_engine = NONE;")
        rebuilds = _count_rebuilds(monkeypatch, client)
        await schema.bootstrap_schema(client)
        assert rebuilds
        assert await _engine(client) == INDEX_ENGINE
    finally:
        await client.close()


@pytest.fixture
async def plane() -> AsyncIterator[DedicatedSurrealClient]:
    """An auth- or content-like database, which bootstraps without a lease of its own."""
    client = DedicatedSurrealClient(url="memory://", namespace="sibyl_content", database="content")
    try:
        yield client
    finally:
        await client.close()


async def _seed_plane(client: DedicatedSurrealClient, *, marked: bool) -> None:
    await ensure_schema_version_table(client.execute_query)
    await client.execute_query(
        "DEFINE TABLE raw SCHEMALESS; DEFINE INDEX raw_at ON raw FIELDS at; "
        "DEFINE INDEX raw_uuid ON raw FIELDS uuid UNIQUE; "
        "CREATE raw:a SET at = time::now(), uuid = 'a';"
    )
    await apply_schema_migrations(
        client.execute_query, [], name="content", index_engine=INDEX_ENGINE if marked else None
    )
    await client.execute_query(
        "UPSERT schema_version:content SET name = 'content', version = 58, migrations = [], "
        "index_engine = $engine;",
        engine=INDEX_ENGINE if marked else None,
    )


async def test_mixed_age_store_each_database_decides_for_itself(
    plane, server_4x, monkeypatch
) -> None:
    """Content created on 4.x is stamped; a graph from 3.x next to it is still rebuilt."""
    fresh_info = await plane.execute_query("INFO FOR DB;")
    assert await settle_database_indexes(plane, name="content", info=fresh_info) == INDEX_ENGINE

    graph = SurrealGraphClient(group_id=f"mixed-{uuid4().hex}", url="memory://")
    try:
        monkeypatch.setattr(index_engine, "index_engine_of", lambda info: None)
        monkeypatch.setattr(schema, "index_engine_of", lambda info: None)
        await schema.bootstrap_schema(graph)
        for module in (index_engine, schema):
            monkeypatch.setattr(module, "index_engine_of", _fourx)
        rebuilds = _count_rebuilds(monkeypatch, graph)
        await schema.bootstrap_schema(graph)
        assert rebuilds
        assert await _engine(graph) == INDEX_ENGINE
    finally:
        await graph.close()


async def test_concurrent_plane_bootstraps_take_the_lease_and_rebuild_once(
    plane, server_4x, monkeypatch
) -> None:
    await _seed_plane(plane, marked=False)
    info = await plane.execute_query("INFO FOR DB;")
    rebuilds = _count_rebuilds(monkeypatch, plane)

    results = await asyncio.gather(
        settle_database_indexes(plane, name="content", info=info),
        settle_database_indexes(plane, name="content", info=info),
    )

    assert results == [None, None]
    assert sorted(_rebuilt(rebuilds)) == ["idx_schema_version_name", "raw_at", "raw_uuid"]
    assert await _engine(plane, "content") == INDEX_ENGINE


async def test_a_process_that_skips_the_bootstrap_still_waits_for_the_rebuild(
    plane, server_4x, monkeypatch
) -> None:
    assert await ensure_database_indexes(plane, name="content") is False  # nothing there yet
    await _seed_plane(plane, marked=False)
    rebuilds = _count_rebuilds(monkeypatch, plane)

    assert await ensure_database_indexes(plane, name="content") is True
    assert sorted(_rebuilt(rebuilds)) == ["idx_schema_version_name", "raw_at", "raw_uuid"]
    assert await _engine(plane, "content") == INDEX_ENGINE
    assert await ensure_database_indexes(plane, name="content") is False
    assert len(rebuilds) == 3


async def test_a_marked_plane_costs_one_record_read(plane, server_4x, monkeypatch) -> None:
    await _seed_plane(plane, marked=True)
    statements: list[str] = []
    original = plane.execute_query

    async def record(statement: str, **params: object) -> object:
        statements.append(statement)
        return await original(statement, **params)

    monkeypatch.setattr(plane, "execute_query", record)
    assert await settle_database_indexes(plane, name="content", info={"compatibility": 3}) is None
    assert len(statements) == 1
    assert statements[0].startswith("SELECT version, index_engine FROM")
