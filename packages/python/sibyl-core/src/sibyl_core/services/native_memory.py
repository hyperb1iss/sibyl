"""Native SurrealDB memory write services."""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, cast

from sibyl_core.auth.memory_policy import (
    MemoryPolicyAction,
    MemoryPolicyDecision,
    authorize_memory_read,
    authorize_memory_reflect,
    authorize_memory_share,
    authorize_memory_write,
)
from sibyl_core.models.entities import Entity, EntityType, Relationship, RelationshipType
from sibyl_core.models.reflection import (
    MemoryLifecycle,
    MemoryLifecycleState,
    ReflectionCandidate,
    ReflectionFinding,
    ReflectionFindingKind,
    correction_finding_kind,
    with_memory_lifecycle_metadata,
    with_reflection_finding_metadata,
)
from sibyl_core.services.memory_autonomy import reflection_autonomy_candidate_metadata
from sibyl_core.services.native_graph import get_native_graph_runtime
from sibyl_core.services.surreal_content import (
    MemoryScope,
    RawMemory,
    get_raw_memory,
    get_raw_memory_by_source_id,
    list_raw_memories_for_scope,
    raw_memory_recallable,
    save_raw_memory,
)
from sibyl_core.tools.helpers import _generate_id
from sibyl_core.tools.responses import AddResponse


class NativeWriteMode(StrEnum):
    DISABLED = "disabled"
    ENABLED = "enabled"


@dataclass(frozen=True, slots=True)
class NativeReflectionWriteResult:
    response: AddResponse
    metadata: dict[str, Any]


@dataclass(frozen=True, slots=True)
class NativeReflectionPromotionResult:
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
class NativeReflectionPromotionPreview:
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
class NativeMemorySharePreview:
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
class NativeMemoryAccessPreview:
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
class NativeMemoryCorrectionPreview:
    allowed: bool
    source_id: str
    action: str
    reason: str
    target_review_state: str
    affected_source_ids: list[str]
    affected_derived_ids: list[str]
    reversible: bool
    recall_impact: dict[str, Any]
    synthesis_impact: dict[str, Any]
    audit_action: str
    policy_decisions: tuple[MemoryPolicyDecision, ...] = ()
    metadata: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class NativeMemoryCorrectionResult:
    applied: bool
    preview: NativeMemoryCorrectionPreview
    updated_memory: RawMemory | None = None


@dataclass(frozen=True, slots=True)
class _NativeReflectionPromotionPlan:
    candidate_memory: RawMemory
    promotion_candidate: ReflectionCandidate
    target_scope: MemoryScope
    target_scope_key: str | None
    target_project: str | None
    raw_source_ids: list[str]
    input_memories: list[RawMemory]


_PROMOTED_REVIEW_STATE = "promoted"
_ACCESS_PREVIEW_OVERFETCH_FACTOR = 4
_CORRECTION_TARGET_STATES: dict[str, str] = {
    "delete": "deleted",
    "hide": "hidden",
    "mark_duplicate": "duplicate",
    "mark_sensitive": "sensitive",
    "mark_stale": "stale",
    "mark_wrong": "wrong",
    "redact": "redacted",
    "restore": "pending",
    "supersede": "superseded",
}
_CORRECTION_RECALL_EXCLUDED_STATES = frozenset(
    {
        "deleted",
        "duplicate",
        "hidden",
        "redacted",
        "sensitive",
        "stale",
        "superseded",
        "wrong",
    }
)
_CORRECTION_IRREVERSIBLE_ACTIONS = frozenset({"delete", "redact"})
_SCOPE_RANK: dict[MemoryScope, int] = {
    MemoryScope.PRIVATE: 0,
    MemoryScope.DELEGATED: 1,
    MemoryScope.PROJECT: 2,
    MemoryScope.TEAM: 3,
    MemoryScope.ORGANIZATION: 4,
    MemoryScope.SHARED: 5,
    MemoryScope.PUBLIC: 6,
}


def coerce_native_write_mode(value: str | NativeWriteMode | None) -> NativeWriteMode:
    if isinstance(value, NativeWriteMode):
        return value
    if value is None or not value.strip():
        return NativeWriteMode.ENABLED
    normalized = value.strip().lower()
    if normalized in {"enabled", "enable", "true", "1", "yes", "on"}:
        return NativeWriteMode.ENABLED
    if normalized in {"disabled", "disable", "false", "0", "no", "off"}:
        return NativeWriteMode.DISABLED
    return NativeWriteMode.DISABLED


def native_write_mode_from_env(environ: Mapping[str, str] | None = None) -> NativeWriteMode:
    source = os.environ if environ is None else environ
    return coerce_native_write_mode(source.get("SIBYL_NATIVE_WRITE"))


def native_reflection_write_enabled(environ: Mapping[str, str] | None = None) -> bool:
    return native_write_mode_from_env(environ) is NativeWriteMode.ENABLED


async def persist_reflection_source_native(
    *,
    title: str,
    content: str,
    organization_id: str,
    principal_id: str | None,
    domain: str | None = None,
    project: str | None = None,
    related_to: Sequence[str] | None = None,
    accessible_projects: Iterable[str] | None = None,
    memory_scope: MemoryScope | str | None = None,
    scope_key: str | None = None,
) -> NativeReflectionWriteResult:
    candidate = ReflectionCandidate(
        kind=EntityType.SESSION.value,
        title=title,
        content=content,
        reason="preserves raw reflection source material",
        confidence=1.0,
        tags=["reflection", EntityType.SESSION.value],
        metadata={"reflection_source": True},
    )
    return await persist_reflection_candidate_native(
        candidate=candidate,
        organization_id=organization_id,
        principal_id=principal_id,
        domain=domain,
        project=project,
        source_id=None,
        related_to=related_to,
        accessible_projects=accessible_projects,
        memory_scope=memory_scope,
        scope_key=scope_key,
    )


async def persist_reflection_candidate_native(
    *,
    candidate: ReflectionCandidate,
    organization_id: str,
    principal_id: str | None,
    domain: str | None = None,
    project: str | None = None,
    source_id: str | None = None,
    related_to: Sequence[str] | None = None,
    accessible_projects: Iterable[str] | None = None,
    memory_scope: MemoryScope | str | None = None,
    scope_key: str | None = None,
    link_source_entity: bool = True,
) -> NativeReflectionWriteResult:
    scope = _resolve_memory_scope(memory_scope, project)
    resolved_scope_key = _resolve_scope_key(scope, scope_key, project)
    policy_decisions = _authorize_reflection_write(
        principal_id=principal_id,
        memory_scope=scope,
        scope_key=resolved_scope_key,
        accessible_projects=accessible_projects,
    )
    policy_metadata = _policy_metadata(policy_decisions)
    if any(not decision.allowed for decision in policy_decisions):
        return NativeReflectionWriteResult(
            response=AddResponse(
                success=False,
                id=None,
                message=_policy_denied_message(policy_decisions),
                timestamp=datetime.now(UTC),
            ),
            metadata=policy_metadata,
        )

    runtime = await get_native_graph_runtime(organization_id)
    source_ids = _candidate_source_ids(candidate, source_id)
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
            "metadata": _promotion_lifecycle_metadata(
                metadata=entity.metadata,
                promoted_entity_id=entity.id,
                source_ids=source_ids,
                source_id=source_ids[0] if source_ids else None,
                reason=candidate.reason,
                policy_metadata=policy_metadata,
            )
        }
    )
    created_id = await runtime.entity_manager.create_direct(entity)
    superseded_ids = await _authorized_superseded_entity_ids(
        runtime=runtime,
        principal_id=principal_id,
        accessible_projects=accessible_projects,
        candidate=candidate,
    )
    relationships = _relationships_for_promotion(
        created_id,
        project=project,
        source_id=source_id if link_source_entity else None,
        related_to=related_to,
        supersedes=superseded_ids,
        raw_source_ids=source_ids,
    )
    if relationships:
        await runtime.relationship_manager.create_bulk(relationships)

    return NativeReflectionWriteResult(
        response=AddResponse(
            success=True,
            id=created_id,
            message=f"Promoted natively: {candidate.title}",
            timestamp=datetime.now(UTC),
        ),
        metadata={
            **policy_metadata,
            "native_write_mode": NativeWriteMode.ENABLED.value,
            "native_write_path": "reflection_promotion",
            "native_relationship_count": len(relationships),
            "raw_source_ids": source_ids,
            "source_ids": source_ids,
        },
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
) -> NativeReflectionPromotionResult:
    plan = await _resolve_reflection_promotion_plan(
        candidate_id=candidate_id,
        organization_id=organization_id,
        principal_id=principal_id,
        promote_to_scope=promote_to_scope,
        promote_to_scope_key=promote_to_scope_key,
        domain=domain,
        project=project,
    )
    if isinstance(plan, NativeReflectionPromotionResult):
        return plan

    native_result = await persist_reflection_candidate_native(
        candidate=plan.promotion_candidate,
        organization_id=organization_id,
        principal_id=principal_id,
        domain=domain or _metadata_str(plan.candidate_memory.metadata, "domain"),
        project=plan.target_project,
        source_id=plan.raw_source_ids[0] if plan.raw_source_ids else None,
        related_to=related_to,
        accessible_projects=accessible_projects,
        memory_scope=plan.target_scope,
        scope_key=plan.target_scope_key,
        link_source_entity=False,
    )
    if not native_result.response.success:
        return NativeReflectionPromotionResult(
            success=False,
            candidate_id=plan.candidate_memory.id,
            promoted_id=None,
            reason=_policy_denial_reason(native_result.metadata),
            review_state=plan.candidate_memory.review_state,
            memory_scope=plan.target_scope,
            scope_key=plan.target_scope_key,
            raw_source_ids=plan.raw_source_ids,
            metadata=native_result.metadata,
        )

    promoted_at = datetime.now(UTC).isoformat()
    metadata = {
        **plan.candidate_memory.metadata,
        **native_result.metadata,
        "review_state": _PROMOTED_REVIEW_STATE,
        "promoted_at": promoted_at,
        "promoted_entity_id": native_result.response.id,
        "promote_to_scope": plan.target_scope.value,
        "promote_to_scope_key": plan.target_scope_key,
        "raw_source_ids": plan.raw_source_ids,
        "source_ids": plan.raw_source_ids,
    }
    metadata = _promotion_lifecycle_metadata(
        metadata=metadata,
        promoted_entity_id=str(native_result.response.id),
        source_ids=plan.raw_source_ids,
        source_id=plan.candidate_memory.id,
        reason="accepted_reflection_candidate",
        policy_metadata=native_result.metadata,
    )
    updated = replace(
        plan.candidate_memory,
        review_state=_PROMOTED_REVIEW_STATE,
        metadata=metadata,
    )
    await save_raw_memory(updated)

    return NativeReflectionPromotionResult(
        success=True,
        candidate_id=plan.candidate_memory.id,
        promoted_id=native_result.response.id,
        reason="promoted",
        review_state=_PROMOTED_REVIEW_STATE,
        memory_scope=plan.target_scope,
        scope_key=plan.target_scope_key,
        raw_source_ids=plan.raw_source_ids,
        metadata=metadata,
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
) -> NativeReflectionPromotionPreview:
    plan = await _resolve_reflection_promotion_plan(
        candidate_id=candidate_id,
        organization_id=organization_id,
        principal_id=principal_id,
        promote_to_scope=promote_to_scope,
        promote_to_scope_key=promote_to_scope_key,
        domain=domain,
        project=project,
    )
    if isinstance(plan, NativeReflectionPromotionResult):
        return _promotion_preview_from_denial(plan)

    policy_decisions = _authorize_reflection_write(
        principal_id=principal_id,
        memory_scope=plan.target_scope,
        scope_key=plan.target_scope_key,
        accessible_projects=accessible_projects,
    )
    metadata = {
        **_policy_metadata(policy_decisions),
        **reflection_autonomy_candidate_metadata(plan.candidate_memory),
        "input_scopes": _scope_metadata(plan.input_memories),
        "source_count": len(plan.raw_source_ids),
        "target_project": plan.target_project,
    }
    allowed = all(decision.allowed for decision in policy_decisions)
    return NativeReflectionPromotionPreview(
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


async def preview_memory_share(
    *,
    source_ids: Sequence[str],
    organization_id: str,
    principal_id: str | None,
    target_scope: MemoryScope | str | None,
    target_scope_key: str | None = None,
    recipient_organization_id: str | None = None,
    accessible_projects: Iterable[str] | None = None,
    accessible_delegations: Iterable[str] | None = None,
) -> NativeMemorySharePreview:
    requested_source_ids = [str(source_id) for source_id in source_ids]
    normalized_target = _coerce_promotion_scope(target_scope)
    target_decision = _authorize_share_target(
        principal_id=principal_id,
        target_scope=normalized_target,
        target_scope_key=target_scope_key,
        recipient_organization_id=recipient_organization_id,
        organization_id=organization_id,
        accessible_projects=accessible_projects,
        accessible_delegations=accessible_delegations,
    )
    decisions: list[MemoryPolicyDecision] = [target_decision]
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
    return NativeMemorySharePreview(
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


def _space_field(space: Mapping[str, object] | object, key: str) -> object | None:
    if isinstance(space, Mapping):
        mapping = cast(Mapping[str, object], space)
        return mapping.get(key)
    return getattr(space, key, None)


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
) -> NativeMemoryAccessPreview:
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
    access_state = (
        "allowed"
        if allowed
        else "partial"
        if visible_source_ids
        else "denied"
    )
    metadata: dict[str, Any] = {
        "access_state": access_state,
        "denied_memory_space_ids": [space_id for space_id in denied_space_ids if space_id],
        "input_scopes": input_scopes,
        "lifecycle_hidden_source_ids": lifecycle_hidden_source_ids,
        "policy_reasons": policy_reasons,
        "target_principal_type": normalized_target_type,
        "visible_count": len(visible_source_ids),
    }
    return NativeMemoryAccessPreview(
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
            str(_space_field(space, "id"))
            for space in memory_spaces
            if _space_field(space, "id")
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
    target_review_state: str = "",
    policy_decisions: Sequence[MemoryPolicyDecision] = (),
    metadata: dict[str, Any] | None = None,
) -> NativeMemoryCorrectionPreview:
    return NativeMemoryCorrectionPreview(
        allowed=False,
        source_id=source_id,
        action=action,
        reason=reason,
        target_review_state=target_review_state,
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
) -> str | None:
    if action == "supersede" and not replacement_source_id:
        return "missing_replacement_source"
    if action == "mark_duplicate" and not duplicate_of_source_id:
        return "missing_duplicate_source"
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


def _correction_impact(target_review_state: str) -> tuple[dict[str, Any], dict[str, Any]]:
    excluded = target_review_state in _CORRECTION_RECALL_EXCLUDED_STATES
    recall = {
        "excluded_from_recall": excluded,
        "target_review_state": target_review_state,
    }
    synthesis = {
        "excluded_from_synthesis": excluded,
        "preserve_source_truth": True,
        "target_review_state": target_review_state,
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
    accessible_delegations: Iterable[str] | None = None,
    replacement_source_id: str | None = None,
    duplicate_of_source_id: str | None = None,
) -> NativeMemoryCorrectionPreview:
    normalized_action = action.strip().lower()
    target_review_state = _CORRECTION_TARGET_STATES.get(normalized_action)
    if target_review_state is None:
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
            target_review_state=target_review_state,
        )
    if normalized_action == "restore":
        target_review_state = _metadata_str(memory.metadata, "prior_review_state") or "pending"

    read_decision = _authorize_share_source_read(
        memory=memory,
        principal_id=principal_id,
        accessible_projects=accessible_projects,
        accessible_delegations=accessible_delegations,
    )
    if not read_decision.allowed:
        return _correction_preview_denied(
            source_id=memory.id,
            action=normalized_action,
            reason=read_decision.reason,
            target_review_state=target_review_state,
            policy_decisions=(read_decision,),
            metadata={
                "policy_allowed": False,
                "policy_reasons": [read_decision.reason],
                "requested_source_id": source_id,
            },
        )

    requirement_reason = _correction_requirement_reason(
        action=normalized_action,
        replacement_source_id=replacement_source_id,
        duplicate_of_source_id=duplicate_of_source_id,
    )
    if requirement_reason:
        return _correction_preview_denied(
            source_id=memory.id,
            action=normalized_action,
            reason=requirement_reason,
            target_review_state=target_review_state,
            policy_decisions=(read_decision,),
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
                target_review_state=target_review_state,
                policy_decisions=(read_decision,),
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
                target_review_state=target_review_state,
                policy_decisions=(read_decision,),
                metadata={
                    "duplicate_of_source_id": duplicate_of_source_id,
                    "policy_allowed": False,
                    "policy_reasons": [reference_reason],
                    "requested_source_id": source_id,
                },
            )
        canonical_duplicate_of_source_id = reference.id if reference else duplicate_of_source_id

    recall_impact, synthesis_impact = _correction_impact(target_review_state)
    affected_derived_ids = _correction_derived_ids(memory)
    metadata = {
        "duplicate_of_source_id": canonical_duplicate_of_source_id,
        "policy_allowed": True,
        "policy_reasons": [read_decision.reason],
        "replacement_source_id": canonical_replacement_source_id,
        "requested_source_id": source_id,
    }
    return NativeMemoryCorrectionPreview(
        allowed=True,
        source_id=memory.id,
        action=normalized_action,
        reason=reason or f"{normalized_action}_preview_allowed",
        target_review_state=target_review_state,
        affected_source_ids=[memory.id],
        affected_derived_ids=affected_derived_ids,
        reversible=normalized_action not in _CORRECTION_IRREVERSIBLE_ACTIONS,
        recall_impact=recall_impact,
        synthesis_impact=synthesis_impact,
        audit_action=_correction_audit_action(normalized_action),
        policy_decisions=(read_decision,),
        metadata=metadata,
    )


def _correction_metadata(
    *,
    memory: RawMemory,
    preview: NativeMemoryCorrectionPreview,
    reason: str | None,
    replacement_source_id: str | None,
    duplicate_of_source_id: str | None,
) -> dict[str, object]:
    metadata = dict(memory.metadata)
    history = list(_metadata_dict_values(metadata, "correction_history"))
    now = datetime.now(UTC).isoformat()
    prior_state = str(memory.review_state or "pending")
    if preview.action == "restore":
        for key in (
            "deleted_at",
            "duplicate_at",
            "duplicate_of_source_id",
            "hidden_at",
            "lifecycle_action",
            "lifecycle_reason",
            "lifecycle_state",
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
            state=preview.target_review_state,
            source_id=memory.id,
            action=preview.action,
            reason=reason or preview.reason,
            prior_state=prior_state,
            reversible=True,
        )
    else:
        if not _metadata_str(metadata, "prior_review_state"):
            metadata["prior_review_state"] = prior_state
        metadata["lifecycle_action"] = preview.action
        metadata["lifecycle_state"] = preview.target_review_state
        metadata["lifecycle_reason"] = reason or preview.reason
        metadata[f"{preview.target_review_state}_at"] = now
        if replacement_source_id:
            metadata["superseded_by_source_id"] = replacement_source_id
        if duplicate_of_source_id:
            metadata["duplicate_of_source_id"] = duplicate_of_source_id
        lifecycle = MemoryLifecycle(
            state=preview.target_review_state,
            source_id=memory.id,
            action=preview.action,
            reason=reason or preview.reason,
            prior_state=prior_state,
            replacement_source_id=replacement_source_id,
            duplicate_of_source_id=duplicate_of_source_id,
            derived_ids=preview.affected_derived_ids,
            reversible=preview.reversible,
        )
    history.append(
        {
            "action": preview.action,
            "audit_action": preview.audit_action,
            "reason": reason or preview.reason,
            "target_review_state": preview.target_review_state,
            "created_at": now,
            "replacement_source_id": replacement_source_id,
            "duplicate_of_source_id": duplicate_of_source_id,
        }
    )
    metadata["correction_history"] = history
    metadata["review_state"] = preview.target_review_state
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
                item
                for item in (replacement_source_id, duplicate_of_source_id)
                if item is not None
            ],
            policy_reasons=_metadata_str_values(preview.metadata or {}, "policy_reasons"),
            reversible=preview.reversible,
            metadata={
                "audit_action": preview.audit_action,
                "target_review_state": preview.target_review_state,
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
    accessible_delegations: Iterable[str] | None = None,
    replacement_source_id: str | None = None,
    duplicate_of_source_id: str | None = None,
) -> NativeMemoryCorrectionResult:
    preview = await preview_memory_correction(
        organization_id=organization_id,
        source_id=source_id,
        principal_id=principal_id,
        action=action,
        reason=reason,
        accessible_projects=accessible_projects,
        accessible_delegations=accessible_delegations,
        replacement_source_id=replacement_source_id,
        duplicate_of_source_id=duplicate_of_source_id,
    )
    if not preview.allowed:
        return NativeMemoryCorrectionResult(applied=False, preview=preview)

    memory = await _load_correction_memory(organization_id=organization_id, source_id=source_id)
    if memory is None:
        denied = _correction_preview_denied(
            source_id=source_id,
            action=preview.action,
            reason="memory_source_not_found",
            target_review_state=preview.target_review_state,
        )
        return NativeMemoryCorrectionResult(applied=False, preview=denied)
    preview_metadata = preview.metadata or {}
    canonical_replacement_source_id = (
        _metadata_str(preview_metadata, "replacement_source_id") or replacement_source_id
    )
    canonical_duplicate_of_source_id = (
        _metadata_str(preview_metadata, "duplicate_of_source_id") or duplicate_of_source_id
    )
    updated = replace(
        memory,
        review_state=preview.target_review_state,
        metadata=_correction_metadata(
            memory=memory,
            preview=preview,
            reason=reason,
            replacement_source_id=canonical_replacement_source_id,
            duplicate_of_source_id=canonical_duplicate_of_source_id,
        ),
    )
    saved = await save_raw_memory(updated)
    return NativeMemoryCorrectionResult(applied=True, preview=preview, updated_memory=saved)


async def _resolve_reflection_promotion_plan(
    *,
    candidate_id: str,
    organization_id: str,
    principal_id: str | None,
    promote_to_scope: MemoryScope | str | None,
    promote_to_scope_key: str | None = None,
    domain: str | None = None,
    project: str | None = None,
) -> _NativeReflectionPromotionPlan | NativeReflectionPromotionResult:
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
    return _NativeReflectionPromotionPlan(
        candidate_memory=candidate_memory,
        promotion_candidate=promotion_candidate,
        target_scope=target_scope,
        target_scope_key=target_scope_key,
        target_project=target_project,
        raw_source_ids=raw_source_ids,
        input_memories=input_memories,
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
) -> tuple[MemoryPolicyDecision, MemoryPolicyDecision]:
    reflect_decision = authorize_memory_reflect(
        principal_id=principal_id,
        memory_scope=memory_scope,
        scope_key=scope_key,
        accessible_projects=accessible_projects,
    )
    write_decision = authorize_memory_write(
        principal_id=principal_id,
        memory_scope=memory_scope,
        scope_key=scope_key,
        accessible_projects=accessible_projects,
    )
    return reflect_decision, write_decision


def _policy_metadata(decisions: Sequence[MemoryPolicyDecision]) -> dict[str, Any]:
    return {
        "native_write_mode": NativeWriteMode.ENABLED.value,
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
            state=MemoryLifecycleState.PROMOTED,
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
            lifecycle_state=MemoryLifecycleState.PROMOTED,
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
) -> NativeReflectionPromotionResult:
    payload = {"policy_reasons": [reason], "policy_allowed": False}
    if metadata:
        payload.update(metadata)
    return NativeReflectionPromotionResult(
        success=False,
        candidate_id=candidate_id,
        promoted_id=None,
        reason=reason,
        review_state=review_state,
        memory_scope=memory_scope,
        scope_key=scope_key,
        raw_source_ids=raw_source_ids,
        metadata=payload,
    )


def _promotion_preview_from_denial(
    result: NativeReflectionPromotionResult,
) -> NativeReflectionPromotionPreview:
    return NativeReflectionPromotionPreview(
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
        accessible_delegations=accessible_delegations,
    )


def _authorize_share_source_read(
    *,
    memory: RawMemory,
    principal_id: str | None,
    accessible_projects: Iterable[str] | None,
    accessible_delegations: Iterable[str] | None,
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


def _superseded_entity_ids(metadata: Mapping[str, object]) -> list[str]:
    return _metadata_str_values(
        metadata,
        "supersedes",
        "supersedes_ids",
        "superseded_ids",
        "supersedes_entity_ids",
    )


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
        metadata = target_entity.metadata if isinstance(target_entity.metadata, Mapping) else {}
        target_scope = _resolve_memory_scope(
            _metadata_str(metadata, "memory_scope"),
            _metadata_str(metadata, "project_id"),
        )
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
        if decision.allowed:
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
) -> NativeReflectionPromotionResult | None:
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
        confidence=_metadata_float(memory.metadata, "reflection_confidence", 1.0),
        tags=list(memory.tags),
        metadata=metadata,
        raw_source_ids=list(raw_source_ids),
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
    metadata = {
        **candidate.metadata,
        "tags": list(candidate.tags),
        "organization_id": organization_id,
        "capture_mode": "reflect",
        "capture_surface": "reflection",
        "remember_kind": candidate.kind,
        "reflection_reason": candidate.reason,
        "reflection_confidence": candidate.confidence,
        "raw_source_ids": source_ids,
        "source_ids": source_ids,
        "native_write_path": "reflection_promotion",
        **dict(policy_metadata),
    }
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
) -> list[Relationship]:
    relationships: list[Relationship] = []
    if project and project != entity_id:
        relationships.append(
            _relationship(
                entity_id,
                project,
                RelationshipType.BELONGS_TO,
                metadata={"native_write_path": "reflection_promotion"},
            )
        )
    if source_id and source_id != entity_id:
        relationships.append(
            _relationship(
                entity_id,
                source_id,
                RelationshipType.DERIVED_FROM,
                metadata={"native_write_path": "reflection_promotion", "source_id": source_id},
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
                metadata={"native_write_path": "reflection_promotion"},
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
                    "native_write_path": "reflection_promotion",
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
    "NativeMemoryAccessPreview",
    "NativeMemoryCorrectionPreview",
    "NativeMemoryCorrectionResult",
    "NativeMemorySharePreview",
    "NativeReflectionPromotionPreview",
    "NativeReflectionPromotionResult",
    "NativeReflectionWriteResult",
    "NativeWriteMode",
    "apply_memory_correction",
    "coerce_native_write_mode",
    "native_reflection_write_enabled",
    "native_write_mode_from_env",
    "persist_reflection_candidate_native",
    "persist_reflection_source_native",
    "preview_memory_access",
    "preview_memory_correction",
    "preview_memory_share",
    "preview_reflection_candidate_promotion",
    "promote_reflection_candidate_review",
]
