"""Work-item listings filter on exact columns so the compound indexes serve them.

The former `(column = $value OR column IS NONE OR column = '')` branches could
not bind the project or parent column of the (entity_type, project_id, ...)
indexes, so every project-scoped task list walked the whole type in updated
order and filtered in memory, and the Python recheck restarted every page at
the first row. Migration 36 promotes any attribute-only legacy values into
the columns so the exact predicates lose nothing.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

import pytest

from sibyl_core.backends.surreal.schema import (
    GRAPH_SCHEMA_MIGRATIONS,
    _graph_schema_migrations,
)
from sibyl_core.backends.surreal.schema_version import GRAPH_SCHEMA_CURRENT_VERSION
from sibyl_core.backends.surreal.schema_work_item_columns import (
    canonicalize_entity_work_item_columns,
    missing_work_item_columns,
)
from sibyl_core.models.entities import Entity, EntityType
from sibyl_core.services.graph_client import SurrealGraphClient, prepare_graph_schema
from sibyl_core.services.graph_entities import EntityManager

_OR_MISSING = re.compile(r"\(\s*\w+ (?:= \$\w+|IN \$\w+) OR \(\w+ IS NONE OR \w+ = ''\)\)")


class _RecordingClient:
    _url = "memory://"

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def execute_query(self, query: str, **params: object) -> list[object]:
        self.calls.append((query, dict(params)))
        return []


async def test_project_task_list_uses_exact_predicates_and_real_offsets() -> None:
    client = _RecordingClient()
    manager = EntityManager(client, group_id="org")  # type: ignore[arg-type]

    await manager.list_by_type(
        EntityType.TASK,
        project_id="project_a",
        parent_task_id="epic_a",
        status="doing,blocked",
        priority="high",
        feature="Search",
        offset=50,
        limit=50,
    )

    query, params = client.calls[0]
    assert not _OR_MISSING.search(query), query
    for clause in (
        "project_id = $project_id",
        "parent_task_id = $parent_task_id",
        "status IN $status_values",
        "priority IN $priority_values",
        "feature = $feature",
        "(status IS NONE OR status = '' OR status != 'archived')",
    ):
        assert clause in query, clause
    assert params["project_id"] == "project_a"
    assert params["status_values"] == ["doing", "blocked"]
    assert params["feature"] == "search"
    assert (params["offset"], params["limit"]) == (50, 50)


async def test_tag_filters_still_restart_from_the_first_row() -> None:
    client = _RecordingClient()
    manager = EntityManager(client, group_id="org")  # type: ignore[arg-type]

    await manager.list_by_type(EntityType.TASK, tags=["sidequest"], offset=50, limit=50)

    _, params = client.calls[0]
    assert (params["offset"], params["limit"]) == (0, 100)


def test_migration_36_promotes_attribute_only_work_item_columns() -> None:
    assert GRAPH_SCHEMA_CURRENT_VERSION >= 36
    migration = next(item for item in GRAPH_SCHEMA_MIGRATIONS if item.version == 36)
    assert migration.name == "entity_work_item_column_canonicalization"
    columns = []
    for statement in migration.statements:
        match = re.match(r"UPDATE entity SET (\w+) = attributes\.\1\s+WHERE", statement)
        assert match is not None, statement
        columns.append(match.group(1))
        assert f"type::is::string(attributes.{match.group(1)})" in statement
    assert columns == ["project_id", "epic_id", "status", "priority", "complexity", "feature"]
    assert migration.action is canonicalize_entity_work_item_columns
    rendered = next(
        item for item in _graph_schema_migrations(url="ws://example/rpc") if item.version == 36
    )
    assert all("type::is_string(" in statement for statement in rendered.statements)


@pytest.fixture
async def graph_client() -> AsyncIterator[SurrealGraphClient]:
    client = SurrealGraphClient(group_id=f"tasks-{uuid4().hex}", url="memory://")
    try:
        await prepare_graph_schema(client)
        yield client
    finally:
        await client.close()


async def test_exact_predicates_page_project_tasks_without_losing_rows(
    graph_client: SurrealGraphClient,
) -> None:
    manager = EntityManager(graph_client, group_id=graph_client.group_id)
    tasks = {
        "task_a_doing": {"project_id": "project_a", "status": "doing"},
        "task_b_doing": {"project_id": "project_b", "status": "doing"},
        "task_a_done": {"project_id": "project_a", "status": "done"},
        "task_a_archived": {"project_id": "project_a", "status": "archived"},
        "task_orphan": {"status": "doing"},
    }
    for task_id, metadata in tasks.items():
        await manager.create_direct(
            Entity(id=task_id, entity_type=EntityType.TASK, name=task_id, metadata=metadata),
            generate_embedding=False,
        )

    listed = await manager.list_by_type(EntityType.TASK, project_id="project_a")
    assert {entity.id for entity in listed} == {"task_a_doing", "task_a_done"}
    doing = await manager.list_by_type(EntityType.TASK, project_id="project_a", status="doing")
    assert [entity.id for entity in doing] == ["task_a_doing"]
    everything = await manager.list_by_type(
        EntityType.TASK, project_id="project_a", include_archived=True
    )
    assert {entity.id for entity in everything} == {
        "task_a_doing",
        "task_a_done",
        "task_a_archived",
    }
    first = await manager.list_by_type(EntityType.TASK, project_id="project_a", limit=1)
    second = await manager.list_by_type(EntityType.TASK, project_id="project_a", limit=1, offset=1)
    assert {first[0].id, second[0].id} == {"task_a_doing", "task_a_done"}
    assert first[0].id != second[0].id


def test_snapshot_values_fill_only_the_missing_columns() -> None:
    snapshot = '{"project_id": "project_a", "epic_id": "epic_a", "status": "doing", "tags": ["x"]}'
    row = {"uuid": "task", "snapshot": snapshot, "project_id": "project_b", "status": ""}
    assert missing_work_item_columns(row) == {
        "epic_id": "epic_a",
        "parent_task_id": "epic_a",
        "status": "doing",
    }
    assert missing_work_item_columns({"uuid": "task", "snapshot": "not json"}) == {}
    assert missing_work_item_columns({"uuid": "task", "snapshot": {"status": "done"}}) == {
        "status": "done"
    }


class _SnapshotClient:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.updates: list[tuple[str, dict[str, Any]]] = []

    async def execute_query(self, query: str, **params: object) -> list[object]:
        if query.startswith("UPDATE entity SET"):
            self.updates.append((query, dict(params)))
            return []
        cursor = str(params["cursor"])
        return [row for row in self.rows if row["uuid"] > cursor][: int(params["limit"])]  # type: ignore[arg-type]


async def test_canonicalization_action_updates_snapshot_only_rows_once() -> None:
    rows = [
        {"uuid": "task_a", "snapshot": '{"project_id": "project_a", "status": "doing"}'},
        {"uuid": "task_b", "snapshot": '{"status": "done"}', "status": "done"},
        {"uuid": "task_c", "snapshot": None},
    ]
    client = _SnapshotClient(rows)

    await canonicalize_entity_work_item_columns(client.execute_query)  # type: ignore[arg-type]

    assert client.updates == [
        (
            "UPDATE entity SET project_id = $project_id, status = $status WHERE uuid = $uuid;",
            {"uuid": "task_a", "project_id": "project_a", "status": "doing"},
        )
    ]
