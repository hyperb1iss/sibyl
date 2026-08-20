"""Authenticated MCP caller context and project-scope resolution."""

from collections.abc import Iterable
from dataclasses import dataclass

import structlog
from mcp.server.auth.middleware.auth_context import get_access_token

from sibyl.auth.mcp_auth import (
    effective_api_key_scopes,
    insufficient_mcp_scope_message,
    mcp_scopes_allow,
)
from sibyl.persistence.auth_runtime import (
    authenticate_api_key,
    resolve_accessible_project_graph_ids,
    resolve_org_role,
)
from sibyl_core.auth.context import MemoryPolicyContext

log = structlog.get_logger()


@dataclass(frozen=True)
class McpContext:
    """Context extracted from MCP authentication token."""

    org_id: str
    user_id: str | None = None
    scopes: list[str] | None = None
    # API key project restrictions (None = all, list = only these)
    api_key_project_ids: list[str] | None = None
    api_key_memory_space_ids: list[str] | None = None
    api_key_memory_scope_keys: list[str] | None = None
    org_role: str | None = None
    delegated_authority: str | None = None
    agent_id: str | None = None
    # True when the caller authenticated with an API key. Scope enforcement is
    # an API-key concern on both surfaces: user sessions carry no scope claim,
    # and REST gates scopes only on its own API-key branch.
    is_api_key: bool = False

    def to_memory_policy_context(
        self,
        *,
        memory_space: str | None = None,
        scope_key: str | None = None,
        project_id: str | None = None,
        accessible_projects: Iterable[str] | None = None,
        accessible_teams: Iterable[str] | None = None,
        accessible_delegations: Iterable[str] | None = None,
        source_surface: str = "mcp",
    ) -> MemoryPolicyContext:
        return MemoryPolicyContext(
            actor_user_id=self.user_id,
            organization_id=self.org_id,
            organization_role=self.org_role,
            accessible_projects=frozenset(str(value) for value in accessible_projects)
            if accessible_projects is not None
            else None,
            accessible_teams=frozenset(str(value) for value in accessible_teams)
            if accessible_teams is not None
            else None,
            accessible_delegations=frozenset(str(value) for value in accessible_delegations)
            if accessible_delegations is not None
            else None,
            delegated_authority=self.delegated_authority,
            agent_id=self.agent_id,
            project_id=project_id,
            memory_space=memory_space,
            scope_key=scope_key,
            source_surface=source_surface,
        )


async def get_context() -> McpContext | None:
    """Extract full context (org_id, user_id, scopes) from MCP token.

    Returns:
        McpContext if authenticated, None otherwise.
    """
    token = get_access_token()
    if token is None:
        return None

    raw = token.token
    if not raw:
        return None

    # API Key authentication
    if raw.startswith("sk_"):
        auth = await authenticate_api_key(raw)
        if auth:
            # Convert project UUIDs to graph IDs (strings)
            project_ids = (
                [str(pid) for pid in auth.project_ids] if auth.project_ids is not None else None
            )
            org_id = str(auth.organization_id)
            user_id = str(auth.user_id)
            org_role = await resolve_org_role(org_id=org_id, user_id=user_id)
            return McpContext(
                org_id=org_id,
                user_id=user_id,
                scopes=auth.scopes,
                api_key_project_ids=project_ids,
                api_key_memory_space_ids=[
                    str(memory_space_id)
                    for memory_space_id in getattr(auth, "memory_space_ids", None) or []
                ]
                if getattr(auth, "memory_space_ids", None) is not None
                else None,
                api_key_memory_scope_keys=[
                    memory_space.policy_key
                    for memory_space in getattr(auth, "memory_spaces", None) or []
                ]
                if getattr(auth, "memory_spaces", None) is not None
                else None,
                org_role=org_role,
                is_api_key=True,
            )
        return None

    # JWT authentication
    from sibyl.auth.jwt import JwtError, verify_access_token

    try:
        claims = verify_access_token(raw)
    except JwtError:
        return None

    org_id = claims.get("org")
    user_id = claims.get("sub")

    if org_id:
        log.debug("mcp_context", org_id=org_id, user_id=user_id)
        org_role = await resolve_org_role(
            org_id=str(org_id),
            user_id=str(user_id) if user_id else None,
        )
        return McpContext(
            org_id=str(org_id),
            user_id=str(user_id) if user_id else None,
            scopes=claims.get("scopes"),
            org_role=org_role,
        )
    return None


async def optional_org_id() -> str | None:
    """Extract the organization ID when the caller presents a usable credential.

    Health reporting works without an org, so an anonymous caller gets None
    rather than an error. A credential that is presented still has to clear the
    read gate: failing scopes are refused, not silently downgraded to anonymous.

    Returns:
        The organization ID string if authenticated and org-scoped, None otherwise.
    """
    ctx = await get_context()
    if ctx is None:
        return None
    authorize_scope(ctx, write=False)
    return ctx.org_id


def authorize_scope(ctx: McpContext, *, write: bool) -> None:
    """Enforce API-key scopes for an MCP tool call.

    Raises:
        ValueError: If the key lacks the scope this tool requires.
    """
    if not ctx.is_api_key:
        return
    scopes = effective_api_key_scopes(ctx.scopes)
    if mcp_scopes_allow(scopes, write=write):
        return
    log.warning(
        "mcp_insufficient_scope",
        org_id=ctx.org_id,
        write=write,
        scopes=sorted(scopes),
    )
    raise ValueError(insufficient_mcp_scope_message(scopes, write=write))


async def require_context(*, write: bool = False) -> McpContext:
    """Require full MCP context including user_id.

    Args:
        write: True when the caller is about to mutate state, which demands the
            `api:write` scope from any key that carries granular REST scopes.

    Raises:
        ValueError: If no context is available or its scopes are insufficient.

    Returns:
        McpContext with org_id and user_id.
    """
    ctx = await get_context()
    if not ctx:
        raise ValueError("Organization context required. Authenticate with an org-scoped token.")
    authorize_scope(ctx, write=write)
    if write and ctx.org_role not in {"owner", "admin", "member"}:
        raise ValueError("organization_write_forbidden")
    return ctx


async def get_accessible_projects(ctx: McpContext) -> set[str] | None:
    """Get project IDs the user can access based on their permissions.

    Combines user permissions with API key project restrictions (if any).

    Returns:
        Set of accessible project graph IDs, or None if no filtering needed (admin).
    """
    if not ctx.user_id:
        # No user context - can't filter by user permissions
        # But still enforce API key restrictions if present
        if ctx.api_key_project_ids is not None:
            return set(ctx.api_key_project_ids)
        return None

    return await resolve_accessible_project_graph_ids(
        user_id=ctx.user_id,
        org_id=ctx.org_id,
        scopes=ctx.scopes,
        api_key_project_ids=ctx.api_key_project_ids,
    )


async def resolve_project_scope(
    ctx: McpContext,
    project: str | None,
    *,
    require_project_when_restricted: bool = False,
) -> set[str] | None:
    """Resolve accessible project scope for MCP tools."""
    accessible_projects = await get_accessible_projects(ctx)
    if accessible_projects is None:
        if project:
            return {project}
        return None
    if project:
        if project not in accessible_projects:
            raise ValueError(f"Project access denied: {project}")
        return {project}
    if require_project_when_restricted:
        raise ValueError("Project is required when MCP credentials are project-scoped.")
    return accessible_projects


async def require_org_id() -> str:
    """Require organization ID from MCP context.

    Raises:
        ValueError: If no organization context is available.

    Returns:
        The organization ID string.
    """
    ctx = await require_context()
    return ctx.org_id
