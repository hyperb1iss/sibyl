"""Metrics endpoints for project and org-level analytics."""

import json
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException

from sibyl.api.schemas import (
    AssigneeStats,
    OrgMetricsResponse,
    ProjectMetrics,
    ProjectMetricsResponse,
    ProjectSummariesResponse,
    ProjectSummary,
    TaskPriorityDistribution,
    TaskStatusDistribution,
    TimeSeriesPoint,
)
from sibyl.auth.context import AuthContext
from sibyl.auth.dependencies import get_auth_context, get_current_organization, require_org_role
from sibyl.persistence.auth_runtime import list_accessible_project_graph_ids
from sibyl_core.auth import AuthOrganization, OrganizationRole
from sibyl_core.models.entities import EntityType
from sibyl_core.services import KnowledgeReadService

log = structlog.get_logger()


async def get_entity_graph_runtime(group_id: str):
    from sibyl.persistence.graph_runtime import get_entity_graph_runtime as service

    return await service(group_id)


async def get_knowledge_read_adapter(group_id: str):
    from sibyl.persistence.graph_runtime import get_knowledge_read_adapter as service

    return await service(group_id)


async def execute_surreal_graph_query(
    group_id: str,
    query: str,
    **params: object,
) -> list[dict[str, object]] | None:
    from sibyl.persistence.graph_runtime import execute_surreal_graph_query

    return await execute_surreal_graph_query(group_id, query, **params)


router = APIRouter(
    prefix="/metrics",
    tags=["metrics"],
    dependencies=[
        Depends(
            require_org_role(
                OrganizationRole.OWNER, OrganizationRole.ADMIN, OrganizationRole.MEMBER
            )
        )
    ],
)


def _parse_iso_date(date_str: str | datetime | None) -> datetime | None:
    """Parse ISO date strings or datetime objects to UTC datetimes."""
    if not date_str:
        return None
    if isinstance(date_str, datetime):
        if date_str.tzinfo is None:
            return date_str.replace(tzinfo=UTC)
        return date_str.astimezone(UTC)
    try:
        parsed = datetime.fromisoformat(date_str)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    except (ValueError, TypeError):
        return None


def _is_open_status(status: str) -> bool:
    """Return whether a task status should count toward open-work metrics."""
    return status not in {"done", "archived"}


def _compute_status_distribution(tasks: list[dict]) -> TaskStatusDistribution:
    """Compute task counts by status."""
    dist = TaskStatusDistribution()
    for task in tasks:
        status = task.get("metadata", {}).get("status", "backlog")
        if hasattr(dist, status):
            setattr(dist, status, getattr(dist, status) + 1)
    return dist


def _compute_priority_distribution(tasks: list[dict]) -> TaskPriorityDistribution:
    """Compute task counts by priority."""
    dist = TaskPriorityDistribution()
    for task in tasks:
        priority = task.get("metadata", {}).get("priority", "medium")
        if hasattr(dist, priority):
            setattr(dist, priority, getattr(dist, priority) + 1)
    return dist


def _compute_assignee_stats(tasks: list[dict]) -> list[AssigneeStats]:
    """Compute stats per assignee."""
    stats: dict[str, dict] = defaultdict(lambda: {"total": 0, "completed": 0, "in_progress": 0})

    for task in tasks:
        assignees = task.get("metadata", {}).get("assignees", [])
        status = task.get("metadata", {}).get("status", "")

        # Handle both list and single assignee
        if isinstance(assignees, str):
            assignees = [assignees] if assignees else []

        for assignee in assignees:
            if not assignee:
                continue
            stats[assignee]["total"] += 1
            if status == "done":
                stats[assignee]["completed"] += 1
            elif status == "doing":
                stats[assignee]["in_progress"] += 1

    return [
        AssigneeStats(name=name, **data)
        for name, data in sorted(stats.items(), key=lambda x: x[1]["total"], reverse=True)
    ]


def _compute_velocity_trend(tasks: list[dict], days: int = 14) -> list[TimeSeriesPoint]:
    """Compute daily completion counts for the last N days."""
    now = datetime.now(UTC)
    daily_counts: dict[str, int] = defaultdict(int)

    # Initialize all days with 0
    for i in range(days):
        date = (now - timedelta(days=i)).strftime("%Y-%m-%d")
        daily_counts[date] = 0

    # Count completions by day
    for task in tasks:
        status = task.get("metadata", {}).get("status", "")
        if status != "done":
            continue

        # Try completed_at, then updated_at
        completed_at = task.get("metadata", {}).get("completed_at")
        if not completed_at:
            completed_at = task.get("updated_at")

        completed_date = _parse_iso_date(completed_at)
        if completed_date and completed_date >= now - timedelta(days=days):
            date_str = completed_date.strftime("%Y-%m-%d")
            if date_str in daily_counts:
                daily_counts[date_str] += 1

    # Return sorted by date ascending
    return [TimeSeriesPoint(date=date, value=count) for date, count in sorted(daily_counts.items())]


def _count_recent_tasks(tasks: list[dict], days: int, field: str = "created_at") -> int:
    """Count tasks created/completed in the last N days."""
    now = datetime.now(UTC)
    cutoff = now - timedelta(days=days)
    count = 0

    for task in tasks:
        date_str = task.get(field) or task.get("metadata", {}).get(field)
        date = _parse_iso_date(date_str)
        if date and date >= cutoff:
            count += 1

    return count


async def _list_entities_by_type_paginated(
    entities: Any,
    entity_type: EntityType,
    *,
    batch_size: int = 1000,
    **filters: Any,
) -> list[Any]:
    """List all matching entities by paging through list_by_type."""
    items: list[Any] = []
    offset = 0

    while True:
        batch = await entities.list_by_type(
            entity_type,
            limit=batch_size,
            offset=offset,
            **filters,
        )
        if not batch:
            break

        items.extend(batch)
        if len(batch) < batch_size:
            break

        offset += batch_size

    return items


async def _list_entities_by_type_paginated_via_service(
    service: KnowledgeReadService,
    entity_type: EntityType,
    *,
    batch_size: int = 1000,
) -> list[Any]:
    """List all entities of a type by following service cursors."""
    entities: list[Any] = []
    cursor: str | None = None

    while True:
        page = await service.list_entities(
            entity_type,
            limit=batch_size,
            cursor=cursor,
        )
        if not page.items:
            break

        entities.extend(page.items)
        if page.next_cursor is None:
            break
        cursor = page.next_cursor

    return entities


def _build_project_summaries(
    projects: list[Any], counts_by_project: dict[str, dict[str, int]]
) -> list[ProjectSummary]:
    """Build project summaries from per-project task counts."""
    projects_summary: list[ProjectSummary] = []
    for project in projects:
        counts = counts_by_project.get(str(project.id), _empty_project_task_counts())
        rate = (counts["completed"] / counts["total"] * 100) if counts["total"] > 0 else 0.0
        projects_summary.append(
            ProjectSummary(
                id=project.id,
                name=project.name,
                total=counts["total"],
                completed=counts["completed"],
                doing=counts["doing"],
                blocked=counts["blocked"],
                review=counts["review"],
                todo=counts["todo"],
                backlog=counts["backlog"],
                critical=counts["critical"],
                high=counts["high"],
                overdue=counts["overdue"],
                completion_rate=round(rate, 1),
            )
        )

    projects_summary.sort(key=lambda summary: summary.total, reverse=True)
    return projects_summary


def _empty_project_task_counts() -> dict[str, int]:
    """Return a zeroed task rollup for a project."""
    return {
        "total": 0,
        "completed": 0,
        "doing": 0,
        "blocked": 0,
        "review": 0,
        "todo": 0,
        "backlog": 0,
        "critical": 0,
        "high": 0,
        "overdue": 0,
    }


def _compute_project_task_counts(
    tasks: list[dict[str, Any]],
    *,
    now: datetime,
) -> dict[str, dict[str, int]]:
    """Aggregate per-project task rollups from normalized task dictionaries."""
    project_task_counts: dict[str, dict[str, int]] = defaultdict(_empty_project_task_counts)

    for task in tasks:
        metadata = task.get("metadata", {})
        proj_id = metadata.get("project_id", "")
        if not proj_id:
            continue

        counts = project_task_counts[proj_id]
        counts["total"] += 1

        status = metadata.get("status", "backlog")
        if status == "done":
            counts["completed"] += 1
        elif status == "doing":
            counts["doing"] += 1
        elif status == "blocked":
            counts["blocked"] += 1
        elif status == "review":
            counts["review"] += 1
        elif status == "todo":
            counts["todo"] += 1
        elif status == "backlog":
            counts["backlog"] += 1

        if _is_open_status(status):
            priority = metadata.get("priority", "")
            if priority == "critical":
                counts["critical"] += 1
            elif priority == "high":
                counts["high"] += 1

            due_date = _parse_iso_date(metadata.get("due_date"))
            if due_date and due_date < now:
                counts["overdue"] += 1

    return project_task_counts


def _parse_metadata_dict(metadata: Any) -> dict[str, Any]:
    """Parse graph metadata payloads into dictionaries."""
    if isinstance(metadata, dict):
        return metadata
    if isinstance(metadata, str):
        try:
            parsed = json.loads(metadata)
        except json.JSONDecodeError:
            return {}
        if isinstance(parsed, dict):
            return parsed
    return {}


def _prefer_metadata_value(metadata: dict[str, Any], row: dict[str, Any], field: str) -> Any:
    """Prefer canonical metadata values and fall back to top-level properties."""
    metadata_value = metadata.get(field)
    if metadata_value not in (None, ""):
        return metadata_value

    row_value = row.get(field)
    if row_value not in (None, ""):
        return row_value

    return None


def _prefer_valid_datetime_value(
    metadata: dict[str, Any], row: dict[str, Any], field: str
) -> str | None:
    """Prefer the first parseable datetime value, falling back across representations."""
    metadata_value = metadata.get(field)
    if _parse_iso_date(metadata_value):
        return metadata_value

    row_value = row.get(field)
    if _parse_iso_date(row_value):
        return row_value

    return metadata_value or row_value


def _filter_projects_by_access(
    projects: list[Any], accessible_project_ids: set[str] | None
) -> list[Any]:
    """Filter projects to the caller's accessible set when provided."""
    if accessible_project_ids is None:
        return projects
    return [project for project in projects if str(project.id) in accessible_project_ids]


def _filter_tasks_by_access(
    tasks: list[dict[str, Any]], accessible_project_ids: set[str] | None
) -> list[dict[str, Any]]:
    """Filter tasks to accessible projects when project RBAC set is available."""
    if accessible_project_ids is None:
        return tasks

    filtered: list[dict[str, Any]] = []
    for task in tasks:
        project_id = str(task.get("metadata", {}).get("project_id") or "")
        if project_id and project_id in accessible_project_ids:
            filtered.append(task)
    return filtered


def _normalize_metric_task_row(row: dict[str, Any]) -> dict[str, Any]:
    """Normalize raw task rows into the legacy task-shaped metrics format."""
    metadata = _parse_metadata_dict(row.get("metadata"))

    assignees = _prefer_metadata_value(metadata, row, "assignees")
    if assignees is None:
        assignees = []

    created_at = _prefer_valid_datetime_value(metadata, row, "created_at")
    completed_at = _prefer_valid_datetime_value(metadata, row, "completed_at")
    due_date = _prefer_valid_datetime_value(metadata, row, "due_date")

    normalized_metadata = {
        **metadata,
        "project_id": _prefer_metadata_value(metadata, row, "project_id"),
        "status": _prefer_metadata_value(metadata, row, "status") or "backlog",
        "priority": _prefer_metadata_value(metadata, row, "priority") or "medium",
        "assignees": assignees,
        "created_at": created_at,
        "completed_at": completed_at,
        "due_date": due_date,
    }

    return {
        "created_at": created_at,
        "completed_at": completed_at,
        "updated_at": row.get("updated_at"),
        "metadata": normalized_metadata,
    }


async def _list_surreal_metric_task_rows(group_id: str) -> list[dict[str, Any]] | None:
    try:
        rows = await execute_surreal_graph_query(
            group_id,
            """
            SELECT
                uuid,
                project_id,
                status,
                priority,
                assignees,
                completed_at,
                due_date,
                attributes.metadata AS metadata,
                created_at,
                updated_at ?? attributes.updated_at AS updated_at
            FROM entity
            WHERE group_id = $group_id
                AND entity_type = $task_type
                AND string::lowercase(status ?? attributes.status ?? '') != 'archived';
            """,
            task_type=EntityType.TASK.value,
        )
    except Exception as exc:
        log.warning(
            "surreal_metric_task_fast_path_failed",
            group_id=group_id,
            error_type=type(exc).__name__,
        )
        return None

    if rows is None:
        return None

    tasks = [_normalize_metric_task_row(row) for row in rows]
    return [
        task for task in tasks if str(task["metadata"].get("status") or "").lower() != "archived"
    ]


async def _list_summary_metric_tasks(
    group_id: str,
    service: KnowledgeReadService,
) -> list[dict[str, Any]]:
    if (rows := await _list_surreal_metric_task_rows(group_id)) is not None:
        return rows

    return [
        task.model_dump()
        for task in await _list_entities_by_type_paginated_via_service(
            service,
            EntityType.TASK,
            batch_size=1000,
        )
    ]


@router.get("/projects/{project_id}", response_model=ProjectMetricsResponse)
async def get_project_metrics(
    project_id: str,
    org: AuthOrganization = Depends(get_current_organization),
) -> ProjectMetricsResponse:
    """Get metrics for a specific project."""
    try:
        group_id = str(org.id)
        service = await get_knowledge_read_adapter(group_id)
        entity_runtime = await get_entity_graph_runtime(group_id)

        # Get project
        project = await service.get_entity(project_id)
        if not project:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"Project not found: {project_id}. Run 'sibyl project relink' or use "
                    "--all-projects for an unscoped write."
                ),
            )

        project_tasks = await _list_entities_by_type_paginated(
            entity_runtime.entity_manager,
            EntityType.TASK,
            project_id=project_id,
        )
        tasks = [task.model_dump() for task in project_tasks]

        # Compute metrics
        status_dist = _compute_status_distribution(tasks)
        priority_dist = _compute_priority_distribution(tasks)
        assignees = _compute_assignee_stats(tasks)
        velocity = _compute_velocity_trend(tasks)

        total = len(tasks)
        completed = status_dist.done
        completion_rate = (completed / total * 100) if total > 0 else 0.0

        # Count recent activity
        tasks_created_7d = _count_recent_tasks(tasks, 7, "created_at")
        tasks_completed_7d = sum(1 for t in tasks if t.get("metadata", {}).get("status") == "done")
        # Re-count completed in last 7d using velocity
        tasks_completed_7d = (
            sum(p.value for p in velocity[-7:])
            if len(velocity) >= 7
            else sum(p.value for p in velocity)
        )

        metrics = ProjectMetrics(
            project_id=project_id,
            project_name=project.name,
            total_tasks=total,
            status_distribution=status_dist,
            priority_distribution=priority_dist,
            completion_rate=round(completion_rate, 1),
            assignees=assignees[:10],  # Top 10 assignees
            tasks_created_last_7d=tasks_created_7d,
            tasks_completed_last_7d=tasks_completed_7d,
            velocity_trend=velocity,
        )

        return ProjectMetricsResponse(metrics=metrics)

    except HTTPException:
        raise
    except Exception as e:
        log.exception("get_project_metrics_failed", project_id=project_id, error=str(e))
        raise HTTPException(
            status_code=500, detail="Failed to get project metrics. Please try again."
        ) from e


@router.get("/projects-summary", response_model=ProjectSummariesResponse)
async def get_project_summaries(
    org: AuthOrganization = Depends(get_current_organization),
    ctx: AuthContext | Any = Depends(get_auth_context),
) -> ProjectSummariesResponse:
    """Get the lean project-summary payload for the projects page."""
    try:
        group_id = str(org.id)
        service = await get_knowledge_read_adapter(group_id)
        projects = await _list_entities_by_type_paginated_via_service(
            service,
            EntityType.PROJECT,
            batch_size=500,
        )
        accessible_project_ids = (
            await list_accessible_project_graph_ids(ctx) if isinstance(ctx, AuthContext) else None
        )
        projects = _filter_projects_by_access(projects, accessible_project_ids)

        tasks = await _list_summary_metric_tasks(group_id, service)
        tasks = _filter_tasks_by_access(tasks, accessible_project_ids)
        counts_by_project = _compute_project_task_counts(
            tasks,
            now=datetime.now(UTC),
        )

        return ProjectSummariesResponse(
            projects_summary=_build_project_summaries(projects, counts_by_project)
        )

    except Exception as e:
        log.exception("get_project_summaries_failed", error=str(e))
        raise HTTPException(
            status_code=500, detail="Failed to get project summaries. Please try again."
        ) from e


@router.get("", response_model=OrgMetricsResponse)
async def get_org_metrics(
    org: AuthOrganization = Depends(get_current_organization),
    ctx: AuthContext | Any = Depends(get_auth_context),
) -> OrgMetricsResponse:
    """Get organization-wide metrics aggregating all projects."""
    try:
        group_id = str(org.id)
        service = await get_knowledge_read_adapter(group_id)

        # Get all projects
        projects = await _list_entities_by_type_paginated_via_service(
            service,
            EntityType.PROJECT,
            batch_size=500,
        )
        accessible_project_ids = (
            await list_accessible_project_graph_ids(ctx) if isinstance(ctx, AuthContext) else None
        )
        projects = _filter_projects_by_access(projects, accessible_project_ids)

        tasks = await _list_summary_metric_tasks(group_id, service)
        tasks = _filter_tasks_by_access(tasks, accessible_project_ids)

        status_dist = _compute_status_distribution(tasks)
        priority_dist = _compute_priority_distribution(tasks)
        assignees = _compute_assignee_stats(tasks)
        velocity = _compute_velocity_trend(tasks)

        total_tasks = len(tasks)
        tasks_created_7d = _count_recent_tasks(tasks, 7, "created_at")
        tasks_completed_7d = (
            sum(p.value for p in velocity[-7:])
            if len(velocity) >= 7
            else sum(p.value for p in velocity)
        )

        completed = status_dist.done
        completion_rate = (completed / total_tasks * 100) if total_tasks > 0 else 0.0

        now = datetime.now(UTC)
        project_task_counts = _compute_project_task_counts(tasks, now=now)
        projects_summary = _build_project_summaries(projects, project_task_counts)

        return OrgMetricsResponse(
            total_projects=len(projects),
            total_tasks=total_tasks,
            status_distribution=status_dist,
            priority_distribution=priority_dist,
            completion_rate=round(completion_rate, 1),
            top_assignees=assignees[:10],
            tasks_created_last_7d=tasks_created_7d,
            tasks_completed_last_7d=tasks_completed_7d,
            velocity_trend=velocity,
            projects_summary=projects_summary,
        )

    except Exception as e:
        log.exception("get_org_metrics_failed", error=str(e))
        raise HTTPException(
            status_code=500, detail="Failed to get organization metrics. Please try again."
        ) from e
