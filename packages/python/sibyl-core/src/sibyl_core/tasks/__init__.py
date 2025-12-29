"""Task management system for Sibyl knowledge graph."""

from sibyl_core.tasks.dependencies import (
    CycleResult,
    DependencyResult,
    TaskOrderResult,
    detect_dependency_cycles,
    get_blocking_tasks,
    get_task_dependencies,
    suggest_task_order,
)
from sibyl_core.tasks.estimation import (
    SimilarTask,
    TaskEstimate,
    batch_estimate,
    calculate_project_estimate,
    estimate_task_effort,
)
from sibyl_core.tasks.manager import TaskManager
from sibyl_core.tasks.workflow import (
    TaskWorkflowEngine,
    get_allowed_transitions,
    is_valid_transition,
)

__all__ = [
    "CycleResult",
    "DependencyResult",
    "SimilarTask",
    "TaskEstimate",
    # Workflow
    "TaskManager",
    "TaskOrderResult",
    "TaskWorkflowEngine",
    "batch_estimate",
    "calculate_project_estimate",
    "detect_dependency_cycles",
    # Estimation
    "estimate_task_effort",
    "get_allowed_transitions",
    "get_blocking_tasks",
    # Dependencies
    "get_task_dependencies",
    "is_valid_transition",
    "suggest_task_order",
]
