"""Unified tools for Sibyl MCP Server.

Four consolidated tools: search, explore, add, manage.
Replaces the previous 18+ specific tools with a generic interface.
"""

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

import structlog

from sibyl.graph.client import GraphClient, get_graph_client
from sibyl.graph.entities import EntityManager
from sibyl.graph.relationships import RelationshipManager
from sibyl.models.entities import EntityType, Episode, Pattern, Relationship, RelationshipType
from sibyl.models.tasks import Project, ProjectStatus, Task, TaskPriority, TaskStatus
from sibyl.retrieval import HybridConfig, hybrid_search, temporal_boost
from sibyl.utils.resilience import TIMEOUTS, with_timeout

log = structlog.get_logger()

# Valid entity types for filtering
VALID_ENTITY_TYPES = {t.value for t in EntityType}

# Validation constants
MAX_TITLE_LENGTH = 200
MAX_CONTENT_LENGTH = 50000


# =============================================================================
# Helper Functions - Entity Attribute Extraction
# =============================================================================


def _get_field(entity: Any, field: str, default: Any = None) -> Any:
    """Get field from entity object or its metadata, with fallback default."""
    value = getattr(entity, field, None)
    if value is None:
        value = entity.metadata.get(field, default)
    return value if value is not None else default


def _serialize_enum(value: Any) -> Any:
    """Serialize enum value to its string representation."""
    if value is None:
        return None
    return value.value if hasattr(value, "value") else value


def _build_entity_metadata(entity: Any) -> dict[str, Any]:
    """Build standardized metadata dict from entity with common fields."""
    status = _serialize_enum(_get_field(entity, "status"))
    priority = _serialize_enum(_get_field(entity, "priority"))

    extra = {
        "category": _get_field(entity, "category"),
        "languages": _get_field(entity, "languages"),
        "severity": _get_field(entity, "severity"),
        "template_type": _get_field(entity, "template_type"),
        "status": status,
        "priority": priority,
        "project_id": _get_field(entity, "project_id"),
        "assignees": _get_field(entity, "assignees"),
    }
    return {**entity.metadata, **{k: v for k, v in extra.items() if v is not None}}


# =============================================================================
# Auto-Tagging System
# =============================================================================

# Domain keywords for auto-tagging
_DOMAIN_KEYWORDS: dict[str, list[str]] = {
    "frontend": [
        "ui",
        "ux",
        "component",
        "react",
        "vue",
        "angular",
        "css",
        "style",
        "layout",
        "responsive",
        "animation",
        "button",
        "modal",
        "form",
        "input",
        "page",
        "view",
        "render",
        "display",
        "browser",
        "dom",
        "jsx",
        "tsx",
        "tailwind",
        "styled",
        "theme",
        "dark mode",
        "light mode",
    ],
    "backend": [
        "api",
        "server",
        "endpoint",
        "route",
        "handler",
        "controller",
        "service",
        "middleware",
        "database",
        "query",
        "model",
        "schema",
        "auth",
        "jwt",
        "token",
        "session",
        "fastapi",
        "flask",
        "django",
        "express",
        "graphql",
        "rest",
        "crud",
    ],
    "database": [
        "database",
        "db",
        "sql",
        "postgres",
        "mysql",
        "mongodb",
        "redis",
        "migration",
        "schema",
        "query",
        "index",
        "table",
        "collection",
        "falkordb",
        "graph",
        "cypher",
        "neo4j",
        "supabase",
    ],
    "devops": [
        "deploy",
        "docker",
        "kubernetes",
        "k8s",
        "ci",
        "cd",
        "pipeline",
        "github actions",
        "terraform",
        "aws",
        "gcp",
        "azure",
        "cloud",
        "nginx",
        "load balancer",
        "scaling",
        "monitoring",
    ],
    "testing": [
        "test",
        "spec",
        "pytest",
        "jest",
        "vitest",
        "unit test",
        "e2e",
        "integration",
        "mock",
        "fixture",
        "coverage",
        "tdd",
        "assertion",
    ],
    "docs": [
        "documentation",
        "readme",
        "docs",
        "comment",
        "docstring",
        "jsdoc",
        "api docs",
        "guide",
        "tutorial",
        "example",
    ],
    "security": [
        "security",
        "auth",
        "authentication",
        "authorization",
        "permission",
        "role",
        "acl",
        "xss",
        "csrf",
        "injection",
        "encrypt",
        "hash",
        "password",
        "secret",
        "vulnerability",
    ],
    "performance": [
        "performance",
        "optimize",
        "cache",
        "lazy",
        "memoize",
        "bundle",
        "minify",
        "compress",
        "speed",
        "latency",
        "profil",
        "benchmark",
    ],
}

# Task type keywords
_TYPE_KEYWORDS: dict[str, list[str]] = {
    "feature": [
        "add",
        "implement",
        "create",
        "build",
        "new",
        "introduce",
        "support",
    ],
    "bug": [
        "fix",
        "bug",
        "issue",
        "error",
        "broken",
        "crash",
        "fail",
        "wrong",
    ],
    "refactor": [
        "refactor",
        "clean",
        "reorganize",
        "restructure",
        "simplify",
        "improve",
        "extract",
        "consolidate",
        "dedup",
    ],
    "chore": [
        "update",
        "upgrade",
        "bump",
        "dependency",
        "deps",
        "config",
        "setup",
        "maintenance",
        "housekeeping",
    ],
    "research": [
        "research",
        "investigate",
        "explore",
        "spike",
        "poc",
        "prototype",
        "experiment",
        "evaluate",
        "compare",
    ],
}


def auto_tag_task(
    title: str,
    description: str,
    technologies: list[str] | None = None,
    domain: str | None = None,
    explicit_tags: list[str] | None = None,
    project_tags: list[str] | None = None,
) -> list[str]:
    """Generate tags automatically based on task content.

    Analyzes title, description, technologies, and domain to generate
    relevant tags. Prefers existing project tags when applicable.

    Args:
        title: Task title
        description: Task description
        technologies: List of technologies
        domain: Knowledge domain
        explicit_tags: Manually specified tags
        project_tags: Existing tags from project's tasks (for consistency)

    Returns:
        Deduplicated list of tags
    """
    tags: set[str] = set()
    text = f"{title} {description}".lower()

    # Normalize project tags for matching
    existing_tags = {t.lower().strip(): t for t in (project_tags or [])}

    # Start with explicit tags (normalized to lowercase)
    if explicit_tags:
        tags.update(t.lower().strip() for t in explicit_tags if t.strip())

    # Add domain as a tag if provided
    if domain:
        tags.add(domain.lower().strip())

    # Add technology-derived tags
    if technologies:
        for tech in technologies:
            normalized = tech.lower().strip()
            # Common tech name mappings to domains
            tech_tag = {
                "react": "frontend",
                "vue": "frontend",
                "angular": "frontend",
                "next.js": "frontend",
                "nextjs": "frontend",
                "tailwind": "frontend",
                "python": "backend",
                "fastapi": "backend",
                "django": "backend",
                "flask": "backend",
                "typescript": "typescript",
                "javascript": "javascript",
                "postgres": "database",
                "postgresql": "database",
                "mongodb": "database",
                "redis": "database",
                "docker": "devops",
                "kubernetes": "devops",
            }.get(normalized)
            if tech_tag:
                tags.add(tech_tag)
            # Also add the tech itself as a tag if it's short enough
            if len(normalized) <= 15:
                tags.add(normalized)

    # Check existing project tags first - prefer consistency
    for existing_lower in existing_tags:
        # Check if existing tag appears in text
        if existing_lower in text:
            tags.add(existing_lower)

    # Match domain keywords (only if not already matched from project tags)
    for tag, keywords in _DOMAIN_KEYWORDS.items():
        if tag not in tags:
            for keyword in keywords:
                if keyword in text:
                    tags.add(tag)
                    break

    # Match type keywords (only add the first match)
    type_tag = None
    for tag, keywords in _TYPE_KEYWORDS.items():
        for keyword in keywords:
            if keyword in text:
                type_tag = tag
                break
        if type_tag:
            break
    if type_tag:
        tags.add(type_tag)

    # Clean and sort tags
    return sorted(t for t in tags if t and len(t) >= 2)


async def get_project_tags(client: "GraphClient", project_id: str) -> list[str]:
    """Fetch all unique tags from a project's existing tasks.

    Args:
        client: Graph client instance
        project_id: Project ID to get tags from

    Returns:
        List of unique tags used in the project
    """
    import json

    try:
        # Query existing tasks in this project for their tags
        result = await client.driver.execute_query(
            """
            MATCH (n)
            WHERE (n:Episodic OR n:Entity)
              AND n.entity_type = 'task'
              AND n.project_id = $project_id
              AND n.tags IS NOT NULL
            RETURN DISTINCT n.tags as tags
            """,
            project_id=project_id,
        )

        all_tags: set[str] = set()
        for row in result.result_set:
            tags = row[0]
            if isinstance(tags, list):
                all_tags.update(t.lower() for t in tags if isinstance(t, str))
            elif isinstance(tags, str):
                # Handle JSON encoded list
                try:
                    parsed = json.loads(tags)
                    if isinstance(parsed, list):
                        all_tags.update(t.lower() for t in parsed if isinstance(t, str))
                except (json.JSONDecodeError, TypeError):
                    pass

        return sorted(all_tags)
    except Exception as e:
        log.debug("Failed to fetch project tags", error=str(e))
        return []


# =============================================================================
# Response Models
# =============================================================================


@dataclass
class SearchResult:
    """A single search result - unified across graph entities and documents."""

    id: str
    type: str
    name: str
    content: str
    score: float
    source: str | None = None
    url: str | None = None
    result_origin: Literal["graph", "document"] = "graph"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class SearchResponse:
    """Response from search operation - unified across graph and documents."""

    results: list[SearchResult]
    total: int
    query: str
    filters: dict[str, Any]
    graph_count: int = 0
    document_count: int = 0


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
    total: int  # Count of entities returned in this response
    filters: dict[str, Any]
    has_more: bool = False  # True if more results exist beyond the limit
    actual_total: int | None = None  # Actual total count in DB (if available)


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


async def _search_documents(
    query: str,
    source_id: str | None = None,
    source_name: str | None = None,
    language: str | None = None,
    limit: int = 10,
    include_content: bool = True,
) -> list[SearchResult]:
    """Search crawled documentation using pgvector similarity.

    Returns SearchResult objects for unified result merging.
    """
    try:
        from uuid import UUID

        from sqlalchemy import select
        from sqlmodel import col

        from sibyl.crawler.embedder import embed_text
        from sibyl.db import CrawledDocument, CrawlSource, DocumentChunk, get_session
        from sibyl.db.models import ChunkType

        # Generate query embedding
        query_embedding = await embed_text(query)

        async with get_session() as session:
            # Build similarity search query
            similarity_expr = 1 - DocumentChunk.embedding.cosine_distance(query_embedding)

            doc_query = (
                select(
                    DocumentChunk,
                    CrawledDocument,
                    CrawlSource.name.label("source_name"),
                    CrawlSource.id.label("source_id"),
                    similarity_expr.label("similarity"),
                )
                .join(CrawledDocument, DocumentChunk.document_id == CrawledDocument.id)
                .join(CrawlSource, CrawledDocument.source_id == CrawlSource.id)
                .where(col(DocumentChunk.embedding).is_not(None))
            )

            # Apply source filters
            if source_id:
                doc_query = doc_query.where(col(CrawlSource.id) == UUID(source_id))
            if source_name:
                doc_query = doc_query.where(col(CrawlSource.name).ilike(f"%{source_name}%"))

            # Apply language filter (for code chunks)
            if language:
                doc_query = doc_query.where(
                    (col(DocumentChunk.language).ilike(language))
                    | (col(DocumentChunk.chunk_type) != ChunkType.CODE)
                )

            # Order by similarity and limit
            doc_query = (
                doc_query.where(similarity_expr >= 0.5)  # Minimum threshold
                .order_by(similarity_expr.desc())
                .limit(limit)
            )

            result = await session.execute(doc_query)
            rows = result.all()

            # Convert to SearchResult
            results = []
            for chunk, doc, src_name, src_id, similarity in rows:
                content = chunk.content if include_content else chunk.content[:200]
                results.append(
                    SearchResult(
                        id=str(chunk.id),
                        type="document",
                        name=doc.title,
                        content=content,
                        score=float(similarity),
                        source=src_name,
                        url=doc.url,
                        result_origin="document",
                        metadata={
                            "document_id": str(doc.id),
                            "source_id": str(src_id),
                            "chunk_type": chunk.chunk_type.value
                            if hasattr(chunk.chunk_type, "value")
                            else str(chunk.chunk_type),
                            "chunk_index": chunk.chunk_index,
                            "heading_path": chunk.heading_path or [],
                            "language": chunk.language,
                            "has_code": doc.has_code,
                        },
                    )
                )
            return results

    except Exception as e:
        log.warning("document_search_failed", error=str(e))
        return []


async def search(  # noqa: PLR0915
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
    organization_id: str | None = None,
) -> SearchResponse:
    """Unified semantic search across knowledge graph AND documentation.

    Searches both Sibyl's knowledge graph (patterns, rules, episodes, tasks)
    AND crawled documentation (pgvector similarity search). Results are
    merged and ranked by relevance score.

    TASK MANAGEMENT WORKFLOW:
    For task searches, always include project filter:
    1. First: explore(mode="list", types=["project"]) - Identify the project
    2. Then: search("query", types=["task"], project="<project_id>") - Search within project

    USE CASES:
    • Find patterns/rules: search("OAuth authentication best practices")
    • Search documentation: search("Next.js middleware", source_name="next-dynenv")
    • Find tasks: search("", types=["task"], project="proj_abc", status="todo")
    • Search by language: search("async patterns", language="python")
    • Documentation only: search("hooks", include_graph=False)
    • Graph only: search("debugging", include_documents=False)

    Args:
        query: Natural language search query. Required.
        types: Entity types to search. Options: pattern, rule, template, topic,
               episode, task, project, document. Include 'document' to search docs.
        language: Filter by programming language (python, typescript, etc.).
        category: Filter by category/domain (authentication, database, api, etc.).
        status: Filter tasks by workflow status (backlog, todo, doing, etc.).
        project: Filter by project_id for tasks.
        source: Filter graph entities by source_id.
        source_id: Filter documents by source UUID.
        source_name: Filter documents by source name (partial match).
        assignee: Filter tasks by assignee name.
        since: Temporal filter - only return entities created after this ISO date.
        limit: Maximum results to return (1-50, default 10).
        include_content: Include full content in results (default True).
        include_documents: Include crawled documentation in search (default True).
        include_graph: Include knowledge graph entities in search (default True).
        use_enhanced: Use enhanced hybrid retrieval for graph (default True).
        boost_recent: Apply temporal boosting for graph results (default True).

    Returns:
        SearchResponse with ranked results from both sources, including
        graph_count and document_count for result breakdown.

    EXAMPLES:
        search("error handling patterns", types=["pattern"], language="python")
        search("Next.js routing", source_name="next-dynenv")
        search("", types=["task"], status="todo", project="proj_auth")
    """
    # Clamp limit
    limit = max(1, min(limit, 50))

    log.info(
        "unified_search",
        query=query[:100],
        types=types,
        language=language,
        category=category,
        status=status,
        project=project,
        source_id=source_id,
        source_name=source_name,
        include_documents=include_documents,
        include_graph=include_graph,
        limit=limit,
    )

    filters = {}
    if types:
        filters["types"] = types
    if language:
        filters["language"] = language
    if category:
        filters["category"] = category
    if status:
        filters["status"] = status
    if project:
        filters["project"] = project
    if source:
        filters["source"] = source
    if source_id:
        filters["source_id"] = source_id
    if source_name:
        filters["source_name"] = source_name
    if assignee:
        filters["assignee"] = assignee
    if since:
        filters["since"] = since

    # Determine if we should search documents based on types filter
    search_documents = include_documents
    search_graph = include_graph
    if types:
        # If 'document' is in types, search documents
        # If only 'document' is in types, skip graph search
        type_set = {t.lower() for t in types}
        if "document" in type_set:
            search_documents = True
            if type_set == {"document"}:
                search_graph = False
        elif source_id or source_name:
            # If source filters are set but document not in types, add document search
            search_documents = True
        else:
            # Types specified but document not included - skip document search
            search_documents = False

    graph_results: list[SearchResult] = []
    doc_results: list[SearchResult] = []

    # =========================================================================
    # GRAPH SEARCH - Search knowledge graph entities
    # =========================================================================
    if search_graph and query:
        try:
            client = await get_graph_client()
            if not organization_id:
                raise ValueError(
                    "organization_id is required - cannot access graph without org context"
                )
            entity_manager = EntityManager(client, group_id=organization_id)

            # Determine entity types to search (exclude 'document' - that's for doc search)
            entity_types = None
            if types:
                entity_types = []
                for t in types:
                    if t.lower() in VALID_ENTITY_TYPES and t.lower() != "document":
                        entity_types.append(EntityType(t.lower()))

            # Parse since date if provided
            since_date = None
            if since:
                try:
                    since_date = datetime.fromisoformat(since)
                except ValueError:
                    log.warning("invalid_since_date", since=since)

            # Perform search - try enhanced hybrid first, fall back to vector-only
            raw_results: list[tuple[Any, float]] = []

            if use_enhanced:
                try:
                    hybrid_config = HybridConfig(
                        apply_temporal=boost_recent,
                        temporal_decay_days=365.0,
                        graph_depth=2,
                    )

                    hybrid_result = await with_timeout(
                        hybrid_search(
                            query=query,
                            client=client,
                            entity_manager=entity_manager,
                            entity_types=entity_types,
                            limit=limit * 3,
                            config=hybrid_config,
                            group_id=organization_id,
                        ),
                        timeout_seconds=TIMEOUTS["search"],
                        operation_name="hybrid_search",
                    )
                    raw_results = hybrid_result.results
                    log.debug("graph_search_enhanced", results=len(raw_results))

                except Exception as e:
                    log.warning("enhanced_search_failed_fallback", error=str(e))

            # Fall back to vector-only search
            if not raw_results:
                raw_results = await with_timeout(
                    entity_manager.search(
                        query=query,
                        entity_types=entity_types,
                        limit=limit * 3,
                    ),
                    timeout_seconds=TIMEOUTS["search"],
                    operation_name="search",
                )
                if boost_recent and raw_results:
                    raw_results = temporal_boost(raw_results, decay_days=365.0)

            # Filter and convert to SearchResult
            for entity, score in raw_results:
                # Apply filters
                if language:
                    entity_langs = _get_field(entity, "languages", [])
                    if language.lower() not in [lang.lower() for lang in entity_langs]:
                        continue

                if category:
                    entity_cat = _get_field(entity, "category", "")
                    if category.lower() not in entity_cat.lower():
                        continue

                if status:
                    entity_status = _get_field(entity, "status")
                    if entity_status is None:
                        continue
                    status_val = _serialize_enum(entity_status)
                    if status.lower() != str(status_val).lower():
                        continue

                if project:
                    if _get_field(entity, "project_id") != project:
                        continue

                if source:
                    if _get_field(entity, "source_id") != source:
                        continue

                if assignee:
                    entity_assignees = _get_field(entity, "assignees", [])
                    if assignee.lower() not in [a.lower() for a in entity_assignees]:
                        continue

                if since_date:
                    entity_created = _get_field(entity, "created_at")
                    if entity_created:
                        try:
                            if isinstance(entity_created, str):
                                entity_created = datetime.fromisoformat(entity_created)
                            if entity_created < since_date:
                                continue
                        except (ValueError, TypeError):
                            pass

                content = ""
                if include_content:
                    content = entity.content[:500] if entity.content else entity.description
                else:
                    content = entity.description[:200] if entity.description else ""

                graph_results.append(
                    SearchResult(
                        id=entity.id,
                        type=entity.entity_type.value,
                        name=entity.name,
                        content=content or "",
                        score=score,
                        source=entity.source_file,
                        result_origin="graph",
                        metadata=_build_entity_metadata(entity),
                    )
                )

                if len(graph_results) >= limit:
                    break

        except Exception as e:
            log.warning("graph_search_failed", error=str(e))

    # =========================================================================
    # DOCUMENT SEARCH - Search crawled documentation
    # =========================================================================
    if search_documents and query:
        try:
            doc_results = await _search_documents(
                query=query,
                source_id=source_id,
                source_name=source_name,
                language=language,
                limit=limit,
                include_content=include_content,
            )
            log.debug("document_search_complete", results=len(doc_results))
        except Exception as e:
            log.warning("document_search_failed", error=str(e))

    # =========================================================================
    # MERGE AND RANK RESULTS
    # =========================================================================
    all_results = graph_results + doc_results

    # Sort by score descending
    all_results.sort(key=lambda r: r.score, reverse=True)

    # Limit to requested count
    final_results = all_results[:limit]

    return SearchResponse(
        results=final_results,
        total=len(final_results),
        query=query,
        filters=filters,
        graph_count=len([r for r in final_results if r.result_origin == "graph"]),
        document_count=len([r for r in final_results if r.result_origin == "document"]),
    )


# =============================================================================
# TOOL 2: explore
# =============================================================================


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
    organization_id: str | None = None,
) -> ExploreResponse:
    """Navigate and browse the Sibyl knowledge graph structure.

    Use this tool to explore entities and their relationships without
    semantic search. Ideal for browsing, listing, and graph traversal.

    MODES:
    • list: Browse entities by type with optional filters
    • related: Find entities directly connected to a specific entity
    • traverse: Multi-hop graph traversal (1-3 hops) from an entity
    • dependencies: Task dependency chains in topological order

    TASK MANAGEMENT WORKFLOW:
    For task operations, always use project-first approach:
    1. First: explore(mode="list", types=["project"]) - Find relevant project
    2. Then: explore(mode="list", types=["task"], project="<project_id>") - List tasks in project
    Prefer listing tasks with a project filter to keep results focused.

    USE CASES:
    • List projects first: explore(mode="list", types=["project"])
    • List tasks for project: explore(mode="list", types=["task"], project="proj_abc", status="todo")
    • Find related knowledge: explore(mode="related", entity_id="pattern_oauth")
    • Task dependency chain: explore(mode="dependencies", entity_id="task_123")
    • Explore project graph: explore(mode="traverse", entity_id="proj_abc", depth=2)

    Args:
        mode: Exploration mode - list, related, traverse, or dependencies.
        types: Entity types to include. Options: pattern, rule, template, topic,
               episode, task, project, source, document (for list mode).
        entity_id: Starting entity ID (required for related/traverse/dependencies).
        relationship_types: Filter edges by type - DEPENDS_ON, BELONGS_TO, RELATED_TO.
        depth: Traversal depth for traverse mode (1-3, default 1).
        language: Filter by programming language.
        category: Filter by category/domain.
        project: Optional project filter (recommended for task listing).
        status: Filter tasks by workflow status (backlog, todo, doing, blocked, review, done).
        limit: Maximum results (1-200, default 50).

    Returns:
        ExploreResponse with:
        - entities: List of matching entities
        - total: Count returned in this response
        - has_more: True if more results exist beyond limit
        - actual_total: Actual count matching filters (for pagination awareness)
        - filters: Applied filter criteria

    EXAMPLES:
        explore(mode="list", types=["project"])  # First: find projects
        explore(mode="list", types=["task"], project="proj_abc", status="todo")  # Then: tasks in project
        explore(mode="related", entity_id="pattern_oauth")
        explore(mode="dependencies", entity_id="task_123")
    """
    # Clamp values
    limit = max(1, min(limit, 200))
    depth = max(1, min(depth, 3))

    log.info(
        "explore",
        mode=mode,
        types=types,
        entity_id=entity_id,
        project=project,
        status=status,
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
    if project:
        filters["project"] = project
    if status:
        filters["status"] = status

    if not organization_id:
        raise ValueError("organization_id is required - cannot access graph without org context")

    try:
        if mode == "dependencies":
            return await _explore_dependencies(
                entity_id=entity_id,
                project=project,
                limit=limit,
                filters=filters,
                group_id=organization_id,
            )
        if mode in ("related", "traverse"):
            return await _explore_related(
                entity_id=entity_id,
                relationship_types=relationship_types,
                depth=depth if mode == "traverse" else 1,
                limit=limit,
                filters=filters,
                mode=mode,
                group_id=organization_id,
            )
        return await _explore_list(
            types=types,
            language=language,
            category=category,
            project=project,
            status=status,
            limit=limit,
            filters=filters,
            group_id=organization_id,
        )

    except Exception as e:
        log.warning("explore_failed", error=str(e), mode=mode)
        return ExploreResponse(mode=mode, entities=[], total=0, filters=filters)


async def _explore_list(
    types: list[str] | None,
    language: str | None,
    category: str | None,
    project: str | None,
    status: str | None,
    limit: int,
    filters: dict[str, Any],
    group_id: str,
) -> ExploreResponse:
    """List entities by type with filters."""
    client = await get_graph_client()
    entity_manager = EntityManager(client, group_id=group_id)

    # Default to listing all types if none specified
    target_types: list[EntityType] = []
    if types:
        target_types = [EntityType(t.lower()) for t in types if t.lower() in VALID_ENTITY_TYPES]
    else:
        # Default to common browsable types
        target_types = [
            EntityType.PATTERN,
            EntityType.RULE,
            EntityType.TEMPLATE,
            EntityType.TOPIC,
        ]

    # Fetch more than limit to detect has_more and apply filters
    fetch_limit = limit + 50  # Over-fetch to detect pagination
    all_entities = []
    for entity_type in target_types:
        entities = await entity_manager.list_by_type(entity_type, limit=fetch_limit)
        all_entities.extend(entities)

    # Apply filters and convert to summaries
    filtered_entities = []
    for entity in all_entities:
        # Language filter
        if language:
            entity_langs = _get_field(entity, "languages", [])
            if language.lower() not in [lang.lower() for lang in entity_langs]:
                continue

        # Category filter
        if category:
            entity_cat = _get_field(entity, "category", "")
            if category.lower() not in entity_cat.lower():
                continue

        # Project filter (for tasks)
        if project:
            if _get_field(entity, "project_id") != project:
                continue

        # Status filter (for tasks)
        if status:
            entity_status = _get_field(entity, "status")
            if entity_status is None:
                continue
            status_val = _serialize_enum(entity_status)
            if status.lower() != str(status_val).lower():
                continue

        filtered_entities.append(entity)

    # Determine pagination
    actual_total = len(filtered_entities)
    has_more = actual_total > limit

    # Build result summaries (limited)
    results = [
        EntitySummary(
            id=entity.id,
            type=entity.entity_type.value,
            name=entity.name,
            description=entity.description[:200] if entity.description else "",
            metadata=_build_entity_metadata(entity),
        )
        for entity in filtered_entities[:limit]
    ]

    return ExploreResponse(
        mode="list",
        entities=results,
        total=len(results),
        filters=filters,
        has_more=has_more,
        actual_total=actual_total,
    )


@dataclass
class DependencyNode:
    """A task in a dependency chain."""

    id: str
    name: str
    status: str | None
    depth: int  # Distance from root task
    is_blocking: bool  # True if this blocks the root task


async def _explore_dependencies(
    entity_id: str | None,
    project: str | None,
    limit: int,
    filters: dict[str, Any],
    group_id: str,
) -> ExploreResponse:
    """Traverse task dependency chains with topological sorting.

    Returns tasks in dependency order (dependencies before dependents).
    Detects and reports circular dependencies.
    """
    if not entity_id:
        return ExploreResponse(
            mode="dependencies",
            entities=[],
            total=0,
            filters={**filters, "error": "entity_id required for dependencies mode"},
        )

    client = await get_graph_client()
    relationship_manager = RelationshipManager(client, group_id=group_id)
    entity_manager = EntityManager(client, group_id=group_id)

    # Track visited nodes and detect cycles
    visited: set[str] = set()
    in_stack: set[str] = set()  # For cycle detection
    dependency_order: list[tuple[str, int]] = []  # (entity_id, depth)
    circular_deps: list[tuple[str, str]] = []  # Detected cycles

    async def traverse_dependencies(task_id: str, depth: int = 0) -> None:
        """DFS traversal to build dependency order."""
        if task_id in in_stack:
            # Cycle detected
            circular_deps.append((entity_id or "", task_id))
            return

        if task_id in visited:
            return

        visited.add(task_id)
        in_stack.add(task_id)

        # Get DEPENDS_ON relationships (tasks this task depends on)
        deps = await relationship_manager.get_related_entities(
            entity_id=task_id,
            relationship_types=[RelationshipType.DEPENDS_ON],
            max_depth=1,
            limit=100,
        )

        for dep_entity, rel in deps:
            # Only follow outgoing DEPENDS_ON (this task depends on dep_entity)
            if rel.source_id == task_id:
                # Apply project filter if specified
                if project:
                    dep_project = getattr(
                        dep_entity, "project_id", None
                    ) or dep_entity.metadata.get("project_id")
                    if dep_project != project:
                        continue
                await traverse_dependencies(dep_entity.id, depth + 1)

        in_stack.remove(task_id)
        dependency_order.append((task_id, depth))

    # Start traversal from the given entity
    await traverse_dependencies(entity_id)

    # dependency_order is in reverse topological order (dependencies first)
    # Reverse it so dependencies come first
    dependency_order.reverse()

    # Build result entities
    results: list[EntitySummary] = []
    for task_id, depth in dependency_order[:limit]:
        try:
            entity = await entity_manager.get(task_id)
            if entity:
                raw_status = getattr(entity, "status", None) or entity.metadata.get("status")
                status_value = (
                    raw_status.value if raw_status and hasattr(raw_status, "value") else raw_status
                )

                results.append(
                    EntitySummary(
                        id=entity.id,
                        type=entity.entity_type.value,
                        name=entity.name,
                        description=entity.description[:200] if entity.description else "",
                        metadata={
                            "status": status_value,
                            "depth": depth,
                            "is_root": entity.id == entity_id,
                            "project_id": getattr(entity, "project_id", None)
                            or entity.metadata.get("project_id"),
                        },
                    )
                )
        except Exception:
            log.warning("dependency_entity_fetch_failed", task_id=task_id)

    # Add circular dependency warning to filters if detected
    result_filters = {**filters}
    if circular_deps:
        result_filters["circular_dependencies"] = [
            {"from": c[0], "to": c[1]} for c in circular_deps
        ]
        result_filters["warning"] = "Circular dependencies detected"

    return ExploreResponse(
        mode="dependencies",
        entities=results,
        total=len(results),
        filters=result_filters,
    )


async def _explore_related(
    entity_id: str | None,
    relationship_types: list[str] | None,
    depth: int,
    limit: int,
    filters: dict[str, Any],
    mode: str,
    group_id: str,
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
    relationship_manager = RelationshipManager(client, group_id=group_id)

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
        max_depth=depth,
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


async def add(  # noqa: PLR0915
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
) -> AddResponse:
    """Add new knowledge to the Sibyl knowledge graph.

    Use this tool to create entities with automatic relationship discovery.
    Supports episodes (learnings), patterns, tasks, and projects.

    ENTITY TYPES:
    • episode: Temporal knowledge snapshot (default) - insights, learnings, discoveries
    • pattern: Coding pattern or best practice
    • task: Work item with workflow state machine
    • project: Container for related tasks

    USE CASES:
    • Record a learning: add("Redis pooling insight", "Discovered that...", category="debugging")
    • Create a pattern: add("Error handling pattern", "...", entity_type="pattern", languages=["python"])
    • Create a task: add("Implement OAuth", "...", entity_type="task", project="sibyl-project", priority="high")
    • Create a project: add("Auth System", "...", entity_type="project", repository_url="...")
    • Auto-link to related knowledge: add("OAuth insight", "...", auto_link=True)

    IMPORTANT: Tasks REQUIRE a project. Always specify project="<project_id>" when creating tasks.
    Use explore(mode="list", types=["project"]) to find available projects first.

    Args:
        title: Short title (max 200 chars).
        content: Full content/description (max 50k chars).
        entity_type: Type to create - episode (default), pattern, task, project.
        category: Domain category (authentication, database, api, debugging, etc.).
        languages: Programming languages (python, typescript, rust, etc.).
        tags: Searchable tags for discovery.
        related_to: Entity IDs to explicitly link (creates RELATED_TO edges).
        metadata: Additional structured data.
        project: Project ID for tasks (REQUIRED for tasks, creates BELONGS_TO edge).
        priority: Task priority - critical, high, medium (default), low, someday.
        assignees: List of assignee names for tasks.
        due_date: Due date for tasks (ISO format: 2024-03-15).
        technologies: Technologies involved (for tasks).
        depends_on: Task IDs this depends on (creates DEPENDS_ON edges).
        repository_url: Repository URL for projects.
        auto_link: Auto-discover related patterns/rules/templates (similarity > 0.75).

    Returns:
        AddResponse with created entity ID, auto-discovered links, and timestamp.

    EXAMPLES:
        add("OAuth redirect bug", "Fixed issue where...", category="debugging", languages=["python"])
        add("Add user auth", "Implement login flow", entity_type="task", project="proj_web", priority="high")
        add("E-commerce API", "Backend services for...", entity_type="project", repository_url="github.com/...")
        add("Connection pooling pattern", "Best practice for...", entity_type="pattern", auto_link=True)
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
        org_id = (metadata or {}).get("organization_id") or (metadata or {}).get("group_id")
        if not org_id:
            raise ValueError(
                "organization_id is required in metadata - cannot create entity without org context"
            )
        org_id = str(org_id)
        entity_manager = EntityManager(client, group_id=org_id)

        # Generate deterministic ID
        entity_id = _generate_id(entity_type, title, category or "general")

        # Merge metadata
        full_metadata = {
            "category": category,
            "languages": languages or [],
            "tags": tags or [],
            "added_at": datetime.now(UTC).isoformat(),
            "organization_id": org_id,
            **(metadata or {}),
        }

        # Create appropriate entity type
        entity: Episode | Pattern | Task | Project
        relationship_manager = RelationshipManager(client, group_id=org_id)

        if entity_type == "task":
            # Parse due date if provided
            parsed_due_date = None
            if due_date:
                try:
                    parsed_due_date = datetime.fromisoformat(due_date)
                except ValueError:
                    log.warning("invalid_due_date", due_date=due_date)

            # Parse priority
            task_priority = TaskPriority.MEDIUM
            if priority:
                try:
                    task_priority = TaskPriority(priority.lower())
                except ValueError:
                    log.warning("invalid_priority", priority=priority)

            # Get existing project tags for consistency (when project-scoped)
            project_tags = await get_project_tags(client, project) if project else []

            # Auto-generate tags based on task content + project context
            task_technologies = technologies or languages or []
            auto_tags = auto_tag_task(
                title=title,
                description=content,
                technologies=task_technologies,
                domain=category,
                explicit_tags=tags,
                project_tags=project_tags,
            )
            full_metadata["tags"] = auto_tags

            log.debug(
                "auto_tags_generated",
                tags=auto_tags,
                count=len(auto_tags),
                project_tags_used=len(project_tags),
            )

            entity = Task(  # type: ignore[call-arg]  # model_validator sets name from title
                id=entity_id,
                title=title,
                description=content,
                status=TaskStatus.TODO,
                priority=task_priority,
                project_id=project or None,
                assignees=assignees or [],
                due_date=parsed_due_date,
                technologies=task_technologies,
                domain=category,
                tags=auto_tags,
                metadata=full_metadata,
            )

        elif entity_type == "project":
            entity = Project(  # type: ignore[call-arg]  # model_validator sets name from title
                id=entity_id,
                title=title,
                description=content,
                status=ProjectStatus.ACTIVE,
                repository_url=repository_url,
                tech_stack=technologies or languages or [],
                tags=tags or [],
                metadata=full_metadata,
            )

        elif entity_type == "pattern":
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
        # Use fast path (direct insert) for structured entities (tasks, projects)
        # Use full Graphiti flow for knowledge entities (episodes, patterns)
        use_fast_path = entity_type in ("task", "project")

        if use_fast_path:
            # Fast path: direct insert (~50ms) + background enrichment
            created_id = await entity_manager.create_direct(entity)

            # Queue background enrichment for embeddings
            try:
                from sibyl.background import get_background_queue

                queue = get_background_queue()
                await queue.enqueue(
                    "enrich_entity",
                    {
                        "entity_id": created_id,
                        "title": title,
                        "content": content,
                        "find_related": False,  # Skip for now, can enable later
                    },
                )
            except Exception as e:
                # Don't fail the request if background queue isn't running
                log.debug("Background enrichment skipped", error=str(e))
        else:
            # Full Graphiti flow for knowledge entities (~5s)
            created_id = await entity_manager.create(entity)

        # Create relationships
        relationships_created = []

        # Task -> Project (BELONGS_TO)
        if entity_type == "task" and project:
            try:
                rel = Relationship(
                    id=f"rel_{created_id}_belongs_to_{project}",
                    source_id=created_id,
                    target_id=project,
                    relationship_type=RelationshipType.BELONGS_TO,
                    metadata={"created_at": datetime.now(UTC).isoformat()},
                )
                await relationship_manager.create(rel)
                relationships_created.append(f"BELONGS_TO:{project}")
            except Exception as e:
                log.warning("relationship_creation_failed", error=str(e), type="BELONGS_TO")

        # Task -> Task (DEPENDS_ON)
        if entity_type == "task" and depends_on:
            for dep_id in depends_on:
                try:
                    rel = Relationship(
                        id=f"rel_{created_id}_depends_on_{dep_id}",
                        source_id=created_id,
                        target_id=dep_id,
                        relationship_type=RelationshipType.DEPENDS_ON,
                        metadata={"created_at": datetime.now(UTC).isoformat()},
                    )
                    await relationship_manager.create(rel)
                    relationships_created.append(f"DEPENDS_ON:{dep_id}")
                except Exception as e:
                    log.warning(
                        "relationship_creation_failed",
                        error=str(e),
                        type="DEPENDS_ON",
                        target=dep_id,
                    )

        # Generic RELATED_TO relationships
        if related_to:
            for related_id in related_to:
                try:
                    rel = Relationship(
                        id=f"rel_{created_id}_related_to_{related_id}",
                        source_id=created_id,
                        target_id=related_id,
                        relationship_type=RelationshipType.RELATED_TO,
                        metadata={"created_at": datetime.now(UTC).isoformat()},
                    )
                    await relationship_manager.create(rel)
                    relationships_created.append(f"RELATED_TO:{related_id}")
                except Exception as e:
                    log.warning(
                        "relationship_creation_failed",
                        error=str(e),
                        type="RELATED_TO",
                        target=related_id,
                    )

        # Auto-link: discover and create REFERENCES relationships
        if auto_link:
            try:
                auto_link_results = await _auto_discover_links(
                    entity_manager=entity_manager,
                    title=title,
                    content=content,
                    technologies=technologies or languages or [],
                    category=category,
                    exclude_id=created_id,
                    threshold=0.75,
                    limit=5,
                )

                for linked_id, score in auto_link_results:
                    try:
                        rel = Relationship(
                            id=f"rel_{created_id}_references_{linked_id}",
                            source_id=created_id,
                            target_id=linked_id,
                            relationship_type=RelationshipType.RELATED_TO,  # Use RELATED_TO for auto-links
                            metadata={
                                "created_at": datetime.now(UTC).isoformat(),
                                "auto_linked": True,
                                "similarity_score": score,
                            },
                        )
                        await relationship_manager.create(rel)
                        relationships_created.append(f"AUTO:{linked_id[:8]}...")
                    except Exception as e:
                        log.warning("auto_link_failed", error=str(e), target=linked_id)

                log.info(
                    "auto_link_complete", entity_id=created_id, links_found=len(auto_link_results)
                )
            except Exception as e:
                log.warning("auto_link_search_failed", error=str(e))

        message = f"Added: {title}"
        if relationships_created:
            message += f" (linked: {len(relationships_created)})"

        return AddResponse(
            success=True,
            id=created_id,
            message=message,
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


async def _auto_discover_links(
    entity_manager: EntityManager,
    title: str,
    content: str,
    technologies: list[str],
    category: str | None,
    exclude_id: str,
    threshold: float = 0.75,
    limit: int = 5,
) -> list[tuple[str, float]]:
    """Discover related entities for auto-linking.

    Searches for patterns, rules, templates, and topics that are
    semantically similar to the new entity.

    Args:
        entity_manager: Entity manager for search.
        title: Entity title.
        content: Entity content.
        technologies: Technologies to include in search.
        category: Category/domain for filtering.
        exclude_id: ID to exclude from results (the new entity).
        threshold: Minimum similarity score (0-1).
        limit: Maximum links to discover.

    Returns:
        List of (entity_id, score) tuples above threshold.
    """
    # Build search query from title, content summary, and technologies
    tech_str = ", ".join(technologies[:5]) if technologies else ""
    query_parts = [title]
    if content:
        # Take first 200 chars of content
        query_parts.append(content[:200])
    if tech_str:
        query_parts.append(tech_str)
    if category:
        query_parts.append(category)

    query = " ".join(query_parts)

    # Search for linkable entity types
    linkable_types = [
        EntityType.PATTERN,
        EntityType.RULE,
        EntityType.TEMPLATE,
        EntityType.TOPIC,
    ]

    try:
        results = await entity_manager.search(
            query=query,
            entity_types=linkable_types,
            limit=limit * 2,  # Over-fetch to filter by threshold
        )

        # Filter by threshold and exclude self
        links: list[tuple[str, float]] = []
        for entity, score in results:
            if entity.id == exclude_id:
                continue
            if score >= threshold:
                links.append((entity.id, score))
            if len(links) >= limit:
                break

        return links

    except Exception as e:
        log.warning("auto_discover_search_failed", error=str(e))
        return []


# =============================================================================
# TOOL 4: manage - MOVED to sibyl/tools/manage.py
# =============================================================================
# The canonical manage() implementation with all action categories
# (task workflow, source operations, analysis, admin) is in tools/manage.py.
# Import from there: from sibyl.tools.manage import manage, ManageResponse


# Removed: ManageResponse class (duplicate of manage.py)
# Removed: manage() function (duplicate with limited actions)


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


async def get_stats(organization_id: str | None = None) -> dict[str, Any]:
    """Get knowledge graph statistics.

    Uses a single aggregation query for performance instead of N separate queries.

    Args:
        organization_id: Organization ID to scope stats to (required).

    Raises:
        ValueError: If organization_id is not provided.
    """
    if not organization_id:
        raise ValueError("organization_id is required - cannot get stats without org context")

    try:
        client = await get_graph_client()

        # Clone driver for org-specific graph (multi-tenancy)
        driver = client.client.driver.clone(organization_id)

        # Single aggregation query - much faster than N separate list queries
        result = await driver.execute_query(
            """
            MATCH (n)
            WHERE n.entity_type IS NOT NULL
            RETURN n.entity_type as type, count(*) as count
            """
        )

        stats: dict[str, Any] = {
            "entity_counts": {},
            "total_entities": 0,
        }

        # Initialize all known types to 0
        for entity_type in EntityType:
            stats["entity_counts"][entity_type.value] = 0

        # Fill in actual counts from query
        data = GraphClient.normalize_result(result)
        for row in data:
            if isinstance(row, dict):
                etype = row.get("type")
                count = row.get("count", 0)
            else:
                etype, count = row[0], row[1]

            if etype:
                stats["entity_counts"][etype] = count
                stats["total_entities"] += count

        return stats

    except Exception as e:
        return {"error": str(e), "entity_counts": {}, "total_entities": 0}
