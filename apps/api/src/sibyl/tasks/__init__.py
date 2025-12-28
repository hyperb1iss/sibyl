"""Task management system for Sibyl knowledge graph."""

from sibyl.tasks.dependencies import (
    CycleResult,
    DependencyResult,
    TaskOrderResult,
    detect_dependency_cycles,
    get_blocking_tasks,
    get_task_dependencies,
    suggest_task_order,
)
from sibyl.tasks.estimation import (
    SimilarTask,
    TaskEstimate,
    batch_estimate,
    calculate_project_estimate,
    estimate_task_effort,
)
from sibyl.tasks.manager import TaskManager
from sibyl.tasks.workflow import (
    TaskWorkflowEngine,
    get_allowed_transitions,
    is_valid_transition,
)

__all__ = [
    # Workflow
    "TaskManager",
    "TaskWorkflowEngine",
    "is_valid_transition",
    "get_allowed_transitions",
    # Dependencies
    "get_task_dependencies",
    "get_blocking_tasks",
    "detect_dependency_cycles",
    "suggest_task_order",
    "DependencyResult",
    "CycleResult",
    "TaskOrderResult",
    # Estimation
    "estimate_task_effort",
    "batch_estimate",
    "calculate_project_estimate",
    "TaskEstimate",
    "SimilarTask",
]
