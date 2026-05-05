"""Legacy project membership adapters backed by the current relational runtime."""

from __future__ import annotations

from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col, select
from starlette.requests import Request

from sibyl.auth.audit import AuditLogger
from sibyl.db.connection import get_session
from sibyl.db.models import Project, ProjectMember, User
from sibyl.persistence.organization_common import (
    ProjectMemberChange,
    ProjectMembersResult,
    can_manage_project_members,
)
from sibyl_core.auth import ProjectRole

LegacyProjectMembersResult = ProjectMembersResult
LegacyProjectMemberChange = ProjectMemberChange


async def _resolve_legacy_project(
    project_id: str,
    *,
    org_id: UUID,
    session: AsyncSession,
) -> Project:
    project: Project | None = None

    if project_id.startswith("project_"):
        result = await session.execute(
            select(Project).where(
                col(Project.organization_id) == org_id,
                col(Project.graph_project_id) == project_id,
            )
        )
        project = result.scalar_one_or_none()
    else:
        try:
            uuid_id = UUID(project_id)
            project = await session.get(Project, uuid_id)
            if project and project.organization_id != org_id:
                project = None
        except ValueError:
            pass

    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")

    return project


async def _get_legacy_project_and_user_role(
    *,
    project_id: str,
    user_id: UUID,
    org_id: UUID,
    session: AsyncSession,
) -> tuple[Project, ProjectRole | None]:
    project = await _resolve_legacy_project(project_id, org_id=org_id, session=session)

    if project.owner_user_id == user_id:
        return project, ProjectRole.OWNER

    result = await session.execute(
        select(ProjectMember).where(
            col(ProjectMember.project_id) == project.id,
            col(ProjectMember.user_id) == user_id,
        )
    )
    member = result.scalar_one_or_none()
    if member:
        return project, member.role

    return project, None


async def get_legacy_project_and_user_role(
    *,
    project_id: str,
    user_id: UUID,
    org_id: UUID,
) -> tuple[Project, ProjectRole | None]:
    async with get_session() as session:
        return await _get_legacy_project_and_user_role(
            project_id=project_id,
            user_id=user_id,
            org_id=org_id,
            session=session,
        )


async def list_legacy_project_members(
    *,
    project_id: str,
    actor: User,
    org_id: UUID,
) -> LegacyProjectMembersResult:
    async with get_session() as session:
        project, user_role = await _get_legacy_project_and_user_role(
            project_id=project_id,
            user_id=actor.id,
            org_id=org_id,
            session=session,
        )

        result = await session.execute(
            select(ProjectMember, User)
            .join(User, col(User.id) == col(ProjectMember.user_id))
            .where(col(ProjectMember.project_id) == project.id)
        )

        members: list[dict[str, object]] = []

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

        for membership, member_user in result.all():
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

        return LegacyProjectMembersResult(
            members=members,
            can_manage=can_manage_project_members(user_role, project, actor),
        )


async def add_legacy_project_member(
    *,
    request: Request,
    project_id: str,
    actor: User,
    org_id: UUID,
    target_user_id: UUID,
    role: ProjectRole,
) -> LegacyProjectMemberChange:
    async with get_session() as session:
        project, user_role = await _get_legacy_project_and_user_role(
            project_id=project_id,
            user_id=actor.id,
            org_id=org_id,
            session=session,
        )

        if not can_manage_project_members(user_role, project, actor):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")

        target_user = await session.get(User, target_user_id)
        if target_user is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

        existing = await session.execute(
            select(ProjectMember).where(
                col(ProjectMember.project_id) == project.id,
                col(ProjectMember.user_id) == target_user_id,
            )
        )
        if existing.scalar_one_or_none():
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="User is already a member",
            )

        membership = ProjectMember(
            organization_id=org_id,
            project_id=project.id,
            user_id=target_user_id,
            role=role,
        )
        session.add(membership)
        await session.flush()

        await AuditLogger(session).log(
            action="project.member.add",
            user_id=actor.id,
            organization_id=org_id,
            request=request,
            details={
                "project_id": str(project_id),
                "target_user_id": str(target_user_id),
                "role": membership.role.value,
            },
        )

        return LegacyProjectMemberChange(
            org_id=org_id,
            project_db_id=project.id,
            user_id=membership.user_id,
            role=membership.role,
        )


async def update_legacy_project_member_role(
    *,
    request: Request,
    project_id: str,
    actor: User,
    org_id: UUID,
    target_user_id: UUID,
    role: ProjectRole,
) -> LegacyProjectMemberChange:
    async with get_session() as session:
        project, user_role = await _get_legacy_project_and_user_role(
            project_id=project_id,
            user_id=actor.id,
            org_id=org_id,
            session=session,
        )

        if not can_manage_project_members(user_role, project, actor):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")

        if target_user_id == project.owner_user_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot change project owner's role",
            )

        result = await session.execute(
            select(ProjectMember).where(
                col(ProjectMember.project_id) == project.id,
                col(ProjectMember.user_id) == target_user_id,
            )
        )
        membership = result.scalar_one_or_none()
        if membership is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Member not found")

        membership.role = role
        session.add(membership)
        await session.flush()

        await AuditLogger(session).log(
            action="project.member.update_role",
            user_id=actor.id,
            organization_id=org_id,
            request=request,
            details={
                "project_id": str(project_id),
                "target_user_id": str(target_user_id),
                "role": membership.role.value,
            },
        )

        return LegacyProjectMemberChange(
            org_id=org_id,
            project_db_id=project.id,
            user_id=membership.user_id,
            role=membership.role,
        )


async def remove_legacy_project_member(
    *,
    request: Request,
    project_id: str,
    actor: User,
    org_id: UUID,
    target_user_id: UUID,
) -> LegacyProjectMemberChange:
    async with get_session() as session:
        project, user_role = await _get_legacy_project_and_user_role(
            project_id=project_id,
            user_id=actor.id,
            org_id=org_id,
            session=session,
        )

        if target_user_id == project.owner_user_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot remove project owner",
            )

        if actor.id != target_user_id and not can_manage_project_members(
            user_role,
            project,
            actor,
        ):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")

        result = await session.execute(
            select(ProjectMember).where(
                col(ProjectMember.project_id) == project.id,
                col(ProjectMember.user_id) == target_user_id,
            )
        )
        membership = result.scalar_one_or_none()
        if membership is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Member not found")

        await session.delete(membership)

        await AuditLogger(session).log(
            action="project.member.remove",
            user_id=actor.id,
            organization_id=org_id,
            request=request,
            details={"project_id": str(project_id), "target_user_id": str(target_user_id)},
        )

        return LegacyProjectMemberChange(
            org_id=org_id,
            project_db_id=project.id,
            user_id=target_user_id,
        )


get_project_and_user_role = get_legacy_project_and_user_role
list_project_members = list_legacy_project_members
add_project_member = add_legacy_project_member
update_project_member_role = update_legacy_project_member_role
remove_project_member = remove_legacy_project_member
