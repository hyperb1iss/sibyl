"""Source-local generations maintained by synchronous mutation events."""

from __future__ import annotations

import time

import structlog

from sibyl_core.backends.surreal.schema_ownership import SchemaOwnership
from sibyl_core.backends.surreal.schema_version import SurrealExecute
from sibyl_core.memory_pipeline.observations import SourceKind

log = structlog.get_logger()

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


def _raw_publication_evidence_changed() -> str:
    """Compare read-relevant lifecycle fields without promotion audit details."""
    stable_states = "[NONE, 'pending', 'promoted', 'active']"

    def state(expression: str) -> str:
        return f"(IF {expression} IN {stable_states} THEN 'active' ELSE {expression} END)"

    def review(prefix: str, field: str) -> str:
        value = f"{prefix}.{field}"
        return (
            f"(IF {prefix}.metadata.eval_consolidation = NONE AND {value} IN "
            f"[NONE, 'pending', 'promoted'] THEN 'ordinary' ELSE {value} END)"
        )

    fields = [
        field
        for field in _RAW_EVIDENCE_FIELDS
        if field
        not in {
            "review_state",
            "metadata.review_state",
            "metadata.lifecycle_state",
            "metadata.lifecycle_flags",
            "metadata.memory_lifecycle",
        }
    ]
    fields.extend(("principal_id", "memory_scope", "scope_key", "project_id", "agent_id"))
    fields.extend(
        f"metadata.{flag}{suffix}"
        for flag in ("hidden", "redacted", "sensitive")
        for suffix in ("", "_at")
    )
    comparisons = [f"$before.{field} != $after.{field}" for field in fields]
    for field in ("review_state", "metadata.review_state"):
        comparisons.append(f"{review('$before', field)} != {review('$after', field)}")
    for field in ("metadata.lifecycle_state", "metadata.memory_lifecycle.state"):
        comparisons.append(f"{state('$before.' + field)} != {state('$after.' + field)}")
    for field in ("metadata.lifecycle_flags", "metadata.memory_lifecycle.flags"):
        comparisons.append(f"($before.{field} ?? []) != ($after.{field} ?? [])")
    for field in ("replacement_source_id", "duplicate_of_source_id"):
        comparisons.append(
            f"$before.metadata.memory_lifecycle.{field} != $after.metadata.memory_lifecycle.{field}"
        )
    return " OR ".join(comparisons)


def source_state_event(
    kind: SourceKind, *, retire_derivations: bool = False, publication_bookkeeping: bool = False
) -> str:
    """Generate only fixed, application-owned identifiers and field expressions.

    Comparing source fields avoids relying on database JSON formatting for the
    Python evidence digest. The ledger deliberately retains no source text.
    """
    table, organization_field = _SOURCE_TABLES[kind]
    fields = _GRAPH_EVIDENCE_FIELDS if kind is SourceKind.GRAPH_ENTITY else _RAW_EVIDENCE_FIELDS
    changed = (
        _raw_publication_evidence_changed()
        if kind is SourceKind.RAW_CAPTURE and publication_bookkeeping
        else " OR ".join(f"$before.{field} != $after.{field}" for field in fields)
    )
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

    Seed the small ledger directly without rewriting indexed source records.
    Read each source inside the same transaction that creates its ledger. The
    installed event uses the same record key, so concurrent source mutations
    contend on that key rather than publishing an obsolete generation.
    Repeated pages preserve existing generations and retained tombstones.
    """
    mutate = ownership.mutate if ownership is not None else execute_query
    table, organization_field = _SOURCE_TABLES[kind]
    deleted = "$source.deleted_at != NONE" if kind is SourceKind.RAW_CAPTURE else "false"
    cursor = None
    checked = 0
    while True:
        started = time.monotonic()
        # Walk primary records so secondary-index scan boundaries cannot skip sources.
        after = "WHERE id > $cursor" if cursor is not None else ""
        rows = await execute_query(
            f"SELECT id, uuid FROM {table} {after} ORDER BY id LIMIT $limit;",
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
                    LET $source = (SELECT uuid, {organization_field}, revision, deleted_at
                        FROM $record)[0];
                    IF $source != NONE {{
                        IF $source.uuid = NONE OR $source.{organization_field} = NONE {{
                            THROW 'source identity is required';
                        }};
                        LET $org = type::string($source.{organization_field});
                        LET $uuid = type::string($source.uuid);
                        LET $key = type::record(string::concat('source_states:',
                            crypto::sha256(type::string([$org, $kind, $uuid]))));
                        IF record::exists($key) = false {{
                            CREATE $key SET organization_id = $org, source_kind = $kind,
                                source_id = $uuid, generation = 1,
                                revision = $source.revision ?? 0, deleted = {deleted}
                                RETURN NONE;
                        }};
                    }};
                }};
                RETURN NONE;
            }};""",
            rows=[r["id"] for r in rows],
            kind=kind.value,
        )
        cursor = rows[-1]["id"]
        checked += len(rows)
        log.info(
            "source_state_backfill_page",
            source_kind=kind.value,
            rows_checked=checked,
            page_rows=len(rows),
            elapsed_ms=round((time.monotonic() - started) * 1000, 2),
        )


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
