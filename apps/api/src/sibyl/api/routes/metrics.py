"""Metrics endpoints for project and org-level analytics.

Every number here is a rollup of the organization's open tasks: counts by
status and priority per project, assignee totals, completions per day, and
tasks created or due within a window. The graph answers those with one
grouped statement whose row count is bounded by the distinct (project,
status, priority) combinations rather than by the number of tasks, and the
result is memoized per organization so a burst of dashboard refetches
computes it once.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, status

from sibyl.api.decorators import handle_workflow_errors
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
from sibyl.auth.authorization import verify_entity_project_access
from sibyl.auth.context import AuthContext
from sibyl.auth.dependencies import get_auth_context, get_current_organization, require_org_role
from sibyl.persistence.auth_runtime import list_accessible_project_graph_ids
from sibyl.persistence.read_memo import OrgReadMemo
from sibyl_core.auth import AuthOrganization, OrganizationRole, ProjectRole
from sibyl_core.models.entities import EntityType
from sibyl_core.services import KnowledgeReadService

log = structlog.get_logger()


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


METRICS_MAX_TASKS = 10_000
VELOCITY_DAYS = 14
RECENT_DAYS = 7
# Rollups are dropped by the write path's broadcast; the window only bounds
# staleness for a write that never broadcasts.
TASK_ROLLUP_TTL_SECONDS = 10.0


class MetricsEntityLimitExceededError(RuntimeError):
    """Raised when metrics endpoint pagination exceeds the allowed entity cap."""


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


def _parse_iso_date(date_str: object) -> datetime | None:
    """Parse ISO date strings or datetime objects to UTC datetimes."""
    if not date_str:
        return None
    if isinstance(date_str, datetime):
        if date_str.tzinfo is None:
            return date_str.replace(tzinfo=UTC)
        return date_str.astimezone(UTC)
    if not isinstance(date_str, str):
        return None
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


# =============================================================================
# Task rollups
# =============================================================================


@dataclass(frozen=True, slots=True)
class TaskRollup:
    """Open tasks sharing one (project, status, priority) combination."""

    project_id: str
    status: str
    priority: str
    count: int


@dataclass(frozen=True, slots=True)
class AssigneeRollup:
    """Open tasks one assignee holds in one project at one status."""

    project_id: str
    assignee: str
    status: str
    count: int


@dataclass(frozen=True, slots=True)
class TaskCompletion:
    """One task completed inside the velocity window."""

    project_id: str
    completed_at: datetime


@dataclass(frozen=True, slots=True)
class TaskDueDate:
    """One open task carrying a due date."""

    project_id: str
    status: str
    due_date: datetime


@dataclass(frozen=True, slots=True)
class OrgTaskRollups:
    """Grouped counts over an organization's non-archived tasks.

    ``project_id`` is the empty string for tasks outside any project; those
    count toward organization totals and never toward a project summary.
    """

    tasks: tuple[TaskRollup, ...]
    assignees: tuple[AssigneeRollup, ...]
    created: tuple[tuple[str, int], ...]
    completions: tuple[TaskCompletion, ...]
    due_dates: tuple[TaskDueDate, ...]

    @property
    def total_tasks(self) -> int:
        return sum(row.count for row in self.tasks)

    def scoped(self, accessible_project_ids: set[str] | None) -> OrgTaskRollups:
        """Keep the rows of accessible projects when project RBAC applies.

        A caller with a project set sees only project work; unassigned tasks
        drop out the way they always have for a scoped reader.
        """
        if accessible_project_ids is None:
            return self

        def allowed(project_id: str) -> bool:
            return bool(project_id) and project_id in accessible_project_ids

        return OrgTaskRollups(
            tasks=tuple(row for row in self.tasks if allowed(row.project_id)),
            assignees=tuple(row for row in self.assignees if allowed(row.project_id)),
            created=tuple(item for item in self.created if allowed(item[0])),
            completions=tuple(row for row in self.completions if allowed(row.project_id)),
            due_dates=tuple(row for row in self.due_dates if allowed(row.project_id)),
        )


def _text(value: object) -> str:
    return str(value).strip() if isinstance(value, str) else ""


def _status_value(value: object) -> str:
    return _text(value).lower() or "backlog"


def _priority_value(value: object) -> str:
    return _text(value).lower() or "medium"


def _count_value(value: object) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _assignee_names(value: object) -> list[str]:
    """Read assignees as the list they are, or the single string they were."""
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, Iterable) and not isinstance(value, bytes | Mapping):
        return [str(name) for name in value if name]
    return []


def _nested_rows(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    return [
        {str(key): item for key, item in row.items()} for row in value if isinstance(row, Mapping)
    ]


def rollups_from_task_dicts(tasks: Iterable[Mapping[str, Any]], *, now: datetime) -> OrgTaskRollups:
    """Group task-shaped dictionaries the way the grouped statement would.

    Serves the runtime that cannot run the aggregate, so a non-Surreal graph
    renders the same response from paged rows.
    """
    task_counts: dict[tuple[str, str, str], int] = defaultdict(int)
    assignee_counts: dict[tuple[str, str, str], int] = defaultdict(int)
    created_counts: dict[str, int] = defaultdict(int)
    completions: list[TaskCompletion] = []
    due_dates: list[TaskDueDate] = []
    created_cutoff = now - timedelta(days=RECENT_DAYS)

    for task in tasks:
        metadata = task.get("metadata")
        if not isinstance(metadata, Mapping):
            metadata = {}
        status_value = _status_value(metadata.get("status"))
        if status_value == "archived":
            continue
        project_id = _text(metadata.get("project_id"))
        priority = _priority_value(metadata.get("priority"))
        task_counts[(project_id, status_value, priority)] += 1
        for name in _assignee_names(metadata.get("assignees")):
            assignee_counts[(project_id, name, status_value)] += 1

        created_at = _parse_iso_date(task.get("created_at") or metadata.get("created_at"))
        if created_at is not None and created_at >= created_cutoff:
            created_counts[project_id] += 1

        if status_value == "done":
            completed_at = _parse_iso_date(metadata.get("completed_at")) or _parse_iso_date(
                task.get("updated_at")
            )
            if completed_at is not None:
                completions.append(TaskCompletion(project_id, completed_at))

        due_date = _parse_iso_date(metadata.get("due_date"))
        if due_date is not None:
            due_dates.append(TaskDueDate(project_id, status_value, due_date))

    return OrgTaskRollups(
        tasks=tuple(TaskRollup(*key, count) for key, count in task_counts.items()),
        assignees=tuple(AssigneeRollup(*key, count) for key, count in assignee_counts.items()),
        created=tuple(created_counts.items()),
        completions=tuple(completions),
        due_dates=tuple(due_dates),
    )


def rollups_from_surreal_payload(payload: Mapping[str, object]) -> OrgTaskRollups:
    """Read the grouped statement's RETURN payload."""
    tasks = tuple(
        TaskRollup(
            _text(row.get("project_id")),
            _status_value(row.get("status")),
            _priority_value(row.get("priority")),
            _count_value(row.get("n")),
        )
        for row in _nested_rows(payload.get("tasks"))
    )
    assignees: list[AssigneeRollup] = []
    for row in _nested_rows(payload.get("assignees")):
        project_id = _text(row.get("project_id"))
        status_value = _status_value(row.get("status"))
        count = _count_value(row.get("n"))
        assignees.extend(
            AssigneeRollup(project_id, name, status_value, count)
            for name in _assignee_names(row.get("assignees"))
        )
    created = tuple(
        (_text(row.get("project_id")), _count_value(row.get("n")))
        for row in _nested_rows(payload.get("created"))
    )
    completions: list[TaskCompletion] = []
    for row in _nested_rows(payload.get("completions")):
        completed_at = _parse_iso_date(row.get("completed_at")) or _parse_iso_date(
            row.get("updated_at")
        )
        if completed_at is not None:
            completions.append(TaskCompletion(_text(row.get("project_id")), completed_at))
    due_dates: list[TaskDueDate] = []
    for row in _nested_rows(payload.get("due_dates")):
        due_date = _parse_iso_date(row.get("due_date"))
        if due_date is not None:
            due_dates.append(
                TaskDueDate(
                    _text(row.get("project_id")), _status_value(row.get("status")), due_date
                )
            )
    return OrgTaskRollups(
        tasks=tasks,
        assignees=tuple(assignees),
        created=created,
        completions=tuple(completions),
        due_dates=tuple(due_dates),
    )


# The reader resolves attributes.* over the top-level column, so every group
# key coalesces the same way. The not-archived predicate filters after the
# entity_type index access; a grouped answer is bounded by distinct
# (project, status, priority) combinations, never by the task count.
# Completions are selected as rows: completed_at is stored as text, so the
# datetime column updated_at (bumped by the completing write, so never older)
# bounds the window and Python reads the real completion instant.
TASK_ROLLUP_STATEMENT = """
RETURN {
    tasks: (
        SELECT attributes.project_id ?? project_id AS project_id,
               attributes.status ?? status AS status,
               attributes.priority ?? priority AS priority,
               count() AS n
        FROM entity
        WHERE group_id = $group_id
          AND entity_type = $task_type
          AND string::lowercase(attributes.status ?? status ?? '') != 'archived'
        GROUP BY project_id, status, priority
    ),
    assignees: (
        SELECT attributes.project_id ?? project_id AS project_id,
               attributes.assignees AS assignees,
               attributes.status ?? status AS status,
               count() AS n
        FROM entity
        WHERE group_id = $group_id
          AND entity_type = $task_type
          AND string::lowercase(attributes.status ?? status ?? '') != 'archived'
          AND attributes.assignees != NONE
        GROUP BY project_id, assignees, status
    ),
    created: (
        SELECT attributes.project_id ?? project_id AS project_id,
               count() AS n
        FROM entity
        WHERE group_id = $group_id
          AND entity_type = $task_type
          AND string::lowercase(attributes.status ?? status ?? '') != 'archived'
          AND created_at >= $created_cutoff
        GROUP BY project_id
    ),
    completions: (
        SELECT attributes.project_id ?? project_id AS project_id,
               attributes.completed_at AS completed_at,
               updated_at
        FROM entity
        WHERE group_id = $group_id
          AND entity_type = $task_type
          AND string::lowercase(attributes.status ?? status ?? '') = 'done'
          AND updated_at >= $completed_cutoff
    ),
    due_dates: (
        SELECT attributes.project_id ?? project_id AS project_id,
               attributes.status ?? status AS status,
               attributes.due_date AS due_date
        FROM entity
        WHERE group_id = $group_id
          AND entity_type = $task_type
          AND string::lowercase(attributes.status ?? status ?? '') NOT IN ['archived', 'done']
          AND attributes.due_date != NONE
    ),
};
"""

_TASK_ROLLUPS: OrgReadMemo[OrgTaskRollups] = OrgReadMemo(
    "task-rollups", ttl_seconds=TASK_ROLLUP_TTL_SECONDS
)


async def _load_surreal_task_rollups(group_id: str) -> OrgTaskRollups | None:
    now = datetime.now(UTC)
    try:
        rows = await execute_surreal_graph_query(
            group_id,
            TASK_ROLLUP_STATEMENT,
            task_type=EntityType.TASK.value,
            created_cutoff=now - timedelta(days=RECENT_DAYS),
            completed_cutoff=now - timedelta(days=VELOCITY_DAYS),
        )
    except Exception as exc:
        log.warning(
            "surreal_task_rollup_failed",
            group_id=group_id,
            error_type=type(exc).__name__,
        )
        return None
    if rows is None:
        return None
    payload = rows[0] if rows and isinstance(rows[0], Mapping) else {}
    return rollups_from_surreal_payload(payload)


async def _list_entities_by_type_paginated_via_service(
    service: KnowledgeReadService,
    entity_type: EntityType,
    *,
    batch_size: int = 1000,
    max_entities: int | None = None,
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
        if max_entities is not None and len(entities) > max_entities:
            raise MetricsEntityLimitExceededError(
                f"metrics entity limit exceeded for {entity_type.value}: {max_entities}"
            )
        if page.next_cursor is None:
            break
        cursor = page.next_cursor

    return entities


async def _load_task_rollups(group_id: str, service: KnowledgeReadService) -> OrgTaskRollups:
    """Return the organization's task rollups, computed once per window."""

    async def compute() -> OrgTaskRollups:
        rollups = await _load_surreal_task_rollups(group_id)
        if rollups is not None:
            return rollups
        tasks = [
            task.model_dump()
            for task in await _list_entities_by_type_paginated_via_service(
                service,
                EntityType.TASK,
                batch_size=1000,
                max_entities=METRICS_MAX_TASKS,
            )
        ]
        return rollups_from_task_dicts(tasks, now=datetime.now(UTC))

    return await _TASK_ROLLUPS.get(group_id, compute)


# =============================================================================
# Rendering
# =============================================================================


def _status_distribution(rollups: OrgTaskRollups) -> TaskStatusDistribution:
    dist = TaskStatusDistribution()
    for row in rollups.tasks:
        if row.status in TaskStatusDistribution.model_fields:
            setattr(dist, row.status, getattr(dist, row.status) + row.count)
    return dist


def _priority_distribution(rollups: OrgTaskRollups) -> TaskPriorityDistribution:
    dist = TaskPriorityDistribution()
    for row in rollups.tasks:
        if row.priority in TaskPriorityDistribution.model_fields:
            setattr(dist, row.priority, getattr(dist, row.priority) + row.count)
    return dist


def _assignee_stats(rollups: OrgTaskRollups) -> list[AssigneeStats]:
    stats: dict[str, dict[str, int]] = defaultdict(
        lambda: {"total": 0, "completed": 0, "in_progress": 0}
    )
    for row in rollups.assignees:
        entry = stats[row.assignee]
        entry["total"] += row.count
        if row.status == "done":
            entry["completed"] += row.count
        elif row.status == "doing":
            entry["in_progress"] += row.count
    return [
        AssigneeStats(name=name, **data)
        for name, data in sorted(stats.items(), key=lambda item: item[1]["total"], reverse=True)
    ]


def _velocity_trend(
    rollups: OrgTaskRollups, *, now: datetime, days: int = VELOCITY_DAYS
) -> list[TimeSeriesPoint]:
    """Daily completion counts for the last ``days`` days, oldest first."""
    daily_counts = {
        (now - timedelta(days=offset)).strftime("%Y-%m-%d"): 0 for offset in range(days)
    }
    cutoff = now - timedelta(days=days)
    for row in rollups.completions:
        if row.completed_at < cutoff:
            continue
        key = row.completed_at.strftime("%Y-%m-%d")
        if key in daily_counts:
            daily_counts[key] += 1
    return [TimeSeriesPoint(date=date, value=count) for date, count in sorted(daily_counts.items())]


def _completed_last_week(velocity: list[TimeSeriesPoint]) -> int:
    return (
        sum(point.value for point in velocity[-RECENT_DAYS:])
        if len(velocity) >= RECENT_DAYS
        else sum(point.value for point in velocity)
    )


def _created_last_week(rollups: OrgTaskRollups) -> int:
    return sum(count for _project_id, count in rollups.created)


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


_STATUS_COUNT_KEYS = {
    "done": "completed",
    "doing": "doing",
    "blocked": "blocked",
    "review": "review",
    "todo": "todo",
    "backlog": "backlog",
}


def _project_task_counts(rollups: OrgTaskRollups, *, now: datetime) -> dict[str, dict[str, int]]:
    """Aggregate per-project task rollups from grouped rows."""
    project_task_counts: dict[str, dict[str, int]] = defaultdict(_empty_project_task_counts)
    for row in rollups.tasks:
        if not row.project_id:
            continue
        counts = project_task_counts[row.project_id]
        counts["total"] += row.count
        if (key := _STATUS_COUNT_KEYS.get(row.status)) is not None:
            counts[key] += row.count
        if _is_open_status(row.status) and row.priority in ("critical", "high"):
            counts[row.priority] += row.count
    for due in rollups.due_dates:
        if due.project_id and _is_open_status(due.status) and due.due_date < now:
            project_task_counts[due.project_id]["overdue"] += 1
    return project_task_counts


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


def _filter_projects_by_access(
    projects: list[Any], accessible_project_ids: set[str] | None
) -> list[Any]:
    """Filter projects to the caller's accessible set when provided."""
    if accessible_project_ids is None:
        return projects
    return [project for project in projects if str(project.id) in accessible_project_ids]


# =============================================================================
# Endpoints
# =============================================================================


@router.get("/projects/{project_id}", response_model=ProjectMetricsResponse)
@handle_workflow_errors("get_project_metrics", id_param="project_id")
async def get_project_metrics(
    project_id: str,
    org: AuthOrganization = Depends(get_current_organization),
    ctx: AuthContext = Depends(get_auth_context),
) -> ProjectMetricsResponse:
    """Get metrics for a specific project."""
    group_id = str(org.id)
    # Metrics summarize a project's tasks, so reaching them requires the same
    # membership that reading the project itself does.
    await verify_entity_project_access(None, ctx, project_id, required_role=ProjectRole.VIEWER)
    service = await get_knowledge_read_adapter(group_id)

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

    rollups = (await _load_task_rollups(group_id, service)).scoped({project_id})
    now = datetime.now(UTC)
    status_dist = _status_distribution(rollups)
    velocity = _velocity_trend(rollups, now=now)
    total = rollups.total_tasks
    completion_rate = (status_dist.done / total * 100) if total > 0 else 0.0

    metrics = ProjectMetrics(
        project_id=project_id,
        project_name=project.name,
        total_tasks=total,
        status_distribution=status_dist,
        priority_distribution=_priority_distribution(rollups),
        completion_rate=round(completion_rate, 1),
        assignees=_assignee_stats(rollups)[:10],  # Top 10 assignees
        tasks_created_last_7d=_created_last_week(rollups),
        tasks_completed_last_7d=_completed_last_week(velocity),
        velocity_trend=velocity,
    )

    return ProjectMetricsResponse(metrics=metrics)


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

        rollups = (await _load_task_rollups(group_id, service)).scoped(accessible_project_ids)
        counts_by_project = _project_task_counts(rollups, now=datetime.now(UTC))

        return ProjectSummariesResponse(
            projects_summary=_build_project_summaries(projects, counts_by_project)
        )

    except MetricsEntityLimitExceededError as e:
        log.warning(
            "get_project_summaries_entity_limit_exceeded", error=str(e), group_id=str(org.id)
        )
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail="Too many tasks to compute project summaries. Please narrow scope.",
        ) from e
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

        rollups = (await _load_task_rollups(group_id, service)).scoped(accessible_project_ids)
        now = datetime.now(UTC)

        status_dist = _status_distribution(rollups)
        velocity = _velocity_trend(rollups, now=now)
        total_tasks = rollups.total_tasks
        completion_rate = (status_dist.done / total_tasks * 100) if total_tasks > 0 else 0.0
        projects_summary = _build_project_summaries(
            projects, _project_task_counts(rollups, now=now)
        )

        return OrgMetricsResponse(
            total_projects=len(projects),
            total_tasks=total_tasks,
            status_distribution=status_dist,
            priority_distribution=_priority_distribution(rollups),
            completion_rate=round(completion_rate, 1),
            top_assignees=_assignee_stats(rollups)[:10],
            tasks_created_last_7d=_created_last_week(rollups),
            tasks_completed_last_7d=_completed_last_week(velocity),
            velocity_trend=velocity,
            projects_summary=projects_summary,
        )

    except MetricsEntityLimitExceededError as e:
        log.warning("get_org_metrics_entity_limit_exceeded", error=str(e), group_id=str(org.id))
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail="Too many tasks to compute organization metrics. Please narrow scope.",
        ) from e
    except Exception as e:
        log.exception("get_org_metrics_failed", error=str(e))
        raise HTTPException(
            status_code=500, detail="Failed to get organization metrics. Please try again."
        ) from e
