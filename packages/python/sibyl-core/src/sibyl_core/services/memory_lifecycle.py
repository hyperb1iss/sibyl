"""Lifecycle projection and graph correction services."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any

import structlog

from sibyl_core.auth.memory_policy import (
    memory_metadata_read_allowed,
    memory_row_project_id,
    private_scope_granted_for,
)
from sibyl_core.memory_pipeline.lifecycle import (
    RECONCILE_PENDING_KEY,
    graph_lifecycle_stamp,
    graph_metadata_recallable,
)
from sibyl_core.memory_pipeline.source_lifecycle import (
    SOURCE_BINDINGS_KEY,
    SOURCE_VALIDATION_PENDING_KEY,
)
from sibyl_core.models.entities import Relationship, RelationshipType
from sibyl_core.models.reflection import (
    ReflectionCandidate,
    ReflectionFindingKind,
    claim_records_from_metadata,
    reflection_findings_from_metadata,
)
from sibyl_core.services.graph import get_surreal_graph_runtime, normalize_records
from sibyl_core.services.memory_contract import MemoryCorrectionPreview
from sibyl_core.services.memory_lineage import CorrectionPropagation, propagate_source_correction
from sibyl_core.services.memory_policy import (
    _candidate_source_ids,
    _correction_derived_ids,
    _metadata_dict_values,
    _metadata_str,
    _metadata_str_values,
    _promoted_entity_write_allowed,
    _raw_memory_write_allowed,
    raw_memory_source_fingerprint,
    suppression_target_visible,
)
from sibyl_core.services.surreal_content import (
    RawMemory,
    get_raw_memory,
    get_raw_memory_by_source_id,
    list_raw_memories_by_source_id,
    save_raw_memory,
)

log = structlog.get_logger()

_TEMPORAL_INVALIDATION_SOURCE_KEYS = (
    "contradiction_source_ids",
    "conflicts_with_source_ids",
    "contradicts_source_ids",
    "supersedes_source_ids",
    "superseded_source_ids",
)


_TEMPORAL_INVALIDATION_REASONS = {
    "contradiction_source_ids": "contradiction",
    "conflicts_with_source_ids": "contradiction",
    "contradicts_source_ids": "contradiction",
    "supersedes_source_ids": "supersession",
    "superseded_source_ids": "supersession",
}


async def projected_row_lifecycle_stamp(
    *,
    organization_id: str,
    metadata: Mapping[str, Any] | None,
    expected_signature: str | None = None,
) -> dict[str, Any]:
    """Read the current verdict for the capture a row is about to be projected from.

    The correction write-through can only stamp rows that exist when it runs.
    Graph writes are queued, so the rows a capture projects into can be created
    minutes after the capture was corrected, from a payload serialized before
    the correction existed. Reading the capture back at creation time is what
    closes that window: `raw_captures` is the row the correction mutated, and
    it is already correct by the time the worker gets here.

    One indexed read per projected row, on the write path only. Nothing on the
    recall path consults this, because the stamp it returns is exactly what
    the recall gate already reads off the row.

    A declared capture that is absent remains pending. Unbound graph rows have
    no capture authority to check; an unavailable store raises for the caller's
    reconciliation retry path.
    """

    fields = metadata if isinstance(metadata, Mapping) else {}
    raw_memory_id = _metadata_str(fields, "raw_memory_id")
    raw_source_id = _metadata_str(fields, "raw_source_id")
    if not raw_memory_id and not raw_source_id:
        return {}
    try:
        memory = None
        if raw_memory_id:
            memory = await get_raw_memory(
                organization_id=str(organization_id),
                memory_id=raw_memory_id,
            )
        if memory is None and raw_source_id:
            memory = await get_raw_memory_by_source_id(
                organization_id=str(organization_id),
                source_id=raw_source_id,
            )
    except Exception as exc:
        # Raised rather than swallowed. Writing the row anyway mints a fully
        # recallable copy of text that may already be retired, and nothing
        # comes back for it: the correction that would have stamped it has
        # already run and found nothing. A raised failure leaves the write to
        # be retried, which is recoverable in a way a silently active row is
        # not.
        log.warning(
            "projected_row_lifecycle_lookup_failed",
            raw_memory_id=raw_memory_id,
            error_type=type(exc).__name__,
        )
        raise
    if memory is None:
        # Absence cannot certify the source lifecycle. Use the same pending
        # marker as an unavailable read so recovery can clear its owned check.
        return {RECONCILE_PENDING_KEY: True}
    # A source revision is stale only relative to the text this row used.
    stamp = graph_lifecycle_stamp(
        replace(
            memory,
            metadata={
                **memory.metadata,
                SOURCE_BINDINGS_KEY: fields.get(SOURCE_BINDINGS_KEY, {}),
            },
        )
    )
    if (
        expected_signature is not None
        and raw_memory_source_fingerprint(memory) != expected_signature
    ):
        stamp[SOURCE_VALIDATION_PENDING_KEY] = True
    if not graph_metadata_recallable(stamp):
        log.info(
            "projected_row_born_retired",
            raw_memory_id=memory.id,
            lifecycle_state=stamp.get("lifecycle_state"),
        )
    return dict(stamp)


_GRAPH_CORRECTION_LOOKUP_LIMIT = 64


_GRAPH_CORRECTION_PROJECTION_PAGE_SIZE = 512


_GRAPH_CORRECTION_PROJECTION_MAX_PAGES = 64


_CORRECTION_NATIVE_WRITE_PATH = "memory_correction"


@dataclass(frozen=True, slots=True)
class _CorrectionGraphTargets:
    """Graph rows a correction may stamp, and the ones it was refused.

    Spans are held apart from the memories they were cut from because the two
    take different halves of the correction. Both get the lifecycle stamp, so
    both leave recall. Only a memory gets a supersession edge: "this row
    replaced that one" is a claim a writer made about a memory, and minting it
    once per span would assert a replacement nobody declared, on up to
    `MAX_PASSAGES_PER_SOURCE` rows per correction.
    """

    authorized: list[str]
    refused: list[str]
    projections: list[str] = field(default_factory=list)
    direct: list[str] = field(default_factory=list)
    truncated: bool = False

    @property
    def stampable(self) -> list[str]:
        return [*self.direct, *self.projections]


async def _correction_graph_entity_ids(
    runtime: Any,
    *,
    organization_id: str,
    memory: RawMemory,
    principal_id: str | None,
    accessible_projects: Iterable[str] | None,
    writable_projects: Iterable[str] | None = None,
    allowed_memory_scope_keys: Iterable[str] | None = None,
) -> _CorrectionGraphTargets:
    """Find the graph rows projected from this capture.

    Two sources, because the two write paths record the link in opposite
    directions. Promotion stamps the entity id onto the raw memory, so that
    one is a metadata read. Direct capture stamps the raw ids onto the graph
    row's attributes instead (`memory_pipeline/capture.py:137-140`), which
    leaves a query as the only way back. That query is unindexed, and it is
    affordable here only because a correction is a rare, operator-initiated
    write rather than anything on the read path.

    The query matches `raw_memory_id` and never `raw_source_id`, even though
    capture writes both. A capture's `source_id` is a non-unique grouping key
    (`idx_raw_captures_source` is not UNIQUE, and
    `list_raw_memories_by_source_id` returns a list), while a correction
    declares exactly one affected capture. Matching the group key would let a
    correction on one memory stamp the projections of its siblings.

    The two halves are guarded differently, because they differ in who
    authored them.

    The query half is server-authoritative. `raw_memory_id` is assigned from
    the completed raw write and overwrites anything the caller stamped
    (`memory_pipeline/capture.py:137-138`), so a row it returns is a
    projection of the very capture `preview_memory_correction` just
    authorized this principal to correct. Re-authorizing it would not add
    safety, and it would subtract enormously: `authorize_memory_write` refuses
    SHARED, ORGANIZATION, and PUBLIC outright, refuses TEAM without
    `accessible_teams`, and refuses a row carrying no scope metadata at all.
    Promotion makes rows visible at ORGANIZATION and PUBLIC, so guarding this
    half would silently disable the write-through for most of a real corpus:
    the capture would retire while the graph row kept ranking, which is the
    exact defect this whole change exists to kill.

    One subtlety, because it reads like a hole and is not: the promotion path
    builds entity metadata by spreading the candidate bag, so a promoted row's
    `raw_memory_id` can carry a caller-supplied value rather than a stamped
    one. Planting somebody else's capture id there only arranges for the row
    to be retired when THAT capture is corrected, and the row in question is
    the planter's own. Reaching a foreign row requires stamping this capture's
    id onto it, which needs the write access the attack is trying to obtain.

    The metadata half is caller-reachable and keeps the guard.
    `promoted_entity_id` and friends are read straight off the capture's
    metadata bag, and capture metadata is pass-through rather than a
    whitelist, so without a check a caller could name somebody else's entity,
    correct their own capture, and retire a row they cannot write. The
    reflection path guards its supersession targets exactly this way
    (`_authorized_superseded_entity_ids`).
    """

    rows: list[dict[str, Any]] = []
    cursor = ""
    while True:
        batch = normalize_records(
            await runtime.client.execute_query(
                "SELECT uuid FROM entity WHERE group_id = $group_id "
                "AND attributes.raw_memory_id = $raw_memory_id AND uuid > $cursor "
                "ORDER BY uuid LIMIT $limit;",
                group_id=str(organization_id),
                raw_memory_id=memory.id,
                cursor=cursor,
                limit=_GRAPH_CORRECTION_LOOKUP_LIMIT,
            )
        )
        rows.extend(batch)
        if len(batch) < _GRAPH_CORRECTION_LOOKUP_LIMIT:
            break
        cursor = str(batch[-1]["uuid"])
    projected_ids = [uuid for row in rows if (uuid := str(row.get("uuid") or ""))]
    declared = [
        entity_id
        for entity_id in _correction_derived_ids(memory)
        if entity_id and entity_id not in set(projected_ids)
    ]

    # Provenance is server-owned as of `SERVER_OWNED_METADATA_KEYS`, but rows
    # written before that could carry a planted `raw_memory_id`, and nothing
    # rewrites history. So a projected row must still be one this principal
    # can currently see: that is what stops "plant the id on a row you can
    # write, lose access, then correct your own capture to retire it". A
    # genuine projection inherits its capture's audience, so this refuses no
    # legitimate target.
    projected = await _readable_correction_targets(
        runtime,
        entity_ids=projected_ids,
        source_id=memory.id,
        principal_id=principal_id,
        accessible_projects=accessible_projects,
        log_event="memory_correction_graph_provenance_unreadable",
    )

    # Declared ids come off caller-writable capture metadata, so the refused
    # list is caller-chosen input echoed back. A row that does not exist and a
    # row that exists but is denied must therefore be reported identically: any
    # difference between the two turns a guessed id into an existence probe for
    # rows outside this principal's scope.
    refused: list[str] = []
    authorized: list[str] = list(projected)
    for entity_id in dict.fromkeys(declared):
        allowed = False
        try:
            target = await runtime.entity_manager.get(entity_id)
        except Exception:
            target = None
        if target is not None:
            allowed = _promoted_entity_write_allowed(
                entity=target,
                principal_id=principal_id,
                writable_projects=tuple(writable_projects or ()),
            )
        if allowed and allowed_memory_scope_keys is not None:
            allowed = suppression_target_visible(
                target,
                principal_id=principal_id,
                accessible_projects=accessible_projects,
                allowed_memory_scope_keys=allowed_memory_scope_keys,
            )
        if allowed:
            authorized.append(entity_id)
            continue
        log.warning(
            "memory_correction_graph_target_refused",
            source_id=memory.id,
            entity_id=entity_id,
        )
        refused.append(entity_id)

    projections, walk_truncated = await _correction_projected_row_ids(
        runtime,
        organization_id=organization_id,
        memory=memory,
        parent_ids=projected,
        principal_id=principal_id,
        accessible_projects=accessible_projects,
    )
    known = set(authorized)
    return _CorrectionGraphTargets(
        authorized=authorized,
        direct=projected,
        refused=refused,
        projections=[
            entity_id for entity_id in dict.fromkeys(projections) if entity_id not in known
        ],
        truncated=walk_truncated,
    )


async def _readable_correction_targets(
    runtime: Any,
    *,
    entity_ids: Sequence[str],
    source_id: str,
    principal_id: str | None,
    accessible_projects: Iterable[str] | None,
    log_event: str,
    allowed_memory_scope_keys: Iterable[str] | None = None,
) -> list[str]:
    """Keep the rows a correction found that this principal can still read.

    `private_scope_granted` says only that this principal may hold private
    memories at all; ownership is still checked inside, so a private row
    belonging to somebody else is refused either way. The correction route has
    already authorized this principal against the capture, so the grant is not
    the thing under test here.
    """

    readable: list[str] = []
    for entity_id in dict.fromkeys(entity_ids):
        try:
            row = await runtime.entity_manager.get(entity_id)
        except Exception:
            continue
        if row is None:
            continue
        row_metadata = getattr(row, "metadata", None)
        if not memory_metadata_read_allowed(
            row_metadata,
            principal_id=principal_id,
            private_scope_granted=private_scope_granted_for(
                allowed_memory_scope_keys, principal_id=principal_id
            ),
            accessible_projects=accessible_projects,
            row_project_id=memory_row_project_id(
                row_metadata,
                entity_type=str(getattr(row, "entity_type", "") or ""),
                entity_id=str(getattr(row, "id", "") or ""),
            ),
            allowed_memory_scope_keys=allowed_memory_scope_keys,
        ):
            log.warning(log_event, source_id=source_id, entity_id=entity_id)
            continue
        readable.append(entity_id)
    return readable


async def _correction_projected_row_ids(
    runtime: Any,
    *,
    organization_id: str,
    memory: RawMemory,
    parent_ids: Sequence[str],
    principal_id: str | None,
    accessible_projects: Iterable[str] | None,
) -> tuple[list[str], bool]:
    """Follow the correction down into every row projected from the corrected rows.

    Three projections mint rows carrying a parent memory's text: spans carry
    its words verbatim (`projection/passages.py`), projected entities carry its
    candidate context, and projected facts carry their span and content
    (`projection/memory.py`). Each is independently indexed and independently
    servable, and each inherits the parent's scope but not its provenance, so
    the provenance query that finds the parent cannot see any of them. A
    correction that stopped at the parent left the retired text ranking under
    every projected id.

    Lineage is the linkage rather than provenance, because it is the only one a
    projected row carries: every projection stamps `source_entity_id`, spans
    stamp `parent_entity_id` beside it, and rows written before any given
    provenance rule still carry them. Matching either is what makes historical
    projections reachable from a correction.

    Requiring `projection_kind` keeps this to rows claiming to be projections
    at all, which is the narrowing the passage-only version got from its
    entity-type filter. It is not the security boundary: the parents are rows
    this correction was already authorized against, and each projected row is
    re-checked for readability exactly like a provenance row, because neither
    lineage nor `projection_kind` is server-owned and naming a parent must not
    be enough to have somebody else's row retired.
    """

    parents = [str(parent_id) for parent_id in dict.fromkeys(parent_ids) if parent_id]
    if not parents:
        return [], False
    rows: list[dict[str, Any]] = []
    cursor = ""
    truncated = False
    try:
        for page in range(_GRAPH_CORRECTION_PROJECTION_MAX_PAGES):
            batch = normalize_records(
                await runtime.client.execute_query(
                    """
                    SELECT uuid FROM entity
                    WHERE group_id = $group_id
                      AND attributes.projection_kind IS NOT NONE
                      AND uuid > $cursor
                      AND (attributes.parent_entity_id IN $parent_ids
                           OR attributes.source_entity_id IN $parent_ids)
                    ORDER BY uuid
                    LIMIT $limit;
                    """,
                    group_id=str(organization_id),
                    parent_ids=parents,
                    cursor=cursor,
                    limit=_GRAPH_CORRECTION_PROJECTION_PAGE_SIZE,
                )
            )
            rows.extend(batch)
            if len(batch) < _GRAPH_CORRECTION_PROJECTION_PAGE_SIZE:
                break
            cursor = str(batch[-1].get("uuid") or "")
            if not cursor:
                break
            if page + 1 >= _GRAPH_CORRECTION_PROJECTION_MAX_PAGES:
                # Only reachable on a corpus far past anything this walk was
                # sized for, and a silent stop here leaves projected rows of a
                # retired memory servable, so it is said out loud.
                log.warning(
                    "memory_correction_projection_walk_truncated",
                    source_id=memory.id,
                    parents=len(parents),
                    rows=len(rows),
                )
                truncated = True
    except Exception as exc:
        # The parent has already been stamped by the time this runs, so a
        # failure here leaves the correction half-applied rather than undone.
        # Saying so is worth more than throwing away the half that landed.
        log.warning(
            "memory_correction_projection_lookup_failed",
            source_id=memory.id,
            error_type=type(exc).__name__,
        )
        return [], truncated
    projected_ids = [uuid for row in rows if (uuid := str(row.get("uuid") or ""))]
    if not projected_ids:
        return [], truncated
    readable = await _readable_correction_targets(
        runtime,
        entity_ids=[entity_id for entity_id in projected_ids if entity_id not in set(parents)],
        source_id=memory.id,
        principal_id=principal_id,
        accessible_projects=accessible_projects,
        log_event="memory_correction_projection_unreadable",
    )
    return readable, truncated


async def _project_correction_to_graph(
    *,
    organization_id: str,
    memory: RawMemory,
    preview: MemoryCorrectionPreview,
    principal_id: str | None,
    accessible_projects: Iterable[str] | None,
    replacement_source_id: str | None,
    duplicate_of_source_id: str | None,
    writable_projects: Iterable[str] | None = None,
    accessible_teams: Iterable[str] | None = None,
    accessible_delegations: Iterable[str] | None = None,
    allowed_memory_scope_keys: Iterable[str] | None = None,
) -> tuple[list[str], list[str], bool, CorrectionPropagation]:
    """Carry a correction across to the rows retrieval actually ranks.

    Correction used to stop at `raw_captures`. The projected entity kept its
    capture-time metadata, kept its embedding, and kept being expanded into,
    so `sibyl correct` had no retrieval consequence at all. The raw write has
    already landed by the time this runs, so a graph failure is logged and
    swallowed: a correction that half-applied is worth more than one that
    reports failure after mutating the substrate.
    """

    runtime = None
    targets = _CorrectionGraphTargets(authorized=[], refused=[])
    graph_discovery_complete = True
    try:
        runtime = await get_surreal_graph_runtime(str(organization_id))
        targets = await _correction_graph_entity_ids(
            runtime,
            organization_id=organization_id,
            memory=memory,
            principal_id=principal_id,
            accessible_projects=accessible_projects,
            writable_projects=writable_projects,
            allowed_memory_scope_keys=allowed_memory_scope_keys,
        )
    except Exception as exc:
        log.warning(
            "memory_correction_graph_lookup_failed",
            source_id=memory.id,
            error_type=type(exc).__name__,
        )
        graph_discovery_complete = False
    propagation = await propagate_source_correction(
        runtime,
        memory=memory,
        principal_id=principal_id,
        accessible_projects=accessible_projects,
        accessible_teams=accessible_teams,
        accessible_delegations=accessible_delegations,
        allowed_memory_scope_keys=allowed_memory_scope_keys,
        projected_entity_ids=targets.stampable,
        declared_entity_ids=[value for value in targets.authorized if value not in targets.direct],
    )
    propagation.complete = propagation.complete and graph_discovery_complete
    applied = propagation.stamped_entity_ids

    if applied:
        cleaned = await _unlink_graph_supersession(
            runtime,
            organization_id=organization_id,
            source_id=memory.id,
            restored_entity_ids=applied,
        )
        propagation.complete = propagation.complete and cleaned
    disclosed = await _readable_correction_targets(
        runtime,
        entity_ids=list(dict.fromkeys([*applied, *propagation.entity_ids])),
        source_id=memory.id,
        principal_id=principal_id,
        accessible_projects=accessible_projects,
        allowed_memory_scope_keys=allowed_memory_scope_keys,
        log_event="memory_correction_receipt_unreadable",
    )
    return (
        disclosed,
        list(targets.refused),
        targets.truncated,
        propagation,
    )


async def _unlink_graph_supersession(
    runtime: Any,
    *,
    organization_id: str,
    source_id: str,
    restored_entity_ids: Sequence[str],
) -> bool:
    """Remove the supersession edges a correction minted, so restore can undo.

    Clearing the lifecycle stamp is not enough on its own. The admission gate
    retires any row carrying an inbound SUPERSEDES edge, so a restored row
    whose edge survived stays excluded forever, and a supersede/restore
    /counter-supersede sequence would black out both rows. Only edges this
    correction path wrote are removed: a supersession asserted by reflection
    promotion is a different claim and restore has no opinion about it.
    """

    if not restored_entity_ids:
        return True
    try:
        await runtime.client.execute_query(
            """
            DELETE FROM relates_to
            WHERE group_id = $group_id
              AND name = $predicate
              AND target_id IN $target_ids
              AND (attributes.correction_root_id = $correction_root_id
                   OR attributes.correction_root_id IS NONE)
              AND attributes.native_write_path = $write_path;
            """,
            group_id=str(organization_id),
            predicate=RelationshipType.SUPERSEDES.value,
            target_ids=list(restored_entity_ids),
            correction_root_id=source_id,
            write_path=_CORRECTION_NATIVE_WRITE_PATH,
        )
    except Exception as exc:
        log.warning(
            "memory_correction_supersedes_unlink_failed",
            source_id=source_id,
            error_type=type(exc).__name__,
        )
        return False
    return True


@dataclass(frozen=True, slots=True)
class _TemporalInvalidationTarget:
    source_id: str
    reason: str


def _metadata_datetime_or_none(value: object) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _temporal_invalidation_cutoff(candidate: ReflectionCandidate) -> datetime:
    for key in ("valid_at", "valid_from", "occurred_at"):
        parsed = _metadata_datetime_or_none(candidate.metadata.get(key))
        if parsed is not None:
            return parsed
    return datetime.now(UTC)


def _candidate_temporal_invalidation_targets(
    candidate: ReflectionCandidate,
) -> list[_TemporalInvalidationTarget]:
    targets: dict[str, _TemporalInvalidationTarget] = {}
    for key in _TEMPORAL_INVALIDATION_SOURCE_KEYS:
        reason = _TEMPORAL_INVALIDATION_REASONS[key]
        for source_id in _metadata_str_values(candidate.metadata, key):
            targets.setdefault(
                source_id,
                _TemporalInvalidationTarget(source_id=source_id, reason=reason),
            )

    for claim in claim_records_from_metadata(candidate.metadata):
        for source_id in claim.contradicts_source_ids:
            targets.setdefault(
                source_id,
                _TemporalInvalidationTarget(source_id=source_id, reason="contradiction"),
            )
        for source_id in claim.supersedes_source_ids:
            targets.setdefault(
                source_id,
                _TemporalInvalidationTarget(source_id=source_id, reason="supersession"),
            )

    for finding in reflection_findings_from_metadata(candidate.metadata):
        kind = str(finding.kind).lower()
        if kind not in {
            ReflectionFindingKind.CONTRADICTION.value,
            ReflectionFindingKind.SUPERSESSION.value,
        }:
            continue
        reason = "contradiction" if kind == "contradiction" else "supersession"
        for source_id in finding.related_source_ids:
            targets.setdefault(
                source_id,
                _TemporalInvalidationTarget(source_id=source_id, reason=reason),
            )

    candidate_sources = set(_candidate_source_ids(candidate, None))
    return [target for target in targets.values() if target.source_id not in candidate_sources]


def _temporal_invalidation_metadata(
    metadata: Mapping[str, object],
    *,
    invalid_at: datetime,
    reason: str,
    replacement_entity_id: str,
    replacement_source_ids: Sequence[str],
) -> dict[str, object]:
    next_metadata = dict(metadata)
    invalid_at_iso = invalid_at.isoformat()
    existing = _metadata_datetime_or_none(
        next_metadata.get("invalid_at") or next_metadata.get("valid_to")
    )
    if existing is not None and existing <= invalid_at:
        invalid_at_iso = existing.isoformat()
    next_metadata["invalid_at"] = invalid_at_iso
    next_metadata["valid_to"] = invalid_at_iso
    next_metadata["invalidated_by_entity_id"] = replacement_entity_id
    next_metadata["invalidated_by_source_ids"] = list(replacement_source_ids)
    next_metadata["invalidation_reason"] = reason
    history = list(_metadata_dict_values(next_metadata, "invalidation_history"))
    history.append(
        {
            "invalid_at": invalid_at_iso,
            "reason": reason,
            "replacement_entity_id": replacement_entity_id,
            "replacement_source_ids": list(replacement_source_ids),
            "created_at": datetime.now(UTC).isoformat(),
        }
    )
    next_metadata["invalidation_history"] = history
    return next_metadata


async def _load_temporal_invalidation_raw_targets(
    *,
    organization_id: str,
    source_id: str,
) -> list[RawMemory]:
    memory = await get_raw_memory(organization_id=organization_id, memory_id=source_id)
    if memory is not None:
        return [memory]
    return await list_raw_memories_by_source_id(
        organization_id=organization_id,
        source_id=source_id,
    )


async def _invalidate_promoted_entity_targets(
    *,
    runtime: Any,
    entity_ids: Sequence[str],
    principal_id: str | None,
    writable_projects: Iterable[str] | None,
    invalid_at: datetime,
    reason: str,
    replacement_entity_id: str,
    replacement_source_ids: Sequence[str],
) -> list[str]:
    updated: list[str] = []
    for entity_id in dict.fromkeys(entity_ids):
        if not entity_id or entity_id == replacement_entity_id:
            continue
        target = await runtime.entity_manager.get(entity_id)
        if target is None:
            continue
        if not _promoted_entity_write_allowed(
            entity=target,
            principal_id=principal_id,
            writable_projects=writable_projects,
        ):
            continue
        metadata = _temporal_invalidation_metadata(
            target.metadata,
            invalid_at=invalid_at,
            reason=reason,
            replacement_entity_id=replacement_entity_id,
            replacement_source_ids=replacement_source_ids,
        )
        await runtime.entity_manager.update(entity_id, {"metadata": metadata})
        updated.append(entity_id)
    return updated


async def _apply_candidate_temporal_invalidations(
    *,
    runtime: Any,
    organization_id: str,
    principal_id: str | None,
    writable_projects: Iterable[str] | None,
    candidate: ReflectionCandidate,
    replacement_entity_id: str,
    replacement_source_ids: Sequence[str],
    authorized_entity_ids: Sequence[str],
) -> dict[str, Any]:
    targets = _candidate_temporal_invalidation_targets(candidate)
    invalid_at = _temporal_invalidation_cutoff(candidate)
    invalidated_source_ids: list[str] = []
    invalidated_entity_ids: list[str] = []
    skipped_source_ids: list[str] = []

    for target in targets:
        target_memories = await _load_temporal_invalidation_raw_targets(
            organization_id=organization_id,
            source_id=target.source_id,
        )
        memory = next(
            (
                candidate
                for candidate in target_memories
                if _raw_memory_write_allowed(
                    memory=candidate,
                    principal_id=principal_id,
                    accessible_projects=writable_projects,
                )
            ),
            None,
        )
        if memory is None:
            skipped_source_ids.append(target.source_id)
            continue
        metadata = _temporal_invalidation_metadata(
            memory.metadata,
            invalid_at=invalid_at,
            reason=target.reason,
            replacement_entity_id=replacement_entity_id,
            replacement_source_ids=replacement_source_ids,
        )
        await save_raw_memory(replace(memory, metadata=metadata))
        invalidated_source_ids.append(memory.id)
        promoted_entity_id = _metadata_str(metadata, "promoted_entity_id")
        if promoted_entity_id:
            invalidated_entity_ids.extend(
                await _invalidate_promoted_entity_targets(
                    runtime=runtime,
                    entity_ids=[promoted_entity_id],
                    principal_id=principal_id,
                    writable_projects=writable_projects,
                    invalid_at=invalid_at,
                    reason=target.reason,
                    replacement_entity_id=replacement_entity_id,
                    replacement_source_ids=replacement_source_ids,
                )
            )

    invalidated_entity_ids.extend(
        await _invalidate_promoted_entity_targets(
            runtime=runtime,
            entity_ids=authorized_entity_ids,
            principal_id=principal_id,
            writable_projects=writable_projects,
            invalid_at=invalid_at,
            reason="supersession",
            replacement_entity_id=replacement_entity_id,
            replacement_source_ids=replacement_source_ids,
        )
    )
    invalidated_entity_ids = list(dict.fromkeys(invalidated_entity_ids))
    return {
        "invalidated_source_ids": invalidated_source_ids,
        "invalidated_source_count": len(invalidated_source_ids),
        "invalidated_entity_ids": invalidated_entity_ids,
        "invalidated_entity_count": len(invalidated_entity_ids),
        "invalidation_skipped_source_ids": skipped_source_ids,
        "invalidation_skipped_source_count": len(skipped_source_ids),
    }


def _relationship(
    source_id: str,
    target_id: str,
    relationship_type: RelationshipType,
    *,
    metadata: dict[str, Any],
) -> Relationship:
    return Relationship(
        id=f"rel_{source_id}_{relationship_type.value.lower()}_{target_id}",
        source_id=source_id,
        target_id=target_id,
        relationship_type=relationship_type,
        metadata={**metadata, "created_at": datetime.now(UTC).isoformat()},
    )
