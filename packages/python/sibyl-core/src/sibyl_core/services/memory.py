"""Native SurrealDB memory write services."""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, cast

import structlog

from sibyl_core.auth.memory_policy import (
    MemoryPolicyAction,
    MemoryPolicyDecision,
    authorize_memory_read,
    authorize_memory_reflect,
    authorize_memory_share,
    authorize_memory_write,
)
from sibyl_core.memory_pipeline.quality import normalize_memory_quality_metadata
from sibyl_core.models.entities import Entity, EntityType, Relationship, RelationshipType
from sibyl_core.models.reflection import (
    MemoryLifecycle,
    MemoryLifecycleFlag,
    MemoryLifecycleState,
    ReflectionCandidate,
    ReflectionFinding,
    ReflectionFindingKind,
    claim_records_from_metadata,
    correction_finding_kind,
    memory_lifecycle_from_metadata,
    reflection_findings_from_metadata,
    with_memory_lifecycle_metadata,
    with_reflection_finding_metadata,
)
from sibyl_core.services.graph import get_surreal_graph_runtime
from sibyl_core.services.memory_autonomy import reflection_autonomy_candidate_metadata
from sibyl_core.services.surreal_content import (
    MemoryScope,
    RawMemory,
    get_raw_memory,
    get_raw_memory_by_source_id,
    list_raw_memories_by_source_id,
    list_raw_memories_for_scope,
    raw_memory_recallable,
    save_raw_memory,
)
from sibyl_core.tools.helpers import _generate_id
from sibyl_core.tools.responses import AddResponse

log = structlog.get_logger()


class WriteMode(StrEnum):
    DISABLED = "disabled"
    ENABLED = "enabled"


@dataclass(frozen=True, slots=True)
class ReflectionWriteResult:
    response: AddResponse
    metadata: dict[str, Any]


@dataclass(frozen=True, slots=True)
class _RelationshipWriteReceipt:
    requested: int = 0
    created: int = 0
    failed: int = 0
    errors: tuple[str, ...] = ()

    @property
    def state(self) -> str:
        return "partial" if self.failed or self.errors else "complete"


@dataclass(frozen=True, slots=True)
class ReflectionPromotionResult:
    success: bool
    candidate_id: str
    promoted_id: str | None
    reason: str
    review_state: str
    memory_scope: MemoryScope | None
    scope_key: str | None
    raw_source_ids: list[str]
    policy_decisions: tuple[MemoryPolicyDecision, ...] = ()
    metadata: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class ReflectionPromotionPreview:
    allowed: bool
    candidate_id: str
    reason: str
    review_state: str
    memory_scope: MemoryScope | None
    scope_key: str | None
    raw_source_ids: list[str]
    policy_decisions: tuple[MemoryPolicyDecision, ...] = ()
    metadata: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class MemorySharePreview:
    allowed: bool
    reason: str
    target_scope: MemoryScope | None
    target_scope_key: str | None
    source_ids: list[str]
    visible_source_ids: list[str]
    denied_source_ids: list[str]
    missing_source_ids: list[str]
    redacted_count: int
    hidden_but_relevant_count: int
    policy_decisions: tuple[MemoryPolicyDecision, ...] = ()
    metadata: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class MemoryShareResult:
    applied: bool
    reason: str
    preview: MemorySharePreview
    promotions: tuple[ReflectionPromotionResult, ...] = ()
    metadata: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class MemoryAccessPreview:
    allowed: bool
    reason: str
    target_principal_type: str
    target_principal_id: str
    memory_space_ids: list[str]
    visible_source_ids: list[str]
    denied_source_ids: list[str]
    missing_source_ids: list[str]
    redacted_count: int
    hidden_but_relevant_count: int
    policy_decisions: tuple[MemoryPolicyDecision, ...] = ()
    metadata: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class MemoryCorrectionPreview:
    allowed: bool
    source_id: str
    action: str
    reason: str
    target_lifecycle_state: str
    target_lifecycle_flags: list[str]
    affected_source_ids: list[str]
    affected_derived_ids: list[str]
    reversible: bool
    recall_impact: dict[str, Any]
    synthesis_impact: dict[str, Any]
    audit_action: str
    policy_decisions: tuple[MemoryPolicyDecision, ...] = ()
    metadata: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class MemoryCorrectionResult:
    applied: bool
    preview: MemoryCorrectionPreview
    updated_memory: RawMemory | None = None


@dataclass(frozen=True, slots=True)
class _ReflectionPromotionPlan:
    candidate_memory: RawMemory
    promotion_candidate: ReflectionCandidate
    target_scope: MemoryScope
    target_scope_key: str | None
    target_project: str | None
    raw_source_ids: list[str]
    input_memories: list[RawMemory]


_PROMOTED_REVIEW_STATE = "promoted"
_ACCESS_PREVIEW_OVERFETCH_FACTOR = 4
_CORRECTION_TARGET_STATES: dict[str, MemoryLifecycleState] = {
    "delete": MemoryLifecycleState.DELETED,
    "mark_duplicate": MemoryLifecycleState.CONTESTED,
    "mark_stale": MemoryLifecycleState.CONTESTED,
    "mark_wrong": MemoryLifecycleState.CONTESTED,
    "restore": MemoryLifecycleState.ACTIVE,
    "supersede": MemoryLifecycleState.SUPERSEDED,
}
_CORRECTION_TARGET_FLAGS: dict[str, MemoryLifecycleFlag] = {
    "hide": MemoryLifecycleFlag.HIDDEN,
    "mark_sensitive": MemoryLifecycleFlag.SENSITIVE,
    "redact": MemoryLifecycleFlag.REDACTED,
}
_CORRECTION_ACTIONS = frozenset({*_CORRECTION_TARGET_STATES, *_CORRECTION_TARGET_FLAGS, "revise"})
_CORRECTION_ACTION_ALIASES = {
    "duplicate": "mark_duplicate",
    "stale": "mark_stale",
    "superseded": "supersede",
    "wrong": "mark_wrong",
}
_CORRECTION_RECALL_EXCLUDED_STATES = frozenset(
    state.value
    for state in (
        MemoryLifecycleState.ARCHIVED,
        MemoryLifecycleState.CONTESTED,
        MemoryLifecycleState.DELETED,
        MemoryLifecycleState.SUPERSEDED,
    )
)
_CORRECTION_RECALL_EXCLUDED_FLAGS = frozenset(flag.value for flag in MemoryLifecycleFlag)
_CORRECTION_IRREVERSIBLE_ACTIONS = frozenset({"delete", "redact"})
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
_SHARE_SOURCE_METADATA_ALLOWLIST = frozenset(
    {
        "category",
        "confidence",
        "domain",
        "remember_kind",
        "share_reason",
    }
)
_SCOPE_RANK: dict[MemoryScope, int] = {
    MemoryScope.PRIVATE: 0,
    MemoryScope.DELEGATED: 1,
    MemoryScope.PROJECT: 2,
    MemoryScope.TEAM: 3,
    MemoryScope.ORGANIZATION: 4,
    MemoryScope.SHARED: 5,
    MemoryScope.PUBLIC: 6,
}


def coerce_write_mode(value: str | WriteMode | None) -> WriteMode:
    if isinstance(value, WriteMode):
        return value
    if value is None or not value.strip():
        return WriteMode.ENABLED
    normalized = value.strip().lower()
    if normalized in {"enabled", "enable", "true", "1", "yes", "on"}:
        return WriteMode.ENABLED
    if normalized in {"disabled", "disable", "false", "0", "no", "off"}:
        return WriteMode.DISABLED
    return WriteMode.DISABLED


def write_mode_from_env(environ: Mapping[str, str] | None = None) -> WriteMode:
    source = os.environ if environ is None else environ
    return coerce_write_mode(source.get("SIBYL_NATIVE_WRITE"))


def reflection_write_enabled(environ: Mapping[str, str] | None = None) -> bool:
    return write_mode_from_env(environ) is WriteMode.ENABLED


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
    created_id = await runtime.entity_manager.create_direct(entity)
    native_write_path = _metadata_str(candidate.metadata, "native_write_path")
    if not native_write_path:
        native_write_path = "reflection_promotion"
    relationships = _relationships_for_promotion(
        created_id,
        project=project,
        source_id=source_id if link_source_entity else None,
        related_to=related_to,
        supersedes=superseded_ids,
        raw_source_ids=source_ids,
        native_write_path=native_write_path,
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

    return ReflectionWriteResult(
        response=AddResponse(
            success=True,
            id=created_id,
            message=f"Promoted natively: {candidate.title}",
            timestamp=datetime.now(UTC),
        ),
        metadata={
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
        },
    )


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
    )
    if not result.response.success:
        return _promotion_write_denied(plan=plan, result=result)
    return await _mark_promotion_plan_promoted(
        plan=plan,
        result=result,
        lifecycle_source_id=lifecycle_source_id,
        lifecycle_reason=lifecycle_reason,
    )


def _promotion_write_denied(
    *,
    plan: _ReflectionPromotionPlan,
    result: ReflectionWriteResult,
) -> ReflectionPromotionResult:
    return ReflectionPromotionResult(
        success=False,
        candidate_id=plan.candidate_memory.id,
        promoted_id=None,
        reason=_policy_denial_reason(result.metadata),
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
    metadata = _promoted_candidate_metadata(
        plan=plan,
        result=result,
        lifecycle_source_id=lifecycle_source_id,
        lifecycle_reason=lifecycle_reason,
    )
    await save_raw_memory(
        replace(
            plan.candidate_memory,
            review_state=_PROMOTED_REVIEW_STATE,
            metadata=metadata,
        )
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


async def preview_memory_share(
    *,
    source_ids: Sequence[str],
    organization_id: str,
    principal_id: str | None,
    target_scope: MemoryScope | str | None,
    target_scope_key: str | None = None,
    recipient_organization_id: str | None = None,
    accessible_projects: Iterable[str] | None = None,
    accessible_teams: Iterable[str] | None = None,
    accessible_delegations: Iterable[str] | None = None,
) -> MemorySharePreview:
    requested_source_ids = [str(source_id) for source_id in source_ids]
    normalized_target = _coerce_promotion_scope(target_scope)
    target_decision = _authorize_share_target(
        principal_id=principal_id,
        target_scope=normalized_target,
        target_scope_key=target_scope_key,
        recipient_organization_id=recipient_organization_id,
        organization_id=organization_id,
        accessible_projects=accessible_projects,
        accessible_teams=accessible_teams,
        accessible_delegations=accessible_delegations,
    )
    decisions: list[MemoryPolicyDecision] = [target_decision]
    if _redact_share_sources_for_denied_target(
        target_scope=normalized_target,
        target_decision=target_decision,
    ):
        metadata: dict[str, Any] = {
            "cross_organization": bool(
                recipient_organization_id and str(recipient_organization_id) != str(organization_id)
            ),
            "input_scopes": [],
            "missing_source_ids": [],
            "policy_reasons": [target_decision.reason],
            "recipient_organization_id": recipient_organization_id,
            "source_count": len(requested_source_ids),
            "source_denial_reasons": {
                source_id: target_decision.reason for source_id in requested_source_ids
            },
            "target_policy_reason": target_decision.reason,
            "visible_count": 0,
        }
        return MemorySharePreview(
            allowed=False,
            reason=target_decision.reason,
            target_scope=normalized_target,
            target_scope_key=target_scope_key,
            source_ids=requested_source_ids,
            visible_source_ids=[],
            denied_source_ids=requested_source_ids,
            missing_source_ids=[],
            redacted_count=len(requested_source_ids),
            hidden_but_relevant_count=len(requested_source_ids),
            policy_decisions=tuple(decisions),
            metadata=metadata,
        )

    visible_source_ids: list[str] = []
    denied_source_ids: list[str] = []
    missing_source_ids: list[str] = []
    source_denial_reasons: dict[str, str] = {}
    input_scopes: list[dict[str, str | None]] = []
    hidden_but_relevant_count = 0

    for source_id in requested_source_ids:
        memory = await get_raw_memory(
            organization_id=organization_id,
            memory_id=source_id,
        )
        if memory is None:
            denied_source_ids.append(source_id)
            missing_source_ids.append(source_id)
            source_denial_reasons[source_id] = "source_not_found"
            decisions.append(
                MemoryPolicyDecision(
                    action=MemoryPolicyAction.READ,
                    allowed=False,
                    reason="source_not_found",
                    memory_scope=MemoryScope.PRIVATE,
                    scope_key=None,
                )
            )
            continue

        read_decision = _authorize_share_source_read(
            memory=memory,
            principal_id=principal_id,
            accessible_projects=accessible_projects,
            accessible_teams=accessible_teams,
            accessible_delegations=accessible_delegations,
        )
        decisions.append(read_decision)
        if read_decision.allowed:
            visible_source_ids.append(memory.id)
            input_scopes.extend(_scope_metadata([memory]))
            continue

        denied_source_ids.append(memory.id)
        source_denial_reasons[memory.id] = read_decision.reason
        hidden_but_relevant_count += 1

    reason = target_decision.reason
    if target_decision.allowed:
        reason = "share_not_enabled"
    metadata: dict[str, Any] = {
        "cross_organization": bool(
            recipient_organization_id and str(recipient_organization_id) != str(organization_id)
        ),
        "input_scopes": input_scopes,
        "missing_source_ids": missing_source_ids,
        "policy_reasons": [decision.reason for decision in decisions],
        "recipient_organization_id": recipient_organization_id,
        "source_denial_reasons": source_denial_reasons,
        "source_count": len(requested_source_ids),
        "target_policy_reason": target_decision.reason,
        "visible_count": len(visible_source_ids),
    }
    return MemorySharePreview(
        allowed=False,
        reason=reason,
        target_scope=normalized_target,
        target_scope_key=target_scope_key,
        source_ids=requested_source_ids,
        visible_source_ids=visible_source_ids,
        denied_source_ids=denied_source_ids,
        missing_source_ids=missing_source_ids,
        redacted_count=hidden_but_relevant_count,
        hidden_but_relevant_count=hidden_but_relevant_count,
        policy_decisions=tuple(decisions),
        metadata=metadata,
    )


async def share_memory(
    *,
    source_ids: Sequence[str],
    organization_id: str,
    principal_id: str | None,
    target_scope: MemoryScope | str | None,
    target_scope_key: str | None = None,
    recipient_organization_id: str | None = None,
    domain: str | None = None,
    project: str | None = None,
    related_to: Sequence[str] | None = None,
    accessible_projects: Iterable[str] | None = None,
    accessible_teams: Iterable[str] | None = None,
    accessible_delegations: Iterable[str] | None = None,
) -> MemoryShareResult:
    preview = await preview_memory_share(
        source_ids=source_ids,
        organization_id=organization_id,
        principal_id=principal_id,
        target_scope=target_scope,
        target_scope_key=target_scope_key,
        recipient_organization_id=recipient_organization_id,
        accessible_projects=accessible_projects,
        accessible_teams=accessible_teams,
        accessible_delegations=accessible_delegations,
    )
    if preview.reason != "scope_crossing_requires_promotion":
        return MemoryShareResult(
            applied=False,
            reason=preview.reason,
            preview=preview,
            metadata={"promotion_count": 0, "target_allowed": False},
        )
    if not preview.visible_source_ids:
        return MemoryShareResult(
            applied=False,
            reason="no_visible_sources",
            preview=preview,
            metadata={"promotion_count": 0, "target_allowed": True},
        )

    promotions: list[ReflectionPromotionResult] = []
    for source_id in preview.visible_source_ids:
        plan = await _resolve_raw_memory_share_plan(
            raw_memory_id=source_id,
            organization_id=organization_id,
            principal_id=principal_id,
            promote_to_scope=preview.target_scope,
            promote_to_scope_key=preview.target_scope_key,
            domain=domain,
            project=project,
            accessible_projects=accessible_projects,
            accessible_teams=accessible_teams,
            accessible_delegations=accessible_delegations,
        )
        if isinstance(plan, ReflectionPromotionResult):
            promotions.append(plan)
            continue
        promotions.append(
            await _apply_share_plan(
                plan=plan,
                organization_id=organization_id,
                principal_id=principal_id,
                domain=domain,
                related_to=related_to,
                accessible_projects=accessible_projects,
                accessible_teams=accessible_teams,
                accessible_delegations=accessible_delegations,
            )
        )

    successful = [promotion for promotion in promotions if promotion.success]
    if len(successful) == len(promotions):
        reason = "shared"
    elif successful:
        reason = "share_partially_applied"
    else:
        reason = promotions[0].reason if promotions else "no_visible_sources"
    metadata = {
        "promotion_count": len(promotions),
        "promoted_count": len(successful),
        "target_allowed": True,
        "target_scope": preview.target_scope.value if preview.target_scope else None,
        "target_scope_key": preview.target_scope_key,
    }
    return MemoryShareResult(
        applied=bool(successful) and len(successful) == len(promotions),
        reason=reason,
        preview=preview,
        promotions=tuple(promotions),
        metadata=metadata,
    )


def _space_field(space: Mapping[str, object] | object, key: str) -> object | None:
    if isinstance(space, Mapping):
        mapping = cast(Mapping[str, object], space)
        return mapping.get(key)
    return getattr(space, key, None)


def _redact_share_sources_for_denied_target(
    *,
    target_scope: MemoryScope | None,
    target_decision: MemoryPolicyDecision,
) -> bool:
    return (
        target_scope is MemoryScope.TEAM
        and not target_decision.allowed
        and target_decision.reason != "scope_crossing_requires_promotion"
    )


def _preview_target_identity(
    *,
    target_principal_type: str,
    target_principal_id: str,
    actor_user_id: str | None,
    memory_scope: MemoryScope,
) -> tuple[str | None, str | None]:
    principal_type = target_principal_type.strip().lower()
    if principal_type == "agent":
        return actor_user_id or target_principal_id, (
            target_principal_id if memory_scope is MemoryScope.PRIVATE else None
        )
    if principal_type == "delegated":
        return actor_user_id or target_principal_id, None
    return target_principal_id, None


def _preview_private_scope_allowed(
    *,
    target_principal_type: str,
    target_principal_id: str,
    actor_user_id: str | None,
    scope_key: str | None,
) -> bool:
    if not scope_key:
        return True
    principal_type = target_principal_type.strip().lower()
    if principal_type == "agent":
        return actor_user_id is not None and scope_key == actor_user_id
    if principal_type == "user":
        return scope_key == target_principal_id
    return False


async def preview_memory_access(
    *,
    organization_id: str,
    actor_user_id: str | None,
    target_principal_type: str,
    target_principal_id: str,
    memory_spaces: Sequence[Mapping[str, object] | object],
    limit: int = 50,
) -> MemoryAccessPreview:
    normalized_target_type = target_principal_type.strip().lower() or "user"
    visible_source_ids: list[str] = []
    denied_source_ids: list[str] = []
    missing_source_ids: list[str] = []
    denied_space_ids: list[str] = []
    lifecycle_hidden_source_ids: list[str] = []
    input_scopes: list[dict[str, str | None]] = []
    decisions: list[MemoryPolicyDecision] = []
    hidden_but_relevant_count = 0

    for space in memory_spaces:
        space_id = str(_space_field(space, "id") or "")
        scope = _coerce_promotion_scope(str(_space_field(space, "memory_scope") or "private"))
        scope_key = _metadata_str({"scope_key": _space_field(space, "scope_key")}, "scope_key")
        state = str(_space_field(space, "state") or "active")
        disabled_reason = _metadata_str(
            {"disabled_reason": _space_field(space, "disabled_reason")},
            "disabled_reason",
        )
        if scope is None:
            denied_space_ids.append(space_id)
            hidden_but_relevant_count += 1
            decisions.append(
                MemoryPolicyDecision(
                    action=MemoryPolicyAction.READ,
                    allowed=False,
                    reason="scope_not_enabled",
                    memory_scope=MemoryScope.PRIVATE,
                    scope_key=scope_key,
                )
            )
            continue
        if state == "disabled":
            reason = disabled_reason or "scope_not_enabled"
            denied_space_ids.append(space_id)
            hidden_but_relevant_count += 1
            decisions.append(
                MemoryPolicyDecision(
                    action=MemoryPolicyAction.READ,
                    allowed=False,
                    reason=reason,
                    memory_scope=scope,
                    scope_key=scope_key,
                )
            )
            continue
        principal_id, agent_id = _preview_target_identity(
            target_principal_type=normalized_target_type,
            target_principal_id=target_principal_id,
            actor_user_id=actor_user_id,
            memory_scope=scope,
        )
        accessible_projects = {scope_key} if scope is MemoryScope.PROJECT and scope_key else None
        accessible_delegations = (
            {scope_key} if scope is MemoryScope.DELEGATED and scope_key else None
        )
        read_decision = authorize_memory_read(
            principal_id=principal_id,
            memory_scope=scope,
            scope_key=scope_key,
            agent_id=agent_id,
            accessible_projects=accessible_projects,
            accessible_delegations=accessible_delegations,
        )
        if scope is MemoryScope.PRIVATE and not _preview_private_scope_allowed(
            target_principal_type=normalized_target_type,
            target_principal_id=target_principal_id,
            actor_user_id=actor_user_id,
            scope_key=scope_key,
        ):
            read_decision = replace(
                read_decision,
                allowed=False,
                reason="unverified_membership",
            )
        decisions.append(read_decision)
        if not read_decision.allowed:
            denied_space_ids.append(space_id)
            hidden_but_relevant_count += 1
            continue
        if len(visible_source_ids) >= limit:
            continue

        remaining = limit - len(visible_source_ids)
        memories = await list_raw_memories_for_scope(
            organization_id=organization_id,
            principal_id=principal_id or target_principal_id,
            memory_scope=scope,
            scope_key=scope_key,
            agent_id=agent_id,
            limit=remaining * _ACCESS_PREVIEW_OVERFETCH_FACTOR,
            include_lifecycle_hidden=True,
        )
        visible_memories: list[RawMemory] = []
        for memory in memories:
            if not raw_memory_recallable(memory):
                if len(lifecycle_hidden_source_ids) < limit:
                    denied_source_ids.append(memory.id)
                    lifecycle_hidden_source_ids.append(memory.id)
                hidden_but_relevant_count += 1
                continue
            if len(visible_source_ids) >= limit:
                continue
            visible_source_ids.append(memory.id)
            visible_memories.append(memory)
        input_scopes.extend(_scope_metadata(visible_memories))

    policy_reasons = [decision.reason for decision in decisions]
    denied_reasons = [decision.reason for decision in decisions if not decision.allowed]
    allowed = not denied_reasons and not lifecycle_hidden_source_ids
    access_state = "allowed" if allowed else "partial" if visible_source_ids else "denied"
    metadata: dict[str, Any] = {
        "access_state": access_state,
        "denied_memory_space_ids": [space_id for space_id in denied_space_ids if space_id],
        "input_scopes": input_scopes,
        "lifecycle_hidden_source_ids": lifecycle_hidden_source_ids,
        "policy_reasons": policy_reasons,
        "target_principal_type": normalized_target_type,
        "visible_count": len(visible_source_ids),
    }
    return MemoryAccessPreview(
        allowed=allowed,
        reason=(
            "access_preview_allowed"
            if allowed
            else denied_reasons[0]
            if denied_reasons
            else "lifecycle_hidden"
        ),
        target_principal_type=normalized_target_type,
        target_principal_id=target_principal_id,
        memory_space_ids=[
            str(_space_field(space, "id")) for space in memory_spaces if _space_field(space, "id")
        ],
        visible_source_ids=visible_source_ids,
        denied_source_ids=denied_source_ids,
        missing_source_ids=missing_source_ids,
        redacted_count=hidden_but_relevant_count,
        hidden_but_relevant_count=hidden_but_relevant_count,
        policy_decisions=tuple(decisions),
        metadata=metadata,
    )


async def _load_correction_memory(
    *,
    organization_id: str,
    source_id: str,
) -> RawMemory | None:
    memory = await get_raw_memory(organization_id=organization_id, memory_id=source_id)
    if memory is not None:
        return memory
    return await get_raw_memory_by_source_id(organization_id=organization_id, source_id=source_id)


def _correction_audit_action(action: str) -> str:
    return f"memory.correction.{action}"


def _correction_derived_ids(memory: RawMemory) -> list[str]:
    return list(
        dict.fromkeys(
            (
                *_metadata_str_values(
                    memory.metadata,
                    "derived_ids",
                    "promoted_entity_id",
                    "promoted_ids",
                    "relationship_ids",
                ),
            )
        )
    )


def _correction_preview_denied(
    *,
    source_id: str,
    action: str,
    reason: str,
    target_lifecycle_state: str = "",
    target_lifecycle_flags: Sequence[str] = (),
    policy_decisions: Sequence[MemoryPolicyDecision] = (),
    metadata: dict[str, Any] | None = None,
) -> MemoryCorrectionPreview:
    return MemoryCorrectionPreview(
        allowed=False,
        source_id=source_id,
        action=action,
        reason=reason,
        target_lifecycle_state=target_lifecycle_state,
        target_lifecycle_flags=list(target_lifecycle_flags),
        affected_source_ids=[],
        affected_derived_ids=[],
        reversible=False,
        recall_impact={"excluded_from_recall": False, "reason": reason},
        synthesis_impact={"excluded_from_synthesis": False, "reason": reason},
        audit_action=_correction_audit_action(action or "unknown"),
        policy_decisions=tuple(policy_decisions),
        metadata=metadata or {"policy_allowed": False, "policy_reasons": [reason]},
    )


def _correction_requirement_reason(
    *,
    action: str,
    replacement_source_id: str | None,
    duplicate_of_source_id: str | None,
    revised_content: str | None,
    current_content: str,
) -> str | None:
    if action == "supersede" and not replacement_source_id:
        return "missing_replacement_source"
    if action == "mark_duplicate" and not duplicate_of_source_id:
        return "missing_duplicate_source"
    if action == "revise":
        if revised_content is None or not revised_content.strip():
            return "missing_revised_content"
        if revised_content == current_content:
            return "unchanged_revised_content"
    return None


async def _validate_correction_reference(
    *,
    organization_id: str,
    memory: RawMemory,
    reference_source_id: str,
    reference_kind: str,
) -> tuple[RawMemory | None, str | None]:
    reference = await _load_correction_memory(
        organization_id=organization_id,
        source_id=reference_source_id,
    )
    if reference is None:
        return None, f"{reference_kind}_source_not_found"
    if reference.id == memory.id:
        return None, f"{reference_kind}_source_self_reference"
    if not raw_memory_recallable(reference):
        return None, f"{reference_kind}_source_not_recallable"
    return reference, None


def _correction_impact(
    target_lifecycle_state: str,
    target_lifecycle_flags: Sequence[str],
) -> tuple[dict[str, Any], dict[str, Any]]:
    excluded = target_lifecycle_state in _CORRECTION_RECALL_EXCLUDED_STATES or bool(
        _CORRECTION_RECALL_EXCLUDED_FLAGS.intersection(target_lifecycle_flags)
    )
    recall = {
        "excluded_from_recall": excluded,
        "target_lifecycle_state": target_lifecycle_state,
        "target_lifecycle_flags": list(target_lifecycle_flags),
    }
    synthesis = {
        "excluded_from_synthesis": excluded,
        "preserve_source_truth": True,
        "target_lifecycle_state": target_lifecycle_state,
        "target_lifecycle_flags": list(target_lifecycle_flags),
    }
    return recall, synthesis


async def preview_memory_correction(
    *,
    organization_id: str,
    source_id: str,
    principal_id: str | None,
    action: str,
    reason: str | None = None,
    accessible_projects: Iterable[str] | None = None,
    accessible_teams: Iterable[str] | None = None,
    accessible_delegations: Iterable[str] | None = None,
    replacement_source_id: str | None = None,
    duplicate_of_source_id: str | None = None,
    revised_content: str | None = None,
) -> MemoryCorrectionPreview:
    requested_action = action.strip().lower()
    normalized_action = _CORRECTION_ACTION_ALIASES.get(requested_action, requested_action)
    if normalized_action not in _CORRECTION_ACTIONS:
        return _correction_preview_denied(
            source_id=source_id,
            action=normalized_action,
            reason="invalid_correction_action",
        )

    memory = await _load_correction_memory(
        organization_id=organization_id,
        source_id=source_id,
    )
    if memory is None:
        return _correction_preview_denied(
            source_id=source_id,
            action=normalized_action,
            reason="memory_source_not_found",
        )
    lifecycle = memory_lifecycle_from_metadata(
        memory.metadata,
        source_id=memory.id,
        review_state=memory.review_state,
    )
    target_state = _CORRECTION_TARGET_STATES.get(normalized_action)
    if target_state is None:
        target_state = MemoryLifecycleState(str(lifecycle.state))
    target_lifecycle_state = target_state.value
    target_lifecycle_flags = [] if normalized_action == "restore" else list(lifecycle.flags)
    if target_flag := _CORRECTION_TARGET_FLAGS.get(normalized_action):
        target_lifecycle_flags = list(dict.fromkeys([*target_lifecycle_flags, target_flag.value]))

    write_decision = _authorize_correction_source_write(
        memory=memory,
        principal_id=principal_id,
        accessible_projects=accessible_projects,
        accessible_teams=accessible_teams,
        accessible_delegations=accessible_delegations,
    )
    if not write_decision.allowed:
        return _correction_preview_denied(
            source_id=memory.id,
            action=normalized_action,
            reason=write_decision.reason,
            target_lifecycle_state=target_lifecycle_state,
            target_lifecycle_flags=target_lifecycle_flags,
            policy_decisions=(write_decision,),
            metadata={
                "policy_allowed": False,
                "policy_reasons": [write_decision.reason],
                "requested_source_id": source_id,
            },
        )

    requirement_reason = _correction_requirement_reason(
        action=normalized_action,
        replacement_source_id=replacement_source_id,
        duplicate_of_source_id=duplicate_of_source_id,
        revised_content=revised_content,
        current_content=memory.raw_content,
    )
    if requirement_reason:
        return _correction_preview_denied(
            source_id=memory.id,
            action=normalized_action,
            reason=requirement_reason,
            target_lifecycle_state=target_lifecycle_state,
            target_lifecycle_flags=target_lifecycle_flags,
            policy_decisions=(write_decision,),
            metadata={
                "policy_allowed": False,
                "policy_reasons": [requirement_reason],
                "requested_source_id": source_id,
            },
        )

    canonical_replacement_source_id = replacement_source_id
    canonical_duplicate_of_source_id = duplicate_of_source_id
    if replacement_source_id:
        reference, reference_reason = await _validate_correction_reference(
            organization_id=organization_id,
            memory=memory,
            reference_source_id=replacement_source_id,
            reference_kind="replacement",
        )
        if reference_reason:
            return _correction_preview_denied(
                source_id=memory.id,
                action=normalized_action,
                reason=reference_reason,
                target_lifecycle_state=target_lifecycle_state,
                target_lifecycle_flags=target_lifecycle_flags,
                policy_decisions=(write_decision,),
                metadata={
                    "policy_allowed": False,
                    "policy_reasons": [reference_reason],
                    "requested_source_id": source_id,
                    "replacement_source_id": replacement_source_id,
                },
            )
        canonical_replacement_source_id = reference.id if reference else replacement_source_id
    if duplicate_of_source_id:
        reference, reference_reason = await _validate_correction_reference(
            organization_id=organization_id,
            memory=memory,
            reference_source_id=duplicate_of_source_id,
            reference_kind="duplicate",
        )
        if reference_reason:
            return _correction_preview_denied(
                source_id=memory.id,
                action=normalized_action,
                reason=reference_reason,
                target_lifecycle_state=target_lifecycle_state,
                target_lifecycle_flags=target_lifecycle_flags,
                policy_decisions=(write_decision,),
                metadata={
                    "duplicate_of_source_id": duplicate_of_source_id,
                    "policy_allowed": False,
                    "policy_reasons": [reference_reason],
                    "requested_source_id": source_id,
                },
            )
        canonical_duplicate_of_source_id = reference.id if reference else duplicate_of_source_id

    recall_impact, synthesis_impact = _correction_impact(
        target_lifecycle_state,
        target_lifecycle_flags,
    )
    affected_derived_ids = _correction_derived_ids(memory)
    metadata = {
        "duplicate_of_source_id": canonical_duplicate_of_source_id,
        "policy_allowed": True,
        "policy_reasons": [write_decision.reason],
        "replacement_source_id": canonical_replacement_source_id,
        "requested_source_id": source_id,
        "revises_content": normalized_action == "revise",
    }
    return MemoryCorrectionPreview(
        allowed=True,
        source_id=memory.id,
        action=normalized_action,
        reason=reason or f"{normalized_action}_preview_allowed",
        target_lifecycle_state=target_lifecycle_state,
        target_lifecycle_flags=target_lifecycle_flags,
        affected_source_ids=[memory.id],
        affected_derived_ids=affected_derived_ids,
        reversible=normalized_action not in _CORRECTION_IRREVERSIBLE_ACTIONS,
        recall_impact=recall_impact,
        synthesis_impact=synthesis_impact,
        audit_action=_correction_audit_action(normalized_action),
        policy_decisions=(write_decision,),
        metadata=metadata,
    )


def _correction_metadata(
    *,
    memory: RawMemory,
    preview: MemoryCorrectionPreview,
    reason: str | None,
    replacement_source_id: str | None,
    duplicate_of_source_id: str | None,
    revised_content: str | None,
    principal_id: str | None,
) -> dict[str, object]:
    metadata = dict(memory.metadata)
    history = list(_metadata_dict_values(metadata, "correction_history"))
    now = datetime.now(UTC).isoformat()
    prior_lifecycle = memory_lifecycle_from_metadata(
        metadata,
        source_id=memory.id,
        review_state=memory.review_state,
    )
    prior_state = str(prior_lifecycle.state)
    if preview.action == "revise" and revised_content is not None:
        revisions = list(_metadata_dict_values(metadata, "content_revisions"))
        revisions.append(
            {
                "revision": memory.revision,
                "content": memory.raw_content,
                "reason": reason or preview.reason,
                "created_at": now,
                "created_by_user_id": principal_id,
            }
        )
        metadata["content_revisions"] = revisions
    if preview.action == "restore":
        for key in (
            "deleted_at",
            "duplicate_at",
            "duplicate_of_source_id",
            "hidden_at",
            "lifecycle_action",
            "lifecycle_reason",
            "lifecycle_state",
            "lifecycle_flags",
            "prior_review_state",
            "redacted_at",
            "sensitive_at",
            "stale_at",
            "superseded_at",
            "superseded_by_source_id",
            "wrong_at",
        ):
            metadata.pop(key, None)
        metadata["restored_at"] = now
        lifecycle = MemoryLifecycle(
            state=preview.target_lifecycle_state,
            source_id=memory.id,
            action=preview.action,
            reason=reason or preview.reason,
            prior_state=prior_state,
            reversible=True,
        )
    else:
        metadata["lifecycle_action"] = preview.action
        metadata["lifecycle_state"] = preview.target_lifecycle_state
        metadata["lifecycle_flags"] = preview.target_lifecycle_flags
        metadata["lifecycle_reason"] = reason or preview.reason
        metadata[f"{preview.action.removeprefix('mark_')}_at"] = now
        if replacement_source_id:
            metadata["superseded_by_source_id"] = replacement_source_id
        if duplicate_of_source_id:
            metadata["duplicate_of_source_id"] = duplicate_of_source_id
        lifecycle = MemoryLifecycle(
            state=preview.target_lifecycle_state,
            source_id=memory.id,
            action=preview.action,
            reason=reason or preview.reason,
            prior_state=prior_state,
            replacement_source_id=replacement_source_id,
            duplicate_of_source_id=duplicate_of_source_id,
            derived_ids=preview.affected_derived_ids,
            flags=preview.target_lifecycle_flags,
            reversible=preview.reversible,
        )
    history.append(
        {
            "action": preview.action,
            "audit_action": preview.audit_action,
            "reason": reason or preview.reason,
            "target_lifecycle_state": preview.target_lifecycle_state,
            "target_lifecycle_flags": preview.target_lifecycle_flags,
            "created_at": now,
            "replacement_source_id": replacement_source_id,
            "duplicate_of_source_id": duplicate_of_source_id,
            "prior_revision": memory.revision,
            "created_by_user_id": principal_id,
        }
    )
    metadata["correction_history"] = history
    metadata = with_memory_lifecycle_metadata(metadata, lifecycle)
    return with_reflection_finding_metadata(
        metadata,
        ReflectionFinding(
            kind=correction_finding_kind(preview.action),
            target_source_id=memory.id,
            reason=reason or preview.reason,
            action=preview.action,
            lifecycle_state=lifecycle.state,
            source_ids=[memory.id],
            related_source_ids=[
                item for item in (replacement_source_id, duplicate_of_source_id) if item is not None
            ],
            policy_reasons=_metadata_str_values(preview.metadata or {}, "policy_reasons"),
            reversible=preview.reversible,
            metadata={
                "audit_action": preview.audit_action,
                "target_lifecycle_state": preview.target_lifecycle_state,
                "target_lifecycle_flags": preview.target_lifecycle_flags,
            },
        ),
    )


async def apply_memory_correction(
    *,
    organization_id: str,
    source_id: str,
    principal_id: str | None,
    action: str,
    reason: str | None = None,
    accessible_projects: Iterable[str] | None = None,
    accessible_teams: Iterable[str] | None = None,
    accessible_delegations: Iterable[str] | None = None,
    replacement_source_id: str | None = None,
    duplicate_of_source_id: str | None = None,
    revised_content: str | None = None,
    expected_revision: int | None = None,
) -> MemoryCorrectionResult:
    preview = await preview_memory_correction(
        organization_id=organization_id,
        source_id=source_id,
        principal_id=principal_id,
        action=action,
        reason=reason,
        accessible_projects=accessible_projects,
        accessible_teams=accessible_teams,
        accessible_delegations=accessible_delegations,
        replacement_source_id=replacement_source_id,
        duplicate_of_source_id=duplicate_of_source_id,
        revised_content=revised_content,
    )
    if not preview.allowed:
        return MemoryCorrectionResult(applied=False, preview=preview)

    memory = await _load_correction_memory(organization_id=organization_id, source_id=source_id)
    if memory is None:
        denied = _correction_preview_denied(
            source_id=source_id,
            action=preview.action,
            reason="memory_source_not_found",
            target_lifecycle_state=preview.target_lifecycle_state,
            target_lifecycle_flags=preview.target_lifecycle_flags,
        )
        return MemoryCorrectionResult(applied=False, preview=denied)
    preview_metadata = preview.metadata or {}
    canonical_replacement_source_id = (
        _metadata_str(preview_metadata, "replacement_source_id") or replacement_source_id
    )
    canonical_duplicate_of_source_id = (
        _metadata_str(preview_metadata, "duplicate_of_source_id") or duplicate_of_source_id
    )
    review_state = memory.review_state
    if preview.action == "restore" and review_state in {
        "deleted",
        "duplicate",
        "hidden",
        "redacted",
        "sensitive",
        "stale",
        "superseded",
        "wrong",
    }:
        review_state = _metadata_str(memory.metadata, "prior_review_state") or "pending"
    updated = replace(
        memory,
        raw_content=revised_content if preview.action == "revise" else memory.raw_content,
        review_state=review_state,
        metadata=_correction_metadata(
            memory=memory,
            preview=preview,
            reason=reason,
            replacement_source_id=canonical_replacement_source_id,
            duplicate_of_source_id=canonical_duplicate_of_source_id,
            revised_content=revised_content,
            principal_id=principal_id,
        ),
    )
    if expected_revision is None:
        saved = await save_raw_memory(updated)
    else:
        saved = await save_raw_memory(updated, expected_revision=expected_revision)
    return MemoryCorrectionResult(applied=True, preview=preview, updated_memory=saved)


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

    if candidate_memory.review_state == _PROMOTED_REVIEW_STATE:
        return _promotion_denied(
            candidate_id=candidate_memory.id,
            reason="candidate_already_promoted",
            review_state=candidate_memory.review_state,
            memory_scope=candidate_memory.memory_scope,
            scope_key=candidate_memory.scope_key,
            raw_source_ids=_raw_source_ids(candidate_memory),
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
    if memory.review_state == _PROMOTED_REVIEW_STATE or memory.metadata.get("promoted_entity_id"):
        return _promotion_denied(
            candidate_id=memory.id,
            reason="candidate_already_promoted",
            review_state=memory.review_state,
            memory_scope=memory.memory_scope,
            scope_key=memory.scope_key,
            raw_source_ids=raw_source_ids,
        )
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


async def _resolve_raw_memory_share_plan(
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
    promotion_candidate = _candidate_from_share_memory(
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


async def _apply_share_plan(
    *,
    plan: _ReflectionPromotionPlan,
    organization_id: str,
    principal_id: str | None,
    domain: str | None,
    related_to: Sequence[str] | None,
    accessible_projects: Iterable[str] | None,
    accessible_teams: Iterable[str] | None,
    accessible_delegations: Iterable[str] | None,
) -> ReflectionPromotionResult:
    result = await persist_reflection_candidate(
        candidate=plan.promotion_candidate,
        organization_id=organization_id,
        principal_id=principal_id,
        domain=domain or _metadata_str(plan.candidate_memory.metadata, "domain"),
        project=plan.target_project,
        source_id=plan.candidate_memory.id,
        related_to=related_to,
        accessible_projects=accessible_projects,
        accessible_teams=accessible_teams,
        accessible_delegations=accessible_delegations,
        memory_scope=plan.target_scope,
        scope_key=plan.target_scope_key,
        link_source_entity=False,
    )
    if not result.response.success:
        return _promotion_write_denied(plan=plan, result=result)

    metadata = {
        **_share_source_metadata(plan.candidate_memory),
        **result.metadata,
        "share_applied_at": datetime.now(UTC).isoformat(),
        "share_source_id": plan.candidate_memory.id,
        "share_source_scope": plan.candidate_memory.memory_scope.value,
        "share_source_scope_key": plan.candidate_memory.scope_key,
        "share_target_scope": plan.target_scope.value,
        "share_target_scope_key": plan.target_scope_key,
        "shared_entity_id": result.response.id,
        "raw_source_ids": plan.raw_source_ids,
        "source_ids": plan.raw_source_ids,
    }
    return ReflectionPromotionResult(
        success=True,
        candidate_id=plan.candidate_memory.id,
        promoted_id=result.response.id,
        reason="shared",
        review_state=plan.candidate_memory.review_state,
        memory_scope=plan.target_scope,
        scope_key=plan.target_scope_key,
        raw_source_ids=plan.raw_source_ids,
        metadata=metadata,
    )


def _resolve_memory_scope(
    memory_scope: MemoryScope | str | None,
    project: str | None,
) -> MemoryScope:
    if memory_scope is not None:
        try:
            return MemoryScope(memory_scope)
        except ValueError:
            return MemoryScope.PRIVATE
    return MemoryScope.PROJECT if project else MemoryScope.PRIVATE


def _resolve_scope_key(
    memory_scope: MemoryScope,
    scope_key: str | None,
    project: str | None,
) -> str | None:
    if memory_scope is MemoryScope.PROJECT:
        return scope_key or project
    return scope_key


def _authorize_reflection_write(
    *,
    principal_id: str | None,
    memory_scope: MemoryScope,
    scope_key: str | None,
    accessible_projects: Iterable[str] | None,
    accessible_teams: Iterable[str] | None = None,
    accessible_delegations: Iterable[str] | None = None,
) -> tuple[MemoryPolicyDecision, MemoryPolicyDecision]:
    reflect_decision = authorize_memory_reflect(
        principal_id=principal_id,
        memory_scope=memory_scope,
        scope_key=scope_key,
        accessible_projects=accessible_projects,
        accessible_teams=accessible_teams,
        accessible_delegations=accessible_delegations,
    )
    write_decision = authorize_memory_write(
        principal_id=principal_id,
        memory_scope=memory_scope,
        scope_key=scope_key,
        accessible_projects=accessible_projects,
        accessible_teams=accessible_teams,
        accessible_delegations=accessible_delegations,
    )
    return reflect_decision, write_decision


def _policy_metadata(decisions: Sequence[MemoryPolicyDecision]) -> dict[str, Any]:
    return {
        "native_write_mode": WriteMode.ENABLED.value,
        "memory_scope": decisions[0].memory_scope.value,
        "scope_key": decisions[0].scope_key,
        "policy_allowed": all(decision.allowed for decision in decisions),
        "policy_reasons": [decision.reason for decision in decisions],
        "policy_actions": [decision.action.value for decision in decisions],
    }


def _promotion_lifecycle_metadata(
    *,
    metadata: Mapping[str, Any],
    promoted_entity_id: str,
    source_ids: Sequence[str],
    source_id: str | None,
    reason: str,
    policy_metadata: Mapping[str, Any] | None,
) -> dict[str, object]:
    target_source_id = source_id or (source_ids[0] if source_ids else promoted_entity_id)
    next_metadata: dict[str, object] = dict(metadata)
    next_metadata = with_memory_lifecycle_metadata(
        next_metadata,
        MemoryLifecycle(
            state=MemoryLifecycleState.ACTIVE,
            source_id=target_source_id,
            action="promote",
            reason=reason or "reflection_promotion",
            prior_state=_metadata_str(next_metadata, "review_state"),
            derived_ids=[promoted_entity_id],
            reversible=True,
            metadata={"promoted_entity_id": promoted_entity_id},
        ),
    )
    return with_reflection_finding_metadata(
        next_metadata,
        ReflectionFinding(
            kind=ReflectionFindingKind.PROMOTION,
            target_source_id=target_source_id,
            reason=reason or "reflection_promotion",
            action="promote",
            lifecycle_state=MemoryLifecycleState.ACTIVE,
            source_ids=list(source_ids),
            related_source_ids=[promoted_entity_id],
            policy_reasons=_metadata_str_values(policy_metadata or {}, "policy_reasons"),
            metadata={"promoted_entity_id": promoted_entity_id},
        ),
    )


def _policy_denied_message(decisions: Sequence[MemoryPolicyDecision]) -> str:
    denied = [decision.reason for decision in decisions if not decision.allowed]
    reason = denied[0] if denied else "unknown"
    return f"Native reflection promotion denied: {reason}"


def _promotion_denied(
    *,
    candidate_id: str,
    reason: str,
    review_state: str,
    memory_scope: MemoryScope | None,
    scope_key: str | None,
    raw_source_ids: list[str],
    metadata: dict[str, Any] | None = None,
    policy_decisions: Sequence[MemoryPolicyDecision] = (),
) -> ReflectionPromotionResult:
    payload = {"policy_reasons": [reason], "policy_allowed": False}
    if metadata:
        payload.update(metadata)
    return ReflectionPromotionResult(
        success=False,
        candidate_id=candidate_id,
        promoted_id=None,
        reason=reason,
        review_state=review_state,
        memory_scope=memory_scope,
        scope_key=scope_key,
        raw_source_ids=raw_source_ids,
        metadata=payload,
        policy_decisions=tuple(policy_decisions),
    )


def _promotion_preview_from_denial(
    result: ReflectionPromotionResult,
) -> ReflectionPromotionPreview:
    return ReflectionPromotionPreview(
        allowed=False,
        candidate_id=result.candidate_id,
        reason=result.reason,
        review_state=result.review_state,
        memory_scope=result.memory_scope,
        scope_key=result.scope_key,
        raw_source_ids=result.raw_source_ids,
        policy_decisions=result.policy_decisions,
        metadata=result.metadata,
    )


def _authorize_share_target(
    *,
    principal_id: str | None,
    target_scope: MemoryScope | None,
    target_scope_key: str | None,
    recipient_organization_id: str | None,
    organization_id: str,
    accessible_projects: Iterable[str] | None,
    accessible_teams: Iterable[str] | None,
    accessible_delegations: Iterable[str] | None,
) -> MemoryPolicyDecision:
    if target_scope is None:
        return MemoryPolicyDecision(
            action=MemoryPolicyAction.SHARE,
            allowed=False,
            reason="missing_memory_scope",
            memory_scope=MemoryScope.PRIVATE,
            scope_key=target_scope_key,
        )
    if recipient_organization_id and str(recipient_organization_id) != str(organization_id):
        return MemoryPolicyDecision(
            action=MemoryPolicyAction.SHARE,
            allowed=False,
            reason="scope_not_enabled",
            memory_scope=target_scope,
            scope_key=target_scope_key,
        )
    return authorize_memory_share(
        principal_id=principal_id,
        memory_scope=target_scope,
        scope_key=target_scope_key,
        accessible_projects=accessible_projects,
        accessible_teams=accessible_teams,
        accessible_delegations=accessible_delegations,
    )


def _authorize_share_source_read(
    *,
    memory: RawMemory,
    principal_id: str | None,
    accessible_projects: Iterable[str] | None,
    accessible_teams: Iterable[str] | None = None,
    accessible_delegations: Iterable[str] | None = None,
) -> MemoryPolicyDecision:
    if memory.memory_scope is MemoryScope.PRIVATE and memory.principal_id != principal_id:
        return MemoryPolicyDecision(
            action=MemoryPolicyAction.READ,
            allowed=False,
            reason="principal_mismatch",
            memory_scope=memory.memory_scope,
            scope_key=memory.scope_key,
        )
    return authorize_memory_read(
        principal_id=principal_id,
        memory_scope=memory.memory_scope,
        scope_key=memory.scope_key,
        accessible_projects=accessible_projects,
        accessible_teams=accessible_teams,
        accessible_delegations=accessible_delegations,
    )


def _authorize_correction_source_write(
    *,
    memory: RawMemory,
    principal_id: str | None,
    accessible_projects: Iterable[str] | None,
    accessible_teams: Iterable[str] | None = None,
    accessible_delegations: Iterable[str] | None = None,
) -> MemoryPolicyDecision:
    if memory.memory_scope is MemoryScope.PRIVATE and memory.principal_id != principal_id:
        return MemoryPolicyDecision(
            action=MemoryPolicyAction.WRITE,
            allowed=False,
            reason="principal_mismatch",
            memory_scope=memory.memory_scope,
            scope_key=memory.scope_key,
        )
    return authorize_memory_write(
        principal_id=principal_id,
        memory_scope=memory.memory_scope,
        scope_key=memory.scope_key,
        accessible_projects=accessible_projects,
        accessible_teams=accessible_teams,
        accessible_delegations=accessible_delegations,
    )


def _policy_denial_reason(metadata: Mapping[str, Any]) -> str:
    reasons = metadata.get("policy_reasons")
    if isinstance(reasons, list):
        denied = [str(reason) for reason in reasons if str(reason)]
        if denied:
            return denied[0]
    return "promotion_policy_denied"


def _metadata_str(metadata: Mapping[str, object], key: str) -> str | None:
    value = metadata.get(key)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _metadata_str_list(metadata: Mapping[str, object], key: str) -> list[str]:
    value = metadata.get(key)
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item)]


def _metadata_str_values(metadata: Mapping[str, object], *keys: str) -> list[str]:
    values: list[str] = []
    for key in keys:
        value = metadata.get(key)
        if isinstance(value, str):
            values.append(value)
            continue
        if isinstance(value, Iterable) and not isinstance(value, Mapping):
            values.extend(str(item) for item in value if str(item))
    return list(dict.fromkeys(item for item in values if item))


def _metadata_dict_values(metadata: Mapping[str, object], key: str) -> list[dict[str, object]]:
    value = metadata.get(key)
    if not isinstance(value, list):
        return []
    dictionaries: list[dict[str, object]] = []
    for item in value:
        if isinstance(item, Mapping):
            mapping = cast(Mapping[object, object], item)
            dictionaries.append({str(field): item_value for field, item_value in mapping.items()})
    return dictionaries


def _metadata_float(metadata: Mapping[str, object], key: str, default: float) -> float:
    value = metadata.get(key)
    if isinstance(value, int | float | str):
        try:
            return float(value)
        except ValueError:
            return default
    return default


def _raw_source_ids(memory: RawMemory) -> list[str]:
    return list(dict.fromkeys(_metadata_str_list(memory.metadata, "raw_source_ids")))


def _candidate_source_ids(
    candidate: ReflectionCandidate,
    source_id: str | None,
) -> list[str]:
    return list(
        dict.fromkeys(
            item
            for item in (
                *([source_id] if source_id else []),
                *candidate.raw_source_ids,
                *_metadata_str_values(candidate.metadata, "raw_source_ids", "source_ids"),
            )
            if item
        )
    )


_SUPERSEDES_METADATA_KEYS = (
    "supersedes",
    "supersedes_ids",
    "superseded_ids",
    "supersedes_entity_ids",
)


def _superseded_entity_ids(metadata: Mapping[str, object]) -> list[str]:
    return _metadata_str_values(metadata, *_SUPERSEDES_METADATA_KEYS)


def _with_authorized_supersedes(
    metadata: Mapping[str, object], authorized_ids: Sequence[str]
) -> dict[str, object]:
    """Replace any supersedes id list in metadata with the authorized targets."""
    sanitized = dict(metadata)
    for key in _SUPERSEDES_METADATA_KEYS:
        if key in sanitized:
            sanitized[key] = list(authorized_ids)
    return sanitized


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


def _raw_memory_write_allowed(
    *,
    memory: RawMemory,
    principal_id: str | None,
    accessible_projects: Iterable[str] | None,
) -> bool:
    if memory.memory_scope is MemoryScope.PRIVATE and memory.principal_id != principal_id:
        return False
    decision = authorize_memory_write(
        principal_id=principal_id,
        memory_scope=memory.memory_scope,
        scope_key=memory.scope_key,
        accessible_projects=accessible_projects,
    )
    return decision.allowed


def _promoted_entity_owner_id(entity: Any, metadata: Mapping[str, object]) -> str | None:
    owner = getattr(entity, "created_by", None)
    if owner:
        return str(owner)
    for key in ("principal_id", "created_by_user_id"):
        value = _metadata_str(metadata, key)
        if value:
            return value
    return None


def _promoted_entity_write_allowed(
    *,
    entity: Any,
    principal_id: str | None,
    accessible_projects: Iterable[str] | None,
) -> bool:
    raw_metadata = getattr(entity, "metadata", {})
    metadata = raw_metadata if isinstance(raw_metadata, Mapping) else {}
    target_scope = _resolve_memory_scope(
        _metadata_str(metadata, "memory_scope"),
        _metadata_str(metadata, "project_id"),
    )
    if target_scope is MemoryScope.PRIVATE:
        owner_id = _promoted_entity_owner_id(entity, metadata)
        if not owner_id or owner_id != principal_id:
            return False
    target_scope_key = _resolve_scope_key(
        target_scope,
        _metadata_str(metadata, "scope_key"),
        _metadata_str(metadata, "project_id"),
    )
    decision = authorize_memory_write(
        principal_id=principal_id,
        memory_scope=target_scope,
        scope_key=target_scope_key,
        accessible_projects=accessible_projects,
    )
    return decision.allowed


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
    accessible_projects: Iterable[str] | None,
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
            accessible_projects=accessible_projects,
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
    accessible_projects: Iterable[str] | None,
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
                    accessible_projects=accessible_projects,
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
                    accessible_projects=accessible_projects,
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
            accessible_projects=accessible_projects,
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


async def _authorized_superseded_entity_ids(
    *,
    runtime: Any,
    principal_id: str | None,
    accessible_projects: Iterable[str] | None,
    candidate: ReflectionCandidate,
) -> list[str]:
    authorized_ids: list[str] = []
    for entity_id in _superseded_entity_ids(candidate.metadata):
        try:
            target_entity = await runtime.entity_manager.get(entity_id)
        except Exception:
            continue
        if target_entity is None:
            continue
        if _promoted_entity_write_allowed(
            entity=target_entity,
            principal_id=principal_id,
            accessible_projects=accessible_projects,
        ):
            authorized_ids.append(entity_id)
    return authorized_ids


def _is_reflection_candidate(memory: RawMemory) -> bool:
    return (
        memory.capture_surface == "reflection_candidate"
        or _metadata_str(memory.metadata, "capture_surface") == "reflection_candidate"
    )


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


def _principal_denial(
    memories: Sequence[RawMemory],
    *,
    candidate_id: str,
    principal_id: str | None,
    raw_source_ids: list[str],
) -> ReflectionPromotionResult | None:
    if not principal_id:
        return _promotion_denied(
            candidate_id=candidate_id,
            reason="principal_mismatch",
            review_state=memories[0].review_state,
            memory_scope=memories[0].memory_scope,
            scope_key=memories[0].scope_key,
            raw_source_ids=raw_source_ids,
        )
    for memory in memories:
        if memory.memory_scope is MemoryScope.PRIVATE and memory.principal_id != principal_id:
            return _promotion_denied(
                candidate_id=candidate_id,
                reason="principal_mismatch",
                review_state=memories[0].review_state,
                memory_scope=memory.memory_scope,
                scope_key=memory.scope_key,
                raw_source_ids=raw_source_ids,
            )
    return None


def _source_scope_denial(
    memories: Sequence[RawMemory],
    *,
    candidate_id: str,
    principal_id: str | None,
    raw_source_ids: list[str],
    accessible_projects: Iterable[str] | None,
    accessible_teams: Iterable[str] | None = None,
    accessible_delegations: Iterable[str] | None = None,
) -> ReflectionPromotionResult | None:
    for memory in memories:
        read_decision = authorize_memory_read(
            principal_id=principal_id,
            memory_scope=memory.memory_scope,
            scope_key=memory.scope_key,
            accessible_projects=accessible_projects,
            accessible_teams=accessible_teams,
            accessible_delegations=accessible_delegations,
        )
        if not read_decision.allowed:
            return _promotion_denied(
                candidate_id=candidate_id,
                reason=read_decision.reason,
                review_state=memories[0].review_state,
                memory_scope=memory.memory_scope,
                scope_key=memory.scope_key,
                raw_source_ids=raw_source_ids,
                policy_decisions=(read_decision,),
                metadata={"input_scopes": _scope_metadata(memories)},
            )
    return None


def _coerce_promotion_scope(value: MemoryScope | str | None) -> MemoryScope | None:
    if isinstance(value, MemoryScope):
        return value
    if value is None:
        return None
    try:
        return MemoryScope(str(value))
    except ValueError:
        return None


def _scope_identity(memory: RawMemory) -> tuple[MemoryScope, str | None]:
    return memory.memory_scope, memory.scope_key


def _has_mixed_scope_inputs(memories: Sequence[RawMemory]) -> bool:
    return len({_scope_identity(memory) for memory in memories}) > 1


def _broadest_scope(memories: Sequence[RawMemory]) -> MemoryScope:
    return max((memory.memory_scope for memory in memories), key=lambda scope: _SCOPE_RANK[scope])


def _scope_metadata(memories: Sequence[RawMemory]) -> list[dict[str, str | None]]:
    return [
        {
            "id": memory.id,
            "memory_scope": memory.memory_scope.value,
            "scope_key": memory.scope_key,
        }
        for memory in memories
    ]


def _missing_promotion_target_reason(
    candidate_memory: RawMemory,
    input_memories: Sequence[RawMemory],
) -> str:
    if _has_mixed_scope_inputs(input_memories):
        return "mixed_scope_inputs_require_promote_to_scope"
    suggested_scope = _coerce_promotion_scope(
        _metadata_str(candidate_memory.metadata, "suggested_memory_scope")
    )
    suggested_key = _metadata_str(candidate_memory.metadata, "suggested_scope_key")
    if suggested_scope and (
        suggested_scope is not candidate_memory.memory_scope
        or suggested_key != candidate_memory.scope_key
    ):
        return "scope_crossing_requires_promotion"
    return "missing_promote_to_scope"


def _resolve_promotion_scope_key(
    *,
    target_scope: MemoryScope,
    promote_to_scope_key: str | None,
    project: str | None,
    candidate_memory: RawMemory,
) -> str | None:
    if promote_to_scope_key:
        return promote_to_scope_key
    if target_scope is MemoryScope.PROJECT:
        return project or _metadata_str(candidate_memory.metadata, "suggested_scope_key")
    return None


def _candidate_from_review_memory(
    memory: RawMemory,
    *,
    raw_source_ids: list[str],
    target_scope: MemoryScope,
    target_scope_key: str | None,
    domain: str | None,
) -> ReflectionCandidate:
    metadata = {
        **memory.metadata,
        "raw_source_ids": raw_source_ids,
        "source_ids": raw_source_ids,
        "review_capture_id": memory.id,
        "suggested_memory_scope": target_scope.value,
        "suggested_scope_key": target_scope_key,
        "review_state": memory.review_state,
    }
    resolved_domain = domain or _metadata_str(memory.metadata, "domain")
    if resolved_domain:
        metadata["domain"] = resolved_domain
    return ReflectionCandidate(
        kind=memory.entity_type or _metadata_str(memory.metadata, "remember_kind") or "episode",
        title=memory.title,
        content=memory.raw_content,
        reason=_metadata_str(memory.metadata, "reflection_reason") or "accepted for promotion",
        confidence=_metadata_float(memory.metadata, "confidence", 1.0),
        tags=list(memory.tags),
        metadata=metadata,
        raw_source_ids=list(raw_source_ids),
        suggested_memory_scope=target_scope.value,
        suggested_scope_key=target_scope_key,
        review_state=memory.review_state,
    )


def _candidate_from_raw_memory(
    memory: RawMemory,
    *,
    target_scope: MemoryScope,
    target_scope_key: str | None,
    domain: str | None,
) -> ReflectionCandidate:
    metadata = {
        **memory.metadata,
        "capture_mode": "promote",
        "imported_capture_id": memory.id,
        "native_write_path": "raw_memory_promotion",
        "promoted_capture_surface": "raw_memory_promotion",
        "raw_source_ids": [memory.id],
        "source_ids": [memory.id],
        "suggested_memory_scope": target_scope.value,
        "suggested_scope_key": target_scope_key,
    }
    resolved_domain = domain or _metadata_str(memory.metadata, "domain")
    if resolved_domain:
        metadata["domain"] = resolved_domain
    return ReflectionCandidate(
        kind=memory.entity_type or _metadata_str(memory.metadata, "remember_kind") or "episode",
        title=memory.title,
        content=memory.raw_content,
        reason=_metadata_str(memory.metadata, "promotion_reason")
        or "accepted raw memory for promotion",
        confidence=_metadata_float(memory.metadata, "confidence", 1.0),
        tags=list(memory.tags),
        metadata=metadata,
        raw_source_ids=[memory.id],
        suggested_memory_scope=target_scope.value,
        suggested_scope_key=target_scope_key,
        review_state=memory.review_state,
    )


def _share_source_metadata(memory: RawMemory) -> dict[str, object]:
    metadata = {
        key: value
        for key, value in memory.metadata.items()
        if key in _SHARE_SOURCE_METADATA_ALLOWLIST
    }
    if promoted_id := _metadata_str(memory.metadata, "promoted_entity_id"):
        metadata["share_source_promoted_entity_id"] = promoted_id
    return metadata


def _candidate_from_share_memory(
    memory: RawMemory,
    *,
    target_scope: MemoryScope,
    target_scope_key: str | None,
    domain: str | None,
) -> ReflectionCandidate:
    metadata = {
        **_share_source_metadata(memory),
        "capture_mode": "share",
        "imported_capture_id": memory.id,
        "native_write_path": "memory_share",
        "promoted_capture_surface": "memory_share",
        "raw_source_ids": [memory.id],
        "share_source_capture_surface": memory.capture_surface,
        "share_source_id": memory.id,
        "share_source_scope": memory.memory_scope.value,
        "share_source_scope_key": memory.scope_key,
        "share_target_scope": target_scope.value,
        "share_target_scope_key": target_scope_key,
        "source_ids": [memory.id],
        "suggested_memory_scope": target_scope.value,
        "suggested_scope_key": target_scope_key,
    }
    resolved_domain = domain or _metadata_str(memory.metadata, "domain")
    if resolved_domain:
        metadata["domain"] = resolved_domain
    return ReflectionCandidate(
        kind=memory.entity_type or _metadata_str(memory.metadata, "remember_kind") or "episode",
        title=memory.title,
        content=memory.raw_content,
        reason=_metadata_str(memory.metadata, "share_reason") or "shared memory promotion",
        confidence=_metadata_float(memory.metadata, "confidence", 1.0),
        tags=list(memory.tags),
        metadata=metadata,
        raw_source_ids=[memory.id],
        suggested_memory_scope=target_scope.value,
        suggested_scope_key=target_scope_key,
        review_state=memory.review_state,
    )


def _entity_type(kind: str) -> EntityType:
    try:
        return EntityType(kind)
    except ValueError:
        return EntityType.EPISODE


def _entity_from_candidate(
    candidate: ReflectionCandidate,
    *,
    organization_id: str,
    principal_id: str | None,
    domain: str | None,
    project: str | None,
    source_id: str | None,
    memory_scope: MemoryScope,
    scope_key: str | None,
    policy_metadata: Mapping[str, Any],
) -> Entity:
    entity_type = _entity_type(candidate.kind)
    entity_id = _generate_id(entity_type.value, candidate.title, domain or "general")
    source_ids = _candidate_source_ids(candidate, source_id)
    primary_source_id = source_id or (source_ids[0] if source_ids else None)
    native_write_path = _metadata_str(candidate.metadata, "native_write_path")
    if not native_write_path:
        native_write_path = "reflection_promotion"
    capture_mode = _metadata_str(candidate.metadata, "capture_mode") or "reflect"
    capture_surface = _metadata_str(candidate.metadata, "promoted_capture_surface")
    if not capture_surface:
        capture_surface = "reflection"
    metadata = normalize_memory_quality_metadata(
        {
            **candidate.metadata,
            "tags": list(candidate.tags),
            "organization_id": organization_id,
            "capture_mode": capture_mode,
            "capture_surface": capture_surface,
            "remember_kind": candidate.kind,
            "reflection_reason": candidate.reason,
            "confidence": candidate.confidence,
            "raw_source_ids": source_ids,
            "source_ids": source_ids,
            "native_write_path": native_write_path,
            **dict(policy_metadata),
        }
    )
    if domain:
        metadata["category"] = domain
    elif metadata.get("category") is None:
        metadata.pop("category", None)
    if project:
        metadata["project_id"] = project
    if primary_source_id:
        metadata["reflection_source_id"] = primary_source_id

    return Entity(
        id=entity_id,
        entity_type=entity_type,
        name=candidate.title,
        description=candidate.content[:500],
        content=candidate.content,
        organization_id=organization_id,
        created_by=principal_id,
        metadata=metadata,
        source_file=primary_source_id,
    )


def _relationships_for_promotion(
    entity_id: str,
    *,
    project: str | None,
    source_id: str | None,
    related_to: Sequence[str] | None,
    supersedes: Sequence[str] | None,
    raw_source_ids: Sequence[str] | None,
    native_write_path: str = "reflection_promotion",
) -> list[Relationship]:
    relationships: list[Relationship] = []
    if project and project != entity_id:
        relationships.append(
            _relationship(
                entity_id,
                project,
                RelationshipType.BELONGS_TO,
                metadata={"native_write_path": native_write_path},
            )
        )
    if source_id and source_id != entity_id:
        relationships.append(
            _relationship(
                entity_id,
                source_id,
                RelationshipType.DERIVED_FROM,
                metadata={"native_write_path": native_write_path, "source_id": source_id},
            )
        )
    excluded_targets = {entity_id, project, source_id}
    for related_id in related_to or ():
        if related_id in excluded_targets:
            continue
        relationships.append(
            _relationship(
                entity_id,
                related_id,
                RelationshipType.RELATED_TO,
                metadata={"native_write_path": native_write_path},
            )
        )
    for superseded_id in supersedes or ():
        if superseded_id in excluded_targets:
            continue
        source_ids = list(raw_source_ids or [])
        valid_from = datetime.now(UTC).isoformat()
        relationships.append(
            _relationship(
                entity_id,
                superseded_id,
                RelationshipType.SUPERSEDES,
                metadata={
                    "native_write_path": native_write_path,
                    "raw_source_ids": source_ids,
                    "source_id": source_ids[0] if source_ids else None,
                    "replacement_reason": "accepted_reflection_candidate",
                    "valid_from": valid_from,
                },
            )
        )
    return relationships


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


__all__ = [
    "MemoryAccessPreview",
    "MemoryCorrectionPreview",
    "MemoryCorrectionResult",
    "MemorySharePreview",
    "MemoryShareResult",
    "ReflectionPromotionPreview",
    "ReflectionPromotionResult",
    "ReflectionWriteResult",
    "WriteMode",
    "apply_memory_correction",
    "coerce_write_mode",
    "persist_reflection_candidate",
    "persist_reflection_source",
    "preview_memory_access",
    "preview_memory_correction",
    "preview_memory_share",
    "preview_raw_memory_promotion",
    "preview_reflection_candidate_promotion",
    "promote_raw_memory",
    "promote_reflection_candidate_review",
    "reflection_write_enabled",
    "share_memory",
    "write_mode_from_env",
]
