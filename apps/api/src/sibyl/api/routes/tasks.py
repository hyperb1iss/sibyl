"""Task workflow endpoints.

Dedicated endpoints for task lifecycle operations with proper event broadcasting.
"""

import uuid
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from sibyl.api.decorators import handle_workflow_errors
from sibyl.api.websocket import broadcast_event
from sibyl.auth.authorization import ProjectRole, verify_entity_project_access
from sibyl.auth.context import AuthContext
from sibyl.auth.dependencies import (
    get_current_organization,
    get_current_user,
    require_org_role,
)
from sibyl.auth.rls import AuthSession, get_auth_session
from sibyl.db.models import Organization, OrganizationRole, User
from sibyl_core.graph.client import get_graph_client
from sibyl_core.graph.entities import EntityManager
from sibyl_core.graph.relationships import RelationshipManager
from sibyl_core.models.tasks import AuthorType, Note, TaskComplexity, TaskPriority, TaskStatus
from sibyl_core.tasks.workflow import TaskWorkflowEngine

log = structlog.get_logger()
_WRITE_ROLES = (
    OrganizationRole.OWNER,
    OrganizationRole.ADMIN,
    OrganizationRole.MEMBER,
)


async def _verify_task_access(
    task_id: str,
    org: Organization,
    ctx: AuthContext,
    session: AsyncSession,
    required_role: ProjectRole = ProjectRole.CONTRIBUTOR,
) -> None:
    """Fetch a task and verify project access.

    Raises ProjectAuthorizationError if user lacks required access.
    """
    client = await get_graph_client()
    entity_manager = EntityManager(client, group_id=str(org.id))
    entity = await entity_manager.get(task_id)
    if not entity:
        raise HTTPException(status_code=404, detail=f"Task not found: {task_id}")

    # Extract project_id from entity metadata
    project_id = entity.metadata.get("project_id") if entity.metadata else None
    await verify_entity_project_access(session, ctx, project_id, required_role=required_role)


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

    status: TaskStatus | None = None
    priority: TaskPriority | None = None
    complexity: TaskComplexity | None = None
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
    priority: TaskPriority = TaskPriority.MEDIUM
    complexity: TaskComplexity = TaskComplexity.MEDIUM
    status: TaskStatus = TaskStatus.TODO
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
    user: User = Depends(get_current_user),
    auth: AuthSession = Depends(get_auth_session),
) -> TaskActionResponse:
    """Create a new task."""
    from sibyl_core.models.entities import Relationship, RelationshipType
    from sibyl_core.models.tasks import Task, TaskComplexity, TaskPriority, TaskStatus

    # Verify user has write access to the target project
    # auth.session has RLS context set for tenant isolation
    await verify_entity_project_access(
        auth.session, auth.ctx, request.project_id, required_role=ProjectRole.CONTRIBUTOR
    )

    try:
        client = await get_graph_client()
        entity_manager = EntityManager(client, group_id=str(org.id))
        relationship_manager = RelationshipManager(client, group_id=str(org.id))

        # Create task entity with actor attribution
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
            created_by=str(user.id),
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
@handle_workflow_errors("start_task")
async def start_task(
    task_id: str,
    org: Organization = Depends(get_current_organization),
    auth: AuthSession = Depends(get_auth_session),
    request: StartTaskRequest | None = None,
) -> TaskActionResponse:
    """Start working on a task (moves to 'doing' status)."""
    # Verify project access before modifying
    # auth.session has RLS context set for tenant isolation
    await _verify_task_access(task_id, org, auth.ctx, auth.session)

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


@router.post("/{task_id}/block", response_model=TaskActionResponse)
@handle_workflow_errors("block_task")
async def block_task(
    task_id: str,
    request: BlockTaskRequest,
    org: Organization = Depends(get_current_organization),
    auth: AuthSession = Depends(get_auth_session),
) -> TaskActionResponse:
    """Mark a task as blocked with a reason."""
    await _verify_task_access(task_id, org, auth.ctx, auth.session)

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


@router.post("/{task_id}/unblock", response_model=TaskActionResponse)
@handle_workflow_errors("unblock_task")
async def unblock_task(
    task_id: str,
    org: Organization = Depends(get_current_organization),
    auth: AuthSession = Depends(get_auth_session),
) -> TaskActionResponse:
    """Resume a blocked task (moves back to 'doing')."""
    await _verify_task_access(task_id, org, auth.ctx, auth.session)

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


@router.post("/{task_id}/review", response_model=TaskActionResponse)
@handle_workflow_errors("submit_review")
async def submit_review(
    task_id: str,
    org: Organization = Depends(get_current_organization),
    auth: AuthSession = Depends(get_auth_session),
    request: ReviewTaskRequest | None = None,
) -> TaskActionResponse:
    """Submit a task for review."""
    await _verify_task_access(task_id, org, auth.ctx, auth.session)

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


@router.post("/{task_id}/complete", response_model=TaskActionResponse)
@handle_workflow_errors("complete_task")
async def complete_task(
    task_id: str,
    org: Organization = Depends(get_current_organization),
    auth: AuthSession = Depends(get_auth_session),
    request: CompleteTaskRequest | None = None,
) -> TaskActionResponse:
    """Complete a task and optionally capture learnings."""
    from sibyl.jobs.queue import enqueue_create_learning_episode

    await _verify_task_access(task_id, org, auth.ctx, auth.session)

    group_id = str(org.id)
    client = await get_graph_client()
    entity_manager = EntityManager(client, group_id=group_id)
    relationship_manager = RelationshipManager(client, group_id=group_id)
    workflow = TaskWorkflowEngine(entity_manager, relationship_manager, client, group_id)

    actual_hours = request.actual_hours if request else None
    learnings = request.learnings if request else None

    # Skip sync episode creation - we'll enqueue it as a background job
    task = await workflow.complete_task(
        task_id, actual_hours, learnings or "", create_episode=False
    )

    # Enqueue learning episode creation as background job (fast response)
    if learnings:
        await enqueue_create_learning_episode(
            task.model_dump(mode="json"),
            group_id,
        )

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


@router.post("/{task_id}/archive", response_model=TaskActionResponse)
@handle_workflow_errors("archive_task")
async def archive_task(
    task_id: str,
    org: Organization = Depends(get_current_organization),
    auth: AuthSession = Depends(get_auth_session),
    request: ArchiveTaskRequest | None = None,
) -> TaskActionResponse:
    """Archive a task (terminal state)."""
    await _verify_task_access(task_id, org, auth.ctx, auth.session)

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


@router.patch("/{task_id}", response_model=TaskActionResponse)
async def update_task(
    task_id: str,
    request: UpdateTaskRequest,
    org: Organization = Depends(get_current_organization),
    user: User = Depends(get_current_user),
    auth: AuthSession = Depends(get_auth_session),
) -> TaskActionResponse:
    """Update task fields directly."""
    from sibyl.locks import LockAcquisitionError, entity_lock
    from sibyl_core.models.entities import Relationship, RelationshipType

    await _verify_task_access(task_id, org, auth.ctx, auth.session)

    group_id = str(org.id)

    try:
        # Acquire distributed lock to prevent concurrent updates
        async with entity_lock(group_id, task_id, blocking=True) as lock_token:
            if not lock_token:
                raise HTTPException(
                    status_code=409,
                    detail="Task is being updated by another process. Please retry.",
                )

            client = await get_graph_client()
            entity_manager = EntityManager(client, group_id=group_id)

            # Get existing task
            existing = await entity_manager.get(task_id)
            if not existing:
                raise HTTPException(status_code=404, detail=f"Task not found: {task_id}")

            # Build update dict with actor attribution
            update_data: dict[str, Any] = {"modified_by": str(user.id)}
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

            # Create BELONGS_TO relationship for epic (if epic_id was updated)
            if request.epic_id is not None:
                relationship_manager = RelationshipManager(client, group_id=group_id)
                belongs_to_epic = Relationship(
                    id=f"rel_{task_id}_belongs_to_{request.epic_id}",
                    source_id=task_id,
                    target_id=request.epic_id,
                    relationship_type=RelationshipType.BELONGS_TO,
                )
                await relationship_manager.create(belongs_to_epic)

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

    except LockAcquisitionError as e:
        raise HTTPException(
            status_code=409,
            detail="Task is locked by another process. Please retry.",
        ) from e
    except HTTPException:
        raise
    except Exception as e:
        log.exception("update_task_failed", task_id=task_id, error=str(e))
        raise HTTPException(
            status_code=500, detail="Failed to update task. Please try again."
        ) from e


# =============================================================================
# Task Notes
# =============================================================================


class CreateNoteRequest(BaseModel):
    """Request to create a note on a task."""

    content: str
    author_type: AuthorType = AuthorType.USER
    author_name: str = ""


class NoteResponse(BaseModel):
    """Response for a single note."""

    id: str
    task_id: str
    content: str
    author_type: str
    author_name: str
    created_at: str
    status: str | None = None  # None = created, "pending" = queued for async creation


class NotesListResponse(BaseModel):
    """Response for listing notes."""

    notes: list[NoteResponse]
    count: int


@router.post("/{task_id}/notes", response_model=NoteResponse)
async def create_note(
    task_id: str,
    request: CreateNoteRequest,
    org: Organization = Depends(get_current_organization),
    user: User = Depends(get_current_user),
    auth: AuthSession = Depends(get_auth_session),
) -> NoteResponse:
    """Create a note on a task.

    If the task is still being created asynchronously, the note will be queued
    and processed when the task materializes. The response will have status="pending".
    """
    from datetime import UTC, datetime

    from sibyl.jobs.pending import is_pending, queue_pending_operation
    from sibyl_core.models.entities import Relationship, RelationshipType

    await _verify_task_access(task_id, org, auth.ctx, auth.session)

    try:
        group_id = str(org.id)
        note_id = f"note_{uuid.uuid4()}"
        created_at = datetime.now(UTC)

        # Check if task is still being created asynchronously
        pending = await is_pending(task_id)
        if pending:
            # Queue the note operation to run when task materializes
            op_id = await queue_pending_operation(
                entity_id=task_id,
                operation="add_note",
                payload={
                    "note_id": note_id,
                    "content": request.content,
                    "author_type": request.author_type.value,
                    "author_name": request.author_name,
                    "created_at": created_at.isoformat(),
                    "user_id": str(user.id),
                },
                user_id=str(user.id),
            )

            log.info(
                "create_note_queued",
                note_id=note_id,
                task_id=task_id,
                op_id=op_id,
            )

            await broadcast_event(
                "note_pending",
                {"id": note_id, "task_id": task_id, "op_id": op_id},
                org_id=group_id,
            )

            return NoteResponse(
                id=note_id,
                task_id=task_id,
                content=request.content,
                author_type=request.author_type.value,
                author_name=request.author_name,
                created_at=created_at.isoformat(),
                status="pending",
            )

        # Task exists - create note synchronously
        client = await get_graph_client()
        entity_manager = EntityManager(client, group_id=group_id)
        relationship_manager = RelationshipManager(client, group_id=group_id)

        # Verify task exists in graph
        task = await entity_manager.get(task_id)
        if not task:
            raise HTTPException(status_code=404, detail=f"Task not found: {task_id}")

        note = Note(  # type: ignore[call-arg]  # model_validator sets name from content
            id=note_id,
            task_id=task_id,
            content=request.content,
            author_type=request.author_type,
            author_name=request.author_name,
            created_at=created_at,
            created_by=str(user.id),
        )

        # Create in graph
        await entity_manager.create_direct(note)

        # Create BELONGS_TO relationship with task
        belongs_to = Relationship(
            id=f"rel_{note_id}_belongs_to_{task_id}",
            source_id=note_id,
            target_id=task_id,
            relationship_type=RelationshipType.BELONGS_TO,
        )
        await relationship_manager.create(belongs_to)

        log.info(
            "create_note_success",
            note_id=note_id,
            task_id=task_id,
            author_type=request.author_type,
        )

        await broadcast_event(
            "note_created",
            {"id": note_id, "task_id": task_id, "author_type": request.author_type.value},
            org_id=group_id,
        )

        return NoteResponse(
            id=note_id,
            task_id=task_id,
            content=request.content,
            author_type=request.author_type.value,
            author_name=request.author_name,
            created_at=created_at.isoformat(),
        )

    except HTTPException:
        raise
    except Exception as e:
        log.exception("create_note_failed", task_id=task_id, error=str(e))
        raise HTTPException(
            status_code=500, detail="Failed to create note. Please try again."
        ) from e


@router.get("/{task_id}/notes", response_model=NotesListResponse)
async def list_notes(
    task_id: str,
    limit: int = 50,
    org: Organization = Depends(get_current_organization),
    auth: AuthSession = Depends(get_auth_session),
) -> NotesListResponse:
    """List all notes for a task."""
    # Read access is sufficient for listing notes
    await _verify_task_access(
        task_id, org, auth.ctx, auth.session, required_role=ProjectRole.VIEWER
    )

    try:
        group_id = str(org.id)
        client = await get_graph_client()
        entity_manager = EntityManager(client, group_id=group_id)

        # Verify task exists
        task = await entity_manager.get(task_id)
        if not task:
            raise HTTPException(status_code=404, detail=f"Task not found: {task_id}")

        # Get notes for task
        notes_entities = await entity_manager.get_notes_for_task(task_id, limit=limit)

        notes = []
        for entity in notes_entities:
            metadata = entity.metadata or {}
            notes.append(
                NoteResponse(
                    id=entity.id,
                    task_id=metadata.get("task_id", task_id),
                    content=entity.content,
                    author_type=metadata.get("author_type", "user"),
                    author_name=metadata.get("author_name", ""),
                    created_at=entity.created_at.isoformat() if entity.created_at else "",
                )
            )

        return NotesListResponse(notes=notes, count=len(notes))

    except HTTPException:
        raise
    except Exception as e:
        log.exception("list_notes_failed", task_id=task_id, error=str(e))
        raise HTTPException(
            status_code=500, detail="Failed to list notes. Please try again."
        ) from e
