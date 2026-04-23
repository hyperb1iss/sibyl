"""Legacy request-time auth runtime helpers backed by Postgres."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any
from uuid import UUID

from fastapi import HTTPException
from sqlmodel import col, select

from sibyl.auth.audit import AuditLogger
from sibyl.auth.sessions import SessionManager
from sibyl.auth.users import UserManager
from sibyl.db.connection import get_session
from sibyl.db.models import (
    OrganizationRole,
    Project,
    ProjectMember,
    ProjectRole,
    ProjectVisibility,
    TeamMember,
    TeamProject,
)
from sibyl.db.project_sync import (
    get_postgres_project_by_graph_id,
    sync_project_create,
    sync_project_delete,
    sync_project_update,
)
from sibyl.db.sync import get_graph_projects
from sibyl.persistence.auth_common import UserNotFoundError
from sibyl.persistence.legacy.auth import LegacyAuthContextResolver
from sibyl.persistence.legacy.users import (
    confirm_legacy_password_reset as legacy_confirm_password_reset,
    list_legacy_oauth_connections as legacy_list_oauth_connections,
    remove_legacy_oauth_connection as legacy_remove_oauth_connection,
    request_legacy_password_reset as legacy_request_password_reset,
)

_PROJECT_ROLE_LEVELS: dict[ProjectRole, int] = {
    ProjectRole.VIEWER: 10,
    ProjectRole.CONTRIBUTOR: 20,
    ProjectRole.MAINTAINER: 30,
    ProjectRole.OWNER: 40,
}
_ORG_ADMIN_ROLES = frozenset({OrganizationRole.OWNER, OrganizationRole.ADMIN})


async def resolve_legacy_auth_context(
    *,
    claims: dict[str, Any],
    session: Any | None = None,
):
    if session is not None:
        resolver = LegacyAuthContextResolver.from_session(session)
        return await resolver.resolve(claims)

    async with get_session() as db_session:
        resolver = LegacyAuthContextResolver.from_session(db_session)
        return await resolver.resolve(claims)


def _max_project_role(*roles: ProjectRole | None) -> ProjectRole | None:
    valid = [role for role in roles if role is not None]
    if not valid:
        return None
    return max(valid, key=lambda role: _PROJECT_ROLE_LEVELS[role])


async def _get_effective_project_role(
    session: Any,
    ctx: Any,
    project: Project,
) -> ProjectRole | None:
    if ctx.org_role in _ORG_ADMIN_ROLES:
        return ProjectRole.OWNER

    result = await session.execute(
        select(ProjectMember.role).where(
            ProjectMember.project_id == project.id,
            ProjectMember.user_id == ctx.user.id,
        )
    )
    direct_role = result.scalar_one_or_none()

    result = await session.execute(
        select(TeamProject.role)
        .join(TeamMember, col(TeamMember.team_id) == col(TeamProject.team_id))
        .where(
            TeamProject.project_id == project.id,
            TeamMember.user_id == ctx.user.id,
        )
    )
    team_roles = [row[0] for row in result.all()]
    team_role = _max_project_role(*team_roles) if team_roles else None

    visibility_role = project.default_role if project.visibility == ProjectVisibility.ORG else None
    return _max_project_role(direct_role, team_role, visibility_role)


async def _resolve_project_by_graph_id(
    session: Any,
    org_id: UUID,
    graph_project_id: str,
) -> Project:
    from fastapi import status

    project = await get_postgres_project_by_graph_id(
        session,
        organization_id=org_id,
        graph_project_id=graph_project_id,
    )
    if project is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Project not found: {graph_project_id}",
        )
    return project


async def _get_project_by_id(
    session: Any,
    org_id: UUID,
    project_id: UUID,
) -> Project:
    from fastapi import status

    project = await session.get(Project, project_id)
    if project is None or project.organization_id != org_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Project not found: {project_id}",
        )
    return project


async def _list_accessible_project_graph_ids(
    session: Any,
    ctx: Any,
) -> set[str]:
    if ctx.organization is None:
        return set()

    org_id = ctx.organization.id
    user_id = ctx.user.id
    org_role = ctx.org_role

    count_result = await session.execute(
        select(Project.id).where(Project.organization_id == org_id).limit(1)
    )
    if count_result.first() is None:
        if org_role is None:
            return set()

        graph_projects = await get_graph_projects(str(org_id))
        return {
            graph_id
            for project in graph_projects
            if (graph_id := project.get("id") or project.get("uuid"))
        }

    if org_role in _ORG_ADMIN_ROLES:
        result = await session.execute(
            select(Project.graph_project_id).where(Project.organization_id == org_id)
        )
        return {row[0] for row in result.all()}

    accessible: set[str] = set()

    result = await session.execute(
        select(Project.graph_project_id).where(
            Project.organization_id == org_id,
            Project.visibility == ProjectVisibility.ORG,
        )
    )
    accessible.update(row[0] for row in result.all())

    result = await session.execute(
        select(Project.graph_project_id)
        .join(ProjectMember, col(ProjectMember.project_id) == col(Project.id))
        .where(
            Project.organization_id == org_id,
            ProjectMember.user_id == user_id,
        )
    )
    accessible.update(row[0] for row in result.all())

    result = await session.execute(
        select(Project.graph_project_id)
        .join(TeamProject, col(TeamProject.project_id) == col(Project.id))
        .join(TeamMember, col(TeamMember.team_id) == col(TeamProject.team_id))
        .where(
            Project.organization_id == org_id,
            TeamMember.user_id == user_id,
        )
    )
    accessible.update(row[0] for row in result.all())

    return accessible


async def list_legacy_accessible_project_graph_ids(ctx: Any) -> set[str] | None:
    async with get_session() as session:
        return await _list_accessible_project_graph_ids(session, ctx)


async def resolve_legacy_accessible_project_graph_ids(
    *,
    user_id: str,
    org_id: str,
    scopes: Sequence[str] | None = None,
    api_key_project_ids: Sequence[str] | None = None,
) -> set[str] | None:
    async with get_session() as session:
        resolver = LegacyAuthContextResolver.from_session(session)
        try:
            auth_ctx = await resolver.resolve(
                {
                    "sub": user_id,
                    "org": org_id,
                    "scopes": list(scopes or []),
                }
            )
        except UserNotFoundError:
            return set()
        if auth_ctx.organization is None:
            return set()

        user_accessible = await _list_accessible_project_graph_ids(session, auth_ctx)

    if api_key_project_ids is not None:
        api_key_allowed = set(api_key_project_ids)
        if user_accessible is None:
            return api_key_allowed
        return user_accessible & api_key_allowed

    return user_accessible


async def _verify_entity_project_access(
    session: Any,
    ctx: Any,
    entity_project_id: str | None,
    required_role: ProjectRole,
) -> ProjectRole | None:
    from sibyl.auth.authorization import ProjectAuthorizationError

    if ctx.organization is None:
        raise ProjectAuthorizationError(
            project_id=entity_project_id or "unknown",
            required_role=required_role,
            actual_role=None,
        )

    if entity_project_id is None:
        if ctx.org_role in _ORG_ADMIN_ROLES:
            return ProjectRole.OWNER
        if ctx.org_role is not None and required_role == ProjectRole.VIEWER:
            return ProjectRole.VIEWER
        raise ProjectAuthorizationError(
            project_id="unassigned",
            required_role=required_role,
            actual_role=ProjectRole.VIEWER if ctx.org_role else None,
        )

    project = await get_postgres_project_by_graph_id(
        session,
        organization_id=ctx.organization.id,
        graph_project_id=entity_project_id,
    )

    if project is None:
        if ctx.org_role in _ORG_ADMIN_ROLES:
            return ProjectRole.OWNER
        if ctx.org_role is not None and required_role == ProjectRole.VIEWER:
            return ProjectRole.VIEWER
        raise ProjectAuthorizationError(
            project_id=entity_project_id,
            required_role=required_role,
            actual_role=ProjectRole.VIEWER if ctx.org_role else None,
        )

    effective_role = await _get_effective_project_role(session, ctx, project)

    if effective_role is None:
        raise ProjectAuthorizationError(
            project_id=entity_project_id,
            required_role=required_role,
            actual_role=None,
        )

    if _PROJECT_ROLE_LEVELS[effective_role] < _PROJECT_ROLE_LEVELS[required_role]:
        raise ProjectAuthorizationError(
            project_id=entity_project_id,
            required_role=required_role,
            actual_role=effective_role,
        )

    return effective_role


async def verify_legacy_entity_project_access(
    *,
    ctx: Any,
    entity_project_id: str | None,
    required_role: ProjectRole,
) -> ProjectRole | None:
    async with get_session() as session:
        return await _verify_entity_project_access(
            session,
            ctx,
            entity_project_id,
            required_role,
        )


async def patch_legacy_auth_user(
    *,
    user_id: UUID,
    updates: dict[str, Any],
    organization_id: UUID | None,
    request: Any,
):
    async with get_session() as session:
        manager = UserManager(session)
        user = await manager.get_by_id(user_id)
        if user is None:
            raise HTTPException(status_code=404, detail="User not found")
        if not updates:
            raise HTTPException(status_code=400, detail="No fields to update")

        changes: list[str] = []
        profile_updates: dict[str, Any] = {}
        if "email" in updates:
            profile_updates["email"] = updates["email"]
            changes.append("email")
        if "name" in updates:
            profile_updates["name"] = updates["name"]
            changes.append("name")
        if "avatar_url" in updates:
            profile_updates["avatar_url"] = updates["avatar_url"]
            changes.append("avatar_url")

        try:
            if profile_updates:
                await manager.update_profile(user, **profile_updates)
            if "bio" in updates:
                user.bio = str(updates["bio"]).strip() or None if updates["bio"] is not None else None
                changes.append("bio")
            if "timezone" in updates:
                timezone = updates["timezone"]
                user.timezone = str(timezone).strip() or "UTC" if timezone is not None else "UTC"
                changes.append("timezone")
            if "preferences" in updates:
                preferences = updates["preferences"]
                if not isinstance(preferences, dict):
                    raise ValueError("Preferences must be an object")
                user.preferences = dict(preferences)
                changes.append("preferences")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        if not changes:
            raise HTTPException(status_code=400, detail="No fields to update")

        await AuditLogger(session).log(
            action="user.update_profile",
            user_id=user.id,
            organization_id=organization_id,
            request=request,
            details={"fields": changes},
        )
        return user


async def create_legacy_project_record(
    *,
    organization_id: UUID,
    owner_user_id: UUID,
    graph_project_id: str,
    name: str,
    description: str | None = None,
):
    async with get_session() as session:
        return await sync_project_create(
            session,
            organization_id=organization_id,
            owner_user_id=owner_user_id,
            graph_project_id=graph_project_id,
            name=name,
            description=description,
        )


async def update_legacy_project_record(
    *,
    organization_id: UUID,
    graph_project_id: str,
    name: str | None = None,
    description: str | None = None,
) -> bool:
    async with get_session() as session:
        return await sync_project_update(
            session,
            organization_id=organization_id,
            graph_project_id=graph_project_id,
            name=name,
            description=description,
        )


async def delete_legacy_project_record(
    *,
    organization_id: UUID,
    graph_project_id: str,
) -> bool:
    async with get_session() as session:
        return await sync_project_delete(
            session,
            organization_id=organization_id,
            graph_project_id=graph_project_id,
        )


async def get_legacy_project_record_by_graph_id(
    *,
    organization_id: UUID,
    graph_project_id: str,
):
    async with get_session() as session:
        return await _resolve_project_by_graph_id(
            session,
            organization_id,
            graph_project_id,
        )


async def get_legacy_project_record_by_id(
    *,
    organization_id: UUID,
    project_id: UUID,
):
    async with get_session() as session:
        return await _get_project_by_id(session, organization_id, project_id)


async def list_legacy_user_sessions(
    *,
    user_id: UUID,
    include_expired: bool = False,
):
    async with get_session() as session:
        manager = SessionManager(session)
        return await manager.list_user_sessions(user_id, include_expired=include_expired)


async def revoke_all_legacy_user_sessions(
    *,
    user_id: UUID,
    exclude_token_hash: str | None = None,
) -> int:
    async with get_session() as session:
        manager = SessionManager(session)
        return await manager.revoke_all_sessions(user_id, exclude_token_hash=exclude_token_hash)


async def revoke_legacy_user_session(
    *,
    user_id: UUID,
    session_id: UUID,
) -> bool:
    async with get_session() as session:
        manager = SessionManager(session)
        return await manager.revoke_session(session_id, user_id)


async def request_legacy_password_reset(email: str) -> None:
    await legacy_request_password_reset(email)


async def confirm_legacy_password_reset(token: str, new_password: str) -> None:
    await legacy_confirm_password_reset(token, new_password)


async def list_legacy_oauth_connections(*, user_id: UUID):
    async with get_session() as session:
        return await legacy_list_oauth_connections(session, user_id)


async def remove_legacy_oauth_connection(
    *,
    user_id: UUID,
    connection_id: UUID,
):
    async with get_session() as session:
        return await legacy_remove_oauth_connection(
            session,
            user_id=user_id,
            connection_id=connection_id,
        )
