"""Task effort estimation from historical data."""

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import structlog

from sibyl.models.entities import EntityType
from sibyl.models.tasks import TaskStatus

if TYPE_CHECKING:
    from sibyl.graph.entities import EntityManager

log = structlog.get_logger()


@dataclass
class SimilarTask:
    """A similar completed task used for estimation."""

    task_id: str
    title: str
    similarity_score: float
    actual_hours: float


@dataclass
class TaskEstimate:
    """Result of effort estimation."""

    estimated_hours: float
    confidence: float  # 0-1 scale
    similar_tasks: list[SimilarTask] = field(default_factory=list)
    sample_count: int = 0
    message: str = ""


async def estimate_task_effort(
    entity_manager: "EntityManager",
    task_id: str | None = None,
    title: str | None = None,
    description: str | None = None,
    min_similarity: float = 0.5,
    max_samples: int = 10,
) -> TaskEstimate:
    """Estimate effort for a task based on similar completed tasks.

    Uses semantic search to find similar completed tasks, then calculates
    a weighted average of their actual hours based on similarity scores.

    Args:
        entity_manager: EntityManager for searching tasks.
        task_id: Existing task ID to estimate (will fetch title/description).
        title: Task title for new task estimation.
        description: Task description for new task estimation.
        min_similarity: Minimum similarity threshold (0-1, default 0.5).
        max_samples: Maximum similar tasks to consider (default 10).

    Returns:
        TaskEstimate with predicted hours, confidence, and similar tasks.
    """
    log.info("estimate_task_effort", task_id=task_id, title=title)

    # Build search query from task info
    query = ""
    if task_id:
        try:
            task = await entity_manager.get(task_id)
            query = f"{task.name} {task.description or ''}"
        except Exception as e:
            log.warning("could_not_fetch_task", task_id=task_id, error=str(e))
            return TaskEstimate(
                estimated_hours=0,
                confidence=0,
                message=f"Could not fetch task: {e}",
            )
    elif title:
        query = f"{title} {description or ''}"
    else:
        return TaskEstimate(
            estimated_hours=0,
            confidence=0,
            message="Either task_id or title is required",
        )

    if not query.strip():
        return TaskEstimate(
            estimated_hours=0,
            confidence=0,
            message="No content to search for similar tasks",
        )

    try:
        # Search for similar tasks
        results = await entity_manager.search(
            query=query.strip(),
            entity_types=[EntityType.TASK],
            limit=max_samples * 3,  # Over-fetch to filter
        )

        # Filter to completed tasks with actual_hours
        similar_tasks: list[SimilarTask] = []
        for entity, score in results:
            # Skip the task itself
            if task_id and entity.id == task_id:
                continue

            # Skip if below similarity threshold
            if score < min_similarity:
                continue

            # Check if task is done and has actual_hours
            metadata = entity.metadata or {}
            status = metadata.get("status")
            actual_hours = metadata.get("actual_hours")

            if status not in (TaskStatus.DONE.value, TaskStatus.ARCHIVED.value):
                continue

            if actual_hours is None or not isinstance(actual_hours, (int, float)):
                continue

            if actual_hours <= 0:
                continue

            similar_tasks.append(
                SimilarTask(
                    task_id=entity.id,
                    title=entity.name,
                    similarity_score=score,
                    actual_hours=float(actual_hours),
                )
            )

            if len(similar_tasks) >= max_samples:
                break

        if not similar_tasks:
            return TaskEstimate(
                estimated_hours=0,
                confidence=0,
                sample_count=0,
                message="No similar completed tasks with time data found",
            )

        # Calculate weighted average
        total_weight = sum(t.similarity_score for t in similar_tasks)
        weighted_hours = sum(t.actual_hours * t.similarity_score for t in similar_tasks)
        estimated_hours = weighted_hours / total_weight if total_weight > 0 else 0

        # Calculate confidence based on sample count and average similarity
        avg_similarity = total_weight / len(similar_tasks)
        sample_factor = min(len(similar_tasks) / 5, 1.0)  # Full confidence at 5+ samples
        confidence = avg_similarity * sample_factor

        # Round to reasonable precision
        estimated_hours = round(estimated_hours, 1)
        confidence = round(confidence, 2)

        log.info(
            "estimation_complete",
            estimated_hours=estimated_hours,
            confidence=confidence,
            sample_count=len(similar_tasks),
        )

        return TaskEstimate(
            estimated_hours=estimated_hours,
            confidence=confidence,
            similar_tasks=similar_tasks,
            sample_count=len(similar_tasks),
            message=f"Estimated from {len(similar_tasks)} similar task(s)",
        )

    except Exception as e:
        log.warning("estimation_failed", error=str(e))
        return TaskEstimate(
            estimated_hours=0,
            confidence=0,
            message=f"Estimation failed: {e}",
        )


async def batch_estimate(
    entity_manager: "EntityManager",
    task_ids: list[str],
    min_similarity: float = 0.5,
) -> dict[str, TaskEstimate]:
    """Estimate effort for multiple tasks.

    Args:
        entity_manager: EntityManager for searching tasks.
        task_ids: List of task IDs to estimate.
        min_similarity: Minimum similarity threshold.

    Returns:
        Dict mapping task_id to TaskEstimate.
    """
    results: dict[str, TaskEstimate] = {}

    for task_id in task_ids:
        estimate = await estimate_task_effort(
            entity_manager=entity_manager,
            task_id=task_id,
            min_similarity=min_similarity,
        )
        results[task_id] = estimate

    return results


def calculate_project_estimate(estimates: list[TaskEstimate]) -> TaskEstimate:
    """Aggregate task estimates into a project estimate.

    Args:
        estimates: List of individual task estimates.

    Returns:
        Combined TaskEstimate for the project.
    """
    if not estimates:
        return TaskEstimate(
            estimated_hours=0,
            confidence=0,
            message="No task estimates provided",
        )

    # Sum hours, use weighted average for confidence
    total_hours = sum(e.estimated_hours for e in estimates)
    total_confidence = sum(e.confidence * e.estimated_hours for e in estimates)
    avg_confidence = total_confidence / total_hours if total_hours > 0 else 0

    return TaskEstimate(
        estimated_hours=round(total_hours, 1),
        confidence=round(avg_confidence, 2),
        sample_count=sum(e.sample_count for e in estimates),
        message=f"Aggregated from {len(estimates)} task estimate(s)",
    )
