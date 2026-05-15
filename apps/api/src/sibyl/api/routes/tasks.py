"""Task workflow endpoints.

Dedicated endpoints for task lifecycle operations with proper event broadcasting.
"""

import asyncio
import uuid
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from sibyl.api.decorators import handle_workflow_errors
from sibyl.api.event_types import WSEvent
from sibyl.api.websocket import broadcast_event
from sibyl.auth.authorization import verify_entity_project_access
from sibyl.auth.context import AuthContext
from sibyl.auth.dependencies import (
    get_auth_context,
    get_current_organization,
    get_current_user,
    require_org_role,
)
from sibyl.jobs.entities import serialize_memory_policy_context
from sibyl.locks import entity_lock
from sibyl.persistence.auth_runtime import list_accessible_project_graph_ids
from sibyl_core.auth import AuthOrganization, AuthUser, OrganizationRole, ProjectRole
from sibyl_core.models.tasks import AuthorType, Note, TaskComplexity, TaskPriority, TaskStatus
from sibyl_core.tasks.workflow import TaskWorkflowEngine
from sibyl_core.tools.helpers import _project_id_for_policy

log = structlog.get_logger()
_WRITE_ROLES = (
    OrganizationRole.OWNER,
    OrganizationRole.ADMIN,
    OrganizationRole.MEMBER,
)


async def get_knowledge_read_adapter(group_id: str):
    from sibyl.persistence.graph_runtime import get_knowledge_read_adapter as service

    return await service(group_id)


async def get_task_graph_runtime(group_id: str):
    from sibyl.persistence.graph_runtime import get_task_graph_runtime as service

    return await service(group_id)


async def _verify_task_access(
    task_id: str,
    org: AuthOrganization,
    ctx: AuthContext,
    required_role: ProjectRole = ProjectRole.CONTRIBUTOR,
) -> Any:
    """Fetch a task and verify project access.

    Raises ProjectAuthorizationError if user lacks required access.
    """
    service = await get_knowledge_read_adapter(str(org.id))
    entity = await service.get_entity(task_id)
    if not entity:
        raise HTTPException(status_code=404, detail=f"Task not found: {task_id}")

    project_id = _project_id_for_policy(entity)
    await verify_entity_project_access(
        None,
        ctx,
        project_id,
        required_role=required_role,
        require_existing_project=True,
    )
    return entity


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
    add_depends_on: list[str] = []
    remove_depends_on: list[str] = []


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
    org: AuthOrganization = Depends(get_current_organization),
    user: AuthUser = Depends(get_current_user),
    auth: AuthContext = Depends(get_auth_context),
) -> TaskActionResponse:
    """Create a new task."""
    from sibyl_core.models.entities import Relationship, RelationshipType
    from sibyl_core.models.tasks import Task, TaskComplexity, TaskPriority, TaskStatus

    await verify_entity_project_access(
        None,
        auth,
        request.project_id,
        required_role=ProjectRole.CONTRIBUTOR,
        require_existing_project=True,
    )

    try:
        runtime = await get_task_graph_runtime(str(org.id))

        if request.epic_id:
            await _verify_epic_exists(runtime.entity_manager, request.epic_id)

        # Create task entity with actor attribution
        task = Task(
            id=str(uuid.uuid4()),
            name=request.title,
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
        task_id = await runtime.entity_manager.create_direct(task)

        relationships = [
            Relationship(
                id=f"rel_{task_id}_belongs_to_{request.project_id}",
                source_id=task_id,
                target_id=request.project_id,
                relationship_type=RelationshipType.BELONGS_TO,
            )
        ]

        if request.epic_id:
            relationships.append(
                Relationship(
                    id=f"rel_{task_id}_belongs_to_{request.epic_id}",
                    source_id=task_id,
                    target_id=request.epic_id,
                    relationship_type=RelationshipType.BELONGS_TO,
                )
            )

        relationships.extend(
            Relationship(
                id=f"rel_{task_id}_depends_on_{dep_id}",
                source_id=task_id,
                target_id=dep_id,
                relationship_type=RelationshipType.DEPENDS_ON,
            )
            for dep_id in request.depends_on
        )

        await asyncio.gather(
            *(runtime.relationship_manager.create(relationship) for relationship in relationships)
        )

        log.info(
            "create_task_success",
            task_id=task_id,
            project_id=request.project_id,
        )

        await broadcast_event(
            WSEvent.ENTITY_CREATED,
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
        WSEvent.ENTITY_UPDATED,
        {
            "id": task_id,
            "entity_type": "task",
            "action": action,
            **data,
        },
        org_id=org_id,
    )


async def _verify_epic_exists(entity_manager: Any, epic_id: str) -> None:
    """Confirm an epic exists before linking a task to it.

    Prevents creating ghost-epic references. Raises HTTPException 404 if missing.
    """
    from sibyl_core.errors import EntityNotFoundError
    from sibyl_core.models.entities import EntityType

    try:
        epic = await entity_manager.get(epic_id)
    except EntityNotFoundError as exc:
        raise HTTPException(
            status_code=404,
            detail=f"Epic not found: {epic_id}",
        ) from exc
    if not epic or epic.entity_type != EntityType.EPIC:
        raise HTTPException(
            status_code=400,
            detail=f"Entity {epic_id} is not an epic",
        )


async def _maybe_start_epic(
    entity_manager: Any,
    task_id: str,
    epic_id: str | None,
    task_status: str,
) -> bool:
    """Auto-start epic if task moves to forward-progress state.

    Args:
        entity_manager: Entity manager for graph operations
        task_id: Task ID for logging
        epic_id: Epic ID to potentially start
        task_status: New task status

    Returns:
        True if epic was auto-started
    """
    from datetime import UTC, datetime

    from sibyl_core.errors import EntityNotFoundError
    from sibyl_core.models.tasks import EpicStatus

    forward_progress_states = {"doing", "review", "blocked"}
    if task_status not in forward_progress_states or not epic_id:
        return False

    try:
        epic = await entity_manager.get(epic_id)
    except EntityNotFoundError:
        log.warning(
            "Epic referenced by task no longer exists; skipping auto-start",
            epic_id=epic_id,
            task_id=task_id,
        )
        return False
    if not epic or epic.metadata.get("status") != "planning":
        return False

    await entity_manager.update(
        epic_id,
        {"status": EpicStatus.IN_PROGRESS, "started_at": datetime.now(UTC)},
    )
    log.info("Epic auto-started", epic_id=epic_id, task_id=task_id, task_status=task_status)
    return True


async def _get_task_workflow_runtime(group_id: str) -> tuple[Any, TaskWorkflowEngine]:
    runtime = await get_task_graph_runtime(group_id)
    workflow = TaskWorkflowEngine(
        runtime.entity_manager,
        runtime.relationship_manager,
        runtime.client,
        group_id,
    )
    return runtime, workflow


@router.post("/{task_id}/start", response_model=TaskActionResponse)
@handle_workflow_errors("start_task")
async def start_task(
    task_id: str,
    org: AuthOrganization = Depends(get_current_organization),
    auth: AuthContext = Depends(get_auth_context),
    request: StartTaskRequest | None = None,
) -> TaskActionResponse:
    """Start working on a task (moves to 'doing' status)."""
    await _verify_task_access(task_id, org, auth)

    group_id = str(org.id)

    async with entity_lock(group_id, task_id, blocking=True) as lock_token:
        if not lock_token:
            raise HTTPException(status_code=409, detail="Task is locked by another process")

        _, workflow = await _get_task_workflow_runtime(group_id)

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
    org: AuthOrganization = Depends(get_current_organization),
    auth: AuthContext = Depends(get_auth_context),
) -> TaskActionResponse:
    """Mark a task as blocked with a reason."""
    await _verify_task_access(task_id, org, auth)

    group_id = str(org.id)

    async with entity_lock(group_id, task_id, blocking=True) as lock_token:
        if not lock_token:
            raise HTTPException(status_code=409, detail="Task is locked by another process")

        _, workflow = await _get_task_workflow_runtime(group_id)

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
    org: AuthOrganization = Depends(get_current_organization),
    auth: AuthContext = Depends(get_auth_context),
) -> TaskActionResponse:
    """Resume a blocked task (moves back to 'doing')."""
    await _verify_task_access(task_id, org, auth)

    group_id = str(org.id)

    async with entity_lock(group_id, task_id, blocking=True) as lock_token:
        if not lock_token:
            raise HTTPException(status_code=409, detail="Task is locked by another process")

        _, workflow = await _get_task_workflow_runtime(group_id)

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
    org: AuthOrganization = Depends(get_current_organization),
    auth: AuthContext = Depends(get_auth_context),
    request: ReviewTaskRequest | None = None,
) -> TaskActionResponse:
    """Submit a task for review."""
    await _verify_task_access(task_id, org, auth)

    group_id = str(org.id)

    async with entity_lock(group_id, task_id, blocking=True) as lock_token:
        if not lock_token:
            raise HTTPException(status_code=409, detail="Task is locked by another process")

        _, workflow = await _get_task_workflow_runtime(group_id)

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
    org: AuthOrganization = Depends(get_current_organization),
    auth: AuthContext = Depends(get_auth_context),
    request: CompleteTaskRequest | None = None,
) -> TaskActionResponse:
    """Complete a task and optionally capture learnings."""
    from sibyl.jobs.queue import (
        enqueue_create_learning_episode,
        enqueue_create_learning_procedure,
    )

    verified_task = await _verify_task_access(task_id, org, auth)
    project_id = _project_id_for_policy(verified_task)
    accessible_projects = None
    if project_id:
        accessible_projects = {
            str(accessible_project_id)
            for accessible_project_id in (await list_accessible_project_graph_ids(auth) or set())
        }
    memory_policy_context = auth.to_memory_policy_context(
        memory_space="project" if project_id else "private",
        scope_key=project_id,
        project_id=project_id,
        accessible_projects=accessible_projects,
        source_surface="task_learning_job",
    )
    policy_payload = serialize_memory_policy_context(memory_policy_context)

    group_id = str(org.id)

    async with entity_lock(group_id, task_id, blocking=True) as lock_token:
        if not lock_token:
            raise HTTPException(status_code=409, detail="Task is locked by another process")

        _, workflow = await _get_task_workflow_runtime(group_id)

        actual_hours = request.actual_hours if request else None
        learnings = request.learnings if request else None

        # Skip sync episode creation - we'll enqueue it as a background job
        task = await workflow.complete_task(
            task_id, actual_hours, learnings or "", create_episode=False
        )

    # Enqueue learning episode creation as background job (fast response)
    if learnings:
        task_data = task.model_dump(mode="json")
        await asyncio.gather(
            enqueue_create_learning_episode(
                task_data,
                group_id,
                policy_context=policy_payload,
            ),
            enqueue_create_learning_procedure(
                task_data,
                group_id,
                policy_context=policy_payload,
            ),
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
    org: AuthOrganization = Depends(get_current_organization),
    auth: AuthContext = Depends(get_auth_context),
    request: ArchiveTaskRequest | None = None,
) -> TaskActionResponse:
    """Archive a task (terminal state)."""
    await _verify_task_access(task_id, org, auth)

    group_id = str(org.id)

    async with entity_lock(group_id, task_id, blocking=True) as lock_token:
        if not lock_token:
            raise HTTPException(status_code=409, detail="Task is locked by another process")

        _, workflow = await _get_task_workflow_runtime(group_id)

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


def _build_update_data(request: UpdateTaskRequest, user_id: str) -> dict[str, Any]:
    """Build the update dict from request fields with actor attribution."""
    update_data: dict[str, Any] = {"modified_by": user_id}
    # Map request fields to entity fields (title → name for graph storage)
    field_map: dict[str, str] = {
        "status": "status",
        "priority": "priority",
        "title": "name",
        "description": "description",
        "assignees": "assignees",
        "epic_id": "epic_id",
        "feature": "feature",
        "complexity": "complexity",
        "tags": "tags",
        "technologies": "technologies",
    }
    for req_field, data_key in field_map.items():
        value = getattr(request, req_field)
        if value is not None:
            update_data[data_key] = value
    return update_data


@router.patch("/{task_id}", response_model=TaskActionResponse)
async def update_task(
    task_id: str,
    request: UpdateTaskRequest,
    sync: bool = Query(False, description="Wait for update to complete synchronously"),
    org: AuthOrganization = Depends(get_current_organization),
    user: AuthUser = Depends(get_current_user),
    auth: AuthContext = Depends(get_auth_context),
) -> TaskActionResponse:
    """Update task fields.

    By default, enqueues the update to the background worker and returns
    immediately (async fast path). Pass ``?sync=true`` to wait for the
    update to complete inline — useful when the caller needs confirmation.
    """
    from sibyl.jobs.queue import enqueue_update_task as enqueue_update_task_async

    await _verify_task_access(task_id, org, auth)

    group_id = str(org.id)
    update_data = _build_update_data(request, str(user.id))

    has_dep_changes = bool(request.add_depends_on or request.remove_depends_on)
    if len(update_data) <= 1 and not has_dep_changes:  # only modified_by
        raise HTTPException(status_code=400, detail="No fields to update")

    if request.epic_id:
        runtime = await get_task_graph_runtime(group_id)
        await _verify_epic_exists(runtime.entity_manager, request.epic_id)

    # --- Async fast path (default) ---
    if not sync:
        job_id = await enqueue_update_task_async(
            task_id,
            update_data,
            group_id,
            epic_id=request.epic_id,
            new_status=request.status.value if request.status else None,
            add_depends_on=request.add_depends_on,
            remove_depends_on=request.remove_depends_on,
        )
        return TaskActionResponse(
            success=True,
            action="update_task",
            task_id=task_id,
            message="Update queued",
            data={"job_id": job_id, **update_data},
        )

    # --- Sync path (?sync=true) — existing inline behaviour ---
    from sibyl.locks import LockAcquisitionError
    from sibyl_core.models.entities import Relationship, RelationshipType

    try:
        async with entity_lock(group_id, task_id, blocking=True) as lock_token:
            if not lock_token:
                raise HTTPException(
                    status_code=409,
                    detail="Task is being updated by another process. Please retry.",
                )

            runtime = await get_task_graph_runtime(group_id)

            updated = await runtime.entity_manager.update(task_id, update_data)
            if not updated:
                raise HTTPException(status_code=500, detail="Update failed")

            # Create relationship manager if any relationship changes needed
            needs_rel_mgr = (
                request.epic_id is not None or request.add_depends_on or request.remove_depends_on
            )
            relationship_manager = runtime.relationship_manager if needs_rel_mgr else None

            if request.epic_id is not None:
                belongs_to_epic = Relationship(
                    id=f"rel_{task_id}_belongs_to_{request.epic_id}",
                    source_id=task_id,
                    target_id=request.epic_id,
                    relationship_type=RelationshipType.BELONGS_TO,
                )
                await relationship_manager.create(belongs_to_epic)

            # Handle dependency mutations
            for dep_id in request.add_depends_on:
                dep_rel = Relationship(
                    id=f"rel_{task_id}_depends_on_{dep_id}",
                    source_id=task_id,
                    target_id=dep_id,
                    relationship_type=RelationshipType.DEPENDS_ON,
                )
                await relationship_manager.create(dep_rel)
            for dep_id in request.remove_depends_on:
                await relationship_manager.delete_between(
                    task_id, dep_id, RelationshipType.DEPENDS_ON
                )

            if request.status:
                epic_id = request.epic_id or updated.metadata.get("epic_id")
                await _maybe_start_epic(runtime.entity_manager, task_id, epic_id, request.status)

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
    org: AuthOrganization = Depends(get_current_organization),
    user: AuthUser = Depends(get_current_user),
    auth: AuthContext = Depends(get_auth_context),
) -> NoteResponse:
    """Create a note on a task.

    If the task is still being created asynchronously, the note will be queued
    and processed when the task materializes. The response will have status="pending".
    """
    from datetime import UTC, datetime

    from sibyl.jobs.pending import is_pending, queue_pending_operation
    from sibyl_core.models.entities import Relationship, RelationshipType

    await _verify_task_access(task_id, org, auth)

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
                WSEvent.NOTE_PENDING,
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
        runtime = await get_task_graph_runtime(group_id)

        # Verify task exists in graph
        task = await runtime.entity_manager.get(task_id)
        if not task:
            raise HTTPException(status_code=404, detail=f"Task not found: {task_id}")

        note = Note(
            id=note_id,
            name=request.content[:50] + ("..." if len(request.content) > 50 else ""),
            task_id=task_id,
            content=request.content,
            author_type=request.author_type,
            author_name=request.author_name,
            created_at=created_at,
            created_by=str(user.id),
        )

        # Create in graph
        await runtime.entity_manager.create_direct(note)

        # Create BELONGS_TO relationship with task
        belongs_to = Relationship(
            id=f"rel_{note_id}_belongs_to_{task_id}",
            source_id=note_id,
            target_id=task_id,
            relationship_type=RelationshipType.BELONGS_TO,
        )
        await runtime.relationship_manager.create(belongs_to)

        log.info(
            "create_note_success",
            note_id=note_id,
            task_id=task_id,
            author_type=request.author_type,
        )

        await broadcast_event(
            WSEvent.NOTE_CREATED,
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
    org: AuthOrganization = Depends(get_current_organization),
    auth: AuthContext = Depends(get_auth_context),
) -> NotesListResponse:
    """List all notes for a task."""
    # Read access is sufficient for listing notes
    await _verify_task_access(task_id, org, auth, required_role=ProjectRole.VIEWER)

    try:
        group_id = str(org.id)
        runtime = await get_task_graph_runtime(group_id)

        # Get notes for task
        notes_entities = await runtime.entity_manager.get_notes_for_task(task_id, limit=limit)

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
