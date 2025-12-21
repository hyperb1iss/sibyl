"""Task workflow endpoints.

Dedicated endpoints for task lifecycle operations with proper event broadcasting.
"""

from typing import Any

import structlog
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from sibyl.api.websocket import broadcast_event
from sibyl.errors import InvalidTransitionError
from sibyl.graph.client import get_graph_client
from sibyl.graph.entities import EntityManager
from sibyl.graph.relationships import RelationshipManager
from sibyl.tasks.workflow import TaskWorkflowEngine

log = structlog.get_logger()
router = APIRouter(prefix="/tasks", tags=["tasks"])


# =============================================================================
# Request/Response Models
# =============================================================================


class TaskActionResponse(BaseModel):
    """Response from task workflow action."""

    success: bool
    action: str
    task_id: str
    message: str
    data: dict[str, Any] = {}


class StartTaskRequest(BaseModel):
    """Request to start a task."""

    assignee: str | None = None


class BlockTaskRequest(BaseModel):
    """Request to block a task."""

    reason: str


class ReviewTaskRequest(BaseModel):
    """Request to submit task for review."""

    pr_url: str | None = None
    commit_shas: list[str] = []


class CompleteTaskRequest(BaseModel):
    """Request to complete a task."""

    actual_hours: float | None = None
    learnings: str | None = None


class ArchiveTaskRequest(BaseModel):
    """Request to archive a task."""

    reason: str | None = None


class UpdateTaskRequest(BaseModel):
    """Request to update task fields."""

    status: str | None = None
    priority: str | None = None
    title: str | None = None
    description: str | None = None
    assignees: list[str] | None = None
    feature: str | None = None


# =============================================================================
# Workflow Endpoints
# =============================================================================


async def _broadcast_task_update(task_id: str, action: str, data: dict[str, Any]) -> None:
    """Broadcast task update event."""
    await broadcast_event(
        "entity_updated",
        {
            "id": task_id,
            "entity_type": "task",
            "action": action,
            **data,
        },
    )


@router.post("/{task_id}/start", response_model=TaskActionResponse)
async def start_task(task_id: str, request: StartTaskRequest | None = None) -> TaskActionResponse:
    """Start working on a task (moves to 'doing' status)."""
    try:
        client = await get_graph_client()
        entity_manager = EntityManager(client)
        relationship_manager = RelationshipManager(client)
        workflow = TaskWorkflowEngine(entity_manager, relationship_manager, client)

        assignee = request.assignee if request else None
        task = await workflow.start_task(task_id, assignee or "system")

        await _broadcast_task_update(
            task_id,
            "start_task",
            {"status": task.status.value, "branch_name": task.branch_name, "name": task.name},
        )

        return TaskActionResponse(
            success=True,
            action="start_task",
            task_id=task_id,
            message="Task started",
            data={"status": task.status.value, "branch_name": task.branch_name},
        )

    except InvalidTransitionError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        log.exception("start_task_failed", task_id=task_id, error=str(e))
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/{task_id}/block", response_model=TaskActionResponse)
async def block_task(task_id: str, request: BlockTaskRequest) -> TaskActionResponse:
    """Mark a task as blocked with a reason."""
    try:
        client = await get_graph_client()
        entity_manager = EntityManager(client)
        relationship_manager = RelationshipManager(client)
        workflow = TaskWorkflowEngine(entity_manager, relationship_manager, client)

        task = await workflow.block_task(task_id, request.reason)

        await _broadcast_task_update(
            task_id,
            "block_task",
            {"status": task.status.value, "blocker": request.reason, "name": task.name},
        )

        return TaskActionResponse(
            success=True,
            action="block_task",
            task_id=task_id,
            message=f"Task blocked: {request.reason}",
            data={"status": task.status.value, "reason": request.reason},
        )

    except InvalidTransitionError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        log.exception("block_task_failed", task_id=task_id, error=str(e))
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/{task_id}/unblock", response_model=TaskActionResponse)
async def unblock_task(task_id: str) -> TaskActionResponse:
    """Resume a blocked task (moves back to 'doing')."""
    try:
        client = await get_graph_client()
        entity_manager = EntityManager(client)
        relationship_manager = RelationshipManager(client)
        workflow = TaskWorkflowEngine(entity_manager, relationship_manager, client)

        task = await workflow.unblock_task(task_id)

        await _broadcast_task_update(
            task_id,
            "unblock_task",
            {"status": task.status.value, "name": task.name},
        )

        return TaskActionResponse(
            success=True,
            action="unblock_task",
            task_id=task_id,
            message="Task unblocked, resuming work",
            data={"status": task.status.value},
        )

    except InvalidTransitionError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        log.exception("unblock_task_failed", task_id=task_id, error=str(e))
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/{task_id}/review", response_model=TaskActionResponse)
async def submit_review(
    task_id: str, request: ReviewTaskRequest | None = None
) -> TaskActionResponse:
    """Submit a task for review."""
    try:
        client = await get_graph_client()
        entity_manager = EntityManager(client)
        relationship_manager = RelationshipManager(client)
        workflow = TaskWorkflowEngine(entity_manager, relationship_manager, client)

        pr_url = request.pr_url if request else None
        commit_shas = request.commit_shas if request else []
        task = await workflow.submit_for_review(task_id, commit_shas, pr_url)

        await _broadcast_task_update(
            task_id,
            "submit_review",
            {"status": task.status.value, "pr_url": task.pr_url, "name": task.name},
        )

        return TaskActionResponse(
            success=True,
            action="submit_review",
            task_id=task_id,
            message="Task submitted for review",
            data={"status": task.status.value, "pr_url": task.pr_url},
        )

    except InvalidTransitionError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        log.exception("submit_review_failed", task_id=task_id, error=str(e))
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/{task_id}/complete", response_model=TaskActionResponse)
async def complete_task(
    task_id: str, request: CompleteTaskRequest | None = None
) -> TaskActionResponse:
    """Complete a task and optionally capture learnings."""
    try:
        client = await get_graph_client()
        entity_manager = EntityManager(client)
        relationship_manager = RelationshipManager(client)
        workflow = TaskWorkflowEngine(entity_manager, relationship_manager, client)

        actual_hours = request.actual_hours if request else None
        learnings = request.learnings if request else None
        task = await workflow.complete_task(task_id, actual_hours, learnings)

        await _broadcast_task_update(
            task_id,
            "complete_task",
            {"status": task.status.value, "learnings": learnings, "name": task.name},
        )

        return TaskActionResponse(
            success=True,
            action="complete_task",
            task_id=task_id,
            message="Task completed" + (" with learnings captured" if learnings else ""),
            data={"status": task.status.value, "learnings": learnings},
        )

    except InvalidTransitionError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        log.exception("complete_task_failed", task_id=task_id, error=str(e))
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/{task_id}/archive", response_model=TaskActionResponse)
async def archive_task(
    task_id: str, request: ArchiveTaskRequest | None = None
) -> TaskActionResponse:
    """Archive a task (terminal state)."""
    try:
        client = await get_graph_client()
        entity_manager = EntityManager(client)
        relationship_manager = RelationshipManager(client)
        workflow = TaskWorkflowEngine(entity_manager, relationship_manager, client)

        reason = request.reason if request else ""
        task = await workflow.archive_task(task_id, reason)

        await _broadcast_task_update(
            task_id,
            "archive_task",
            {"status": task.status.value, "name": task.name},
        )

        return TaskActionResponse(
            success=True,
            action="archive_task",
            task_id=task_id,
            message="Task archived",
            data={"status": task.status.value},
        )

    except InvalidTransitionError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        log.exception("archive_task_failed", task_id=task_id, error=str(e))
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.patch("/{task_id}", response_model=TaskActionResponse)
async def update_task(task_id: str, request: UpdateTaskRequest) -> TaskActionResponse:
    """Update task fields directly."""
    try:
        client = await get_graph_client()
        entity_manager = EntityManager(client)

        # Get existing task
        existing = await entity_manager.get(task_id)
        if not existing:
            raise HTTPException(status_code=404, detail=f"Task not found: {task_id}")

        # Build update dict
        update_data: dict[str, Any] = {}
        if request.status is not None:
            update_data["status"] = request.status
        if request.priority is not None:
            update_data["priority"] = request.priority
        if request.title is not None:
            update_data["name"] = request.title
        if request.description is not None:
            update_data["description"] = request.description
        if request.assignees is not None:
            update_data["assignees"] = request.assignees
        if request.feature is not None:
            update_data["feature"] = request.feature

        if not update_data:
            raise HTTPException(status_code=400, detail="No fields to update")

        # Update in graph
        updated = await entity_manager.update(task_id, update_data)
        if not updated:
            raise HTTPException(status_code=500, detail="Update failed")

        await _broadcast_task_update(
            task_id,
            "update_task",
            {"name": updated.name, **update_data},
        )

        return TaskActionResponse(
            success=True,
            action="update_task",
            task_id=task_id,
            message=f"Task updated: {', '.join(update_data.keys())}",
            data=update_data,
        )

    except HTTPException:
        raise
    except Exception as e:
        log.exception("update_task_failed", task_id=task_id, error=str(e))
        raise HTTPException(status_code=500, detail=str(e)) from e
