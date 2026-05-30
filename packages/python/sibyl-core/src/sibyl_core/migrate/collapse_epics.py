"""W14 data migration: collapse standalone Epic entities into the task tree.

A task with children IS an epic (W14). U1 added ``parent_task_id`` + subtask
listing, U2 added the read-only epic projection. This module is U3: the in-place,
fully reversible data flip that converts stored ``epic`` entities into ``task``
entities and repoints their children onto ``parent_task_id``.

Design (approved):

Forward (``reverse=False``)
    For each ``entity_type == 'epic'`` row in an org, rewrite it in place to
    ``entity_type == 'task'`` (same record id), mapping ``EpicStatus`` onto
    ``TaskStatus``. The original epic status is stashed in metadata under
    ``migrated_from_epic_status`` so reversal is lossless and idempotency can
    detect already-converted records. Each child task (``epic_id`` pointing at
    the converted epic) gets ``parent_task_id = epic_id``; ``epic_id`` is kept
    populated so the linkage stays reversible until U5.

Reverse (``reverse=True``)
    For each task carrying the ``migrated_from_epic_status`` marker, flip
    ``entity_type`` back to ``epic``, restore the original status from the
    marker, and drop the marker. Children of those epics have ``parent_task_id``
    cleared; ``epic_id`` is still intact, so the epic linkage is fully restored.

Both directions are idempotent (``entity_type`` is the natural gate forward; the
marker is the gate in reverse) and atomic per org (the destructive writes go
through a single ``BEGIN..COMMIT`` via ``execute_query_raw`` + ``raise_on_error``,
org-scoped and ``$param``-bound).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import structlog

from sibyl_core.backends.surreal.records import normalize_records, raise_on_error
from sibyl_core.models.entities import Entity, EntityType
from sibyl_core.models.tasks import EpicStatus, TaskStatus
from sibyl_core.services.graph import (
    _ENTITY_BULK_UPSERT_QUERY,
    _entity_from_row,
    _entity_record,
)

if TYPE_CHECKING:
    from sibyl_core.services.graph import SurrealGraphClient

log = structlog.get_logger()

# Reversibility marker: stores the pre-migration EpicStatus on the converted
# task so reverse can restore it exactly and forward can skip it on re-run.
MIGRATED_FROM_EPIC_STATUS_KEY = "migrated_from_epic_status"

_COLLAPSE_PAGE_SIZE = 1000

# EpicStatus -> TaskStatus when an epic becomes a task (forward).
_EPIC_TO_TASK_STATUS: dict[EpicStatus, TaskStatus] = {
    EpicStatus.PLANNING: TaskStatus.TODO,
    EpicStatus.IN_PROGRESS: TaskStatus.DOING,
    EpicStatus.BLOCKED: TaskStatus.BLOCKED,
    EpicStatus.COMPLETED: TaskStatus.DONE,
    EpicStatus.ARCHIVED: TaskStatus.ARCHIVED,
}


@dataclass
class CollapseEpicsResult:
    """Outcome of a single-org collapse-epics run."""

    success: bool
    organization_id: str
    reverse: bool
    dry_run: bool
    epics_converted: int
    children_repointed: int
    duration_seconds: float
    errors: list[str] = field(default_factory=list)


async def collapse_epics_in_org(
    client: SurrealGraphClient,
    *,
    group_id: str,
    reverse: bool = False,
    dry_run: bool = True,
) -> CollapseEpicsResult:
    """Collapse epics into tasks (or restore them) for one organization.

    ``dry_run`` reports the counts that would change and writes nothing. The
    applied path wraps every record rewrite for the org in a single
    transaction, so a partial failure rolls back to the pre-migration state.
    """
    start_time = time.time()
    log.info(
        "collapse_epics_start",
        organization_id=group_id,
        reverse=reverse,
        dry_run=dry_run,
    )
    try:
        if reverse:
            records, epics_converted, children_repointed = await _build_reverse_records(
                client, group_id=group_id
            )
        else:
            records, epics_converted, children_repointed = await _build_forward_records(
                client, group_id=group_id
            )

        if not dry_run and records:
            await _apply_records(client, records, group_id=group_id, reverse=reverse)

        duration = time.time() - start_time
        log.info(
            "collapse_epics_complete",
            organization_id=group_id,
            reverse=reverse,
            dry_run=dry_run,
            epics_converted=epics_converted,
            children_repointed=children_repointed,
            duration=duration,
        )
        return CollapseEpicsResult(
            success=True,
            organization_id=group_id,
            reverse=reverse,
            dry_run=dry_run,
            epics_converted=epics_converted,
            children_repointed=children_repointed,
            duration_seconds=duration,
        )
    except Exception as exc:
        duration = time.time() - start_time
        log.exception(
            "collapse_epics_failed",
            organization_id=group_id,
            reverse=reverse,
            error=str(exc),
        )
        return CollapseEpicsResult(
            success=False,
            organization_id=group_id,
            reverse=reverse,
            dry_run=dry_run,
            epics_converted=0,
            children_repointed=0,
            duration_seconds=duration,
            errors=[str(exc)],
        )


async def _build_forward_records(
    client: SurrealGraphClient,
    *,
    group_id: str,
) -> tuple[list[dict[str, object]], int, int]:
    """Build the task-shaped records for converting epics + repointing children.

    Idempotency gate: only ``entity_type == 'epic'`` rows are read, so a row
    already converted to ``task`` is never seen again on a re-run.
    """
    epics = await _list_entities_by_type(client, group_id=group_id, entity_type=EntityType.EPIC)
    if not epics:
        return [], 0, 0

    records: list[dict[str, object]] = []
    epic_ids: list[str] = []
    for epic in epics:
        epic_ids.append(epic.id)
        records.append(_converted_task_record(epic, group_id=group_id))

    children = await _list_children_by_epic_ids(client, group_id=group_id, epic_ids=epic_ids)
    children_repointed = 0
    for child in children:
        metadata = dict(child.metadata or {})
        epic_id = metadata.get("epic_id")
        if not epic_id:
            continue
        # Idempotent: a child already pointing at its epic needs no rewrite.
        if metadata.get("parent_task_id") == epic_id:
            continue
        metadata["parent_task_id"] = epic_id
        records.append(_record_with_metadata(child, metadata, group_id=group_id))
        children_repointed += 1

    return records, len(epics), children_repointed


async def _build_reverse_records(
    client: SurrealGraphClient,
    *,
    group_id: str,
) -> tuple[list[dict[str, object]], int, int]:
    """Build the epic-shaped records for restoring converted tasks + children.

    Idempotency gate: only tasks carrying ``migrated_from_epic_status`` are
    restored, so a second reverse run is a no-op.
    """
    tasks = await _list_entities_by_type(client, group_id=group_id, entity_type=EntityType.TASK)
    converted = [task for task in tasks if (task.metadata or {}).get(MIGRATED_FROM_EPIC_STATUS_KEY)]
    if not converted:
        return [], 0, 0

    records: list[dict[str, object]] = []
    epic_ids: list[str] = []
    for task in converted:
        epic_ids.append(task.id)
        records.append(_restored_epic_record(task, group_id=group_id))

    children = await _list_children_by_epic_ids(client, group_id=group_id, epic_ids=epic_ids)
    children_repointed = 0
    for child in children:
        metadata = dict(child.metadata or {})
        # Idempotent: a child whose parent pointer is already cleared is skipped.
        if not metadata.get("parent_task_id"):
            continue
        metadata.pop("parent_task_id", None)
        records.append(_record_with_metadata(child, metadata, group_id=group_id))
        children_repointed += 1

    return records, len(converted), children_repointed


def _converted_task_record(epic: Entity, *, group_id: str) -> dict[str, object]:
    """Project a stored epic entity into a task record, stashing its old status."""
    metadata = dict(epic.metadata or {})
    original_status = str(metadata.get("status") or EpicStatus.PLANNING.value)
    epic_status = _coerce_epic_status(original_status)
    task_status = _EPIC_TO_TASK_STATUS[epic_status]

    metadata["status"] = task_status.value
    metadata[MIGRATED_FROM_EPIC_STATUS_KEY] = epic_status.value
    return _record_with_metadata(epic, metadata, entity_type=EntityType.TASK, group_id=group_id)


def _restored_epic_record(task: Entity, *, group_id: str) -> dict[str, object]:
    """Project a converted task back into an epic record, restoring its status."""
    metadata = dict(task.metadata or {})
    original_status = str(metadata.pop(MIGRATED_FROM_EPIC_STATUS_KEY, "") or "")
    metadata["status"] = _coerce_epic_status(original_status).value
    return _record_with_metadata(task, metadata, entity_type=EntityType.EPIC, group_id=group_id)


def _record_with_metadata(
    source: Entity,
    metadata: dict[str, object],
    *,
    entity_type: EntityType | None = None,
    group_id: str,
) -> dict[str, object]:
    """Build a canonical entity record from a source plus overridden metadata.

    Constructed as a plain ``Entity`` rather than a typed ``Task``/``Epic`` on
    purpose: ``_entity_record`` overlays the model's own dumped fields on top of
    metadata, so a typed model would re-inject its strongly-typed ``status`` /
    ``epic_id`` / ``parent_task_id`` and silently shadow the values this
    migration is rewriting. Driving everything from ``metadata`` keeps the
    denormalized columns, the flat ``attributes`` mirror, and the serialized
    ``attributes.metadata`` all consistent with the intended change.
    """
    rebuilt = Entity(
        id=source.id,
        entity_type=entity_type or source.entity_type,
        name=source.name,
        description=source.description,
        content=source.content,
        organization_id=source.organization_id,
        created_by=source.created_by,
        modified_by=source.modified_by,
        metadata=metadata,
        created_at=source.created_at,
        updated_at=source.updated_at,
        source_file=source.source_file,
        embedding=source.embedding,
    )
    return _entity_record(rebuilt, group_id=group_id, canonicalize_parent_task_id=False)


def _coerce_epic_status(value: str) -> EpicStatus:
    try:
        return EpicStatus(value)
    except ValueError:
        return EpicStatus.PLANNING


async def _apply_records(
    client: SurrealGraphClient,
    records: list[dict[str, object]],
    *,
    group_id: str,
    reverse: bool,
) -> None:
    """Persist all rewritten records for an org inside one transaction.

    Reuses the canonical bulk upsert (matched by the UNIQUE uuid index, so each
    row is rewritten in place) and wraps it in ``BEGIN..COMMIT`` so a partial
    failure rolls back. ``$rows`` keeps the write fully parameter-bound.
    """
    query = f"BEGIN TRANSACTION;\n{_ENTITY_BULK_UPSERT_QUERY}\nCOMMIT TRANSACTION;"
    result = await client.execute_query_raw(query, rows=records)
    direction = "reverse" if reverse else "forward"
    raise_on_error(result, query=f"collapse_epics:{direction}:{group_id}")


async def _list_entities_by_type(
    client: SurrealGraphClient,
    *,
    group_id: str,
    entity_type: EntityType,
) -> list[Entity]:
    """Page every entity of a type in an org, including archived rows."""
    entities: list[Entity] = []
    offset = 0
    while True:
        rows = normalize_records(
            await client.execute_query(
                """
                SELECT *
                FROM entity
                WHERE group_id = $group_id AND entity_type = $entity_type
                ORDER BY uuid
                LIMIT $limit START $offset;
                """,
                group_id=group_id,
                entity_type=entity_type.value,
                limit=_COLLAPSE_PAGE_SIZE,
                offset=offset,
            )
        )
        if not rows:
            break
        entities.extend(_entity_from_row(row) for row in rows)
        if len(rows) < _COLLAPSE_PAGE_SIZE:
            break
        offset += len(rows)
    return entities


async def _list_children_by_epic_ids(
    client: SurrealGraphClient,
    *,
    group_id: str,
    epic_ids: list[str],
) -> list[Entity]:
    """Return every task whose ``epic_id`` points at one of the given epics.

    Children are matched on the hydrated ``epic_id`` rather than the raw column
    so legacy rows that carry ``epic_id`` only inside ``attributes.metadata``
    are still caught — the same denormalization blind spot the optimized list
    filters work around. The id set is small (one entry per epic), so scanning
    the org's tasks once is cheaper than a per-shape query matrix.
    """
    if not epic_ids:
        return []
    wanted = set(epic_ids)
    tasks = await _list_entities_by_type(client, group_id=group_id, entity_type=EntityType.TASK)
    return [task for task in tasks if (task.metadata or {}).get("epic_id") in wanted]


__all__ = [
    "MIGRATED_FROM_EPIC_STATUS_KEY",
    "CollapseEpicsResult",
    "collapse_epics_in_org",
]
