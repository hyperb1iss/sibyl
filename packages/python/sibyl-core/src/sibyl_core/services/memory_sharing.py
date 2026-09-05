"""Memory sharing and access preview services."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, cast

from sibyl_core.auth.memory_policy import (
    MemoryPolicyAction,
    MemoryPolicyDecision,
    authorize_memory_read,
)
from sibyl_core.models.reflection import ReflectionCandidate
from sibyl_core.services.memory_contract import (
    MemoryAccessPreview,
    MemorySharePreview,
    MemoryShareResult,
    ReflectionPromotionResult,
    _ReflectionPromotionPlan,
)
from sibyl_core.services.memory_policy import (
    _authorize_share_source_read,
    _authorize_share_target,
    _metadata_float,
    _metadata_str,
    _promotion_denied,
)
from sibyl_core.services.memory_promotion import (
    _coerce_promotion_scope,
    _principal_denial,
    _resolve_promotion_scope_key,
    _scope_metadata,
    _source_scope_denial,
)
from sibyl_core.services.memory_reflection import (
    _promotion_write_denied,
    persist_reflection_candidate,
)
from sibyl_core.services.surreal_content import (
    MemoryScope,
    RawMemory,
    get_raw_memory,
    list_raw_memories_for_scope,
    raw_memory_recallable,
)

_ACCESS_PREVIEW_OVERFETCH_FACTOR = 4


_SHARE_SOURCE_METADATA_ALLOWLIST = frozenset(
    {
        "category",
        "confidence",
        "domain",
        "remember_kind",
        "share_reason",
    }
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
    if not result.response.success or result.metadata.get("promotion_state") == "partial":
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
