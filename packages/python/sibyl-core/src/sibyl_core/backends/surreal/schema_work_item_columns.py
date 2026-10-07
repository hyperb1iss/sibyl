"""Promote work-item filter fields a legacy row still holds only in its snapshot.

Rows written before the denormalized columns existed keep ``project_id``,
``status`` and the other listing filters inside the JSON string stored at
``attributes.metadata``. The listing predicates are exact column matches, so
such a row would be invisible to every filtered list until something rewrote
it. This runs once per namespace from the schema migration and copies each
value its column is missing; a row that already carries the column is left
alone, so repeated runs are no-ops. Every entity type carrying a snapshot is
promoted, not only tasks: a note or topic whose snapshot holds a status or a
project gains the column too, which is exactly how the entity reader already
coalesces that metadata for every row.
"""

from __future__ import annotations

import json
from collections.abc import Mapping

from sibyl_core.backends.surreal.records import normalize_records
from sibyl_core.backends.surreal.schema_version import SurrealExecute

WORK_ITEM_COLUMNS = (
    "project_id",
    "epic_id",
    "parent_task_id",
    "status",
    "priority",
    "complexity",
    "feature",
)

_PAGE_SIZE = 500


def _column_missing(value: object) -> bool:
    return value is None or value == ""


def _snapshot_metadata(value: object) -> Mapping[str, object]:
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, Mapping) else {}
    return value if isinstance(value, Mapping) else {}


def missing_work_item_columns(row: Mapping[str, object]) -> dict[str, str]:
    """Column values the snapshot holds that the row's columns lack."""
    metadata = _snapshot_metadata(row.get("snapshot"))
    updates: dict[str, str] = {}
    for column in WORK_ITEM_COLUMNS:
        value = metadata.get(column)
        if column == "parent_task_id" and not isinstance(value, str):
            # The epic alias became parent_task_id at version 7.
            value = metadata.get("epic_id")
        if isinstance(value, str) and value and _column_missing(row.get(column)):
            updates[column] = value
    return updates


async def canonicalize_entity_work_item_columns(execute_query: SurrealExecute) -> None:
    """Copy snapshot-only filter values into their columns, one namespace at a time."""
    projection = ", ".join(WORK_ITEM_COLUMNS)
    cursor = ""
    while True:
        rows = normalize_records(
            await execute_query(
                f"SELECT uuid, attributes.metadata AS snapshot, {projection} FROM entity "
                "WHERE uuid > $cursor AND attributes.metadata != NONE "
                "ORDER BY uuid LIMIT $limit;",
                cursor=cursor,
                limit=_PAGE_SIZE,
            )
        )
        if not rows:
            return
        for row in rows:
            uuid = row.get("uuid")
            updates = missing_work_item_columns(row)
            if not isinstance(uuid, str) or not updates:
                continue
            assignments = ", ".join(f"{column} = ${column}" for column in updates)
            await execute_query(
                f"UPDATE entity SET {assignments} WHERE uuid = $uuid;",
                uuid=uuid,
                **updates,
            )
        cursor = str(rows[-1]["uuid"])
        if len(rows) < _PAGE_SIZE:
            return
