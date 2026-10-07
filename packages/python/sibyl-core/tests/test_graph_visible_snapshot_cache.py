"""The validated reader snapshot is cached per reader and retired by writes."""

from __future__ import annotations

import asyncio
from itertools import pairwise
from types import SimpleNamespace
from uuid import uuid4

import pytest

from sibyl_core.models.entities import Entity, EntityType, Relationship, RelationshipType
from sibyl_core.services import graph_cache_invalidation as invalidation
from sibyl_core.services import graph_community_managers as managers
from sibyl_core.services import graph_community_snapshot as snapshots
from sibyl_core.services import graph_derivations, graph_view_availability
from sibyl_core.services.graph_client import SurrealGraphClient
from sibyl_core.services.usage import MemoryUsageItemKind, MemoryUsageStamp, _stamp_graph_entities
from tests.test_reflection_identity import content_store as content_store
from tests.test_reflection_identity import runtime as runtime

ORG = "org-visible-cache"
READER = {"principal_id": "user_a", "accessible_projects": {"project_a"}}
OTHER_READER = {"principal_id": "user_b", "accessible_projects": {"project_b"}}


def _entities(count: int, organization_id: str = ORG, prefix: str = "node") -> list[Entity]:
    return [
        Entity(
            id=f"{prefix}-{index}",
            name=f"{prefix} {index}",
            entity_type=EntityType.TOPIC,
            organization_id=organization_id,
        )
        for index in range(count)
    ]


def _chain(entities: list[Entity]) -> list[Relationship]:
    return [
        Relationship(
            id=f"{left.id}->{right.id}",
            source_id=left.id,
            target_id=right.id,
            relationship_type=RelationshipType.RELATED_TO,
        )
        for left, right in pairwise(entities)
    ]


def _reset() -> None:
    assert not snapshots.GRAPH_SNAPSHOT_LOADS
    assert not snapshots.GRAPH_VISIBLE_SNAPSHOT_LOADS
    snapshots.GRAPH_SNAPSHOT_CACHE.clear()
    snapshots.GRAPH_VISIBLE_SNAPSHOT_CACHE.clear()


@pytest.fixture
def fake_graph(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Enumeration from fakes; the validation pass is counted at the view seam."""
    _reset()
    entities = _entities(6)
    relationships = _chain(entities)
    calls: dict = {"enumerations": 0, "validations": 0, "release": None}

    async def list_entities(*args, **kwargs):
        calls["enumerations"] += 1
        return list(entities)

    async def list_relationships(*args, **kwargs):
        return list(relationships)

    async def view(organization_id, ids, edges, *, runtime, source_visible=None):
        calls["validations"] += 1
        if calls["release"] is not None:
            await calls["release"].wait()
        return {entity.id: entity for entity in entities if entity.id in ids}, dict(edges)

    monkeypatch.setattr(snapshots, "_list_all_entities", list_entities)
    monkeypatch.setattr(snapshots, "_list_all_relationships", list_relationships)
    monkeypatch.setattr(graph_view_availability, "available_graph_view", view)
    monkeypatch.setattr(
        managers, "_runtime_for_client", lambda client, _org: SimpleNamespace(client=client)
    )
    return calls


async def test_warm_reader_snapshot_runs_no_validation(fake_graph: dict) -> None:
    client = object()
    first = await snapshots._get_visible_graph_snapshot(client, ORG, **READER)
    second = await snapshots._get_visible_graph_snapshot(client, ORG, **READER)

    assert second is first
    assert fake_graph["validations"] == 1
    assert fake_graph["enumerations"] == 1
    assert set(first.entity_by_id) == {f"node-{index}" for index in range(6)}
    assert len(first.relationships) == 5


async def test_each_reader_proves_its_own_view(fake_graph: dict) -> None:
    client = object()
    await snapshots._get_visible_graph_snapshot(client, ORG, **READER)
    await snapshots._get_visible_graph_snapshot(client, ORG, **OTHER_READER)

    assert fake_graph["validations"] == 2
    assert fake_graph["enumerations"] == 1


async def test_write_generation_retires_the_reader_snapshot(fake_graph: dict) -> None:
    client = object()
    first = await snapshots._get_visible_graph_snapshot(client, ORG, **READER)

    invalidation.invalidate_graph_caches(ORG)
    second = await snapshots._get_visible_graph_snapshot(client, ORG, **READER)

    assert second is not first
    assert fake_graph["validations"] == 2
    assert fake_graph["enumerations"] == 2

    invalidation.invalidate_graph_caches("org-elsewhere")
    assert await snapshots._get_visible_graph_snapshot(client, ORG, **READER) is second
    assert fake_graph["validations"] == 2


async def test_clearing_the_enumeration_retires_the_reader_snapshot(fake_graph: dict) -> None:
    client = object()
    first = await snapshots._get_visible_graph_snapshot(client, ORG, **READER)

    snapshots.GRAPH_SNAPSHOT_CACHE.clear()
    second = await snapshots._get_visible_graph_snapshot(client, ORG, **READER)

    assert second is not first
    assert fake_graph["validations"] == 2


async def test_concurrent_readers_share_one_validation(fake_graph: dict) -> None:
    fake_graph["release"] = asyncio.Event()
    client = object()
    first_task = asyncio.create_task(snapshots._get_visible_graph_snapshot(client, ORG, **READER))
    second_task = asyncio.create_task(snapshots._get_visible_graph_snapshot(client, ORG, **READER))
    for _ in range(10):
        await asyncio.sleep(0)
    assert fake_graph["validations"] == 1

    fake_graph["release"].set()
    first, second = await asyncio.gather(first_task, second_task)

    assert first is second
    assert fake_graph["validations"] == 1
    assert snapshots.GRAPH_VISIBLE_SNAPSHOT_LOADS == {}


async def test_write_during_the_proof_does_not_cache_the_stale_view(fake_graph: dict) -> None:
    fake_graph["release"] = asyncio.Event()
    client = object()
    task = asyncio.create_task(snapshots._get_visible_graph_snapshot(client, ORG, **READER))
    for _ in range(10):
        await asyncio.sleep(0)
    assert fake_graph["validations"] == 1

    invalidation.invalidate_graph_caches(ORG)
    fake_graph["release"].set()
    stale = await task
    fresh = await snapshots._get_visible_graph_snapshot(client, ORG, **READER)

    assert fresh is not stale
    assert fake_graph["validations"] == 2


async def test_graph_client_write_statements_bump_the_generation() -> None:
    client = SurrealGraphClient(group_id=f"gen-{uuid4().hex}", url="memory://")
    try:
        before = invalidation.graph_generation(client.group_id)
        await client.execute_query("RETURN 1;")
        await client.execute_query("SELECT * FROM probe LIMIT 1;")
        assert invalidation.graph_generation(client.group_id) == before

        await client.execute_query("CREATE probe:one SET value = 1;")
        assert invalidation.graph_generation(client.group_id) == before + 1
        await client.execute_query("UPDATE probe:one SET value = 2;")
        assert invalidation.graph_generation(client.group_id) == before + 2
    finally:
        await client.close()


async def test_verdict_reads_batch_to_the_index_union_limit_without_vectors() -> None:
    captured: list[tuple[str, dict]] = []

    async def execute_query(query: str, **params):
        captured.append((query, params))
        return [{"targets": [], "associations": []}]

    client = SimpleNamespace(execute_query=execute_query)
    ids = [f"id-{index}" for index in range(70)]
    verdicts = await graph_derivations._graph_derivation_verdicts(ORG, ids, client=client)

    assert verdicts == {}
    assert [len(params["ids"]) for _, params in captured] == [32, 32, 6]
    assert all(
        "SELECT * OMIT embedding, name_embedding FROM entity" in query for query, _ in captured
    )
    assert all("SELECT * FROM entity" not in query for query, _ in captured)


@pytest.mark.parametrize(("pool_size", "expected_peak"), [(1, 1), (4, 3)])
async def test_verdict_batches_overlap_only_on_a_pool_with_slots(
    pool_size: int, expected_peak: int
) -> None:
    """A single-slot pool takes the batches in turn: queued waiters would bind
    the pool's queue to this loop, which the next loop sharing the client
    cannot wait on."""
    in_flight = {"now": 0, "peak": 0}

    async def execute_query(query: str, **params):
        in_flight["now"] += 1
        in_flight["peak"] = max(in_flight["peak"], in_flight["now"])
        await asyncio.sleep(0)
        in_flight["now"] -= 1
        return [{"targets": [], "associations": []}]

    client = SimpleNamespace(execute_query=execute_query, pool_size=pool_size)
    ids = [f"id-{index}" for index in range(96)]
    await graph_derivations._graph_derivation_verdicts(ORG, ids, client=client)

    assert in_flight["peak"] == expected_peak


async def test_two_thousand_node_graph_is_warm_after_one_proof(
    runtime, content_store, monkeypatch: pytest.MonkeyPatch
) -> None:
    _reset()
    org = runtime.client.group_id
    entities = _entities(2000, organization_id=org, prefix="bench")
    await runtime.entity_manager.create_direct_bulk(entities)
    await runtime.relationship_manager.create_direct_bulk(_chain(entities))

    statements: list[str] = []
    original = runtime.client.execute_query
    original_batch = runtime.client.execute_query_batch

    async def counted(query: str, **params):
        statements.append(query)
        return await original(query, **params)

    async def counted_batch(query: str, **params):
        statements.append(query)
        return await original_batch(query, **params)

    monkeypatch.setattr(runtime.client, "execute_query", counted)
    monkeypatch.setattr(runtime.client, "execute_query_batch", counted_batch)
    reader = {"principal_id": "user_a", "accessible_projects": {"project_a"}}
    cold = await snapshots._get_visible_graph_snapshot(runtime.client, org, **reader)
    assert {entity.id for entity in entities} <= set(cold.entity_by_id)
    assert len(cold.relationships) == 1999
    cold_statements = len(statements)
    assert cold_statements > 0

    warm = await snapshots._get_visible_graph_snapshot(runtime.client, org, **reader)

    assert warm is cold
    assert len(statements) == cold_statements


def test_bookkeeping_labels_exempt_a_statement_from_the_bump() -> None:
    stamp = "UPDATE entity SET last_recalled_at = time::now() WHERE uuid = $id;"
    assert invalidation.query_mutates_graph(stamp) is True
    assert invalidation.query_mutates_graph(stamp, label="entity.search.vector") is True
    assert invalidation.query_mutates_graph(stamp, label="usage.graph_stamp") is False
    assert invalidation.query_mutates_graph(stamp, label="entity.bookkeeping") is False
    assert invalidation.query_mutates_graph("SELECT uuid FROM entity;") is False
    assert invalidation.query_mutates_graph("RETURN fn::retire_rows($org);") is True
    assert invalidation.query_mutates_graph("REMOVE TABLE entity;") is True
    assert invalidation.query_mutates_graph("IMPORT 'rows.surql';") is True
    assert invalidation.query_mutates_graph("DEFINE INDEX idx ON entity FIELDS uuid;") is False


def test_mutation_tokens_are_the_row_changing_subset_of_write_tokens() -> None:
    from sibyl_core.backends.surreal.connection import _WRITE_QUERY_TOKENS

    tokens = {
        token
        for token in _WRITE_QUERY_TOKENS
        if invalidation.query_mutates_graph(f"{token} something;")
    }
    assert tokens == {
        "CREATE",
        "UPDATE",
        "UPSERT",
        "DELETE",
        "INSERT",
        "RELATE",
        "FN",
        "IMPORT",
        "REMOVE",
    }


async def test_graph_client_keeps_caches_warm_for_labelled_bookkeeping() -> None:
    client = SurrealGraphClient(group_id=f"label-{uuid4().hex}", url="memory://")
    try:
        await client.execute_query("CREATE probe:one SET value = 1;")
        before = invalidation.graph_generation(client.group_id)

        await client.execute_query(
            "UPDATE probe:one SET value = 2;", _query_label="usage.graph_stamp"
        )
        await client.execute_query_batch(
            "UPDATE probe:one SET value = 3; RETURN 1;", _query_label="entity.bookkeeping"
        )
        assert invalidation.graph_generation(client.group_id) == before

        await client.execute_query("UPDATE probe:one SET value = 4;")
        assert invalidation.graph_generation(client.group_id) == before + 1
        await client.execute_query("UPDATE probe:one SET value = 5;", _query_label="other")
        assert invalidation.graph_generation(client.group_id) == before + 2
    finally:
        await client.close()


async def test_usage_stamps_and_bookkeeping_leave_the_generation_alone(
    runtime, content_store
) -> None:
    """The real call sites carry the label; structural writes still bump."""
    from datetime import UTC, datetime

    org = runtime.client.group_id
    before = invalidation.graph_generation(org)
    entity = Entity(id="stamped", name="Stamped", entity_type=EntityType.TOPIC, organization_id=org)
    await runtime.entity_manager.create_direct(entity)
    assert invalidation.graph_generation(org) == before + 1

    stamped = await _stamp_graph_entities(
        runtime.client,
        organization_id=org,
        stamps=[
            MemoryUsageStamp(
                item_kind=MemoryUsageItemKind.GRAPH_ENTITY,
                item_id=entity.id,
                retrieval_count=3,
                citation_count=1,
                last_recalled_at=datetime.now(UTC),
                last_used_at=None,
            )
        ],
    )
    assert stamped[0].retrieval_count == 3
    await runtime.entity_manager.write_bookkeeping(
        entity.id, {"last_activity_at": datetime.now(UTC), "total_tasks": 1}
    )
    assert invalidation.graph_generation(org) == before + 1

    assert await runtime.entity_manager.delete(entity.id)
    assert invalidation.graph_generation(org) == before + 2
