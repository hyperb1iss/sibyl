"""Aggregate reads a task create runs on every write.

Creating a task looks up the project's tag vocabulary; the answer comes from
the database without reading task rows into Python.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from sibyl_core.models.entities import Entity, EntityType
from sibyl_core.services.graph import EntityManager, SurrealGraphClient, prepare_graph_schema

GROUP = "work-item-aggregates-org"


@pytest.fixture
async def entities() -> AsyncIterator[EntityManager]:
    client = SurrealGraphClient(group_id=GROUP, url="memory://")
    await client.connect()
    try:
        await prepare_graph_schema(client)
        yield EntityManager(client, group_id=GROUP)
    finally:
        await client.close()


async def _add(
    entities: EntityManager,
    uuid: str,
    entity_type: EntityType,
    **metadata: object,
) -> None:
    await entities.create_direct(
        Entity(id=uuid, name=uuid, entity_type=entity_type, metadata=dict(metadata)),
        generate_embedding=False,
    )


async def test_project_task_tags_unions_the_projects_task_tags(
    entities: EntityManager,
) -> None:
    await _add(entities, "task-a", EntityType.TASK, project_id="proj-1", tags=["API", "backend"])
    await _add(entities, "task-b", EntityType.TASK, project_id="proj-1", tags=["backend", "Ops"])
    await _add(
        entities,
        "task-archived",
        EntityType.TASK,
        project_id="proj-1",
        tags=["legacy"],
        status="archived",
    )
    await _add(entities, "task-untagged", EntityType.TASK, project_id="proj-1")
    await _add(entities, "task-other", EntityType.TASK, project_id="proj-2", tags=["elsewhere"])
    await _add(entities, "note-same", EntityType.NOTE, project_id="proj-1", tags=["notes-only"])

    assert await entities.project_task_tags("proj-1") == ["api", "backend", "legacy", "ops"]
    assert await entities.project_task_tags("proj-2") == ["elsewhere"]
    assert await entities.project_task_tags("proj-empty") == []


async def test_project_task_tags_prefer_attributes_like_entity_reads(
    entities: EntityManager,
) -> None:
    await _add(entities, "task-a", EntityType.TASK, project_id="proj-1", tags=["Kept"])
    await entities._client.execute_query("UPDATE entity SET tags = NONE WHERE uuid = 'task-a';")

    assert (await entities.get("task-a")).metadata["tags"] == ["Kept"]
    assert await entities.project_task_tags("proj-1") == ["kept"]
