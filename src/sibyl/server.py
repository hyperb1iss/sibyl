"""MCP Server definition using FastMCP with streamable-http transport.

Exposes 3 tools and 2 resources:
- Tools: search, explore, add
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
    mcp = FastMCP(
        settings.server_name,
        host=host,
        port=port,
        stateless_http=False,  # Maintain session state
    )

    _register_tools(mcp)
    _register_resources(mcp)
    return mcp


def get_mcp_server() -> FastMCP:
    """Get or create the default MCP server instance."""
    global _mcp
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
    # TOOL 1: search
    # =========================================================================

    @mcp.tool()
    async def search(
        query: str,
        types: list[str] | None = None,
        language: str | None = None,
        category: str | None = None,
        limit: int = 10,
        include_content: bool = True,
    ) -> dict[str, Any]:
        """Semantic search across the knowledge graph.

        Search for patterns, rules, templates, episodes, and other knowledge
        using natural language queries. Results are ranked by relevance.

        Args:
            query: Natural language search query
            types: Entity types to search (pattern, rule, template, topic,
                   episode, tool, language, config_file, slash_command).
                   If not specified, searches all types.
            language: Filter results by programming language
            category: Filter results by category/topic
            limit: Maximum results to return (1-50, default: 10)
            include_content: Include full content in results (default: True)

        Returns:
            Search results with id, type, name, content, score, and metadata

        Examples:
            search("error handling patterns", types=["pattern"], language="python")
            search("authentication best practices")
            search("typescript config", types=["template", "config_file"])
        """
        from sibyl.tools.core import search as _search

        result = await _search(
            query=query,
            types=types,
            language=language,
            category=category,
            limit=limit,
            include_content=include_content,
        )
        return _to_dict(result)

    # =========================================================================
    # TOOL 2: explore
    # =========================================================================

    @mcp.tool()
    async def explore(
        mode: Literal["list", "related", "traverse"] = "list",
        types: list[str] | None = None,
        entity_id: str | None = None,
        relationship_types: list[str] | None = None,
        depth: int = 1,
        language: str | None = None,
        category: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Explore and browse the knowledge graph.

        Three modes of exploration:
        - list: Browse entities by type with optional filters
        - related: Find entities directly connected to a specific entity
        - traverse: Multi-hop graph traversal from an entity

        Args:
            mode: Exploration mode - "list", "related", or "traverse"
            types: Entity types to explore (for list mode)
            entity_id: Starting entity ID (required for related/traverse modes)
            relationship_types: Filter by relationship types
                               (APPLIES_TO, REQUIRES, CONFLICTS_WITH, SUPERSEDES,
                                DOCUMENTED_IN, ENABLES, BREAKS, PART_OF, RELATED_TO,
                                DERIVED_FROM)
            depth: Traversal depth for traverse mode (1-3, default: 1)
            language: Filter by programming language
            category: Filter by category
            limit: Maximum results (1-200, default: 50)

        Returns:
            Exploration results with entities and/or relationships

        Examples:
            explore(mode="list", types=["pattern"], language="typescript")
            explore(mode="related", entity_id="pattern:error-handling")
            explore(mode="traverse", entity_id="topic:auth", depth=2)
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
    ) -> dict[str, Any]:
        """Add new knowledge to the graph.

        Creates a new knowledge entity that can be searched and explored.
        Use this to record learnings, patterns, debugging victories, or
        any other knowledge worth preserving.

        Args:
            title: Short title for the knowledge (max 200 chars)
            content: Full content/description (max 50000 chars)
            entity_type: Type of entity - "episode" (default) or "pattern"
            category: Category for organization (e.g., "debugging", "architecture")
            languages: Applicable programming languages
            tags: Searchable tags for discovery
            related_to: IDs of related entities to link
            metadata: Additional structured metadata (stored as JSON)

        Returns:
            Result with success status, entity ID, and message

        Examples:
            add("TypeScript strict mode", "Always enable strictNullChecks...",
                category="type-safety", languages=["typescript"])

            add("Debug: Redis timeout", "Problem was connection pool exhaustion",
                entity_type="pattern", category="debugging",
                metadata={"root_cause": "pool size", "solution": "increase pool"})
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
