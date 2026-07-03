"""Team memory control-plane endpoints."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from sibyl.auth.context import AuthContext
from sibyl.auth.dependencies import get_auth_context, get_current_organization, require_org_role
from sibyl.persistence.auth_runtime import (
    add_team_member_record as runtime_add_team_member,
    create_team_record as runtime_create_team,
    delete_team_record as runtime_delete_team,
    get_team_record as runtime_get_team,
    link_team_project_record as runtime_link_team_project,
    list_team_records as runtime_list_teams,
    remove_team_member_record as runtime_remove_team_member,
    unlink_team_project_record as runtime_unlink_team_project,
    update_team_record as runtime_update_team,
)
from sibyl_core.auth import AuthOrganization, OrganizationRole, ProjectRole

router = APIRouter(prefix="/teams", tags=["teams"])
_ADMIN_ROLES = (OrganizationRole.OWNER, OrganizationRole.ADMIN)


class TeamCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    slug: str | None = Field(default=None, max_length=64)
    description: str | None = Field(default=None, max_length=2000)


class TeamUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    slug: str | None = Field(default=None, max_length=64)
    description: str | None = Field(default=None, max_length=2000)


class TeamMemberCreateRequest(BaseModel):
    user_id: UUID
    role: OrganizationRole = Field(default=OrganizationRole.MEMBER)


class TeamProjectLinkRequest(BaseModel):
    project_id: str = Field(min_length=1)
    role: ProjectRole = Field(default=ProjectRole.CONTRIBUTOR)


class TeamMemberResponse(BaseModel):
    id: str
    team_id: str
    user_id: str
    role: str
    joined_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class TeamProjectResponse(BaseModel):
    id: str
    organization_id: str
    team_id: str
    project_id: str
    graph_project_id: str | None = None
    role: ProjectRole
    created_at: datetime | None = None
    updated_at: datetime | None = None


class TeamResponse(BaseModel):
    id: str
    organization_id: str
    name: str
    slug: str
    description: str | None = None
    is_default: bool = False
    graph_entity_id: str | None = None
    memory_space_id: str | None = None
    memory_scope_key: str
    created_at: datetime | None = None
    updated_at: datetime | None = None
    members: list[TeamMemberResponse] = Field(default_factory=list)
    projects: list[TeamProjectResponse] = Field(default_factory=list)


class TeamListResponse(BaseModel):
    teams: list[TeamResponse] = Field(default_factory=list)


def _actor_user_uuid(ctx: AuthContext) -> UUID:
    if not ctx.user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        return UUID(str(ctx.user_id))
    except ValueError as exc:
        raise HTTPException(status_code=401, detail="invalid_actor") from exc


def _team_member_response(member: Any) -> TeamMemberResponse:
    return TeamMemberResponse(
        id=str(member.id),
        team_id=str(member.team_id),
        user_id=str(member.user_id),
        role=str(member.role),
        joined_at=getattr(member, "joined_at", None),
        created_at=getattr(member, "created_at", None),
        updated_at=getattr(member, "updated_at", None),
    )


def _team_project_response(project: Any) -> TeamProjectResponse:
    return TeamProjectResponse(
        id=str(project.id),
        organization_id=str(project.organization_id),
        team_id=str(project.team_id),
        project_id=str(project.project_id),
        graph_project_id=getattr(project, "graph_project_id", None),
        role=ProjectRole(str(project.role)),
        created_at=getattr(project, "created_at", None),
        updated_at=getattr(project, "updated_at", None),
    )


def _team_response(team: Any) -> TeamResponse:
    return TeamResponse(
        id=str(team.id),
        organization_id=str(team.organization_id),
        name=str(team.name),
        slug=str(team.slug),
        description=getattr(team, "description", None),
        is_default=bool(getattr(team, "is_default", False)),
        graph_entity_id=getattr(team, "graph_entity_id", None),
        memory_space_id=(
            str(team.memory_space_id) if getattr(team, "memory_space_id", None) else None
        ),
        memory_scope_key=str(getattr(team, "memory_scope_key", None) or team.id),
        created_at=getattr(team, "created_at", None),
        updated_at=getattr(team, "updated_at", None),
        members=[_team_member_response(member) for member in getattr(team, "members", [])],
        projects=[_team_project_response(project) for project in getattr(team, "projects", [])],
    )


@router.get(
    "",
    response_model=TeamListResponse,
    dependencies=[Depends(require_org_role(*_ADMIN_ROLES))],
)
async def list_teams(
    org: AuthOrganization = Depends(get_current_organization),
) -> TeamListResponse:
    """List teams in the current organization."""
    teams = await runtime_list_teams(organization_id=org.id)
    return TeamListResponse(teams=[_team_response(team) for team in teams])


@router.post(
    "",
    response_model=TeamResponse,
    dependencies=[Depends(require_org_role(*_ADMIN_ROLES))],
)
async def create_team(
    request: TeamCreateRequest,
    org: AuthOrganization = Depends(get_current_organization),
    ctx: AuthContext = Depends(get_auth_context),
) -> TeamResponse:
    """Create a team and its canonical team memory space."""
    team = await runtime_create_team(
        organization_id=org.id,
        created_by_user_id=_actor_user_uuid(ctx),
        name=request.name,
        slug=request.slug,
        description=request.description,
    )
    return _team_response(team)


@router.get(
    "/{team_id}",
    response_model=TeamResponse,
    dependencies=[Depends(require_org_role(*_ADMIN_ROLES))],
)
async def get_team(
    team_id: str,
    org: AuthOrganization = Depends(get_current_organization),
) -> TeamResponse:
    """Inspect a team, its members, project links, and memory-space binding."""
    team = await runtime_get_team(organization_id=org.id, team_ref=team_id)
    return _team_response(team)


@router.patch(
    "/{team_id}",
    response_model=TeamResponse,
    dependencies=[Depends(require_org_role(*_ADMIN_ROLES))],
)
async def update_team(
    team_id: str,
    request: TeamUpdateRequest,
    org: AuthOrganization = Depends(get_current_organization),
) -> TeamResponse:
    """Update team metadata."""
    team = await runtime_update_team(
        organization_id=org.id,
        team_ref=team_id,
        name=request.name,
        slug=request.slug,
        description=request.description,
    )
    return _team_response(team)


@router.delete(
    "/{team_id}",
    dependencies=[Depends(require_org_role(*_ADMIN_ROLES))],
)
async def delete_team(
    team_id: str,
    org: AuthOrganization = Depends(get_current_organization),
) -> dict[str, bool]:
    """Delete a team control-plane record and its team memory-space binding."""
    await runtime_delete_team(organization_id=org.id, team_ref=team_id)
    return {"success": True}


@router.post(
    "/{team_id}/members",
    response_model=TeamMemberResponse,
    dependencies=[Depends(require_org_role(*_ADMIN_ROLES))],
)
async def add_team_member(
    team_id: str,
    request: TeamMemberCreateRequest,
    org: AuthOrganization = Depends(get_current_organization),
) -> TeamMemberResponse:
    """Add or update a team member."""
    member = await runtime_add_team_member(
        organization_id=org.id,
        team_ref=team_id,
        user_id=request.user_id,
        role=request.role.value,
    )
    return _team_member_response(member)


@router.delete(
    "/{team_id}/members/{user_id}",
    dependencies=[Depends(require_org_role(*_ADMIN_ROLES))],
)
async def remove_team_member(
    team_id: str,
    user_id: UUID,
    org: AuthOrganization = Depends(get_current_organization),
) -> dict[str, bool]:
    """Remove a team member."""
    await runtime_remove_team_member(
        organization_id=org.id,
        team_ref=team_id,
        user_id=user_id,
    )
    return {"success": True}


@router.post(
    "/{team_id}/projects",
    response_model=TeamProjectResponse,
    dependencies=[Depends(require_org_role(*_ADMIN_ROLES))],
)
async def link_team_project(
    team_id: str,
    request: TeamProjectLinkRequest,
    org: AuthOrganization = Depends(get_current_organization),
) -> TeamProjectResponse:
    """Grant a team a project role."""
    project = await runtime_link_team_project(
        organization_id=org.id,
        team_ref=team_id,
        project_ref=request.project_id,
        role=request.role,
    )
    return _team_project_response(project)


@router.delete(
    "/{team_id}/projects/{project_id}",
    dependencies=[Depends(require_org_role(*_ADMIN_ROLES))],
)
async def unlink_team_project(
    team_id: str,
    project_id: str,
    org: AuthOrganization = Depends(get_current_organization),
) -> dict[str, bool]:
    """Remove a team's project grant."""
    await runtime_unlink_team_project(
        organization_id=org.id,
        team_ref=team_id,
        project_ref=project_id,
    )
    return {"success": True}
