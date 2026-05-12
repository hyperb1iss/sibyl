"""Raw memory API routes."""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends, HTTPException

from sibyl.api.schemas import (
    RawMemoryRecallRequest,
    RawMemoryRecallResponse,
    RawMemoryRememberRequest,
    RawMemoryResponse,
    ReflectionPromotionRequest,
    ReflectionPromotionResponse,
)
from sibyl.auth.authorization import verify_entity_project_access
from sibyl.auth.context import AuthContext
from sibyl.auth.dependencies import get_auth_context, get_current_organization, require_org_role
from sibyl.persistence.auth_runtime import list_accessible_project_graph_ids
from sibyl_core.auth import AuthOrganization, OrganizationRole, ProjectRole
from sibyl_core.auth.memory_policy import (
    MemoryPolicyAction,
    MemoryPolicyDecision,
    authorize_memory_read,
    authorize_memory_write,
)
from sibyl_core.services.native_memory import (
    NativeReflectionPromotionResult,
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

router = APIRouter(prefix="/memory", tags=["memory"])


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
    agent_id: str | None = None,
    project_id: str | None = None,
) -> MemoryPolicyDecision:
    accessible_projects = await _project_accessible_for_policy(
        ctx=ctx,
        memory_scope=memory_scope,
        scope_key=scope_key,
    )
    if action is MemoryPolicyAction.READ:
        decision = authorize_memory_read(
            principal_id=ctx.user_id,
            memory_scope=memory_scope,
            scope_key=scope_key,
            agent_id=agent_id,
            project_id=project_id,
            accessible_projects=accessible_projects,
        )
    elif action is MemoryPolicyAction.WRITE:
        decision = authorize_memory_write(
            principal_id=ctx.user_id,
            memory_scope=memory_scope,
            scope_key=scope_key,
            accessible_projects=accessible_projects,
        )
    else:
        msg = f"Unsupported raw memory policy action: {action.value}"
        raise ValueError(msg)

    _log_policy_decision(ctx=ctx, decision=decision, surface=surface)
    if not decision.allowed:
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
) -> None:
    if not project_id:
        return
    await verify_entity_project_access(
        None,
        ctx,
        project_id,
        required_role=required_project_role,
        require_existing_project=True,
    )


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
    policy_reasons = metadata.get("policy_reasons")
    return ReflectionPromotionResponse(
        success=result.success,
        candidate_id=result.candidate_id,
        promoted_id=result.promoted_id,
        reason=result.reason,
        review_state=result.review_state,
        memory_scope=result.memory_scope.value if result.memory_scope else None,
        scope_key=result.scope_key,
        raw_source_ids=list(result.raw_source_ids),
        policy_reasons=list(policy_reasons) if isinstance(policy_reasons, list) else [],
        metadata=metadata,
    )


async def _accessible_projects_for_promotion(
    *,
    ctx: AuthContext,
    request: ReflectionPromotionRequest,
) -> set[str]:
    project_ids: set[str] = set()
    if request.project:
        project_ids.add(request.project)
    if request.promote_to_scope == "project":
        target_project = request.promote_to_scope_key or request.project
        if target_project:
            project_ids.add(target_project)

    for project_id in project_ids:
        await verify_entity_project_access(
            None,
            ctx,
            project_id,
            required_role=ProjectRole.CONTRIBUTOR,
            require_existing_project=True,
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
        )
        write_decision = await _authorize_memory_policy(
            ctx=ctx,
            action=MemoryPolicyAction.WRITE,
            memory_scope=request.memory_scope,
            scope_key=request.scope_key,
            surface="raw_remember",
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
            agent_id=request.agent_id,
            project_id=request.project_id,
        )
        await _authorize_project_filter(
            ctx=ctx,
            project_id=request.project_id,
            required_project_role=ProjectRole.VIEWER,
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


@router.post(
    "/reflection/promote",
    response_model=ReflectionPromotionResponse,
    dependencies=[Depends(require_org_role(*_WRITE_ROLES))],
)
async def promote_reflection_candidate(
    request: ReflectionPromotionRequest,
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
