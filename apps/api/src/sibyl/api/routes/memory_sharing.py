"""Memory sharing preview and mutation routes."""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request

from sibyl.api.decorators import handle_workflow_errors
from sibyl.api.routes import memory_auth, memory_serialization as serialization
from sibyl.api.schemas import (
    MemorySharePreviewRequest,
    MemorySharePreviewResponse,
    MemoryShareRequest,
    MemoryShareResponse,
)
from sibyl.auth.context import AuthContext
from sibyl.auth.dependencies import get_auth_context, get_current_organization, require_org_role
from sibyl_core.auth import AuthOrganization, OrganizationRole
from sibyl_core.services.memory import (
    preview_memory_share,
    share_memory,
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


@router.post(
    "/share/preview",
    response_model=MemorySharePreviewResponse,
    dependencies=[Depends(require_org_role(*_WRITE_ROLES))],
)
@handle_workflow_errors("preview_memory_share")
async def preview_memory_share_route(
    request: MemorySharePreviewRequest,
    http_request: Request = memory_auth.REQUEST_AUTO_INJECT_SENTINEL,
    org: AuthOrganization = Depends(get_current_organization),
    ctx: AuthContext = Depends(get_auth_context),
) -> MemorySharePreviewResponse:
    """Preview memory sharing without enabling a share write."""
    principal_id = ctx.user_id
    if not principal_id:
        raise HTTPException(status_code=401, detail="Not authenticated")

    accessible_projects = await memory_auth.accessible_projects_for_share_preview(
        ctx=ctx,
        request=request,
        http_request=http_request,
    )
    accessible_teams = await memory_auth.accessible_teams_for_share(ctx=ctx)
    await memory_auth.authorize_share_api_key_scopes(
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
    await memory_auth.log_memory_audit(
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
    return serialization.share_preview_response(result)


@router.post(
    "/share",
    response_model=MemoryShareResponse,
    dependencies=[Depends(require_org_role(*_WRITE_ROLES))],
)
@handle_workflow_errors("share_memory")
async def share_memory_route(
    request: MemoryShareRequest,
    http_request: Request = memory_auth.REQUEST_AUTO_INJECT_SENTINEL,
    org: AuthOrganization = Depends(get_current_organization),
    ctx: AuthContext = Depends(get_auth_context),
) -> MemoryShareResponse:
    """Apply same-org memory sharing through promotion-backed native writes."""
    principal_id = ctx.user_id
    if not principal_id:
        raise HTTPException(status_code=401, detail="Not authenticated")

    accessible_projects = await memory_auth.accessible_projects_for_share_preview(
        ctx=ctx,
        request=request,
        http_request=http_request,
    )
    accessible_teams = await memory_auth.accessible_teams_for_share(ctx=ctx)
    await memory_auth.authorize_share_api_key_scopes(
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
    audit_id = await memory_auth.log_memory_audit(
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
    return serialization.share_response(result, audit_event_ids=audit_event_ids)
