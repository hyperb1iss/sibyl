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
   private, so this is the only pass that can produce ``private``. The capture's
   values are authoritative and replace whatever the graph row carried.
2. **Derive a scope** for whatever is left, preserving today's effective
   behavior rather than guessing at intent. A row carrying a ``project_id`` is
   already gated on project membership, so it becomes ``project`` keyed to that
   project.

Recovery must precede derivation. Run the other way round and a genuinely
private memory whose stamp was lost gets stamped for a wider audience, which
converts a missing field into a real leak.

**A row is left unstamped whenever no scope value would keep it readable.**
Read authorization admits only ``private``, ``project``, ``team``, and
``delegated``; ``organization``, ``shared``, and ``public`` reach
``scope_not_enabled``, and the graph read path forwards neither team nor
delegation memberships today. So a projectless row -- org-readable now purely
because the absent scope fails open -- has no stampable equivalent, and stamping
one anyway would revoke every reader it has. Those rows stay unstamped and are
counted, which is what makes ``skipped`` the honest answer to whether the
fail-open can close yet: while it is non-zero, closing it would hide real rows.

Only rows with no scope are touched, so nothing can be downgraded: the pass has
no path to lowering an existing stamp. Re-running is a no-op because the absent
scope is the gate. Every row records why it got its scope and what the pass
overwrote to put it there, which makes the pass auditable and lets ``reverse``
restore the prior values exactly rather than guessing which fields it created.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import structlog

from sibyl_core.backends.surreal.records import normalize_records, raise_on_error
from sibyl_core.models.memory_scope import MemoryScope
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
SCOPE_BACKFILL_PRIOR_KEY = "scope_backfill_prior"
SOURCE_RAW_CAPTURE = "raw_capture"
SOURCE_DERIVED_PROJECT = "derived_project"

# Read authorization admits only these. organization, shared, and public all
# reach scope_not_enabled, so there is no scope value that reproduces today's
# fail-open org readability -- which is why a projectless row cannot be stamped
# at all rather than being stamped "organization".
_STAMPABLE_SCOPES = frozenset(
    {
        MemoryScope.PRIVATE,
        MemoryScope.PROJECT,
    }
)

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
    skipped_no_readable_scope: int = 0
    skipped_unreadable: int = 0
    duration_seconds: float = 0.0
    errors: tuple[str, ...] = field(default_factory=tuple)

    @property
    def stamped(self) -> int:
        return self.recovered + self.derived_project

    @property
    def skipped(self) -> int:
        """Rows left unstamped because no scope value would keep them readable.

        Non-zero here is the honest answer to "can the fail-open close yet":
        every one of these rows still reads as unscoped, so closing it would
        hide them. Not an error, and not progress either.
        """
        return self.skipped_no_readable_scope + self.skipped_unreadable


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
        skipped_no_readable_scope=result.skipped_no_readable_scope,
        skipped_unreadable=result.skipped_unreadable,
        duration=duration,
    )
    return ScopeBackfillResult(
        success=True,
        organization_id=group_id,
        dry_run=dry_run,
        scanned=result.scanned,
        recovered=result.recovered,
        derived_project=result.derived_project,
        skipped_no_readable_scope=result.skipped_no_readable_scope,
        skipped_unreadable=result.skipped_unreadable,
        duration_seconds=duration,
    )


@dataclass
class _Counts:
    scanned: int = 0
    recovered: int = 0
    derived_project: int = 0
    skipped_no_readable_scope: int = 0
    skipped_unreadable: int = 0


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
            if (recovered := _recovered(metadata, scope, scope_key, principal_id)) is not None:
                counts.recovered += 1
                return _with_metadata(entity, recovered)
            # A capture whose scope no reader can satisfy is left alone: see
            # _readable_scope. Falling through to derivation would be worse,
            # since it would widen a row the capture says is narrow.
            counts.skipped_unreadable += 1
            return None

    project_id = _text(metadata.get("project_id"))
    if project_id:
        # Already gated on project membership today, and the gate reads
        # scope_key once a scope exists. Any other key names a different
        # project, so it is replaced rather than preserved -- keeping it would
        # hand the row to that project's members, which is a widening.
        metadata.update(_overwritten(metadata, ("memory_scope", "scope_key")))
        metadata["memory_scope"] = MemoryScope.PROJECT.value
        metadata["scope_key"] = project_id
        metadata[SCOPE_BACKFILL_SOURCE_KEY] = SOURCE_DERIVED_PROJECT
        counts.derived_project += 1
        return _with_metadata(entity, metadata)

    # A projectless row is org-readable today only because the absent scope
    # fails open. No scope value reproduces that: organization, shared, and
    # public all reach scope_not_enabled, so stamping any of them would revoke
    # every reader the row has. Leaving it unstamped keeps today's behavior and
    # keeps the row visible in the remaining-work count, which is the honest
    # signal that the fail-open cannot close on this population yet.
    counts.skipped_no_readable_scope += 1
    return None


def _recovered(
    metadata: dict[str, Any],
    scope: str,
    scope_key: str | None,
    principal_id: str | None,
) -> dict[str, Any] | None:
    """Apply a capture's authoritative scope, or refuse if no reader could pass.

    The capture owns these values, so they replace whatever the graph row
    carried rather than deferring to it. A stale or forged ``principal_id``
    winning over the capture's would both lock the real owner out and hand a
    private row to whoever the stale value names, which is why the canonical
    write path in ``stamp_memory_scope_metadata`` drops caller-supplied owner
    fields unconditionally.
    """
    if (resolved := _readable_scope(scope)) is None:
        return None
    stamped = dict(metadata)
    stamped.update(_overwritten(metadata, ("memory_scope", "scope_key", "principal_id")))
    # Without this marker the reverse pass cannot see the row at all: it selects
    # on the marker's presence, so an unmarked stamp is permanent.
    stamped[SCOPE_BACKFILL_SOURCE_KEY] = SOURCE_RAW_CAPTURE
    stamped["memory_scope"] = resolved.value
    if resolved is MemoryScope.PRIVATE:
        # Private resolves its owner from principal_id, so the owner has to
        # travel with the scope or the stamp denies everyone including them.
        if not (owner := _text(principal_id)):
            return None
        stamped["principal_id"] = owner
        stamped.pop("scope_key", None)
        return stamped
    if not (key := _text(scope_key)):
        return None
    stamped["scope_key"] = key
    if owner := _text(principal_id):
        stamped["principal_id"] = owner
    return stamped


def _readable_scope(scope: str) -> MemoryScope | None:
    """The scope to stamp, or None when no reader could satisfy it.

    Read authorization admits exactly private, project, team, and delegated;
    every other member reaches ``scope_not_enabled``. Team and delegated are
    admitted by ``authorize_memory_read`` but ``memory_metadata_read_allowed``
    forwards neither membership set today, so a row stamped with either is
    unreadable by everyone through the graph surface. Stamping a scope nobody
    can pass is data loss wearing a security fix's clothes, so those rows stay
    unstamped and counted until the read path carries those memberships.
    """
    try:
        resolved = MemoryScope(scope)
    except ValueError:
        return None
    return resolved if resolved in _STAMPABLE_SCOPES else None


def _overwritten(metadata: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    """Record what this pass is about to write, so reverse can undo exactly it.

    Two facts are needed and neither implies the other: which fields the pass
    touched, and what they held first. Reverse guessing either one corrupts a
    row. Deleting a field the pass never wrote loses data -- a ``principal_id``
    that was already correct on a project row. Deleting one it merely replaced
    loses the original -- a pre-existing project ``scope_key``. ``touched`` is
    stored explicitly rather than inferred from ``prior``, because "written
    where nothing was" is precisely the case an absent prior value cannot
    distinguish from "not written at all".
    """
    prior = {key: value for key in keys if (value := metadata.get(key)) is not None}
    return {SCOPE_BACKFILL_PRIOR_KEY: {"touched": list(keys), "prior": prior}}


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
        metadata.pop(SCOPE_BACKFILL_SOURCE_KEY, None)
        # Restore from what the forward pass recorded rather than guessing which
        # fields it introduced. Anything it overwrote comes back with its
        # original value; anything it created is removed. Guessing is how a
        # pre-existing scope_key gets deleted by a rollback that was supposed to
        # be a no-op for it.
        raw_record = metadata.pop(SCOPE_BACKFILL_PRIOR_KEY, None)
        record: dict[Any, Any] = raw_record if isinstance(raw_record, dict) else {}
        raw_prior = record.get("prior")
        prior: dict[Any, Any] = raw_prior if isinstance(raw_prior, dict) else {}
        touched = record.get("touched")
        for key in touched if isinstance(touched, list) else ():
            if (value := prior.get(key)) is not None:
                metadata[str(key)] = value
            else:
                metadata.pop(str(key), None)
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
    "SCOPE_BACKFILL_PRIOR_KEY",
    "SCOPE_BACKFILL_SOURCE_KEY",
    "SOURCE_DERIVED_PROJECT",
    "SOURCE_RAW_CAPTURE",
    "RawScopeLookup",
    "ScopeBackfillResult",
    "backfill_entity_scope_in_org",
]
