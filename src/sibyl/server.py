"""MCP Server definition using FastMCP with streamable-http transport.

Exposes 4 tools and 2 resources:
- Tools: search, explore, add, manage
- Resources: sibyl://health, sibyl://stats
"""

from dataclasses import asdict
from typing import Any, Literal

from mcp.server.fastmcp import FastMCP

from sibyl.config import settings

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
        async def _oauth_login_get(request):  # type: ignore[no-untyped-def]
            return await auth_server_provider.ui_login_get(request)

        @mcp.custom_route("/_oauth/login", methods=["POST"])
        async def _oauth_login_post(request):  # type: ignore[no-untyped-def]
            return await auth_server_provider.ui_login_post(request)

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
            Unified search results with id, type, name, content, score, url,
            result_origin (graph/document), and metadata. Also includes
            graph_count and document_count for result breakdown.

        Examples:
            # Search everything
            search("authentication patterns")

            # Search only documentation
            search("Next.js middleware", include_graph=False)

            # Search specific doc source
            search("environment variables", source_name="next-dynenv")

            # Search only knowledge graph
            search("OAuth implementation", include_documents=False)

            # Search by type
            search("error handling", types=["pattern", "document"])
        """
        from sibyl.tools.core import search as _search

        result = await _search(
            query=query,
            types=types,
            language=language,
            category=category,
            status=status,
            project=project,
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
        from sibyl.tools.core import explore as _explore

        result = await _explore(
            mode=mode,
            types=types,
            entity_id=entity_id,
            relationship_types=relationship_types,
            depth=depth,
            language=language,
            category=category,
            project=project,
            status=status,
            limit=limit,
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
        # Auto-linking
        auto_link: bool = False,
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
            auto_link: Auto-discover related patterns/rules (similarity > 0.75)

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
        from sibyl.tools.core import add as _add

        result = await _add(
            title=title,
            content=content,
            entity_type=entity_type,
            category=category,
            languages=languages,
            tags=tags,
            related_to=related_to,
            metadata=metadata,
            project=project,
            priority=priority,
            assignees=assignees,
            due_date=due_date,
            technologies=technologies,
            depends_on=depends_on,
            repository_url=repository_url,
            auto_link=auto_link,
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
            manage("estimate", entity_id="task-456")
            manage("health")
        """
        from sibyl.tools.manage import manage as _manage

        result = await _manage(
            action=action,
            entity_id=entity_id,
            data=data,
        )
        return _to_dict(result)


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

        from sibyl.tools.core import get_health

        health = await get_health()
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

        from sibyl.tools.core import get_stats

        stats = await get_stats()
        return json.dumps(stats, indent=2)
