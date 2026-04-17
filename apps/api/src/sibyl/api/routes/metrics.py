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
from sibyl.auth.dependencies import get_current_organization, require_org_role
from sibyl.db.models import Organization, OrganizationRole
from sibyl.persistence.legacy.graph import get_legacy_knowledge_read_adapter
from sibyl_core.graph.client import get_graph_client
from sibyl_core.graph.entities import EntityManager
from sibyl_core.models.entities import EntityType
from sibyl_core.services import KnowledgeReadService

log = structlog.get_logger()

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
    entity_manager: EntityManager,
    entity_type: EntityType,
    *,
    batch_size: int = 1000,
    **filters: Any,
) -> list[Any]:
    """List all matching entities by paging through the graph query."""
    entities: list[Any] = []
    offset = 0

    while True:
        batch = await entity_manager.list_by_type(
            entity_type,
            limit=batch_size,
            offset=offset,
            **filters,
        )
        if not batch:
            break

        entities.extend(batch)
        if len(batch) < batch_size:
            break

        offset += batch_size

    return entities


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


async def _fetch_org_metric_tasks(client, organization_id: str) -> list[dict[str, Any]]:
    """Fetch raw task rows and normalize legacy metadata fallbacks in Python."""
    rows = await client.execute_read_org(
        f"""
        {_task_metrics_where_clause()}
        RETURN n.project_id AS project_id,
               n.status AS status,
               n.priority AS priority,
               n.assignees AS assignees,
               n.created_at AS created_at,
               n.completed_at AS completed_at,
               n.updated_at AS updated_at,
               n.due_date AS due_date,
               n.metadata AS metadata
        """,
        organization_id,
        group_id=organization_id,
    )

    return [_normalize_metric_task_row(row) for row in rows]


def _task_metrics_where_clause() -> str:
    """Return the shared task filter used by org-wide metrics queries."""
    return """
        MATCH (n)
        WHERE n.entity_type = 'task'
          AND n.group_id = $group_id
          AND (n.status IS NULL OR toLower(n.status) <> 'archived')
          AND NOT toLower(toString(n.metadata)) CONTAINS '"status":"archived"'
          AND NOT toLower(toString(n.metadata)) CONTAINS '"status": "archived"'
    """


async def _fetch_task_overview(
    client, organization_id: str, now: datetime
) -> tuple[TaskStatusDistribution, TaskPriorityDistribution, int, int]:
    """Fetch org-wide task counts, distributions, and recent creation totals."""
    cutoff = (now - timedelta(days=7)).isoformat()
    rows = await client.execute_read_org(
        f"""
        {_task_metrics_where_clause()}
        RETURN count(n) AS total_tasks,
               count(CASE WHEN n.status IS NULL OR toLower(n.status) = 'backlog' THEN 1 END) AS backlog_tasks,
               count(CASE WHEN toLower(n.status) = 'todo' THEN 1 END) AS todo_tasks,
               count(CASE WHEN toLower(n.status) = 'doing' THEN 1 END) AS doing_tasks,
               count(CASE WHEN toLower(n.status) = 'blocked' THEN 1 END) AS blocked_tasks,
               count(CASE WHEN toLower(n.status) = 'review' THEN 1 END) AS review_tasks,
               count(CASE WHEN toLower(n.status) = 'done' THEN 1 END) AS done_tasks,
               count(CASE WHEN n.priority IS NULL OR toLower(n.priority) = 'medium' THEN 1 END) AS medium_tasks,
               count(CASE WHEN toLower(n.priority) = 'critical' THEN 1 END) AS critical_tasks,
               count(CASE WHEN toLower(n.priority) = 'high' THEN 1 END) AS high_tasks,
               count(CASE WHEN toLower(n.priority) = 'low' THEN 1 END) AS low_tasks,
               count(CASE WHEN toLower(n.priority) = 'someday' THEN 1 END) AS someday_tasks,
               count(
                   CASE
                       WHEN n.created_at IS NOT NULL
                        AND n.created_at >= $recent_cutoff
                       THEN 1
                   END
               ) AS tasks_created_last_7d
        """,
        organization_id,
        group_id=organization_id,
        recent_cutoff=cutoff,
    )
    row = rows[0] if rows else {}

    status_distribution = TaskStatusDistribution(
        backlog=int(row.get("backlog_tasks", 0) or 0),
        todo=int(row.get("todo_tasks", 0) or 0),
        doing=int(row.get("doing_tasks", 0) or 0),
        blocked=int(row.get("blocked_tasks", 0) or 0),
        review=int(row.get("review_tasks", 0) or 0),
        done=int(row.get("done_tasks", 0) or 0),
    )
    priority_distribution = TaskPriorityDistribution(
        critical=int(row.get("critical_tasks", 0) or 0),
        high=int(row.get("high_tasks", 0) or 0),
        medium=int(row.get("medium_tasks", 0) or 0),
        low=int(row.get("low_tasks", 0) or 0),
        someday=int(row.get("someday_tasks", 0) or 0),
    )
    total_tasks = int(row.get("total_tasks", 0) or 0)
    tasks_created_last_7d = int(row.get("tasks_created_last_7d", 0) or 0)

    return status_distribution, priority_distribution, total_tasks, tasks_created_last_7d


async def _fetch_project_task_counts(
    client, organization_id: str, now: datetime
) -> dict[str, dict]:
    """Fetch per-project task rollups for org-wide metrics."""
    rows = await client.execute_read_org(
        f"""
        {_task_metrics_where_clause()}
          AND coalesce(n.project_id, '') <> ''
        RETURN n.project_id AS project_id,
               count(n) AS total,
               count(CASE WHEN n.status IS NULL OR toLower(n.status) = 'backlog' THEN 1 END) AS backlog,
               count(CASE WHEN toLower(n.status) = 'todo' THEN 1 END) AS todo,
               count(CASE WHEN toLower(n.status) = 'doing' THEN 1 END) AS doing,
               count(CASE WHEN toLower(n.status) = 'blocked' THEN 1 END) AS blocked,
               count(CASE WHEN toLower(n.status) = 'review' THEN 1 END) AS review,
               count(CASE WHEN toLower(n.status) = 'done' THEN 1 END) AS completed,
               count(
                   CASE
                       WHEN toLower(coalesce(n.status, 'backlog')) <> 'done'
                        AND toLower(n.priority) = 'critical'
                       THEN 1
                   END
               ) AS critical,
               count(
                   CASE
                       WHEN toLower(coalesce(n.status, 'backlog')) <> 'done'
                        AND toLower(n.priority) = 'high'
                       THEN 1
                   END
               ) AS high,
               count(
                   CASE
                       WHEN toLower(coalesce(n.status, 'backlog')) <> 'done'
                        AND n.due_date IS NOT NULL
                        AND n.due_date <> ''
                        AND n.due_date < $now_iso
                       THEN 1
                   END
               ) AS overdue
        ORDER BY total DESC, project_id ASC
        """,
        organization_id,
        group_id=organization_id,
        now_iso=now.isoformat(),
    )

    counts_by_project: dict[str, dict] = {}
    for row in rows:
        project_id = row.get("project_id")
        if not project_id:
            continue
        counts_by_project[str(project_id)] = {
            "total": int(row.get("total", 0) or 0),
            "completed": int(row.get("completed", 0) or 0),
            "doing": int(row.get("doing", 0) or 0),
            "blocked": int(row.get("blocked", 0) or 0),
            "review": int(row.get("review", 0) or 0),
            "todo": int(row.get("todo", 0) or 0),
            "backlog": int(row.get("backlog", 0) or 0),
            "critical": int(row.get("critical", 0) or 0),
            "high": int(row.get("high", 0) or 0),
            "overdue": int(row.get("overdue", 0) or 0),
        }

    return counts_by_project


async def _fetch_top_assignees(client, organization_id: str) -> list[AssigneeStats]:
    """Fetch the top assignees without materializing all task rows."""
    rows = await client.execute_read_org(
        f"""
        {_task_metrics_where_clause()}
        WITH coalesce(n.assignees, []) AS assignees, coalesce(n.status, '') AS status
        UNWIND assignees AS assignee
        WITH assignee, status
        WHERE assignee IS NOT NULL AND assignee <> ''
        RETURN assignee AS name,
               count(*) AS total,
               count(CASE WHEN toLower(status) = 'done' THEN 1 END) AS completed,
               count(CASE WHEN toLower(status) = 'doing' THEN 1 END) AS in_progress
        ORDER BY total DESC, name ASC
        LIMIT 10
        """,
        organization_id,
        group_id=organization_id,
    )

    return [
        AssigneeStats(
            name=str(row.get("name", "")),
            total=int(row.get("total", 0) or 0),
            completed=int(row.get("completed", 0) or 0),
            in_progress=int(row.get("in_progress", 0) or 0),
        )
        for row in rows
        if row.get("name")
    ]


async def _fetch_velocity_trend(
    client, organization_id: str, now: datetime
) -> tuple[list[TimeSeriesPoint], int]:
    """Fetch the completion trend and last-7-day completion count."""
    trend_start = (now - timedelta(days=13)).isoformat()
    rows = await client.execute_read_org(
        f"""
        {_task_metrics_where_clause()}
          AND toLower(coalesce(n.status, '')) = 'done'
          AND coalesce(n.completed_at, n.updated_at) IS NOT NULL
        WITH substring(coalesce(n.completed_at, n.updated_at), 0, 10) AS date, count(*) AS value
        WHERE date >= $trend_start
        RETURN date, value
        ORDER BY date ASC
        """,
        organization_id,
        group_id=organization_id,
        trend_start=trend_start[:10],
    )

    counts_by_date = {
        str(row.get("date")): int(row.get("value", 0) or 0) for row in rows if row.get("date")
    }
    trend: list[TimeSeriesPoint] = []
    for i in range(13, -1, -1):
        date = (now - timedelta(days=i)).strftime("%Y-%m-%d")
        trend.append(TimeSeriesPoint(date=date, value=counts_by_date.get(date, 0)))

    tasks_completed_last_7d = sum(point.value for point in trend[-7:])
    return trend, tasks_completed_last_7d


@router.get("/projects/{project_id}", response_model=ProjectMetricsResponse)
async def get_project_metrics(
    project_id: str,
    org: Organization = Depends(get_current_organization),
) -> ProjectMetricsResponse:
    """Get metrics for a specific project."""
    try:
        group_id = str(org.id)
        service = await get_legacy_knowledge_read_adapter(group_id)

        # Get project
        project = await service.get_entity(project_id)
        if not project:
            raise HTTPException(status_code=404, detail=f"Project not found: {project_id}")

        client = await get_graph_client()
        entity_manager = EntityManager(client, group_id=group_id)

        project_tasks = await _list_entities_by_type_paginated(
            entity_manager,
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
    org: Organization = Depends(get_current_organization),
) -> ProjectSummariesResponse:
    """Get the lean project-summary payload for the projects page."""
    try:
        group_id = str(org.id)
        service = await get_legacy_knowledge_read_adapter(group_id)
        client = await get_graph_client()
        projects = await _list_entities_by_type_paginated_via_service(
            service,
            EntityType.PROJECT,
            batch_size=500,
        )
        counts_by_project = await _fetch_project_task_counts(client, group_id, datetime.now(UTC))

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
    org: Organization = Depends(get_current_organization),
) -> OrgMetricsResponse:
    """Get organization-wide metrics aggregating all projects."""
    try:
        group_id = str(org.id)
        service = await get_legacy_knowledge_read_adapter(group_id)
        client = await get_graph_client()

        # Get all projects
        projects = await _list_entities_by_type_paginated_via_service(
            service,
            EntityType.PROJECT,
            batch_size=500,
        )
        tasks = await _fetch_org_metric_tasks(client, group_id)

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

        project_task_counts: dict[str, dict[str, int]] = defaultdict(_empty_project_task_counts)
        now = datetime.now(UTC)
        for task in tasks:
            metadata = task.get("metadata", {})
            proj_id = metadata.get("project_id", "")
            if proj_id:
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
