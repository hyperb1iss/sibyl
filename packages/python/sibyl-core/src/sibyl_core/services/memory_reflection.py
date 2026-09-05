"""Reflection persistence and promotion orchestration."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import structlog

from sibyl_core.errors import EntityNotFoundError, RevisionConflictError
from sibyl_core.memory_pipeline.lifecycle import graph_lifecycle_stamp, graph_metadata_recallable
from sibyl_core.models.entities import EntityType, Relationship
from sibyl_core.models.reflection import ReflectionCandidate
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
from sibyl_core.services.memory_identity import verify_reflection_identity
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
    accessible_teams: Iterable[str] | None = None,
    accessible_delegations: Iterable[str] | None = None,
    memory_scope: MemoryScope | str | None = None,
    scope_key: str | None = None,
) -> ReflectionWriteResult:
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
    accessible_teams: Iterable[str] | None = None,
    accessible_delegations: Iterable[str] | None = None,
    memory_scope: MemoryScope | str | None = None,
    scope_key: str | None = None,
    link_source_entity: bool = True,
    source_memories: Sequence[RawMemory] = (),
) -> ReflectionWriteResult:
    scope = _resolve_memory_scope(memory_scope, project)
    resolved_scope_key = _resolve_scope_key(scope, scope_key, project)
    policy_decisions = _authorize_reflection_write(
        principal_id=principal_id,
        memory_scope=scope,
        scope_key=resolved_scope_key,
        accessible_projects=accessible_projects,
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

    runtime = await get_surreal_graph_runtime(organization_id)
    source_ids = _candidate_source_ids(candidate, source_id)
    superseded_ids = await _authorized_superseded_entity_ids(
        runtime=runtime,
        principal_id=principal_id,
        accessible_projects=accessible_projects,
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
    entity.metadata["reflection_publication"] = {
        "state": "pending",
        "request": publication_request,
    }
    if not await _verify_promotion_sources(runtime, source_memories):
        return _retired_reflection_result(entity.id)
    stored, created = await runtime.entity_manager.create_direct_if_absent(entity)
    verify_reflection_identity(entity, stored)
    if not await _verify_promotion_sources(runtime, source_memories, entity_id=stored.id):
        return _retired_reflection_result(stored.id)
    if not graph_metadata_recallable(stored.metadata):
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
        accessible_projects=accessible_projects,
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
        if not graph_metadata_recallable(current.metadata):
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
    accessible_teams: Iterable[str] | None = None,
    accessible_delegations: Iterable[str] | None = None,
) -> ReflectionPromotionResult:
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
        return plan

    return await _apply_promotion_plan(
        plan=plan,
        organization_id=organization_id,
        principal_id=principal_id,
        domain=domain,
        related_to=related_to,
        accessible_projects=accessible_projects,
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
    accessible_teams: Iterable[str] | None = None,
    accessible_delegations: Iterable[str] | None = None,
) -> ReflectionPromotionResult:
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
        return plan

    return await _apply_promotion_plan(
        plan=plan,
        organization_id=organization_id,
        principal_id=principal_id,
        domain=domain,
        related_to=related_to,
        accessible_projects=accessible_projects,
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
    accessible_teams: Iterable[str] | None = None,
    accessible_delegations: Iterable[str] | None = None,
) -> ReflectionPromotionPreview:
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
        accessible_projects=accessible_projects,
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
    accessible_teams: Iterable[str] | None = None,
    accessible_delegations: Iterable[str] | None = None,
) -> ReflectionPromotionPreview:
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
        accessible_projects=accessible_projects,
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
    accessible_teams: Iterable[str] | None,
    accessible_delegations: Iterable[str] | None,
    native_source_id: str | None,
    lifecycle_source_id: str,
    lifecycle_reason: str,
) -> ReflectionPromotionResult:
    policy_decisions = _authorize_reflection_write(
        principal_id=principal_id,
        memory_scope=plan.target_scope,
        scope_key=plan.target_scope_key,
        accessible_projects=accessible_projects,
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
    )
    reservation = await _reserve_promotion(plan, prospective.id)
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
        accessible_teams=accessible_teams,
        accessible_delegations=accessible_delegations,
        memory_scope=plan.target_scope,
        scope_key=plan.target_scope_key,
        link_source_entity=False,
        source_memories=plan.input_memories,
    )
    if not result.response.success or result.metadata.get("promotion_state") == "partial":
        return _promotion_write_denied(plan=plan, result=result)
    runtime = await get_surreal_graph_runtime(organization_id)
    if not await _verify_promotion_sources(
        runtime, plan.input_memories, entity_id=result.response.id
    ):
        return _promotion_write_denied(plan=plan, result=_retired_reflection_result(prospective.id))
    return await _mark_promotion_plan_promoted(
        plan=plan,
        result=result,
        lifecycle_source_id=lifecycle_source_id,
        lifecycle_reason=lifecycle_reason,
    )


def _promotion_source_signature(memory: RawMemory) -> tuple[object, ...]:
    return (
        memory.organization_id,
        memory.principal_id,
        memory.memory_scope,
        memory.scope_key,
        memory.source_id,
        memory.title,
        memory.raw_content,
        memory.entity_type,
        memory.metadata.get("domain"),
        memory.metadata.get("category"),
        tuple(sorted(source_id for source_id in _raw_source_ids(memory) if source_id != memory.id)),
    )


async def _verify_promotion_sources(
    runtime: Any,
    memories: Sequence[RawMemory],
    *,
    entity_id: str | None = None,
) -> bool:
    """Close correction's read-before-insert gap without restoring retired rows."""
    for expected in memories:
        current = await get_raw_memory(
            organization_id=expected.organization_id, memory_id=expected.id
        )
        stamp: dict[str, object] = {}
        if current is None:
            stamp = {"excluded_from_recall": True, "lifecycle_state": "deleted"}
        elif not raw_memory_recallable(current):
            stamp = graph_lifecycle_stamp(current)
        elif _promotion_source_signature(current) != _promotion_source_signature(expected):
            stamp = {"excluded_from_recall": True, "lifecycle_state": "contested"}
        if stamp:
            if entity_id is not None:
                await runtime.entity_manager.update(entity_id, {"metadata": stamp})
            return False
    return True


async def _reserve_promotion(
    plan: _ReflectionPromotionPlan,
    entity_id: str,
) -> _ReflectionPromotionPlan | ReflectionPromotionResult:
    """Reserve correction's existing pointer before publishing any graph row."""
    memory = plan.candidate_memory
    recorded_id = _metadata_str(memory.metadata, "promoted_entity_id")
    if recorded_id or memory.review_state == _PROMOTED_REVIEW_STATE:
        if recorded_id != entity_id:
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
        )
    except RevisionConflictError:
        current = await get_raw_memory(organization_id=memory.organization_id, memory_id=memory.id)
        if current is None or not raw_memory_recallable(current):
            return _promotion_denied(
                candidate_id=memory.id,
                reason="source_not_recallable",
                review_state=current.review_state if current else "missing",
                memory_scope=plan.target_scope,
                scope_key=plan.target_scope_key,
                raw_source_ids=plan.raw_source_ids,
            )
        if _metadata_str(
            current.metadata, "promoted_entity_id"
        ) != entity_id or _promotion_source_signature(current) != _promotion_source_signature(
            memory
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
) -> _ReflectionPromotionPlan | ReflectionPromotionResult:
    candidate_memory = await get_raw_memory(
        organization_id=organization_id,
        memory_id=candidate_id,
    )
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

    raw_source_ids = _raw_source_ids(candidate_memory)
    source_memories = await _load_raw_sources(
        organization_id=organization_id,
        raw_source_ids=raw_source_ids,
    )
    raw_source_ids = raw_source_ids or [candidate_memory.id]
    input_memories = [candidate_memory, *source_memories]

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

    if any(not raw_memory_recallable(memory) for memory in input_memories):
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
        candidate_memory=candidate_memory,
        promotion_candidate=promotion_candidate,
        target_scope=target_scope,
        target_scope_key=target_scope_key,
        target_project=target_project,
        raw_source_ids=raw_source_ids,
        input_memories=input_memories,
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
) -> _ReflectionPromotionPlan | ReflectionPromotionResult:
    memory = await get_raw_memory(
        organization_id=organization_id,
        memory_id=raw_memory_id,
    )
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
        candidate_memory=memory,
        promotion_candidate=promotion_candidate,
        target_scope=target_scope,
        target_scope_key=target_scope_key,
        target_project=target_project,
        raw_source_ids=raw_source_ids,
        input_memories=input_memories,
    )
