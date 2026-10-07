"""Whole-graph enumeration walks the indexes by keyset, never by offset."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from itertools import pairwise

import pytest

from sibyl_core.models.entities import Entity, EntityType, Relationship, RelationshipType
from sibyl_core.services.graph_client import SurrealGraphClient
from sibyl_core.services.graph_community_managers import (
    _list_all_entities,
    _list_all_relationships,
)
from tests.test_reflection_identity import runtime as runtime

PAGE = 1000
DATED = 1250
TIED = 1100
UNDATED = 300


@pytest.fixture
async def enumerated_graph(runtime, monkeypatch: pytest.MonkeyPatch):
    """A graph larger than one page, with a tie group wider than a page and
    an undated tail, so every keyset phase boundary is crossed."""
    org = runtime.client.group_id
    base = datetime(2026, 1, 1, tzinfo=UTC)
    entities = [
        Entity(
            id=f"walk-{index:05d}",
            name=f"walk {index}",
            entity_type=EntityType.TOPIC,
            organization_id=org,
            created_at=base + timedelta(seconds=index),
            updated_at=base + timedelta(seconds=index),
        )
        for index in range(DATED + TIED + UNDATED)
    ]
    await runtime.entity_manager.create_direct_bulk(entities)
    tied_ids = [entity.id for entity in entities[DATED : DATED + TIED]]
    undated_ids = [entity.id for entity in entities[DATED + TIED :]]
    stamp = base - timedelta(days=1)
    await runtime.client.execute_query(
        "UPDATE entity SET updated_at = $stamp, created_at = $stamp "
        "WHERE group_id = $group_id AND uuid IN $ids;",
        group_id=org,
        ids=tied_ids,
        stamp=stamp,
    )
    await runtime.client.execute_query(
        "UPDATE entity SET updated_at = NONE WHERE group_id = $group_id AND uuid IN $ids;",
        group_id=org,
        ids=undated_ids,
    )
    edges = [
        Relationship(
            id=f"walk-edge-{index:05d}",
            source_id=left.id,
            target_id=right.id,
            relationship_type=RelationshipType.RELATED_TO,
            created_at=base + timedelta(seconds=index // 400),
        )
        for index, (left, right) in enumerate(pairwise(entities))
    ]
    await runtime.relationship_manager.create_direct_bulk(edges)

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
    return runtime, entities, edges, statements


async def test_entity_walk_matches_offset_order_without_offset_pages(enumerated_graph) -> None:
    runtime, entities, _edges, statements = enumerated_graph
    org = runtime.client.group_id
    expected = await runtime.entity_manager.list_all(limit=10_000, include_archived=True)
    statements.clear()

    walked = await _list_all_entities(runtime.client, org, batch_size=PAGE)

    assert [entity.id for entity in walked] == [entity.id for entity in expected]
    assert {entity.id for entity in entities} <= {entity.id for entity in walked}
    assert all("START" not in statement for statement in statements)
    # Dated pages, the tie group, the undated tail and the terminating short
    # page: each is one round trip.
    assert len(statements) <= len(walked) // PAGE + 3
    assert walked[-1].id in {entity.id for entity in entities[DATED + TIED :]}


async def test_entity_walk_honours_the_item_cap(enumerated_graph) -> None:
    runtime, _entities, _edges, statements = enumerated_graph
    org = runtime.client.group_id
    expected = await runtime.entity_manager.list_all(limit=10_000, include_archived=True)
    statements.clear()

    walked = await _list_all_entities(runtime.client, org, batch_size=PAGE, max_items=1500)

    assert [entity.id for entity in walked] == [entity.id for entity in expected[:1500]]
    assert len(statements) == 2


async def test_relationship_walk_reads_legacy_metadata_once(enumerated_graph) -> None:
    runtime, _entities, edges, statements = enumerated_graph
    org = runtime.client.group_id
    expected = await runtime.relationship_manager.list_all(limit=10_000)
    statements.clear()

    walked = await _list_all_relationships(runtime.client, org, batch_size=PAGE)

    assert [edge.id for edge in walked] == [edge.id for edge in expected]
    assert {edge.id for edge in walked} == {edge.id for edge in edges}
    assert all("START" not in statement for statement in statements)
    legacy = [statement for statement in statements if "GROUP BY metadata" in statement]
    assert len(legacy) == 1
    assert len(statements) == 1 + len(edges) // PAGE + 1
    assert all("fact_embedding" not in edge.metadata for edge in walked)


async def test_relationship_walk_decodes_edges_as_the_validation_snapshot_does(
    enumerated_graph,
) -> None:
    from sibyl_core.services.graph_community_snapshot import _current_graph_relationships
    from sibyl_core.services.graph_view_availability import _edge_evidence

    runtime, _entities, _edges, _statements = enumerated_graph
    org = runtime.client.group_id

    walked = await _list_all_relationships(runtime.client, org, batch_size=PAGE, max_items=5)
    refreshed = await _current_graph_relationships(runtime.client, org, [e.id for e in walked])

    assert [_edge_evidence(edge) for edge in walked] == [
        _edge_evidence(refreshed[edge.id]) for edge in walked
    ]


async def test_server_dialect_binds_cursors_as_native_datetimes(monkeypatch) -> None:
    """A networked engine seeks the index for a datetime parameter, never a cast."""
    from surrealdb.data.types.datetime import Datetime

    client = SurrealGraphClient(group_id="org-dialect", url="ws://127.0.0.1:1/rpc")
    pages = [
        [
            {
                "uuid": f"row-{index}",
                "name": f"row {index}",
                "entity_type": "topic",
                "group_id": "org-dialect",
                "updated_at": "2026-01-01T00:00:00Z",
                "updated_at_text": "2026-01-01T00:00:00.000000123Z",
                "created_at_text": "2026-01-01T00:00:00.000000123Z",
            }
            for index in range(2)
        ],
        [],
        [],
        [],
    ]
    calls: list[tuple[str, dict]] = []

    async def execute_query(query: str, **params):
        calls.append((query, params))
        return pages.pop(0)

    async def execute_query_batch(query: str, **params):
        calls.append((query, params))
        return [pages.pop(0), pages.pop(0)]

    monkeypatch.setattr(client, "execute_query", execute_query)
    monkeypatch.setattr(client, "execute_query_batch", execute_query_batch)

    walked = await _list_all_entities(client, "org-dialect", batch_size=2)

    assert [entity.id for entity in walked] == ["row-0", "row-1"]
    _first, second, undated = calls
    assert all("START" not in query for query, _ in calls)
    assert "updated_at IS NONE" in undated[0]
    assert "updated_at < $updated_at" in second[0]
    assert "<datetime>" not in second[0]
    assert isinstance(second[1]["updated_at"], Datetime)
    assert second[1]["updated_at"].dt == "2026-01-01T00:00:00.000000123Z"
    assert second[1]["uuid"] == "row-1"
