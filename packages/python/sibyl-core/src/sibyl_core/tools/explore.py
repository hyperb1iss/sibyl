"""Explore tool for navigating the Sibyl knowledge graph."""

from typing import TYPE_CHECKING, Any, Literal

import structlog

from sibyl_core.models.entities import Entity, EntityType, RelationshipType
from sibyl_core.services.legacy_graph import get_legacy_graph_runtime
from sibyl_core.tools.helpers import (
    VALID_ENTITY_TYPES,
    _build_entity_metadata,
    _get_field,
    _serialize_enum,
)
from sibyl_core.tools.responses import (
    DependencyNode,
    EntitySummary,
    ExploreResponse,
    RelatedEntity,
)

if TYPE_CHECKING:
    pass

log = structlog.get_logger()

# Re-export for backwards compatibility
__all__ = ["DependencyNode", "explore"]


def _normalize_project_ids(
    project: str | None,
    project_ids: list[str] | None,
) -> list[str] | None:
    """Normalize single- and multi-project filters into a deduplicated list."""
    raw_ids = project_ids or ([project] if project else [])
    if not raw_ids:
        return None

    normalized: list[str] = []
    for project_id in raw_ids:
        if project_id and project_id not in normalized:
            normalized.append(project_id)
    return normalized or None


async def explore(
    mode: Literal["list", "related", "traverse", "dependencies"] = "list",
    types: list[str] | None = None,
    entity_id: str | None = None,
    relationship_types: list[str] | None = None,
    depth: int = 1,
    language: str | None = None,
    category: str | None = None,
    project: str | None = None,
    project_ids: list[str] | None = None,
    accessible_projects: set[str] | None = None,
    epic: str | None = None,
    no_epic: bool = False,
    status: str | None = None,
    priority: str | None = None,
    complexity: str | None = None,
    feature: str | None = None,
    tags: str | None = None,
    include_archived: bool = False,
    limit: int = 50,
    offset: int = 0,
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
    2. Then: explore(mode="list", types=["epic"], project="<project_id>") - List epics in project
    3. Then: explore(mode="list", types=["task"], epic="<epic_id>") - List tasks in epic
    Prefer listing tasks with a project or epic filter to keep results focused.

    USE CASES:
    • List projects first: explore(mode="list", types=["project"])
    • List epics in project: explore(mode="list", types=["epic"], project="proj_abc")
    • List tasks in epic: explore(mode="list", types=["task"], epic="epic_abc")
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
        project: Optional single-project filter (recommended for task/epic listing).
        project_ids: Optional multi-project filter for task/epic listing.
        epic: Optional epic filter (for listing tasks within an epic).
        no_epic: Filter for tasks without an epic (mutually exclusive with epic).
        status: Filter tasks by workflow status (backlog, todo, doing, blocked, review, done).
        priority: Filter tasks by priority (critical, high, medium, low, someday).
        complexity: Filter tasks by complexity (trivial, simple, medium, complex, epic).
        feature: Filter tasks by feature area.
        tags: Filter tasks by tags (comma-separated, matches if task has ANY).
        limit: Maximum results (1-200, default 50).
        offset: Offset for pagination (default 0).

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
    offset = max(0, offset)
    depth = max(1, min(depth, 3))

    log.info(
        "explore",
        mode=mode,
        types=types,
        entity_id=entity_id,
        project=project,
        project_ids=project_ids,
        status=status,
        depth=depth,
    )

    requested_project_ids = _normalize_project_ids(project, project_ids)

    filters = {}
    if types:
        filters["types"] = types
    if language:
        filters["language"] = language
    if category:
        filters["category"] = category
    if entity_id:
        filters["entity_id"] = entity_id
    if requested_project_ids:
        if len(requested_project_ids) == 1:
            filters["project"] = requested_project_ids[0]
        else:
            filters["project_ids"] = requested_project_ids
    if epic:
        filters["epic"] = epic
    if no_epic:
        filters["no_epic"] = True
    if status:
        filters["status"] = status
    if priority:
        filters["priority"] = priority
    if complexity:
        filters["complexity"] = complexity
    if feature:
        filters["feature"] = feature
    if tags:
        filters["tags"] = tags

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
                accessible_projects=accessible_projects,
            )
        return await _explore_list(
            types=types,
            language=language,
            category=category,
            project=project,
            project_ids=requested_project_ids,
            accessible_projects=accessible_projects,
            epic=epic,
            no_epic=no_epic,
            status=status,
            priority=priority,
            complexity=complexity,
            feature=feature,
            tags=tags,
            include_archived=include_archived,
            limit=limit,
            offset=offset,
            filters=filters,
            group_id=organization_id,
        )

    except Exception as e:
        log.warning("explore_failed", error=str(e), mode=mode)
        return ExploreResponse(mode=mode, entities=[], total=0, filters=filters)


def _passes_entity_filters(
    entity: Entity,
    language: str | None,
    category: str | None,
    project: str | None,
    accessible_projects: set[str] | None,
    epic: str | None,
    status: str | None,
    priority: str | None,
    complexity: str | None,
    feature: str | None,
    tags: str | None,
    include_archived: bool,
    project_ids: set[str] | None = None,
) -> bool:
    """Check if an entity passes all specified filters."""
    # RBAC: Filter by accessible projects
    # Include entities that: have no project_id OR project_id is in accessible set
    if accessible_projects is not None:
        entity_project = _get_field(entity, "project_id")
        if entity_project is not None and entity_project not in accessible_projects:
            return False

    # Language filter
    if language:
        entity_langs = _get_field(entity, "languages", [])
        if language.lower() not in [lang.lower() for lang in entity_langs]:
            return False

    # Category filter
    if category:
        entity_cat = _get_field(entity, "category", "")
        if category.lower() not in entity_cat.lower():
            return False

    # Project filter (for tasks and epics)
    if project and _get_field(entity, "project_id") != project:
        return False
    if project_ids and _get_field(entity, "project_id") not in project_ids:
        return False

    # Epic filter (for tasks)
    if epic and _get_field(entity, "epic_id") != epic:
        return False

    # Status filter (for tasks) - supports comma-separated values
    if status:
        entity_status = _get_field(entity, "status")
        if entity_status is None:
            return False
        status_val = str(_serialize_enum(entity_status)).lower()
        status_list = [s.strip().lower() for s in status.split(",")]
        if status_val not in status_list:
            return False

    # Priority filter (for tasks) - supports comma-separated values
    if priority:
        entity_priority = _get_field(entity, "priority")
        if entity_priority is None:
            return False
        priority_val = str(_serialize_enum(entity_priority)).lower()
        priority_list = [p.strip().lower() for p in priority.split(",")]
        if priority_val not in priority_list:
            return False

    # Complexity filter (for tasks) - supports comma-separated values
    if complexity:
        entity_complexity = _get_field(entity, "complexity")
        if entity_complexity is None:
            return False
        complexity_val = str(_serialize_enum(entity_complexity)).lower()
        complexity_list = [c.strip().lower() for c in complexity.split(",")]
        if complexity_val not in complexity_list:
            return False

    # Feature filter (for tasks)
    if feature:
        entity_feature = _get_field(entity, "feature")
        if entity_feature is None or entity_feature.lower() != feature.lower():
            return False

    # Tags filter (for tasks) - match if ANY tag matches
    if tags:
        tag_list = [t.strip().lower() for t in tags.split(",")]
        entity_tags = _get_field(entity, "tags", [])
        entity_tags_lower = [str(t).lower() for t in entity_tags]
        if not any(t in entity_tags_lower for t in tag_list):
            return False

    # Archive filter (for projects) - hide archived unless explicitly included
    if not include_archived:
        entity_type = _get_field(entity, "entity_type")
        if entity_type and str(entity_type).lower() == "project":
            entity_status = _get_field(entity, "status")
            if entity_status:
                status_val = _serialize_enum(entity_status)
                if str(status_val).lower() == "archived":
                    return False

    return True


async def _explore_list(
    types: list[str] | None,
    language: str | None,
    category: str | None,
    project: str | None,
    project_ids: list[str] | None,
    accessible_projects: set[str] | None,
    epic: str | None,
    no_epic: bool,
    status: str | None,
    priority: str | None,
    complexity: str | None,
    feature: str | None,
    tags: str | None,
    include_archived: bool,
    limit: int,
    offset: int,
    filters: dict[str, Any],
    group_id: str,
) -> ExploreResponse:
    """List entities by type with filters."""
    runtime = await get_legacy_graph_runtime(group_id)
    entity_manager = runtime.entity_manager

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

    # Parse tags into list if provided
    tag_list = [t.strip() for t in tags.split(",")] if tags else None
    requested_project_ids = _normalize_project_ids(project, project_ids)
    project_id_filter = (
        requested_project_ids[0]
        if requested_project_ids and len(requested_project_ids) == 1
        else None
    )
    requested_project_id_set = set(requested_project_ids) if requested_project_ids else None

    # Fetch with DB-level filtering for efficiency
    # Over-fetch to detect has_more after any remaining client-side filters
    fetch_limit = limit + offset + 50
    all_entities = []
    for entity_type in target_types:
        entities = await entity_manager.list_by_type(
            entity_type,
            limit=fetch_limit,
            project_id=project_id_filter,
            epic_id=epic,
            no_epic=no_epic,
            status=status,
            priority=priority,
            complexity=complexity,
            feature=feature,
            tags=tag_list,
            include_archived=include_archived,
        )
        all_entities.extend(entities)

    # Apply remaining filters not handled by DB (language, category, accessible_projects)
    filtered_entities = [
        entity
        for entity in all_entities
        if _passes_entity_filters(
            entity=entity,
            language=language,
            category=category,
            project=None,  # project already filtered by DB when only one is requested
            project_ids=requested_project_id_set,
            accessible_projects=accessible_projects,
            epic=None,
            status=None,
            priority=None,
            complexity=None,
            feature=None,
            tags=None,
            include_archived=include_archived,
        )
    ]

    # Apply pagination
    actual_total = len(filtered_entities)
    paginated_entities = filtered_entities[offset : offset + limit]
    has_more = offset + len(paginated_entities) < actual_total

    # Build result summaries
    results = [
        EntitySummary(
            id=entity.id,
            type=entity.entity_type.value,
            name=entity.name,
            description=entity.description[:200] if entity.description else "",
            metadata=_build_entity_metadata(entity),
        )
        for entity in paginated_entities
    ]

    return ExploreResponse(
        mode="list",
        entities=results,
        total=len(results),
        filters=filters,
        limit=limit,
        offset=offset,
        has_more=has_more,
        actual_total=actual_total,
    )


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

    runtime = await get_legacy_graph_runtime(group_id)
    relationship_manager = runtime.relationship_manager
    entity_manager = runtime.entity_manager

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
    accessible_projects: set[str] | None = None,
) -> ExploreResponse:
    """Find related entities via graph traversal."""
    if not entity_id:
        return ExploreResponse(
            mode=mode,
            entities=[],
            total=0,
            filters={**filters, "error": "entity_id required for related/traverse mode"},
        )

    runtime = await get_legacy_graph_runtime(group_id)
    relationship_manager = runtime.relationship_manager

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
        # RBAC: Filter by accessible projects
        if accessible_projects is not None:
            entity_project = _get_field(entity, "project_id")
            if entity_project is not None and entity_project not in accessible_projects:
                continue

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
