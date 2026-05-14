"""Raw memory API routes."""

from __future__ import annotations

from datetime import datetime
from typing import cast

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request

from sibyl.api.schemas import (
    MemoryAuditEventResponse,
    MemoryAuditListResponse,
    MemoryScopeInputResponse,
    MemoryScopeLiteral,
    RawMemoryRecallRequest,
    RawMemoryRecallResponse,
    RawMemoryRememberRequest,
    RawMemoryResponse,
    ReflectionPromotionPreviewResponse,
    ReflectionPromotionRequest,
    ReflectionPromotionResponse,
)
from sibyl.auth.authorization import verify_entity_project_access
from sibyl.auth.context import AuthContext
from sibyl.auth.dependencies import get_auth_context, get_current_organization, require_org_role
from sibyl.persistence.auth_runtime import (
    list_accessible_project_graph_ids,
    list_memory_audit_events,
    log_memory_audit_event,
)
from sibyl_core.auth import AuthOrganization, MemoryPolicyContext, OrganizationRole, ProjectRole
from sibyl_core.auth.memory_policy import (
    MemoryPolicyAction,
    MemoryPolicyDecision,
    authorize_memory_read,
    authorize_memory_write,
)
from sibyl_core.services.native_memory import (
    NativeReflectionPromotionPreview,
    NativeReflectionPromotionResult,
    preview_reflection_candidate_promotion,
    promote_reflection_candidate_review,
)
from sibyl_core.services.surreal_content import (
    AGENT_DIARY_CAPTURE_SURFACE,
    RawMemory,
    recall_raw_memory,
    remember_raw_memory,
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
) -> None:
    try:
        await log_memory_audit_event(
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
    policy_context = MemoryPolicyContext(
        actor_user_id=ctx.user_id,
        organization_id=ctx.organization_id,
        organization_role=ctx.org_role,
        memory_space=memory_scope,
        scope_key=scope_key,
        project_id=project_id,
        accessible_projects=accessible_projects,
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
        policy_reason=policy_reason,
    )


def _promotion_response(result: NativeReflectionPromotionResult) -> ReflectionPromotionResponse:
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
    result: NativeReflectionPromotionPreview,
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


def _validate_memory_audit_action(action: str | None) -> None:
    if action and not action.startswith("memory."):
        raise HTTPException(status_code=400, detail="invalid_memory_audit_action")


def _promotion_policy_allowed(result: NativeReflectionPromotionResult) -> bool | None:
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
        return _raw_memory_response(memory, policy_reason=write_decision.reason)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except HTTPException:
        raise
    except Exception as e:
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
        memories = await recall_raw_memory(
            organization_id=str(org.id),
            principal_id=principal_id,
            query=request.query,
            memory_scope=request.memory_scope,
            scope_key=request.scope_key,
            agent_id=request.agent_id,
            project_id=request.project_id,
            limit=request.limit,
        )
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
            details={
                "agent_id": request.agent_id,
                "diary": request.diary,
                "limit": request.limit,
                "result_count": len(memories),
            },
        )
        return RawMemoryRecallResponse(
            query=request.query,
            limit=request.limit,
            memories=[
                _raw_memory_response(memory, policy_reason=read_decision.reason)
                for memory in memories
            ],
            policy_reason=read_decision.reason,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except HTTPException:
        raise
    except Exception as e:
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
    "/reflection/promote/preview",
    response_model=ReflectionPromotionPreviewResponse,
    dependencies=[Depends(require_org_role(*_WRITE_ROLES))],
)
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

    try:
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
            memory_scope=result.memory_scope.value
            if result.memory_scope
            else request.promote_to_scope,
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
    except HTTPException:
        raise
    except Exception as e:
        log.exception(
            "preview_reflection_promotion_failed",
            candidate_id=request.candidate_id,
            error=str(e),
        )
        raise HTTPException(
            status_code=500,
            detail="Failed to preview reflection promotion.",
        ) from e


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
