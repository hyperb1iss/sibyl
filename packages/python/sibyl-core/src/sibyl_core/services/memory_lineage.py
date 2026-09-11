"""Propagate source corrections without replacing a derivative's own verdict."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable, Sequence
from dataclasses import dataclass, field, replace

import structlog

from sibyl_core.auth.memory_policy import (
    memory_metadata_read_allowed,
    memory_row_project_id,
    private_scope_granted_for,
)
from sibyl_core.errors import RevisionConflictError
from sibyl_core.memory_pipeline.lifecycle import raw_memory_lifecycle_recallable
from sibyl_core.memory_pipeline.source_lifecycle import (
    CORRECTION_BLOCKERS_KEY,
    correction_event,
    merge_source_correction,
)
from sibyl_core.projection.pending import pending_patch
from sibyl_core.projection.reconcile import RECONCILE_PENDING_KEY
from sibyl_core.services import content_client, content_models
from sibyl_core.services.content_raw_persistence import get_raw_memory, save_raw_memory
from sibyl_core.services.graph_runtime import GraphRuntime
from sibyl_core.services.memory_policy import _authorize_share_source_read

log = structlog.get_logger()
_PAGE_SIZE = 512


@dataclass(slots=True)
class CorrectionPropagation:
    raw_memory_ids: list[str] = field(default_factory=list)
    entity_ids: list[str] = field(default_factory=list)
    complete: bool = True
    # Internal bookkeeping must also reach successfully stamped private rows.
    stamped_entity_ids: list[str] = field(default_factory=list, repr=False)


async def _graph_descendant_ids(
    runtime: GraphRuntime,
    *,
    organization_id: str,
    raw_ids: Sequence[str],
    graph_ids: Sequence[str],
    lookup_failures: set[str] | None = None,
) -> AsyncIterator[str]:
    # Separate predicates keep each lookup index-served. Combining the
    # provenance and projection arms with OR causes a table scan.
    lookups = (
        ("raw_source_ids.*", "idx_entity_raw_sources", "CONTAINSANY", raw_ids, False),
        ("review_capture_id", "idx_entity_review_capture", "IN", raw_ids, False),
        ("raw_memory_id", "idx_entity_raw_memory", "IN", raw_ids, False),
        ("parent_entity_id", "idx_entity_projection_parent", "IN", graph_ids, True),
        ("source_entity_id", "idx_entity_projection_source", "IN", graph_ids, True),
    )
    for attribute, index, operator, values, projection in lookups:
        for batch in content_client.value_batches(values):
            cursor = ""
            try:
                while True:
                    projection_filter = (
                        " AND attributes.projection_kind IS NOT NONE" if projection else ""
                    )
                    rows = content_client.normalize_records(
                        await runtime.client.execute_query(
                            f"SELECT uuid FROM entity WITH INDEX {index} "
                            "WHERE group_id = $group_id AND uuid > $cursor "
                            f"AND attributes.{attribute} {operator} $source_ids"
                            f"{projection_filter} ORDER BY uuid LIMIT $limit;",
                            group_id=organization_id,
                            cursor=cursor,
                            source_ids=batch,
                            limit=_PAGE_SIZE,
                        )
                    )
                    for row in rows:
                        yield str(row["uuid"])
                    if len(rows) < _PAGE_SIZE:
                        break
                    cursor = str(rows[-1]["uuid"])
            except Exception as exc:
                if lookup_failures is None:
                    raise
                _failed_lookup(lookup_failures, index, exc)


def _failed_lookup(failures: set[str], index: str, exc: Exception) -> None:
    failures.add(index)
    log.warning(
        "memory_correction_lineage_lookup_failed", index=index, error_type=type(exc).__name__
    )


async def _raw_descendant_ids(
    *, organization_id: str, source_ids: Sequence[str], lookup_failures: set[str]
) -> AsyncIterator[str]:
    index = "idx_raw_captures_source_lineage"
    if not source_ids:
        return
    try:
        async with content_client.surreal_content_client() as client:
            for batch in content_client.value_batches(source_ids):
                cursor = ""
                try:
                    while True:
                        rows = await content_client.select_many(
                            client,
                            "SELECT uuid FROM raw_captures WITH INDEX idx_raw_captures_source_lineage "
                            "WHERE organization_id = $organization_id AND uuid > $cursor "
                            "AND metadata.raw_source_ids.* CONTAINSANY $source_ids "
                            "ORDER BY uuid LIMIT $limit;",
                            organization_id=organization_id,
                            cursor=cursor,
                            source_ids=batch,
                            limit=_PAGE_SIZE,
                        )
                        for row in rows:
                            yield str(row["uuid"])
                        if len(rows) < _PAGE_SIZE:
                            break
                        cursor = str(rows[-1]["uuid"])
                except Exception as exc:
                    _failed_lookup(lookup_failures, index, exc)
    except Exception as exc:
        _failed_lookup(lookup_failures, index, exc)


async def propagate_source_correction(
    runtime: GraphRuntime | None,
    *,
    memory: content_models.RawMemory,
    principal_id: str | None,
    accessible_projects: Iterable[str] | None,
    accessible_teams: Iterable[str] | None = None,
    accessible_delegations: Iterable[str] | None = None,
    allowed_memory_scope_keys: Iterable[str] | None = None,
    declared_entity_ids: Sequence[str] = (),
    projected_entity_ids: Sequence[str] = (),
) -> CorrectionPropagation:
    """Walk canonical capture IDs and projection links with one clock per root.

    Each row is read and written with a revision check. A conflicting writer
    causes a fresh merge, so different source corrections cannot erase one
    another. The walk has a page size, but no total row or depth ceiling.

    The caller authorizes the source correction and any declared target IDs.
    Server-discovered descendants inherit that correction even when private;
    the caller's read authority controls which IDs the receipt may disclose.
    """
    receipt = CorrectionPropagation(complete=runtime is not None)
    try:
        event = correction_event(
            memory,
            blocking=not raw_memory_lifecycle_recallable(memory, include_source_corrections=False),
        )
    except ValueError:
        return CorrectionPropagation(complete=False)
    projects = None if accessible_projects is None else tuple(accessible_projects)
    teams = None if accessible_teams is None else tuple(accessible_teams)
    delegations = None if accessible_delegations is None else tuple(accessible_delegations)
    grants = None if allowed_memory_scope_keys is None else frozenset(allowed_memory_scope_keys)
    seen_raw = {memory.id}
    seen_graph: set[str] = set()
    raw_frontier = [memory.id]
    graph_frontier: list[str] = list(projected_entity_ids)
    # Direct projections also contain source-era text. The flat source stamp
    # alone cannot retire an old copy after a content revision.
    seeds = list(dict.fromkeys([*projected_entity_ids, *declared_entity_ids]))

    def failed_row(kind: str, exc: Exception) -> None:
        receipt.complete = False
        log.warning(
            "memory_correction_lineage_row_failed",
            source_id=memory.id,
            row_kind=kind,
            error_type=type(exc).__name__,
        )

    async def stamp_graph(entity_id: str) -> bool | None:
        if runtime is None:
            return None
        while True:
            row = await runtime.entity_manager.get(entity_id)
            if row is None:
                receipt.complete = False
                return None
            if row.observed_revision is None:
                receipt.complete = False
                return None
            readable = memory_metadata_read_allowed(
                row.metadata,
                principal_id=principal_id,
                private_scope_granted=private_scope_granted_for(grants, principal_id=principal_id),
                allowed_memory_scope_keys=grants,
                accessible_projects=projects,
                row_project_id=memory_row_project_id(
                    row.metadata,
                    entity_type=str(getattr(row, "entity_type", "") or ""),
                    entity_id=str(getattr(row, "id", "") or ""),
                ),
            )
            metadata = merge_source_correction(row.metadata, event)
            patch: dict[str, object] = {CORRECTION_BLOCKERS_KEY: metadata[CORRECTION_BLOCKERS_KEY]}
            if row.metadata.get("raw_memory_id") == memory.id and row.metadata.get(
                RECONCILE_PENDING_KEY
            ):
                # Reading this capture can clear only its own pending check.
                # A second parent may still be unresolved on the same row.
                resolved = pending_patch(row.metadata, {}, authority=f"capture:{memory.id}")
                if RECONCILE_PENDING_KEY in resolved:
                    patch[RECONCILE_PENDING_KEY] = resolved[RECONCILE_PENDING_KEY]
            if metadata == row.metadata and RECONCILE_PENDING_KEY not in patch:
                return readable
            try:
                updated = await runtime.entity_manager.update(
                    entity_id,
                    {"metadata": patch},
                    expected_revision=row.observed_revision,
                    replace_metadata_keys=tuple(patch),
                )
                if updated is None:
                    receipt.complete = False
                    return None
                return readable
            except RevisionConflictError:
                continue

    async def stamp_raw(memory_id: str) -> bool | None:
        while True:
            row = await get_raw_memory(organization_id=memory.organization_id, memory_id=memory_id)
            if row is None:
                receipt.complete = False
                return None
            if row.observed_revision is None:
                receipt.complete = False
                return None
            readable = _authorize_share_source_read(
                memory=row,
                principal_id=principal_id,
                accessible_projects=projects,
                accessible_teams=teams,
                accessible_delegations=delegations,
                allowed_memory_scope_keys=grants,
            ).allowed
            metadata = merge_source_correction(row.metadata, event)
            if metadata == row.metadata:
                return readable
            try:
                await save_raw_memory(
                    replace(row, metadata=metadata), expected_revision=row.observed_revision
                )
                return readable
            except RevisionConflictError:
                continue

    try:
        while raw_frontier or graph_frontier or seeds:
            next_raw: list[str] = []
            next_graph: list[str] = []
            lookup_failures: set[str] = set()
            async for memory_id in _raw_descendant_ids(
                organization_id=memory.organization_id,
                source_ids=raw_frontier,
                lookup_failures=lookup_failures,
            ):
                if memory_id not in seen_raw:
                    seen_raw.add(memory_id)
                    try:
                        applied = await stamp_raw(memory_id)
                    except Exception as exc:
                        failed_row("capture", exc)
                        applied = None
                    if applied:
                        receipt.raw_memory_ids.append(memory_id)
                    next_raw.append(memory_id)
            if runtime is not None:
                async for entity_id in _graph_descendant_ids(
                    runtime,
                    organization_id=memory.organization_id,
                    raw_ids=raw_frontier,
                    graph_ids=graph_frontier,
                    lookup_failures=lookup_failures,
                ):
                    seeds.append(entity_id)
            if lookup_failures:
                receipt.complete = False
            for entity_id in seeds:
                if entity_id not in seen_graph:
                    seen_graph.add(entity_id)
                    try:
                        applied = await stamp_graph(entity_id)
                    except Exception as exc:
                        failed_row("entity", exc)
                        applied = None
                    if applied is not None:
                        receipt.stamped_entity_ids.append(entity_id)
                    if applied:
                        receipt.entity_ids.append(entity_id)
                    next_graph.append(entity_id)
            raw_frontier, graph_frontier, seeds = next_raw, next_graph, []
    except Exception as exc:
        receipt.complete = False
        log.warning(
            "memory_correction_lineage_incomplete",
            source_id=memory.id,
            error_type=type(exc).__name__,
        )
    return receipt
