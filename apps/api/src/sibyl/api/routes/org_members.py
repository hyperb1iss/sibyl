"""Organization membership endpoints."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, Request
from pydantic import BaseModel, Field

from sibyl.api.websocket import broadcast_event
from sibyl.auth.dependencies import get_current_user
from sibyl.persistence import organization_runtime
from sibyl_core.auth import AuthUser, OrganizationRole

router = APIRouter(prefix="/orgs/{slug}/members", tags=["org-members"])


class MemberAddRequest(BaseModel):
    user_id: UUID
    role: OrganizationRole = Field(default=OrganizationRole.MEMBER)


class MemberRoleUpdateRequest(BaseModel):
    role: OrganizationRole


class MemberHandleUpdateRequest(BaseModel):
    handle: str = Field(description="New handle; the member's inbox becomes `@<handle>`")


@router.get("")
async def list_members(
    slug: str,
    user: AuthUser = Depends(get_current_user),
):
    return {"members": await organization_runtime.list_org_members(slug=slug, actor_id=user.id)}


@router.post("")
async def add_member(
    request: Request,
    slug: str,
    body: MemberAddRequest,
    background_tasks: BackgroundTasks,
    user: AuthUser = Depends(get_current_user),
):
    membership = await organization_runtime.add_org_member(
        slug=slug,
        actor_id=user.id,
        target_user_id=body.user_id,
        role=body.role,
        request=request,
    )

    background_tasks.add_task(
        broadcast_event,
        "permission_changed",
        {
            "user_id": str(body.user_id),
            "change_type": "org_member_added",
            "org_role": membership.role.value,
        },
        org_id=str(membership.org_id),
    )

    return {"user_id": str(membership.user_id), "role": membership.role.value}


@router.patch("/{user_id}")
async def update_member_role(
    request: Request,
    slug: str,
    user_id: UUID,
    body: MemberRoleUpdateRequest,
    background_tasks: BackgroundTasks,
    user: AuthUser = Depends(get_current_user),
):
    membership = await organization_runtime.update_org_member_role(
        slug=slug,
        actor_id=user.id,
        target_user_id=user_id,
        role=body.role,
        request=request,
    )

    background_tasks.add_task(
        broadcast_event,
        "permission_changed",
        {
            "user_id": str(user_id),
            "change_type": "org_role_changed",
            "org_role": membership.role.value,
        },
        org_id=str(membership.org_id),
    )

    return {"user_id": str(membership.user_id), "role": membership.role.value}


@router.put("/{user_id}/handle")
async def update_member_handle(
    request: Request,
    slug: str,
    user_id: UUID,
    body: MemberHandleUpdateRequest,
    user: AuthUser = Depends(get_current_user),
):
    """Change a member's handle, and so their inbox; owners and admins for anyone, members for
    themselves. Messages already in the old inbox stay there."""
    changed = await organization_runtime.update_org_member_handle(
        slug=slug,
        actor_id=user.id,
        target_user_id=user_id,
        handle=body.handle,
        request=request,
    )
    return {
        "user_id": str(changed.user_id),
        "handle": changed.handle,
        "inbox": f"@{changed.handle}",
    }


@router.delete("/{user_id}")
async def remove_member(
    request: Request,
    slug: str,
    user_id: UUID,
    background_tasks: BackgroundTasks,
    user: AuthUser = Depends(get_current_user),
):
    membership = await organization_runtime.remove_org_member(
        slug=slug,
        actor_id=user.id,
        target_user_id=user_id,
        request=request,
    )

    background_tasks.add_task(
        broadcast_event,
        "permission_changed",
        {
            "user_id": str(user_id),
            "change_type": "org_member_removed",
        },
        org_id=str(membership.org_id),
    )

    return {"success": True}
