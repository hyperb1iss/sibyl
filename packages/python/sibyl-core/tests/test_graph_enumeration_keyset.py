"""Whole-graph enumeration walks the indexes by keyset, never by offset."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from uuid import uuid4

import pytest
from surrealdb.data.types.datetime import Datetime

from sibyl_core.backends.surreal.url_schemes import is_embedded_surreal_url
from sibyl_core.models.entities import Entity, EntityType, Relationship, RelationshipType
from sibyl_core.services.graph_client import SurrealGraphClient, prepare_graph_schema
from sibyl_core.services.graph_common import normalize_graph_records
from sibyl_core.services.graph_community_managers import (
    _list_all_entities,
    _list_all_relationships,
)
from tests.test_reflection_identity import content_store as content_store
from tests.test_reflection_identity import runtime as runtime

PAGE = 1000
DATED = 1250
TIED = 1100
UNDATED = 300
UNDATED_IDS = {f"walk-{index:05d}" for index in range(DATED + TIED, DATED + TIED + UNDATED)}


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
    runtime, _entities, _edges, statements = enumerated_graph
    org = runtime.client.group_id
    expected = await runtime.entity_manager.list_all(limit=10_000, include_archived=True)
    statements.clear()

    walked = await _list_all_entities(runtime.client, org, batch_size=PAGE)

    walked_ids = [entity.id for entity in walked]
    assert len(walked_ids) == len(set(walked_ids))
    assert set(walked_ids) == {entity.id for entity in expected}
    # Dated rows first, newest first; a tie group may be emitted in its own
    # order, and the undated tail comes last.
    dated = [entity for entity in walked if entity.id not in UNDATED_IDS]
    assert [entity.id for entity in walked[len(dated) :]] and all(
        entity.id in UNDATED_IDS for entity in walked[len(dated) :]
    )
    stamps = [entity.updated_at for entity in dated]
    assert stamps == sorted(stamps, reverse=True)
    assert all("START" not in statement for statement in statements)
    # Range pages, the tie group drains, the undated tail and the terminating
    # short page: each is one round trip.
    assert len(statements) <= len(walked) // PAGE + 4


async def test_entity_walk_honours_the_item_cap(enumerated_graph) -> None:
    runtime, _entities, _edges, statements = enumerated_graph
    org = runtime.client.group_id
    expected = await runtime.entity_manager.list_all(limit=10_000, include_archived=True)
    statements.clear()

    walked = await _list_all_entities(runtime.client, org, batch_size=PAGE, max_items=1500)

    assert len(walked) == 1500
    assert len({entity.id for entity in walked}) == 1500
    assert {entity.id for entity in walked} <= {entity.id for entity in expected}
    assert len(statements) <= 3


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
    enumerated_graph, content_store
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
    # The page ended inside a group: its drain seeks the group by uuid and the
    # range below it seeks the index, both on a native datetime parameter.
    assert "updated_at = $updated_at" in second[0] and "ORDER BY uuid DESC" in second[0]
    assert "updated_at < $updated_at" in second[0]
    assert "<datetime>" not in second[0] and "created_at <" not in second[0]
    assert isinstance(second[1]["updated_at"], Datetime)
    assert second[1]["updated_at"].dt == "2026-01-01T00:00:00.000000123Z"


def _iso(stamp: datetime, nanos: int = 0) -> str:
    return f"{stamp.strftime('%Y-%m-%dT%H:%M:%S.%f')}{nanos:03d}Z"


@pytest.fixture(params=["embedded", "live"])
async def tie_graph(request) -> AsyncIterator[tuple[SurrealGraphClient, str, list[str]]]:
    """Whole-second and fractional stamps mixed inside one updated_at group.

    The live variant runs against a SurrealDB server (the engine whose index
    orders a whole-second datetime ahead of fractional ones in the same
    second) when SIBYL_LIVE_SURREAL_TESTS=1 and SIBYL_SURREAL_URL name one.
    """
    group_id = f"walk-tie-{uuid4().hex[:10]}"
    if request.param == "embedded":
        client = SurrealGraphClient(group_id=group_id, url="memory://")
    else:
        if os.environ.get("SIBYL_LIVE_SURREAL_TESTS") != "1":
            pytest.skip("live SurrealDB tests are disabled")
        url = os.environ.get("SIBYL_SURREAL_URL", "")
        if not url or is_embedded_surreal_url(url):
            pytest.skip("live SurrealDB tests require SIBYL_SURREAL_URL to point at a server")
        client = SurrealGraphClient(
            group_id=group_id,
            url=url,
            username=os.environ.get("SIBYL_SURREAL_USERNAME", "root"),
            password=os.environ.get("SIBYL_SURREAL_PASSWORD", "root"),
            namespace_prefix="verify_",
            database="graph",
            pool_size=2,
        )
    try:
        await prepare_graph_schema(client)
        base = datetime(2026, 1, 1, tzinfo=UTC)
        day = timedelta(days=1)
        rows: list[dict[str, object]] = []

        def add(uuid: str, updated: str | None, created: str) -> None:
            rows.append(
                {
                    "uuid": uuid,
                    "updated_at": Datetime(updated) if updated else None,
                    "created_at": Datetime(created),
                }
            )

        # The verifier's shape: one whole-second row among fractional ones in
        # a single updated_at group, then a second group of the same shape.
        add("tie-a-whole", _iso(base), _iso(base - day))
        for index in range(1, 6):
            add(
                f"tie-a-micro-{index}",
                _iso(base),
                (base - day + timedelta(microseconds=index)).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            )
        add("tie-b-whole", _iso(base - day), _iso(base - 2 * day))
        for index in range(1, 4):
            add(f"tie-b-nano-{index}", _iso(base - day), _iso(base - 2 * day, nanos=index))
        add("older", _iso(base - 5 * day, nanos=7), _iso(base - 5 * day, nanos=7))
        add("undated-whole", None, _iso(base - 3 * day))
        for index in range(1, 4):
            add(
                f"undated-micro-{index}",
                None,
                (base - 3 * day + timedelta(microseconds=index)).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            )
        await client.execute_query(
            "FOR $row IN $rows { CREATE entity CONTENT { uuid: $row.uuid, name: $row.uuid, "
            "entity_type: 'topic', group_id: $group_id, created_at: $row.created_at, "
            "updated_at: $row.updated_at, attributes: {} }; };",
            rows=rows,
            group_id=group_id,
        )
        listed = normalize_graph_records(
            await client.execute_query(
                "SELECT uuid, updated_at, created_at FROM entity WHERE group_id = $group_id "
                "ORDER BY updated_at DESC, created_at DESC, uuid DESC;",
                group_id=group_id,
            )
        )
        assert len(listed) == len(rows)
        yield client, group_id, [str(row["uuid"]) for row in listed]
    finally:
        if request.param == "live":
            await client.execute_query(f"REMOVE NAMESPACE IF EXISTS {client.namespace};")
        await client.close()


@pytest.mark.parametrize("page", [1, 2, 3, 7])
async def test_tie_groups_with_mixed_precision_walk_without_skips_or_duplicates(
    tie_graph, page: int
) -> None:
    client, group_id, engine_order = tie_graph

    walked = [entity.id for entity in await _list_all_entities(client, group_id, batch_size=page)]

    assert len(walked) == len(set(walked)), walked
    assert set(walked) == set(engine_order), walked
    assert walked[:6] and set(walked[:6]) == {row for row in engine_order[:6]}
    assert walked[-4:] and set(walked[-4:]) == set(engine_order[-4:])
