"""MetaOrchestrator management endpoints.

REST API for managing MetaOrchestrators - the Tier 1 project-level coordinators.

MetaOrchestrators manage sprint execution by:
- Maintaining a queue of tasks to process
- Spawning TaskOrchestrators according to strategy
- Tracking budget and cost across tasks
- Aggregating metrics for reporting
"""

from datetime import datetime

import structlog
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from sibyl.auth.authorization import verify_entity_project_access
from sibyl.auth.rls import AuthSession, get_auth_session
from sibyl.db.models import Organization, ProjectRole
from sibyl_core.errors import EntityNotFoundError
from sibyl_core.graph.client import get_graph_client
from sibyl_core.graph.entities import EntityManager
from sibyl_core.models import (
    EntityType,
    MetaOrchestratorRecord,
    SprintStrategy,
)

log = structlog.get_logger()

router = APIRouter(prefix="/meta-orchestrators", tags=["orchestrators"])


# =============================================================================
# Request/Response Models
# =============================================================================


class GetOrCreateRequest(BaseModel):
    """Request to get or create a MetaOrchestrator."""

    project_id: str = Field(..., description="Project UUID")


class QueueTasksRequest(BaseModel):
    """Request to queue tasks for processing."""

    task_ids: list[str] = Field(..., description="Task UUIDs to queue")


class SetStrategyRequest(BaseModel):
    """Request to update strategy settings."""

    strategy: str = Field(
        ...,
        description="Sprint strategy: sequential, parallel, priority",
    )
    max_concurrent: int | None = Field(
        default=None,
        ge=1,
        le=10,
        description="Max concurrent TaskOrchestrators (for parallel strategy)",
    )


class SetBudgetRequest(BaseModel):
    """Request to update budget settings."""

    budget_usd: float = Field(..., ge=0, description="Budget limit in USD")
    alert_threshold: float = Field(
        default=0.8,
        ge=0,
        le=1,
        description="Alert when this percentage of budget is consumed",
    )


class StartRequest(BaseModel):
    """Request to start orchestration."""

    gate_config: list[str] | None = Field(
        default=None,
        description="Quality gates for spawned orchestrators",
    )


class MetaOrchestratorResponse(BaseModel):
    """MetaOrchestrator details."""

    id: str
    project_id: str
    status: str
    strategy: str
    queue_size: int
    active_count: int
    tasks_completed: int
    tasks_failed: int
    total_rework_cycles: int
    budget_usd: float
    spent_usd: float
    budget_remaining: float
    max_concurrent: int
    created_at: datetime | None


class MetaOrchestratorActionResponse(BaseModel):
    """Response from MetaOrchestrator action."""

    success: bool
    orchestrator_id: str
    action: str
    message: str


class MetaOrchestratorStatusResponse(BaseModel):
    """Detailed status information."""

    id: str
    status: str
    strategy: str
    queue_size: int
    active_count: int
    tasks_completed: int
    tasks_failed: int
    total_rework_cycles: int
    budget_usd: float
    spent_usd: float
    budget_remaining: float
    budget_utilization: float


# =============================================================================
# Helper Functions
# =============================================================================


def _require_org(auth: AuthSession) -> Organization:
    """Require organization context."""
    if auth.ctx.organization is None:
        raise HTTPException(status_code=403, detail="Organization context required")
    return auth.ctx.organization


def _record_to_response(record: MetaOrchestratorRecord) -> MetaOrchestratorResponse:
    """Convert MetaOrchestratorRecord to response model."""
    return MetaOrchestratorResponse(
        id=record.id,
        project_id=record.project_id,
        status=record.status.value,
        strategy=record.strategy.value,
        queue_size=len(record.task_queue),
        active_count=len(record.active_orchestrators),
        tasks_completed=record.tasks_completed,
        tasks_failed=record.tasks_failed,
        total_rework_cycles=record.total_rework_cycles,
        budget_usd=record.budget_usd,
        spent_usd=record.spent_usd,
        budget_remaining=record.budget_usd - record.spent_usd,
        max_concurrent=record.max_concurrent,
        created_at=record.created_at,
    )


def _parse_strategy(strategy: str) -> SprintStrategy:
    """Parse strategy string to enum."""
    try:
        return SprintStrategy(strategy.lower())
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid strategy: {strategy}. Valid options: sequential, parallel, priority",
        ) from None


async def _get_meta_orchestrator(
    entity_manager: EntityManager,
    org: Organization,
    orchestrator_id: str,
) -> MetaOrchestratorRecord:
    """Get MetaOrchestrator by ID."""
    try:
        entity = await entity_manager.get(orchestrator_id)
        if not entity or entity.entity_type != EntityType.META_ORCHESTRATOR:
            raise HTTPException(
                status_code=404,
                detail=f"MetaOrchestrator not found: {orchestrator_id}",
            )
    except EntityNotFoundError:
        raise HTTPException(
            status_code=404,
            detail=f"MetaOrchestrator not found: {orchestrator_id}",
        ) from None

    if isinstance(entity, MetaOrchestratorRecord):
        return entity
    return MetaOrchestratorRecord.from_entity(entity, str(org.id))


# =============================================================================
# Endpoints
# =============================================================================


@router.post("", response_model=MetaOrchestratorResponse)
async def get_or_create_orchestrator(
    request: GetOrCreateRequest,
    auth: AuthSession = Depends(get_auth_session),
) -> MetaOrchestratorResponse:
    """Get existing or create new MetaOrchestrator for a project.

    MetaOrchestrator is a singleton per project - only one can exist.
    If one already exists, it is returned.

    Requires ADMIN access to the project.
    """
    org = _require_org(auth)

    # Verify project access
    await verify_entity_project_access(
        auth.session, auth.ctx, request.project_id, required_role=ProjectRole.ADMIN
    )

    client = await get_graph_client()
    entity_manager = EntityManager(client, group_id=str(org.id))

    # Use the MetaOrchestratorService
    from sibyl.agents.meta_orchestrator import MetaOrchestratorService

    service = MetaOrchestratorService(
        entity_manager=entity_manager,
        org_id=str(org.id),
        project_id=request.project_id,
    )

    record = await service.get_or_create()

    log.info(
        "MetaOrchestrator get_or_create",
        orchestrator_id=record.id,
        project_id=request.project_id,
    )

    return _record_to_response(record)


@router.get("/{orchestrator_id}", response_model=MetaOrchestratorResponse)
async def get_orchestrator(
    orchestrator_id: str,
    auth: AuthSession = Depends(get_auth_session),
) -> MetaOrchestratorResponse:
    """Get MetaOrchestrator details.

    Requires VIEWER+ access to the orchestrator's project.
    """
    org = _require_org(auth)

    client = await get_graph_client()
    entity_manager = EntityManager(client, group_id=str(org.id))

    record = await _get_meta_orchestrator(entity_manager, org, orchestrator_id)

    # Verify project access
    await verify_entity_project_access(
        auth.session, auth.ctx, record.project_id, required_role=ProjectRole.VIEWER
    )

    return _record_to_response(record)


@router.get("/{orchestrator_id}/status", response_model=MetaOrchestratorStatusResponse)
async def get_orchestrator_status(
    orchestrator_id: str,
    auth: AuthSession = Depends(get_auth_session),
) -> MetaOrchestratorStatusResponse:
    """Get detailed MetaOrchestrator status.

    Includes budget utilization and metrics.

    Requires VIEWER+ access to the orchestrator's project.
    """
    org = _require_org(auth)

    client = await get_graph_client()
    entity_manager = EntityManager(client, group_id=str(org.id))

    record = await _get_meta_orchestrator(entity_manager, org, orchestrator_id)

    # Verify project access
    await verify_entity_project_access(
        auth.session, auth.ctx, record.project_id, required_role=ProjectRole.VIEWER
    )

    from sibyl.agents.meta_orchestrator import MetaOrchestratorService

    service = MetaOrchestratorService(
        entity_manager=entity_manager,
        org_id=str(org.id),
        project_id=record.project_id,
    )

    status = await service.get_status(orchestrator_id)

    return MetaOrchestratorStatusResponse(**status)


@router.post("/{orchestrator_id}/queue", response_model=MetaOrchestratorResponse)
async def queue_tasks(
    orchestrator_id: str,
    request: QueueTasksRequest,
    auth: AuthSession = Depends(get_auth_session),
) -> MetaOrchestratorResponse:
    """Add tasks to the processing queue.

    Tasks are processed according to the configured strategy.
    Duplicate tasks are automatically skipped.

    Requires CONTRIBUTOR+ access to the orchestrator's project.
    """
    org = _require_org(auth)

    client = await get_graph_client()
    entity_manager = EntityManager(client, group_id=str(org.id))

    record = await _get_meta_orchestrator(entity_manager, org, orchestrator_id)

    # Verify project access
    await verify_entity_project_access(
        auth.session, auth.ctx, record.project_id, required_role=ProjectRole.CONTRIBUTOR
    )

    from sibyl.agents.meta_orchestrator import MetaOrchestratorService

    service = MetaOrchestratorService(
        entity_manager=entity_manager,
        org_id=str(org.id),
        project_id=record.project_id,
    )

    updated = await service.queue_tasks(orchestrator_id, request.task_ids)

    log.info(
        "Tasks queued to MetaOrchestrator",
        orchestrator_id=orchestrator_id,
        task_count=len(request.task_ids),
    )

    return _record_to_response(updated)


@router.post("/{orchestrator_id}/start", response_model=MetaOrchestratorActionResponse)
async def start_orchestrator(
    orchestrator_id: str,
    request: StartRequest | None = None,
    auth: AuthSession = Depends(get_auth_session),
) -> MetaOrchestratorActionResponse:
    """Start processing the task queue.

    Spawns TaskOrchestrators according to the configured strategy.

    Requires ADMIN access to the orchestrator's project.
    """
    org = _require_org(auth)

    client = await get_graph_client()
    entity_manager = EntityManager(client, group_id=str(org.id))

    record = await _get_meta_orchestrator(entity_manager, org, orchestrator_id)

    # Verify project access
    await verify_entity_project_access(
        auth.session, auth.ctx, record.project_id, required_role=ProjectRole.ADMIN
    )

    from sibyl.agents.meta_orchestrator import MetaOrchestratorError, MetaOrchestratorService

    service = MetaOrchestratorService(
        entity_manager=entity_manager,
        org_id=str(org.id),
        project_id=record.project_id,
        # Note: Full spawning requires additional dependencies (agent_runner, etc.)
        # For now, just update status - actual spawning happens via worker jobs
    )

    try:
        # Parse gate config if provided
        gate_config = None
        if request and request.gate_config:
            from sibyl_core.models import QualityGateType

            gate_config = []
            for gate in request.gate_config:
                try:
                    gate_config.append(QualityGateType(gate.lower()))
                except ValueError:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Invalid gate type: {gate}",
                    ) from None

        await service.start(orchestrator_id, gate_config=gate_config)
    except MetaOrchestratorError as e:
        raise HTTPException(status_code=400, detail=str(e)) from None

    log.info("Started MetaOrchestrator", orchestrator_id=orchestrator_id)

    return MetaOrchestratorActionResponse(
        success=True,
        orchestrator_id=orchestrator_id,
        action="start",
        message="MetaOrchestrator started, spawning TaskOrchestrators",
    )


@router.post("/{orchestrator_id}/pause", response_model=MetaOrchestratorActionResponse)
async def pause_orchestrator(
    orchestrator_id: str,
    auth: AuthSession = Depends(get_auth_session),
) -> MetaOrchestratorActionResponse:
    """Pause orchestration.

    Active TaskOrchestrators continue but no new ones spawn.

    Requires CONTRIBUTOR+ access to the orchestrator's project.
    """
    org = _require_org(auth)

    client = await get_graph_client()
    entity_manager = EntityManager(client, group_id=str(org.id))

    record = await _get_meta_orchestrator(entity_manager, org, orchestrator_id)

    # Verify project access
    await verify_entity_project_access(
        auth.session, auth.ctx, record.project_id, required_role=ProjectRole.CONTRIBUTOR
    )

    from sibyl.agents.meta_orchestrator import MetaOrchestratorService

    service = MetaOrchestratorService(
        entity_manager=entity_manager,
        org_id=str(org.id),
        project_id=record.project_id,
    )

    await service.pause(orchestrator_id, reason="user_requested")

    log.info("Paused MetaOrchestrator", orchestrator_id=orchestrator_id)

    return MetaOrchestratorActionResponse(
        success=True,
        orchestrator_id=orchestrator_id,
        action="pause",
        message="MetaOrchestrator paused",
    )


@router.post("/{orchestrator_id}/resume", response_model=MetaOrchestratorActionResponse)
async def resume_orchestrator(
    orchestrator_id: str,
    auth: AuthSession = Depends(get_auth_session),
) -> MetaOrchestratorActionResponse:
    """Resume paused orchestration.

    Requires CONTRIBUTOR+ access to the orchestrator's project.
    """
    org = _require_org(auth)

    client = await get_graph_client()
    entity_manager = EntityManager(client, group_id=str(org.id))

    record = await _get_meta_orchestrator(entity_manager, org, orchestrator_id)

    # Verify project access
    await verify_entity_project_access(
        auth.session, auth.ctx, record.project_id, required_role=ProjectRole.CONTRIBUTOR
    )

    from sibyl.agents.meta_orchestrator import MetaOrchestratorError, MetaOrchestratorService

    service = MetaOrchestratorService(
        entity_manager=entity_manager,
        org_id=str(org.id),
        project_id=record.project_id,
    )

    try:
        await service.resume(orchestrator_id)
    except MetaOrchestratorError as e:
        raise HTTPException(status_code=400, detail=str(e)) from None

    log.info("Resumed MetaOrchestrator", orchestrator_id=orchestrator_id)

    return MetaOrchestratorActionResponse(
        success=True,
        orchestrator_id=orchestrator_id,
        action="resume",
        message="MetaOrchestrator resumed",
    )


@router.put("/{orchestrator_id}/strategy", response_model=MetaOrchestratorResponse)
async def update_strategy(
    orchestrator_id: str,
    request: SetStrategyRequest,
    auth: AuthSession = Depends(get_auth_session),
) -> MetaOrchestratorResponse:
    """Update orchestration strategy.

    - sequential: One task at a time
    - parallel: Multiple concurrent tasks (up to max_concurrent)
    - priority: One at a time, highest priority first

    Requires ADMIN access to the orchestrator's project.
    """
    org = _require_org(auth)

    client = await get_graph_client()
    entity_manager = EntityManager(client, group_id=str(org.id))

    record = await _get_meta_orchestrator(entity_manager, org, orchestrator_id)

    # Verify project access
    await verify_entity_project_access(
        auth.session, auth.ctx, record.project_id, required_role=ProjectRole.ADMIN
    )

    strategy = _parse_strategy(request.strategy)

    from sibyl.agents.meta_orchestrator import MetaOrchestratorService

    service = MetaOrchestratorService(
        entity_manager=entity_manager,
        org_id=str(org.id),
        project_id=record.project_id,
    )

    updated = await service.set_strategy(
        orchestrator_id,
        strategy=strategy,
        max_concurrent=request.max_concurrent,
    )

    log.info(
        "Updated MetaOrchestrator strategy",
        orchestrator_id=orchestrator_id,
        strategy=strategy.value,
    )

    return _record_to_response(updated)


@router.put("/{orchestrator_id}/budget", response_model=MetaOrchestratorResponse)
async def update_budget(
    orchestrator_id: str,
    request: SetBudgetRequest,
    auth: AuthSession = Depends(get_auth_session),
) -> MetaOrchestratorResponse:
    """Update budget settings.

    Sets the maximum budget and alert threshold for the sprint.

    Requires ADMIN access to the orchestrator's project.
    """
    org = _require_org(auth)

    client = await get_graph_client()
    entity_manager = EntityManager(client, group_id=str(org.id))

    record = await _get_meta_orchestrator(entity_manager, org, orchestrator_id)

    # Verify project access
    await verify_entity_project_access(
        auth.session, auth.ctx, record.project_id, required_role=ProjectRole.ADMIN
    )

    from sibyl.agents.meta_orchestrator import MetaOrchestratorService

    service = MetaOrchestratorService(
        entity_manager=entity_manager,
        org_id=str(org.id),
        project_id=record.project_id,
    )

    updated = await service.set_budget(
        orchestrator_id,
        budget_usd=request.budget_usd,
        alert_threshold=request.alert_threshold,
    )

    log.info(
        "Updated MetaOrchestrator budget",
        orchestrator_id=orchestrator_id,
        budget_usd=request.budget_usd,
    )

    return _record_to_response(updated)
