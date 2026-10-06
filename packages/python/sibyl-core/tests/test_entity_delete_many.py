"""Deleting several entities at once takes their edges with them.

Passage retirement sweeps every index a memory could own, so it deletes up to
`MAX_PASSAGES_PER_SOURCE` ids per deleted row; one transaction for the range
replaces a round trip per index.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from sibyl_core.models.entities import Entity, EntityType, Relationship, RelationshipType
from sibyl_core.services.graph import (
    EntityManager,
    RelationshipManager,
    SurrealGraphClient,
    prepare_graph_schema,
)

GROUP = "delete-many-org"


@pytest.fixture
async def client() -> AsyncIterator[SurrealGraphClient]:
    client = SurrealGraphClient(group_id=GROUP, url="memory://")
    await client.connect()
    try:
        await prepare_graph_schema(client)
        yield client
    finally:
        await client.close()


async def test_delete_many_removes_rows_and_edges_and_reports_what_existed(
    client: SurrealGraphClient,
) -> None:
    entities = EntityManager(client, group_id=GROUP)
    relationships = RelationshipManager(client, group_id=GROUP)
    for uuid in ("note-a", "note-b", "note-kept"):
        await entities.create_direct(
            Entity(id=uuid, name=uuid, entity_type=EntityType.NOTE), generate_embedding=False
        )
    await relationships.create_direct_bulk(
        [
            Relationship(
                id="edge-a-kept",
                source_id="note-a",
                target_id="note-kept",
                relationship_type=RelationshipType.RELATED_TO,
            ),
            Relationship(
                id="edge-kept-b",
                source_id="note-kept",
                target_id="note-b",
                relationship_type=RelationshipType.RELATED_TO,
            ),
        ]
    )

    removed = await entities.delete_many(["note-a", "note-b", "note-missing", "note-a"])

    assert removed == {"note-a", "note-b"}
    rows = await client.execute_query(
        "SELECT uuid FROM entity WHERE group_id = $group;", group=GROUP
    )
    assert {row["uuid"] for row in rows} == {"note-kept"}
    edges = await client.execute_query(
        "SELECT uuid FROM relates_to WHERE group_id = $group;", group=GROUP
    )
    assert edges == []


async def test_delete_many_with_nothing_to_delete_touches_nothing(
    client: SurrealGraphClient,
) -> None:
    entities = EntityManager(client, group_id=GROUP)
    await entities.create_direct(
        Entity(id="note-kept", name="kept", entity_type=EntityType.NOTE), generate_embedding=False
    )

    assert await entities.delete_many([]) == set()
    assert await entities.delete_many(["note-missing"]) == set()
    rows = await client.execute_query(
        "SELECT uuid FROM entity WHERE group_id = $group;", group=GROUP
    )
    assert [row["uuid"] for row in rows] == ["note-kept"]
