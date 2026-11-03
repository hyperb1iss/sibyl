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

from collections.abc import Awaitable, Callable
from typing import Protocol
from uuid import UUID

import structlog
from fastapi import Depends, HTTPException, Request, status

from sibyl.auth.api_key_common import api_key_memory_scope_key
from sibyl.auth.context import AuthContext
from sibyl.auth.dependencies import get_auth_context
from sibyl.persistence.auth_runtime import (
    get_project_record_by_graph_id,
    get_project_record_by_id,
    list_accessible_project_graph_ids as list_runtime_accessible_project_graph_ids,
    verify_entity_project_access as verify_runtime_entity_project_access,
)
from sibyl_core.auth import ProjectRole
from sibyl_core.models.memory_scope import MemoryScope

log = structlog.get_logger()


class ProjectRecord(Protocol):
    graph_project_id: str


def _request_project_id(request: Request, project_id_param: str) -> str | None:
    path_value = request.path_params.get(project_id_param)
    if isinstance(path_value, str):
        return path_value

    query_value = request.query_params.get(project_id_param)
    return query_value or None


def _entity_project_id(entity: object) -> str | None:
    project_id = getattr(entity, "project_id", None)
    return project_id if isinstance(project_id, str) else None


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
    session: object,
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
) -> Callable[..., Awaitable[ProjectRecord]]:
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
    ) -> ProjectRecord:
        if ctx.organization is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="No organization context",
            )

        project_id_value = _request_project_id(request, project_id_param)
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
# API-Key Narrowing for Project Administration Routes
# =============================================================================


async def _path_graph_project_id(ctx: AuthContext, project_id: str | None) -> str | None:
    """Graph id of a project named in a path by graph id or by record UUID.

    A graph id is returned as given. A UUID resolves through the project record
    in the caller's organization; a value that names no project resolves to
    None, so callers refuse an unknown project and an excluded one alike.
    """
    if not project_id:
        return None
    if project_id.startswith("project_"):
        return project_id
    if ctx.organization is None:
        return None
    try:
        project_uuid = UUID(project_id)
    except ValueError:
        return None
    try:
        project = await get_project_record_by_id(
            organization_id=ctx.organization.id,
            project_id=project_uuid,
        )
    except HTTPException as exc:
        if exc.status_code == status.HTTP_404_NOT_FOUND:
            return None
        raise
    return str(project.graph_project_id or "").strip() or None


def require_api_key_project_scope(
    project_id_param: str = "project_id",
) -> Callable[..., Awaitable[None]]:
    """Refuse a request whose project falls outside the API key's project binding.

    Session callers and unbound keys pass untouched. A bound key gets the same
    403 the search and capture routes give for a project outside its grants,
    whether the project exists or not, before any role lookup runs.
    """

    async def dependency(
        request: Request,
        ctx: AuthContext = Depends(get_auth_context),
    ) -> None:
        allowed = ctx.api_key_project_ids
        if allowed is None:
            return
        project_id = _request_project_id(request, project_id_param)
        graph_project_id = await _path_graph_project_id(ctx, project_id)
        if graph_project_id is None or graph_project_id not in allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="project_scope_denied",
            )

    return dependency


def require_api_key_project_memory_grant(
    project_id_param: str = "project_id",
) -> Callable[..., Awaitable[None]]:
    """Require a memory-bound API key to hold the project's memory space.

    For routes that change who reaches a project's memory or where its
    captures land. Session callers and keys without a memory-space binding
    pass untouched.
    """

    async def dependency(
        request: Request,
        ctx: AuthContext = Depends(get_auth_context),
    ) -> None:
        allowed = ctx.api_key_memory_scope_keys
        if allowed is None:
            return
        project_id = _request_project_id(request, project_id_param)
        graph_project_id = await _path_graph_project_id(ctx, project_id)
        if (
            graph_project_id is None
            or api_key_memory_scope_key(MemoryScope.PROJECT.value, graph_project_id) not in allowed
        ):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="api_key_memory_space_denied",
            )

    return dependency


# =============================================================================
# Entity-Based Authorization (for tasks, agents, etc.)
# =============================================================================


async def verify_entity_project_access(
    session: object | None,
    ctx: AuthContext,
    entity_project_id: str | None,
    *,
    required_role: ProjectRole = ProjectRole.VIEWER,
    require_existing_project: bool = False,
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
        require_existing_project=require_existing_project,
    )
