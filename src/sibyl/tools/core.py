"""Unified tools for Sibyl MCP Server.

Three consolidated tools: search, explore, add.
Replaces the previous 18+ specific tools with a generic interface.
"""

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

import structlog

from sibyl.graph.client import get_graph_client
from sibyl.graph.entities import EntityManager
from sibyl.graph.relationships import RelationshipManager
from sibyl.models.entities import EntityType, Episode, Pattern, RelationshipType
from sibyl.utils.resilience import TIMEOUTS, with_timeout

log = structlog.get_logger()

# Valid entity types for filtering
VALID_ENTITY_TYPES = {t.value for t in EntityType}

# Validation constants
MAX_TITLE_LENGTH = 200
MAX_CONTENT_LENGTH = 50000


# =============================================================================
# Response Models
# =============================================================================


@dataclass
class SearchResult:
    """A single search result."""

    id: str
    type: str
    name: str
    content: str
    score: float
    source: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class SearchResponse:
    """Response from search operation."""

    results: list[SearchResult]
    total: int
    query: str
    filters: dict[str, Any]


@dataclass
class EntitySummary:
    """Summary of an entity for listing."""

    id: str
    type: str
    name: str
    description: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class RelatedEntity:
    """An entity related through the graph."""

    id: str
    type: str
    name: str
    relationship: str
    direction: Literal["outgoing", "incoming"]
    distance: int = 1


@dataclass
class ExploreResponse:
    """Response from explore operation."""

    mode: str
    entities: list[EntitySummary] | list[RelatedEntity]
    total: int
    filters: dict[str, Any]


@dataclass
class AddResponse:
    """Response from add operation."""

    success: bool
    id: str | None
    message: str
    timestamp: datetime


# =============================================================================
# TOOL 1: search
# =============================================================================


async def search(
    query: str,
    types: list[str] | None = None,
    language: str | None = None,
    category: str | None = None,
    limit: int = 10,
    include_content: bool = True,
) -> SearchResponse:
    """Semantic search across the knowledge graph.

    Args:
        query: Natural language search query.
        types: Entity types to search (pattern, rule, template, topic, episode, etc.).
               If None, searches all types.
        language: Filter by programming language.
        category: Filter by category/topic.
        limit: Maximum results (1-50).
        include_content: Whether to include full content in results.

    Returns:
        SearchResponse with ranked results.
    """
    # Clamp limit
    limit = max(1, min(limit, 50))

    log.info(
        "search",
        query=query[:100],
        types=types,
        language=language,
        category=category,
        limit=limit,
    )

    filters = {}
    if types:
        filters["types"] = types
    if language:
        filters["language"] = language
    if category:
        filters["category"] = category

    try:
        client = await get_graph_client()
        entity_manager = EntityManager(client)

        # Determine entity types to search
        entity_types = None
        if types:
            entity_types = []
            for t in types:
                if t.lower() in VALID_ENTITY_TYPES:
                    entity_types.append(EntityType(t.lower()))
                else:
                    log.warning("unknown_entity_type", type=t)

        # Perform semantic search
        raw_results = await with_timeout(
            entity_manager.search(
                query=query,
                entity_types=entity_types,
                limit=limit * 2,  # Over-fetch for filtering
            ),
            timeout_seconds=TIMEOUTS["search"],
            operation_name="search",
        )

        results = []
        for entity, score in raw_results:
            # Apply language filter
            if language:
                entity_langs = getattr(entity, "languages", []) or []
                if language.lower() not in [l.lower() for l in entity_langs]:
                    continue

            # Apply category filter
            if category:
                entity_cat = getattr(entity, "category", "") or ""
                if category.lower() not in entity_cat.lower():
                    continue

            # Build content based on include_content flag
            if include_content:
                content = entity.content[:500] if entity.content else entity.description
            else:
                content = entity.description[:200] if entity.description else ""

            results.append(
                SearchResult(
                    id=entity.id,
                    type=entity.entity_type.value,
                    name=entity.name,
                    content=content or "",
                    score=score,
                    source=entity.source_file,
                    metadata={
                        k: v
                        for k, v in {
                            "category": getattr(entity, "category", None),
                            "languages": getattr(entity, "languages", None),
                            "severity": getattr(entity, "severity", None),
                        }.items()
                        if v is not None
                    },
                )
            )

            if len(results) >= limit:
                break

        return SearchResponse(
            results=results,
            total=len(results),
            query=query,
            filters=filters,
        )

    except Exception as e:
        log.warning("search_failed", error=str(e))
        return SearchResponse(results=[], total=0, query=query, filters=filters)


# =============================================================================
# TOOL 2: explore
# =============================================================================


async def explore(
    mode: Literal["list", "related", "traverse"] = "list",
    types: list[str] | None = None,
    entity_id: str | None = None,
    relationship_types: list[str] | None = None,
    depth: int = 1,
    language: str | None = None,
    category: str | None = None,
    limit: int = 50,
) -> ExploreResponse:
    """Explore and browse the knowledge graph.

    Modes:
        - list: Browse entities by type with optional filters
        - related: Find entities connected to a specific entity
        - traverse: Multi-hop graph traversal from an entity

    Args:
        mode: Exploration mode.
        types: Entity types to explore (for list mode).
        entity_id: Starting entity for related/traverse modes.
        relationship_types: Filter by relationship types (for related/traverse).
        depth: Traversal depth for traverse mode (1-3).
        language: Filter by language.
        category: Filter by category.
        limit: Maximum results.

    Returns:
        ExploreResponse with entities or relationships.
    """
    # Clamp values
    limit = max(1, min(limit, 200))
    depth = max(1, min(depth, 3))

    log.info(
        "explore",
        mode=mode,
        types=types,
        entity_id=entity_id,
        depth=depth,
    )

    filters = {}
    if types:
        filters["types"] = types
    if language:
        filters["language"] = language
    if category:
        filters["category"] = category
    if entity_id:
        filters["entity_id"] = entity_id

    try:
        if mode in ("related", "traverse"):
            return await _explore_related(
                entity_id=entity_id,
                relationship_types=relationship_types,
                depth=depth if mode == "traverse" else 1,
                limit=limit,
                filters=filters,
                mode=mode,
            )
        return await _explore_list(
            types=types,
            language=language,
            category=category,
            limit=limit,
            filters=filters,
        )

    except Exception as e:
        log.warning("explore_failed", error=str(e), mode=mode)
        return ExploreResponse(mode=mode, entities=[], total=0, filters=filters)


async def _explore_list(
    types: list[str] | None,
    language: str | None,
    category: str | None,
    limit: int,
    filters: dict[str, Any],
) -> ExploreResponse:
    """List entities by type with filters."""
    client = await get_graph_client()
    entity_manager = EntityManager(client)

    # Default to listing all types if none specified
    target_types = []
    if types:
        for t in types:
            if t.lower() in VALID_ENTITY_TYPES:
                target_types.append(EntityType(t.lower()))
    else:
        # Default to common browsable types
        target_types = [
            EntityType.PATTERN,
            EntityType.RULE,
            EntityType.TEMPLATE,
            EntityType.TOPIC,
        ]

    all_entities = []
    for entity_type in target_types:
        entities = await entity_manager.list_by_type(entity_type, limit=limit)
        all_entities.extend(entities)

    # Apply filters and convert to summaries
    results = []
    for entity in all_entities:
        # Language filter
        if language:
            entity_langs = getattr(entity, "languages", []) or []
            if language.lower() not in [l.lower() for l in entity_langs]:
                continue

        # Category filter
        if category:
            entity_cat = getattr(entity, "category", "") or ""
            if category.lower() not in entity_cat.lower():
                continue

        results.append(
            EntitySummary(
                id=entity.id,
                type=entity.entity_type.value,
                name=entity.name,
                description=entity.description[:200] if entity.description else "",
                metadata={
                    k: v
                    for k, v in {
                        "category": getattr(entity, "category", None),
                        "languages": getattr(entity, "languages", None),
                        "severity": getattr(entity, "severity", None),
                        "template_type": getattr(entity, "template_type", None),
                    }.items()
                    if v is not None
                },
            )
        )

        if len(results) >= limit:
            break

    return ExploreResponse(
        mode="list",
        entities=results,
        total=len(results),
        filters=filters,
    )


async def _explore_related(
    entity_id: str | None,
    relationship_types: list[str] | None,
    depth: int,
    limit: int,
    filters: dict[str, Any],
    mode: str,
) -> ExploreResponse:
    """Find related entities via graph traversal."""
    if not entity_id:
        return ExploreResponse(
            mode=mode,
            entities=[],
            total=0,
            filters={**filters, "error": "entity_id required for related/traverse mode"},
        )

    client = await get_graph_client()
    relationship_manager = RelationshipManager(client)

    # Convert relationship type strings to enum
    rel_types = None
    if relationship_types:
        rel_types = []
        for rt in relationship_types:
            try:
                rel_types.append(RelationshipType(rt.upper()))
            except ValueError:
                log.warning("unknown_relationship_type", type=rt)

    # Get related entities
    raw_results = await relationship_manager.get_related_entities(
        entity_id=entity_id,
        relationship_types=rel_types,
        depth=depth,
        limit=limit,
    )

    results = []
    for entity, relationship in raw_results:
        direction: Literal["outgoing", "incoming"] = (
            "outgoing" if relationship.source_id == entity_id else "incoming"
        )

        results.append(
            RelatedEntity(
                id=entity.id,
                type=entity.entity_type.value,
                name=entity.name,
                relationship=relationship.relationship_type.value,
                direction=direction,
                distance=1,  # Would need path info for multi-hop
            )
        )

    return ExploreResponse(
        mode=mode,
        entities=results,
        total=len(results),
        filters=filters,
    )


# =============================================================================
# TOOL 3: add
# =============================================================================


async def add(
    title: str,
    content: str,
    entity_type: str = "episode",
    category: str | None = None,
    languages: list[str] | None = None,
    tags: list[str] | None = None,
    related_to: list[str] | None = None,  # noqa: ARG001 - TODO: implement relationship creation
    metadata: dict[str, Any] | None = None,
) -> AddResponse:
    """Add new knowledge to the graph.

    Args:
        title: Short title for the knowledge.
        content: Full content/description.
        entity_type: Type of entity to create (default: episode).
        category: Category for organization.
        languages: Applicable programming languages.
        tags: Searchable tags.
        related_to: IDs of related entities to link.
        metadata: Additional structured metadata.

    Returns:
        AddResponse indicating success or failure.
    """
    # Sanitize inputs
    title = title.strip()
    content = content.strip()

    # Validate
    if not title:
        return AddResponse(
            success=False,
            id=None,
            message="Title cannot be empty",
            timestamp=datetime.now(UTC),
        )

    if len(title) > MAX_TITLE_LENGTH:
        return AddResponse(
            success=False,
            id=None,
            message=f"Title exceeds {MAX_TITLE_LENGTH} characters",
            timestamp=datetime.now(UTC),
        )

    if not content:
        return AddResponse(
            success=False,
            id=None,
            message="Content cannot be empty",
            timestamp=datetime.now(UTC),
        )

    if len(content) > MAX_CONTENT_LENGTH:
        return AddResponse(
            success=False,
            id=None,
            message=f"Content exceeds {MAX_CONTENT_LENGTH} characters",
            timestamp=datetime.now(UTC),
        )

    log.info(
        "add",
        title=title[:50],
        entity_type=entity_type,
        category=category,
        languages=languages,
    )

    try:
        client = await get_graph_client()
        entity_manager = EntityManager(client)

        # Generate deterministic ID
        entity_id = _generate_id(entity_type, title, category or "general")

        # Merge metadata
        full_metadata = {
            "category": category,
            "languages": languages or [],
            "tags": tags or [],
            "added_at": datetime.now(UTC).isoformat(),
            **(metadata or {}),
        }

        # Create appropriate entity type
        if entity_type == "pattern":
            entity = Pattern(
                id=entity_id,
                entity_type=EntityType.PATTERN,
                name=title,
                description=content[:500] if len(content) > 500 else content,
                content=content,
                category=category or "",
                languages=languages or [],
                metadata=full_metadata,
            )
        else:
            # Default to Episode for temporal knowledge
            entity = Episode(
                id=entity_id,
                entity_type=EntityType.EPISODE,
                name=title,
                description=content[:500] if len(content) > 500 else content,
                content=content,
                metadata=full_metadata,
            )

        # Store in graph
        created_id = await entity_manager.create(entity)

        # TODO: Create relationships to related_to entities if provided

        return AddResponse(
            success=True,
            id=created_id,
            message=f"Added: {title}",
            timestamp=datetime.now(UTC),
        )

    except Exception as e:
        log.warning("add_failed", error=str(e))
        return AddResponse(
            success=False,
            id=None,
            message=f"Failed: {e}",
            timestamp=datetime.now(UTC),
        )


def _generate_id(prefix: str, *parts: str) -> str:
    """Generate a deterministic entity ID."""
    combined = ":".join(str(p)[:100] for p in parts)
    hash_bytes = hashlib.sha256(combined.encode()).hexdigest()[:12]
    return f"{prefix}_{hash_bytes}"


# Resource functions (Admin-only, exposed as MCP resources)

# Module-level state for uptime tracking
_server_start_time: float | None = None


async def get_health() -> dict[str, Any]:
    """Get server health status."""
    import time

    from sibyl.config import settings

    global _server_start_time  # noqa: PLW0603
    if _server_start_time is None:
        _server_start_time = time.time()

    health = {
        "status": "unknown",
        "server_name": settings.server_name,
        "uptime_seconds": int(time.time() - _server_start_time),
        "graph_connected": False,
        "entity_counts": {},
        "errors": [],
    }

    try:
        client = await get_graph_client()
        entity_manager = EntityManager(client)

        # Test connectivity
        health["graph_connected"] = True

        # Get entity counts
        for entity_type in [EntityType.PATTERN, EntityType.RULE, EntityType.EPISODE]:
            try:
                entities = await entity_manager.list_by_type(entity_type, limit=1000)
                health["entity_counts"][entity_type.value] = len(entities)
            except Exception:
                health["entity_counts"][entity_type.value] = -1

        health["status"] = "healthy"

    except Exception as e:
        health["status"] = "unhealthy"
        health["errors"].append(str(e))

    return health


async def get_stats() -> dict[str, Any]:
    """Get knowledge graph statistics."""
    try:
        client = await get_graph_client()
        entity_manager = EntityManager(client)

        stats = {
            "entity_counts": {},
            "total_entities": 0,
        }

        for entity_type in EntityType:
            try:
                entities = await entity_manager.list_by_type(entity_type, limit=10000)
                count = len(entities)
                stats["entity_counts"][entity_type.value] = count
                stats["total_entities"] += count
            except Exception:
                stats["entity_counts"][entity_type.value] = 0

        return stats

    except Exception as e:
        return {"error": str(e), "entity_counts": {}, "total_entities": 0}
