"""Metrics endpoints for project and org-level analytics."""

from collections import defaultdict
from datetime import UTC, datetime, timedelta

import structlog
from fastapi import APIRouter, Depends, HTTPException

from sibyl.api.schemas import (
    AssigneeStats,
    OrgMetricsResponse,
    ProjectMetrics,
    ProjectMetricsResponse,
    TaskPriorityDistribution,
    TaskStatusDistribution,
    TimeSeriesPoint,
)
from sibyl.auth.dependencies import get_current_organization, require_org_role
from sibyl.db.models import Organization, OrganizationRole
from sibyl_core.graph.client import get_graph_client
from sibyl_core.graph.entities import EntityManager
from sibyl_core.models.entities import EntityType

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


def _parse_iso_date(date_str: str | None) -> datetime | None:
    """Parse ISO date string to datetime."""
    if not date_str:
        return None
    try:
        return datetime.fromisoformat(date_str)
    except (ValueError, TypeError):
        return None


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


@router.get("/projects/{project_id}", response_model=ProjectMetricsResponse)
async def get_project_metrics(
    project_id: str,
    org: Organization = Depends(get_current_organization),
) -> ProjectMetricsResponse:
    """Get metrics for a specific project."""
    try:
        group_id = str(org.id)
        client = await get_graph_client()
        entity_manager = EntityManager(client, group_id=group_id)

        # Get project
        project = await entity_manager.get(project_id)
        if not project:
            raise HTTPException(status_code=404, detail=f"Project not found: {project_id}")

        # Get all tasks for this project
        all_tasks = await entity_manager.list_by_type(EntityType.TASK, limit=1000)
        # Filter to this project
        tasks = [t.model_dump() for t in all_tasks if t.metadata.get("project_id") == project_id]

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


@router.get("", response_model=OrgMetricsResponse)
async def get_org_metrics(
    org: Organization = Depends(get_current_organization),
) -> OrgMetricsResponse:
    """Get organization-wide metrics aggregating all projects."""
    try:
        group_id = str(org.id)
        client = await get_graph_client()
        entity_manager = EntityManager(client, group_id=group_id)

        # Get all projects
        projects = await entity_manager.list_by_type(EntityType.PROJECT, limit=500)

        # Get all tasks
        all_tasks = await entity_manager.list_by_type(EntityType.TASK, limit=5000)
        tasks = [t.model_dump() for t in all_tasks]

        # Compute aggregate metrics
        status_dist = _compute_status_distribution(tasks)
        priority_dist = _compute_priority_distribution(tasks)
        assignees = _compute_assignee_stats(tasks)
        velocity = _compute_velocity_trend(tasks)

        total_tasks = len(tasks)
        completed = status_dist.done
        completion_rate = (completed / total_tasks * 100) if total_tasks > 0 else 0.0

        tasks_created_7d = _count_recent_tasks(tasks, 7, "created_at")
        tasks_completed_7d = (
            sum(p.value for p in velocity[-7:])
            if len(velocity) >= 7
            else sum(p.value for p in velocity)
        )

        # Build project summaries
        project_task_counts: dict[str, dict] = defaultdict(lambda: {"total": 0, "completed": 0})
        for task in tasks:
            proj_id = task.get("metadata", {}).get("project_id", "")
            if proj_id:
                project_task_counts[proj_id]["total"] += 1
                if task.get("metadata", {}).get("status") == "done":
                    project_task_counts[proj_id]["completed"] += 1

        projects_summary = []
        for project in projects:
            counts = project_task_counts.get(project.id, {"total": 0, "completed": 0})
            rate = (counts["completed"] / counts["total"] * 100) if counts["total"] > 0 else 0.0
            projects_summary.append(
                {
                    "id": project.id,
                    "name": project.name,
                    "total": counts["total"],
                    "completed": counts["completed"],
                    "completion_rate": round(rate, 1),
                }
            )

        # Sort by total tasks descending
        projects_summary.sort(key=lambda x: x["total"], reverse=True)

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
            projects_summary=projects_summary[:20],  # Top 20 projects
        )

    except Exception as e:
        log.exception("get_org_metrics_failed", error=str(e))
        raise HTTPException(
            status_code=500, detail="Failed to get organization metrics. Please try again."
        ) from e
