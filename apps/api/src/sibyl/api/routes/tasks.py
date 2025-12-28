"""Task workflow endpoints.

Dedicated endpoints for task lifecycle operations with proper event broadcasting.
"""

import uuid
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from sibyl.api.websocket import broadcast_event
from sibyl.auth.dependencies import get_current_organization, require_org_role
from sibyl.db.models import Organization, OrganizationRole
from sibyl.errors import EntityNotFoundError, InvalidTransitionError
from sibyl.graph.client import get_graph_client
from sibyl.graph.entities import EntityManager
from sibyl.graph.relationships import RelationshipManager
from sibyl.tasks.workflow import TaskWorkflowEngine

log = structlog.get_logger()
_WRITE_ROLES = (
    OrganizationRole.OWNER,
    OrganizationRole.ADMIN,
    OrganizationRole.MEMBER,
)

router = APIRouter(
    prefix="/tasks",
    tags=["tasks"],
    dependencies=[Depends(require_org_role(*_WRITE_ROLES))],
)


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
    complexity: str | None = None
    title: str | None = None
    description: str | None = None
    assignees: list[str] | None = None
    epic_id: str | None = None
    feature: str | None = None
    tags: list[str] | None = None
    technologies: list[str] | None = None


class CreateTaskRequest(BaseModel):
    """Request to create a new task."""

    title: str
    description: str | None = None
    project_id: str
    priority: str = "medium"
    complexity: str = "medium"
    status: str = "todo"
    assignees: list[str] = []
    epic_id: str | None = None
    feature: str | None = None
    tags: list[str] = []
    technologies: list[str] = []
    depends_on: list[str] = []


# =============================================================================
# Task CRUD
# =============================================================================


@router.post("", response_model=TaskActionResponse)
async def create_task(
    request: CreateTaskRequest,
    org: Organization = Depends(get_current_organization),
) -> TaskActionResponse:
    """Create a new task."""
    from sibyl.models.entities import Relationship, RelationshipType
    from sibyl.models.tasks import Task, TaskComplexity, TaskPriority, TaskStatus

    try:
        client = await get_graph_client()
        entity_manager = EntityManager(client, group_id=str(org.id))
        relationship_manager = RelationshipManager(client, group_id=str(org.id))

        # Create task entity
        task = Task(  # type: ignore[call-arg]  # model_validator sets name from title
            id=str(uuid.uuid4()),
            title=request.title,
            description=request.description or "",
            status=TaskStatus(request.status),
            priority=TaskPriority(request.priority),
            complexity=TaskComplexity(request.complexity),
            project_id=request.project_id,
            epic_id=request.epic_id,
            assignees=request.assignees,
            feature=request.feature,
            tags=request.tags,
            technologies=request.technologies,
        )

        # Create in graph
        task_id = await entity_manager.create_direct(task)

        # Create BELONGS_TO relationship with project
        belongs_to = Relationship(
            id=f"rel_{task_id}_belongs_to_{request.project_id}",
            source_id=task_id,
            target_id=request.project_id,
            relationship_type=RelationshipType.BELONGS_TO,
        )
        await relationship_manager.create(belongs_to)

        # Create BELONGS_TO relationship with epic (if provided)
        if request.epic_id:
            belongs_to_epic = Relationship(
                id=f"rel_{task_id}_belongs_to_{request.epic_id}",
                source_id=task_id,
                target_id=request.epic_id,
                relationship_type=RelationshipType.BELONGS_TO,
            )
            await relationship_manager.create(belongs_to_epic)

        # Create DEPENDS_ON relationships
        for dep_id in request.depends_on:
            depends_on = Relationship(
                id=f"rel_{task_id}_depends_on_{dep_id}",
                source_id=task_id,
                target_id=dep_id,
                relationship_type=RelationshipType.DEPENDS_ON,
            )
            await relationship_manager.create(depends_on)

        log.info(
            "create_task_success",
            task_id=task_id,
            project_id=request.project_id,
        )

        await broadcast_event(
            "entity_created",
            {"id": task_id, "entity_type": "task", "name": request.title},
            org_id=str(org.id),
        )

        return TaskActionResponse(
            success=True,
            action="create",
            task_id=task_id,
            message="Task created successfully",
            data={"project_id": request.project_id},
        )

    except Exception as e:
        log.exception("create_task_failed", error=str(e))
        raise HTTPException(
            status_code=500, detail="Failed to create task. Please try again."
        ) from e


# =============================================================================
# Workflow Endpoints
# =============================================================================


async def _broadcast_task_update(
    task_id: str, action: str, data: dict[str, Any], *, org_id: str | None = None
) -> None:
    """Broadcast task update event (scoped to org)."""
    await broadcast_event(
        "entity_updated",
        {
            "id": task_id,
            "entity_type": "task",
            "action": action,
            **data,
        },
        org_id=org_id,
    )


@router.post("/{task_id}/start", response_model=TaskActionResponse)
async def start_task(
    task_id: str,
    org: Organization = Depends(get_current_organization),
    request: StartTaskRequest | None = None,
) -> TaskActionResponse:
    """Start working on a task (moves to 'doing' status)."""
    try:
        group_id = str(org.id)
        client = await get_graph_client()
        entity_manager = EntityManager(client, group_id=group_id)
        relationship_manager = RelationshipManager(client, group_id=group_id)
        workflow = TaskWorkflowEngine(entity_manager, relationship_manager, client, group_id)

        assignee = request.assignee if request else None
        task = await workflow.start_task(task_id, assignee or "system")

        await _broadcast_task_update(
            task_id,
            "start_task",
            {"status": task.status.value, "branch_name": task.branch_name, "name": task.name},
            org_id=group_id,
        )

        return TaskActionResponse(
            success=True,
            action="start_task",
            task_id=task_id,
            message="Task started",
            data={"status": task.status.value, "branch_name": task.branch_name},
        )

    except EntityNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except InvalidTransitionError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        log.exception("start_task_failed", task_id=task_id, error=str(e))
        raise HTTPException(
            status_code=500, detail="Failed to start task. Please try again."
        ) from e


@router.post("/{task_id}/block", response_model=TaskActionResponse)
async def block_task(
    task_id: str,
    request: BlockTaskRequest,
    org: Organization = Depends(get_current_organization),
) -> TaskActionResponse:
    """Mark a task as blocked with a reason."""
    try:
        group_id = str(org.id)
        client = await get_graph_client()
        entity_manager = EntityManager(client, group_id=group_id)
        relationship_manager = RelationshipManager(client, group_id=group_id)
        workflow = TaskWorkflowEngine(entity_manager, relationship_manager, client, group_id)

        task = await workflow.block_task(task_id, request.reason)

        await _broadcast_task_update(
            task_id,
            "block_task",
            {"status": task.status.value, "blocker": request.reason, "name": task.name},
            org_id=group_id,
        )

        return TaskActionResponse(
            success=True,
            action="block_task",
            task_id=task_id,
            message=f"Task blocked: {request.reason}",
            data={"status": task.status.value, "reason": request.reason},
        )

    except EntityNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except InvalidTransitionError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        log.exception("block_task_failed", task_id=task_id, error=str(e))
        raise HTTPException(
            status_code=500, detail="Failed to block task. Please try again."
        ) from e


@router.post("/{task_id}/unblock", response_model=TaskActionResponse)
async def unblock_task(
    task_id: str,
    org: Organization = Depends(get_current_organization),
) -> TaskActionResponse:
    """Resume a blocked task (moves back to 'doing')."""
    try:
        group_id = str(org.id)
        client = await get_graph_client()
        entity_manager = EntityManager(client, group_id=group_id)
        relationship_manager = RelationshipManager(client, group_id=group_id)
        workflow = TaskWorkflowEngine(entity_manager, relationship_manager, client, group_id)

        task = await workflow.unblock_task(task_id)

        await _broadcast_task_update(
            task_id,
            "unblock_task",
            {"status": task.status.value, "name": task.name},
            org_id=group_id,
        )

        return TaskActionResponse(
            success=True,
            action="unblock_task",
            task_id=task_id,
            message="Task unblocked, resuming work",
            data={"status": task.status.value},
        )

    except EntityNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except InvalidTransitionError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        log.exception("unblock_task_failed", task_id=task_id, error=str(e))
        raise HTTPException(
            status_code=500, detail="Failed to unblock task. Please try again."
        ) from e


@router.post("/{task_id}/review", response_model=TaskActionResponse)
async def submit_review(
    task_id: str,
    request: ReviewTaskRequest | None = None,
    org: Organization = Depends(get_current_organization),
) -> TaskActionResponse:
    """Submit a task for review."""
    try:
        group_id = str(org.id)
        client = await get_graph_client()
        entity_manager = EntityManager(client, group_id=group_id)
        relationship_manager = RelationshipManager(client, group_id=group_id)
        workflow = TaskWorkflowEngine(entity_manager, relationship_manager, client, group_id)

        pr_url = request.pr_url if request else None
        commit_shas = request.commit_shas if request else []
        task = await workflow.submit_for_review(task_id, commit_shas, pr_url)

        await _broadcast_task_update(
            task_id,
            "submit_review",
            {"status": task.status.value, "pr_url": task.pr_url, "name": task.name},
            org_id=group_id,
        )

        return TaskActionResponse(
            success=True,
            action="submit_review",
            task_id=task_id,
            message="Task submitted for review",
            data={"status": task.status.value, "pr_url": task.pr_url},
        )

    except EntityNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except InvalidTransitionError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        log.exception("submit_review_failed", task_id=task_id, error=str(e))
        raise HTTPException(
            status_code=500, detail="Failed to submit review. Please try again."
        ) from e


@router.post("/{task_id}/complete", response_model=TaskActionResponse)
async def complete_task(
    task_id: str,
    request: CompleteTaskRequest | None = None,
    org: Organization = Depends(get_current_organization),
) -> TaskActionResponse:
    """Complete a task and optionally capture learnings."""
    try:
        group_id = str(org.id)
        client = await get_graph_client()
        entity_manager = EntityManager(client, group_id=group_id)
        relationship_manager = RelationshipManager(client, group_id=group_id)
        workflow = TaskWorkflowEngine(entity_manager, relationship_manager, client, group_id)

        actual_hours = request.actual_hours if request else None
        learnings = request.learnings if request else None
        task = await workflow.complete_task(task_id, actual_hours, learnings)

        await _broadcast_task_update(
            task_id,
            "complete_task",
            {"status": task.status.value, "learnings": learnings, "name": task.name},
            org_id=group_id,
        )

        return TaskActionResponse(
            success=True,
            action="complete_task",
            task_id=task_id,
            message="Task completed" + (" with learnings captured" if learnings else ""),
            data={"status": task.status.value, "learnings": learnings},
        )

    except EntityNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except InvalidTransitionError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        log.exception("complete_task_failed", task_id=task_id, error=str(e))
        raise HTTPException(
            status_code=500, detail="Failed to complete task. Please try again."
        ) from e


@router.post("/{task_id}/archive", response_model=TaskActionResponse)
async def archive_task(
    task_id: str,
    request: ArchiveTaskRequest | None = None,
    org: Organization = Depends(get_current_organization),
) -> TaskActionResponse:
    """Archive a task (terminal state)."""
    try:
        group_id = str(org.id)
        client = await get_graph_client()
        entity_manager = EntityManager(client, group_id=group_id)
        relationship_manager = RelationshipManager(client, group_id=group_id)
        workflow = TaskWorkflowEngine(entity_manager, relationship_manager, client, group_id)

        reason = request.reason if request else ""
        task = await workflow.archive_task(task_id, reason)

        await _broadcast_task_update(
            task_id,
            "archive_task",
            {"status": task.status.value, "name": task.name},
            org_id=group_id,
        )

        return TaskActionResponse(
            success=True,
            action="archive_task",
            task_id=task_id,
            message="Task archived",
            data={"status": task.status.value},
        )

    except EntityNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except InvalidTransitionError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        log.exception("archive_task_failed", task_id=task_id, error=str(e))
        raise HTTPException(
            status_code=500, detail="Failed to archive task. Please try again."
        ) from e


@router.patch("/{task_id}", response_model=TaskActionResponse)
async def update_task(
    task_id: str,
    request: UpdateTaskRequest,
    org: Organization = Depends(get_current_organization),
) -> TaskActionResponse:
    """Update task fields directly."""
    try:
        group_id = str(org.id)
        client = await get_graph_client()
        entity_manager = EntityManager(client, group_id=group_id)

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
        if request.epic_id is not None:
            update_data["epic_id"] = request.epic_id
        if request.feature is not None:
            update_data["feature"] = request.feature
        if request.complexity is not None:
            update_data["complexity"] = request.complexity
        if request.tags is not None:
            update_data["tags"] = request.tags
        if request.technologies is not None:
            update_data["technologies"] = request.technologies

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
            org_id=group_id,
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
        raise HTTPException(
            status_code=500, detail="Failed to update task. Please try again."
        ) from e
