"""Project-level authorization module.

This module implements project RBAC on top of the existing org RBAC.
It provides:
- Resolution of graph project IDs through the active auth runtime
- Project access verification through the active auth runtime
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

from sibyl.auth.context import AuthContext
from sibyl.auth.dependencies import get_auth_context
from sibyl.db.models import Project, ProjectRole
from sibyl.persistence.auth_runtime import (
    get_project_record_by_graph_id,
    get_project_record_by_id,
    list_accessible_project_graph_ids as list_runtime_accessible_project_graph_ids,
    verify_entity_project_access as verify_runtime_entity_project_access,
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

def _max_role(*roles: ProjectRole | None) -> ProjectRole | None:
    """Return the highest-privilege role from the given roles."""
    valid = [r for r in roles if r is not None]
    if not valid:
        return None
    return max(valid, key=lambda r: PROJECT_ROLE_LEVELS[r])


async def list_accessible_project_graph_ids(
    session: Any,
    ctx: AuthContext,
) -> set[str]:
    """Get all graph project IDs the user can access.

    Used for filtering graph queries. Returns graph_project_id strings.

    Args:
        session: Database session
        ctx: Auth context with user and org info

    Returns:
        Set of accessible graph_project_id strings.
    """
    del session
    return await list_runtime_accessible_project_graph_ids(ctx) or set()


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
        use_graph_id: If True, param contains graph_project_id; if False, storage UUID

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

        required_role = min(allowed_roles, key=lambda r: PROJECT_ROLE_LEVELS[r])

        if use_graph_id:
            project = await get_project_record_by_graph_id(
                organization_id=ctx.organization.id,
                graph_project_id=project_id_value,
            )
        else:
            try:
                project_uuid = UUID(project_id_value)
            except ValueError as e:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Invalid project ID format: {project_id_value}",
                ) from e
            project = await get_project_record_by_id(
                organization_id=ctx.organization.id,
                project_id=project_uuid,
            )

        graph_project_id = str(project.graph_project_id or "").strip()
        if not graph_project_id:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Project record is missing graph_project_id",
            )

        effective_role = await verify_runtime_entity_project_access(
            ctx=ctx,
            entity_project_id=graph_project_id,
            required_role=required_role,
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
    session: Any | None,
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
    del session
    return await verify_runtime_entity_project_access(
        ctx=ctx,
        entity_project_id=entity_project_id,
        required_role=required_role,
    )


async def filter_accessible_entities(
    session: Any,
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
        # Include if: no project (unassigned) or project is accessible
        if project_id is None or project_id in accessible_graph_ids:
            result.append(entity)

    return result
