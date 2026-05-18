"""Task dependency detection and cycle checking."""

from dataclasses import dataclass, field
from typing import Any

import structlog

from sibyl_core.models.entities import EntityType, RelationshipType
from sibyl_core.models.tasks import TaskStatus

log = structlog.get_logger()

_PAGINATION_BATCH_SIZE = 500


@dataclass
class DependencyResult:
    """Result of dependency traversal."""

    task_id: str
    dependencies: list[str]  # Task IDs this task depends on
    blockers: list[str]  # Task IDs blocking this task (incomplete dependencies)
    depth: int = 1  # Traversal depth


@dataclass
class CycleResult:
    """Result of cycle detection."""

    has_cycles: bool
    cycles: list[list[str]] = field(default_factory=list)  # List of cycle paths
    message: str = ""


@dataclass
class TaskOrderResult:
    """Result of topological sort."""

    ordered_tasks: list[str]  # Task IDs in execution order
    unordered_tasks: list[str] = field(default_factory=list)  # Tasks in cycles
    warnings: list[str] = field(default_factory=list)


def _get_graph_managers(
    client: Any,
    organization_id: str,
) -> tuple[Any, Any]:
    from sibyl_core.services.native_graph import (
        NativeEntityManager,
        NativeRelationshipManager,
        NativeSurrealGraphClient,
    )

    if isinstance(client, NativeSurrealGraphClient):
        return (
            NativeEntityManager(client, group_id=organization_id),
            NativeRelationshipManager(client, group_id=organization_id),
        )

    raise RuntimeError("Task dependency traversal requires a native graph client")


def _task_status_value(task: Any) -> str | None:
    status = getattr(task, "status", None)
    if isinstance(status, TaskStatus):
        return status.value
    if isinstance(status, str):
        return status

    metadata = getattr(task, "metadata", None) or {}
    if isinstance(metadata, dict):
        raw_status = metadata.get("status")
        if isinstance(raw_status, str):
            return raw_status
    return None


def _task_order_value(task: Any) -> int:
    task_order = getattr(task, "task_order", None)
    if isinstance(task_order, int):
        return task_order

    metadata = getattr(task, "metadata", None) or {}
    raw_task_order = metadata.get("task_order") if isinstance(metadata, dict) else None
    if isinstance(raw_task_order, int):
        return raw_task_order
    return 0


async def _list_task_entities(
    entity_manager: Any,
    *,
    project_id: str | None = None,
) -> list[Any]:
    tasks: list[Any] = []
    offset = 0

    while True:
        batch = await entity_manager.list_by_type(
            EntityType.TASK,
            project_id=project_id,
            limit=_PAGINATION_BATCH_SIZE,
            offset=offset,
            include_archived=True,
        )
        if not batch:
            break

        tasks.extend(batch)
        offset += len(batch)
        if len(batch) < _PAGINATION_BATCH_SIZE:
            break

    return tasks


async def _list_dependency_relationships(
    relationship_manager: Any,
    *,
    task_ids: set[str] | None = None,
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    offset = 0

    while True:
        batch = await relationship_manager.list_all(
            relationship_types=[RelationshipType.DEPENDS_ON],
            limit=_PAGINATION_BATCH_SIZE,
            offset=offset,
        )
        if not batch:
            break

        for relationship in batch:
            source_id = relationship.source_id
            target_id = relationship.target_id
            if not source_id or not target_id:
                continue
            if task_ids is not None and (source_id not in task_ids or target_id not in task_ids):
                continue
            rows.append({"from_id": source_id, "to_id": target_id})

        offset += len(batch)
        if len(batch) < _PAGINATION_BATCH_SIZE:
            break

    return rows


async def get_task_dependencies(
    client: Any,
    task_id: str,
    organization_id: str,
    depth: int = 1,
    include_transitive: bool = False,
) -> DependencyResult:
    """Get tasks that a given task depends on.

    Traverses DEPENDS_ON relationships outward from the task.

    Args:
        client: Native graph client for queries.
        task_id: The task to find dependencies for.
        depth: Maximum traversal depth (1-5, default 1).
        include_transitive: Include transitive dependencies.

    Returns:
        DependencyResult with direct and optionally transitive dependencies.
    """
    depth = max(1, min(depth, 5))
    actual_depth = depth if include_transitive else 1

    log.info("get_task_dependencies", task_id=task_id, depth=actual_depth)

    try:
        entity_manager, relationship_manager = _get_graph_managers(client, organization_id)
        seen: set[str] = set()
        frontier = [task_id]
        rows: list[dict[str, str | None]] = []

        for _ in range(actual_depth):
            if not frontier:
                break
            next_frontier: list[str] = []
            for current_task_id in frontier:
                relationships = await relationship_manager.get_for_entity(
                    current_task_id,
                    relationship_types=[RelationshipType.DEPENDS_ON],
                    direction="outgoing",
                )
                for relationship in relationships:
                    dep_id = relationship.target_id
                    if not dep_id or dep_id in seen:
                        continue
                    seen.add(dep_id)
                    next_frontier.append(dep_id)
                    try:
                        dependency = await entity_manager.get(dep_id)
                    except Exception:
                        dependency = None
                    rows.append({"dep_id": dep_id, "dep_status": _task_status_value(dependency)})
            frontier = next_frontier

        dependencies: list[str] = []
        blockers: list[str] = []

        for record in rows:
            # Handle list-based FalkorDB results
            if isinstance(record, (list, tuple)):
                dep_id = record[0] if len(record) > 0 else None
                dep_status = record[1] if len(record) > 1 else None
            else:
                dep_id = record.get("dep_id")
                dep_status = record.get("dep_status")

            if dep_id:
                dependencies.append(dep_id)
                # Check if dependency is incomplete (blocking)
                if dep_status and dep_status not in (
                    TaskStatus.DONE.value,
                    TaskStatus.ARCHIVED.value,
                ):
                    blockers.append(dep_id)

        log.info(
            "dependencies_found",
            task_id=task_id,
            count=len(dependencies),
            blockers=len(blockers),
        )

        return DependencyResult(
            task_id=task_id,
            dependencies=dependencies,
            blockers=blockers,
            depth=actual_depth,
        )

    except Exception as e:
        log.warning("get_dependencies_failed", task_id=task_id, error=str(e))
        return DependencyResult(task_id=task_id, dependencies=[], blockers=[], depth=actual_depth)


async def get_blocking_tasks(
    client: Any,
    task_id: str,
    organization_id: str,
    depth: int = 1,
) -> DependencyResult:
    """Get tasks that are blocked by a given task.

    Traverses BLOCKS relationships outward (or inverse DEPENDS_ON).

    Args:
        client: Native graph client for queries.
        task_id: The task to find dependents for.
        depth: Maximum traversal depth (1-5, default 1).

    Returns:
        DependencyResult with tasks that depend on this task.
    """
    depth = max(1, min(depth, 5))

    log.info("get_blocking_tasks", task_id=task_id, depth=depth)

    try:
        entity_manager, relationship_manager = _get_graph_managers(client, organization_id)
        relationships = await relationship_manager.get_for_entity(
            task_id,
            relationship_types=[RelationshipType.DEPENDS_ON],
            direction="incoming",
        )
        rows = []
        for relationship in relationships:
            dep_id = relationship.source_id
            if not dep_id:
                continue
            try:
                dependency = await entity_manager.get(dep_id)
            except Exception:
                dependency = None
            rows.append({"dep_id": dep_id, "dep_status": _task_status_value(dependency)})

        blocked_tasks: list[str] = []
        incomplete: list[str] = []

        for record in rows:
            # Handle list-based FalkorDB results
            if isinstance(record, (list, tuple)):
                dep_id = record[0] if len(record) > 0 else None
                dep_status = record[1] if len(record) > 1 else None
            else:
                dep_id = record.get("dep_id")
                dep_status = record.get("dep_status")

            if dep_id:
                blocked_tasks.append(dep_id)
                if dep_status and dep_status not in (
                    TaskStatus.DONE.value,
                    TaskStatus.ARCHIVED.value,
                ):
                    incomplete.append(dep_id)

        log.info(
            "blocking_tasks_found",
            task_id=task_id,
            count=len(blocked_tasks),
        )

        return DependencyResult(
            task_id=task_id,
            dependencies=blocked_tasks,  # Tasks that depend on this one
            blockers=incomplete,  # Incomplete dependents
            depth=depth,
        )

    except Exception as e:
        log.warning("get_blocking_failed", task_id=task_id, error=str(e))
        return DependencyResult(task_id=task_id, dependencies=[], blockers=[], depth=depth)


async def detect_dependency_cycles(
    client: Any | None,
    organization_id: str,
    project_id: str | None = None,
    max_depth: int = 10,
    *,
    entity_manager: Any | None = None,
    relationship_manager: Any | None = None,
) -> CycleResult:
    """Detect circular dependencies in the task graph.

    Uses DFS-based cycle detection on DEPENDS_ON relationships.

    Args:
        client: Native graph client for queries.
        project_id: Optional project to scope the search.
        max_depth: Maximum cycle length to detect (default 10).

    Returns:
        CycleResult with detected cycles.
    """
    log.info("detect_dependency_cycles", project_id=project_id, max_depth=max_depth)

    try:
        if entity_manager is None or relationship_manager is None:
            if client is None:
                raise ValueError("client required when graph managers are not provided")
            entity_manager, relationship_manager = _get_graph_managers(client, organization_id)
        tasks = await _list_task_entities(entity_manager, project_id=project_id)
        task_ids = {task.id for task in tasks if getattr(task, "id", None)}
        rows = await _list_dependency_relationships(
            relationship_manager,
            task_ids=task_ids,
        )

        # Build adjacency list
        graph: dict[str, list[str]] = {}
        for record in rows:
            if isinstance(record, (list, tuple)):
                from_id = record[0] if len(record) > 0 else None
                to_id = record[1] if len(record) > 1 else None
            else:
                from_id = record.get("from_id")
                to_id = record.get("to_id")

            if from_id and to_id:
                if from_id not in graph:
                    graph[from_id] = []
                graph[from_id].append(to_id)

        # DFS-based cycle detection
        cycles: list[list[str]] = []
        visited: set[str] = set()
        rec_stack: set[str] = set()
        path: list[str] = []

        def dfs(node: str) -> None:
            visited.add(node)
            rec_stack.add(node)
            path.append(node)

            for neighbor in graph.get(node, []):
                if neighbor not in visited:
                    dfs(neighbor)
                elif neighbor in rec_stack:
                    # Found a cycle - extract it from path
                    cycle_start = path.index(neighbor)
                    cycle = [*path[cycle_start:], neighbor]
                    cycles.append(cycle)

            path.pop()
            rec_stack.remove(node)

        # Run DFS from all nodes
        for node in graph:
            if node not in visited:
                dfs(node)

        has_cycles = len(cycles) > 0
        message = f"Found {len(cycles)} cycle(s)" if has_cycles else "No cycles detected"

        log.info(
            "cycle_detection_complete",
            project_id=project_id,
            has_cycles=has_cycles,
            cycle_count=len(cycles),
        )

        return CycleResult(
            has_cycles=has_cycles,
            cycles=cycles,
            message=message,
        )

    except Exception as e:
        log.warning("cycle_detection_failed", project_id=project_id, error=str(e))
        return CycleResult(
            has_cycles=False,
            cycles=[],
            message=f"Cycle detection failed: {e}",
        )


async def suggest_task_order(
    client: Any,
    organization_id: str,
    project_id: str | None = None,
    status_filter: list[TaskStatus] | None = None,
) -> TaskOrderResult:
    """Suggest task execution order using topological sort.

    Returns tasks ordered so dependencies come before dependents.
    Tasks in cycles are reported separately.

    Args:
        client: Graph client for queries.
        project_id: Optional project to scope the search.
        status_filter: Only include tasks with these statuses.

    Returns:
        TaskOrderResult with ordered task IDs.
    """
    log.info("suggest_task_order", project_id=project_id, status_filter=status_filter)

    try:
        entity_manager, relationship_manager = _get_graph_managers(client, organization_id)
        task_entities = await _list_task_entities(entity_manager, project_id=project_id)
        task_rows = [
            {
                "task_id": task.id,
                "status": _task_status_value(task),
                "priority": _task_order_value(task),
            }
            for task in task_entities
            if getattr(task, "id", None)
        ]

        # Build task set with priorities
        tasks: dict[str, int] = {}  # task_id -> priority
        for record in task_rows:
            if isinstance(record, (list, tuple)):
                task_id = record[0] if len(record) > 0 else None
                status = record[1] if len(record) > 1 else None
                priority = record[2] if len(record) > 2 else 0
            else:
                task_id = record.get("task_id")
                status = record.get("status")
                priority = record.get("priority", 0)

            # Apply status filter
            if status_filter:
                status_values = [s.value for s in status_filter]
                if status not in status_values:
                    continue

            if isinstance(task_id, str):
                try:
                    tasks[task_id] = int(priority or 0)
                except (TypeError, ValueError):
                    tasks[task_id] = 0

        dep_rows = await _list_dependency_relationships(
            relationship_manager,
            task_ids=set(tasks),
        )

        # Build adjacency list and in-degree count
        graph: dict[str, list[str]] = {task_id: [] for task_id in tasks}
        in_degree: dict[str, int] = dict.fromkeys(tasks, 0)

        for record in dep_rows:
            if isinstance(record, (list, tuple)):
                from_id = record[0] if len(record) > 0 else None
                to_id = record[1] if len(record) > 1 else None
            else:
                from_id = record.get("from_id")
                to_id = record.get("to_id")

            if from_id and to_id and from_id in tasks and to_id in tasks:
                graph[to_id].append(from_id)  # to_id must complete before from_id
                in_degree[from_id] += 1

        # Kahn's algorithm for topological sort
        # Use priority as secondary sort key
        queue: list[tuple[int, str]] = []
        for task_id, degree in in_degree.items():
            if degree == 0:
                queue.append((-tasks[task_id], task_id))  # Negative for max-heap behavior

        queue.sort()  # Sort by priority (highest first)
        ordered: list[str] = []
        warnings: list[str] = []

        while queue:
            _, task_id = queue.pop(0)
            ordered.append(task_id)

            for dependent in graph.get(task_id, []):
                in_degree[dependent] -= 1
                if in_degree[dependent] == 0:
                    queue.append((-tasks[dependent], dependent))
                    queue.sort()

        # Tasks not in ordered list are in cycles
        unordered = [task_id for task_id in tasks if task_id not in ordered]
        if unordered:
            warnings.append(
                f"{len(unordered)} task(s) could not be ordered due to circular dependencies"
            )

        log.info(
            "task_order_complete",
            project_id=project_id,
            ordered_count=len(ordered),
            unordered_count=len(unordered),
        )

        return TaskOrderResult(
            ordered_tasks=ordered,
            unordered_tasks=unordered,
            warnings=warnings,
        )

    except Exception as e:
        log.warning("suggest_order_failed", project_id=project_id, error=str(e))
        return TaskOrderResult(
            ordered_tasks=[],
            unordered_tasks=[],
            warnings=[f"Task ordering failed: {e}"],
        )
