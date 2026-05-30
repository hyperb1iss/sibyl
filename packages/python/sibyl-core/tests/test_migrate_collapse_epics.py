"""Tests for the W14 collapse-epics data migration (memory:// only).

These run the real migration SQL against an ephemeral in-memory SurrealDB graph,
never a live org, so the round-trip (status mapping, parent repointing, marker
storage, reversibility, idempotency, org isolation) is exercised end to end.
"""

from __future__ import annotations

import json
import uuid

import pytest

from sibyl_core.migrate.collapse_epics import (
    MIGRATED_FROM_EPIC_STATUS_KEY,
    collapse_epics_in_org,
)
from sibyl_core.models.entities import EntityType
from sibyl_core.models.tasks import Epic, EpicStatus, Task, TaskStatus
from sibyl_core.services.graph import (
    _ENTITY_BULK_UPSERT_QUERY,
    EntityManager,
    SurrealGraphClient,
    _entity_record,
    prepare_graph_schema,
)


async def _make_graph(group_id: str) -> tuple[SurrealGraphClient, EntityManager]:
    client = SurrealGraphClient(group_id=group_id, url="memory://")
    await client.connect()
    await prepare_graph_schema(client)
    return client, EntityManager(client, group_id=group_id)


def _epic(*, title: str, status: EpicStatus, project_id: str = "project-1") -> Epic:
    return Epic(
        id=f"epic_{uuid.uuid4().hex[:8]}",
        name=title,
        title=title,
        project_id=project_id,
        status=status,
    )


def _task(
    *,
    title: str,
    status: TaskStatus = TaskStatus.TODO,
    epic_id: str | None = None,
    project_id: str = "project-1",
) -> Task:
    return Task(
        id=f"task_{uuid.uuid4().hex[:8]}",
        name=title,
        title=title,
        status=status,
        epic_id=epic_id,
        project_id=project_id,
    )


async def _create_legacy_epic_child(
    client: SurrealGraphClient,
    task: Task,
    *,
    group_id: str,
) -> None:
    record = _entity_record(task, group_id=group_id)
    record["parent_task_id"] = None
    attributes = dict(record["attributes"])
    attributes.pop("parent_task_id", None)
    raw_metadata = attributes.get("metadata")
    if isinstance(raw_metadata, str):
        metadata = json.loads(raw_metadata)
        metadata.pop("parent_task_id", None)
        attributes["metadata"] = json.dumps(metadata)
    record["attributes"] = attributes
    await client.execute_query(_ENTITY_BULK_UPSERT_QUERY, rows=[record])


@pytest.mark.asyncio
async def test_forward_converts_epic_and_repoints_children() -> None:
    group_id = "00000000-0000-0000-0000-0000000000a1"
    client, manager = await _make_graph(group_id)

    epic = _epic(title="Launch", status=EpicStatus.IN_PROGRESS)
    await manager.create_direct(epic)
    child_a = _task(title="Child A", status=TaskStatus.DOING, epic_id=epic.id)
    child_b = _task(title="Child B", status=TaskStatus.TODO, epic_id=epic.id)
    await _create_legacy_epic_child(client, child_a, group_id=group_id)
    await _create_legacy_epic_child(client, child_b, group_id=group_id)

    result = await collapse_epics_in_org(client, group_id=group_id, dry_run=False)

    assert result.success is True
    assert result.epics_converted == 1
    assert result.children_repointed == 2

    converted = await manager.get(epic.id)
    assert converted.entity_type is EntityType.TASK
    meta = converted.metadata or {}
    # IN_PROGRESS -> DOING, with the original epic status stashed for reversal.
    assert meta.get("status") == TaskStatus.DOING.value
    assert meta.get(MIGRATED_FROM_EPIC_STATUS_KEY) == EpicStatus.IN_PROGRESS.value

    for child_id in (child_a.id, child_b.id):
        child = await manager.get(child_id)
        child_meta = child.metadata or {}
        assert child_meta.get("parent_task_id") == epic.id
        # epic_id stays populated until U5 so the flip is reversible.
        assert child_meta.get("epic_id") == epic.id

    await client.close()


@pytest.mark.asyncio
async def test_forward_maps_every_epic_status() -> None:
    group_id = "00000000-0000-0000-0000-0000000000a2"
    client, manager = await _make_graph(group_id)

    cases = {
        EpicStatus.PLANNING: TaskStatus.TODO,
        EpicStatus.IN_PROGRESS: TaskStatus.DOING,
        EpicStatus.BLOCKED: TaskStatus.BLOCKED,
        EpicStatus.COMPLETED: TaskStatus.DONE,
        EpicStatus.ARCHIVED: TaskStatus.ARCHIVED,
    }
    epics = {}
    for epic_status in cases:
        epic = _epic(title=f"E-{epic_status.value}", status=epic_status)
        await manager.create_direct(epic)
        epics[epic_status] = epic

    result = await collapse_epics_in_org(client, group_id=group_id, dry_run=False)
    assert result.success is True
    assert result.epics_converted == len(cases)

    for epic_status, expected_task_status in cases.items():
        converted = await manager.get(epics[epic_status].id)
        assert converted.entity_type is EntityType.TASK
        assert (converted.metadata or {}).get("status") == expected_task_status.value
        assert (converted.metadata or {}).get(MIGRATED_FROM_EPIC_STATUS_KEY) == epic_status.value

    await client.close()


@pytest.mark.asyncio
async def test_forward_is_idempotent() -> None:
    group_id = "00000000-0000-0000-0000-0000000000a3"
    client, manager = await _make_graph(group_id)

    epic = _epic(title="Launch", status=EpicStatus.COMPLETED)
    await manager.create_direct(epic)
    child = _task(title="Child", status=TaskStatus.DONE, epic_id=epic.id)
    await _create_legacy_epic_child(client, child, group_id=group_id)

    first = await collapse_epics_in_org(client, group_id=group_id, dry_run=False)
    assert first.epics_converted == 1
    assert first.children_repointed == 1

    second = await collapse_epics_in_org(client, group_id=group_id, dry_run=False)
    assert second.success is True
    # Nothing left to convert; the child already points at its parent.
    assert second.epics_converted == 0
    assert second.children_repointed == 0

    converted = await manager.get(epic.id)
    assert converted.entity_type is EntityType.TASK
    assert (converted.metadata or {}).get("status") == TaskStatus.DONE.value

    await client.close()


@pytest.mark.asyncio
async def test_reverse_restores_epic_and_clears_parent() -> None:
    group_id = "00000000-0000-0000-0000-0000000000a4"
    client, manager = await _make_graph(group_id)

    epic = _epic(title="Launch", status=EpicStatus.BLOCKED)
    await manager.create_direct(epic)
    child = _task(title="Child", status=TaskStatus.BLOCKED, epic_id=epic.id)
    await _create_legacy_epic_child(client, child, group_id=group_id)

    forward = await collapse_epics_in_org(client, group_id=group_id, dry_run=False)
    assert forward.epics_converted == 1

    reverse = await collapse_epics_in_org(client, group_id=group_id, reverse=True, dry_run=False)
    assert reverse.success is True
    assert reverse.epics_converted == 1
    assert reverse.children_repointed == 1

    restored = await manager.get(epic.id)
    assert restored.entity_type is EntityType.EPIC
    meta = restored.metadata or {}
    # Original status restored, marker removed.
    assert meta.get("status") == EpicStatus.BLOCKED.value
    assert MIGRATED_FROM_EPIC_STATUS_KEY not in meta

    child_after = await manager.get(child.id)
    child_meta = child_after.metadata or {}
    assert not child_meta.get("parent_task_id")
    # epic_id was never cleared, so the linkage is fully restored.
    assert child_meta.get("epic_id") == epic.id

    await client.close()


@pytest.mark.asyncio
async def test_reverse_is_idempotent_on_unconverted_graph() -> None:
    group_id = "00000000-0000-0000-0000-0000000000a5"
    client, manager = await _make_graph(group_id)

    # A plain epic + child that were never collapsed: reverse is a no-op.
    epic = _epic(title="Untouched", status=EpicStatus.PLANNING)
    await manager.create_direct(epic)
    await manager.create_direct(_task(title="Child", epic_id=epic.id))

    result = await collapse_epics_in_org(client, group_id=group_id, reverse=True, dry_run=False)
    assert result.success is True
    assert result.epics_converted == 0
    assert result.children_repointed == 0

    untouched = await manager.get(epic.id)
    assert untouched.entity_type is EntityType.EPIC

    await client.close()


@pytest.mark.asyncio
async def test_dry_run_writes_nothing() -> None:
    group_id = "00000000-0000-0000-0000-0000000000a6"
    client, manager = await _make_graph(group_id)

    epic = _epic(title="Launch", status=EpicStatus.PLANNING)
    await manager.create_direct(epic)
    child = _task(title="Child", epic_id=epic.id)
    await _create_legacy_epic_child(client, child, group_id=group_id)

    result = await collapse_epics_in_org(client, group_id=group_id, dry_run=True)
    assert result.success is True
    # Counts are reported...
    assert result.epics_converted == 1
    assert result.children_repointed == 1

    # ...but nothing changed on disk.
    epic_after = await manager.get(epic.id)
    assert epic_after.entity_type is EntityType.EPIC
    assert (epic_after.metadata or {}).get("status") == EpicStatus.PLANNING.value
    assert MIGRATED_FROM_EPIC_STATUS_KEY not in (epic_after.metadata or {})

    child_after = await manager.get(child.id)
    assert not (child_after.metadata or {}).get("parent_task_id")

    await client.close()


@pytest.mark.asyncio
async def test_child_without_epic_is_untouched() -> None:
    group_id = "00000000-0000-0000-0000-0000000000a7"
    client, manager = await _make_graph(group_id)

    epic = _epic(title="Launch", status=EpicStatus.IN_PROGRESS)
    await manager.create_direct(epic)
    orphan = _task(title="Orphan", status=TaskStatus.TODO, epic_id=None)
    await manager.create_direct(orphan)

    result = await collapse_epics_in_org(client, group_id=group_id, dry_run=False)
    assert result.success is True
    assert result.epics_converted == 1
    assert result.children_repointed == 0

    orphan_after = await manager.get(orphan.id)
    orphan_meta = orphan_after.metadata or {}
    assert not orphan_meta.get("parent_task_id")
    assert not orphan_meta.get("epic_id")
    assert orphan_after.entity_type is EntityType.TASK

    await client.close()


@pytest.mark.asyncio
async def test_org_isolation() -> None:
    group_a = "00000000-0000-0000-0000-0000000000b1"
    group_b = "00000000-0000-0000-0000-0000000000b2"
    client_a, manager_a = await _make_graph(group_a)
    client_b, manager_b = await _make_graph(group_b)

    epic_a = _epic(title="Org A epic", status=EpicStatus.IN_PROGRESS)
    await manager_a.create_direct(epic_a)
    epic_b = _epic(title="Org B epic", status=EpicStatus.IN_PROGRESS)
    await manager_b.create_direct(epic_b)

    # Run the migration only against org A.
    result = await collapse_epics_in_org(client_a, group_id=group_a, dry_run=False)
    assert result.success is True
    assert result.epics_converted == 1

    converted_a = await manager_a.get(epic_a.id)
    assert converted_a.entity_type is EntityType.TASK

    # Org B's epic must be completely untouched.
    untouched_b = await manager_b.get(epic_b.id)
    assert untouched_b.entity_type is EntityType.EPIC
    assert (untouched_b.metadata or {}).get("status") == EpicStatus.IN_PROGRESS.value
    assert MIGRATED_FROM_EPIC_STATUS_KEY not in (untouched_b.metadata or {})

    await client_a.close()
    await client_b.close()


@pytest.mark.asyncio
async def test_round_trip_preserves_epic_only_metadata() -> None:
    """Forward then reverse must not lose epic-only fields or custom metadata."""
    group_id = "00000000-0000-0000-0000-0000000000b3"
    client, manager = await _make_graph(group_id)

    epic = Epic(
        id=f"epic_{uuid.uuid4().hex[:8]}",
        name="Launch",
        title="Launch",
        project_id="project-7",
        status=EpicStatus.COMPLETED,
        total_tasks=4,
        completed_tasks=4,
    )
    epic.metadata["custom_key"] = "keep-me"
    await manager.create_direct(epic)

    await collapse_epics_in_org(client, group_id=group_id, dry_run=False)
    await collapse_epics_in_org(client, group_id=group_id, reverse=True, dry_run=False)

    restored = await manager.get(epic.id)
    assert restored.entity_type is EntityType.EPIC
    meta = restored.metadata or {}
    assert meta.get("status") == EpicStatus.COMPLETED.value
    assert meta.get("project_id") == "project-7"
    assert meta.get("custom_key") == "keep-me"
    assert int(meta.get("total_tasks") or 0) == 4
    assert MIGRATED_FROM_EPIC_STATUS_KEY not in meta

    await client.close()
