"""Stamp a memory scope onto every graph row that predates scope stamping.

Schema v17 gave ``entity`` a first-class ``memory_scope`` column and backfilled
it from ``attributes.memory_scope``, which reaches only rows stamped at capture.
Everything older carries no scope at all, and the read path treats that absence
as permission to return the row. That fail-open is what leaks a private memory
to an org co-member, and it cannot close while rows have nothing to check.

This runs per organization and resolves scope in two passes, in an order that is
itself the safety property:

1. **Recover the true scope** from ``raw_captures`` for any row whose
   ``attributes.raw_memory_id`` still resolves. A capture knows whether it was
   private, so this is the only pass that can produce ``private``.
2. **Derive a scope** for whatever is left, preserving today's effective
   behavior rather than guessing at intent. A row carrying a ``project_id`` is
   already gated on project membership, so it becomes ``project`` keyed to that
   project; defaulting it to ``org`` would *widen* access. A row with no project
   is already readable by any org member under the fail-open, so it becomes
   ``org``.

Recovery must precede derivation. Run the other way round and a genuinely
private memory whose stamp was lost gets stamped ``org``, which converts a
missing field into a real leak.

Only rows with no scope are touched, so nothing can be downgraded: the pass has
no path to lowering an existing stamp. Re-running is a no-op because the absent
scope is the gate. Every row records why it got its scope, which makes the pass
auditable and lets ``reverse`` undo exactly what it wrote and nothing else.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import structlog

from sibyl_core.backends.surreal.records import normalize_records, raise_on_error
from sibyl_core.services.graph import (
    _ENTITY_BULK_UPSERT_QUERY,
    _entity_from_row,
    _entity_record,
)

if TYPE_CHECKING:
    from sibyl_core.models.entities import Entity
    from sibyl_core.services.graph import SurrealGraphClient

log = structlog.get_logger()

SCOPE_BACKFILL_SOURCE_KEY = "scope_backfill_source"
SOURCE_RAW_CAPTURE = "raw_capture"
SOURCE_DERIVED_PROJECT = "derived_project"
SOURCE_DERIVED_ORG = "derived_org"

_PAGE_SIZE = 500
_WRITE_BATCH = 200

# What a resolved raw capture contributes: its scope, its scope key, and the
# principal that owns it. Returning None means the capture is gone, which is
# not an error -- the row simply falls through to derivation.
RawScopeLookup = Callable[[str], Awaitable[tuple[str, str | None, str | None] | None]]


@dataclass(frozen=True, slots=True)
class ScopeBackfillResult:
    """Outcome of one organization's scope backfill."""

    success: bool
    organization_id: str
    dry_run: bool
    scanned: int = 0
    recovered: int = 0
    derived_project: int = 0
    derived_org: int = 0
    duration_seconds: float = 0.0
    errors: tuple[str, ...] = field(default_factory=tuple)

    @property
    def stamped(self) -> int:
        return self.recovered + self.derived_project + self.derived_org


async def backfill_entity_scope_in_org(
    client: SurrealGraphClient,
    *,
    group_id: str,
    raw_scope_lookup: RawScopeLookup | None = None,
    dry_run: bool = True,
    reverse: bool = False,
) -> ScopeBackfillResult:
    """Stamp, or unstamp, one organization's scopeless rows.

    ``dry_run`` counts what would change and writes nothing. ``reverse`` clears
    only the rows this pass stamped, identified by the marker it wrote, so an
    unrelated stamp is never disturbed.
    """
    started = time.time()
    log.info(
        "scope_backfill_start",
        organization_id=group_id,
        dry_run=dry_run,
        reverse=reverse,
    )
    try:
        if reverse:
            result = await _reverse_in_org(client, group_id=group_id, dry_run=dry_run)
        else:
            result = await _forward_in_org(
                client,
                group_id=group_id,
                raw_scope_lookup=raw_scope_lookup,
                dry_run=dry_run,
            )
    except Exception as exc:
        log.exception(
            "scope_backfill_failed",
            organization_id=group_id,
            error_type=type(exc).__name__,
        )
        return ScopeBackfillResult(
            success=False,
            organization_id=group_id,
            dry_run=dry_run,
            duration_seconds=time.time() - started,
            errors=(str(exc),),
        )

    duration = time.time() - started
    log.info(
        "scope_backfill_complete",
        organization_id=group_id,
        dry_run=dry_run,
        reverse=reverse,
        scanned=result.scanned,
        recovered=result.recovered,
        derived_project=result.derived_project,
        derived_org=result.derived_org,
        duration=duration,
    )
    return ScopeBackfillResult(
        success=True,
        organization_id=group_id,
        dry_run=dry_run,
        scanned=result.scanned,
        recovered=result.recovered,
        derived_project=result.derived_project,
        derived_org=result.derived_org,
        duration_seconds=duration,
    )


@dataclass
class _Counts:
    scanned: int = 0
    recovered: int = 0
    derived_project: int = 0
    derived_org: int = 0


async def _forward_in_org(
    client: SurrealGraphClient,
    *,
    group_id: str,
    raw_scope_lookup: RawScopeLookup | None,
    dry_run: bool,
) -> _Counts:
    counts = _Counts()
    pending: list[Entity] = []

    async for entity in _iter_scopeless_entities(client, group_id=group_id):
        counts.scanned += 1
        stamped = await _stamped_entity(entity, raw_scope_lookup=raw_scope_lookup, counts=counts)
        if stamped is None:
            continue
        pending.append(stamped)
        if not dry_run and len(pending) >= _WRITE_BATCH:
            await _apply(client, pending, group_id=group_id, operation="forward")
            pending = []

    if not dry_run and pending:
        await _apply(client, pending, group_id=group_id, operation="forward")
    return counts


async def _stamped_entity(
    entity: Entity,
    *,
    raw_scope_lookup: RawScopeLookup | None,
    counts: _Counts,
) -> Entity | None:
    metadata = dict(entity.metadata or {})

    raw_memory_id = _text(metadata.get("raw_memory_id"))
    if raw_memory_id and raw_scope_lookup is not None:
        resolved = await raw_scope_lookup(raw_memory_id)
        if resolved is not None:
            scope, scope_key, principal_id = resolved
            metadata["memory_scope"] = scope
            if scope_key:
                metadata["scope_key"] = scope_key
            # A private row is only reachable by its owner, so the owner has to
            # travel with the scope or the stamp denies everyone.
            if principal_id and not _text(metadata.get("principal_id")):
                metadata["principal_id"] = principal_id
            metadata[SCOPE_BACKFILL_SOURCE_KEY] = SOURCE_RAW_CAPTURE
            counts.recovered += 1
            return _with_metadata(entity, metadata)

    project_id = _text(metadata.get("project_id"))
    if project_id:
        # Already gated on project membership today. Anything wider is a
        # widening, not a default.
        metadata["memory_scope"] = "project"
        if not _text(metadata.get("scope_key")):
            metadata["scope_key"] = project_id
        metadata[SCOPE_BACKFILL_SOURCE_KEY] = SOURCE_DERIVED_PROJECT
        counts.derived_project += 1
        return _with_metadata(entity, metadata)

    metadata["memory_scope"] = "org"
    metadata[SCOPE_BACKFILL_SOURCE_KEY] = SOURCE_DERIVED_ORG
    counts.derived_org += 1
    return _with_metadata(entity, metadata)


async def _reverse_in_org(
    client: SurrealGraphClient,
    *,
    group_id: str,
    dry_run: bool,
) -> _Counts:
    counts = _Counts()
    pending: list[Entity] = []

    async for entity in _iter_backfilled_entities(client, group_id=group_id):
        counts.scanned += 1
        metadata = dict(entity.metadata or {})
        source = _text(metadata.pop(SCOPE_BACKFILL_SOURCE_KEY, None))
        # Only the fields this pass introduced come back off. A scope_key that
        # was already present when we arrived is left alone.
        metadata.pop("memory_scope", None)
        if source in {SOURCE_DERIVED_PROJECT}:
            metadata.pop("scope_key", None)
        if source == SOURCE_RAW_CAPTURE:
            metadata.pop("scope_key", None)
        counts.recovered += 1
        pending.append(_with_metadata(entity, metadata))
        if not dry_run and len(pending) >= _WRITE_BATCH:
            await _apply(client, pending, group_id=group_id, operation="reverse")
            pending = []

    if not dry_run and pending:
        await _apply(client, pending, group_id=group_id, operation="reverse")
    return counts


async def _iter_entities_matching(
    client: SurrealGraphClient,
    *,
    group_id: str,
    predicate: str,
) -> Any:
    """Page rows matching a predicate this pass is about to falsify.

    Paging by uuid cursor rather than by offset is load-bearing here. Both
    passes select on the very field they then write, so each stamped page
    leaves the result set and every remaining row slides down into offsets
    already consumed. ``START`` would skip exactly as many rows as the last
    page stamped, and the short-page check would never fire, so the loop
    exited reporting success with the tail silently untouched. A cursor is
    stable under that mutation, and it keeps the dry run terminating too:
    there the set never shrinks, so a fixed ``START 0`` would spin forever.
    """
    cursor = ""
    while True:
        rows = normalize_records(
            await client.execute_query(
                f"""
                SELECT *
                FROM entity
                WHERE group_id = $group_id
                    AND uuid > $cursor
                    AND {predicate}
                ORDER BY uuid
                LIMIT $limit;
                """,
                group_id=group_id,
                cursor=cursor,
                limit=_PAGE_SIZE,
            )
        )
        if not rows:
            return
        for row in rows:
            yield _entity_from_row(row)
        if len(rows) < _PAGE_SIZE:
            return
        cursor = str(rows[-1]["uuid"])


def _iter_scopeless_entities(
    client: SurrealGraphClient,
    *,
    group_id: str,
) -> Any:
    """Page rows carrying no scope.

    The absence is the gate, which is what makes the pass idempotent: a stamped
    row is never selected again.
    """
    return _iter_entities_matching(
        client,
        group_id=group_id,
        predicate="memory_scope = NONE AND attributes.memory_scope = NONE",
    )


def _iter_backfilled_entities(
    client: SurrealGraphClient,
    *,
    group_id: str,
) -> Any:
    return _iter_entities_matching(
        client,
        group_id=group_id,
        predicate=f"attributes.{SCOPE_BACKFILL_SOURCE_KEY} != NONE",
    )


def _with_metadata(entity: Entity, metadata: dict[str, Any]) -> Entity:
    """Rebuild a row around new metadata.

    Constructed from the base ``Entity`` on purpose, matching collapse_epics:
    ``_entity_record`` overlays a typed model's own dumped fields on top of
    metadata, so a typed subclass would re-inject the very values being
    rewritten.
    """
    from sibyl_core.models.entities import Entity as BaseEntity

    return BaseEntity(
        id=entity.id,
        entity_type=entity.entity_type,
        name=entity.name,
        description=entity.description,
        content=entity.content,
        organization_id=entity.organization_id,
        created_by=entity.created_by,
        modified_by=entity.modified_by,
        metadata=metadata,
        created_at=entity.created_at,
        updated_at=entity.updated_at,
        source_file=entity.source_file,
        embedding=entity.embedding,
    )


async def _apply(
    client: SurrealGraphClient,
    entities: list[Entity],
    *,
    group_id: str,
    operation: str,
) -> None:
    """Persist one batch inside a transaction so a partial batch cannot land."""
    records = [_entity_record(entity, group_id=group_id) for entity in entities]
    query = f"BEGIN TRANSACTION;\n{_ENTITY_BULK_UPSERT_QUERY}\nCOMMIT TRANSACTION;"
    result = await client.execute_query_raw(query, rows=records)
    raise_on_error(result, query=f"scope_backfill:{operation}:{group_id}")


def _text(value: object | None) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


__all__ = [
    "SCOPE_BACKFILL_SOURCE_KEY",
    "SOURCE_DERIVED_ORG",
    "SOURCE_DERIVED_PROJECT",
    "SOURCE_RAW_CAPTURE",
    "RawScopeLookup",
    "ScopeBackfillResult",
    "backfill_entity_scope_in_org",
]
