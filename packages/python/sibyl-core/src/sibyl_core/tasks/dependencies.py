"""Task dependency detection and cycle checking."""

from dataclasses import dataclass, field

import structlog

from sibyl_core.graph.client import GraphClient
from sibyl_core.models.tasks import TaskStatus

log = structlog.get_logger()


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


async def get_task_dependencies(
    client: "GraphClient",
    task_id: str,
    organization_id: str,
    depth: int = 1,
    include_transitive: bool = False,
) -> DependencyResult:
    """Get tasks that a given task depends on.

    Traverses DEPENDS_ON relationships outward from the task.

    Args:
        client: Graph client for queries.
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
        # Query for DEPENDS_ON relationships
        query = f"""
        MATCH (task {{uuid: $task_id}})-[:RELATIONSHIP*1..{actual_depth}]->(dep)
        WHERE ALL(r IN relationships(path) WHERE r.relationship_type = 'DEPENDS_ON')
        RETURN DISTINCT dep.uuid as dep_id, dep.status as dep_status
        """

        # Simpler query for direct dependencies only
        if actual_depth == 1:
            query = """
            MATCH (task {uuid: $task_id})-[r:RELATIONSHIP {relationship_type: 'DEPENDS_ON'}]->(dep)
            RETURN dep.uuid as dep_id, dep.status as dep_status
            """

        rows = await client.execute_read_org(query, organization_id, task_id=task_id)

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
    client: "GraphClient",
    task_id: str,
    organization_id: str,
    depth: int = 1,
) -> DependencyResult:
    """Get tasks that are blocked by a given task.

    Traverses BLOCKS relationships outward (or inverse DEPENDS_ON).

    Args:
        client: Graph client for queries.
        task_id: The task to find dependents for.
        depth: Maximum traversal depth (1-5, default 1).

    Returns:
        DependencyResult with tasks that depend on this task.
    """
    depth = max(1, min(depth, 5))

    log.info("get_blocking_tasks", task_id=task_id, depth=depth)

    try:
        # Query for tasks that DEPEND_ON this task (inverse relationship)
        query = """
        MATCH (dependent)-[r:RELATIONSHIP {relationship_type: 'DEPENDS_ON'}]->(task {uuid: $task_id})
        RETURN dependent.uuid as dep_id, dependent.status as dep_status
        """

        rows = await client.execute_read_org(query, organization_id, task_id=task_id)

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
    client: "GraphClient",
    organization_id: str,
    project_id: str | None = None,
    max_depth: int = 10,
) -> CycleResult:
    """Detect circular dependencies in the task graph.

    Uses DFS-based cycle detection on DEPENDS_ON relationships.

    Args:
        client: Graph client for queries.
        project_id: Optional project to scope the search.
        max_depth: Maximum cycle length to detect (default 10).

    Returns:
        CycleResult with detected cycles.
    """
    log.info("detect_dependency_cycles", project_id=project_id, max_depth=max_depth)

    try:
        # Query for all DEPENDS_ON edges, optionally scoped to project
        if project_id:
            query = """
            MATCH (task)-[belongs:RELATIONSHIP {relationship_type: 'BELONGS_TO'}]->(project {uuid: $project_id})
            WITH task
            MATCH (task)-[r:RELATIONSHIP {relationship_type: 'DEPENDS_ON'}]->(dep)
            RETURN task.uuid as from_id, dep.uuid as to_id
            """
            rows = await client.execute_read_org(query, organization_id, project_id=project_id)
        else:
            query = """
            MATCH (task)-[r:RELATIONSHIP {relationship_type: 'DEPENDS_ON'}]->(dep)
            RETURN task.uuid as from_id, dep.uuid as to_id
            """
            rows = await client.execute_read_org(query, organization_id)

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
    client: "GraphClient",
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
        # Get all tasks and their dependencies
        if project_id:
            task_query = """
            MATCH (task)-[r:RELATIONSHIP {relationship_type: 'BELONGS_TO'}]->(project {uuid: $project_id})
            RETURN task.uuid as task_id, task.status as status, task.task_order as priority
            """
            task_rows = await client.execute_read_org(
                task_query, organization_id, project_id=project_id
            )
        else:
            task_query = """
            MATCH (task)
            WHERE task.entity_type = 'task'
            RETURN task.uuid as task_id, task.status as status, task.task_order as priority
            """
            task_rows = await client.execute_read_org(task_query, organization_id)

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

            if task_id:
                tasks[task_id] = priority or 0

        # Get dependency edges
        if project_id:
            dep_query = """
            MATCH (task)-[:RELATIONSHIP {relationship_type: 'BELONGS_TO'}]->(project {uuid: $project_id})
            WITH task
            MATCH (task)-[r:RELATIONSHIP {relationship_type: 'DEPENDS_ON'}]->(dep)
            RETURN task.uuid as from_id, dep.uuid as to_id
            """
            dep_rows = await client.execute_read_org(
                dep_query, organization_id, project_id=project_id
            )
        else:
            dep_query = """
            MATCH (task)-[r:RELATIONSHIP {relationship_type: 'DEPENDS_ON'}]->(dep)
            WHERE task.entity_type = 'task'
            RETURN task.uuid as from_id, dep.uuid as to_id
            """
            dep_rows = await client.execute_read_org(dep_query, organization_id)

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
