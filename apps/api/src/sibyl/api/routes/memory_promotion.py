"""Memory promotion preview and mutation routes."""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request

from sibyl.api.decorators import handle_workflow_errors
from sibyl.api.routes import memory_auth, memory_serialization as serialization
from sibyl.api.schemas import (
    ReflectionPromotionPreviewResponse,
    ReflectionPromotionRequest,
    ReflectionPromotionResponse,
)
from sibyl.auth.context import AuthContext
from sibyl.auth.dependencies import get_auth_context, get_current_organization, require_org_role
from sibyl_core.auth import AuthOrganization, OrganizationRole
from sibyl_core.services.memory import (
    preview_raw_memory_promotion,
    preview_reflection_candidate_promotion,
    promote_raw_memory,
    promote_reflection_candidate_review,
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


preview_router = APIRouter(prefix="/memory", tags=["memory"])
mutation_router = APIRouter(prefix="/memory", tags=["memory"])


@preview_router.post(
    "/reflection/promote/preview",
    response_model=ReflectionPromotionPreviewResponse,
    dependencies=[Depends(require_org_role(*_WRITE_ROLES))],
)
@handle_workflow_errors("preview_reflection_promotion", id_param="candidate_id")
async def preview_reflection_promotion(
    request: ReflectionPromotionRequest,
    http_request: Request = memory_auth.REQUEST_AUTO_INJECT_SENTINEL,
    org: AuthOrganization = Depends(get_current_organization),
    ctx: AuthContext = Depends(get_auth_context),
) -> ReflectionPromotionPreviewResponse:
    """Preview a reflection promotion without writing native memory."""
    principal_id = ctx.user_id
    if not principal_id:
        raise HTTPException(status_code=401, detail="Not authenticated")

    accessible_projects = await memory_auth.accessible_projects_for_promotion(
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
    await memory_auth.log_memory_audit(
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
    return serialization.promotion_preview_response(result)


@preview_router.post(
    "/promote/preview",
    response_model=ReflectionPromotionPreviewResponse,
    dependencies=[Depends(require_org_role(*_WRITE_ROLES))],
)
@handle_workflow_errors("preview_memory_promotion", id_param="candidate_id")
async def preview_memory_promotion(
    request: ReflectionPromotionRequest,
    http_request: Request = memory_auth.REQUEST_AUTO_INJECT_SENTINEL,
    org: AuthOrganization = Depends(get_current_organization),
    ctx: AuthContext = Depends(get_auth_context),
) -> ReflectionPromotionPreviewResponse:
    """Preview promotion for a reflection candidate or imported raw memory."""
    principal_id = ctx.user_id
    if not principal_id:
        raise HTTPException(status_code=401, detail="Not authenticated")

    accessible_projects = await memory_auth.accessible_projects_for_promotion(
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
        await memory_auth.authorize_raw_promotion_api_key_scopes(
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
    await memory_auth.log_memory_audit(
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
    return serialization.promotion_preview_response(result)


@mutation_router.post(
    "/reflection/promote",
    response_model=ReflectionPromotionResponse,
    dependencies=[Depends(require_org_role(*_WRITE_ROLES))],
)
async def promote_reflection_candidate(
    request: ReflectionPromotionRequest,
    http_request: Request = memory_auth.REQUEST_AUTO_INJECT_SENTINEL,
    org: AuthOrganization = Depends(get_current_organization),
    ctx: AuthContext = Depends(get_auth_context),
) -> ReflectionPromotionResponse:
    """Promote a reviewed reflection candidate into native Surreal memory."""
    principal_id = ctx.user_id
    if not principal_id:
        raise HTTPException(status_code=401, detail="Not authenticated")

    try:
        accessible_projects = await memory_auth.accessible_projects_for_promotion(
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
        await memory_auth.log_memory_audit(
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
            policy_allowed=memory_auth.promotion_policy_allowed(result),
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
        return serialization.promotion_response(result)
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


@mutation_router.post(
    "/promote",
    response_model=ReflectionPromotionResponse,
    dependencies=[Depends(require_org_role(*_WRITE_ROLES))],
)
async def promote_memory(
    request: ReflectionPromotionRequest,
    http_request: Request = memory_auth.REQUEST_AUTO_INJECT_SENTINEL,
    org: AuthOrganization = Depends(get_current_organization),
    ctx: AuthContext = Depends(get_auth_context),
) -> ReflectionPromotionResponse:
    """Promote a reflection candidate or imported raw memory."""
    principal_id = ctx.user_id
    if not principal_id:
        raise HTTPException(status_code=401, detail="Not authenticated")

    try:
        accessible_projects = await memory_auth.accessible_projects_for_promotion(
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
            await memory_auth.authorize_raw_promotion_api_key_scopes(
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
        await memory_auth.log_memory_audit(
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
            policy_allowed=memory_auth.promotion_policy_allowed(result),
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
        return serialization.promotion_response(result)
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
