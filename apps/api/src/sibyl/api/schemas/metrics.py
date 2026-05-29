"""Project and organization metrics request/response models."""

from pydantic import BaseModel


class TaskStatusDistribution(BaseModel):
    """Task counts by status."""

    backlog: int = 0
    todo: int = 0
    doing: int = 0
    blocked: int = 0
    review: int = 0
    done: int = 0


class TaskPriorityDistribution(BaseModel):
    """Task counts by priority."""

    critical: int = 0
    high: int = 0
    medium: int = 0
    low: int = 0
    someday: int = 0


class AssigneeStats(BaseModel):
    """Stats per assignee."""

    name: str
    total: int = 0
    completed: int = 0
    in_progress: int = 0


class TimeSeriesPoint(BaseModel):
    """Single point in a time series."""

    date: str  # ISO date string (YYYY-MM-DD)
    value: int


class ProjectMetrics(BaseModel):
    """Metrics for a single project."""

    project_id: str
    project_name: str
    total_tasks: int
    status_distribution: TaskStatusDistribution
    priority_distribution: TaskPriorityDistribution
    completion_rate: float  # 0-100
    assignees: list[AssigneeStats]
    tasks_created_last_7d: int
    tasks_completed_last_7d: int
    velocity_trend: list[TimeSeriesPoint]  # completions per day last 14 days


class ProjectMetricsResponse(BaseModel):
    """Response for project metrics."""

    metrics: ProjectMetrics


class ProjectSummary(BaseModel):
    """Task rollup for a single project in org-wide metrics."""

    id: str
    name: str
    total: int = 0
    completed: int = 0
    doing: int = 0
    blocked: int = 0
    review: int = 0
    todo: int = 0
    backlog: int = 0
    critical: int = 0
    high: int = 0
    overdue: int = 0
    completion_rate: float = 0.0


class ProjectSummariesResponse(BaseModel):
    """Lean response for project-summary views."""

    projects_summary: list[ProjectSummary]


class OrgMetricsResponse(BaseModel):
    """Organization-level metrics aggregating all projects."""

    total_projects: int
    total_tasks: int
    status_distribution: TaskStatusDistribution
    priority_distribution: TaskPriorityDistribution
    completion_rate: float
    top_assignees: list[AssigneeStats]
    tasks_created_last_7d: int
    tasks_completed_last_7d: int
    velocity_trend: list[TimeSeriesPoint]
    projects_summary: list[ProjectSummary]
