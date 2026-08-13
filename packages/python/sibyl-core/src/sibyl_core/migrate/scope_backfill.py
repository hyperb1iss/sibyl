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
    CLEAR_MEMORY_SCOPE,
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

# Shared by the walk and the post-run sweep so the two cannot disagree about
# what "still needs a scope" means.
_SCOPELESS_PREDICATE = "memory_scope = NONE AND attributes.memory_scope = NONE"

# What a resolved raw capture contributes: its scope, its scope key, and the
# principal that owns it. Returning None means the capture is gone, which is
# not an error -- the row simply falls through to derivation.
RawScopeLookup = Callable[[str], Awaitable[tuple[str, str | None, str | None] | None]]


async def no_raw_scope_recovery(raw_memory_id: str) -> None:
    """A lookup that recovers nothing, for callers with no captures to join.

    Spelled as a named function so "recovery is unavailable here" is a stated
    choice in the call rather than an omitted argument. Every row then reaches
    derivation, which cannot produce ``private``: any row whose true scope was
    private gets the wider project scope instead, permanently, because the
    stamp removes it from the pass's own selection.
    """
    return None


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
    remaining: int = 0
    duration_seconds: float = 0.0
    errors: tuple[str, ...] = field(default_factory=tuple)

    @property
    def stamped(self) -> int:
        return self.recovered + self.derived_project

    @property
    def stampable_remaining(self) -> int:
        """Scopeless rows this pass could stamp but did not reach.

        Counted from a sweep after the run, because ``scanned`` only knows what
        the cursor walked past. A row inserted behind the cursor, or made
        scopeless again by a concurrent upsert, is invisible to the pass and
        would otherwise leave it reporting a completion it did not achieve.
        Non-zero means run again against a quiet graph, not that anything broke.

        A dry run writes nothing, so every row it would have stamped is still
        scopeless afterwards and none of it is outstanding work -- subtracting
        only ``skipped`` there would report the whole run as unreached.
        """
        if self.dry_run:
            return 0
        return max(0, self.remaining - self.skipped)

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
    raw_scope_lookup: RawScopeLookup,
    dry_run: bool = True,
    reverse: bool = False,
) -> ScopeBackfillResult:
    """Stamp, or unstamp, one organization's scopeless rows.

    ``dry_run`` counts what would change and writes nothing. ``reverse`` clears
    only the rows this pass stamped, identified by the marker it wrote, so an
    unrelated stamp is never disturbed.

    ``raw_scope_lookup`` is required, and required with no default, because
    idempotence does not undo a wrong answer. Running without it stamps a
    genuinely private row as ``project`` from its own ``project_id``; the row is
    then scoped, so a corrected re-run does not select it and the mis-stamp is
    permanent. Pass ``no_raw_scope_recovery`` to state that recovery is
    deliberately unavailable. ``reverse`` ignores it and takes it only so a
    caller cannot mistake the reverse path for one that needs no decision.
    """
    started = time.time()
    log.info(
        "scope_backfill_start",
        organization_id=group_id,
        dry_run=dry_run,
        reverse=reverse,
    )
    # Owned out here so a failure can still report the batches that landed.
    # Writes commit per batch, so an abort mid-run leaves the org partially
    # migrated; a result that zeroed those counts would describe the run as
    # having done nothing when it did not.
    result = _Counts()
    try:
        if reverse:
            await _reverse_in_org(client, group_id=group_id, dry_run=dry_run, counts=result)
        else:
            await _forward_in_org(
                client,
                group_id=group_id,
                raw_scope_lookup=raw_scope_lookup,
                dry_run=dry_run,
                counts=result,
            )
    except Exception as exc:
        log.exception(
            "scope_backfill_failed",
            organization_id=group_id,
            error_type=type(exc).__name__,
            scanned=result.scanned,
            stamped=result.recovered + result.derived_project,
        )
        return ScopeBackfillResult(
            success=False,
            organization_id=group_id,
            dry_run=dry_run,
            scanned=result.scanned,
            recovered=result.recovered,
            derived_project=result.derived_project,
            skipped_no_readable_scope=result.skipped_no_readable_scope,
            skipped_unreadable=result.skipped_unreadable,
            duration_seconds=time.time() - started,
            errors=(str(exc),),
        )

    # Counted after the walk rather than inferred from it: the cursor cannot
    # see a row that became scopeless behind it.
    remaining = 0 if reverse else await _scopeless_count(client, group_id=group_id)
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
        remaining=remaining,
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
        remaining=remaining,
        duration_seconds=duration,
    )


@dataclass
class _Counts:
    scanned: int = 0
    recovered: int = 0
    derived_project: int = 0
    skipped_no_readable_scope: int = 0
    skipped_unreadable: int = 0


async def _scopeless_count(client: SurrealGraphClient, *, group_id: str) -> int:
    rows = normalize_records(
        await client.execute_query(
            f"""
            SELECT count() FROM entity
            WHERE group_id = $group_id AND {_SCOPELESS_PREDICATE}
            GROUP ALL;
            """,
            group_id=group_id,
        )
    )
    if not rows:
        return 0
    counted = rows[0].get("count")
    return int(counted) if isinstance(counted, int | float | str) else 0


async def _forward_in_org(
    client: SurrealGraphClient,
    *,
    group_id: str,
    raw_scope_lookup: RawScopeLookup,
    dry_run: bool,
    counts: _Counts,
) -> None:
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


async def _stamped_entity(
    entity: Entity,
    *,
    raw_scope_lookup: RawScopeLookup,
    counts: _Counts,
) -> Entity | None:
    metadata = dict(entity.metadata or {})
    # A scopeless row cannot legitimately carry this pass's provenance: the
    # forward pass only ever writes it alongside a scope, and the reverse pass
    # removes both together. Finding one here means it arrived on a write
    # payload, so it is dropped rather than trusted or restored from. Written as
    # None rather than popped: the upsert reads an absent key as "this write
    # does not speak to it" and would leave the untrusted value standing.
    metadata[SCOPE_BACKFILL_PRIOR_KEY] = None
    metadata[SCOPE_BACKFILL_SOURCE_KEY] = None

    raw_memory_id = _text(metadata.get("raw_memory_id"))
    if raw_memory_id:
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
        # None, not popped: an absent key leaves the stale value on the row, and
        # a stale scope_key beside a private scope is the widening this guards.
        stamped["scope_key"] = None
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
    counts: _Counts,
) -> None:
    pending: list[Entity] = []

    async for entity in _iter_backfilled_entities(client, group_id=group_id):
        counts.scanned += 1
        metadata = dict(entity.metadata or {})
        # Every removal here is written as None rather than popped. The upsert
        # reads an absent key as "this write does not speak to it" and preserves
        # whatever the row already had, so a rollback that dropped keys from its
        # payload would leave the stamp it exists to undo standing on the row.
        metadata[SCOPE_BACKFILL_SOURCE_KEY] = None
        # Restore from what the forward pass recorded rather than guessing which
        # fields it introduced. Anything it overwrote comes back with its
        # original value; anything it created is removed. Guessing is how a
        # pre-existing scope_key gets deleted by a rollback that was supposed to
        # be a no-op for it.
        raw_record = metadata.get(SCOPE_BACKFILL_PRIOR_KEY)
        metadata[SCOPE_BACKFILL_PRIOR_KEY] = None
        record: dict[Any, Any] = raw_record if isinstance(raw_record, dict) else {}
        raw_prior = record.get("prior")
        prior: dict[Any, Any] = raw_prior if isinstance(raw_prior, dict) else {}
        touched = record.get("touched")
        for key in touched if isinstance(touched, list) else ():
            metadata[str(key)] = prior.get(key)
        # Scope has its own way of saying it: the column beside the bag takes a
        # sentinel, and returning a row to scopeless has to be said out loud.
        if metadata.get("memory_scope") is None:
            metadata["memory_scope"] = CLEAR_MEMORY_SCOPE
        counts.recovered += 1
        pending.append(_with_metadata(entity, metadata))
        if not dry_run and len(pending) >= _WRITE_BATCH:
            await _apply(client, pending, group_id=group_id, operation="reverse")
            pending = []

    if not dry_run and pending:
        await _apply(client, pending, group_id=group_id, operation="reverse")


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
        predicate=_SCOPELESS_PREDICATE,
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
    "no_raw_scope_recovery",
]
