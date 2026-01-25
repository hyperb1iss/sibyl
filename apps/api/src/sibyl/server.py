"""MCP Server definition using FastMCP with streamable-http transport.

Exposes 5 tools and 2 resources:
- Tools: search, explore, add, manage, logs
- Resources: sibyl://health, sibyl://stats
"""

from dataclasses import asdict, dataclass
from typing import Any, Literal
from uuid import UUID

import structlog
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.fastmcp import FastMCP

from sibyl.config import settings

log = structlog.get_logger()


@dataclass(frozen=True)
class McpContext:
    """Context extracted from MCP authentication token."""

    org_id: str
    user_id: str | None = None
    scopes: list[str] | None = None
    # API key project restrictions (None = all, list = only these)
    api_key_project_ids: list[str] | None = None


async def _get_mcp_context() -> McpContext | None:
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
        from sibyl.auth.api_keys import ApiKeyManager
        from sibyl.db.connection import get_session

        async with get_session() as session:
            auth = await ApiKeyManager.from_session(session).authenticate(raw)
        if auth:
            # Convert project UUIDs to graph IDs (strings)
            project_ids = (
                [str(pid) for pid in auth.project_ids] if auth.project_ids is not None else None
            )
            return McpContext(
                org_id=str(auth.organization_id),
                user_id=str(auth.user_id),
                scopes=auth.scopes,
                api_key_project_ids=project_ids,
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
        return McpContext(
            org_id=str(org_id),
            user_id=str(user_id) if user_id else None,
            scopes=claims.get("scopes"),
        )
    return None


async def _get_org_id_from_context() -> str | None:
    """Extract organization ID from the authenticated MCP context.

    Returns:
        The organization ID string if authenticated and org-scoped, None otherwise.
    """
    ctx = await _get_mcp_context()
    return ctx.org_id if ctx else None


async def _require_mcp_context() -> McpContext:
    """Require full MCP context including user_id.

    Raises:
        ValueError: If no context is available.

    Returns:
        McpContext with org_id and user_id.
    """
    ctx = await _get_mcp_context()
    if not ctx:
        raise ValueError("Organization context required. Authenticate with an org-scoped token.")
    return ctx


async def _get_accessible_projects(ctx: McpContext) -> set[str] | None:
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

    from sibyl.auth.context import AuthContext
    from sibyl.db.connection import get_session

    async with get_session() as session:
        # Build a minimal AuthContext for authorization lookup
        from sqlmodel import select

        from sibyl.db.models import Organization, OrganizationMember, User

        # Get user
        user_result = await session.execute(select(User).where(User.id == UUID(ctx.user_id)))
        user = user_result.scalar_one_or_none()
        if not user:
            return set()  # User not found - no access

        # Get organization
        org_result = await session.execute(
            select(Organization).where(Organization.id == UUID(ctx.org_id))
        )
        org = org_result.scalar_one_or_none()
        if not org:
            return set()  # Org not found - no access

        # Get org role
        member_result = await session.execute(
            select(OrganizationMember).where(
                OrganizationMember.organization_id == org.id,
                OrganizationMember.user_id == user.id,
            )
        )
        member = member_result.scalar_one_or_none()
        org_role = member.role if member else None

        # Build AuthContext
        auth_ctx = AuthContext(
            user=user,
            organization=org,
            org_role=org_role,
            scopes=ctx.scopes or [],
        )

        # Get accessible projects based on user permissions
        from sibyl.auth.authorization import list_accessible_project_graph_ids

        user_accessible = await list_accessible_project_graph_ids(session, auth_ctx)

        # Intersect with API key project restrictions if present
        if ctx.api_key_project_ids is not None:
            api_key_allowed = set(ctx.api_key_project_ids)
            if user_accessible is None:
                # User has admin access but API key is restricted
                return api_key_allowed
            # Intersect: user must have access AND API key must allow it
            return user_accessible & api_key_allowed

        return user_accessible


async def _require_org_id() -> str:
    """Require organization ID from MCP context.

    Raises:
        ValueError: If no organization context is available.

    Returns:
        The organization ID string.
    """
    org_id = await _get_org_id_from_context()
    if not org_id:
        raise ValueError("Organization context required. Authenticate with an org-scoped token.")
    return org_id


# Module-level server instance (created lazily)
_mcp: FastMCP | None = None


def create_mcp_server(
    host: str = "localhost",
    port: int = 3334,
) -> FastMCP:
    """Create and configure the MCP server instance.

    Args:
        host: Host to bind to
        port: Port to listen on

    Returns:
        Configured FastMCP server instance
    """

    auth_mode = settings.mcp_auth_mode
    jwt_secret_set = bool(settings.jwt_secret.get_secret_value())
    auth_enabled = auth_mode == "on" or (auth_mode == "auto" and jwt_secret_set)

    auth_settings = None
    auth_server_provider = None
    token_verifier = None
    if auth_enabled:
        from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions

        server_url = settings.server_url.rstrip("/")
        auth_settings = AuthSettings(
            issuer_url=server_url,
            resource_server_url=f"{server_url}/mcp",
            required_scopes=["mcp"],
            client_registration_options=ClientRegistrationOptions(
                enabled=True,
                valid_scopes=["mcp"],
                default_scopes=["mcp"],
            ),
        )
        from sibyl.auth.mcp_oauth import SibylMcpOAuthProvider

        auth_server_provider = SibylMcpOAuthProvider()
        # NOTE: FastMCP does not allow configuring both an auth_server_provider
        # and a token_verifier at the same time. Our OAuth provider implements
        # access token validation via `load_access_token()`, so we rely on it.

    mcp = FastMCP(
        settings.server_name,
        host=host,
        port=port,
        stateless_http=False,  # Maintain session state
        auth=auth_settings,
        auth_server_provider=auth_server_provider,
        token_verifier=token_verifier,
    )

    if auth_server_provider is not None:

        @mcp.custom_route("/_oauth/login", methods=["GET"])
        async def _oauth_login_get(request):
            return await auth_server_provider.ui_login_get(request)

        @mcp.custom_route("/_oauth/login", methods=["POST"])
        async def _oauth_login_post(request):
            return await auth_server_provider.ui_login_post(request)

        @mcp.custom_route("/_oauth/org", methods=["GET"])
        async def _oauth_org_get(request):
            return await auth_server_provider.ui_org_get(request)

        @mcp.custom_route("/_oauth/org", methods=["POST"])
        async def _oauth_org_post(request):
            return await auth_server_provider.ui_org_post(request)

    _register_tools(mcp)
    _register_resources(mcp)
    return mcp


def get_mcp_server() -> FastMCP:
    """Get or create the default MCP server instance."""
    global _mcp  # noqa: PLW0603
    if _mcp is None:
        _mcp = create_mcp_server(
            host=settings.server_host,
            port=settings.server_port,
        )
    return _mcp


def _to_dict(obj: Any) -> Any:
    """Convert dataclass or object to dict for JSON serialization."""
    if hasattr(obj, "__dataclass_fields__"):
        return asdict(obj)
    if isinstance(obj, list):
        return [_to_dict(item) for item in obj]
    return obj


def _register_tools(mcp: FastMCP) -> None:
    """Register all MCP tools on the server instance."""

    # =========================================================================
    # TOOL 1: search - UNIFIED SEARCH (graph + documents)
    # =========================================================================

    @mcp.tool()
    async def search(
        query: str,
        types: list[str] | None = None,
        language: str | None = None,
        category: str | None = None,
        status: str | None = None,
        project: str | None = None,
        source: str | None = None,
        source_id: str | None = None,
        source_name: str | None = None,
        assignee: str | None = None,
        since: str | None = None,
        limit: int = 10,
        include_content: bool = True,
        include_documents: bool = True,
        include_graph: bool = True,
        use_enhanced: bool = True,
        boost_recent: bool = True,
    ) -> dict[str, Any]:
        """Unified semantic search across knowledge graph AND documentation.

        Searches both Sibyl's knowledge graph (patterns, rules, episodes, tasks)
        AND crawled documentation (pgvector similarity search). Results are
        merged and ranked by relevance score.

        IMPORTANT FOR AGENTS:
        - Results contain PREVIEWS only (truncated content)
        - To get FULL content, use: sibyl entity show <id>
        - Do NOT try to read URLs directly - content is stored in Sibyl
        - The 'id' field is the entity/chunk ID to fetch full content

        Args:
            query: Natural language search query
            types: Entity types to search. Options: pattern, rule, template,
                   topic, episode, task, project, document.
                   Include 'document' to search crawled docs.
            language: Filter by programming language (python, typescript, etc.)
            category: Filter by category/domain (authentication, database, etc.)
            status: Filter tasks by status (backlog, todo, doing, blocked, review, done)
            project: Filter tasks by project ID
            source: Alias for source_name (for convenience)
            source_id: Filter documents by source UUID
            source_name: Filter documents by source name (partial match)
            assignee: Filter tasks by assignee name
            since: Filter by creation date (ISO format: 2024-03-15 or relative: 7d, 2w)
            limit: Maximum results to return (1-50, default: 10)
            include_content: Include full content in results (default: True)
            include_documents: Search crawled documentation (default: True)
            include_graph: Search knowledge graph entities (default: True)
            use_enhanced: Use enhanced retrieval with reranking (default: True)
            boost_recent: Boost recent results in ranking (default: True)

        Returns:
            Search results with:
            - id: Entity/chunk ID (use with 'sibyl entity show <id>' for full content)
            - type: Entity type (pattern, rule, task, document, etc.)
            - name: Title/name of the result
            - content: PREVIEW only - truncated, use entity show for full content
            - score: Relevance score (0-1)
            - source: Source name for documentation results
            - result_origin: "graph" or "document" indicating data source
            - usage_hint: Instructions for getting full content

        Examples:
            # Search everything
            search("authentication patterns")

            # Search only documentation
            search("Next.js middleware", include_graph=False)

            # Get full content of a result
            # 1. search("OAuth") -> returns results with IDs
            # 2. sibyl entity show <id> -> returns full content
        """
        from sibyl_core.tools.core import search as _search

        # Get full context from authenticated MCP session
        ctx = await _require_mcp_context()
        accessible_projects = await _get_accessible_projects(ctx)

        result = await _search(
            query=query,
            types=types,
            language=language,
            category=category,
            status=status,
            project=project,
            accessible_projects=accessible_projects,
            source=source,
            source_id=source_id,
            source_name=source_name,
            assignee=assignee,
            since=since,
            limit=limit,
            include_content=include_content,
            include_documents=include_documents,
            include_graph=include_graph,
            use_enhanced=use_enhanced,
            boost_recent=boost_recent,
            organization_id=ctx.org_id,
        )
        return _to_dict(result)

    # =========================================================================
    # TOOL 2: explore
    # =========================================================================

    @mcp.tool()
    async def explore(
        mode: Literal["list", "related", "traverse", "dependencies"] = "list",
        types: list[str] | None = None,
        entity_id: str | None = None,
        relationship_types: list[str] | None = None,
        depth: int = 1,
        language: str | None = None,
        category: str | None = None,
        project: str | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Explore and browse the knowledge graph.

        Four modes of exploration:
        - list: Browse entities by type with optional filters
        - related: Find entities directly connected to a specific entity
        - traverse: Multi-hop graph traversal from an entity
        - dependencies: Task dependency chains in topological order

        Args:
            mode: Exploration mode - "list", "related", "traverse", or "dependencies"
            types: Entity types to explore (for list mode)
            entity_id: Starting entity ID (required for related/traverse/dependencies modes)
            relationship_types: Filter by relationship types
                               (APPLIES_TO, REQUIRES, CONFLICTS_WITH, SUPERSEDES,
                                DOCUMENTED_IN, ENABLES, BREAKS, PART_OF, RELATED_TO,
                                DERIVED_FROM)
            depth: Traversal depth for traverse mode (1-3, default: 1)
            language: Filter by programming language
            category: Filter by category
            project: Filter tasks by project ID (for list mode with tasks)
            status: Filter tasks by status (for list mode with tasks)
            limit: Maximum results (1-200, default: 50)

        Returns:
            Exploration results with entities and/or relationships

        Examples:
            explore(mode="list", types=["pattern"], language="typescript")
            explore(mode="list", types=["task"], project="proj_abc", status="todo")
            explore(mode="related", entity_id="pattern:error-handling")
            explore(mode="traverse", entity_id="topic:auth", depth=2)
            explore(mode="dependencies", entity_id="task_xyz")
        """
        from sibyl_core.tools.core import explore as _explore

        # Get full context from authenticated MCP session
        ctx = await _require_mcp_context()
        accessible_projects = await _get_accessible_projects(ctx)

        result = await _explore(
            mode=mode,
            types=types,
            entity_id=entity_id,
            relationship_types=relationship_types,
            depth=depth,
            language=language,
            category=category,
            project=project,
            accessible_projects=accessible_projects,
            status=status,
            limit=limit,
            organization_id=ctx.org_id,
        )
        return _to_dict(result)

    # =========================================================================
    # TOOL 3: add
    # =========================================================================

    @mcp.tool()
    async def add(
        title: str,
        content: str,
        entity_type: str = "episode",
        category: str | None = None,
        languages: list[str] | None = None,
        tags: list[str] | None = None,
        related_to: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        # Task-specific parameters
        project: str | None = None,
        priority: str | None = None,
        assignees: list[str] | None = None,
        due_date: str | None = None,
        technologies: list[str] | None = None,
        depends_on: list[str] | None = None,
        # Project-specific parameters
        repository_url: str | None = None,
    ) -> dict[str, Any]:
        """Add new knowledge to the graph.

        Creates a new knowledge entity that can be searched and explored.
        Supports episodes (learnings), patterns, tasks, and projects.

        ENTITY TYPES:
        - episode: Temporal knowledge (default) - insights, learnings, discoveries
        - pattern: Coding pattern or best practice
        - task: Work item with workflow state machine (REQUIRES project)
        - project: Container for related tasks

        Args:
            title: Short title for the knowledge (max 200 chars)
            content: Full content/description (max 50000 chars)
            entity_type: Type - "episode" (default), "pattern", "task", or "project"
            category: Category for organization (e.g., "debugging", "architecture")
            languages: Applicable programming languages
            tags: Searchable tags for discovery
            related_to: IDs of related entities to link
            metadata: Additional structured metadata (stored as JSON)
            project: Project ID (REQUIRED for tasks). Use explore(types=["project"]) to find projects.
            priority: Task priority - critical, high, medium (default), low, someday
            assignees: List of assignee names for tasks
            due_date: Due date for tasks (ISO format: 2024-03-15)
            technologies: Technologies involved (for tasks)
            depends_on: Task IDs this depends on (creates DEPENDS_ON edges)
            repository_url: Repository URL for projects

        Returns:
            Result with success status, entity ID, and message

        Examples:
            # Record a learning
            add("Debug: Redis timeout", "Problem was connection pool exhaustion",
                entity_type="pattern", category="debugging")

            # Create a task (project is REQUIRED)
            add("Implement OAuth", "Add OAuth2 login flow",
                entity_type="task", project="sibyl-project", priority="high")

            # Create a project
            add("Auth System", "Authentication and authorization",
                entity_type="project", repository_url="github.com/org/auth")
        """
        from sibyl_core.tools.core import add as _add

        # Get full context from authenticated MCP session
        ctx = await _require_mcp_context()

        # Inject org and user context into metadata
        full_metadata = metadata or {}
        full_metadata["organization_id"] = ctx.org_id
        if ctx.user_id:
            full_metadata["created_by"] = ctx.user_id

        result = await _add(
            title=title,
            content=content,
            entity_type=entity_type,
            category=category,
            languages=languages,
            tags=tags,
            related_to=related_to,
            metadata=full_metadata,
            project=project,
            priority=priority,
            assignees=assignees,
            due_date=due_date,
            technologies=technologies,
            depends_on=depends_on,
            repository_url=repository_url,
        )
        return _to_dict(result)

    # =========================================================================
    # TOOL 4: manage
    # =========================================================================

    @mcp.tool()
    async def manage(
        action: str,
        entity_id: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Manage operations that modify state in the knowledge graph.

        The manage() tool handles all state-changing operations including task
        workflow, source operations, analysis, and admin actions.

        Task Workflow Actions:
            - start_task: Begin work on a task (sets status to 'doing')
            - block_task: Mark task as blocked (data.reason required)
            - unblock_task: Remove blocked status, resume work
            - submit_review: Submit for code review (sets status to 'review')
            - complete_task: Mark done (data.learnings optional)
            - archive_task: Archive without completing
            - update_task: Update task fields (data contains updates)

        Source Operations:
            - crawl: Trigger crawl of URL (data.url required, data.depth optional)
            - sync: Re-crawl existing source (entity_id = source ID)
            - refresh: Sync all sources
            - link_graph: Link document chunks to knowledge graph (entity_id = source ID, optional)
            - link_graph_status: Get status of pending graph linking

        Analysis Actions:
            - estimate: Estimate task effort from similar completed tasks
            - prioritize: Get smart task ordering for project
            - detect_cycles: Find circular dependencies in project
            - suggest: Get knowledge suggestions for a task

        Admin Actions:
            - health: Server health check
            - stats: Graph statistics
            - rebuild_index: Rebuild search indices

        Args:
            action: Action to perform (see categories above)
            entity_id: Target entity ID (required for most actions)
            data: Action-specific data dict

        Returns:
            Result with success, action, entity_id, message, and data

        Examples:
            manage("start_task", entity_id="task-123")
            manage("complete_task", entity_id="task-123",
                   data={"learnings": "OAuth needs exact redirect URIs"})
            manage("crawl", data={"url": "https://docs.example.com", "depth": 3})
            manage("link_graph")  # Link all pending chunks
            manage("link_graph", entity_id="source-123")  # Link specific source
            manage("link_graph_status")  # Check pending work
            manage("estimate", entity_id="task-456")
            manage("health")
        """
        from sibyl_core.tools.manage import manage as _manage

        # Get full context from authenticated MCP session
        ctx = await _require_mcp_context()

        # Inject org and user context into data
        full_data = data or {}
        full_data["organization_id"] = ctx.org_id
        if ctx.user_id:
            full_data["user_id"] = ctx.user_id

        result = await _manage(
            action=action,
            entity_id=entity_id,
            data=full_data,
        )
        return _to_dict(result)

    # =========================================================================
    # TOOL 5: logs (Developer Introspection)
    # =========================================================================

    @mcp.tool()
    async def logs(
        limit: int = 50,
        service: str | None = None,
        level: str | None = None,
    ) -> list[dict[str, Any]]:
        """Get recent server logs for debugging and development.

        Returns log entries from the server's in-memory ring buffer.
        Useful for debugging issues without needing direct server access.

        Requires OWNER role (super admin equivalent).

        Args:
            limit: Maximum entries to return (default 50, max 500)
            service: Filter by service name (api, worker)
            level: Filter by log level (debug, info, warning, error)

        Returns:
            List of log entries with timestamp, service, level, event, context

        Examples:
            logs()                    # Last 50 entries
            logs(limit=100)           # Last 100 entries
            logs(service="worker")    # Worker logs only
            logs(level="error")       # Errors only
        """
        from sqlmodel import select

        from sibyl.db.connection import get_session
        from sibyl.db.models import OrganizationMember, OrganizationRole
        from sibyl_core.logging import LogBuffer

        # Require auth context
        ctx = await _require_mcp_context()

        # Check OWNER role (super admin)
        async with get_session() as session:
            result = await session.execute(
                select(OrganizationMember).where(
                    OrganizationMember.organization_id == UUID(ctx.org_id),
                    OrganizationMember.user_id == UUID(ctx.user_id) if ctx.user_id else False,
                )
            )
            membership = result.scalar_one_or_none()
            if not membership or membership.role != OrganizationRole.OWNER:
                raise ValueError("OWNER role required for log access")

        # Clamp limit
        limit = min(max(1, limit), 500)

        # Get logs from buffer
        buffer = LogBuffer.get()
        entries = buffer.tail(n=limit, service=service, level=level)
        return [e.to_dict() for e in entries]


def _register_resources(mcp: FastMCP) -> None:
    """Register MCP resources on the server instance."""

    # =========================================================================
    # RESOURCE: sibyl://health
    # =========================================================================

    @mcp.resource("sibyl://health")
    async def health_resource() -> str:
        """Server health and connectivity status.

        Returns JSON with:
        - status: "healthy" or "unhealthy"
        - server_name: Name of the server
        - uptime_seconds: Server uptime
        - graph_connected: Whether FalkorDB is reachable
        - entity_counts: Count of entities by type
        - errors: Any error messages
        """
        import json

        from sibyl_core.tools.core import get_health

        # Get org context (optional for health - basic health works without org)
        org_id = await _get_org_id_from_context()
        health = await get_health(organization_id=org_id)
        return json.dumps(health, indent=2)

    # =========================================================================
    # RESOURCE: sibyl://stats
    # =========================================================================

    @mcp.resource("sibyl://stats")
    async def stats_resource() -> str:
        """Knowledge graph statistics.

        Returns JSON with:
        - entity_counts: Count of entities by type
        - total_entities: Total entity count
        """
        import json

        from sibyl_core.tools.core import get_stats

        # Get org context (required for stats)
        org_id = await _require_org_id()
        stats = await get_stats(organization_id=org_id)
        return json.dumps(stats, indent=2)
