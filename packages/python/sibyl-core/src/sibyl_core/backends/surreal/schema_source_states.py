"""Source-local generations maintained by synchronous mutation events."""

from __future__ import annotations

from sibyl_core.backends.surreal.schema_ownership import SchemaOwnership
from sibyl_core.backends.surreal.schema_version import SurrealExecute
from sibyl_core.memory_pipeline.observations import SourceKind

SOURCE_STATE_DEFINITIONS = """
DEFINE TABLE IF NOT EXISTS source_states SCHEMAFULL PERMISSIONS NONE;
DEFINE FIELD IF NOT EXISTS organization_id ON source_states TYPE string;
DEFINE FIELD IF NOT EXISTS source_kind ON source_states TYPE string;
DEFINE FIELD IF NOT EXISTS source_id ON source_states TYPE string;
DEFINE FIELD IF NOT EXISTS generation ON source_states TYPE int ASSERT $value > 0;
DEFINE FIELD IF NOT EXISTS revision ON source_states TYPE int ASSERT $value >= 0;
DEFINE FIELD IF NOT EXISTS deleted ON source_states TYPE bool;
DEFINE INDEX IF NOT EXISTS source_state_identity ON source_states
    FIELDS organization_id, source_kind, source_id UNIQUE;
"""

_SOURCE_TABLES = {
    SourceKind.GRAPH_ENTITY: ("entity", "group_id"),
    SourceKind.RAW_CAPTURE: ("raw_captures", "organization_id"),
}
_GRAPH_EVIDENCE_FIELDS = (
    "name",
    "title",
    "description",
    "summary",
    "content",
    "entity_type",
    "labels",
    "attributes.content",
    "attributes.description",
    "attributes.entity_type",
    "attributes.metadata",
    "attributes.reflection_identity.version",
)
_LIFECYCLE_FIELDS = (
    "review_state",
    "lifecycle_state",
    "lifecycle_flags",
    "lifecycle",
    "memory_lifecycle",
    "excluded_from_recall",
    "superseded_by_source_id",
    "superseded_by_raw_memory_id",
    "duplicate_of_source_id",
    "correction_blockers",
    "source_validation_pending",
    "lifecycle_reconciliation_pending",
)
_GRAPH_EVIDENCE_FIELDS += tuple(f"attributes.{field}" for field in _LIFECYCLE_FIELDS)
_RAW_EVIDENCE_FIELDS = (
    "raw_content",
    "title",
    "review_state",
    "deleted_at",
    *(f"metadata.{field}" for field in _LIFECYCLE_FIELDS),
)


def source_state_event(kind: SourceKind, *, retire_derivations: bool = False) -> str:
    """Generate only fixed, application-owned identifiers and field expressions.

    Comparing source fields avoids relying on database JSON formatting for the
    Python evidence digest. The ledger deliberately retains no source text.
    """
    table, organization_field = _SOURCE_TABLES[kind]
    fields = _GRAPH_EVIDENCE_FIELDS if kind is SourceKind.GRAPH_ENTITY else _RAW_EVIDENCE_FIELDS
    changed = " OR ".join(f"$before.{field} != $after.{field}" for field in fields)
    deleted = "$event = 'DELETE'"
    if kind is SourceKind.RAW_CAPTURE:
        deleted += " OR $after.deleted_at != NONE"
    retirement = (
        f"""IF $event = 'DELETE' {{
            UPDATE memory_derivations SET active = false
                WHERE organization_id = $org AND target_kind = '{kind.value}' AND target_id = $uuid;
        }};"""
        if retire_derivations
        else ""
    )
    return f"""
DEFINE EVENT IF NOT EXISTS maintain_source_state ON {table} WHEN true THEN {{
    LET $source = IF $event = 'DELETE' THEN $before ELSE $after END;
    IF $source.uuid = NONE OR $source.{organization_field} = NONE {{
        THROW 'source identity is required';
    }};
    IF $event = 'UPDATE' AND ($before.uuid != $after.uuid
        OR $before.{organization_field} != $after.{organization_field}) {{
        THROW 'source identity is immutable';
    }};
    LET $org = type::string($source.{organization_field});
    LET $uuid = type::string($source.uuid);
    LET $key = type::record(string::concat('source_states:', crypto::sha256(type::string([$org, '{kind.value}', $uuid]))));
    LET $state = (SELECT * FROM $key)[0];
    LET $changed = $event != 'UPDATE' OR {changed};
    UPSERT $key SET organization_id = $org, source_kind = '{kind.value}', source_id = $uuid,
        generation = ($state.generation ?? 0) + IF $state = NONE OR $changed
            OR ($state.deleted AND NOT ({deleted})) THEN 1 ELSE 0 END,
        revision = $source.revision ?? 0,
        deleted = {deleted};
    {retirement}
}};
"""


async def migrate_source_states(
    execute_query: SurrealExecute,
    *,
    kind: SourceKind,
    ownership: SchemaOwnership | None = None,
) -> None:
    """Backfill in restartable pages after the source event has been installed.

    An untracked source gets one bookkeeping revision bump to trigger its
    event. Repeated pages skip existing generations. Concurrent new writes
    maintain their own state.
    """
    mutate = ownership.mutate if ownership is not None else execute_query
    table, organization_field = _SOURCE_TABLES[kind]
    cursor = ""
    while True:
        rows = await execute_query(
            f"SELECT id, uuid FROM {table} WHERE uuid > $cursor ORDER BY uuid LIMIT $limit;",
            cursor=cursor,
            limit=512,
        )
        if not isinstance(rows, list):
            raise TypeError("source state backfill expected record rows")
        if not rows:
            return
        await mutate(
            f"""RETURN {{
                FOR $record IN $rows {{
                    LET $source = (SELECT * FROM $record)[0];
                    LET $state = (SELECT * FROM source_states
                        WHERE organization_id = $source.{organization_field}
                            AND source_kind = $kind AND source_id = $source.uuid LIMIT 1)[0];
                    IF $source != NONE AND $state = NONE {{
                        UPDATE $record SET revision = (revision ?? 0) + 1 RETURN NONE;
                    }};
                }};
                RETURN NONE;
            }};""",
            rows=[r["id"] for r in rows],
            kind=kind.value,
        )
        cursor = str(rows[-1]["uuid"])


async def migrate_graph_source_states(
    execute_query: SurrealExecute, *, ownership: SchemaOwnership | None = None
) -> None:
    """Bind the graph migration to its existing schema owner."""
    await migrate_source_states(execute_query, kind=SourceKind.GRAPH_ENTITY, ownership=ownership)


async def retire_source_states(execute_query: SurrealExecute, *, kind: SourceKind) -> None:
    """Preserve high-water tombstones before an intentional source-table reset."""
    from sibyl_core.backends.surreal.schema_derivations import DERIVATION_DEFINITIONS

    await execute_query(SOURCE_STATE_DEFINITIONS)
    if kind is SourceKind.RAW_CAPTURE:
        await execute_query(DERIVATION_DEFINITIONS)
        await execute_query(
            "UPDATE memory_derivations SET active=false WHERE target_kind=$kind;", kind=kind.value
        )
    await execute_query(
        "UPDATE source_states SET generation += 1, deleted = true "
        "WHERE source_kind = $kind AND deleted = false RETURN NONE;",
        kind=kind.value,
    )
