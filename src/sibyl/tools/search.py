"""Search tools for the Conventions MCP Server.

Provides semantic search capabilities across wisdom, patterns, and solutions.
"""

from dataclasses import dataclass

import structlog

from sibyl.graph.client import get_graph_client
from sibyl.graph.entities import EntityManager
from sibyl.models.entities import EntityType
from sibyl.utils.resilience import TIMEOUTS, with_timeout

log = structlog.get_logger()


@dataclass
class SearchResult:
    """A search result with relevance scoring."""

    entity_id: str
    entity_type: str
    title: str
    content: str
    score: float
    source_file: str | None = None
    highlights: list[str] | None = None


async def search_wisdom(
    query: str,
    topic: str | None = None,
    language: str | None = None,
    limit: int = 10,
    include_sacred_rules: bool = True,
) -> list[SearchResult]:
    """Semantic search across all development wisdom and patterns.

    Args:
        query: Natural language search query.
        topic: Optional topic filter.
        language: Optional programming language filter.
        limit: Maximum results to return.
        include_sacred_rules: Whether to include sacred rules in results.

    Returns:
        List of search results ordered by relevance.
    """
    log.info(
        "Searching wisdom",
        query=query,
        topic=topic,
        language=language,
        limit=limit,
    )

    try:
        client = await get_graph_client()
        entity_manager = EntityManager(client)

        # Determine which entity types to search
        entity_types = [
            EntityType.PATTERN,
            EntityType.EPISODE,
            EntityType.TOPIC,
        ]
        if include_sacred_rules:
            entity_types.append(EntityType.RULE)

        # Perform semantic search with timeout protection
        results = await with_timeout(
            entity_manager.search(
                query=query,
                entity_types=entity_types,
                limit=limit,
            ),
            timeout_seconds=TIMEOUTS["search"],
            operation_name="search_wisdom",
        )

        # Convert to SearchResult objects
        search_results = []
        for entity, score in results:
            # Apply filters
            if language and hasattr(entity, "languages"):
                if language.lower() not in [l.lower() for l in entity.languages]:
                    continue

            search_results.append(
                SearchResult(
                    entity_id=entity.id,
                    entity_type=entity.entity_type.value,
                    title=entity.name,
                    content=entity.content[:500] if entity.content else entity.description,
                    score=score,
                    source_file=entity.source_file,
                )
            )

        return search_results[:limit]

    except Exception as e:
        log.warning("Search failed, returning empty results", error=str(e))
        return []


async def search_patterns(
    query: str,
    category: str | None = None,
    language: str | None = None,
    limit: int = 10,
    detail_level: str = "summary",
) -> list[SearchResult]:
    """Search for specific coding patterns and practices.

    Args:
        query: Search query describing the pattern.
        category: Optional category filter (e.g., "error-handling", "testing").
        language: Optional programming language filter.
        limit: Maximum results to return.
        detail_level: "summary" or "full" - controls content length.

    Returns:
        List of pattern search results.
    """
    log.info(
        "Searching patterns",
        query=query,
        category=category,
        language=language,
    )

    try:
        client = await get_graph_client()
        entity_manager = EntityManager(client)

        # Search only pattern entities with timeout protection
        results = await with_timeout(
            entity_manager.search(
                query=query,
                entity_types=[EntityType.PATTERN],
                limit=limit * 2,  # Over-fetch for filtering
            ),
            timeout_seconds=TIMEOUTS["search"],
            operation_name="search_patterns",
        )

        search_results = []
        for entity, score in results:
            # Apply category filter
            if category and hasattr(entity, "category"):
                if category.lower() not in entity.category.lower():
                    continue

            # Apply language filter
            if language and hasattr(entity, "languages"):
                if language.lower() not in [l.lower() for l in entity.languages]:
                    continue

            # Determine content based on detail level
            if detail_level == "full":
                content = entity.content or entity.description
            else:
                content = entity.description[:200] if entity.description else ""

            search_results.append(
                SearchResult(
                    entity_id=entity.id,
                    entity_type=entity.entity_type.value,
                    title=entity.name,
                    content=content,
                    score=score,
                    source_file=entity.source_file,
                )
            )

            if len(search_results) >= limit:
                break

        return search_results

    except Exception as e:
        log.warning("Pattern search failed", error=str(e))
        return []


async def find_solution(
    problem: str,
    context: str | None = None,
    language: str | None = None,
    limit: int = 5,
) -> list[SearchResult]:
    """Find solutions for a specific problem or error.

    Searches through debugging victories and known solutions.

    Args:
        problem: Description of the problem or error message.
        context: Additional context about when/where the problem occurs.
        language: Optional programming language filter.
        limit: Maximum results to return.

    Returns:
        List of potential solutions ordered by relevance.
    """
    # Combine problem and context for richer search
    search_query = problem
    if context:
        search_query = f"{problem} {context}"

    log.info("Finding solutions", problem=problem[:100], language=language)

    try:
        client = await get_graph_client()
        entity_manager = EntityManager(client)

        # Search across episodes that might contain solutions
        # Focus on patterns and episodes which often contain debugging info
        results = await with_timeout(
            entity_manager.search(
                query=search_query,
                entity_types=[EntityType.PATTERN, EntityType.EPISODE, EntityType.RULE],
                limit=limit * 2,
            ),
            timeout_seconds=TIMEOUTS["search"],
            operation_name="find_solution",
        )

        search_results = []
        for entity, score in results:
            # Apply language filter
            if language and hasattr(entity, "languages"):
                if language.lower() not in [l.lower() for l in entity.languages]:
                    continue

            # Extract relevant content - look for solution-like patterns
            content = entity.content or entity.description
            if not content:
                continue

            search_results.append(
                SearchResult(
                    entity_id=entity.id,
                    entity_type=entity.entity_type.value,
                    title=entity.name,
                    content=content[:600],
                    score=score,
                    source_file=entity.source_file,
                )
            )

            if len(search_results) >= limit:
                break

        return search_results

    except Exception as e:
        log.warning("Solution search failed", error=str(e))
        return []


async def search_templates(
    query: str,
    template_type: str | None = None,
    language: str | None = None,
    limit: int = 10,
) -> list[SearchResult]:
    """Search for code and configuration templates.

    Args:
        query: Search query for templates.
        template_type: Optional type filter (code, config, project, workflow).
        language: Optional programming language filter.
        limit: Maximum results to return.

    Returns:
        List of matching templates.
    """
    log.info(
        "Searching templates",
        query=query,
        template_type=template_type,
        language=language,
    )

    try:
        client = await get_graph_client()
        entity_manager = EntityManager(client)

        # Search template entities with timeout protection
        results = await with_timeout(
            entity_manager.search(
                query=query,
                entity_types=[EntityType.TEMPLATE, EntityType.CONFIG_FILE],
                limit=limit * 2,
            ),
            timeout_seconds=TIMEOUTS["search"],
            operation_name="search_templates",
        )

        search_results = []
        for entity, score in results:
            # Apply template type filter
            if template_type and hasattr(entity, "template_type"):
                if template_type.lower() != entity.template_type.lower():
                    continue

            # Apply language filter
            if language:
                entity_lang = getattr(entity, "language", None) or ""
                if language.lower() not in entity_lang.lower():
                    continue

            search_results.append(
                SearchResult(
                    entity_id=entity.id,
                    entity_type=entity.entity_type.value,
                    title=entity.name,
                    content=entity.description[:300] if entity.description else "",
                    score=score,
                    source_file=entity.source_file,
                )
            )

            if len(search_results) >= limit:
                break

        return search_results

    except Exception as e:
        log.warning("Template search failed", error=str(e))
        return []
