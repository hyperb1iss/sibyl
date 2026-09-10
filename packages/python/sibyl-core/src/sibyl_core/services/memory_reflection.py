"""Reflection persistence and promotion orchestration."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sibyl_core.services.memory_source_validation import SourceReadAuthority

import re
from collections.abc import Iterable, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import structlog

from sibyl_core.auth.memory_policy import EVAL_CONSOLIDATION_METADATA_KEY
from sibyl_core.errors import (
    EntityNotFoundError,
    RevisionConflictError,
    SourceObservationConflictError,
)
from sibyl_core.memory_pipeline.lifecycle import (
    RECONCILE_PENDING_KEY,
    graph_lifecycle_stamp,
    graph_metadata_recallable,
)
from sibyl_core.memory_pipeline.observations import SourceObservation
from sibyl_core.memory_pipeline.source_lifecycle import (
    CORRECTION_BLOCKERS_KEY,
    SOURCE_BINDINGS_KEY,
    UNKNOWN_SOURCE_REVISION,
    source_revision_bindings,
)
from sibyl_core.models.entities import EntityType, Relationship
from sibyl_core.models.reflection import ReflectionCandidate
from sibyl_core.projection.pending import pending_patch
from sibyl_core.projection.reconcile import reconcile_with_capture
from sibyl_core.services.graph import get_surreal_graph_runtime
from sibyl_core.services.memory_autonomy import reflection_autonomy_candidate_metadata
from sibyl_core.services.memory_contract import (
    ReflectionPromotionPreview,
    ReflectionPromotionResult,
    ReflectionWriteResult,
    WriteMode,
    _ReflectionPromotionPlan,
    _RelationshipWriteReceipt,
)
from sibyl_core.services.memory_identity import (
    IDENTITY_KEY,
    reflection_entity_id,
    verify_reflection_identity,
)
from sibyl_core.services.memory_lifecycle import (
    _apply_candidate_temporal_invalidations,
    _candidate_temporal_invalidation_targets,
)
from sibyl_core.services.memory_policy import (
    _authorize_reflection_write,
    _authorized_superseded_entity_ids,
    _candidate_source_ids,
    _metadata_str,
    _policy_denial_reason,
    _policy_denied_message,
    _policy_metadata,
    _promotion_denied,
    _promotion_lifecycle_metadata,
    _promotion_preview_from_denial,
    _raw_source_ids,
    _resolve_memory_scope,
    _resolve_scope_key,
    _with_authorized_supersedes,
    raw_memory_source_fingerprint,
)
from sibyl_core.services.memory_policy import (
    raw_memory_source_signature as _promotion_source_signature,
)
from sibyl_core.services.memory_promotion import (
    _broadest_scope,
    _candidate_from_raw_memory,
    _candidate_from_review_memory,
    _coerce_promotion_scope,
    _entity_from_candidate,
    _has_mixed_scope_inputs,
    _is_reflection_candidate,
    _linkable_related_targets,
    _missing_promotion_target_reason,
    _principal_denial,
    _relationships_for_promotion,
    _resolve_promotion_scope_key,
    _scope_metadata,
    _source_scope_denial,
)
from sibyl_core.services.promotion_observations import load_promotion_source
from sibyl_core.services.surreal_content import (
    MemoryScope,
    RawMemory,
    get_raw_memory,
    raw_memory_recallable,
    save_raw_memory,
)
from sibyl_core.tools.responses import AddResponse

log = structlog.get_logger()

_PROMOTED_REVIEW_STATE = "promoted"


def _publication_source_recallable(memory: RawMemory, candidate_id: str | None = None) -> bool:
    if memory.id != candidate_id or not memory.metadata.get(EVAL_CONSOLIDATION_METADATA_KEY):
        return raw_memory_recallable(memory)
    metadata = dict(memory.metadata)
    metadata.pop(EVAL_CONSOLIDATION_METADATA_KEY)
    return raw_memory_recallable(replace(memory, metadata=metadata))


def _publication_graph_recallable(metadata: dict[str, Any], candidate_id: str | None) -> bool:
    if candidate_id is None:
        return graph_metadata_recallable(metadata)
    # Only the designated candidate's pending-publication blocker is waived
    # inside this writer. The stored row remains excluded until finalization.
    checked = dict(metadata)
    blockers = checked.get(CORRECTION_BLOCKERS_KEY)
    if isinstance(blockers, dict):
        checked[CORRECTION_BLOCKERS_KEY] = {
            key: value for key, value in blockers.items() if key != candidate_id
        }
    return graph_metadata_recallable(checked)


async def _load_raw_sources(
    *,
    organization_id: str,
    raw_source_ids: Sequence[str],
) -> list[RawMemory]:
    memories: list[RawMemory] = []
    for source_id in dict.fromkeys(raw_source_ids):
        source = await get_raw_memory(
            organization_id=organization_id,
            memory_id=str(source_id),
        )
        if source is not None:
            memories.append(source)
    return memories


async def _load_promotion_inputs(
    memory: RawMemory, *, organization_id: str
) -> tuple[list[RawMemory], bool, tuple[SourceObservation, ...]]:
    """Resolve every declared retained capture for promotion and sharing."""
    source_ids = _raw_source_ids(memory)
    import asyncio

    snapshots = await asyncio.gather(
        *(load_promotion_source(organization_id, source_id) for source_id in source_ids)
    )
    sources = [snapshot.memory for snapshot in snapshots if snapshot is not None]
    observations = tuple(snapshot.observation for snapshot in snapshots if snapshot is not None)
    required = set(source_ids)
    loaded = {source.id for source in sources}
    if memory.source_id in required and re.fullmatch(
        r"reflection:input:[0-9a-f]{16}", memory.source_id
    ):
        # Unretained input anchors have no stored source or revision. Present
        # rows still participate in authorization and lifecycle checks.
        required.remove(memory.source_id)
        loaded.discard(memory.source_id)
    return [memory, *sources], required == loaded, observations


async def persist_reflection_source(
    *,
    title: str,
    content: str,
    organization_id: str,
    principal_id: str | None,
    domain: str | None = None,
    project: str | None = None,
    related_to: Sequence[str] | None = None,
    accessible_projects: Iterable[str] | None = None,
    writable_projects: Iterable[str] | None = None,
    accessible_teams: Iterable[str] | None = None,
    accessible_delegations: Iterable[str] | None = None,
    memory_scope: MemoryScope | str | None = None,
    scope_key: str | None = None,
) -> ReflectionWriteResult:
    accessible_projects = (
        frozenset(accessible_projects) if accessible_projects is not None else None
    )
    writable_projects = frozenset(writable_projects) if writable_projects is not None else None
    candidate = ReflectionCandidate(
        kind=EntityType.SESSION.value,
        title=title,
        content=content,
        reason="preserves raw reflection source material",
        confidence=1.0,
        tags=["reflection", EntityType.SESSION.value],
        metadata={"reflection_source": True},
    )
    return await persist_reflection_candidate(
        candidate=candidate,
        organization_id=organization_id,
        principal_id=principal_id,
        domain=domain,
        project=project,
        source_id=None,
        related_to=related_to,
        accessible_projects=accessible_projects,
        writable_projects=writable_projects,
        accessible_teams=accessible_teams,
        accessible_delegations=accessible_delegations,
        memory_scope=memory_scope,
        scope_key=scope_key,
    )


async def persist_reflection_candidate(
    *,
    candidate: ReflectionCandidate,
    organization_id: str,
    principal_id: str | None,
    domain: str | None = None,
    project: str | None = None,
    source_id: str | None = None,
    related_to: Sequence[str] | None = None,
    accessible_projects: Iterable[str] | None = None,
    writable_projects: Iterable[str] | None = None,
    accessible_teams: Iterable[str] | None = None,
    accessible_delegations: Iterable[str] | None = None,
    memory_scope: MemoryScope | str | None = None,
    scope_key: str | None = None,
    link_source_entity: bool = True,
    source_memories: Sequence[RawMemory] = (),
    source_observations: Sequence[SourceObservation] = (),
    source_authority: SourceReadAuthority | None = None,
    reserved_entity_id: str | None = None,
    publication_candidate_id: str | None = None,
) -> ReflectionWriteResult:
    writable_projects = frozenset(writable_projects) if writable_projects is not None else None
    accessible_projects = (
        frozenset(accessible_projects) if accessible_projects is not None else None
    )
    scope = _resolve_memory_scope(memory_scope, project)
    resolved_scope_key = _resolve_scope_key(scope, scope_key, project)
    policy_decisions = _authorize_reflection_write(
        principal_id=principal_id,
        memory_scope=scope,
        scope_key=resolved_scope_key,
        accessible_projects=writable_projects
        if writable_projects is not None
        else accessible_projects,
        accessible_teams=accessible_teams,
        accessible_delegations=accessible_delegations,
    )
    policy_metadata = _policy_metadata(policy_decisions)
    if any(not decision.allowed for decision in policy_decisions):
        return ReflectionWriteResult(
            response=AddResponse(
                success=False,
                id=None,
                message=_policy_denied_message(policy_decisions),
                timestamp=datetime.now(UTC),
            ),
            metadata=policy_metadata,
        )

    if source_observations:
        from sibyl_core.services.memory_source_validation import SourceReadAuthority
        from sibyl_core.services.promotion_observations import promotion_observations_current

        current_authority = SourceReadAuthority(
            principal_id=str(principal_id or ""),
            projects=frozenset(accessible_projects or ()),
            teams=frozenset(accessible_teams or ()),
            delegations=frozenset(accessible_delegations or ()),
        )
        if source_authority is not None:
            if source_authority.principal_id != current_authority.principal_id:
                return _retired_reflection_result(reserved_entity_id or "")
            source_authority = replace(
                source_authority,
                projects=source_authority.projects & current_authority.projects,
                teams=source_authority.teams & current_authority.teams,
                delegations=source_authority.delegations & current_authority.delegations,
            )
        else:
            source_authority = current_authority
        if not await promotion_observations_current(
            source_observations, source_authority, organization_id
        ):
            return _retired_reflection_result(reserved_entity_id or "")
    runtime = await get_surreal_graph_runtime(organization_id)
    source_ids = _candidate_source_ids(candidate, source_id)
    superseded_ids = await _authorized_superseded_entity_ids(
        runtime=runtime,
        principal_id=principal_id,
        writable_projects=writable_projects,
        candidate=candidate,
    )
    entity = _entity_from_candidate(
        candidate,
        organization_id=organization_id,
        principal_id=principal_id,
        domain=domain,
        project=project,
        source_id=source_id,
        memory_scope=scope,
        scope_key=resolved_scope_key,
        policy_metadata=policy_metadata,
        source_memories=source_memories or (),
        reserved_entity_id=reserved_entity_id,
    )
    entity = entity.model_copy(
        update={
            "metadata": _with_authorized_supersedes(
                _promotion_lifecycle_metadata(
                    metadata=entity.metadata,
                    promoted_entity_id=entity.id,
                    source_ids=source_ids,
                    source_id=source_ids[0] if source_ids else None,
                    reason=candidate.reason,
                    policy_metadata=policy_metadata,
                ),
                superseded_ids,
            )
        }
    )
    # Resolved before the row lands. A store that fails here fails the whole
    # promotion rather than persisting a memory and then reporting a complete
    # write that requested no edges at all.
    legacy_reservation = (
        reserved_entity_id is not None and entity.metadata[IDENTITY_KEY]["version"] == 2
    )
    if legacy_reservation:
        # The reserved output predates durable source observations. Its existing
        # unknown-revision reconciliation must not acquire newly observed trust.
        source_observations = ()
    if source_memories:
        bindings = source_revision_bindings(source_memories)
        if legacy_reservation:
            # A v2 reservation proves its output, not the source epochs it read.
            bindings = dict.fromkeys(bindings, UNKNOWN_SOURCE_REVISION)
            for memory in source_memories:
                entity.metadata.update(
                    pending_patch(
                        entity.metadata,
                        {RECONCILE_PENDING_KEY: True},
                        authority=f"capture:{memory.id}",
                    )
                )
        entity.metadata[SOURCE_BINDINGS_KEY] = bindings
    linkable_related_to = await _linkable_related_targets(
        runtime=runtime,
        related_to=related_to,
        principal_id=principal_id,
        accessible_projects=accessible_projects,
    )
    created_id = entity.id
    native_write_path = _metadata_str(candidate.metadata, "native_write_path")
    if not native_write_path:
        native_write_path = "reflection_promotion"
    relationships = _relationships_for_promotion(
        created_id,
        project=project,
        source_id=source_id if link_source_entity else None,
        related_to=linkable_related_to,
        supersedes=superseded_ids,
        raw_source_ids=source_ids,
        native_write_path=native_write_path,
    )
    publication_request = {
        "relationships": sorted(relationship.id for relationship in relationships),
        "invalidation_targets": sorted(
            [target.source_id, target.reason]
            for target in _candidate_temporal_invalidation_targets(candidate)
        ),
        "invalidation_cutoff": {
            key: str(candidate.metadata[key])
            for key in ("valid_at", "valid_from", "occurred_at")
            if candidate.metadata.get(key) is not None
        },
    }
    if publication_candidate_id is not None:
        publication_candidate = next(
            (memory for memory in source_memories if memory.id == publication_candidate_id), None
        )
        if publication_candidate is None or not publication_candidate.metadata.get(
            EVAL_CONSOLIDATION_METADATA_KEY
        ):
            raise ValueError("publication candidate must be a retained consolidation input")
        publication_stamp = graph_lifecycle_stamp(publication_candidate)
        publication_stamp.pop(SOURCE_BINDINGS_KEY, None)
        entity.metadata.update(publication_stamp)
    entity.metadata["reflection_publication"] = {
        "state": "pending",
        "request": publication_request,
    }
    if not await _verify_promotion_sources(
        runtime,
        source_memories,
        publication_candidate_id=publication_candidate_id,
        source_observations=source_observations,
        source_authority=source_authority,
    ):
        return _retired_reflection_result(entity.id)
    derivation = None
    if source_observations:
        from dataclasses import asdict

        if source_authority is None:
            raise ValueError("source authority is required for graph derivation")
        derivation = {
            "organization_id": organization_id,
            "target_kind": "graph_entity",
            "target_id": entity.id,
            "principal_id": source_authority.principal_id,
            "authority_ceiling": source_authority.ceiling_metadata(),
            "observations": [asdict(observation) for observation in source_observations],
            "active": True,
        }
    if derivation is None:
        stored, created = await runtime.entity_manager.create_direct_if_absent(entity)
    else:
        stored, created = await runtime.entity_manager.create_direct_if_absent(
            entity, derivation=derivation
        )
    verify_reflection_identity(entity, stored)
    if legacy_reservation:
        for memory in source_memories:
            await reconcile_with_capture(
                runtime.entity_manager,
                organization_id=organization_id,
                metadata={"raw_memory_id": memory.id},
                row_ids=[stored.id],
            )
        stored = await runtime.entity_manager.get(stored.id)
        verify_reflection_identity(entity, stored)
    if not await _verify_promotion_sources(
        runtime,
        source_memories,
        source_observations=source_observations,
        source_authority=source_authority,
        publication_candidate_id=publication_candidate_id,
        entity_id=stored.id,
    ):
        return _retired_reflection_result(stored.id)
    if not _publication_graph_recallable(stored.metadata, publication_candidate_id):
        return _retired_reflection_result(stored.id)
    prior_publication = stored.metadata.get("reflection_publication", {})
    if (
        not created
        and prior_publication.get("state") == "complete"
        and prior_publication.get("request") == publication_request
    ):
        return _reflection_publication_result(
            stored.id,
            candidate.title,
            prior_publication["receipt"],
            "replayed",
        )
    relationship_receipt = await _write_promotion_relationships(
        runtime.relationship_manager,
        relationships,
    )

    invalidation_metadata = await _apply_candidate_temporal_invalidations(
        runtime=runtime,
        organization_id=organization_id,
        principal_id=principal_id,
        writable_projects=writable_projects,
        candidate=candidate,
        replacement_entity_id=created_id,
        replacement_source_ids=source_ids,
        authorized_entity_ids=superseded_ids,
    )

    receipt = {
        **policy_metadata,
        "native_write_mode": WriteMode.ENABLED.value,
        "native_write_path": native_write_path,
        "native_relationship_count": relationship_receipt.created,
        "native_relationship_requested_count": relationship_receipt.requested,
        "native_relationship_failed_count": relationship_receipt.failed,
        "promotion_state": relationship_receipt.state,
        "promotion_errors": list(relationship_receipt.errors),
        "raw_source_ids": source_ids,
        "source_ids": source_ids,
        **invalidation_metadata,
    }
    publication = {
        "state": relationship_receipt.state,
        "request": publication_request,
        "receipt": _public_publication_receipt(receipt),
    }
    try:
        updated = await runtime.entity_manager.update(
            stored.id,
            {"metadata": {"reflection_publication": publication}},
            expected_revision=stored.revision,
            replace_metadata_keys=("reflection_publication",),
        )
    except RevisionConflictError:
        try:
            current = await runtime.entity_manager.get(stored.id)
        except (KeyError, EntityNotFoundError) as exc:
            raise RuntimeError("reflection evidence disappeared during publication") from exc
        if current is None:
            raise RuntimeError("reflection evidence disappeared during publication") from None
        verify_reflection_identity(entity, current)
        if not await _verify_promotion_sources(
            runtime,
            source_memories,
            source_observations=source_observations,
            source_authority=source_authority,
            publication_candidate_id=publication_candidate_id,
            entity_id=current.id,
        ):
            return _retired_reflection_result(current.id)
        if not _publication_graph_recallable(current.metadata, publication_candidate_id):
            return _retired_reflection_result(current.id)
        current_publication = current.metadata.get("reflection_publication", {})
        if (
            current_publication.get("state") == "complete"
            and current_publication.get("request") == publication_request
        ):
            return _reflection_publication_result(
                current.id,
                candidate.title,
                current_publication["receipt"],
                "replayed",
            )
        raise
    if updated is None:
        raise RuntimeError("reflection evidence disappeared during publication")
    if not await _verify_promotion_sources(
        runtime,
        source_memories,
        source_observations=source_observations,
        source_authority=source_authority,
        publication_candidate_id=publication_candidate_id,
        entity_id=stored.id,
    ):
        return _retired_reflection_result(stored.id)
    return _reflection_publication_result(
        stored.id,
        candidate.title,
        receipt,
        "created" if created else "resumed",
    )


def _retired_reflection_result(entity_id: str) -> ReflectionWriteResult:
    return ReflectionWriteResult(
        response=AddResponse(
            success=False,
            id=entity_id,
            message="Existing reflection evidence is retired; use an explicit lifecycle action",
            timestamp=datetime.now(UTC),
        ),
        metadata={"promotion_state": "denied", "publication_outcome": "retired"},
    )


def _reflection_publication_result(
    entity_id: str,
    title: str,
    receipt: dict[str, Any],
    outcome: str,
) -> ReflectionWriteResult:
    return ReflectionWriteResult(
        response=AddResponse(
            success=True,
            id=entity_id,
            message=f"Promoted natively: {title}",
            timestamp=datetime.now(UTC),
        ),
        metadata={
            "scope_key": None,
            "invalidation_details_available": True,
            **receipt,
            "publication_outcome": outcome,
        },
    )


def _public_publication_receipt(receipt: dict[str, Any]) -> dict[str, Any]:
    """A target-scoped row must not reveal the writer's private invalidation results."""
    public_keys = (
        "memory_scope",
        "scope_key",
        "policy_allowed",
        "policy_reasons",
        "policy_actions",
        "native_write_mode",
        "native_write_path",
        "native_relationship_count",
        "native_relationship_requested_count",
        "native_relationship_failed_count",
        "promotion_state",
        "raw_source_ids",
        "source_ids",
    )
    return {
        **{key: receipt[key] for key in public_keys if key in receipt},
        "promotion_errors": ["Relationship publication incomplete"]
        if receipt.get("promotion_errors")
        else [],
        "invalidation_details_available": False,
    }


async def _write_promotion_relationships(
    relationship_manager: Any,
    relationships: Sequence[Relationship],
) -> _RelationshipWriteReceipt:
    requested = len(relationships)
    if not relationships:
        return _RelationshipWriteReceipt()
    try:
        created, failed = await relationship_manager.create_bulk(relationships)
    except Exception as exc:
        log.warning(
            "reflection_promotion_relationships_failed",
            relationships=requested,
            error_type=type(exc).__name__,
        )
        return _RelationshipWriteReceipt(
            requested=requested,
            failed=requested,
            errors=(str(exc),),
        )
    return _RelationshipWriteReceipt(
        requested=requested,
        created=created,
        failed=failed,
        errors=(f"{failed} promotion relationships failed",) if failed else (),
    )


async def promote_reflection_candidate_review(
    *,
    candidate_id: str,
    organization_id: str,
    principal_id: str | None,
    promote_to_scope: MemoryScope | str | None,
    promote_to_scope_key: str | None = None,
    domain: str | None = None,
    project: str | None = None,
    related_to: Sequence[str] | None = None,
    accessible_projects: Iterable[str] | None = None,
    writable_projects: Iterable[str] | None = None,
    accessible_teams: Iterable[str] | None = None,
    accessible_delegations: Iterable[str] | None = None,
    allowed_memory_scope_keys: Iterable[str] | None = None,
) -> ReflectionPromotionResult:
    accessible_projects = (
        frozenset(accessible_projects) if accessible_projects is not None else None
    )
    writable_projects = frozenset(writable_projects) if writable_projects is not None else None
    plan = await _resolve_reflection_promotion_plan(
        allowed_memory_scope_keys=allowed_memory_scope_keys,
        candidate_id=candidate_id,
        organization_id=organization_id,
        principal_id=principal_id,
        promote_to_scope=promote_to_scope,
        promote_to_scope_key=promote_to_scope_key,
        domain=domain,
        project=project,
        accessible_projects=accessible_projects,
        accessible_teams=accessible_teams,
        accessible_delegations=accessible_delegations,
    )
    if isinstance(plan, ReflectionPromotionResult):
        return plan

    return await _apply_promotion_plan(
        plan=plan,
        organization_id=organization_id,
        principal_id=principal_id,
        domain=domain,
        related_to=related_to,
        accessible_projects=accessible_projects,
        writable_projects=writable_projects,
        accessible_teams=accessible_teams,
        accessible_delegations=accessible_delegations,
        native_source_id=plan.raw_source_ids[0] if plan.raw_source_ids else None,
        lifecycle_source_id=plan.candidate_memory.id,
        lifecycle_reason="accepted_reflection_candidate",
    )


async def promote_raw_memory(
    *,
    raw_memory_id: str,
    organization_id: str,
    principal_id: str | None,
    promote_to_scope: MemoryScope | str | None,
    promote_to_scope_key: str | None = None,
    domain: str | None = None,
    project: str | None = None,
    related_to: Sequence[str] | None = None,
    accessible_projects: Iterable[str] | None = None,
    writable_projects: Iterable[str] | None = None,
    accessible_teams: Iterable[str] | None = None,
    accessible_delegations: Iterable[str] | None = None,
    allowed_memory_scope_keys: Iterable[str] | None = None,
) -> ReflectionPromotionResult:
    accessible_projects = (
        frozenset(accessible_projects) if accessible_projects is not None else None
    )
    writable_projects = frozenset(writable_projects) if writable_projects is not None else None
    plan = await _resolve_raw_memory_promotion_plan(
        allowed_memory_scope_keys=allowed_memory_scope_keys,
        raw_memory_id=raw_memory_id,
        organization_id=organization_id,
        principal_id=principal_id,
        promote_to_scope=promote_to_scope,
        promote_to_scope_key=promote_to_scope_key,
        domain=domain,
        project=project,
        accessible_projects=accessible_projects,
        accessible_teams=accessible_teams,
        accessible_delegations=accessible_delegations,
    )
    if isinstance(plan, ReflectionPromotionResult):
        return plan

    return await _apply_promotion_plan(
        plan=plan,
        organization_id=organization_id,
        principal_id=principal_id,
        domain=domain,
        related_to=related_to,
        accessible_projects=accessible_projects,
        writable_projects=writable_projects,
        accessible_teams=accessible_teams,
        accessible_delegations=accessible_delegations,
        native_source_id=plan.candidate_memory.id,
        lifecycle_source_id=plan.candidate_memory.id,
        lifecycle_reason="accepted_raw_memory",
    )


async def preview_reflection_candidate_promotion(
    *,
    candidate_id: str,
    organization_id: str,
    principal_id: str | None,
    promote_to_scope: MemoryScope | str | None,
    promote_to_scope_key: str | None = None,
    domain: str | None = None,
    project: str | None = None,
    accessible_projects: Iterable[str] | None = None,
    writable_projects: Iterable[str] | None = None,
    accessible_teams: Iterable[str] | None = None,
    accessible_delegations: Iterable[str] | None = None,
) -> ReflectionPromotionPreview:
    accessible_projects = (
        frozenset(accessible_projects) if accessible_projects is not None else None
    )
    writable_projects = frozenset(writable_projects) if writable_projects is not None else None
    plan = await _resolve_reflection_promotion_plan(
        candidate_id=candidate_id,
        organization_id=organization_id,
        principal_id=principal_id,
        promote_to_scope=promote_to_scope,
        promote_to_scope_key=promote_to_scope_key,
        domain=domain,
        project=project,
        accessible_projects=accessible_projects,
        accessible_teams=accessible_teams,
        accessible_delegations=accessible_delegations,
    )
    if isinstance(plan, ReflectionPromotionResult):
        return _promotion_preview_from_denial(plan)

    policy_decisions = _authorize_reflection_write(
        principal_id=principal_id,
        memory_scope=plan.target_scope,
        scope_key=plan.target_scope_key,
        accessible_projects=writable_projects
        if writable_projects is not None
        else accessible_projects,
        accessible_teams=accessible_teams,
        accessible_delegations=accessible_delegations,
    )
    metadata = {
        **_policy_metadata(policy_decisions),
        **reflection_autonomy_candidate_metadata(plan.candidate_memory),
        "input_scopes": _scope_metadata(plan.input_memories),
        "source_count": len(plan.raw_source_ids),
        "target_project": plan.target_project,
    }
    allowed = all(decision.allowed for decision in policy_decisions)
    return ReflectionPromotionPreview(
        allowed=allowed,
        candidate_id=plan.candidate_memory.id,
        reason="promotion_preview_allowed" if allowed else _policy_denial_reason(metadata),
        review_state=plan.candidate_memory.review_state,
        memory_scope=plan.target_scope,
        scope_key=plan.target_scope_key,
        raw_source_ids=plan.raw_source_ids,
        policy_decisions=policy_decisions,
        metadata=metadata,
    )


async def preview_raw_memory_promotion(
    *,
    raw_memory_id: str,
    organization_id: str,
    principal_id: str | None,
    promote_to_scope: MemoryScope | str | None,
    promote_to_scope_key: str | None = None,
    domain: str | None = None,
    project: str | None = None,
    accessible_projects: Iterable[str] | None = None,
    writable_projects: Iterable[str] | None = None,
    accessible_teams: Iterable[str] | None = None,
    accessible_delegations: Iterable[str] | None = None,
) -> ReflectionPromotionPreview:
    accessible_projects = (
        frozenset(accessible_projects) if accessible_projects is not None else None
    )
    writable_projects = frozenset(writable_projects) if writable_projects is not None else None
    plan = await _resolve_raw_memory_promotion_plan(
        raw_memory_id=raw_memory_id,
        organization_id=organization_id,
        principal_id=principal_id,
        promote_to_scope=promote_to_scope,
        promote_to_scope_key=promote_to_scope_key,
        domain=domain,
        project=project,
        accessible_projects=accessible_projects,
        accessible_teams=accessible_teams,
        accessible_delegations=accessible_delegations,
    )
    if isinstance(plan, ReflectionPromotionResult):
        return _promotion_preview_from_denial(plan)

    policy_decisions = _authorize_reflection_write(
        principal_id=principal_id,
        memory_scope=plan.target_scope,
        scope_key=plan.target_scope_key,
        accessible_projects=writable_projects
        if writable_projects is not None
        else accessible_projects,
        accessible_teams=accessible_teams,
        accessible_delegations=accessible_delegations,
    )
    metadata = {
        **_policy_metadata(policy_decisions),
        "input_scopes": _scope_metadata(plan.input_memories),
        "source_count": len(plan.raw_source_ids),
        "source_family": "raw_memory",
        "target_project": plan.target_project,
    }
    allowed = all(decision.allowed for decision in policy_decisions)
    return ReflectionPromotionPreview(
        allowed=allowed,
        candidate_id=plan.candidate_memory.id,
        reason="promotion_preview_allowed" if allowed else _policy_denial_reason(metadata),
        review_state=plan.candidate_memory.review_state,
        memory_scope=plan.target_scope,
        scope_key=plan.target_scope_key,
        raw_source_ids=plan.raw_source_ids,
        policy_decisions=policy_decisions,
        metadata=metadata,
    )


async def _apply_promotion_plan(
    *,
    plan: _ReflectionPromotionPlan,
    organization_id: str,
    principal_id: str | None,
    domain: str | None,
    related_to: Sequence[str] | None,
    accessible_projects: Iterable[str] | None,
    writable_projects: Iterable[str] | None = None,
    accessible_teams: Iterable[str] | None,
    accessible_delegations: Iterable[str] | None,
    native_source_id: str | None,
    lifecycle_source_id: str,
    lifecycle_reason: str,
) -> ReflectionPromotionResult:
    accessible_projects = (
        frozenset(accessible_projects) if accessible_projects is not None else None
    )
    writable_projects = frozenset(writable_projects) if writable_projects is not None else None
    policy_decisions = _authorize_reflection_write(
        principal_id=principal_id,
        memory_scope=plan.target_scope,
        scope_key=plan.target_scope_key,
        accessible_projects=writable_projects
        if writable_projects is not None
        else accessible_projects,
        accessible_teams=accessible_teams,
        accessible_delegations=accessible_delegations,
    )
    policy_metadata = _policy_metadata(policy_decisions)
    if any(not decision.allowed for decision in policy_decisions):
        return _promotion_denied(
            candidate_id=plan.candidate_memory.id,
            reason=_policy_denial_reason(policy_metadata),
            review_state=plan.candidate_memory.review_state,
            memory_scope=plan.target_scope,
            scope_key=plan.target_scope_key,
            raw_source_ids=plan.raw_source_ids,
            metadata=policy_metadata,
        )
    prospective = _entity_from_candidate(
        plan.promotion_candidate,
        organization_id=organization_id,
        principal_id=principal_id,
        domain=domain or _metadata_str(plan.candidate_memory.metadata, "domain"),
        project=plan.target_project,
        source_id=native_source_id,
        memory_scope=plan.target_scope,
        scope_key=_resolve_scope_key(plan.target_scope, plan.target_scope_key, plan.target_project),
        policy_metadata=policy_metadata,
        source_memories=plan.input_memories,
    )
    reservation = await _reserve_promotion(
        plan,
        prospective.id,
        legacy_entity_id=reflection_entity_id(
            prospective.model_copy(update={"name": plan.promotion_candidate.title}), version=2
        ),
    )
    if isinstance(reservation, ReflectionPromotionResult):
        return reservation
    plan = reservation
    result = await persist_reflection_candidate(
        candidate=plan.promotion_candidate,
        organization_id=organization_id,
        principal_id=principal_id,
        domain=domain or _metadata_str(plan.candidate_memory.metadata, "domain"),
        project=plan.target_project,
        source_id=native_source_id,
        related_to=related_to,
        accessible_projects=accessible_projects,
        writable_projects=writable_projects,
        accessible_teams=accessible_teams,
        accessible_delegations=accessible_delegations,
        memory_scope=plan.target_scope,
        scope_key=plan.target_scope_key,
        link_source_entity=False,
        source_memories=plan.input_memories,
        source_observations=plan.source_observations,
        source_authority=plan.source_authority,
        reserved_entity_id=_metadata_str(plan.candidate_memory.metadata, "promoted_entity_id"),
        publication_candidate_id=plan.candidate_memory.id
        if plan.candidate_memory.metadata.get(EVAL_CONSOLIDATION_METADATA_KEY)
        else None,
    )
    if not result.response.success or result.metadata.get("promotion_state") == "partial":
        return _promotion_write_denied(plan=plan, result=result)
    return await _mark_promotion_plan_promoted(
        plan=plan,
        result=result,
        lifecycle_source_id=lifecycle_source_id,
        lifecycle_reason=lifecycle_reason,
    )


async def _verify_promotion_sources(
    runtime: Any,
    memories: Sequence[RawMemory],
    *,
    source_observations: Sequence[SourceObservation] = (),
    source_authority: SourceReadAuthority | None = None,
    entity_id: str | None = None,
    publication_candidate_id: str | None = None,
) -> bool:
    """Close correction's read-before-insert gap without restoring retired rows."""
    verified = True
    if source_observations:
        from sibyl_core.services.promotion_observations import promotion_observations_current

        if source_authority is None or not await promotion_observations_current(
            source_observations, source_authority, runtime.client.group_id
        ):
            verified = False
    for expected in memories:
        signature = raw_memory_source_fingerprint(expected)
        try:
            current = await get_raw_memory(
                organization_id=expected.organization_id, memory_id=expected.id
            )
        except Exception as exc:
            log.warning("promotion_source_read_failed", error_type=type(exc).__name__)
            current = None
        if current is not None and current.id == publication_candidate_id:
            from sibyl_core.services.eval_publication_guards import verify_publication_admissions

            if not await verify_publication_admissions(current):
                verified = False
                continue
        if (
            current is not None
            and _publication_source_recallable(current, publication_candidate_id)
            and _promotion_source_signature(current) == _promotion_source_signature(expected)
        ):
            continue
        if entity_id is not None:
            await reconcile_with_capture(
                runtime.entity_manager,
                organization_id=expected.organization_id,
                metadata={"raw_memory_id": expected.id},
                row_ids=[entity_id],
                expected_signature=signature,
            )
        verified = False
    return verified


async def _reserve_promotion(
    plan: _ReflectionPromotionPlan,
    entity_id: str,
    *,
    legacy_entity_id: str | None = None,
) -> _ReflectionPromotionPlan | ReflectionPromotionResult:
    """Reserve correction's existing pointer before publishing any graph row."""
    memory = plan.candidate_memory
    recorded_id = _metadata_str(memory.metadata, "promoted_entity_id")
    if recorded_id or memory.review_state == _PROMOTED_REVIEW_STATE:
        if not recorded_id or (recorded_id != entity_id and recorded_id != legacy_entity_id):
            return _promotion_denied(
                candidate_id=memory.id,
                reason="candidate_already_promoted",
                review_state=memory.review_state,
                memory_scope=plan.target_scope,
                scope_key=plan.target_scope_key,
                raw_source_ids=plan.raw_source_ids,
            )
        return plan
    try:
        reserved = await save_raw_memory(
            replace(
                memory,
                metadata={
                    **memory.metadata,
                    "promoted_entity_id": entity_id,
                    "promotion_state": "pending",
                },
            ),
            expected_revision=memory.revision,
            **(
                {"publication_operation_id": str(memory.metadata[EVAL_CONSOLIDATION_METADATA_KEY])}
                if memory.metadata.get(EVAL_CONSOLIDATION_METADATA_KEY)
                else {}
            ),
        )
    except SourceObservationConflictError:
        return _promotion_denied(
            candidate_id=memory.id,
            reason="original_admission_changed",
            review_state=memory.review_state,
            memory_scope=memory.memory_scope,
            scope_key=memory.scope_key,
            raw_source_ids=plan.raw_source_ids,
        )
    except RevisionConflictError:
        current = await get_raw_memory(organization_id=memory.organization_id, memory_id=memory.id)
        if current is None or not _publication_source_recallable(current, memory.id):
            return _promotion_denied(
                candidate_id=memory.id,
                reason="source_not_recallable",
                review_state=current.review_state if current else "missing",
                memory_scope=plan.target_scope,
                scope_key=plan.target_scope_key,
                raw_source_ids=plan.raw_source_ids,
            )
        recorded_id = _metadata_str(current.metadata, "promoted_entity_id")
        if (
            not recorded_id
            or (recorded_id != entity_id and recorded_id != legacy_entity_id)
            or _promotion_source_signature(current) != _promotion_source_signature(memory)
        ):
            return _promotion_denied(
                candidate_id=memory.id,
                reason="candidate_already_promoted",
                review_state=current.review_state,
                memory_scope=plan.target_scope,
                scope_key=plan.target_scope_key,
                raw_source_ids=plan.raw_source_ids,
            )
        reserved = current
    return replace(
        plan,
        candidate_memory=reserved,
        input_memories=[
            reserved if source.id == memory.id else source for source in plan.input_memories
        ],
    )


def _promotion_write_denied(
    *,
    plan: _ReflectionPromotionPlan,
    result: ReflectionWriteResult,
) -> ReflectionPromotionResult:
    return ReflectionPromotionResult(
        success=False,
        candidate_id=plan.candidate_memory.id,
        promoted_id=result.response.id,
        reason="promotion_incomplete"
        if result.metadata.get("promotion_state") == "partial"
        else result.metadata.get("publication_outcome") or _policy_denial_reason(result.metadata),
        review_state=plan.candidate_memory.review_state,
        memory_scope=plan.target_scope,
        scope_key=plan.target_scope_key,
        raw_source_ids=plan.raw_source_ids,
        metadata=result.metadata,
    )


async def _mark_promotion_plan_promoted(
    *,
    plan: _ReflectionPromotionPlan,
    result: ReflectionWriteResult,
    lifecycle_source_id: str,
    lifecycle_reason: str,
) -> ReflectionPromotionResult:
    if (
        result.metadata.get("publication_outcome") == "replayed"
        and plan.candidate_memory.review_state == _PROMOTED_REVIEW_STATE
        and plan.candidate_memory.metadata.get("promoted_entity_id") == result.response.id
    ):
        metadata = {**plan.candidate_memory.metadata, **result.metadata}
    else:
        metadata = _promoted_candidate_metadata(
            plan=plan,
            result=result,
            lifecycle_source_id=lifecycle_source_id,
            lifecycle_reason=lifecycle_reason,
        )
        try:
            await save_raw_memory(
                replace(
                    plan.candidate_memory,
                    review_state=_PROMOTED_REVIEW_STATE,
                    metadata=metadata,
                ),
                expected_revision=plan.candidate_memory.revision,
                **(
                    {"source_observations": plan.input_memories}
                    if plan.candidate_memory.metadata.get(EVAL_CONSOLIDATION_METADATA_KEY)
                    else {}
                ),
            )
        except SourceObservationConflictError:
            return _promotion_write_denied(
                plan=plan, result=_retired_reflection_result(str(result.response.id))
            )
        except RevisionConflictError:
            current = await get_raw_memory(
                organization_id=plan.candidate_memory.organization_id,
                memory_id=plan.candidate_memory.id,
            )
            if (
                current is not None
                and raw_memory_recallable(current)
                and current.review_state == _PROMOTED_REVIEW_STATE
                and current.metadata.get("promoted_entity_id") == result.response.id
                and _promotion_source_signature(current)
                == _promotion_source_signature(plan.candidate_memory)
            ):
                metadata = {**current.metadata, **result.metadata}
            else:
                runtime = await get_surreal_graph_runtime(plan.candidate_memory.organization_id)
                if not await _verify_promotion_sources(
                    runtime, plan.input_memories, entity_id=result.response.id
                ):
                    return _promotion_write_denied(
                        plan=plan,
                        result=_retired_reflection_result(str(result.response.id)),
                    )
                raise
    if plan.candidate_memory.metadata.get(EVAL_CONSOLIDATION_METADATA_KEY):
        runtime = await get_surreal_graph_runtime(plan.candidate_memory.organization_id)
        reconciled = await reconcile_with_capture(
            runtime.entity_manager,
            organization_id=plan.candidate_memory.organization_id,
            metadata={"raw_memory_id": plan.candidate_memory.id},
            row_ids=[str(result.response.id)],
        )
        stored = await runtime.entity_manager.get(str(result.response.id))
        if reconciled.unverified or not graph_metadata_recallable(stored.metadata):
            return _promotion_write_denied(
                plan=plan, result=_retired_reflection_result(str(result.response.id))
            )
    return ReflectionPromotionResult(
        success=True,
        candidate_id=plan.candidate_memory.id,
        promoted_id=result.response.id,
        reason="promoted",
        review_state=_PROMOTED_REVIEW_STATE,
        memory_scope=plan.target_scope,
        scope_key=plan.target_scope_key,
        raw_source_ids=plan.raw_source_ids,
        metadata=metadata,
    )


def _promoted_candidate_metadata(
    *,
    plan: _ReflectionPromotionPlan,
    result: ReflectionWriteResult,
    lifecycle_source_id: str,
    lifecycle_reason: str,
) -> dict[str, object]:
    promoted_id = result.response.id
    metadata = {
        **plan.candidate_memory.metadata,
        **result.metadata,
        "review_state": _PROMOTED_REVIEW_STATE,
        "promoted_at": datetime.now(UTC).isoformat(),
        "promoted_entity_id": promoted_id,
        "promote_to_scope": plan.target_scope.value,
        "promote_to_scope_key": plan.target_scope_key,
        "raw_source_ids": plan.raw_source_ids,
        "source_ids": plan.raw_source_ids,
    }
    return _promotion_lifecycle_metadata(
        metadata=metadata,
        promoted_entity_id=str(promoted_id),
        source_ids=plan.raw_source_ids,
        source_id=lifecycle_source_id,
        reason=lifecycle_reason,
        policy_metadata=result.metadata,
    )


async def _resolve_reflection_promotion_plan(
    *,
    candidate_id: str,
    organization_id: str,
    principal_id: str | None,
    promote_to_scope: MemoryScope | str | None,
    promote_to_scope_key: str | None = None,
    domain: str | None = None,
    project: str | None = None,
    accessible_projects: Iterable[str] | None = None,
    accessible_teams: Iterable[str] | None = None,
    accessible_delegations: Iterable[str] | None = None,
    allowed_memory_scope_keys: Iterable[str] | None = None,
) -> _ReflectionPromotionPlan | ReflectionPromotionResult:
    from sibyl_core.services.memory_source_validation import SourceReadAuthority

    snapshot = await load_promotion_source(organization_id, candidate_id)
    candidate_memory = snapshot.memory if snapshot is not None else None
    if candidate_memory is None:
        return _promotion_denied(
            candidate_id=candidate_id,
            reason="candidate_not_found",
            review_state="missing",
            memory_scope=None,
            scope_key=None,
            raw_source_ids=[],
        )

    if not _is_reflection_candidate(candidate_memory):
        return _promotion_denied(
            candidate_id=candidate_memory.id,
            reason="not_reflection_candidate",
            review_state=candidate_memory.review_state,
            memory_scope=candidate_memory.memory_scope,
            scope_key=candidate_memory.scope_key,
            raw_source_ids=[],
        )

    if candidate_memory.review_state == "archived":
        return _promotion_denied(
            candidate_id=candidate_memory.id,
            reason="candidate_archived",
            review_state=candidate_memory.review_state,
            memory_scope=candidate_memory.memory_scope,
            scope_key=candidate_memory.scope_key,
            raw_source_ids=_raw_source_ids(candidate_memory),
        )

    raw_source_ids = _raw_source_ids(candidate_memory) or [candidate_memory.id]
    input_memories, sources_complete, input_observations = await _load_promotion_inputs(
        candidate_memory, organization_id=organization_id
    )

    ownership_denial = _principal_denial(
        input_memories,
        candidate_id=candidate_memory.id,
        principal_id=principal_id,
        raw_source_ids=raw_source_ids,
    )
    if ownership_denial is not None:
        return ownership_denial
    source_scope_denial = _source_scope_denial(
        input_memories,
        candidate_id=candidate_memory.id,
        principal_id=principal_id,
        raw_source_ids=raw_source_ids,
        accessible_projects=accessible_projects,
        accessible_teams=accessible_teams,
        accessible_delegations=accessible_delegations,
    )
    if source_scope_denial is not None:
        return source_scope_denial

    if candidate_memory.metadata.get("source_validation_pending"):
        from sibyl_core.services.memory_source_validation import reconcile_raw_source_lifecycle

        candidate_memory = await reconcile_raw_source_lifecycle(
            candidate_memory,
            principal_id=str(principal_id),
            accessible_projects=accessible_projects,
            accessible_teams=accessible_teams,
            accessible_delegations=accessible_delegations,
        )
        snapshot = await load_promotion_source(organization_id, candidate_memory.id)
        if (
            snapshot is None
            or snapshot.memory.revision != candidate_memory.revision
            or snapshot.memory.raw_content != candidate_memory.raw_content
        ):
            return _promotion_denied(
                candidate_id=candidate_memory.id,
                reason="source_changed",
                review_state=candidate_memory.review_state,
                memory_scope=candidate_memory.memory_scope,
                scope_key=candidate_memory.scope_key,
                raw_source_ids=raw_source_ids,
            )
        candidate_memory = snapshot.memory
        input_memories[0] = candidate_memory

    from sibyl_core.services.eval_publication_guards import verify_publication_admissions

    if not await verify_publication_admissions(candidate_memory):
        return _promotion_denied(
            candidate_id=candidate_memory.id,
            reason="original_admission_changed",
            review_state=candidate_memory.review_state,
            memory_scope=candidate_memory.memory_scope,
            scope_key=candidate_memory.scope_key,
            raw_source_ids=raw_source_ids,
        )

    if not sources_complete or any(
        not _publication_source_recallable(memory, candidate_memory.id) for memory in input_memories
    ):
        return _promotion_denied(
            candidate_id=candidate_memory.id,
            reason="source_not_recallable",
            review_state=candidate_memory.review_state,
            memory_scope=candidate_memory.memory_scope,
            scope_key=candidate_memory.scope_key,
            raw_source_ids=raw_source_ids,
        )

    target_scope = _coerce_promotion_scope(promote_to_scope)
    if target_scope is None:
        reason = _missing_promotion_target_reason(candidate_memory, input_memories)
        return _promotion_denied(
            candidate_id=candidate_memory.id,
            reason=reason,
            review_state=candidate_memory.review_state,
            memory_scope=candidate_memory.memory_scope,
            scope_key=candidate_memory.scope_key,
            raw_source_ids=raw_source_ids,
            metadata={"input_scopes": _scope_metadata(input_memories)},
        )

    target_scope_key = _resolve_promotion_scope_key(
        target_scope=target_scope,
        promote_to_scope_key=promote_to_scope_key,
        project=project,
        candidate_memory=candidate_memory,
    )
    broadest_scope = _broadest_scope(input_memories)
    if _has_mixed_scope_inputs(input_memories) and target_scope is not broadest_scope:
        return _promotion_denied(
            candidate_id=candidate_memory.id,
            reason="promote_to_scope_must_match_broadest_input_scope",
            review_state=candidate_memory.review_state,
            memory_scope=target_scope,
            scope_key=target_scope_key,
            raw_source_ids=raw_source_ids,
            metadata={
                "broadest_input_scope": broadest_scope.value,
                "input_scopes": _scope_metadata(input_memories),
            },
        )

    promotion_candidate = _candidate_from_review_memory(
        candidate_memory,
        raw_source_ids=raw_source_ids,
        target_scope=target_scope,
        target_scope_key=target_scope_key,
        domain=domain,
    )
    target_project = project or (
        target_scope_key
        if target_scope is MemoryScope.PROJECT
        else _metadata_str(
            candidate_memory.metadata,
            "project_id",
        )
    )
    return _ReflectionPromotionPlan(
        source_authority=SourceReadAuthority(
            str(principal_id or ""),
            projects=frozenset(accessible_projects or ()),
            teams=frozenset(accessible_teams or ()),
            delegations=frozenset(accessible_delegations or ()),
            scope_keys=frozenset(allowed_memory_scope_keys)
            if allowed_memory_scope_keys is not None
            else None,
        ),
        candidate_memory=candidate_memory,
        promotion_candidate=promotion_candidate,
        target_scope=target_scope,
        target_scope_key=target_scope_key,
        target_project=target_project,
        raw_source_ids=raw_source_ids,
        input_memories=input_memories,
        source_observations=(
            (snapshot.observation,)
            if snapshot is not None
            and not candidate_memory.metadata.get(EVAL_CONSOLIDATION_METADATA_KEY)
            else ()
        )
        + input_observations,
    )


async def _resolve_raw_memory_promotion_plan(
    *,
    raw_memory_id: str,
    organization_id: str,
    principal_id: str | None,
    promote_to_scope: MemoryScope | str | None,
    promote_to_scope_key: str | None = None,
    domain: str | None = None,
    project: str | None = None,
    accessible_projects: Iterable[str] | None = None,
    accessible_teams: Iterable[str] | None = None,
    accessible_delegations: Iterable[str] | None = None,
    allowed_memory_scope_keys: Iterable[str] | None = None,
) -> _ReflectionPromotionPlan | ReflectionPromotionResult:
    from sibyl_core.services.memory_source_validation import SourceReadAuthority

    snapshot = await load_promotion_source(organization_id, raw_memory_id)
    memory = snapshot.memory if snapshot is not None else None
    if memory is None:
        return _promotion_denied(
            candidate_id=raw_memory_id,
            reason="candidate_not_found",
            review_state="missing",
            memory_scope=None,
            scope_key=None,
            raw_source_ids=[],
        )
    if _is_reflection_candidate(memory):
        return _promotion_denied(
            candidate_id=memory.id,
            reason="reflection_candidate_requires_reflection_promotion",
            review_state=memory.review_state,
            memory_scope=memory.memory_scope,
            scope_key=memory.scope_key,
            raw_source_ids=_raw_source_ids(memory),
        )

    raw_source_ids = [memory.id]
    if not raw_memory_recallable(memory):
        return _promotion_denied(
            candidate_id=memory.id,
            reason="raw_memory_not_recallable",
            review_state=memory.review_state,
            memory_scope=memory.memory_scope,
            scope_key=memory.scope_key,
            raw_source_ids=raw_source_ids,
        )

    input_memories = [memory]
    ownership_denial = _principal_denial(
        input_memories,
        candidate_id=memory.id,
        principal_id=principal_id,
        raw_source_ids=raw_source_ids,
    )
    if ownership_denial is not None:
        return ownership_denial
    source_scope_denial = _source_scope_denial(
        input_memories,
        candidate_id=memory.id,
        principal_id=principal_id,
        raw_source_ids=raw_source_ids,
        accessible_projects=accessible_projects,
        accessible_teams=accessible_teams,
        accessible_delegations=accessible_delegations,
    )
    if source_scope_denial is not None:
        return source_scope_denial

    target_scope = _coerce_promotion_scope(promote_to_scope)
    if target_scope is None:
        return _promotion_denied(
            candidate_id=memory.id,
            reason="missing_promote_to_scope",
            review_state=memory.review_state,
            memory_scope=memory.memory_scope,
            scope_key=memory.scope_key,
            raw_source_ids=raw_source_ids,
            metadata={"input_scopes": _scope_metadata(input_memories)},
        )

    target_scope_key = _resolve_promotion_scope_key(
        target_scope=target_scope,
        promote_to_scope_key=promote_to_scope_key,
        project=project,
        candidate_memory=memory,
    )
    promotion_candidate = _candidate_from_raw_memory(
        memory,
        target_scope=target_scope,
        target_scope_key=target_scope_key,
        domain=domain,
    )
    target_project = project or (
        target_scope_key
        if target_scope is MemoryScope.PROJECT
        else _metadata_str(memory.metadata, "project_id")
    )
    return _ReflectionPromotionPlan(
        source_authority=SourceReadAuthority(
            str(principal_id or ""),
            projects=frozenset(accessible_projects or ()),
            teams=frozenset(accessible_teams or ()),
            delegations=frozenset(accessible_delegations or ()),
            scope_keys=frozenset(allowed_memory_scope_keys)
            if allowed_memory_scope_keys is not None
            else None,
        ),
        candidate_memory=memory,
        promotion_candidate=promotion_candidate,
        target_scope=target_scope,
        target_scope_key=target_scope_key,
        target_project=target_project,
        raw_source_ids=raw_source_ids,
        input_memories=input_memories,
        source_observations=(snapshot.observation,) if snapshot is not None else (),
    )
