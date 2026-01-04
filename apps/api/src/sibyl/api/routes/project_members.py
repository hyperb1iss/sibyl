"""Project membership endpoints.

Accepts both graph project IDs (project_abc123) and Postgres UUIDs.
Graph IDs are resolved to Postgres projects internally.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from sibyl.api.websocket import broadcast_event
from sibyl.auth.audit import AuditLogger
from sibyl.auth.dependencies import get_current_org_role, get_current_organization, get_current_user
from sibyl.db.connection import get_session_dependency
from sibyl.db.models import Organization, Project, ProjectMember, ProjectRole, User

router = APIRouter(prefix="/projects/{project_id}/members", tags=["project-members"])


class MemberAddRequest(BaseModel):
    user_id: UUID
    role: ProjectRole = Field(default=ProjectRole.CONTRIBUTOR)


class MemberRoleUpdateRequest(BaseModel):
    role: ProjectRole


async def _resolve_project(
    project_id: str,
    org: Organization,
    session: AsyncSession,
) -> Project:
    """Resolve project by graph ID or Postgres UUID.

    Args:
        project_id: Either a graph ID (project_abc123) or Postgres UUID
        org: Organization context
        session: Database session

    Returns:
        Project model

    Raises:
        HTTPException: 404 if project not found or doesn't belong to org
    """
    project: Project | None = None

    # Try as graph ID first (most common from frontend)
    if project_id.startswith("project_"):
        result = await session.execute(
            select(Project).where(
                Project.organization_id == org.id,
                Project.graph_project_id == project_id,
            )
        )
        project = result.scalar_one_or_none()
    else:
        # Try as UUID
        try:
            uuid_id = UUID(project_id)
            project = await session.get(Project, uuid_id)
            if project and project.organization_id != org.id:
                project = None
        except ValueError:
            pass  # Not a valid UUID

    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")

    return project


async def _get_project_and_user_role(
    *,
    project_id: str,
    user: User,
    org: Organization,
    session: AsyncSession,
) -> tuple[Project, ProjectRole | None]:
    """Get project and current user's role in it."""
    project = await _resolve_project(project_id, org, session)

    # Check if user is project owner
    if project.owner_user_id == user.id:
        return project, ProjectRole.OWNER

    # Check direct membership
    result = await session.execute(
        select(ProjectMember).where(
            ProjectMember.project_id == project_id,
            ProjectMember.user_id == user.id,
        )
    )
    member = result.scalar_one_or_none()
    if member:
        return project, member.role

    return project, None


def _can_manage_members(role: ProjectRole | None, project: Project, user: User) -> bool:
    """Check if user can add/remove/update members."""
    # Project owner can always manage
    if project.owner_user_id == user.id:
        return True
    # OWNER and MAINTAINER roles can manage
    return role in {ProjectRole.OWNER, ProjectRole.MAINTAINER}


@router.get("")
async def list_members(
    project_id: str,
    user: User = Depends(get_current_user),
    org: Organization = Depends(get_current_organization),
    _org_role=Depends(get_current_org_role),  # Verifies user is org member
    session: AsyncSession = Depends(get_session_dependency),
):
    """List all members of a project.

    Accepts graph project ID (project_abc123) or Postgres UUID.
    """
    project, user_role = await _get_project_and_user_role(
        project_id=project_id, user=user, org=org, session=session
    )

    # Get all direct members (use resolved project.id)
    result = await session.execute(
        select(ProjectMember, User)
        .join(User, User.id == ProjectMember.user_id)
        .where(ProjectMember.project_id == project.id)
    )

    members = []

    # Add project owner first if they exist
    if project.owner_user_id:
        owner = await session.get(User, project.owner_user_id)
        if owner:
            members.append(
                {
                    "user": {
                        "id": str(owner.id),
                        "email": owner.email,
                        "name": owner.name,
                        "avatar_url": owner.avatar_url,
                    },
                    "role": ProjectRole.OWNER.value,
                    "is_owner": True,
                    "created_at": project.created_at,
                }
            )

    # Add other members
    for membership, member_user in result.all():
        # Skip if this is the owner (already added)
        if member_user.id == project.owner_user_id:
            continue
        members.append(
            {
                "user": {
                    "id": str(member_user.id),
                    "email": member_user.email,
                    "name": member_user.name,
                    "avatar_url": member_user.avatar_url,
                },
                "role": membership.role.value,
                "is_owner": False,
                "created_at": membership.created_at,
            }
        )

    return {
        "members": members,
        "can_manage": _can_manage_members(user_role, project, user),
    }


@router.post("")
async def add_member(
    request: Request,
    project_id: str,
    body: MemberAddRequest,
    background_tasks: BackgroundTasks,
    user: User = Depends(get_current_user),
    org: Organization = Depends(get_current_organization),
    _org_role=Depends(get_current_org_role),  # Verifies user is org member
    session: AsyncSession = Depends(get_session_dependency),
):
    """Add a member to a project.

    Accepts graph project ID (project_abc123) or Postgres UUID.
    """
    project, user_role = await _get_project_and_user_role(
        project_id=project_id, user=user, org=org, session=session
    )

    if not _can_manage_members(user_role, project, user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")

    # Verify target user exists
    target_user = await session.get(User, body.user_id)
    if target_user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    # Check if already a member (use resolved project.id)
    existing = await session.execute(
        select(ProjectMember).where(
            ProjectMember.project_id == project.id,
            ProjectMember.user_id == body.user_id,
        )
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="User is already a member")

    # Create membership (use resolved project.id)
    membership = ProjectMember(
        organization_id=org.id,
        project_id=project.id,
        user_id=body.user_id,
        role=body.role,
    )
    session.add(membership)
    await session.commit()
    await session.refresh(membership)

    await AuditLogger(session).log(
        action="project.member.add",
        user_id=user.id,
        organization_id=org.id,
        request=request,
        details={
            "project_id": str(project_id),
            "target_user_id": str(body.user_id),
            "role": membership.role.value,
        },
    )

    # Broadcast permission change
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
    user: User = Depends(get_current_user),
    org: Organization = Depends(get_current_organization),
    _org_role=Depends(get_current_org_role),  # Verifies user is org member
    session: AsyncSession = Depends(get_session_dependency),
):
    """Update a member's role in a project.

    Accepts graph project ID (project_abc123) or Postgres UUID.
    """
    project, user_role = await _get_project_and_user_role(
        project_id=project_id, user=user, org=org, session=session
    )

    if not _can_manage_members(user_role, project, user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")

    # Cannot change project owner's role
    if user_id == project.owner_user_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot change project owner's role",
        )

    # Find and update membership (use resolved project.id)
    result = await session.execute(
        select(ProjectMember).where(
            ProjectMember.project_id == project.id,
            ProjectMember.user_id == user_id,
        )
    )
    membership = result.scalar_one_or_none()
    if membership is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Member not found")

    membership.role = body.role
    session.add(membership)
    await session.commit()
    await session.refresh(membership)

    await AuditLogger(session).log(
        action="project.member.update_role",
        user_id=user.id,
        organization_id=org.id,
        request=request,
        details={
            "project_id": str(project_id),
            "target_user_id": str(user_id),
            "role": membership.role.value,
        },
    )

    # Broadcast permission change
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
    user: User = Depends(get_current_user),
    org: Organization = Depends(get_current_organization),
    _org_role=Depends(get_current_org_role),  # Verifies user is org member
    session: AsyncSession = Depends(get_session_dependency),
):
    """Remove a member from a project.

    Accepts graph project ID (project_abc123) or Postgres UUID.
    """
    project, user_role = await _get_project_and_user_role(
        project_id=project_id, user=user, org=org, session=session
    )

    # Cannot remove project owner
    if user_id == project.owner_user_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot remove project owner",
        )

    # Users can remove themselves, otherwise need manage permission
    if user.id != user_id and not _can_manage_members(user_role, project, user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")

    # Find and delete membership (use resolved project.id)
    result = await session.execute(
        select(ProjectMember).where(
            ProjectMember.project_id == project.id,
            ProjectMember.user_id == user_id,
        )
    )
    membership = result.scalar_one_or_none()
    if membership is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Member not found")

    await session.delete(membership)
    await session.commit()

    await AuditLogger(session).log(
        action="project.member.remove",
        user_id=user.id,
        organization_id=org.id,
        request=request,
        details={"project_id": str(project_id), "target_user_id": str(user_id)},
    )

    # Broadcast permission change
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
