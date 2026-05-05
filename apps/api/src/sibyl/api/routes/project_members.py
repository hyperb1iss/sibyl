"""Project membership endpoints."""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, Request
from pydantic import BaseModel, Field

from sibyl.api.websocket import broadcast_event
from sibyl.auth.dependencies import get_current_org_role, get_current_organization, get_current_user
from sibyl.persistence import organization_runtime
from sibyl_core.auth import AuthOrganization, AuthUser, ProjectRole

router = APIRouter(prefix="/projects/{project_id}/members", tags=["project-members"])


class MemberAddRequest(BaseModel):
    user_id: UUID
    role: ProjectRole = Field(default=ProjectRole.CONTRIBUTOR)


class MemberRoleUpdateRequest(BaseModel):
    role: ProjectRole


class ProjectMembershipSubject(Protocol):
    @property
    def owner_user_id(self) -> UUID | None:
        ...


class ProjectMembershipActor(Protocol):
    @property
    def id(self) -> UUID:
        ...


def _can_manage_members(
    role: ProjectRole | None,
    project: ProjectMembershipSubject,
    user: ProjectMembershipActor,
) -> bool:
    return organization_runtime.can_manage_project_members(role, project, user)


@router.get("")
async def list_members(
    project_id: str,
    user: AuthUser = Depends(get_current_user),
    org: AuthOrganization = Depends(get_current_organization),
    _org_role=Depends(get_current_org_role),
):
    result = await organization_runtime.list_project_members(
        project_id=project_id,
        actor=user,
        org_id=org.id,
    )
    return {"members": result.members, "can_manage": result.can_manage}


@router.post("")
async def add_member(
    request: Request,
    project_id: str,
    body: MemberAddRequest,
    background_tasks: BackgroundTasks,
    user: AuthUser = Depends(get_current_user),
    org: AuthOrganization = Depends(get_current_organization),
    _org_role=Depends(get_current_org_role),
):
    membership = await organization_runtime.add_project_member(
        request=request,
        project_id=project_id,
        actor=user,
        org_id=org.id,
        target_user_id=body.user_id,
        role=body.role,
    )

    background_tasks.add_task(
        broadcast_event,
        "permission_changed",
        {
            "user_id": str(body.user_id),
            "change_type": "project_member_added",
            "project_id": str(project_id),
            "project_role": membership.role.value,
        },
        org_id=str(org.id),
    )

    return {"user_id": str(membership.user_id), "role": membership.role.value}


@router.patch("/{user_id}")
async def update_member_role(
    request: Request,
    project_id: str,
    user_id: UUID,
    body: MemberRoleUpdateRequest,
    background_tasks: BackgroundTasks,
    user: AuthUser = Depends(get_current_user),
    org: AuthOrganization = Depends(get_current_organization),
    _org_role=Depends(get_current_org_role),
):
    membership = await organization_runtime.update_project_member_role(
        request=request,
        project_id=project_id,
        actor=user,
        org_id=org.id,
        target_user_id=user_id,
        role=body.role,
    )

    background_tasks.add_task(
        broadcast_event,
        "permission_changed",
        {
            "user_id": str(user_id),
            "change_type": "project_role_changed",
            "project_id": str(project_id),
            "project_role": membership.role.value,
        },
        org_id=str(org.id),
    )

    return {"user_id": str(membership.user_id), "role": membership.role.value}


@router.delete("/{user_id}")
async def remove_member(
    request: Request,
    project_id: str,
    user_id: UUID,
    background_tasks: BackgroundTasks,
    user: AuthUser = Depends(get_current_user),
    org: AuthOrganization = Depends(get_current_organization),
    _org_role=Depends(get_current_org_role),
):
    await organization_runtime.remove_project_member(
        request=request,
        project_id=project_id,
        actor=user,
        org_id=org.id,
        target_user_id=user_id,
    )

    background_tasks.add_task(
        broadcast_event,
        "permission_changed",
        {
            "user_id": str(user_id),
            "change_type": "project_member_removed",
            "project_id": str(project_id),
        },
        org_id=str(org.id),
    )

    return {"success": True}
