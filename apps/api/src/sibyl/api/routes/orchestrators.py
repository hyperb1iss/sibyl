"""TaskOrchestrator management endpoints.

REST API for managing TaskOrchestrators - the Tier 2 build loop coordinators.

TaskOrchestrators manage the implement → review → rework cycle for individual
tasks, with quality gates and Ralph Loop safety controls.
"""

import contextlib
from datetime import UTC, datetime
from typing import cast
from uuid import uuid4

import structlog
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import update

from sibyl.agents.approval_queue import create_approval_queue
from sibyl.agents.state_sync import update_agent_state
from sibyl.auth.authorization import verify_entity_project_access
from sibyl.auth.rls import AuthSession, get_auth_session
from sibyl.db.models import AgentMessage, Organization, ProjectRole
from sibyl.jobs.queue import enqueue_agent_execution, enqueue_agent_resume
from sibyl_core.errors import EntityNotFoundError
from sibyl_core.graph.client import get_graph_client
from sibyl_core.graph.entities import EntityManager
from sibyl_core.graph.relationships import RelationshipManager
from sibyl_core.models import (
    AgentRecord,
    AgentSpawnSource,
    AgentStatus,
    AgentType,
    ApprovalStatus,
    EntityType,
    QualityGateType,
    Relationship,
    RelationshipType,
    Task,
    TaskOrchestratorPhase,
    TaskOrchestratorRecord,
    TaskOrchestratorStatus,
    TaskStatus,
)

log = structlog.get_logger()

router = APIRouter(prefix="/task-orchestrators", tags=["orchestrators"])


# =============================================================================
# Request/Response Models
# =============================================================================


class CreateOrchestratorRequest(BaseModel):
    """Request to create a TaskOrchestrator."""

    task_id: str = Field(..., description="Task to orchestrate")
    project_id: str = Field(..., description="Project UUID")
    gate_config: list[str] | None = Field(
        default=None,
        description="Quality gates to run. Options: lint, typecheck, test, ai_review, security_scan, human_review",
    )
    max_rework_attempts: int = Field(
        default=3,
        ge=1,
        le=10,
        description="Max rework iterations before human escalation (Ralph Loop safety)",
    )
    auto_start: bool = Field(
        default=True,
        description="Start immediately after creation",
    )


class OrchestratorResponse(BaseModel):
    """TaskOrchestrator details."""

    id: str
    task_id: str
    project_id: str
    status: str
    current_phase: str
    worker_id: str | None
    rework_count: int
    max_rework_attempts: int
    gate_config: list[str]
    pending_approval_id: str | None
    created_at: datetime | None
    started_at: datetime | None
    completed_at: datetime | None


class OrchestratorActionResponse(BaseModel):
    """Response from orchestrator action."""

    success: bool
    orchestrator_id: str
    action: str
    message: str


class HumanReviewRequest(BaseModel):
    """Request to approve or reject human review."""

    approved: bool
    feedback: str | None = None


class OrchestratorListResponse(BaseModel):
    """List of orchestrators."""

    orchestrators: list[OrchestratorResponse]
    total: int


# =============================================================================
# Helper Functions
# =============================================================================


def _require_org(auth: AuthSession) -> Organization:
    """Require organization context."""
    if auth.ctx.organization is None:
        raise HTTPException(status_code=403, detail="Organization context required")
    return auth.ctx.organization


def _record_to_response(record: TaskOrchestratorRecord) -> OrchestratorResponse:
    """Convert TaskOrchestratorRecord to response model."""
    return OrchestratorResponse(
        id=record.id,
        task_id=record.task_id,
        project_id=record.project_id,
        status=record.status.value,
        current_phase=record.current_phase.value,
        worker_id=record.worker_id,
        rework_count=record.rework_count,
        max_rework_attempts=record.max_rework_attempts,
        gate_config=[g.value for g in record.gate_config],
        pending_approval_id=record.pending_approval_id,
        created_at=record.created_at,
        started_at=record.started_at,
        completed_at=record.completed_at,
    )


async def _respond_to_review_approval(
    *,
    entity_manager: EntityManager,
    org_id: str,
    auth: AuthSession,
    record: TaskOrchestratorRecord,
    approved: bool,
    feedback: str | None,
) -> tuple[str | None, str]:
    """Respond to the pending approval request for human review."""
    approval_id = record.pending_approval_id
    approval_status = ApprovalStatus.APPROVED.value if approved else ApprovalStatus.DENIED.value
    responded_at = datetime.now(UTC).isoformat()

    if approval_id:
        queue = await create_approval_queue(
            entity_manager=entity_manager,
            org_id=org_id,
            project_id=record.project_id,
            agent_id=record.worker_id or record.id,
            task_id=record.task_id,
        )
        try:
            await queue.respond(
                approval_id,
                approved=approved,
                message=feedback or "",
                responded_by=str(auth.ctx.user.id),
            )
        except Exception as exc:
            raise HTTPException(
                status_code=500,
                detail="Failed to respond to approval request",
            ) from exc
    else:
        log.warning("Human review missing pending approval id", orchestrator_id=record.id)

    if approval_id and record.worker_id:
        try:
            stmt = (
                update(AgentMessage)
                .where(AgentMessage.agent_id == record.worker_id)
                .where(AgentMessage.extra["approval_id"].astext == approval_id)
                .values(
                    extra=AgentMessage.extra.op("||")(
                        {"status": approval_status, "responded_at": responded_at}
                    )
                )
            )
            await auth.session.execute(stmt)
            await auth.session.commit()
        except Exception as e:
            log.warning("Failed to update approval message status", error=str(e))

    return approval_id, approval_status


async def _apply_review_outcome(
    *,
    entity_manager: EntityManager,
    record: TaskOrchestratorRecord,
    approved: bool,
) -> tuple[str, str, AgentStatus]:
    """Apply a human review decision to the TaskOrchestrator."""
    metadata = {**(record.metadata or {}), "pending_approval_id": None}

    if approved:
        completed_at = datetime.now(UTC).isoformat()
        metadata.update(
            {
                "status": TaskOrchestratorStatus.COMPLETE.value,
                "current_phase": TaskOrchestratorPhase.MERGE.value,
                "completed_at": completed_at,
            }
        )
        await entity_manager.update(
            record.id,
            {
                "status": TaskOrchestratorStatus.COMPLETE.value,
                "current_phase": TaskOrchestratorPhase.MERGE.value,
                "completed_at": completed_at,
                "pending_approval_id": None,
                "metadata": metadata,
            },
        )
        await entity_manager.update(
            record.task_id,
            {"status": TaskStatus.REVIEW.value},
        )
        return "approved", "Review approved, orchestrator complete", AgentStatus.COMPLETED

    new_rework_count = record.rework_count + 1
    if new_rework_count >= record.max_rework_attempts:
        metadata.update(
            {
                "status": TaskOrchestratorStatus.FAILED.value,
                "failure_reason": "max_rework_exceeded",
            }
        )
        await entity_manager.update(
            record.id,
            {
                "status": TaskOrchestratorStatus.FAILED.value,
                "pending_approval_id": None,
                "metadata": metadata,
            },
        )
        message = (
            f"Review rejected, max rework attempts ({record.max_rework_attempts}) exceeded"
        )
        return "rejected_failed", message, AgentStatus.FAILED

    metadata.update(
        {
            "status": TaskOrchestratorStatus.REWORKING.value,
            "current_phase": TaskOrchestratorPhase.REWORK.value,
            "rework_count": new_rework_count,
        }
    )
    await entity_manager.update(
        record.id,
        {
            "status": TaskOrchestratorStatus.REWORKING.value,
            "current_phase": TaskOrchestratorPhase.REWORK.value,
            "rework_count": new_rework_count,
            "pending_approval_id": None,
            "metadata": metadata,
        },
    )
    return (
        "rejected_rework",
        f"Review rejected, rework iteration {new_rework_count}",
        AgentStatus.WORKING,
    )


def _build_worker_prompt(task: Task, record: TaskOrchestratorRecord) -> str:
    """Build the worker prompt for the TaskOrchestrator."""
    gates_desc = ", ".join(g.value for g in record.gate_config)

    return f"""You are implementing a task as part of an orchestrated build loop.

## Task
**{task.title}**

{task.description}

## Quality Gates
Your implementation will be reviewed by these automated gates: {gates_desc}

## Instructions
1. Implement the task completely
2. Ensure all quality gates can pass (lint, types, tests)
3. When finished, signal completion so gates can run
"""


async def _resolve_repo_path(
    entity_manager: EntityManager,
    project_id: str,
) -> str | None:
    """Resolve repo_path from project metadata when available."""
    with contextlib.suppress(Exception):
        project = await entity_manager.get(project_id)
        if project and hasattr(project, "repo_path"):
            return getattr(project, "repo_path", None)
    return None


async def _ensure_worker_record(
    *,
    entity_manager: EntityManager,
    org_id: str,
    project_id: str,
    record: TaskOrchestratorRecord,
    task: Task,
    prompt: str,
    worker_id: str | None = None,
) -> tuple[str, bool]:
    """Ensure a worker AgentRecord exists for the orchestrator."""
    if worker_id is None:
        worker_id = f"agent_{uuid4().hex[:12]}"

    created = False
    try:
        existing = await entity_manager.get(worker_id)
    except EntityNotFoundError:
        existing = None

    if existing:
        if existing.entity_type != EntityType.AGENT:
            raise HTTPException(
                status_code=409,
                detail=f"Worker ID {worker_id} is not an agent entity",
            )
        updates = {
            "task_id": task.id,
            "project_id": project_id,
            "task_orchestrator_id": record.id,
            "standalone": False,
        }
        if prompt:
            updates["initial_prompt"] = prompt[:500]
        await entity_manager.update(worker_id, updates)
    else:
        worker_record = AgentRecord(
            id=worker_id,
            name=f"Worker: {(task.title or 'Task')[:50]}",
            organization_id=org_id,
            project_id=project_id,
            agent_type=AgentType.IMPLEMENTER,
            spawn_source=AgentSpawnSource.ORCHESTRATOR,
            task_id=task.id,
            status=AgentStatus.INITIALIZING,
            initial_prompt=prompt[:500],
            task_orchestrator_id=record.id,
            standalone=False,
        )
        await entity_manager.create_direct(worker_record)
        created = True

    return worker_id, created


def _parse_gate_config(gates: list[str] | None) -> list[QualityGateType] | None:
    """Parse gate config strings to enum values."""
    if gates is None:
        return None

    result = []
    for gate in gates:
        try:
            result.append(QualityGateType(gate.lower()))
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid gate type: {gate}. Valid options: lint, typecheck, test, ai_review, security_scan, human_review",
            ) from None
    return result


# =============================================================================
# Endpoints
# =============================================================================


@router.post("", response_model=OrchestratorResponse)
async def create_orchestrator(
    request: CreateOrchestratorRequest,
    auth: AuthSession = Depends(get_auth_session),
) -> OrchestratorResponse:
    """Create a TaskOrchestrator for a task.

    Creates a build loop coordinator that will manage the implement → review → rework
    cycle for the specified task.

    If auto_start is True (default), immediately spawns a worker agent.

    Requires CONTRIBUTOR+ access to the project.
    """

    org = _require_org(auth)

    # Verify project access
    await verify_entity_project_access(
        auth.session, auth.ctx, request.project_id, required_role=ProjectRole.CONTRIBUTOR
    )

    client = await get_graph_client()
    entity_manager = EntityManager(client, group_id=str(org.id))
    relationship_manager = RelationshipManager(client, group_id=str(org.id))

    # Get task
    try:
        task_entity = await entity_manager.get(request.task_id)
        if not task_entity:
            raise HTTPException(status_code=404, detail=f"Task not found: {request.task_id}")
    except EntityNotFoundError:
        raise HTTPException(status_code=404, detail=f"Task not found: {request.task_id}") from None

    # Verify task belongs to project
    task_meta = task_entity.metadata or {}
    if task_meta.get("project_id") != request.project_id:
        raise HTTPException(status_code=400, detail="Task does not belong to specified project")

    # Parse gate config
    gate_config = _parse_gate_config(request.gate_config)

    # Create service (without runner for now - will be added when we spawn)
    # For now, create orchestrator directly
    from uuid import uuid4

    orchestrator_id = f"taskorch_{uuid4().hex[:16]}"
    now = datetime.now(UTC)

    # Get task name for orchestrator
    task_name = task_entity.name if hasattr(task_entity, "name") else "Task"

    record = TaskOrchestratorRecord(
        id=orchestrator_id,
        name=f"TaskOrchestrator: {task_name[:50]}",
        organization_id=str(org.id),
        project_id=request.project_id,
        task_id=request.task_id,
        status=TaskOrchestratorStatus.INITIALIZING,
        current_phase=TaskOrchestratorPhase.IMPLEMENT,
        gate_config=gate_config
        or [
            QualityGateType.LINT,
            QualityGateType.TYPECHECK,
            QualityGateType.TEST,
            QualityGateType.AI_REVIEW,
        ],
        max_rework_attempts=request.max_rework_attempts,
        started_at=now if request.auto_start else None,
    )

    await entity_manager.create_direct(record)

    # Create relationship to task
    await relationship_manager.create(
        Relationship(
            id=f"rel_{uuid4().hex[:16]}",
            source_id=orchestrator_id,
            target_id=request.task_id,
            relationship_type=RelationshipType.WORKS_ON,
        )
    )

    log.info(
        "Created TaskOrchestrator",
        orchestrator_id=orchestrator_id,
        task_id=request.task_id,
        auto_start=request.auto_start,
    )

    if request.auto_start:
        task = cast("Task", task_entity)
        prompt = _build_worker_prompt(task, record)
        worker_id, created = await _ensure_worker_record(
            entity_manager=entity_manager,
            org_id=str(org.id),
            project_id=request.project_id,
            record=record,
            task=task,
            prompt=prompt,
            worker_id=record.worker_id,
        )
        repo_path = await _resolve_repo_path(entity_manager, request.project_id)

        try:
            await enqueue_agent_execution(
                agent_id=worker_id,
                org_id=str(org.id),
                project_id=request.project_id,
                prompt=prompt,
                agent_type=AgentType.IMPLEMENTER.value,
                task_id=request.task_id,
                created_by=str(auth.ctx.user.id) if auth.ctx.user else None,
                create_worktree=True,
                repo_path=repo_path,
            )
        except Exception as exc:
            if created:
                with contextlib.suppress(Exception):
                    await entity_manager.delete(worker_id)
            raise HTTPException(
                status_code=500, detail="Failed to enqueue worker"
            ) from exc

        await update_agent_state(
            org_id=str(org.id),
            agent_id=worker_id,
            status=AgentStatus.INITIALIZING.value,
            task_id=request.task_id,
            orchestrator_id=orchestrator_id,
        )

        with contextlib.suppress(Exception):
            await relationship_manager.create(
                Relationship(
                    id=f"rel_{uuid4().hex[:16]}",
                    source_id=orchestrator_id,
                    target_id=worker_id,
                    relationship_type=RelationshipType.ORCHESTRATES,
                )
            )

        started_at = record.started_at or datetime.now(UTC)
        metadata = {
            **(record.metadata or {}),
            "worker_id": worker_id,
            "status": TaskOrchestratorStatus.IMPLEMENTING.value,
            "current_phase": TaskOrchestratorPhase.IMPLEMENT.value,
            "started_at": started_at.isoformat(),
        }
        await entity_manager.update(
            orchestrator_id,
            {
                "worker_id": worker_id,
                "status": TaskOrchestratorStatus.IMPLEMENTING.value,
                "current_phase": TaskOrchestratorPhase.IMPLEMENT.value,
                "started_at": started_at.isoformat(),
                "metadata": metadata,
            },
        )

        record.worker_id = worker_id
        record.status = TaskOrchestratorStatus.IMPLEMENTING
        record.current_phase = TaskOrchestratorPhase.IMPLEMENT
        record.started_at = started_at
        record.metadata = metadata

    return _record_to_response(record)


@router.get("", response_model=OrchestratorListResponse)
async def list_orchestrators(
    project_id: str,
    status: str | None = None,
    limit: int = 50,
    auth: AuthSession = Depends(get_auth_session),
) -> OrchestratorListResponse:
    """List TaskOrchestrators for a project.

    Optionally filter by status: initializing, implementing, reviewing, reworking,
    human_review, complete, failed, paused.

    Requires VIEWER+ access to the project.
    """
    org = _require_org(auth)

    # Verify project access
    await verify_entity_project_access(
        auth.session, auth.ctx, project_id, required_role=ProjectRole.VIEWER
    )

    client = await get_graph_client()
    entity_manager = EntityManager(client, group_id=str(org.id))

    # Get all orchestrators for project
    entities = await entity_manager.list_by_type(EntityType.TASK_ORCHESTRATOR, limit=limit * 2)

    # Filter and convert
    records = []
    for e in entities:
        if isinstance(e, TaskOrchestratorRecord):
            record = e
        else:
            record = TaskOrchestratorRecord.from_entity(e, str(org.id))

        # Filter by project
        if record.project_id != project_id:
            continue

        # Filter by status if specified
        if status and record.status.value != status:
            continue

        records.append(record)

    records = records[:limit]

    return OrchestratorListResponse(
        orchestrators=[_record_to_response(r) for r in records],
        total=len(records),
    )


@router.get("/{orchestrator_id}", response_model=OrchestratorResponse)
async def get_orchestrator(
    orchestrator_id: str,
    auth: AuthSession = Depends(get_auth_session),
) -> OrchestratorResponse:
    """Get TaskOrchestrator details.

    Requires VIEWER+ access to the orchestrator's project.
    """
    org = _require_org(auth)

    client = await get_graph_client()
    entity_manager = EntityManager(client, group_id=str(org.id))

    try:
        entity = await entity_manager.get(orchestrator_id)
        if not entity or entity.entity_type != EntityType.TASK_ORCHESTRATOR:
            raise HTTPException(
                status_code=404, detail=f"Orchestrator not found: {orchestrator_id}"
            )
    except EntityNotFoundError:
        raise HTTPException(
            status_code=404, detail=f"Orchestrator not found: {orchestrator_id}"
        ) from None

    if isinstance(entity, TaskOrchestratorRecord):
        record = entity
    else:
        record = TaskOrchestratorRecord.from_entity(entity, str(org.id))

    # Verify project access
    await verify_entity_project_access(
        auth.session, auth.ctx, record.project_id, required_role=ProjectRole.VIEWER
    )

    return _record_to_response(record)


@router.post("/{orchestrator_id}/start", response_model=OrchestratorActionResponse)
async def start_orchestrator(
    orchestrator_id: str,
    auth: AuthSession = Depends(get_auth_session),
) -> OrchestratorActionResponse:
    """Start a TaskOrchestrator that was created without auto_start.

    Spawns a worker agent and begins the build loop.

    Requires CONTRIBUTOR+ access to the orchestrator's project.
    """
    org = _require_org(auth)

    client = await get_graph_client()
    entity_manager = EntityManager(client, group_id=str(org.id))

    try:
        entity = await entity_manager.get(orchestrator_id)
        if not entity or entity.entity_type != EntityType.TASK_ORCHESTRATOR:
            raise HTTPException(
                status_code=404, detail=f"Orchestrator not found: {orchestrator_id}"
            )
    except EntityNotFoundError:
        raise HTTPException(
            status_code=404, detail=f"Orchestrator not found: {orchestrator_id}"
        ) from None

    if isinstance(entity, TaskOrchestratorRecord):
        record = entity
    else:
        record = TaskOrchestratorRecord.from_entity(entity, str(org.id))

    # Verify project access
    await verify_entity_project_access(
        auth.session, auth.ctx, record.project_id, required_role=ProjectRole.CONTRIBUTOR
    )

    if record.status != TaskOrchestratorStatus.INITIALIZING:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot start orchestrator in state: {record.status.value}",
        )

    relationship_manager = RelationshipManager(client, group_id=str(org.id))

    task_entity = await entity_manager.get(record.task_id)
    if not task_entity:
        raise HTTPException(status_code=404, detail=f"Task not found: {record.task_id}")
    task = cast("Task", task_entity)

    prompt = _build_worker_prompt(task, record)
    worker_id, created = await _ensure_worker_record(
        entity_manager=entity_manager,
        org_id=str(org.id),
        project_id=record.project_id,
        record=record,
        task=task,
        prompt=prompt,
        worker_id=record.worker_id,
    )
    repo_path = await _resolve_repo_path(entity_manager, record.project_id)

    try:
        await enqueue_agent_execution(
            agent_id=worker_id,
            org_id=str(org.id),
            project_id=record.project_id,
            prompt=prompt,
            agent_type=AgentType.IMPLEMENTER.value,
            task_id=record.task_id,
            created_by=str(auth.ctx.user.id) if auth.ctx.user else None,
            create_worktree=True,
            repo_path=repo_path,
        )
    except Exception as exc:
        if created:
            with contextlib.suppress(Exception):
                await entity_manager.delete(worker_id)
        raise HTTPException(status_code=500, detail="Failed to enqueue worker") from exc

    await update_agent_state(
        org_id=str(org.id),
        agent_id=worker_id,
        status=AgentStatus.INITIALIZING.value,
        task_id=record.task_id,
        orchestrator_id=orchestrator_id,
    )

    with contextlib.suppress(Exception):
        await relationship_manager.create(
            Relationship(
                id=f"rel_{uuid4().hex[:16]}",
                source_id=orchestrator_id,
                target_id=worker_id,
                relationship_type=RelationshipType.ORCHESTRATES,
            )
        )

    started_at = record.started_at or datetime.now(UTC)
    metadata = {
        **(record.metadata or {}),
        "worker_id": worker_id,
        "status": TaskOrchestratorStatus.IMPLEMENTING.value,
        "current_phase": TaskOrchestratorPhase.IMPLEMENT.value,
        "started_at": started_at.isoformat(),
    }
    await entity_manager.update(
        orchestrator_id,
        {
            "worker_id": worker_id,
            "status": TaskOrchestratorStatus.IMPLEMENTING.value,
            "current_phase": TaskOrchestratorPhase.IMPLEMENT.value,
            "started_at": started_at.isoformat(),
            "metadata": metadata,
        },
    )

    log.info("Started TaskOrchestrator", orchestrator_id=orchestrator_id)

    return OrchestratorActionResponse(
        success=True,
        orchestrator_id=orchestrator_id,
        action="start",
        message="Orchestrator started, spawning worker",
    )


@router.post("/{orchestrator_id}/pause", response_model=OrchestratorActionResponse)
async def pause_orchestrator(
    orchestrator_id: str,
    auth: AuthSession = Depends(get_auth_session),
) -> OrchestratorActionResponse:
    """Pause a running TaskOrchestrator.

    Pauses the build loop and the worker agent.

    Requires CONTRIBUTOR+ access to the orchestrator's project.
    """
    org = _require_org(auth)

    client = await get_graph_client()
    entity_manager = EntityManager(client, group_id=str(org.id))

    try:
        entity = await entity_manager.get(orchestrator_id)
        if not entity or entity.entity_type != EntityType.TASK_ORCHESTRATOR:
            raise HTTPException(
                status_code=404, detail=f"Orchestrator not found: {orchestrator_id}"
            )
    except EntityNotFoundError:
        raise HTTPException(
            status_code=404, detail=f"Orchestrator not found: {orchestrator_id}"
        ) from None

    if isinstance(entity, TaskOrchestratorRecord):
        record = entity
    else:
        record = TaskOrchestratorRecord.from_entity(entity, str(org.id))

    # Verify project access
    await verify_entity_project_access(
        auth.session, auth.ctx, record.project_id, required_role=ProjectRole.CONTRIBUTOR
    )

    pausable_states = (
        TaskOrchestratorStatus.IMPLEMENTING,
        TaskOrchestratorStatus.REVIEWING,
        TaskOrchestratorStatus.REWORKING,
    )
    if record.status not in pausable_states:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot pause orchestrator in state: {record.status.value}",
        )

    # Update status
    await entity_manager.update(
        orchestrator_id,
        {"status": TaskOrchestratorStatus.PAUSED.value},
    )

    # TODO: Signal worker to pause

    log.info("Paused TaskOrchestrator", orchestrator_id=orchestrator_id)

    return OrchestratorActionResponse(
        success=True,
        orchestrator_id=orchestrator_id,
        action="pause",
        message="Orchestrator paused",
    )


@router.post("/{orchestrator_id}/resume", response_model=OrchestratorActionResponse)
async def resume_orchestrator(
    orchestrator_id: str,
    auth: AuthSession = Depends(get_auth_session),
) -> OrchestratorActionResponse:
    """Resume a paused TaskOrchestrator.

    Requires CONTRIBUTOR+ access to the orchestrator's project.
    """
    org = _require_org(auth)

    client = await get_graph_client()
    entity_manager = EntityManager(client, group_id=str(org.id))

    try:
        entity = await entity_manager.get(orchestrator_id)
        if not entity or entity.entity_type != EntityType.TASK_ORCHESTRATOR:
            raise HTTPException(
                status_code=404, detail=f"Orchestrator not found: {orchestrator_id}"
            )
    except EntityNotFoundError:
        raise HTTPException(
            status_code=404, detail=f"Orchestrator not found: {orchestrator_id}"
        ) from None

    if isinstance(entity, TaskOrchestratorRecord):
        record = entity
    else:
        record = TaskOrchestratorRecord.from_entity(entity, str(org.id))

    # Verify project access
    await verify_entity_project_access(
        auth.session, auth.ctx, record.project_id, required_role=ProjectRole.CONTRIBUTOR
    )

    if record.status != TaskOrchestratorStatus.PAUSED:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot resume orchestrator in state: {record.status.value}",
        )

    # Determine appropriate status based on phase
    status_map = {
        TaskOrchestratorPhase.IMPLEMENT: TaskOrchestratorStatus.IMPLEMENTING,
        TaskOrchestratorPhase.REVIEW: TaskOrchestratorStatus.REVIEWING,
        TaskOrchestratorPhase.REWORK: TaskOrchestratorStatus.REWORKING,
        TaskOrchestratorPhase.HUMAN_REVIEW: TaskOrchestratorStatus.HUMAN_REVIEW,
    }
    new_status = status_map.get(record.current_phase, TaskOrchestratorStatus.IMPLEMENTING)

    if not record.worker_id:
        raise HTTPException(
            status_code=400,
            detail="Cannot resume orchestrator without a worker",
        )

    try:
        await enqueue_agent_resume(
            agent_id=record.worker_id,
            org_id=str(org.id),
        )
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail="Failed to enqueue worker resume",
        ) from exc

    metadata = {
        **(record.metadata or {}),
        "worker_id": record.worker_id,
        "status": new_status.value,
        "current_phase": record.current_phase.value,
    }
    await entity_manager.update(
        orchestrator_id,
        {
            "status": new_status.value,
            "metadata": metadata,
        },
    )

    log.info("Resumed TaskOrchestrator", orchestrator_id=orchestrator_id)

    return OrchestratorActionResponse(
        success=True,
        orchestrator_id=orchestrator_id,
        action="resume",
        message="Orchestrator resumed",
    )


@router.post("/{orchestrator_id}/review", response_model=OrchestratorActionResponse)
async def respond_to_review(
    orchestrator_id: str,
    request: HumanReviewRequest,
    auth: AuthSession = Depends(get_auth_session),
) -> OrchestratorActionResponse:
    """Respond to a human review request.

    Approve or reject the implementation, optionally providing feedback.

    Requires CONTRIBUTOR+ access to the orchestrator's project.
    """
    org = _require_org(auth)

    client = await get_graph_client()
    entity_manager = EntityManager(client, group_id=str(org.id))

    try:
        entity = await entity_manager.get(orchestrator_id)
        if not entity or entity.entity_type != EntityType.TASK_ORCHESTRATOR:
            raise HTTPException(
                status_code=404, detail=f"Orchestrator not found: {orchestrator_id}"
            )
    except EntityNotFoundError:
        raise HTTPException(
            status_code=404, detail=f"Orchestrator not found: {orchestrator_id}"
        ) from None

    if isinstance(entity, TaskOrchestratorRecord):
        record = entity
    else:
        record = TaskOrchestratorRecord.from_entity(entity, str(org.id))

    # Verify project access
    await verify_entity_project_access(
        auth.session, auth.ctx, record.project_id, required_role=ProjectRole.CONTRIBUTOR
    )

    if record.status != TaskOrchestratorStatus.HUMAN_REVIEW:
        raise HTTPException(
            status_code=400,
            detail=f"Orchestrator not awaiting review: {record.status.value}",
        )

    approval_id, approval_status = await _respond_to_review_approval(
        entity_manager=entity_manager,
        org_id=str(org.id),
        auth=auth,
        record=record,
        approved=request.approved,
        feedback=request.feedback,
    )

    action, message, agent_status = await _apply_review_outcome(
        entity_manager=entity_manager,
        record=record,
        approved=request.approved,
    )


    if record.worker_id:
        await entity_manager.update(record.worker_id, {"status": agent_status.value})
        await update_agent_state(
            org_id=str(org.id),
            agent_id=record.worker_id,
            status=agent_status.value,
        )

    from sibyl.api.pubsub import publish_event

    if approval_id:
        await publish_event(
            "approval_response",
            {
                "approval_id": approval_id,
                "agent_id": record.worker_id,
                "action": "approve" if request.approved else "deny",
                "status": approval_status,
                "response_by": str(auth.ctx.user.id),
            },
            org_id=str(org.id),
        )

    if record.worker_id:
        await publish_event(
            "agent_status",
            {"agent_id": record.worker_id, "status": agent_status.value},
            org_id=str(org.id),
        )

    log.info(
        "Human review response",
        orchestrator_id=orchestrator_id,
        approved=request.approved,
        action=action,
    )

    return OrchestratorActionResponse(
        success=True,
        orchestrator_id=orchestrator_id,
        action=action,
        message=message,
    )
