"""Project-level authorization module.

This module implements project RBAC on top of the existing org RBAC.
It provides:
- Resolution of graph project IDs to Postgres Project rows
- Effective role calculation (org role override + direct membership + team grants)
- FastAPI dependencies for route protection

Inheritance rules:
- Org owner/admin: implicit project_owner on all projects
- Org member/viewer: access determined by project visibility + explicit grants
"""

from collections.abc import Callable
from typing import Any
from uuid import UUID

import structlog
from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sibyl.auth.context import AuthContext
from sibyl.auth.dependencies import get_auth_context
from sibyl.db.connection import get_session_dependency
from sibyl.db.models import (
    OrganizationRole,
    Project,
    ProjectMember,
    ProjectRole,
    ProjectVisibility,
    TeamMember,
    TeamProject,
)

log = structlog.get_logger()


# =============================================================================
# Role Hierarchy
# =============================================================================

# Map project roles to permission levels (higher = more access)
PROJECT_ROLE_LEVELS: dict[ProjectRole, int] = {
    ProjectRole.VIEWER: 10,
    ProjectRole.CONTRIBUTOR: 20,
    ProjectRole.MAINTAINER: 30,
    ProjectRole.OWNER: 40,
}

# Org roles that grant implicit project_owner
ORG_ADMIN_ROLES = frozenset({OrganizationRole.OWNER, OrganizationRole.ADMIN})


def _max_role(*roles: ProjectRole | None) -> ProjectRole | None:
    """Return the highest-privilege role from the given roles."""
    valid = [r for r in roles if r is not None]
    if not valid:
        return None
    return max(valid, key=lambda r: PROJECT_ROLE_LEVELS[r])


# =============================================================================
# Project Resolution
# =============================================================================


async def resolve_project_by_graph_id(
    session: AsyncSession,
    org_id: UUID,
    graph_project_id: str,
) -> Project:
    """Resolve a graph project ID to its Postgres Project row.

    Args:
        session: Database session
        org_id: Organization UUID (from auth context)
        graph_project_id: The graph entity ID (e.g. "project_abc123")

    Returns:
        The Project model instance

    Raises:
        HTTPException 404: Project not found in this org
    """
    result = await session.execute(
        select(Project).where(
            Project.organization_id == org_id,
            Project.graph_project_id == graph_project_id,
        )
    )
    project = result.scalar_one_or_none()

    if project is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Project not found: {graph_project_id}",
        )

    return project


async def get_project_by_id(
    session: AsyncSession,
    org_id: UUID,
    project_id: UUID,
) -> Project:
    """Resolve a Postgres project UUID to its Project row.

    Args:
        session: Database session
        org_id: Organization UUID (from auth context)
        project_id: The Postgres project UUID

    Returns:
        The Project model instance

    Raises:
        HTTPException 404: Project not found in this org
    """
    result = await session.execute(
        select(Project).where(
            Project.organization_id == org_id,
            Project.id == project_id,
        )
    )
    project = result.scalar_one_or_none()

    if project is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Project not found: {project_id}",
        )

    return project


# =============================================================================
# Role Resolution
# =============================================================================


async def get_effective_project_role(
    session: AsyncSession,
    ctx: AuthContext,
    project: Project,
) -> ProjectRole | None:
    """Calculate the effective project role for the current user.

    Role resolution order (highest wins):
    1. Org owner/admin → implicit project_owner
    2. Direct project membership
    3. Team grants (max of all team memberships)
    4. Org visibility default (if visibility=org)

    Args:
        session: Database session
        ctx: Auth context with user and org_role
        project: The Project to check access for

    Returns:
        The effective ProjectRole, or None if no access
    """
    user_id = ctx.user.id
    org_role = ctx.org_role

    # 1. Org owner/admin always has project_owner
    if org_role in ORG_ADMIN_ROLES:
        return ProjectRole.OWNER

    # 2. Check direct membership
    result = await session.execute(
        select(ProjectMember.role).where(
            ProjectMember.project_id == project.id,
            ProjectMember.user_id == user_id,
        )
    )
    direct_role = result.scalar_one_or_none()

    # 3. Check team grants
    # Find all teams the user belongs to that have grants on this project
    result = await session.execute(
        select(TeamProject.role)
        .join(TeamMember, TeamMember.team_id == TeamProject.team_id)
        .where(
            TeamProject.project_id == project.id,
            TeamMember.user_id == user_id,
        )
    )
    team_roles = [row[0] for row in result.all()]
    team_role = _max_role(*team_roles) if team_roles else None

    # 4. Check org visibility default
    visibility_role: ProjectRole | None = None
    if project.visibility == ProjectVisibility.ORG:
        visibility_role = project.default_role

    # Return max of all applicable roles
    return _max_role(direct_role, team_role, visibility_role)


async def list_accessible_project_graph_ids(
    session: AsyncSession,
    ctx: AuthContext,
) -> set[str] | None:
    """Get all graph project IDs the user can access.

    Used for filtering graph queries. Returns graph_project_id strings.

    Args:
        session: Database session
        ctx: Auth context with user and org info

    Returns:
        Set of accessible graph_project_id strings, or None to skip filtering
        (during migration period when no projects are registered in Postgres)
    """
    if ctx.organization is None:
        return set()

    org_id = ctx.organization.id
    user_id = ctx.user.id
    org_role = ctx.org_role

    # Check if ANY projects exist in Postgres for this org
    # If not, we're in migration period - skip filtering for org members
    count_result = await session.execute(
        select(Project.id).where(Project.organization_id == org_id).limit(1)
    )
    if count_result.first() is None:
        # No projects registered yet - allow org members to access everything
        if ctx.org_role is not None:
            log.debug("project_rbac_migration_mode", org_id=str(org_id))
            return None  # None means "skip filtering"
        return set()

    # Org owner/admin can access all projects in org
    if org_role in ORG_ADMIN_ROLES:
        result = await session.execute(
            select(Project.graph_project_id).where(Project.organization_id == org_id)
        )
        return {row[0] for row in result.all()}

    accessible: set[str] = set()

    # Projects with org visibility
    result = await session.execute(
        select(Project.graph_project_id).where(
            Project.organization_id == org_id,
            Project.visibility == ProjectVisibility.ORG,
        )
    )
    accessible.update(row[0] for row in result.all())

    # Direct memberships
    result = await session.execute(
        select(Project.graph_project_id)
        .join(ProjectMember, ProjectMember.project_id == Project.id)
        .where(
            Project.organization_id == org_id,
            ProjectMember.user_id == user_id,
        )
    )
    accessible.update(row[0] for row in result.all())

    # Team grants
    result = await session.execute(
        select(Project.graph_project_id)
        .join(TeamProject, TeamProject.project_id == Project.id)
        .join(TeamMember, TeamMember.team_id == TeamProject.team_id)
        .where(
            Project.organization_id == org_id,
            TeamMember.user_id == user_id,
        )
    )
    accessible.update(row[0] for row in result.all())

    return accessible


# =============================================================================
# FastAPI Dependencies
# =============================================================================


class ProjectAuthorizationError(HTTPException):
    """Structured 403 for project authorization failures.

    DEPRECATED: Use ProjectAccessDeniedError from sibyl.auth.errors instead.
    Kept for backwards compatibility.
    """

    def __init__(
        self,
        project_id: str,
        required_role: ProjectRole,
        actual_role: ProjectRole | None,
    ):
        detail = {
            "error": "project_access_denied",
            "message": f"Requires {required_role.value} access to project",
            "details": {
                "project_id": project_id,
                "required_role": required_role.value,
                "actual_role": actual_role.value if actual_role else None,
            },
        }
        super().__init__(status_code=status.HTTP_403_FORBIDDEN, detail=detail)


def require_project_role(
    *allowed_roles: ProjectRole,
    project_id_param: str = "project_id",
    use_graph_id: bool = True,
) -> Callable[..., Any]:
    """Create a dependency that requires a minimum project role.

    Args:
        allowed_roles: One or more ProjectRole values that are allowed
        project_id_param: Name of the path/query parameter containing the project ID
        use_graph_id: If True, param contains graph_project_id; if False, Postgres UUID

    Returns:
        FastAPI dependency function

    Example:
        @router.get("/projects/{project_id}/tasks")
        async def list_tasks(
            project_id: str,
            _: None = Depends(require_project_role(ProjectRole.VIEWER)),
        ):
            ...
    """

    async def dependency(
        request: Request,
        ctx: AuthContext = Depends(get_auth_context),
        session: AsyncSession = Depends(get_session_dependency),
    ) -> Project:
        if ctx.organization is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="No organization context",
            )

        # Extract project ID from request
        project_id_value = request.path_params.get(project_id_param)
        if project_id_value is None:
            project_id_value = request.query_params.get(project_id_param)

        if project_id_value is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Missing required parameter: {project_id_param}",
            )

        # Resolve project
        if use_graph_id:
            project = await resolve_project_by_graph_id(
                session, ctx.organization.id, project_id_value
            )
        else:
            try:
                project_uuid = UUID(project_id_value)
            except ValueError as e:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Invalid project ID format: {project_id_value}",
                ) from e
            project = await get_project_by_id(session, ctx.organization.id, project_uuid)

        # Check role
        effective_role = await get_effective_project_role(session, ctx, project)

        if effective_role is None:
            raise ProjectAuthorizationError(
                project_id=project_id_value,
                required_role=min(allowed_roles, key=lambda r: PROJECT_ROLE_LEVELS[r]),
                actual_role=None,
            )

        # Check if effective role is in allowed roles or higher
        min_required_level = min(PROJECT_ROLE_LEVELS[r] for r in allowed_roles)
        if PROJECT_ROLE_LEVELS[effective_role] < min_required_level:
            raise ProjectAuthorizationError(
                project_id=project_id_value,
                required_role=min(allowed_roles, key=lambda r: PROJECT_ROLE_LEVELS[r]),
                actual_role=effective_role,
            )

        log.debug(
            "project_access_granted",
            project_id=project_id_value,
            user_id=str(ctx.user.id),
            effective_role=effective_role.value,
        )

        return project

    return dependency


# Convenience shortcuts
def require_project_read(project_id_param: str = "project_id", use_graph_id: bool = True):
    """Require at least viewer access to the project."""
    return require_project_role(
        ProjectRole.VIEWER,
        ProjectRole.CONTRIBUTOR,
        ProjectRole.MAINTAINER,
        ProjectRole.OWNER,
        project_id_param=project_id_param,
        use_graph_id=use_graph_id,
    )


def require_project_write(project_id_param: str = "project_id", use_graph_id: bool = True):
    """Require at least contributor access to the project."""
    return require_project_role(
        ProjectRole.CONTRIBUTOR,
        ProjectRole.MAINTAINER,
        ProjectRole.OWNER,
        project_id_param=project_id_param,
        use_graph_id=use_graph_id,
    )


def require_project_admin(project_id_param: str = "project_id", use_graph_id: bool = True):
    """Require maintainer or owner access to the project."""
    return require_project_role(
        ProjectRole.MAINTAINER,
        ProjectRole.OWNER,
        project_id_param=project_id_param,
        use_graph_id=use_graph_id,
    )


# =============================================================================
# Entity-Based Authorization (for tasks, agents, etc.)
# =============================================================================


async def verify_entity_project_access(
    session: AsyncSession,
    ctx: AuthContext,
    entity_project_id: str | None,
    *,
    required_role: ProjectRole = ProjectRole.VIEWER,
) -> ProjectRole | None:
    """Verify access to an entity's project.

    For entities (tasks, agents, etc.) that have a project_id in their metadata,
    this checks if the current user has access to that project.

    Args:
        session: Database session
        ctx: Auth context
        entity_project_id: The project_id from the entity's metadata (graph_project_id)
        required_role: Minimum role required for access

    Returns:
        The effective role if access granted, None otherwise

    Raises:
        ProjectAuthorizationError: If user lacks required access
    """
    if ctx.organization is None:
        raise ProjectAuthorizationError(
            project_id=entity_project_id or "unknown",
            required_role=required_role,
            actual_role=None,
        )

    # If entity has no project, apply org-level permissions
    if entity_project_id is None:
        # Org admins can do any operation on unassigned entities
        if ctx.org_role in (OrganizationRole.OWNER, OrganizationRole.ADMIN):
            return ProjectRole.OWNER  # Full access for admins

        # Non-admins can only do VIEWER operations on unassigned entities
        if ctx.org_role is not None and required_role == ProjectRole.VIEWER:
            return ProjectRole.VIEWER

        # Non-admins cannot do write operations on unassigned entities
        log.warning(
            "write_attempt_on_unassigned_entity",
            user_id=str(ctx.user.id) if ctx.user else None,
            required_role=required_role.value,
        )
        raise ProjectAuthorizationError(
            project_id="unassigned",
            required_role=required_role,
            actual_role=ProjectRole.VIEWER if ctx.org_role else None,
        )

    # Resolve project and check access
    try:
        project = await resolve_project_by_graph_id(session, ctx.organization.id, entity_project_id)
    except HTTPException:
        # Project not registered in Postgres - same rules as unassigned
        # Org admins can do any operation
        if ctx.org_role in (OrganizationRole.OWNER, OrganizationRole.ADMIN):
            log.warning(
                "project_not_synced_admin_access",
                project_id=entity_project_id,
                user_id=str(ctx.user.id) if ctx.user else None,
            )
            return ProjectRole.OWNER

        # Non-admins can only do VIEWER operations
        if ctx.org_role is not None and required_role == ProjectRole.VIEWER:
            log.warning(
                "project_not_synced_viewer_access",
                project_id=entity_project_id,
                user_id=str(ctx.user.id) if ctx.user else None,
            )
            return ProjectRole.VIEWER

        # Non-admins cannot do write operations on unsynced projects
        log.warning(
            "write_attempt_on_unsynced_project",
            project_id=entity_project_id,
            user_id=str(ctx.user.id) if ctx.user else None,
            required_role=required_role.value,
        )
        raise ProjectAuthorizationError(
            project_id=entity_project_id,
            required_role=required_role,
            actual_role=ProjectRole.VIEWER if ctx.org_role else None,
        ) from None

    effective_role = await get_effective_project_role(session, ctx, project)

    if effective_role is None:
        raise ProjectAuthorizationError(
            project_id=entity_project_id,
            required_role=required_role,
            actual_role=None,
        )

    min_required_level = PROJECT_ROLE_LEVELS[required_role]
    if PROJECT_ROLE_LEVELS[effective_role] < min_required_level:
        raise ProjectAuthorizationError(
            project_id=entity_project_id,
            required_role=required_role,
            actual_role=effective_role,
        )

    return effective_role


async def filter_accessible_entities(
    session: AsyncSession,
    ctx: AuthContext,
    entities: list[Any],
    project_id_getter: Callable[[Any], str | None] = lambda e: getattr(e, "project_id", None),
) -> list[Any]:
    """Filter a list of entities to only those the user can access.

    Args:
        session: Database session
        ctx: Auth context
        entities: List of entities to filter
        project_id_getter: Function to extract project_id from an entity

    Returns:
        Filtered list of accessible entities
    """
    accessible_graph_ids = await list_accessible_project_graph_ids(session, ctx)

    result = []
    for entity in entities:
        project_id = project_id_getter(entity)
        # Include if: no project (unassigned), skip filtering (None), or project is accessible
        if project_id is None or accessible_graph_ids is None or project_id in accessible_graph_ids:
            result.append(entity)

    return result
