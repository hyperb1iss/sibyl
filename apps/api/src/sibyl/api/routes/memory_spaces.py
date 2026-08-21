"""Memory space and access-preview routes."""

from __future__ import annotations

from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request

from sibyl.api.routes import memory_auth, memory_serialization as serialization
from sibyl.api.schemas import (
    MemorySpaceAccessPreviewRequest,
    MemorySpaceAccessPreviewResponse,
    MemorySpaceCreateRequest,
    MemorySpaceListResponse,
    MemorySpaceMemberCreateRequest,
    MemorySpaceMemberResponse,
    MemorySpaceResponse,
    MemorySpaceUpdateRequest,
)
from sibyl.auth.context import AuthContext
from sibyl.auth.dependencies import get_auth_context, get_current_organization, require_org_role
from sibyl.persistence.auth_runtime import (
    add_memory_space_member,
    create_memory_space,
    get_memory_space,
    list_memory_space_members,
    list_memory_spaces,
    update_memory_space,
)
from sibyl_core.auth import AuthOrganization, OrganizationRole
from sibyl_core.services.memory import (
    preview_memory_access,
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
        spaces=[serialization.memory_space_response(space) for space in spaces],
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
    actor_user_id = serialization.actor_user_uuid(ctx)
    space = await create_memory_space(
        organization_id=org.id,
        created_by_user_id=actor_user_id,
        memory_scope=request.memory_scope,
        scope_key=request.scope_key,
        name=request.name,
        description=request.description,
        metadata=request.metadata,
    )
    return serialization.memory_space_response(space)


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
    return serialization.memory_space_response(space, members=members)


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
    return serialization.memory_space_response(space, members=members)


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
    actor_user_id = serialization.actor_user_uuid(ctx)
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
    return serialization.memory_space_member_response(member)


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
    http_request: Request = memory_auth.REQUEST_AUTO_INJECT_SENTINEL,
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
    await memory_auth.log_memory_audit(
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
    return serialization.access_preview_response(result)
