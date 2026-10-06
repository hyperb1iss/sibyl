"""Project progress is one aggregate read and one bookkeeping merge, no edit."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from sibyl_core.backends.surreal.records import normalize_records
from sibyl_core.models.entities import Entity, EntityType
from sibyl_core.services.graph import EntityManager, SurrealGraphClient, prepare_graph_schema


class _RecordingGraphClient:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def execute_query(self, query: str, **params: Any) -> Any:
        self.calls.append((query, params))
        return self.rows


@pytest.mark.asyncio
async def test_count_by_status_is_a_single_aggregate_statement() -> None:
    client = _RecordingGraphClient(
        [
            {"status": "done", "total": 2},
            {"status": "doing", "total": 1},
            {"status": None, "total": 1},
            {"status": "", "total": 1},
        ]
    )
    manager = EntityManager(client, group_id="org-counts")  # type: ignore[arg-type]

    counts = await manager.count_by_status(EntityType.TASK, project_id="proj-1")

    assert counts == {"done": 2, "doing": 1, "todo": 2}
    assert len(client.calls) == 1, "one round trip, no row paging"
    query, params = client.calls[0]
    assert "count() AS total" in query
    assert "GROUP BY status" in query
    assert "SELECT *" not in query
    assert "LIMIT" not in query
    assert "entity_type = $entity_type" in query
    assert "project_id = $project_id" in query
    assert params == {
        "group_id": "org-counts",
        "entity_type": "task",
        "project_id": "proj-1",
    }


def _task(entity_id: str, *, status: str, project_id: str, epic_id: str | None = None) -> Entity:
    metadata: dict[str, object] = {"status": status, "project_id": project_id}
    if epic_id:
        metadata["epic_id"] = epic_id
    return Entity(
        id=entity_id,
        entity_type=EntityType.TASK,
        name=entity_id,
        organization_id="org-bookkeeping",
        metadata=metadata,
    )


@pytest.mark.asyncio
async def test_count_by_status_agrees_with_list_by_type_on_the_embedded_engine() -> None:
    client = SurrealGraphClient(group_id="org-bookkeeping", url="memory://")
    try:
        await prepare_graph_schema(client)
        manager = EntityManager(client, group_id=client.group_id)
        for entity in (
            _task("task-done", status="done", project_id="proj-a", epic_id="epic-a"),
            _task("task-doing", status="doing", project_id="proj-a", epic_id="epic-a"),
            _task("task-archived", status="archived", project_id="proj-a"),
            _task("task-todo", status="todo", project_id="proj-a"),
            _task("task-elsewhere", status="done", project_id="proj-b", epic_id="epic-b"),
        ):
            await manager.create_direct(entity)

        project_counts = await manager.count_by_status(EntityType.TASK, project_id="proj-a")
        listed = await manager.list_by_type(
            EntityType.TASK, project_id="proj-a", limit=10_000, include_archived=True
        )
        assert project_counts == {"done": 1, "doing": 1, "archived": 1, "todo": 1}
        assert sum(project_counts.values()) == len(listed) == 4

        epic_counts = await manager.count_by_status(EntityType.TASK, epic_id="epic-a")
        assert epic_counts == {"done": 1, "doing": 1}
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_write_bookkeeping_merges_fields_and_leaves_revision_alone() -> None:
    client = SurrealGraphClient(group_id="org-bookkeeping-rev", url="memory://")
    try:
        await prepare_graph_schema(client)
        manager = EntityManager(client, group_id=client.group_id)
        await manager.create_direct(
            Entity(
                id="proj-hot",
                entity_type=EntityType.PROJECT,
                name="Hot project",
                organization_id=client.group_id,
                metadata={"status": "active", "description_note": "kept"},
            )
        )
        before = await manager.get("proj-hot")
        stamped_at = datetime(2026, 10, 6, 15, 0, tzinfo=UTC).isoformat()

        await manager.write_bookkeeping(
            "proj-hot",
            {
                "last_activity_at": stamped_at,
                "total_tasks": 3,
                "completed_tasks": 1,
                "in_progress_tasks": 1,
            },
        )

        after = await manager.get("proj-hot")
        assert after.revision == before.revision, "bookkeeping is not an edit"
        assert after.metadata["last_activity_at"] == stamped_at
        assert after.metadata["total_tasks"] == 3
        assert after.metadata["completed_tasks"] == 1
        assert after.metadata["in_progress_tasks"] == 1
        assert after.metadata["description_note"] == "kept"
        assert after.metadata["status"] == "active"

        rows = normalize_records(
            await client.execute_query(
                "SELECT revision, updated_at FROM entity WHERE uuid = 'proj-hot' LIMIT 1;"
            )
        )
        assert rows[0]["revision"] == before.revision
    finally:
        await client.close()
