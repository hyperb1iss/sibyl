"""MCP Server definition using FastMCP with streamable-http transport."""

from dataclasses import asdict
from typing import Any

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

    # ========================================================================
    # Search Tools
    # ========================================================================

    @mcp.tool()
    async def search_wisdom(
        query: str,
        topic: str | None = None,
        language: str | None = None,
        limit: int = 10,
        include_sacred_rules: bool = True,
    ) -> list[dict[str, Any]]:
        """Semantic search across all development wisdom and patterns.

        Args:
            query: Natural language search query
            topic: Optional topic filter
            language: Filter by programming language
            limit: Maximum results to return
            include_sacred_rules: Include sacred rules in results
        """
        from sibyl.tools.search import search_wisdom as _search_wisdom

        results = await _search_wisdom(
            query=query,
            topic=topic,
            language=language,
            limit=limit,
            include_sacred_rules=include_sacred_rules,
        )
        return [_to_dict(r) for r in results]

    @mcp.tool()
    async def search_patterns(
        query: str,
        category: str | None = None,
        language: str | None = None,
        limit: int = 10,
        detail_level: str = "summary",
    ) -> list[dict[str, Any]]:
        """Search for specific coding patterns and practices.

        Args:
            query: Search query
            category: Category filter
            language: Language filter
            limit: Maximum results
            detail_level: 'summary' or 'full'
        """
        from sibyl.tools.search import search_patterns as _search_patterns

        results = await _search_patterns(
            query=query,
            category=category,
            language=language,
            limit=limit,
            detail_level=detail_level,
        )
        return [_to_dict(r) for r in results]

    @mcp.tool()
    async def find_solution(
        problem: str,
        context: str | None = None,
        language: str | None = None,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        """Find solutions for a specific problem or error.

        Args:
            problem: Problem or error description
            context: Additional context
            language: Language filter
            limit: Maximum results
        """
        from sibyl.tools.search import find_solution as _find_solution

        results = await _find_solution(
            problem=problem,
            context=context,
            language=language,
            limit=limit,
        )
        return [_to_dict(r) for r in results]

    @mcp.tool()
    async def search_templates(
        query: str,
        template_type: str | None = None,
        language: str | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Search for code and configuration templates.

        Args:
            query: Search query
            template_type: Type filter (code, config, project, workflow)
            language: Language filter
            limit: Maximum results
        """
        from sibyl.tools.search import search_templates as _search_templates

        results = await _search_templates(
            query=query,
            template_type=template_type,
            language=language,
            limit=limit,
        )
        return [_to_dict(r) for r in results]

    # ========================================================================
    # Lookup Tools
    # ========================================================================

    @mcp.tool()
    async def get_sacred_rules(
        category: str | None = None,
        language: str | None = None,
        severity: str | None = None,
    ) -> list[dict[str, Any]]:
        """Get all sacred rules/invariants for a category or language.

        Args:
            category: Category filter (development, type_safety, database)
            language: Language filter
            severity: Severity filter (error, warning, info)
        """
        from sibyl.tools.lookup import get_sacred_rules as _get_sacred_rules

        results = await _get_sacred_rules(
            category=category,
            language=language,
            severity=severity,
        )
        return [_to_dict(r) for r in results]

    @mcp.tool()
    async def get_language_guide(
        language: str,
        section: str | None = None,
    ) -> dict[str, Any]:
        """Get the complete conventions guide for a programming language.

        Args:
            language: Language name (python, typescript, rust, swift)
            section: Specific section (tooling, patterns, style, testing, all)
        """
        from sibyl.tools.lookup import get_language_guide as _get_language_guide

        result = await _get_language_guide(
            language=language,
            section=section,
        )
        return _to_dict(result) if result else {"error": "Language guide not found"}

    @mcp.tool()
    async def get_template(
        name: str,
        template_type: str | None = None,
        language: str | None = None,
    ) -> dict[str, Any]:
        """Get a specific template by name.

        Args:
            name: Template name or identifier
            template_type: Type filter
            language: Language filter
        """
        from sibyl.tools.lookup import get_template as _get_template

        result = await _get_template(
            name=name,
            template_type=template_type,
            language=language,
        )
        return _to_dict(result) if result else {"error": "Template not found"}

    # ========================================================================
    # Discovery Tools
    # ========================================================================

    @mcp.tool()
    async def list_patterns(
        category: str | None = None,
        language: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """List all available patterns.

        Args:
            category: Category filter
            language: Language filter
            limit: Maximum results
        """
        from sibyl.tools.discovery import list_patterns as _list_patterns

        result = await _list_patterns(
            category=category,
            language=language,
            limit=limit,
        )
        return _to_dict(result)

    @mcp.tool()
    async def list_templates(
        template_type: str | None = None,
        language: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """List all available templates.

        Args:
            template_type: Type filter (code, config, project, workflow)
            language: Language filter
            limit: Maximum results
        """
        from sibyl.tools.discovery import list_templates as _list_templates

        result = await _list_templates(
            template_type=template_type,
            language=language,
            limit=limit,
        )
        return _to_dict(result)

    @mcp.tool()
    async def list_rules(
        severity: str | None = None,
        language: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """List all sacred rules.

        Args:
            severity: Severity filter (error, warning, info)
            language: Language filter
            limit: Maximum results
        """
        from sibyl.tools.discovery import list_rules as _list_rules

        result = await _list_rules(
            severity=severity,
            language=language,
            limit=limit,
        )
        return _to_dict(result)

    @mcp.tool()
    async def list_topics(
        parent: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """List all knowledge topics.

        Args:
            parent: Parent topic filter
            limit: Maximum results
        """
        from sibyl.tools.discovery import list_topics as _list_topics

        result = await _list_topics(
            parent=parent,
            limit=limit,
        )
        return _to_dict(result)

    @mcp.tool()
    async def get_related(
        entity_id: str,
        relationship_types: list[str] | None = None,
        depth: int = 1,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Get entities related to a specific entity.

        Args:
            entity_id: ID of the entity
            relationship_types: Filter by relationship types
            depth: Traversal depth (1-3)
            limit: Maximum results
        """
        from sibyl.tools.discovery import get_related as _get_related

        results = await _get_related(
            entity_id=entity_id,
            relationship_types=relationship_types,
            depth=depth,
            limit=limit,
        )
        return [_to_dict(r) for r in results]

    # ========================================================================
    # Mutation Tools
    # ========================================================================

    @mcp.tool()
    async def add_learning(
        title: str,
        content: str,
        category: str,
        languages: list[str] | None = None,
        related_to: list[str] | None = None,
        source: str | None = None,
    ) -> dict[str, Any]:
        """Add a new piece of wisdom or learning to the graph.

        Args:
            title: Title of the learning
            content: Detailed content
            category: Category (debugging, architecture, performance)
            languages: Applicable programming languages
            related_to: IDs of related entities
            source: Source of the learning
        """
        from sibyl.tools.mutation import add_learning as _add_learning

        result = await _add_learning(
            title=title,
            content=content,
            category=category,
            languages=languages,
            _related_to=related_to,
            source=source,
        )
        return _to_dict(result)

    @mcp.tool()
    async def record_debugging_victory(
        problem: str,
        root_cause: str,
        solution: str,
        prevention: str | None = None,
        languages: list[str] | None = None,
        tools: list[str] | None = None,
        time_spent: str | None = None,
    ) -> dict[str, Any]:
        """Record a debugging victory with problem, root cause, and solution.

        Args:
            problem: The problem encountered
            root_cause: Root cause discovered
            solution: How it was solved
            prevention: How to prevent in future
            languages: Languages involved
            tools: Tools involved
            time_spent: Time spent debugging
        """
        from sibyl.tools.mutation import (
            record_debugging_victory as _record_debugging_victory,
        )

        result = await _record_debugging_victory(
            problem=problem,
            root_cause=root_cause,
            solution=solution,
            prevention=prevention,
            languages=languages,
            tools=tools,
            time_spent=time_spent,
        )
        return _to_dict(result)

    # ========================================================================
    # Admin Tools
    # ========================================================================

    @mcp.tool()
    async def health_check() -> dict[str, Any]:
        """Check server health and return status."""
        from sibyl.tools.admin import health_check as _health_check

        result = await _health_check()
        return {
            "status": result.status,
            "server_name": result.server_name,
            "uptime_seconds": result.uptime_seconds,
            "graph_connected": result.graph_connected,
            "search_latency_ms": result.search_latency_ms,
            "entity_counts": result.entity_counts,
            "errors": result.errors,
        }

    @mcp.tool()
    async def sync_wisdom_docs(
        path: str | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        """Re-ingest wisdom documentation from files.

        Args:
            path: Specific path to sync (optional)
            force: Force re-process all files
        """
        from sibyl.tools.admin import sync_wisdom_docs as _sync_wisdom_docs

        result = await _sync_wisdom_docs(path=path, force=force)
        return {
            "success": result.success,
            "files_processed": result.files_processed,
            "entities_created": result.entities_created,
            "entities_updated": result.entities_updated,
            "duration_seconds": result.duration_seconds,
            "errors": result.errors,
        }

    @mcp.tool()
    async def rebuild_indices(
        index_type: str | None = None,
    ) -> dict[str, Any]:
        """Rebuild graph indices for better query performance.

        Args:
            index_type: Type of index to rebuild (search, relationships, all)
        """
        from sibyl.tools.admin import rebuild_indices as _rebuild_indices

        result = await _rebuild_indices(index_type=index_type)
        return _to_dict(result)

    @mcp.tool()
    async def get_stats() -> dict[str, Any]:
        """Get detailed statistics about the knowledge graph."""
        from sibyl.tools.admin import get_stats as _get_stats

        return await _get_stats()
