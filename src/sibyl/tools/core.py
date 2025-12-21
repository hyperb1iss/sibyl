"""Unified tools for Sibyl MCP Server.

Four consolidated tools: search, explore, add, manage.
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
from sibyl.models.entities import EntityType, Episode, Pattern, Relationship, RelationshipType
from sibyl.models.tasks import Project, ProjectStatus, Task, TaskPriority, TaskStatus
from sibyl.retrieval import HybridConfig, hybrid_search, temporal_boost
from sibyl.tasks.workflow import TaskWorkflowEngine
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


async def search(  # noqa: PLR0915
    query: str,
    types: list[str] | None = None,
    language: str | None = None,
    category: str | None = None,
    status: str | None = None,
    project: str | None = None,
    source: str | None = None,
    assignee: str | None = None,
    since: str | None = None,
    limit: int = 10,
    include_content: bool = True,
    use_enhanced: bool = True,
    boost_recent: bool = True,
) -> SearchResponse:
    """Search the Sibyl knowledge graph by meaning.

    Use this tool to find knowledge across all entity types using natural
    language queries. Results are ranked by semantic similarity.

    USE CASES:
    • Find patterns/rules related to a technology: search("OAuth authentication best practices")
    • Find open tasks: search("", types=["task"], status="doing")
    • Find knowledge by technology: search("", language="python", types=["pattern"])
    • Find recent learnings: search("", types=["episode"], since="2024-01-01")
    • Find tasks for a project: search("", types=["task"], project="proj_abc")
    • Find documentation: search("hooks state management", types=["document"])

    Args:
        query: Natural language search query. Can be empty if using filters.
        types: Entity types to search. Options: pattern, rule, template, topic,
               episode, task, project, source, document. If None, searches all.
        language: Filter by programming language (python, typescript, rust, etc.).
        category: Filter by category/domain (authentication, database, api, etc.).
        status: Filter tasks by workflow status (backlog, todo, doing, blocked, review, done).
        project: Filter by project_id to find tasks within a specific project.
        source: Filter documents by source_id (documentation source).
        assignee: Filter tasks by assignee name.
        since: Temporal filter - only return entities created after this ISO date.
        limit: Maximum results to return (1-50, default 10).
        include_content: Include full content in results (default True).
        use_enhanced: Use enhanced Graph-RAG retrieval (hybrid search + temporal boosting).
                      Falls back to vector-only if hybrid fails. Default True.
        boost_recent: Apply temporal boosting to rank recent knowledge higher. Default True.

    Returns:
        SearchResponse with ranked results including id, name, type, score, and metadata.

    EXAMPLES:
        search("error handling patterns", types=["pattern"], language="python")
        search("", types=["task"], status="todo", project="proj_auth")
        search("database optimization", types=["pattern", "episode"])
    """
    # Clamp limit
    limit = max(1, min(limit, 50))

    log.info(
        "search",
        query=query[:100],
        types=types,
        language=language,
        category=category,
        status=status,
        project=project,
        assignee=assignee,
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
    if assignee:
        filters["assignee"] = assignee
    if since:
        filters["since"] = since

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

        # Parse since date if provided
        since_date = None
        if since:
            try:
                since_date = datetime.fromisoformat(since.replace("Z", "+00:00"))
            except ValueError:
                log.warning("invalid_since_date", since=since)

        # Perform search - try enhanced hybrid first, fall back to vector-only
        raw_results: list[tuple[Any, float]] = []

        if use_enhanced and query:  # Enhanced only makes sense with a query
            try:
                # Configure hybrid search
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
                        limit=limit * 3,  # Over-fetch for filtering
                        config=hybrid_config,
                    ),
                    timeout_seconds=TIMEOUTS["search"],
                    operation_name="hybrid_search",
                )
                raw_results = hybrid_result.results
                log.debug("search_used_enhanced", results=len(raw_results))

            except Exception as e:
                log.warning("enhanced_search_failed_fallback", error=str(e))
                # Fall through to vector-only search

        # Fall back to vector-only search if enhanced failed or disabled
        if not raw_results:
            raw_results = await with_timeout(
                entity_manager.search(
                    query=query,
                    entity_types=entity_types,
                    limit=limit * 3,  # Over-fetch for filtering
                ),
                timeout_seconds=TIMEOUTS["search"],
                operation_name="search",
            )

            # Apply temporal boosting to vector results if requested
            if boost_recent and raw_results:
                raw_results = temporal_boost(raw_results, decay_days=365.0)

        results = []
        for entity, score in raw_results:
            # Apply language filter
            if language:
                entity_langs = _get_field(entity, "languages", [])
                if language.lower() not in [lang.lower() for lang in entity_langs]:
                    continue

            # Apply category filter
            if category:
                entity_cat = _get_field(entity, "category", "")
                if category.lower() not in entity_cat.lower():
                    continue

            # Apply status filter (for tasks)
            if status:
                entity_status = _get_field(entity, "status")
                if entity_status is None:
                    continue
                status_val = _serialize_enum(entity_status)
                if status.lower() != str(status_val).lower():
                    continue

            # Apply project filter
            if project:
                if _get_field(entity, "project_id") != project:
                    continue

            # Apply source filter
            if source:
                if _get_field(entity, "source_id") != source:
                    continue

            # Apply assignee filter (for tasks)
            if assignee:
                entity_assignees = _get_field(entity, "assignees", [])
                if assignee.lower() not in [a.lower() for a in entity_assignees]:
                    continue

            # Apply temporal filter
            if since_date:
                entity_created = _get_field(entity, "created_at")
                if entity_created:
                    try:
                        if isinstance(entity_created, str):
                            entity_created = datetime.fromisoformat(
                                entity_created.replace("Z", "+00:00")
                            )
                        if entity_created < since_date:
                            continue
                    except (ValueError, TypeError):
                        pass  # Skip temporal filter if date parsing fails

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
                    metadata=_build_entity_metadata(entity),
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
) -> ExploreResponse:
    """Navigate and browse the Sibyl knowledge graph structure.

    Use this tool to explore entities and their relationships without
    semantic search. Ideal for browsing, listing, and graph traversal.

    MODES:
    • list: Browse entities by type with optional filters
    • related: Find entities directly connected to a specific entity
    • traverse: Multi-hop graph traversal (1-3 hops) from an entity
    • dependencies: Task dependency chains in topological order

    USE CASES:
    • List all patterns: explore(mode="list", types=["pattern"])
    • List open tasks for project: explore(mode="list", types=["task"], project="proj_abc", status="todo")
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
        project: Filter by project_id (especially for task listing).
        status: Filter tasks by workflow status (backlog, todo, doing, blocked, review, done).
        limit: Maximum results (1-200, default 50).

    Returns:
        ExploreResponse with entities, relationships, and navigation metadata.

    EXAMPLES:
        explore(mode="list", types=["task"], status="doing")
        explore(mode="related", entity_id="pattern_oauth")
        explore(mode="dependencies", project="proj_auth")
        explore(mode="traverse", entity_id="task_abc", depth=2, relationship_types=["DEPENDS_ON"])
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

    try:
        if mode == "dependencies":
            return await _explore_dependencies(
                entity_id=entity_id,
                project=project,
                limit=limit,
                filters=filters,
            )
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
            project=project,
            status=status,
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
    project: str | None,
    status: str | None,
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
        entities = await entity_manager.list_by_type(
            entity_type, limit=limit * 2
        )  # Over-fetch for filtering
        all_entities.extend(entities)

    # Apply filters and convert to summaries
    results = []
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

        results.append(
            EntitySummary(
                id=entity.id,
                type=entity.entity_type.value,
                name=entity.name,
                description=entity.description[:200] if entity.description else "",
                metadata=_build_entity_metadata(entity),
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
    relationship_manager = RelationshipManager(client)
    entity_manager = EntityManager(client)

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
            depth=1,
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
    • Create a task: add("Implement OAuth", "...", entity_type="task", project="proj_auth", priority="high")
    • Create a project: add("Auth System", "...", entity_type="project", repository_url="...")
    • Auto-link to related knowledge: add("OAuth insight", "...", auto_link=True)

    Args:
        title: Short title (max 200 chars).
        content: Full content/description (max 50k chars).
        entity_type: Type to create - episode (default), pattern, task, project.
        category: Domain category (authentication, database, api, debugging, etc.).
        languages: Programming languages (python, typescript, rust, etc.).
        tags: Searchable tags for discovery.
        related_to: Entity IDs to explicitly link (creates RELATED_TO edges).
        metadata: Additional structured data.
        project: Project ID for tasks (creates BELONGS_TO edge).
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
        entity: Episode | Pattern | Task | Project
        relationship_manager = RelationshipManager(client)

        if entity_type == "task":
            # Parse due date if provided
            parsed_due_date = None
            if due_date:
                try:
                    parsed_due_date = datetime.fromisoformat(due_date.replace("Z", "+00:00"))
                except ValueError:
                    log.warning("invalid_due_date", due_date=due_date)

            # Parse priority
            task_priority = TaskPriority.MEDIUM
            if priority:
                try:
                    task_priority = TaskPriority(priority.lower())
                except ValueError:
                    log.warning("invalid_priority", priority=priority)

            entity = Task(
                id=entity_id,
                title=title,
                description=content,
                status=TaskStatus.TODO,
                priority=task_priority,
                project_id=project,
                assignees=assignees or [],
                due_date=parsed_due_date,
                technologies=technologies or languages or [],
                domain=category,
                metadata=full_metadata,
            )

        elif entity_type == "project":
            entity = Project(
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
# TOOL 4: manage
# =============================================================================


@dataclass
class ManageResponse:
    """Response from manage operation."""

    success: bool
    action: str
    entity_id: str | None
    message: str
    data: dict[str, Any] = field(default_factory=dict)


async def manage(
    action: Literal[
        "start_task",
        "block_task",
        "unblock_task",
        "submit_review",
        "complete_task",
        "archive",
        "health",
    ],
    entity_id: str | None = None,
    # Task workflow parameters
    assignee: str | None = None,
    blocker: str | None = None,
    commit_shas: list[str] | None = None,
    pr_url: str | None = None,
    actual_hours: float | None = None,
    learnings: str | None = None,
) -> ManageResponse:
    """Manage task workflows and administrative operations.

    Use this tool for task state machine transitions and system management.
    Provides structured workflow actions with automatic metadata capture.

    TASK WORKFLOW (state machine):
    backlog → todo → doing → blocked ↔ doing → review → done → archived

    ACTIONS:
    • start_task: Begin work (backlog/todo→doing), auto-generates branch name
    • block_task: Mark blocked with reason (doing→blocked)
    • unblock_task: Resume work (blocked→doing)
    • submit_review: Submit for review with commits/PR (doing→review)
    • complete_task: Mark done with learnings captured (review→done)
    • archive: Archive completed task (done→archived)
    • health: System health check (no entity_id needed)

    USE CASES:
    • Start working on a task: manage("start_task", entity_id="task_abc", assignee="alice")
    • Block with reason: manage("block_task", entity_id="task_abc", blocker="Waiting on API access")
    • Submit for review: manage("submit_review", entity_id="task_abc", pr_url="github.com/.../pull/42")
    • Complete with learnings: manage("complete_task", entity_id="task_abc", actual_hours=4.5, learnings="...")
    • Check system health: manage("health")

    Args:
        action: Workflow action to perform.
        entity_id: Task ID (required for task actions, optional for health).
        assignee: User starting the task (for start_task).
        blocker: Description of blocking issue (for block_task).
        commit_shas: Git commit SHAs associated with work (for submit_review).
        pr_url: Pull request URL for review (for submit_review).
        actual_hours: Actual time spent in hours (for complete_task).
        learnings: Knowledge captured during task (for complete_task, stored as episode).

    Returns:
        ManageResponse with success status, action result, and any generated data
        (e.g., branch name for start_task, captured learnings for complete_task).

    EXAMPLES:
        manage("start_task", entity_id="task_123", assignee="alice")
        manage("block_task", entity_id="task_123", blocker="Need design approval")
        manage("submit_review", entity_id="task_123", commit_shas=["abc123"], pr_url="...")
        manage("complete_task", entity_id="task_123", actual_hours=3.0, learnings="OAuth requires...")
        manage("health")
    """
    log.info(
        "manage",
        action=action,
        entity_id=entity_id,
    )

    try:
        # Health check doesn't need entity_id
        if action == "health":
            health_data = await get_health()
            return ManageResponse(
                success=True,
                action=action,
                entity_id=None,
                message=f"System status: {health_data.get('status', 'unknown')}",
                data=health_data,
            )

        # All other actions require entity_id
        if not entity_id:
            return ManageResponse(
                success=False,
                action=action,
                entity_id=None,
                message="entity_id is required for this action",
            )

        client = await get_graph_client()
        entity_manager = EntityManager(client)
        relationship_manager = RelationshipManager(client)
        workflow = TaskWorkflowEngine(entity_manager, relationship_manager, client)

        if action == "start_task":
            if not assignee:
                return ManageResponse(
                    success=False,
                    action=action,
                    entity_id=entity_id,
                    message="assignee is required for start_task",
                )
            task = await workflow.start_task(entity_id, assignee)
            return ManageResponse(
                success=True,
                action=action,
                entity_id=entity_id,
                message=f"Task started: {task.title}",
                data={
                    "status": task.status.value,
                    "branch_name": task.branch_name,
                    "assignees": task.assignees,
                    "started_at": task.started_at.isoformat() if task.started_at else None,
                },
            )

        if action == "block_task":
            if not blocker:
                return ManageResponse(
                    success=False,
                    action=action,
                    entity_id=entity_id,
                    message="blocker description is required for block_task",
                )
            task = await workflow.block_task(entity_id, blocker)
            return ManageResponse(
                success=True,
                action=action,
                entity_id=entity_id,
                message=f"Task blocked: {blocker}",
                data={
                    "status": task.status.value,
                    "blockers": task.blockers_encountered,
                },
            )

        if action == "unblock_task":
            task = await workflow.unblock_task(entity_id)
            return ManageResponse(
                success=True,
                action=action,
                entity_id=entity_id,
                message="Task unblocked",
                data={"status": task.status.value},
            )

        if action == "submit_review":
            task = await workflow.submit_for_review(
                entity_id,
                commit_shas=commit_shas or [],
                pr_url=pr_url,
            )
            return ManageResponse(
                success=True,
                action=action,
                entity_id=entity_id,
                message="Task submitted for review",
                data={
                    "status": task.status.value,
                    "pr_url": task.pr_url,
                    "commit_shas": task.commit_shas,
                },
            )

        if action == "complete_task":
            task = await workflow.complete_task(
                entity_id,
                actual_hours=actual_hours,
                learnings=learnings or "",
            )
            return ManageResponse(
                success=True,
                action=action,
                entity_id=entity_id,
                message=f"Task completed: {task.title}",
                data={
                    "status": task.status.value,
                    "actual_hours": task.actual_hours,
                    "completed_at": task.completed_at.isoformat() if task.completed_at else None,
                    "learnings_captured": bool(learnings),
                },
            )

        if action == "archive":
            # Generic archive - update status to archived
            await entity_manager.update(entity_id, {"status": TaskStatus.ARCHIVED})
            return ManageResponse(
                success=True,
                action=action,
                entity_id=entity_id,
                message="Entity archived",
                data={"status": "archived"},
            )

        return ManageResponse(
            success=False,
            action=action,
            entity_id=entity_id,
            message=f"Unknown action: {action}",
        )

    except Exception as e:
        log.warning("manage_failed", error=str(e), action=action)
        return ManageResponse(
            success=False,
            action=action,
            entity_id=entity_id,
            message=f"Failed: {e}",
        )


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
    """Get knowledge graph statistics.

    Uses a single aggregation query for performance instead of N separate queries.
    """
    try:
        client = await get_graph_client()

        # Single aggregation query - much faster than N separate list queries
        result = await client.client.driver.execute_query(
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
        if result and len(result) > 0:
            data = result[0] if isinstance(result, tuple) else result
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
