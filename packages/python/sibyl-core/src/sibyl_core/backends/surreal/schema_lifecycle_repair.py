"""Restartable backfill and index activation for pending lifecycle discovery."""

from __future__ import annotations

from sibyl_core.backends.surreal.schema_index_recovery import ensure_owned_concurrent_index
from sibyl_core.backends.surreal.schema_ownership import SchemaOwnership
from sibyl_core.backends.surreal.schema_version import (
    ConcurrentIndexDefinition,
    SurrealExecute,
    _first_record,
    wait_for_index_ready,
)

LIFECYCLE_REPAIR_FIELDS = """
DEFINE FIELD IF NOT EXISTS lifecycle_repair_key ON entity TYPE option<string>
    VALUE IF attributes.lifecycle_reconciliation_pending OR attributes.source_validation_pending
          THEN uuid ELSE NONE END;
DEFINE FIELD IF NOT EXISTS lifecycle_repair_cursor ON schema_version TYPE option<string>;
"""

_BATCH_SIZE = 512
_INDEX = "idx_entity_lifecycle_repair_key"


async def migrate_lifecycle_repair(
    execute_query: SurrealExecute,
    *,
    concurrent: bool = False,
    resume: bool = True,
    ownership: SchemaOwnership | None = None,
) -> None:
    """Commit each bounded row refresh with its cursor before activating discovery.

    New writes derive their own key as soon as the field exists. A source that
    becomes pending behind the cursor therefore does not need another backfill.
    The unique key gives repair sweeps an unambiguous pagination boundary;
    healthy rows have NONE, which permits multiple rows under this index.
    """
    mutate = ownership.mutate if ownership is not None else execute_query
    if not resume:
        await mutate("UPDATE schema_version:graph SET lifecycle_repair_cursor = NONE RETURN NONE;")
    progress = _first_record(
        await execute_query("SELECT lifecycle_repair_cursor FROM schema_version:graph;")
    )
    cursor = str(progress.get("lifecycle_repair_cursor") or "") if progress else ""
    while True:
        rows = await execute_query(
            "SELECT id, uuid FROM entity WITH INDEX idx_entity_uuid "
            "WHERE uuid > $cursor ORDER BY uuid LIMIT $limit;",
            cursor=cursor,
            limit=_BATCH_SIZE,
        )
        if not isinstance(rows, list):
            raise TypeError("lifecycle repair backfill expected a record list")
        if not rows:
            break
        next_cursor = str(rows[-1]["uuid"])
        page = (
            "UPDATE $rows SET lifecycle_repair_key = uuid "
            "WHERE lifecycle_repair_key IS NONE AND "
            "(attributes.lifecycle_reconciliation_pending OR attributes.source_validation_pending) "
            "RETURN NONE; "
            "UPDATE schema_version:graph SET lifecycle_repair_cursor = $cursor RETURN NONE;"
        )
        if ownership is None:
            page = f"BEGIN TRANSACTION; {page} COMMIT TRANSACTION;"
        await mutate(
            page,
            rows=[row["id"] for row in rows],
            cursor=next_cursor,
        )
        cursor = next_cursor

    # Keep the legacy index until its replacement has reached readiness.
    if concurrent and ownership is not None:
        await ensure_owned_concurrent_index(
            ownership,
            ConcurrentIndexDefinition(
                name=_INDEX,
                table="entity",
                definition=f"DEFINE INDEX {_INDEX} ON entity FIELDS lifecycle_repair_key UNIQUE",
            ),
        )
    else:
        await mutate(
            f"DEFINE INDEX IF NOT EXISTS {_INDEX} ON entity FIELDS lifecycle_repair_key UNIQUE"
            + (" CONCURRENTLY;" if concurrent else ";")
        )
        if concurrent:
            await wait_for_index_ready(
                execute_query, name=_INDEX, table="entity", require_status=True
            )
    await mutate("REMOVE INDEX IF EXISTS idx_entity_lifecycle_repair ON entity;")
    await mutate("REMOVE FIELD IF EXISTS lifecycle_repair_pending ON entity;")
    await mutate("UPDATE schema_version:graph SET lifecycle_repair_cursor = NONE RETURN NONE;")
