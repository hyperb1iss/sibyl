"""Raw memory API routes."""

from __future__ import annotations

import time
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request

from sibyl.api.decorators import handle_workflow_errors
from sibyl.api.raw_capture_events import publish_raw_capture_changed
from sibyl.api.schemas import (
    MemoryAuditEventResponse,
    MemoryAuditListResponse,
    MemoryCitationRequest,
    MemoryCitationResponse,
    MemoryCorrectionRequest,
    MemoryCorrectionResponse,
    MemoryDerivedRecordResponse,
    MemoryScopeInputResponse,
    MemoryScopeLiteral,
    MemorySharePreviewRequest,
    MemorySharePreviewResponse,
    MemoryShareRequest,
    MemoryShareResponse,
    MemorySourceInspectResponse,
    MemorySpaceAccessPreviewRequest,
    MemorySpaceAccessPreviewResponse,
    MemorySpaceCreateRequest,
    MemorySpaceListResponse,
    MemorySpaceMemberCreateRequest,
    MemorySpaceMemberResponse,
    MemorySpaceResponse,
    MemorySpaceStateLiteral,
    MemorySpaceUpdateRequest,
    RawMemoryRecallRequest,
    RawMemoryRecallResponse,
    RawMemoryRememberRequest,
    RawMemoryResponse,
    ReflectionAutonomyRequest,
    ReflectionAutonomyResponse,
    ReflectionPromotionPreviewResponse,
    ReflectionPromotionRequest,
    ReflectionPromotionResponse,
    ReflectionReviewDrainItem,
    ReflectionReviewDrainRequest,
    ReflectionReviewDrainResponse,
    SourceImportStatusResponse,
)
from sibyl.auth.api_key_common import api_key_memory_scope_key
from sibyl.auth.authorization import verify_entity_project_access
from sibyl.auth.context import AuthContext
from sibyl.auth.dependencies import get_auth_context, get_current_organization, require_org_role
from sibyl.jobs.source_imports import get_source_import_status
from sibyl.persistence.auth_runtime import (
    add_memory_space_member,
    create_memory_space,
    get_memory_space,
    list_accessible_project_graph_ids,
    list_accessible_team_scope_keys,
    list_memory_audit_events,
    list_memory_space_members,
    list_memory_spaces,
    log_memory_audit_event,
    update_memory_space,
)
from sibyl.services.recall_limits import (
    RecallConcurrencyLimitExceededError,
    recall_concurrency_slot,
)
from sibyl_core.auth import AuthOrganization, MemoryPolicyContext, OrganizationRole, ProjectRole
from sibyl_core.auth.memory_policy import (
    MemoryPolicyAction,
    MemoryPolicyDecision,
    authorize_memory_read,
    authorize_memory_write,
)
from sibyl_core.models.reflection import (
    claim_records_from_metadata,
    memory_lifecycle_from_metadata,
    reflection_findings_from_metadata,
)
from sibyl_core.observability import elapsed_ms, telemetry_registry
from sibyl_core.services.memory import (
    MemoryAccessPreview,
    MemoryCorrectionPreview,
    MemoryCorrectionResult,
    MemorySharePreview,
    MemoryShareResult,
    ReflectionPromotionPreview,
    ReflectionPromotionResult,
    apply_memory_correction,
    preview_memory_access,
    preview_memory_correction,
    preview_memory_share,
    preview_raw_memory_promotion,
    preview_reflection_candidate_promotion,
    promote_raw_memory,
    promote_reflection_candidate_review,
    share_memory,
)
from sibyl_core.services.memory_autonomy import (
    ReflectionAutonomyDecision,
    ReflectionAutonomyOutcome,
    ReflectionAutonomyPolicy,
    decide_reflection_candidate_autonomy,
)
from sibyl_core.services.surreal_content import (
    AGENT_DIARY_CAPTURE_SURFACE,
    MemoryScope,
    RawMemory,
    RawMemoryRecallResult,
    get_raw_memory,
    get_raw_memory_by_source_id,
    list_reflection_candidate_reviews,
    recall_raw_memory_with_sources as recall_raw_memory,
    remember_raw_memory,
    save_raw_memory,
)

log = structlog.get_logger()

_READ_ROLES = (
    OrganizationRole.OWNER,
    OrganizationRole.ADMIN,
    OrganizationRole.MEMBER,
    OrganizationRole.VIEWER,
)
_WRITE_ROLES = (
    OrganizationRole.OWNER,
    OrganizationRole.ADMIN,
    OrganizationRole.MEMBER,
)
_ADMIN_ROLES = (OrganizationRole.OWNER, OrganizationRole.ADMIN)
_ARCHIVEABLE_REFLECTION_EXCEPTION_REASONS = frozenset(
    {
        "duplicate_candidate",
        "stale_candidate",
    }
)

router = APIRouter(prefix="/memory", tags=["memory"])
_REQUEST_AUTO_INJECT_SENTINEL: Request = cast("Request", None)


def _policy_http_status(reason: str) -> int:
    if reason == "missing_scope_key":
        return 400
    if reason == "principal_mismatch":
        return 401
    return 403


def _log_policy_decision(
    *,
    ctx: AuthContext,
    decision: MemoryPolicyDecision,
    surface: str,
) -> None:
    log.info(
        "memory_policy_decision",
        action=decision.action.value,
        allowed=decision.allowed,
        memory_scope=decision.memory_scope.value,
        organization_id=ctx.organization_id,
        policy_reason=decision.reason,
        principal_id=ctx.user_id,
        scope_key=decision.scope_key,
        surface=surface,
    )


async def _log_memory_audit(
    *,
    action: str,
    ctx: AuthContext,
    request: Request | None = None,
    memory_scope: str | None,
    scope_key: str | None,
    source_surface: str,
    policy_allowed: bool | None,
    policy_reason: str | None,
    project_id: str | None = None,
    source_ids: list[str] | None = None,
    derived_ids: list[str] | None = None,
    details: dict[str, object] | None = None,
) -> str | None:
    try:
        return await log_memory_audit_event(
            action=action,
            user_id=ctx.user_id,
            organization_id=ctx.organization_id,
            request=request,
            memory_scope=memory_scope,
            scope_key=scope_key,
            project_id=project_id,
            source_surface=source_surface,
            source_ids=source_ids,
            derived_ids=derived_ids,
            policy_allowed=policy_allowed,
            policy_reason=policy_reason,
            details=details,
        )
    except Exception as exc:
        log.warning("memory_audit_event_failed", action=action, error=str(exc), exc_info=True)
        return None


async def _project_accessible_for_policy(
    *,
    ctx: AuthContext,
    memory_scope: str,
    scope_key: str | None,
) -> set[str] | None:
    if memory_scope != "project" or not scope_key:
        return None
    accessible_projects = await list_accessible_project_graph_ids(ctx)
    return {str(project_id) for project_id in accessible_projects or set()}


async def _team_accessible_for_policy(
    *,
    ctx: AuthContext,
    memory_scope: str,
    scope_key: str | None,
) -> set[str] | None:
    if memory_scope != "team" or not scope_key:
        return None
    accessible_teams = await list_accessible_team_scope_keys(ctx)
    return {str(team_id) for team_id in accessible_teams or set()}


async def _authorize_project_scope_write(
    *,
    ctx: AuthContext,
    memory_scope: str,
    scope_key: str | None,
) -> None:
    if memory_scope != "project" or not scope_key:
        return
    await verify_entity_project_access(
        None,
        ctx,
        scope_key,
        required_role=ProjectRole.CONTRIBUTOR,
        require_existing_project=True,
    )


def _api_key_memory_scope_allowed(
    ctx: AuthContext,
    *,
    memory_scope: str,
    scope_key: str | None,
) -> bool:
    allowed_scope_keys = getattr(ctx, "api_key_memory_scope_keys", None)
    if allowed_scope_keys is None:
        return True
    if not isinstance(allowed_scope_keys, list | tuple | set | frozenset):
        return True
    effective_scope_key = ctx.user_id if memory_scope == "private" and not scope_key else scope_key
    scope_key_id = api_key_memory_scope_key(memory_scope, effective_scope_key)
    return scope_key_id in allowed_scope_keys


def _api_key_memory_scope_denial(
    *,
    action: MemoryPolicyAction,
    memory_scope: str,
    scope_key: str | None,
    policy_context: MemoryPolicyContext,
) -> MemoryPolicyDecision:
    try:
        normalized_scope = MemoryScope(memory_scope)
    except ValueError:
        normalized_scope = MemoryScope.PRIVATE
    return MemoryPolicyDecision(
        action=action,
        allowed=False,
        reason="api_key_memory_space_denied",
        memory_scope=normalized_scope,
        scope_key=scope_key,
        policy_context=policy_context,
    )


async def _authorize_memory_policy(
    *,
    ctx: AuthContext,
    action: MemoryPolicyAction,
    memory_scope: str,
    scope_key: str | None,
    surface: str,
    request: Request | None = None,
    agent_id: str | None = None,
    project_id: str | None = None,
) -> MemoryPolicyDecision:
    accessible_projects = await _project_accessible_for_policy(
        ctx=ctx,
        memory_scope=memory_scope,
        scope_key=scope_key,
    )
    accessible_teams = await _team_accessible_for_policy(
        ctx=ctx,
        memory_scope=memory_scope,
        scope_key=scope_key,
    )
    policy_context = MemoryPolicyContext(
        actor_user_id=ctx.user_id,
        organization_id=ctx.organization_id,
        organization_role=ctx.org_role,
        memory_space=memory_scope,
        scope_key=scope_key,
        project_id=project_id,
        accessible_projects=accessible_projects,
        accessible_teams=accessible_teams,
        agent_id=agent_id,
        source_surface=surface,
    )
    if action is MemoryPolicyAction.READ:
        decision = authorize_memory_read(
            policy_context=policy_context,
        )
    elif action is MemoryPolicyAction.WRITE:
        decision = authorize_memory_write(
            policy_context=policy_context,
        )
    else:
        msg = f"Unsupported raw memory policy action: {action.value}"
        raise ValueError(msg)

    _log_policy_decision(ctx=ctx, decision=decision, surface=surface)
    if not decision.allowed:
        await _log_memory_audit(
            action="memory.policy_deny",
            ctx=ctx,
            request=request,
            memory_scope=memory_scope,
            scope_key=scope_key,
            project_id=project_id,
            source_surface=surface,
            policy_allowed=False,
            policy_reason=decision.reason,
            details={"policy_action": decision.action.value},
        )
        raise HTTPException(
            status_code=_policy_http_status(decision.reason),
            detail=decision.reason,
        )
    if not _api_key_memory_scope_allowed(ctx, memory_scope=memory_scope, scope_key=scope_key):
        deny_decision = _api_key_memory_scope_denial(
            action=action,
            memory_scope=memory_scope,
            scope_key=scope_key,
            policy_context=policy_context,
        )
        _log_policy_decision(ctx=ctx, decision=deny_decision, surface=surface)
        await _log_memory_audit(
            action="memory.policy_deny",
            ctx=ctx,
            request=request,
            memory_scope=memory_scope,
            scope_key=scope_key,
            project_id=project_id,
            source_surface=surface,
            policy_allowed=False,
            policy_reason=deny_decision.reason,
            details={"policy_action": deny_decision.action.value},
        )
        raise HTTPException(
            status_code=_policy_http_status(deny_decision.reason),
            detail=deny_decision.reason,
        )
    return decision


async def _authorize_project_filter(
    *,
    ctx: AuthContext,
    project_id: str | None,
    required_project_role: ProjectRole,
    surface: str,
    memory_scope: str | None,
    scope_key: str | None,
    policy_action: str,
    request: Request | None = None,
) -> None:
    if not project_id:
        return
    try:
        await verify_entity_project_access(
            None,
            ctx,
            project_id,
            required_role=required_project_role,
            require_existing_project=True,
        )
    except HTTPException as exc:
        await _log_memory_audit(
            action="memory.policy_deny",
            ctx=ctx,
            request=request,
            memory_scope=memory_scope,
            scope_key=scope_key,
            project_id=project_id,
            source_surface=surface,
            policy_allowed=False,
            policy_reason=str(exc.detail),
            details={
                "policy_action": policy_action,
                "required_project_role": required_project_role.value,
            },
        )
        raise


def _diary_metadata(
    *,
    metadata: dict[str, object],
    diary: bool,
    agent_id: str | None,
    project_id: str | None,
) -> dict[str, object]:
    if not diary:
        return dict(metadata)
    if not agent_id:
        raise HTTPException(status_code=400, detail="agent_id is required for diary memory")
    out = dict(metadata)
    out["agent_id"] = agent_id
    out["memory_kind"] = "agent_diary"
    if project_id:
        out["project_id"] = project_id
    return out


def _validate_diary_request(*, diary: bool, agent_id: str | None, memory_scope: str) -> None:
    if not diary:
        return
    if memory_scope != "private":
        raise HTTPException(status_code=400, detail="diary memory must use private scope")
    if not agent_id:
        raise HTTPException(status_code=400, detail="agent_id is required for diary memory")


def _raw_recall_audit_details(
    request: RawMemoryRecallRequest,
    *,
    result_count: int,
) -> dict[str, object]:
    details: dict[str, object] = {
        "agent_id": request.agent_id,
        "diary": request.diary,
        "limit": request.limit,
        "result_count": result_count,
    }
    if request.participants:
        details["participants"] = list(request.participants)
    if request.labels:
        details["labels"] = list(request.labels)
    if request.thread_id:
        details["thread_id"] = request.thread_id
    if request.occurred_after:
        details["occurred_after"] = request.occurred_after.isoformat()
    if request.occurred_before:
        details["occurred_before"] = request.occurred_before.isoformat()
    if request.as_of:
        details["as_of"] = request.as_of.isoformat()
    return details


def _raw_memory_response(
    memory: RawMemory,
    *,
    policy_reason: str | None = None,
) -> RawMemoryResponse:
    return RawMemoryResponse(
        id=memory.id,
        organization_id=memory.organization_id,
        source_id=memory.source_id,
        principal_id=memory.principal_id,
        memory_scope=memory.memory_scope.value,
        scope_key=memory.scope_key,
        title=memory.title,
        raw_content=memory.raw_content,
        tags=memory.tags,
        metadata=memory.metadata,
        provenance=memory.provenance,
        capture_surface=memory.capture_surface,
        captured_at=memory.captured_at,
        created_at=memory.created_at,
        score=memory.score,
        snippet=memory.snippet,
        policy_reason=policy_reason,
    )


def _actor_user_uuid(ctx: AuthContext) -> UUID:
    if not ctx.user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        return UUID(str(ctx.user_id))
    except ValueError as exc:
        raise HTTPException(status_code=401, detail="invalid_actor") from exc


def _memory_space_member_response(member: Any) -> MemorySpaceMemberResponse:
    return MemorySpaceMemberResponse(
        id=str(member.id),
        organization_id=str(member.organization_id),
        space_id=str(member.space_id),
        principal_type=str(member.principal_type),
        principal_id=str(member.principal_id),
        role=str(member.role),
        permissions=list(getattr(member, "permissions", [])),
        expires_at=getattr(member, "expires_at", None),
        created_by_user_id=str(member.created_by_user_id),
        created_at=getattr(member, "created_at", None),
        updated_at=getattr(member, "updated_at", None),
    )


def _memory_space_response(
    space: Any,
    *,
    members: list[Any] | None = None,
) -> MemorySpaceResponse:
    return MemorySpaceResponse(
        id=str(space.id),
        organization_id=str(space.organization_id),
        memory_scope=cast("MemoryScopeLiteral", str(space.memory_scope)),
        scope_key=getattr(space, "scope_key", None),
        name=str(space.name),
        description=getattr(space, "description", None),
        state=cast("MemorySpaceStateLiteral", str(space.state)),
        disabled_reason=getattr(space, "disabled_reason", None),
        metadata=dict(getattr(space, "metadata", {}) or {}),
        created_by_user_id=str(space.created_by_user_id),
        created_at=getattr(space, "created_at", None),
        updated_at=getattr(space, "updated_at", None),
        members=[_memory_space_member_response(member) for member in members or []],
    )


def _promotion_response(result: ReflectionPromotionResult) -> ReflectionPromotionResponse:
    metadata = dict(result.metadata or {})
    return ReflectionPromotionResponse(
        success=result.success,
        candidate_id=result.candidate_id,
        promoted_id=result.promoted_id,
        reason=result.reason,
        review_state=result.review_state,
        memory_scope=result.memory_scope.value if result.memory_scope else None,
        scope_key=result.scope_key,
        raw_source_ids=list(result.raw_source_ids),
        policy_reasons=_metadata_str_list(metadata.get("policy_reasons")),
        metadata=metadata,
    )


def _promotion_preview_response(
    result: ReflectionPromotionPreview,
) -> ReflectionPromotionPreviewResponse:
    metadata = dict(result.metadata or {})
    source_count = metadata.get("source_count")
    return ReflectionPromotionPreviewResponse(
        allowed=result.allowed,
        candidate_id=result.candidate_id,
        reason=result.reason,
        review_state=result.review_state,
        promote_to_scope=result.memory_scope.value if result.memory_scope else None,
        promote_to_scope_key=result.scope_key,
        raw_source_ids=list(result.raw_source_ids),
        policy_reasons=_metadata_str_list(metadata.get("policy_reasons")),
        input_scopes=[
            MemoryScopeInputResponse(
                id=str(item.get("id") or ""),
                memory_scope=cast(
                    "MemoryScopeLiteral",
                    str(item.get("memory_scope") or "private"),
                ),
                scope_key=str(item["scope_key"]) if item.get("scope_key") else None,
            )
            for item in _metadata_dict_list(metadata.get("input_scopes"))
        ],
        source_count=source_count if isinstance(source_count, int) else 0,
        metadata=metadata,
    )


def _autonomy_response(
    *,
    decision: ReflectionAutonomyDecision,
    preview: ReflectionPromotionPreview,
    promotion: ReflectionPromotionResult | None = None,
) -> ReflectionAutonomyResponse:
    promotion_response = _promotion_response(promotion) if promotion is not None else None
    promoted_id = promotion.promoted_id if promotion and promotion.success else None
    metadata = dict(decision.metadata or {})
    if promotion is not None:
        metadata["promotion_reason"] = promotion.reason
        metadata["promotion_success"] = promotion.success
    return ReflectionAutonomyResponse(
        outcome=decision.outcome.value,
        recommended_action=decision.recommended_action.value,
        applied=promotion is not None and promotion.success,
        dry_run=decision.dry_run,
        candidate_id=decision.candidate_id,
        reason=decision.reason,
        review_state=promotion.review_state if promotion else decision.review_state,
        promote_to_scope=decision.memory_scope.value if decision.memory_scope else None,
        promote_to_scope_key=decision.scope_key,
        promoted_id=promoted_id,
        raw_source_ids=list(decision.raw_source_ids),
        policy_reasons=list(decision.policy_reasons),
        exception_reasons=list(decision.exception_reasons),
        confidence=decision.confidence,
        confidence_threshold=decision.confidence_threshold,
        preview=_promotion_preview_response(preview),
        promotion=promotion_response,
        metadata=metadata,
    )


def _drain_item_from_autonomy(
    response: ReflectionAutonomyResponse,
    *,
    archived: bool = False,
    review_state: str | None = None,
) -> ReflectionReviewDrainItem:
    return ReflectionReviewDrainItem(
        candidate_id=response.candidate_id,
        outcome=response.outcome,
        recommended_action=response.recommended_action,
        applied=response.applied,
        archived=archived,
        dry_run=response.dry_run,
        reason=response.reason,
        review_state=review_state or response.review_state,
        promoted_id=response.promoted_id,
        raw_source_ids=list(response.raw_source_ids),
        policy_reasons=list(response.policy_reasons),
        exception_reasons=list(response.exception_reasons),
        confidence=response.confidence,
    )


def _drain_error_item(
    candidate_id: str,
    *,
    error: object,
    dry_run: bool,
) -> ReflectionReviewDrainItem:
    return ReflectionReviewDrainItem(
        candidate_id=candidate_id,
        outcome="error",
        recommended_action="error",
        dry_run=dry_run,
        reason="review_failed",
        review_state="unknown",
        error=str(error),
    )


def _drain_response(
    *,
    request: ReflectionReviewDrainRequest,
    results: list[ReflectionReviewDrainItem],
) -> ReflectionReviewDrainResponse:
    effective_archive_reasons = sorted(
        {
            reason
            for reason in request.archive_exception_reasons
            if reason in _ARCHIVEABLE_REFLECTION_EXCEPTION_REASONS
        }
    )
    return ReflectionReviewDrainResponse(
        dry_run=request.dry_run,
        limit=request.limit,
        scanned_count=len(results),
        auto_promote_count=sum(1 for item in results if item.outcome == "auto_promote"),
        applied_count=sum(1 for item in results if item.applied),
        archived_count=sum(1 for item in results if item.archived),
        exception_count=sum(1 for item in results if item.outcome == "exception"),
        skip_count=sum(1 for item in results if item.outcome == "skip"),
        failed_count=sum(1 for item in results if item.outcome == "error"),
        results=results,
        metadata={
            "archive_exceptions": request.archive_exceptions,
            "archive_exception_reasons": effective_archive_reasons,
            "requested_archive_exception_reasons": list(request.archive_exception_reasons),
        },
    )


def _should_archive_reflection_exception(
    response: ReflectionAutonomyResponse,
    *,
    archive_reasons: set[str],
) -> bool:
    if response.outcome != "exception":
        return False
    exception_reasons = {str(reason) for reason in response.exception_reasons if str(reason)}
    if not exception_reasons:
        return False
    return bool(exception_reasons & archive_reasons) and exception_reasons <= archive_reasons


async def _archive_reflection_exception_candidate(
    *,
    response: ReflectionAutonomyResponse,
    org: AuthOrganization,
    ctx: AuthContext,
    request: Request | None,
    project_id: str | None,
) -> RawMemory | None:
    memory = await get_raw_memory(
        organization_id=str(org.id),
        memory_id=response.candidate_id,
    )
    if memory is None:
        return None
    archived_at = datetime.now(UTC).isoformat()
    metadata = {
        **memory.metadata,
        "review_state": "archived",
        "archived_at": archived_at,
        "archive_reason": response.reason,
        "archive_reasons": list(response.exception_reasons),
        "autonomy_outcome": response.outcome,
        "autonomy_recommended_action": response.recommended_action,
    }
    updated = await save_raw_memory(
        replace(
            memory,
            review_state="archived",
            metadata=metadata,
        )
    )
    await _log_memory_audit(
        action="memory.reflect.auto_archive",
        ctx=ctx,
        request=request,
        memory_scope=response.promote_to_scope,
        scope_key=response.promote_to_scope_key,
        project_id=project_id,
        source_surface="reflection_auto_review",
        source_ids=[response.candidate_id, *response.raw_source_ids],
        derived_ids=[],
        policy_allowed=response.preview.allowed,
        policy_reason=response.reason,
        details={
            "archive_reasons": list(response.exception_reasons),
            "dry_run": False,
            "outcome": response.outcome,
            "recommended_action": response.recommended_action,
            "review_state": "archived",
        },
    )
    return updated


def _share_preview_response(result: MemorySharePreview) -> MemorySharePreviewResponse:
    metadata = dict(result.metadata or {})
    return MemorySharePreviewResponse(
        allowed=result.allowed,
        reason=result.reason,
        target_scope=result.target_scope.value if result.target_scope else None,
        target_scope_key=result.target_scope_key,
        source_ids=list(result.source_ids),
        visible_source_ids=list(result.visible_source_ids),
        denied_source_ids=list(result.denied_source_ids),
        missing_source_ids=list(result.missing_source_ids),
        redacted_count=result.redacted_count,
        hidden_but_relevant_count=result.hidden_but_relevant_count,
        policy_reasons=_metadata_str_list(metadata.get("policy_reasons")),
        input_scopes=[
            MemoryScopeInputResponse(
                id=str(item.get("id") or ""),
                memory_scope=cast(
                    "MemoryScopeLiteral",
                    str(item.get("memory_scope") or "private"),
                ),
                scope_key=str(item["scope_key"]) if item.get("scope_key") else None,
            )
            for item in _metadata_dict_list(metadata.get("input_scopes"))
        ],
        metadata=metadata,
    )


def _share_response(
    result: MemoryShareResult,
    *,
    audit_event_ids: list[str],
) -> MemoryShareResponse:
    preview = _share_preview_response(result.preview)
    promotions = [_promotion_response(promotion) for promotion in result.promotions]
    promoted_ids = [
        str(promotion.promoted_id)
        for promotion in result.promotions
        if promotion.success and promotion.promoted_id
    ]
    metadata = {
        **dict(result.metadata or {}),
        "audit_event_ids": list(audit_event_ids),
    }
    return MemoryShareResponse(
        applied=result.applied,
        reason=result.reason,
        target_scope=preview.target_scope,
        target_scope_key=preview.target_scope_key,
        source_ids=list(preview.source_ids),
        visible_source_ids=list(preview.visible_source_ids),
        denied_source_ids=list(preview.denied_source_ids),
        missing_source_ids=list(preview.missing_source_ids),
        promoted_ids=promoted_ids,
        audit_event_ids=list(audit_event_ids),
        preview=preview,
        promotions=promotions,
        metadata=metadata,
    )


def _access_preview_response(result: MemoryAccessPreview) -> MemorySpaceAccessPreviewResponse:
    metadata = dict(result.metadata or {})
    return MemorySpaceAccessPreviewResponse(
        allowed=result.allowed,
        reason=result.reason,
        target_principal_type=result.target_principal_type,
        target_principal_id=result.target_principal_id,
        memory_space_ids=list(result.memory_space_ids),
        visible_source_ids=list(result.visible_source_ids),
        denied_source_ids=list(result.denied_source_ids),
        missing_source_ids=list(result.missing_source_ids),
        redacted_count=result.redacted_count,
        hidden_but_relevant_count=result.hidden_but_relevant_count,
        policy_reasons=[decision.reason for decision in result.policy_decisions]
        or _metadata_str_list(metadata.get("policy_reasons")),
        metadata=metadata,
    )


def _correction_response(
    preview: MemoryCorrectionPreview,
    *,
    applied: bool = False,
    updated_memory: RawMemory | None = None,
) -> MemoryCorrectionResponse:
    metadata = dict(preview.metadata or {})
    lifecycle: dict[str, Any] = {}
    reflection_finding: dict[str, Any] | None = None
    if updated_memory is not None:
        lifecycle = memory_lifecycle_from_metadata(
            updated_memory.metadata,
            source_id=updated_memory.id,
            review_state=updated_memory.review_state,
        ).to_dict()
        findings = [
            finding.to_dict()
            for finding in reflection_findings_from_metadata(updated_memory.metadata)
        ]
        reflection_finding = findings[-1] if findings else None
    return MemoryCorrectionResponse(
        allowed=preview.allowed,
        applied=applied,
        source_id=preview.source_id,
        action=preview.action,
        reason=preview.reason,
        target_lifecycle_state=preview.target_lifecycle_state,
        target_lifecycle_flags=preview.target_lifecycle_flags,
        updated_review_state=updated_memory.review_state if updated_memory else None,
        lifecycle=lifecycle,
        reflection_finding=reflection_finding,
        affected_source_ids=list(preview.affected_source_ids),
        affected_derived_ids=list(preview.affected_derived_ids),
        reversible=preview.reversible,
        recall_impact=dict(preview.recall_impact),
        synthesis_impact=dict(preview.synthesis_impact),
        audit_action=preview.audit_action,
        policy_reasons=[decision.reason for decision in preview.policy_decisions]
        or _metadata_str_list(metadata.get("policy_reasons")),
        metadata=metadata,
    )


def _correction_result_response(result: MemoryCorrectionResult) -> MemoryCorrectionResponse:
    return _correction_response(
        result.preview,
        applied=result.applied,
        updated_memory=result.updated_memory,
    )


def _metadata_str_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item)]


def _metadata_dict_list(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    return [{str(key): item[key] for key in item} for item in value if isinstance(item, dict)]


def _str_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def _audit_event_response(row: dict[str, object]) -> MemoryAuditEventResponse:
    details = row.get("details") if isinstance(row.get("details"), dict) else {}
    payload = (
        {str(key): value for key, value in details.items()} if isinstance(details, dict) else {}
    )
    return MemoryAuditEventResponse(
        id=str(row.get("uuid") or ""),
        organization_id=str(row["organization_id"]) if row.get("organization_id") else None,
        user_id=str(row["user_id"]) if row.get("user_id") else None,
        action=str(row.get("action") or ""),
        memory_scope=payload.get("memory_scope")
        if isinstance(payload.get("memory_scope"), str)
        else None,
        scope_key=payload.get("scope_key") if isinstance(payload.get("scope_key"), str) else None,
        project_id=payload.get("project_id")
        if isinstance(payload.get("project_id"), str)
        else None,
        source_surface=payload.get("source_surface")
        if isinstance(payload.get("source_surface"), str)
        else None,
        source_ids=_str_list(payload.get("source_ids")),
        source_ids_truncated=payload.get("source_ids_truncated")
        if isinstance(payload.get("source_ids_truncated"), int)
        else None,
        derived_ids=_str_list(payload.get("derived_ids")),
        derived_ids_truncated=payload.get("derived_ids_truncated")
        if isinstance(payload.get("derived_ids_truncated"), int)
        else None,
        policy_allowed=payload.get("policy_allowed")
        if isinstance(payload.get("policy_allowed"), bool)
        else None,
        policy_reason=payload.get("policy_reason")
        if isinstance(payload.get("policy_reason"), str)
        else None,
        details=payload.get("details") if isinstance(payload.get("details"), dict) else {},
        created_at=row.get("created_at") if isinstance(row.get("created_at"), datetime) else None,
    )


def _memory_metadata_str(memory: RawMemory, key: str) -> str | None:
    value = memory.metadata.get(key)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _memory_project_id(memory: RawMemory) -> str | None:
    return memory.project_id or _memory_metadata_str(memory, "project_id")


def _memory_lifecycle_state(memory: RawMemory) -> str:
    return str(
        memory_lifecycle_from_metadata(
            memory.metadata,
            source_id=memory.id,
            review_state=memory.review_state,
        ).state
    )


def _memory_lifecycle_redacts_content(memory: RawMemory) -> bool:
    lifecycle = memory_lifecycle_from_metadata(
        memory.metadata,
        source_id=memory.id,
        review_state=memory.review_state,
    )
    return str(lifecycle.state).lower() == "deleted" or "redacted" in lifecycle.flags


async def _load_memory_source_for_org(
    *,
    organization_id: str,
    source_id: str,
) -> RawMemory:
    memory = await get_raw_memory(organization_id=organization_id, memory_id=source_id)
    if memory is None:
        memory = await get_raw_memory_by_source_id(
            organization_id=organization_id,
            source_id=source_id,
        )
    if memory is None:
        raise HTTPException(status_code=404, detail="memory_source_not_found")
    return memory


async def _inspect_content_policy(
    *,
    ctx: AuthContext,
    memory: RawMemory,
) -> MemoryPolicyDecision:
    project_id = _memory_project_id(memory)
    accessible_projects = await _project_accessible_for_policy(
        ctx=ctx,
        memory_scope=memory.memory_scope.value,
        scope_key=memory.scope_key,
    )
    accessible_teams = await _team_accessible_for_policy(
        ctx=ctx,
        memory_scope=memory.memory_scope.value,
        scope_key=memory.scope_key,
    )
    policy_context = MemoryPolicyContext(
        actor_user_id=ctx.user_id,
        organization_id=ctx.organization_id,
        organization_role=ctx.org_role,
        memory_space=memory.memory_scope.value,
        scope_key=memory.scope_key,
        project_id=project_id,
        accessible_projects=accessible_projects,
        accessible_teams=accessible_teams,
        agent_id=memory.agent_id,
        source_surface="memory_inspect",
    )
    decision = authorize_memory_read(policy_context=policy_context)
    if memory.memory_scope.value == "private" and memory.principal_id != ctx.user_id:
        decision = MemoryPolicyDecision(
            action=MemoryPolicyAction.READ,
            allowed=False,
            reason="principal_mismatch",
            memory_scope=memory.memory_scope,
            scope_key=memory.scope_key,
            policy_context=policy_context,
        )
    _log_policy_decision(ctx=ctx, decision=decision, surface="memory_inspect")
    return decision


def _dedupe_audit_rows(rows: list[dict[str, object]], *, limit: int) -> list[dict[str, object]]:
    seen: set[str] = set()
    deduped: list[dict[str, object]] = []
    for row in rows:
        key = str(row.get("uuid") or row.get("id") or id(row))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(row)
    fallback = datetime.min.replace(tzinfo=UTC)
    deduped.sort(
        key=lambda row: (
            row.get("created_at") if isinstance(row.get("created_at"), datetime) else fallback
        ),
        reverse=True,
    )
    return deduped[:limit]


async def _source_audit_events(
    *,
    organization_id: str,
    source_id: str,
    memory: RawMemory,
    limit: int = 20,
) -> list[MemoryAuditEventResponse]:
    rows: list[dict[str, object]] = []
    source_filters = list(dict.fromkeys([source_id, memory.id, memory.source_id]))
    for value in source_filters:
        rows.extend(
            await list_memory_audit_events(
                organization_id=organization_id,
                source_id=value,
                limit=limit,
            )
        )
    rows.extend(
        await list_memory_audit_events(
            organization_id=organization_id,
            derived_id=memory.id,
            limit=limit,
        )
    )
    return [_audit_event_response(row) for row in _dedupe_audit_rows(rows, limit=limit)]


def _derived_record_type(
    *,
    source_action: str,
    derived_id: str,
    memory: RawMemory,
) -> str:
    if derived_id == memory.id:
        return "raw_memory"
    if "promote" in source_action:
        return "graph_entity"
    if "reflect" in source_action:
        return "reflection"
    if "context" in source_action:
        return "context_render"
    return source_action.removeprefix("memory.").replace(".", "_") or "memory_record"


def _derived_records_from_audit(
    *,
    events: list[MemoryAuditEventResponse],
    memory: RawMemory,
) -> list[MemoryDerivedRecordResponse]:
    records: dict[str, MemoryDerivedRecordResponse] = {}
    for event in events:
        for derived_id in event.derived_ids:
            if derived_id in records:
                continue
            records[derived_id] = MemoryDerivedRecordResponse(
                id=derived_id,
                record_type=_derived_record_type(
                    source_action=event.action,
                    derived_id=derived_id,
                    memory=memory,
                ),
                source_action=event.action,
            )
    return list(records.values())


def _audit_events_for_visibility(
    events: list[MemoryAuditEventResponse],
    *,
    content_visible: bool,
) -> list[MemoryAuditEventResponse]:
    if content_visible:
        return events
    return [event.model_copy(update={"details": {}}) for event in events]


def _metadata_dicts(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


def _correction_history(
    *,
    memory: RawMemory,
    audit_events: list[MemoryAuditEventResponse],
) -> list[dict[str, Any]]:
    history = _metadata_dicts(memory.metadata.get("correction_history"))
    for event in audit_events:
        if not (
            event.action.startswith("memory.correction")
            or event.action
            in {
                "memory.hide",
                "memory.redact",
                "memory.restore",
                "memory.delete",
            }
        ):
            continue
        history.append(
            {
                "audit_event_id": event.id,
                "action": event.action,
                "policy_reason": event.policy_reason,
                "derived_ids": list(event.derived_ids),
                "created_at": event.created_at,
            }
        )
    return history


def _promotion_state(
    *,
    memory: RawMemory,
    audit_events: list[MemoryAuditEventResponse],
) -> dict[str, Any]:
    promotion_events = [event.id for event in audit_events if "promote" in event.action]
    promoted_id = _memory_metadata_str(memory, "promoted_entity_id")
    state = "promoted" if memory.review_state == "promoted" or promoted_id else "not_promoted"
    return {
        "state": state,
        "promoted_id": promoted_id,
        "promoted_at": _memory_metadata_str(memory, "promoted_at"),
        "audit_event_ids": promotion_events,
    }


def _share_state(audit_events: list[MemoryAuditEventResponse]) -> dict[str, Any]:
    share_events = [event.id for event in audit_events if "share" in event.action]
    return {
        "state": "previewed" if share_events else "none",
        "audit_event_ids": share_events,
    }


def _transform_versions(metadata: dict[str, object]) -> dict[str, Any]:
    keys = (
        "adapter_version",
        "embedding_model",
        "embedding_model_version",
        "extraction_version",
        "schema_version",
        "source_adapter_version",
        "transform_version",
    )
    return {key: metadata[key] for key in keys if key in metadata}


def _available_source_actions(
    *,
    memory: RawMemory,
    policy_decision: MemoryPolicyDecision,
) -> list[dict[str, Any]]:
    visible = policy_decision.allowed
    lifecycle_open = memory.review_state not in {"archived", "promoted"}
    return [
        {"action": "inspect", "available": True, "preview_required": False},
        {
            "action": "promotion.preview",
            "available": visible and lifecycle_open,
            "preview_required": True,
        },
        {
            "action": "share.preview",
            "available": visible,
            "preview_required": True,
        },
        {
            "action": "correction.preview",
            "available": visible,
            "preview_required": True,
            "reason": None if visible else policy_decision.reason,
        },
    ]


def _memory_source_inspect_response(
    *,
    memory: RawMemory,
    policy_decision: MemoryPolicyDecision,
    audit_events: list[MemoryAuditEventResponse],
) -> MemorySourceInspectResponse:
    content_redacted = not policy_decision.allowed or _memory_lifecycle_redacts_content(memory)
    metadata = dict(memory.metadata)
    if content_redacted:
        metadata.pop("memory_lifecycle", None)
        metadata.pop("reflection_findings", None)
        metadata.pop("claim_records", None)
    visible_audit_events = _audit_events_for_visibility(
        audit_events,
        content_visible=policy_decision.allowed,
    )
    derived_records = _derived_records_from_audit(events=visible_audit_events, memory=memory)
    derived_ids = [record.id for record in derived_records]
    derived_types = list(dict.fromkeys(record.record_type for record in derived_records))
    project_id = _memory_project_id(memory)
    lifecycle = (
        {}
        if content_redacted
        else memory_lifecycle_from_metadata(
            memory.metadata,
            source_id=memory.id,
            review_state=memory.review_state,
        ).to_dict()
    )
    reflection_findings = (
        []
        if content_redacted
        else [finding.to_dict() for finding in reflection_findings_from_metadata(memory.metadata)]
    )
    claim_records = (
        []
        if content_redacted
        else [claim.to_dict() for claim in claim_records_from_metadata(memory.metadata)]
    )
    return MemorySourceInspectResponse(
        id=memory.id,
        organization_id=memory.organization_id,
        source_id=memory.source_id,
        principal_id=memory.principal_id,
        agent_id=memory.agent_id,
        project_id=project_id,
        memory_scope=memory.memory_scope.value,
        scope_key=memory.scope_key,
        review_state=memory.review_state,
        visibility={
            "content_visible": policy_decision.allowed,
            "content_redacted": content_redacted,
            "lifecycle_state": _memory_lifecycle_state(memory),
            "memory_scope": memory.memory_scope.value,
            "scope_key": memory.scope_key,
            "principal_id": memory.principal_id,
            "agent_id": memory.agent_id,
            "project_id": project_id,
            "policy_reason": policy_decision.reason,
        },
        lifecycle=lifecycle,
        reflection_findings=reflection_findings,
        claim_records=claim_records,
        correction_history=_correction_history(
            memory=memory,
            audit_events=visible_audit_events,
        ),
        promotion_state=_promotion_state(
            memory=memory,
            audit_events=visible_audit_events,
        ),
        share_state=_share_state(visible_audit_events),
        entity_type=memory.entity_type,
        title=memory.title,
        raw_content=None if content_redacted else memory.raw_content,
        content_redacted=content_redacted,
        raw_content_length=len(memory.raw_content),
        tags=memory.tags,
        metadata=metadata,
        provenance=memory.provenance,
        capture_surface=memory.capture_surface,
        captured_at=memory.captured_at,
        created_at=memory.created_at,
        freshness_timestamps={
            "captured_at": memory.captured_at,
            "created_at": memory.created_at,
        },
        transform_versions=_transform_versions(memory.metadata),
        policy_allowed=policy_decision.allowed,
        policy_reason=policy_decision.reason,
        policy_metadata={
            "policy_action": policy_decision.action.value,
            "content_redacted": content_redacted,
            "source_surface": "memory_inspect",
        },
        derived_ids=derived_ids,
        derived_types=derived_types,
        derived_records=derived_records,
        recent_audit_events=visible_audit_events,
        audit_event_count=len(visible_audit_events),
        available_actions=_available_source_actions(
            memory=memory,
            policy_decision=policy_decision,
        ),
    )


def _validate_memory_audit_action(action: str | None) -> None:
    if action and not action.startswith("memory."):
        raise HTTPException(status_code=400, detail="invalid_memory_audit_action")


def _promotion_policy_allowed(result: ReflectionPromotionResult) -> bool | None:
    metadata = dict(result.metadata or {})
    raw_allowed = metadata.get("policy_allowed")
    if isinstance(raw_allowed, bool):
        return raw_allowed
    policy_reasons = metadata.get("policy_reasons")
    if isinstance(policy_reasons, list) and policy_reasons:
        return result.success
    if result.success:
        return True
    return None


async def _accessible_projects_for_promotion(
    *,
    ctx: AuthContext,
    request: ReflectionPromotionRequest,
    http_request: Request | None = None,
) -> set[str]:
    project_ids: set[str] = set()
    if request.project:
        project_ids.add(request.project)
    if request.promote_to_scope == "project":
        target_project = request.promote_to_scope_key or request.project
        if target_project:
            project_ids.add(target_project)

    for project_id in project_ids:
        await _authorize_project_filter(
            ctx=ctx,
            project_id=project_id,
            required_project_role=ProjectRole.CONTRIBUTOR,
            surface="reflection_promote",
            memory_scope=request.promote_to_scope,
            scope_key=request.promote_to_scope_key or request.project,
            policy_action="promote",
            request=http_request,
        )

    if project_ids:
        return project_ids
    accessible_projects = await list_accessible_project_graph_ids(ctx)
    return {str(project_id) for project_id in accessible_projects or set()}


def _promotion_target_scope(
    request: ReflectionPromotionRequest,
) -> tuple[str, str | None] | None:
    if request.promote_to_scope is None:
        return None
    try:
        target_scope = MemoryScope(request.promote_to_scope)
    except ValueError:
        return None
    target_scope_key = request.promote_to_scope_key
    if target_scope is MemoryScope.PROJECT:
        target_scope_key = target_scope_key or request.project
    return target_scope.value, target_scope_key


async def _authorize_raw_promotion_api_key_scopes(
    *,
    ctx: AuthContext,
    request: ReflectionPromotionRequest,
    organization_id: str,
    accessible_projects: set[str],
    http_request: Request | None,
    surface: str,
) -> None:
    allowed_scope_keys = getattr(ctx, "api_key_memory_scope_keys", None)
    if allowed_scope_keys is None or not isinstance(
        allowed_scope_keys, list | tuple | set | frozenset
    ):
        return

    memory = await get_raw_memory(
        organization_id=organization_id,
        memory_id=request.candidate_id,
    )
    if memory is None:
        return

    checks: tuple[tuple[MemoryPolicyAction, str, str | None, str | None], ...] = (
        (
            MemoryPolicyAction.READ,
            memory.memory_scope.value,
            memory.scope_key,
            _memory_project_id(memory),
        ),
    )
    target_scope = _promotion_target_scope(request)
    if target_scope is not None:
        checks = (
            *checks,
            (MemoryPolicyAction.WRITE, target_scope[0], target_scope[1], request.project),
        )

    for action, memory_scope, scope_key, project_id in checks:
        if _api_key_memory_scope_allowed(ctx, memory_scope=memory_scope, scope_key=scope_key):
            continue
        policy_context = MemoryPolicyContext(
            actor_user_id=ctx.user_id,
            organization_id=ctx.organization_id,
            organization_role=ctx.org_role,
            memory_space=memory_scope,
            scope_key=scope_key,
            project_id=project_id,
            accessible_projects=accessible_projects,
            source_surface=surface,
        )
        deny_decision = _api_key_memory_scope_denial(
            action=action,
            memory_scope=memory_scope,
            scope_key=scope_key,
            policy_context=policy_context,
        )
        _log_policy_decision(ctx=ctx, decision=deny_decision, surface=surface)
        await _log_memory_audit(
            action="memory.policy_deny",
            ctx=ctx,
            request=http_request,
            memory_scope=memory_scope,
            scope_key=scope_key,
            project_id=project_id,
            source_surface=surface,
            source_ids=[request.candidate_id],
            policy_allowed=False,
            policy_reason=deny_decision.reason,
            details={"policy_action": deny_decision.action.value},
        )
        raise HTTPException(
            status_code=_policy_http_status(deny_decision.reason),
            detail=deny_decision.reason,
        )


def _share_target_policy_scope(
    request: MemorySharePreviewRequest,
) -> tuple[str, str | None, str | None]:
    target_scope = str(request.target_scope)
    target_scope_key = request.target_scope_key
    project_id = request.project_id
    if target_scope == "project":
        target_scope_key = target_scope_key or request.project_id
        project_id = project_id or target_scope_key
    return target_scope, target_scope_key, project_id


async def _authorize_share_api_key_scopes(
    *,
    ctx: AuthContext,
    request: MemorySharePreviewRequest,
    organization_id: str,
    accessible_projects: set[str],
    accessible_teams: set[str] | None,
    http_request: Request | None,
    surface: str,
) -> None:
    allowed_scope_keys = getattr(ctx, "api_key_memory_scope_keys", None)
    if allowed_scope_keys is None or not isinstance(
        allowed_scope_keys, list | tuple | set | frozenset
    ):
        return

    target_scope, target_scope_key, target_project_id = _share_target_policy_scope(request)
    target_denied = not _api_key_memory_scope_allowed(
        ctx,
        memory_scope=target_scope,
        scope_key=target_scope_key,
    )
    checks: list[tuple[MemoryPolicyAction, str, str | None, str | None, str | None]] = (
        [
            (
                MemoryPolicyAction.WRITE,
                target_scope,
                target_scope_key,
                target_project_id,
                None,
            )
        ]
        if target_denied
        else []
    )
    if not target_denied:
        for source_id in request.source_ids:
            memory = await get_raw_memory(
                organization_id=organization_id,
                memory_id=source_id,
            )
            if memory is None:
                continue
            checks.append(
                (
                    MemoryPolicyAction.READ,
                    memory.memory_scope.value,
                    memory.scope_key,
                    _memory_project_id(memory),
                    source_id,
                )
            )

    for action, memory_scope, scope_key, project_id, source_id in checks:
        if _api_key_memory_scope_allowed(ctx, memory_scope=memory_scope, scope_key=scope_key):
            continue
        policy_context = MemoryPolicyContext(
            actor_user_id=ctx.user_id,
            organization_id=ctx.organization_id,
            organization_role=ctx.org_role,
            memory_space=memory_scope,
            scope_key=scope_key,
            project_id=project_id,
            accessible_projects=accessible_projects,
            accessible_teams=accessible_teams,
            source_surface=surface,
        )
        deny_decision = _api_key_memory_scope_denial(
            action=action,
            memory_scope=memory_scope,
            scope_key=scope_key,
            policy_context=policy_context,
        )
        _log_policy_decision(ctx=ctx, decision=deny_decision, surface=surface)
        await _log_memory_audit(
            action="memory.policy_deny",
            ctx=ctx,
            request=http_request,
            memory_scope=memory_scope,
            scope_key=scope_key,
            project_id=project_id,
            source_surface=surface,
            source_ids=[source_id] if source_id else list(request.source_ids),
            policy_allowed=False,
            policy_reason=deny_decision.reason,
            details={"policy_action": deny_decision.action.value},
        )
        raise HTTPException(
            status_code=_policy_http_status(deny_decision.reason),
            detail=deny_decision.reason,
        )


async def _accessible_projects_for_share_preview(
    *,
    ctx: AuthContext,
    request: MemorySharePreviewRequest,
    http_request: Request | None = None,
) -> set[str]:
    target_project = request.target_scope_key if request.target_scope == "project" else None
    project_ids = {project_id for project_id in (target_project, request.project_id) if project_id}
    for project_id in project_ids:
        await _authorize_project_filter(
            ctx=ctx,
            project_id=project_id,
            required_project_role=ProjectRole.CONTRIBUTOR,
            surface="memory_share_preview",
            memory_scope=request.target_scope,
            scope_key=request.target_scope_key,
            policy_action="share_preview",
            request=http_request,
        )

    accessible_projects = await list_accessible_project_graph_ids(ctx)
    projects = {str(project_id) for project_id in accessible_projects or set()}
    projects.update(project_ids)
    return projects


async def _accessible_teams_for_share(
    *,
    ctx: AuthContext,
) -> set[str] | None:
    accessible_teams = await list_accessible_team_scope_keys(ctx)
    return {str(team_id) for team_id in accessible_teams or set()}


@router.get(
    "/spaces",
    response_model=MemorySpaceListResponse,
    dependencies=[Depends(require_org_role(*_ADMIN_ROLES))],
)
async def list_memory_space_records(
    org: AuthOrganization = Depends(get_current_organization),
) -> MemorySpaceListResponse:
    """List persisted memory spaces for owner/admin inspection."""
    spaces = await list_memory_spaces(organization_id=org.id)
    return MemorySpaceListResponse(
        spaces=[_memory_space_response(space) for space in spaces],
    )


@router.post(
    "/spaces",
    response_model=MemorySpaceResponse,
    dependencies=[Depends(require_org_role(*_ADMIN_ROLES))],
)
async def create_memory_space_record(
    request: MemorySpaceCreateRequest,
    org: AuthOrganization = Depends(get_current_organization),
    ctx: AuthContext = Depends(get_auth_context),
) -> MemorySpaceResponse:
    """Create a persisted memory-space record."""
    actor_user_id = _actor_user_uuid(ctx)
    space = await create_memory_space(
        organization_id=org.id,
        created_by_user_id=actor_user_id,
        memory_scope=request.memory_scope,
        scope_key=request.scope_key,
        name=request.name,
        description=request.description,
        metadata=request.metadata,
    )
    return _memory_space_response(space)


@router.get(
    "/spaces/{space_id}",
    response_model=MemorySpaceResponse,
    dependencies=[Depends(require_org_role(*_ADMIN_ROLES))],
)
async def get_memory_space_record(
    space_id: UUID,
    org: AuthOrganization = Depends(get_current_organization),
) -> MemorySpaceResponse:
    """Inspect a persisted memory-space record and its memberships."""
    space = await get_memory_space(organization_id=org.id, space_id=space_id)
    members = await list_memory_space_members(organization_id=org.id, space_id=space_id)
    return _memory_space_response(space, members=members)


@router.patch(
    "/spaces/{space_id}",
    response_model=MemorySpaceResponse,
    dependencies=[Depends(require_org_role(*_ADMIN_ROLES))],
)
async def update_memory_space_record(
    space_id: UUID,
    request: MemorySpaceUpdateRequest,
    org: AuthOrganization = Depends(get_current_organization),
) -> MemorySpaceResponse:
    """Update memory-space metadata or state."""
    space = await update_memory_space(
        organization_id=org.id,
        space_id=space_id,
        name=request.name,
        description=request.description,
        state=request.state,
        metadata=request.metadata,
    )
    members = await list_memory_space_members(organization_id=org.id, space_id=space_id)
    return _memory_space_response(space, members=members)


@router.post(
    "/spaces/{space_id}/members",
    response_model=MemorySpaceMemberResponse,
    dependencies=[Depends(require_org_role(*_ADMIN_ROLES))],
)
async def add_memory_space_member_record(
    space_id: UUID,
    request: MemorySpaceMemberCreateRequest,
    org: AuthOrganization = Depends(get_current_organization),
    ctx: AuthContext = Depends(get_auth_context),
) -> MemorySpaceMemberResponse:
    """Grant a principal membership in a memory space."""
    actor_user_id = _actor_user_uuid(ctx)
    member = await add_memory_space_member(
        organization_id=org.id,
        space_id=space_id,
        created_by_user_id=actor_user_id,
        principal_type=request.principal_type,
        principal_id=request.principal_id,
        role=request.role,
        permissions=request.permissions,
        expires_at=request.expires_at,
    )
    return _memory_space_member_response(member)


async def _preview_memory_spaces(
    *,
    organization_id: UUID,
    primary_space_id: UUID,
    additional_space_ids: list[str],
) -> list[object]:
    seen: set[UUID] = set()
    spaces: list[object] = []
    for space_id in (primary_space_id, *additional_space_ids):
        try:
            normalized_space_id = space_id if isinstance(space_id, UUID) else UUID(str(space_id))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid_memory_space_id") from exc
        if normalized_space_id in seen:
            continue
        seen.add(normalized_space_id)
        spaces.append(
            await get_memory_space(
                organization_id=organization_id,
                space_id=normalized_space_id,
            )
        )
    return spaces


@router.post(
    "/spaces/{space_id}/members/preview",
    response_model=MemorySpaceAccessPreviewResponse,
    dependencies=[Depends(require_org_role(*_ADMIN_ROLES))],
)
async def preview_memory_space_member_access(
    space_id: UUID,
    request: MemorySpaceAccessPreviewRequest,
    http_request: Request = _REQUEST_AUTO_INJECT_SENTINEL,
    org: AuthOrganization = Depends(get_current_organization),
    ctx: AuthContext = Depends(get_auth_context),
) -> MemorySpaceAccessPreviewResponse:
    """Preview what a principal could recall from selected memory spaces."""
    if not ctx.user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    spaces = await _preview_memory_spaces(
        organization_id=org.id,
        primary_space_id=space_id,
        additional_space_ids=request.additional_space_ids,
    )
    result = await preview_memory_access(
        organization_id=str(org.id),
        actor_user_id=str(ctx.user_id),
        target_principal_type=request.target_principal_type,
        target_principal_id=request.target_principal_id,
        memory_spaces=spaces,
        limit=request.limit,
    )
    await _log_memory_audit(
        action="memory.access.preview",
        ctx=ctx,
        request=http_request,
        memory_scope=str(getattr(spaces[0], "memory_scope", "private")) if spaces else None,
        scope_key=getattr(spaces[0], "scope_key", None) if spaces else None,
        project_id=(
            getattr(spaces[0], "scope_key", None)
            if spaces and getattr(spaces[0], "memory_scope", None) == "project"
            else None
        ),
        source_surface="memory_access_preview",
        source_ids=list(result.visible_source_ids),
        derived_ids=list(result.memory_space_ids),
        policy_allowed=result.allowed,
        policy_reason=result.reason,
        details={
            "hidden_but_relevant_count": result.hidden_but_relevant_count,
            "preview": True,
            "redacted_count": result.redacted_count,
            "target_principal_id": request.target_principal_id,
            "target_principal_type": request.target_principal_type,
            "visible_source_count": len(result.visible_source_ids),
        },
    )
    return _access_preview_response(result)


@router.post(
    "/raw",
    response_model=RawMemoryResponse,
    dependencies=[Depends(require_org_role(*_WRITE_ROLES))],
)
async def remember_raw(
    request: RawMemoryRememberRequest,
    http_request: Request = _REQUEST_AUTO_INJECT_SENTINEL,
    org: AuthOrganization = Depends(get_current_organization),
    ctx: AuthContext = Depends(get_auth_context),
) -> RawMemoryResponse:
    """Store verbatim memory before extraction or graph reflection."""
    principal_id = ctx.user_id
    if not principal_id:
        raise HTTPException(status_code=401, detail="Not authenticated")

    started_at = time.perf_counter()
    try:
        capture_surface = AGENT_DIARY_CAPTURE_SURFACE if request.diary else request.capture_surface
        source_id = request.source_id or f"{capture_surface}:manual"
        _validate_diary_request(
            diary=request.diary,
            agent_id=request.agent_id,
            memory_scope=request.memory_scope,
        )
        await _authorize_project_filter(
            ctx=ctx,
            project_id=request.project_id,
            required_project_role=ProjectRole.CONTRIBUTOR,
            surface="raw_remember",
            memory_scope=request.memory_scope,
            scope_key=request.scope_key,
            policy_action="write",
            request=http_request,
        )
        await _authorize_project_scope_write(
            ctx=ctx,
            memory_scope=request.memory_scope,
            scope_key=request.scope_key,
        )
        write_decision = await _authorize_memory_policy(
            ctx=ctx,
            action=MemoryPolicyAction.WRITE,
            memory_scope=request.memory_scope,
            scope_key=request.scope_key,
            surface="raw_remember",
            request=http_request,
            project_id=request.project_id,
        )
        metadata = _diary_metadata(
            metadata=request.metadata,
            diary=request.diary,
            agent_id=request.agent_id,
            project_id=request.project_id,
        )
        memory = await remember_raw_memory(
            organization_id=str(org.id),
            principal_id=principal_id,
            source_id=source_id,
            raw_content=request.raw_content,
            title=request.title,
            memory_scope=request.memory_scope,
            scope_key=request.scope_key,
            tags=request.tags,
            metadata=metadata,
            provenance=request.provenance,
            capture_surface=capture_surface,
        )
        await _log_memory_audit(
            action="memory.remember",
            ctx=ctx,
            request=http_request,
            memory_scope=memory.memory_scope.value,
            scope_key=memory.scope_key,
            project_id=request.project_id,
            source_surface=capture_surface,
            source_ids=[memory.source_id],
            derived_ids=[memory.id],
            policy_allowed=write_decision.allowed,
            policy_reason=write_decision.reason,
            details={
                "agent_id": request.agent_id,
                "diary": request.diary,
                "tag_count": len(request.tags),
            },
        )
        await publish_raw_capture_changed(
            organization_id=memory.organization_id,
            raw_memory_ids=[memory.id],
        )
        response = _raw_memory_response(memory, policy_reason=write_decision.reason)
        telemetry_registry().record_memory_operation(
            operation="remember_raw",
            status="ok",
            duration_ms=elapsed_ms(started_at),
            result_count=1,
        )
        return response
    except ValueError as e:
        telemetry_registry().record_memory_operation(
            operation="remember_raw",
            status="error",
            duration_ms=elapsed_ms(started_at),
        )
        raise HTTPException(status_code=400, detail=str(e)) from e
    except HTTPException:
        telemetry_registry().record_memory_operation(
            operation="remember_raw",
            status="error",
            duration_ms=elapsed_ms(started_at),
        )
        raise
    except Exception as e:
        telemetry_registry().record_memory_operation(
            operation="remember_raw",
            status="error",
            duration_ms=elapsed_ms(started_at),
        )
        log.exception("remember_raw_memory_failed", error=str(e))
        raise HTTPException(status_code=500, detail="Failed to remember raw memory.") from e


@router.post(
    "/raw/recall",
    response_model=RawMemoryRecallResponse,
    dependencies=[Depends(require_org_role(*_READ_ROLES))],
)
async def recall_raw(
    request: RawMemoryRecallRequest,
    http_request: Request = _REQUEST_AUTO_INJECT_SENTINEL,
    org: AuthOrganization = Depends(get_current_organization),
    ctx: AuthContext = Depends(get_auth_context),
) -> RawMemoryRecallResponse:
    """Recall verbatim memories through scoped retrieval."""
    principal_id = ctx.user_id
    if not principal_id:
        raise HTTPException(status_code=401, detail="Not authenticated")

    started_at = time.perf_counter()
    try:
        _validate_diary_request(
            diary=request.diary,
            agent_id=request.agent_id,
            memory_scope=request.memory_scope,
        )
        read_decision = await _authorize_memory_policy(
            ctx=ctx,
            action=MemoryPolicyAction.READ,
            memory_scope=request.memory_scope,
            scope_key=request.scope_key,
            surface="raw_recall",
            request=http_request,
            agent_id=request.agent_id,
            project_id=request.project_id,
        )
        await _authorize_project_filter(
            ctx=ctx,
            project_id=request.project_id,
            required_project_role=ProjectRole.VIEWER,
            surface="raw_recall",
            memory_scope=request.memory_scope,
            scope_key=request.scope_key,
            policy_action="read",
            request=http_request,
        )
        async with recall_concurrency_slot(
            organization_id=str(org.id),
            user_id=principal_id,
            organization_role=ctx.org_role,
        ):
            recall_kwargs: dict[str, Any] = {}
            if request.participants:
                recall_kwargs["participants"] = request.participants
            if request.labels:
                recall_kwargs["labels"] = request.labels
            if request.thread_id:
                recall_kwargs["thread_id"] = request.thread_id
            if request.occurred_after:
                recall_kwargs["occurred_after"] = request.occurred_after
            if request.occurred_before:
                recall_kwargs["occurred_before"] = request.occurred_before
            if request.as_of:
                recall_kwargs["as_of"] = request.as_of
            recall_result = await recall_raw_memory(
                organization_id=str(org.id),
                principal_id=principal_id,
                query=request.query,
                memory_scope=request.memory_scope,
                scope_key=request.scope_key,
                agent_id=request.agent_id,
                project_id=request.project_id,
                limit=request.limit,
                **recall_kwargs,
            )
            if isinstance(recall_result, RawMemoryRecallResult):
                memories = list(recall_result.memories)
                source_failures = [failure.as_metadata() for failure in recall_result.failures]
            else:
                memories = recall_result
                source_failures = []
        await _log_memory_audit(
            action="memory.recall",
            ctx=ctx,
            request=http_request,
            memory_scope=request.memory_scope,
            scope_key=request.scope_key,
            project_id=request.project_id,
            source_surface="raw_recall",
            source_ids=[memory.source_id for memory in memories],
            derived_ids=[memory.id for memory in memories],
            policy_allowed=read_decision.allowed,
            policy_reason=read_decision.reason,
            details=_raw_recall_audit_details(request, result_count=len(memories)),
        )
        response = RawMemoryRecallResponse(
            query=request.query,
            limit=request.limit,
            memories=[
                _raw_memory_response(memory, policy_reason=read_decision.reason)
                for memory in memories
            ],
            policy_reason=read_decision.reason,
            source_degraded=bool(source_failures),
            source_failure_count=len(source_failures),
            source_failures=source_failures,
        )
        telemetry_registry().record_memory_operation(
            operation="recall_raw",
            status="ok",
            duration_ms=elapsed_ms(started_at),
            result_count=len(response.memories),
        )
        return response
    except ValueError as e:
        telemetry_registry().record_memory_operation(
            operation="recall_raw",
            status="error",
            duration_ms=elapsed_ms(started_at),
        )
        raise HTTPException(status_code=400, detail=str(e)) from e
    except RecallConcurrencyLimitExceededError as e:
        telemetry_registry().record_memory_operation(
            operation="recall_raw",
            status="rate_limited",
            duration_ms=elapsed_ms(started_at),
        )
        raise HTTPException(
            status_code=429,
            detail={
                "error": "recall_concurrency_limit_exceeded",
                "max_concurrent": e.max_concurrent,
            },
        ) from e
    except HTTPException:
        telemetry_registry().record_memory_operation(
            operation="recall_raw",
            status="error",
            duration_ms=elapsed_ms(started_at),
        )
        raise
    except Exception as e:
        telemetry_registry().record_memory_operation(
            operation="recall_raw",
            status="error",
            duration_ms=elapsed_ms(started_at),
        )
        log.exception("recall_raw_memory_failed", error=str(e))
        raise HTTPException(status_code=500, detail="Failed to recall raw memory.") from e


@router.get(
    "/audit",
    response_model=MemoryAuditListResponse,
    dependencies=[Depends(require_org_role(*_ADMIN_ROLES))],
)
async def list_memory_audit(
    org: AuthOrganization = Depends(get_current_organization),
    action: str | None = Query(default=None, description="Filter by audit action"),
    actor_user_id: str | None = Query(default=None, description="Filter by actor user ID"),
    source_id: str | None = Query(default=None, description="Filter by source ID"),
    derived_id: str | None = Query(default=None, description="Filter by derived ID"),
    memory_scope: str | None = Query(default=None, description="Filter by memory scope"),
    project_id: str | None = Query(default=None, description="Filter by project ID"),
    policy_allowed: bool | None = Query(default=None, description="Filter by policy state"),
    limit: int = Query(default=50, ge=1, le=200, description="Maximum audit events"),
) -> MemoryAuditListResponse:
    """List memory audit events for owner/admin inspection."""
    _validate_memory_audit_action(action)
    rows = await list_memory_audit_events(
        organization_id=org.id,
        user_id=actor_user_id,
        action=action,
        source_id=source_id,
        derived_id=derived_id,
        memory_scope=memory_scope,
        project_id=project_id,
        policy_allowed=policy_allowed,
        limit=limit,
    )
    return MemoryAuditListResponse(
        events=[_audit_event_response(row) for row in rows],
        limit=limit,
    )


@router.post(
    "/cite",
    response_model=MemoryCitationResponse,
    dependencies=[Depends(require_org_role(*_WRITE_ROLES))],
)
async def cite_memory(
    request: MemoryCitationRequest,
    http_request: Request = _REQUEST_AUTO_INJECT_SENTINEL,
    org: AuthOrganization = Depends(get_current_organization),
    ctx: AuthContext = Depends(get_auth_context),
) -> MemoryCitationResponse:
    """Record memories that materially informed an answer or action."""
    from sibyl_core.tools.usage_citation import record_cited_item_usages

    principal_id = ctx.user_id
    if not principal_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    if request.project_id:
        await verify_entity_project_access(
            None,
            ctx,
            request.project_id,
            required_role=ProjectRole.VIEWER,
        )

    usage = await record_cited_item_usages(
        request.cited_ids,
        organization_id=str(org.id),
        principal_id=principal_id,
        project_id=request.project_id,
        source_surface=request.source_surface,
        request_metadata={
            "route": "memory_cite",
            "metadata": request.metadata,
        },
    )
    await _log_memory_audit(
        action="memory.cite",
        ctx=ctx,
        request=http_request,
        memory_scope="project" if request.project_id else None,
        scope_key=request.project_id,
        source_surface=request.source_surface,
        policy_allowed=True,
        policy_reason="citation_recorded",
        project_id=request.project_id,
        source_ids=request.cited_ids,
        details={"usage": usage, "metadata": request.metadata},
    )
    return MemoryCitationResponse(cited_ids=request.cited_ids, usage=usage)


@router.get(
    "/source-imports/{import_id:path}",
    response_model=SourceImportStatusResponse,
    dependencies=[Depends(require_org_role(*_READ_ROLES))],
)
async def get_memory_source_import_status(
    import_id: str,
    org: AuthOrganization = Depends(get_current_organization),
    ctx: AuthContext = Depends(get_auth_context),
) -> SourceImportStatusResponse:
    """Get source-safe import progress from the memory surface."""
    principal_id = ctx.user_id
    if not principal_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        payload = await get_source_import_status(
            import_id,
            organization_id=str(org.id),
            principal_id=principal_id,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="source_import_not_found") from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail="source_import_forbidden") from exc
    return SourceImportStatusResponse.model_validate(payload)


@router.get(
    "/inspect/{source_id:path}",
    response_model=MemorySourceInspectResponse,
    dependencies=[Depends(require_org_role(*_ADMIN_ROLES))],
)
@handle_workflow_errors("inspect_memory_source", id_param="source_id")
async def inspect_memory_source(
    source_id: str,
    http_request: Request = _REQUEST_AUTO_INJECT_SENTINEL,
    org: AuthOrganization = Depends(get_current_organization),
    ctx: AuthContext = Depends(get_auth_context),
) -> MemorySourceInspectResponse:
    """Inspect a raw memory source and its audit-derived records."""
    principal_id = ctx.user_id
    if not principal_id:
        raise HTTPException(status_code=401, detail="Not authenticated")

    memory = await _load_memory_source_for_org(
        organization_id=str(org.id),
        source_id=source_id,
    )
    policy_decision = await _inspect_content_policy(ctx=ctx, memory=memory)
    audit_events = await _source_audit_events(
        organization_id=str(org.id),
        source_id=source_id,
        memory=memory,
    )
    response = _memory_source_inspect_response(
        memory=memory,
        policy_decision=policy_decision,
        audit_events=audit_events,
    )
    await _log_memory_audit(
        action="memory.inspect",
        ctx=ctx,
        request=http_request,
        memory_scope=memory.memory_scope.value,
        scope_key=memory.scope_key,
        project_id=response.project_id,
        source_surface="memory_inspect",
        source_ids=[memory.id, memory.source_id],
        derived_ids=response.derived_ids,
        policy_allowed=policy_decision.allowed,
        policy_reason=policy_decision.reason,
        details={
            "audit_event_count": response.audit_event_count,
            "content_redacted": response.content_redacted,
        },
    )
    return response


@router.post(
    "/inspect/{source_id:path}/corrections/preview",
    response_model=MemoryCorrectionResponse,
    dependencies=[Depends(require_org_role(*_ADMIN_ROLES))],
)
async def preview_memory_correction_route(
    source_id: str,
    request: MemoryCorrectionRequest,
    http_request: Request = _REQUEST_AUTO_INJECT_SENTINEL,
    org: AuthOrganization = Depends(get_current_organization),
    ctx: AuthContext = Depends(get_auth_context),
) -> MemoryCorrectionResponse:
    """Preview a memory correction or lifecycle action without mutating."""
    principal_id = ctx.user_id
    if not principal_id:
        raise HTTPException(status_code=401, detail="Not authenticated")

    memory = await _load_memory_source_for_org(
        organization_id=str(org.id),
        source_id=source_id,
    )
    accessible_projects = await _project_accessible_for_policy(
        ctx=ctx,
        memory_scope=memory.memory_scope.value,
        scope_key=memory.scope_key,
    )
    preview = await preview_memory_correction(
        organization_id=str(org.id),
        source_id=memory.id,
        principal_id=principal_id,
        action=request.action,
        reason=request.reason,
        accessible_projects=accessible_projects,
        replacement_source_id=request.replacement_source_id,
        duplicate_of_source_id=request.duplicate_of_source_id,
    )
    response = _correction_response(preview)
    await _log_memory_audit(
        action=f"{preview.audit_action}.preview",
        ctx=ctx,
        request=http_request,
        memory_scope=memory.memory_scope.value,
        scope_key=memory.scope_key,
        project_id=_memory_project_id(memory),
        source_surface="memory_correction_preview",
        source_ids=preview.affected_source_ids or [memory.id],
        derived_ids=preview.affected_derived_ids,
        policy_allowed=preview.allowed,
        policy_reason=preview.reason,
        details={
            "action": preview.action,
            "metadata": dict(request.metadata),
            "recall_impact": dict(preview.recall_impact),
            "synthesis_impact": dict(preview.synthesis_impact),
            "target_lifecycle_state": preview.target_lifecycle_state,
            "target_lifecycle_flags": preview.target_lifecycle_flags,
        },
    )
    return response


@router.post(
    "/inspect/{source_id:path}/corrections",
    response_model=MemoryCorrectionResponse,
    dependencies=[Depends(require_org_role(*_ADMIN_ROLES))],
)
async def apply_memory_correction_route(
    source_id: str,
    request: MemoryCorrectionRequest,
    http_request: Request = _REQUEST_AUTO_INJECT_SENTINEL,
    org: AuthOrganization = Depends(get_current_organization),
    ctx: AuthContext = Depends(get_auth_context),
) -> MemoryCorrectionResponse:
    """Apply a memory correction or lifecycle action."""
    principal_id = ctx.user_id
    if not principal_id:
        raise HTTPException(status_code=401, detail="Not authenticated")

    memory = await _load_memory_source_for_org(
        organization_id=str(org.id),
        source_id=source_id,
    )
    accessible_projects = await _project_accessible_for_policy(
        ctx=ctx,
        memory_scope=memory.memory_scope.value,
        scope_key=memory.scope_key,
    )
    result = await apply_memory_correction(
        organization_id=str(org.id),
        source_id=memory.id,
        principal_id=principal_id,
        action=request.action,
        reason=request.reason,
        accessible_projects=accessible_projects,
        replacement_source_id=request.replacement_source_id,
        duplicate_of_source_id=request.duplicate_of_source_id,
    )
    response = _correction_result_response(result)
    await _log_memory_audit(
        action=result.preview.audit_action,
        ctx=ctx,
        request=http_request,
        memory_scope=memory.memory_scope.value,
        scope_key=memory.scope_key,
        project_id=_memory_project_id(memory),
        source_surface="memory_correction",
        source_ids=result.preview.affected_source_ids or [memory.id],
        derived_ids=result.preview.affected_derived_ids,
        policy_allowed=result.preview.allowed and result.applied,
        policy_reason=result.preview.reason,
        details={
            "action": result.preview.action,
            "applied": result.applied,
            "metadata": dict(request.metadata),
            "recall_impact": dict(result.preview.recall_impact),
            "synthesis_impact": dict(result.preview.synthesis_impact),
            "target_lifecycle_state": result.preview.target_lifecycle_state,
            "target_lifecycle_flags": result.preview.target_lifecycle_flags,
            "updated_review_state": response.updated_review_state,
        },
    )
    return response


@router.post(
    "/share/preview",
    response_model=MemorySharePreviewResponse,
    dependencies=[Depends(require_org_role(*_WRITE_ROLES))],
)
@handle_workflow_errors("preview_memory_share")
async def preview_memory_share_route(
    request: MemorySharePreviewRequest,
    http_request: Request = _REQUEST_AUTO_INJECT_SENTINEL,
    org: AuthOrganization = Depends(get_current_organization),
    ctx: AuthContext = Depends(get_auth_context),
) -> MemorySharePreviewResponse:
    """Preview memory sharing without enabling a share write."""
    principal_id = ctx.user_id
    if not principal_id:
        raise HTTPException(status_code=401, detail="Not authenticated")

    accessible_projects = await _accessible_projects_for_share_preview(
        ctx=ctx,
        request=request,
        http_request=http_request,
    )
    accessible_teams = await _accessible_teams_for_share(ctx=ctx)
    await _authorize_share_api_key_scopes(
        ctx=ctx,
        request=request,
        organization_id=str(org.id),
        accessible_projects=accessible_projects,
        accessible_teams=accessible_teams,
        http_request=http_request,
        surface="memory_share_preview",
    )
    result = await preview_memory_share(
        source_ids=request.source_ids,
        organization_id=str(org.id),
        principal_id=principal_id,
        target_scope=request.target_scope,
        target_scope_key=request.target_scope_key,
        recipient_organization_id=request.recipient_organization_id,
        accessible_projects=accessible_projects,
        accessible_teams=accessible_teams,
    )
    await _log_memory_audit(
        action="memory.share.preview",
        ctx=ctx,
        request=http_request,
        memory_scope=result.target_scope.value if result.target_scope else request.target_scope,
        scope_key=result.target_scope_key or request.target_scope_key,
        project_id=request.project_id
        or (request.target_scope_key if request.target_scope == "project" else None),
        source_surface="memory_share_preview",
        source_ids=list(result.source_ids),
        derived_ids=[],
        policy_allowed=result.allowed,
        policy_reason=result.reason,
        details={
            "denied_source_count": len(result.denied_source_ids),
            "hidden_but_relevant_count": result.hidden_but_relevant_count,
            "preview": True,
            "recipient_organization_id": request.recipient_organization_id,
            "redacted_count": result.redacted_count,
            "target_scope": result.target_scope.value if result.target_scope else None,
            "visible_source_count": len(result.visible_source_ids),
        },
    )
    return _share_preview_response(result)


@router.post(
    "/share",
    response_model=MemoryShareResponse,
    dependencies=[Depends(require_org_role(*_WRITE_ROLES))],
)
@handle_workflow_errors("share_memory")
async def share_memory_route(
    request: MemoryShareRequest,
    http_request: Request = _REQUEST_AUTO_INJECT_SENTINEL,
    org: AuthOrganization = Depends(get_current_organization),
    ctx: AuthContext = Depends(get_auth_context),
) -> MemoryShareResponse:
    """Apply same-org memory sharing through promotion-backed native writes."""
    principal_id = ctx.user_id
    if not principal_id:
        raise HTTPException(status_code=401, detail="Not authenticated")

    accessible_projects = await _accessible_projects_for_share_preview(
        ctx=ctx,
        request=request,
        http_request=http_request,
    )
    accessible_teams = await _accessible_teams_for_share(ctx=ctx)
    await _authorize_share_api_key_scopes(
        ctx=ctx,
        request=request,
        organization_id=str(org.id),
        accessible_projects=accessible_projects,
        accessible_teams=accessible_teams,
        http_request=http_request,
        surface="memory_share",
    )
    project_id = request.project_id or (
        request.target_scope_key if request.target_scope == "project" else None
    )
    result = await share_memory(
        source_ids=request.source_ids,
        organization_id=str(org.id),
        principal_id=principal_id,
        target_scope=request.target_scope,
        target_scope_key=request.target_scope_key,
        recipient_organization_id=request.recipient_organization_id,
        project=project_id,
        accessible_projects=accessible_projects,
        accessible_teams=accessible_teams,
    )
    promoted_ids = [
        str(promotion.promoted_id)
        for promotion in result.promotions
        if promotion.success and promotion.promoted_id
    ]
    audit_id = await _log_memory_audit(
        action="memory.share.apply",
        ctx=ctx,
        request=http_request,
        memory_scope=result.preview.target_scope.value
        if result.preview.target_scope
        else request.target_scope,
        scope_key=result.preview.target_scope_key or request.target_scope_key,
        project_id=project_id,
        source_surface="memory_share",
        source_ids=list(result.preview.source_ids),
        derived_ids=promoted_ids,
        policy_allowed=result.applied,
        policy_reason=result.reason,
        details={
            "applied": result.applied,
            "denied_source_count": len(result.preview.denied_source_ids),
            "hidden_but_relevant_count": result.preview.hidden_but_relevant_count,
            "preview": False,
            "promoted_count": len(promoted_ids),
            "recipient_organization_id": request.recipient_organization_id,
            "redacted_count": result.preview.redacted_count,
            "target_policy_reason": (result.preview.metadata or {}).get("target_policy_reason"),
            "target_scope": result.preview.target_scope.value
            if result.preview.target_scope
            else None,
            "visible_source_count": len(result.preview.visible_source_ids),
        },
    )
    audit_event_ids = [audit_id] if audit_id else []
    return _share_response(result, audit_event_ids=audit_event_ids)


@router.post(
    "/reflection/promote/preview",
    response_model=ReflectionPromotionPreviewResponse,
    dependencies=[Depends(require_org_role(*_WRITE_ROLES))],
)
@handle_workflow_errors("preview_reflection_promotion", id_param="candidate_id")
async def preview_reflection_promotion(
    request: ReflectionPromotionRequest,
    http_request: Request = _REQUEST_AUTO_INJECT_SENTINEL,
    org: AuthOrganization = Depends(get_current_organization),
    ctx: AuthContext = Depends(get_auth_context),
) -> ReflectionPromotionPreviewResponse:
    """Preview a reflection promotion without writing native memory."""
    principal_id = ctx.user_id
    if not principal_id:
        raise HTTPException(status_code=401, detail="Not authenticated")

    accessible_projects = await _accessible_projects_for_promotion(
        ctx=ctx,
        request=request,
        http_request=http_request,
    )
    result = await preview_reflection_candidate_promotion(
        candidate_id=request.candidate_id,
        organization_id=str(org.id),
        principal_id=principal_id,
        promote_to_scope=request.promote_to_scope,
        promote_to_scope_key=request.promote_to_scope_key,
        domain=request.domain,
        project=request.project,
        accessible_projects=accessible_projects,
    )
    await _log_memory_audit(
        action="memory.reflect.promote.preview",
        ctx=ctx,
        request=http_request,
        memory_scope=result.memory_scope.value if result.memory_scope else request.promote_to_scope,
        scope_key=result.scope_key or request.promote_to_scope_key,
        project_id=request.project,
        source_surface="reflection_promote_preview",
        source_ids=[request.candidate_id, *result.raw_source_ids],
        derived_ids=[],
        policy_allowed=result.allowed,
        policy_reason=result.reason,
        details={
            "domain": request.domain,
            "preview": True,
            "related_to_count": len(request.related_to),
            "review_state": result.review_state,
            "source_count": len(result.raw_source_ids),
        },
    )
    if result.reason == "candidate_not_found":
        raise HTTPException(
            status_code=404,
            detail="reflection_candidate_not_found",
        )
    return _promotion_preview_response(result)


@router.post(
    "/promote/preview",
    response_model=ReflectionPromotionPreviewResponse,
    dependencies=[Depends(require_org_role(*_WRITE_ROLES))],
)
@handle_workflow_errors("preview_memory_promotion", id_param="candidate_id")
async def preview_memory_promotion(
    request: ReflectionPromotionRequest,
    http_request: Request = _REQUEST_AUTO_INJECT_SENTINEL,
    org: AuthOrganization = Depends(get_current_organization),
    ctx: AuthContext = Depends(get_auth_context),
) -> ReflectionPromotionPreviewResponse:
    """Preview promotion for a reflection candidate or imported raw memory."""
    principal_id = ctx.user_id
    if not principal_id:
        raise HTTPException(status_code=401, detail="Not authenticated")

    accessible_projects = await _accessible_projects_for_promotion(
        ctx=ctx,
        request=request,
        http_request=http_request,
    )
    result = await preview_reflection_candidate_promotion(
        candidate_id=request.candidate_id,
        organization_id=str(org.id),
        principal_id=principal_id,
        promote_to_scope=request.promote_to_scope,
        promote_to_scope_key=request.promote_to_scope_key,
        domain=request.domain,
        project=request.project,
        accessible_projects=accessible_projects,
    )
    if result.reason == "not_reflection_candidate":
        await _authorize_raw_promotion_api_key_scopes(
            ctx=ctx,
            request=request,
            organization_id=str(org.id),
            accessible_projects=accessible_projects,
            http_request=http_request,
            surface="memory_promote_preview",
        )
        result = await preview_raw_memory_promotion(
            raw_memory_id=request.candidate_id,
            organization_id=str(org.id),
            principal_id=principal_id,
            promote_to_scope=request.promote_to_scope,
            promote_to_scope_key=request.promote_to_scope_key,
            domain=request.domain,
            project=request.project,
            accessible_projects=accessible_projects,
        )
    await _log_memory_audit(
        action="memory.promote.preview",
        ctx=ctx,
        request=http_request,
        memory_scope=result.memory_scope.value if result.memory_scope else request.promote_to_scope,
        scope_key=result.scope_key or request.promote_to_scope_key,
        project_id=request.project,
        source_surface="memory_promote_preview",
        source_ids=[request.candidate_id, *result.raw_source_ids],
        derived_ids=[],
        policy_allowed=result.allowed,
        policy_reason=result.reason,
        details={
            "domain": request.domain,
            "preview": True,
            "related_to_count": len(request.related_to),
            "review_state": result.review_state,
            "source_count": len(result.raw_source_ids),
        },
    )
    if result.reason == "candidate_not_found":
        raise HTTPException(
            status_code=404,
            detail="memory_candidate_not_found",
        )
    return _promotion_preview_response(result)


@router.post(
    "/reflection/review/auto",
    response_model=ReflectionAutonomyResponse,
    dependencies=[Depends(require_org_role(*_WRITE_ROLES))],
)
async def auto_review_reflection_candidate(
    request: ReflectionAutonomyRequest,
    http_request: Request = _REQUEST_AUTO_INJECT_SENTINEL,
    org: AuthOrganization = Depends(get_current_organization),
    ctx: AuthContext = Depends(get_auth_context),
) -> ReflectionAutonomyResponse:
    """Automatically review and promote safe reflection candidates."""
    try:
        return await _auto_review_reflection_candidate(
            request=request,
            http_request=http_request,
            org=org,
            ctx=ctx,
        )
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        log.exception(
            "auto_review_reflection_candidate_failed",
            candidate_id=request.candidate_id,
            error=str(e),
        )
        raise HTTPException(
            status_code=500,
            detail="Failed to auto-review reflection candidate.",
        ) from e


async def _auto_review_reflection_candidate(
    *,
    request: ReflectionAutonomyRequest,
    http_request: Request,
    org: AuthOrganization,
    ctx: AuthContext,
    accessible_projects: set[str] | None = None,
) -> ReflectionAutonomyResponse:
    principal_id = ctx.user_id
    if not principal_id:
        raise HTTPException(status_code=401, detail="Not authenticated")

    if accessible_projects is None:
        accessible_projects = await _accessible_projects_for_promotion(
            ctx=ctx,
            request=request,
            http_request=http_request,
        )
    preview = await preview_reflection_candidate_promotion(
        candidate_id=request.candidate_id,
        organization_id=str(org.id),
        principal_id=principal_id,
        promote_to_scope=request.promote_to_scope,
        promote_to_scope_key=request.promote_to_scope_key,
        domain=request.domain,
        project=request.project,
        accessible_projects=accessible_projects,
    )
    confidence_threshold = (
        request.confidence_threshold
        if request.confidence_threshold is not None
        else ReflectionAutonomyPolicy().confidence_threshold
    )
    decision = decide_reflection_candidate_autonomy(
        preview,
        policy=ReflectionAutonomyPolicy(confidence_threshold=confidence_threshold),
        dry_run=request.dry_run,
    )
    promotion: ReflectionPromotionResult | None = None
    if decision.should_promote:
        promotion = await promote_reflection_candidate_review(
            candidate_id=request.candidate_id,
            organization_id=str(org.id),
            principal_id=principal_id,
            promote_to_scope=request.promote_to_scope,
            promote_to_scope_key=request.promote_to_scope_key,
            domain=request.domain,
            project=request.project,
            related_to=request.related_to,
            accessible_projects=accessible_projects,
        )

    audit_action = (
        "memory.reflect.auto_promote"
        if decision.outcome is ReflectionAutonomyOutcome.AUTO_PROMOTE
        else "memory.reflect.auto_review"
    )
    audit_scope = decision.memory_scope.value if decision.memory_scope else request.promote_to_scope
    audit_scope_key = decision.scope_key or request.promote_to_scope_key
    await _log_memory_audit(
        action=audit_action,
        ctx=ctx,
        request=http_request,
        memory_scope=audit_scope,
        scope_key=audit_scope_key,
        project_id=request.project,
        source_surface="reflection_auto_review",
        source_ids=[request.candidate_id, *decision.raw_source_ids],
        derived_ids=[promotion.promoted_id] if promotion and promotion.promoted_id else [],
        policy_allowed=preview.allowed,
        policy_reason=decision.reason,
        details={
            "action_succeeded": promotion.success if promotion else False,
            "confidence": decision.confidence,
            "confidence_threshold": decision.confidence_threshold,
            "domain": request.domain,
            "dry_run": request.dry_run,
            "exception_reasons": decision.exception_reasons,
            "outcome": decision.outcome.value,
            "recommended_action": decision.recommended_action.value,
            "related_to_count": len(request.related_to),
            "review_state": promotion.review_state if promotion else decision.review_state,
            "source_count": len(decision.raw_source_ids),
        },
    )
    if preview.reason == "candidate_not_found":
        raise HTTPException(
            status_code=404,
            detail="reflection_candidate_not_found",
        )
    return _autonomy_response(decision=decision, preview=preview, promotion=promotion)


@router.post(
    "/reflection/review/drain",
    response_model=ReflectionReviewDrainResponse,
    dependencies=[Depends(require_org_role(*_WRITE_ROLES))],
)
async def drain_reflection_review(
    request: ReflectionReviewDrainRequest,
    http_request: Request = _REQUEST_AUTO_INJECT_SENTINEL,
    org: AuthOrganization = Depends(get_current_organization),
    ctx: AuthContext = Depends(get_auth_context),
) -> ReflectionReviewDrainResponse:
    """Bulk auto-review pending reflection candidates."""
    if not ctx.user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")

    archive_reasons = {
        reason
        for reason in request.archive_exception_reasons
        if reason in _ARCHIVEABLE_REFLECTION_EXCEPTION_REASONS
    }
    try:
        candidates = await list_reflection_candidate_reviews(
            organization_id=str(org.id),
            review_state="pending",
            limit=request.limit,
        )
        if not candidates:
            return _drain_response(request=request, results=[])
        accessible_projects = await _accessible_projects_for_promotion(
            ctx=ctx,
            request=ReflectionAutonomyRequest(
                candidate_id="reflection-review-drain",
                promote_to_scope=request.promote_to_scope,
                promote_to_scope_key=request.promote_to_scope_key,
                domain=request.domain,
                project=request.project,
                related_to=request.related_to,
                dry_run=request.dry_run,
                confidence_threshold=request.confidence_threshold,
            ),
            http_request=http_request,
        )
        source_accessible_projects = await list_accessible_project_graph_ids(ctx)
        readable_projects = {str(project_id) for project_id in source_accessible_projects or set()}
        results: list[ReflectionReviewDrainItem] = []
        for candidate in candidates:
            policy_context = MemoryPolicyContext(
                actor_user_id=ctx.user_id,
                organization_id=ctx.organization_id,
                organization_role=ctx.org_role,
                memory_space=candidate.memory_scope.value,
                scope_key=candidate.scope_key,
                project_id=_memory_project_id(candidate),
                accessible_projects=readable_projects,
                agent_id=candidate.agent_id,
                source_surface="reflection_review_drain",
            )
            decision = authorize_memory_read(policy_context=policy_context)
            if not decision.allowed:
                results.append(
                    ReflectionReviewDrainItem(
                        candidate_id=candidate.id,
                        outcome="skip",
                        recommended_action="route_to_review",
                        dry_run=request.dry_run,
                        reason="policy_denied",
                        review_state=candidate.review_state,
                        raw_source_ids=[],
                        policy_reasons=[decision.reason],
                    )
                )
                continue
            candidate_request = ReflectionAutonomyRequest(
                candidate_id=candidate.id,
                promote_to_scope=request.promote_to_scope,
                promote_to_scope_key=request.promote_to_scope_key,
                domain=request.domain,
                project=request.project,
                related_to=request.related_to,
                dry_run=request.dry_run,
                confidence_threshold=request.confidence_threshold,
            )
            try:
                response = await _auto_review_reflection_candidate(
                    request=candidate_request,
                    http_request=http_request,
                    org=org,
                    ctx=ctx,
                    accessible_projects=accessible_projects,
                )
                archived = False
                review_state = response.review_state
                if (
                    request.archive_exceptions
                    and not request.dry_run
                    and _should_archive_reflection_exception(
                        response,
                        archive_reasons=archive_reasons,
                    )
                ):
                    archived_memory = await _archive_reflection_exception_candidate(
                        response=response,
                        org=org,
                        ctx=ctx,
                        request=http_request,
                        project_id=request.project,
                    )
                    archived = archived_memory is not None
                    if archived_memory is not None:
                        review_state = archived_memory.review_state
                results.append(
                    _drain_item_from_autonomy(
                        response,
                        archived=archived,
                        review_state=review_state,
                    )
                )
            except HTTPException as exc:
                results.append(
                    _drain_error_item(candidate.id, error=exc.detail, dry_run=request.dry_run)
                )
            except Exception as exc:
                log.warning(
                    "drain_reflection_review_candidate_failed",
                    candidate_id=candidate.id,
                    error=str(exc),
                    exc_info=True,
                )
                results.append(
                    _drain_error_item(candidate.id, error=str(exc), dry_run=request.dry_run)
                )
        return _drain_response(request=request, results=results)
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        log.exception("drain_reflection_review_failed", error=str(exc))
        raise HTTPException(
            status_code=500,
            detail="Failed to drain reflection review queue.",
        ) from exc


@router.post(
    "/reflection/promote",
    response_model=ReflectionPromotionResponse,
    dependencies=[Depends(require_org_role(*_WRITE_ROLES))],
)
async def promote_reflection_candidate(
    request: ReflectionPromotionRequest,
    http_request: Request = _REQUEST_AUTO_INJECT_SENTINEL,
    org: AuthOrganization = Depends(get_current_organization),
    ctx: AuthContext = Depends(get_auth_context),
) -> ReflectionPromotionResponse:
    """Promote a reviewed reflection candidate into native Surreal memory."""
    principal_id = ctx.user_id
    if not principal_id:
        raise HTTPException(status_code=401, detail="Not authenticated")

    try:
        accessible_projects = await _accessible_projects_for_promotion(
            ctx=ctx,
            request=request,
            http_request=http_request,
        )
        result = await promote_reflection_candidate_review(
            candidate_id=request.candidate_id,
            organization_id=str(org.id),
            principal_id=principal_id,
            promote_to_scope=request.promote_to_scope,
            promote_to_scope_key=request.promote_to_scope_key,
            domain=request.domain,
            project=request.project,
            related_to=request.related_to,
            accessible_projects=accessible_projects,
        )
        await _log_memory_audit(
            action="memory.reflect.promote",
            ctx=ctx,
            request=http_request,
            memory_scope=result.memory_scope.value
            if result.memory_scope
            else request.promote_to_scope,
            scope_key=result.scope_key or request.promote_to_scope_key,
            project_id=request.project,
            source_surface="reflection_promote",
            source_ids=[request.candidate_id, *result.raw_source_ids],
            derived_ids=[result.promoted_id] if result.promoted_id else [],
            policy_allowed=_promotion_policy_allowed(result),
            policy_reason=result.reason,
            details={
                "action_succeeded": result.success,
                "domain": request.domain,
                "related_to_count": len(request.related_to),
                "review_state": result.review_state,
            },
        )
        if result.reason == "candidate_not_found":
            raise HTTPException(
                status_code=404,
                detail=f"Reflection candidate not found: {request.candidate_id}",
            )
        return _promotion_response(result)
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        log.exception(
            "promote_reflection_candidate_failed",
            candidate_id=request.candidate_id,
            error=str(e),
        )
        raise HTTPException(
            status_code=500,
            detail="Failed to promote reflection candidate.",
        ) from e


@router.post(
    "/promote",
    response_model=ReflectionPromotionResponse,
    dependencies=[Depends(require_org_role(*_WRITE_ROLES))],
)
async def promote_memory(
    request: ReflectionPromotionRequest,
    http_request: Request = _REQUEST_AUTO_INJECT_SENTINEL,
    org: AuthOrganization = Depends(get_current_organization),
    ctx: AuthContext = Depends(get_auth_context),
) -> ReflectionPromotionResponse:
    """Promote a reflection candidate or imported raw memory."""
    principal_id = ctx.user_id
    if not principal_id:
        raise HTTPException(status_code=401, detail="Not authenticated")

    try:
        accessible_projects = await _accessible_projects_for_promotion(
            ctx=ctx,
            request=request,
            http_request=http_request,
        )
        result = await promote_reflection_candidate_review(
            candidate_id=request.candidate_id,
            organization_id=str(org.id),
            principal_id=principal_id,
            promote_to_scope=request.promote_to_scope,
            promote_to_scope_key=request.promote_to_scope_key,
            domain=request.domain,
            project=request.project,
            related_to=request.related_to,
            accessible_projects=accessible_projects,
        )
        if result.reason == "not_reflection_candidate":
            await _authorize_raw_promotion_api_key_scopes(
                ctx=ctx,
                request=request,
                organization_id=str(org.id),
                accessible_projects=accessible_projects,
                http_request=http_request,
                surface="memory_promote",
            )
            result = await promote_raw_memory(
                raw_memory_id=request.candidate_id,
                organization_id=str(org.id),
                principal_id=principal_id,
                promote_to_scope=request.promote_to_scope,
                promote_to_scope_key=request.promote_to_scope_key,
                domain=request.domain,
                project=request.project,
                related_to=request.related_to,
                accessible_projects=accessible_projects,
            )
        await _log_memory_audit(
            action="memory.promote",
            ctx=ctx,
            request=http_request,
            memory_scope=result.memory_scope.value
            if result.memory_scope
            else request.promote_to_scope,
            scope_key=result.scope_key or request.promote_to_scope_key,
            project_id=request.project,
            source_surface="memory_promote",
            source_ids=[request.candidate_id, *result.raw_source_ids],
            derived_ids=[result.promoted_id] if result.promoted_id else [],
            policy_allowed=_promotion_policy_allowed(result),
            policy_reason=result.reason,
            details={
                "action_succeeded": result.success,
                "domain": request.domain,
                "related_to_count": len(request.related_to),
                "review_state": result.review_state,
            },
        )
        if result.reason == "candidate_not_found":
            raise HTTPException(
                status_code=404,
                detail=f"Memory candidate not found: {request.candidate_id}",
            )
        return _promotion_response(result)
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        log.exception(
            "promote_memory_failed",
            candidate_id=request.candidate_id,
            error=str(e),
        )
        raise HTTPException(
            status_code=500,
            detail="Failed to promote memory.",
        ) from e
