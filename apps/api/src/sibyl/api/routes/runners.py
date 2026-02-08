"""Runner management endpoints.

REST API and WebSocket for managing distributed runners that execute agents.
"""

import asyncio
import contextlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from sibyl.agents.task_router import RoutingResult, RunnerScore, TaskRouter
from sibyl.auth.dependencies import require_org_role
from sibyl.auth.http import extract_bearer_token
from sibyl.auth.jwt import JwtError, verify_access_token
from sibyl.auth.rls import AuthSession, get_auth_session
from sibyl.config import settings
from sibyl.db.models import (
    Organization,
    OrganizationRole,
    Runner,
    RunnerProject,
    RunnerStatus,
)

log = structlog.get_logger()


@dataclass
class RunnerAuthContext:
    """Decoded auth context for runner WebSocket sessions."""

    org_id: UUID
    user_id: UUID
    runner_id: UUID | None = None
    sandbox_id: UUID | None = None
    scopes: set[str] = field(default_factory=set)

# Auth: org member required for all runner operations
_RUNNER_ROLES = (OrganizationRole.MEMBER, OrganizationRole.ADMIN, OrganizationRole.OWNER)

router = APIRouter(
    prefix="/runners",
    tags=["runners"],
    dependencies=[Depends(require_org_role(*_RUNNER_ROLES))],
)


# =============================================================================
# Request/Response Models
# =============================================================================


class RunnerRegisterRequest(BaseModel):
    """Request to register a new runner."""

    name: str = Field(..., min_length=1, max_length=255, description="Runner display name")
    hostname: str = Field(..., min_length=1, max_length=255, description="Host machine name")
    capabilities: list[str] = Field(
        default_factory=list, description="Runner capabilities (docker, gpu, large_context)"
    )
    max_concurrent_agents: int = Field(
        default=3, ge=1, le=20, description="Max concurrent agent executions"
    )
    client_version: str | None = Field(default=None, max_length=32, description="Client version")


class RunnerResponse(BaseModel):
    """Runner details response."""

    id: UUID
    name: str
    hostname: str
    graph_runner_id: str
    capabilities: list[str]
    max_concurrent_agents: int
    current_agent_count: int
    status: str
    last_heartbeat: datetime | None
    client_version: str | None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class RunnerListResponse(BaseModel):
    """List of runners."""

    runners: list[RunnerResponse]
    total: int


class RunnerProjectResponse(BaseModel):
    """Runner project assignment."""

    id: UUID
    runner_id: UUID
    project_id: UUID
    worktree_path: str
    worktree_branch: str | None
    last_used_at: datetime | None

    class Config:
        from_attributes = True


class RunnerUpdateRequest(BaseModel):
    """Request to update runner settings."""

    name: str | None = Field(default=None, min_length=1, max_length=255)
    capabilities: list[str] | None = None
    max_concurrent_agents: int | None = Field(default=None, ge=1, le=20)


class RouteTaskRequest(BaseModel):
    """Request to route a task to an optimal runner."""

    project_id: UUID | None = Field(default=None, description="Project ID for affinity scoring")
    required_capabilities: list[str] = Field(
        default_factory=list, description="Capabilities the runner must have"
    )
    preferred_runner_id: UUID | None = Field(
        default=None, description="Prefer this runner if available"
    )
    exclude_runners: list[UUID] = Field(default_factory=list, description="Runner IDs to exclude")


class RunnerScoreResponse(BaseModel):
    """Score breakdown for a runner."""

    runner_id: UUID
    runner_name: str
    total_score: float
    affinity_score: float
    capability_score: float
    load_score: float
    health_penalty: float
    available_slots: int
    has_warm_worktree: bool
    missing_capabilities: list[str] | None

    @classmethod
    def from_score(cls, score: RunnerScore) -> "RunnerScoreResponse":
        return cls(
            runner_id=score.runner_id,
            runner_name=score.runner_name,
            total_score=score.total_score,
            affinity_score=score.affinity_score,
            capability_score=score.capability_score,
            load_score=score.load_score,
            health_penalty=score.health_penalty,
            available_slots=score.available_slots,
            has_warm_worktree=score.has_warm_worktree,
            missing_capabilities=score.missing_capabilities,
        )


class RouteTaskResponse(BaseModel):
    """Result of task routing decision."""

    success: bool
    runner_id: UUID | None = None
    runner_name: str | None = None
    score: RunnerScoreResponse | None = None
    all_scores: list[RunnerScoreResponse] | None = None
    reason: str | None = None

    @classmethod
    def from_result(cls, result: RoutingResult) -> "RouteTaskResponse":
        return cls(
            success=result.success,
            runner_id=result.runner_id,
            runner_name=result.runner_name,
            score=RunnerScoreResponse.from_score(result.score) if result.score else None,
            all_scores=[RunnerScoreResponse.from_score(s) for s in result.all_scores]
            if result.all_scores
            else None,
            reason=result.reason,
        )


class RunnerScoresResponse(BaseModel):
    """Response containing runner scores."""

    scores: list[RunnerScoreResponse]
    total: int


# =============================================================================
# Helpers
# =============================================================================


def _require_org(auth: AuthSession) -> Organization:
    """Require organization context, raise 403 if missing."""
    if auth.ctx.organization is None:
        raise HTTPException(status_code=403, detail="Organization context required")
    return auth.ctx.organization


def _generate_graph_runner_id() -> str:
    """Generate a unique graph runner ID."""
    import secrets

    return f"runner_{secrets.token_hex(8)}"


# =============================================================================
# REST Routes
# =============================================================================


@router.post("/register", response_model=RunnerResponse)
async def register_runner(
    request: RunnerRegisterRequest,
    auth: AuthSession = Depends(get_auth_session),
) -> RunnerResponse:
    """Register a new runner for this organization.

    The runner will be created with OFFLINE status. Once connected via WebSocket,
    status will change to ONLINE.

    Requires: Org member role (any member can register runners).
    """
    org = _require_org(auth)
    session = auth.session

    # Generate graph entity ID
    graph_runner_id = _generate_graph_runner_id()

    # Create runner record
    runner = Runner(
        organization_id=org.id,
        user_id=auth.ctx.user.id,
        graph_runner_id=graph_runner_id,
        name=request.name,
        hostname=request.hostname,
        capabilities=request.capabilities,
        max_concurrent_agents=request.max_concurrent_agents,
        client_version=request.client_version,
        status=RunnerStatus.OFFLINE.value,
    )

    session.add(runner)
    await session.commit()
    await session.refresh(runner)

    log.info(
        "runner_registered",
        runner_id=str(runner.id),
        graph_runner_id=graph_runner_id,
        name=request.name,
        org_id=str(org.id),
    )

    return RunnerResponse.model_validate(runner)


@router.get("", response_model=RunnerListResponse)
async def list_runners(
    status: str | None = None,
    auth: AuthSession = Depends(get_auth_session),
) -> RunnerListResponse:
    """List all runners for this organization.

    Optionally filter by status (online, offline, busy, draining).
    """
    org = _require_org(auth)
    session = auth.session

    query = select(Runner).where(Runner.organization_id == org.id)

    if status:
        query = query.where(Runner.status == status)

    query = query.order_by(Runner.created_at.desc())

    result = await session.execute(query)
    runners = result.scalars().all()

    return RunnerListResponse(
        runners=[RunnerResponse.model_validate(r) for r in runners],
        total=len(runners),
    )


@router.get("/{runner_id}", response_model=RunnerResponse)
async def get_runner(
    runner_id: UUID,
    auth: AuthSession = Depends(get_auth_session),
) -> RunnerResponse:
    """Get details for a specific runner."""
    org = _require_org(auth)
    session = auth.session

    result = await session.execute(
        select(Runner).where(Runner.id == runner_id, Runner.organization_id == org.id)
    )
    runner = result.scalar_one_or_none()

    if not runner:
        raise HTTPException(status_code=404, detail="Runner not found")

    return RunnerResponse.model_validate(runner)


@router.patch("/{runner_id}", response_model=RunnerResponse)
async def update_runner(
    runner_id: UUID,
    request: RunnerUpdateRequest,
    auth: AuthSession = Depends(get_auth_session),
) -> RunnerResponse:
    """Update runner settings.

    Only the runner owner or org admin can update.
    """
    org = _require_org(auth)
    session = auth.session

    result = await session.execute(
        select(Runner).where(Runner.id == runner_id, Runner.organization_id == org.id)
    )
    runner = result.scalar_one_or_none()

    if not runner:
        raise HTTPException(status_code=404, detail="Runner not found")

    # Check ownership
    if runner.user_id != auth.ctx.user.id and auth.ctx.org_role not in (
        OrganizationRole.OWNER,
        OrganizationRole.ADMIN,
    ):
        raise HTTPException(status_code=403, detail="Cannot update runner you don't own")

    # Apply updates
    if request.name is not None:
        runner.name = request.name
    if request.capabilities is not None:
        runner.capabilities = request.capabilities
    if request.max_concurrent_agents is not None:
        runner.max_concurrent_agents = request.max_concurrent_agents

    await session.commit()
    await session.refresh(runner)

    return RunnerResponse.model_validate(runner)


@router.delete("/{runner_id}")
async def delete_runner(
    runner_id: UUID,
    auth: AuthSession = Depends(get_auth_session),
) -> dict[str, str]:
    """Delete a runner.

    Only the runner owner or org admin can delete.
    Runner must be offline.
    """
    org = _require_org(auth)
    session = auth.session

    result = await session.execute(
        select(Runner).where(Runner.id == runner_id, Runner.organization_id == org.id)
    )
    runner = result.scalar_one_or_none()

    if not runner:
        raise HTTPException(status_code=404, detail="Runner not found")

    # Check ownership
    if runner.user_id != auth.ctx.user.id and auth.ctx.org_role not in (
        OrganizationRole.OWNER,
        OrganizationRole.ADMIN,
    ):
        raise HTTPException(status_code=403, detail="Cannot delete runner you don't own")

    # Must be offline
    if runner.status != RunnerStatus.OFFLINE.value:
        raise HTTPException(
            status_code=400, detail="Runner must be offline before deletion. Disconnect it first."
        )

    await session.delete(runner)
    await session.commit()

    log.info("runner_deleted", runner_id=str(runner_id), org_id=str(org.id))

    return {"status": "deleted", "runner_id": str(runner_id)}


@router.get("/{runner_id}/projects", response_model=list[RunnerProjectResponse])
async def list_runner_projects(
    runner_id: UUID,
    auth: AuthSession = Depends(get_auth_session),
) -> list[RunnerProjectResponse]:
    """List projects with warm worktrees on this runner."""
    org = _require_org(auth)
    session = auth.session

    # Verify runner exists and belongs to org
    result = await session.execute(
        select(Runner).where(Runner.id == runner_id, Runner.organization_id == org.id)
    )
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Runner not found")

    result = await session.execute(
        select(RunnerProject).where(RunnerProject.runner_id == runner_id)
    )
    projects = result.scalars().all()

    return [RunnerProjectResponse.model_validate(p) for p in projects]


@router.post("/route", response_model=RouteTaskResponse)
async def route_task(
    request: RouteTaskRequest,
    auth: AuthSession = Depends(get_auth_session),
) -> RouteTaskResponse:
    """Route a task to the optimal runner.

    Scores runners based on:
    - Project affinity (warm worktrees = 50 points)
    - Capability match (30 points if all required capabilities present)
    - Current load (0-20 points based on available capacity)
    - Health (stale heartbeat = -100 penalty)

    Returns the best runner and full scoring breakdown for transparency.
    """
    org = _require_org(auth)

    # Route to the current user's runners only
    task_router = TaskRouter(auth.session, org.id, auth.ctx.user.id)
    result = await task_router.route_task(
        project_id=request.project_id,
        required_capabilities=request.required_capabilities,
        preferred_runner_id=request.preferred_runner_id,
        exclude_runners=request.exclude_runners,
    )

    return RouteTaskResponse.from_result(result)


@router.get("/scores", response_model=RunnerScoresResponse)
async def get_runner_scores(
    project_id: UUID | None = None,
    capabilities: str | None = None,
    auth: AuthSession = Depends(get_auth_session),
) -> RunnerScoresResponse:
    """Get routing scores for all runners.

    Useful for debugging and UI display of runner availability.

    Query params:
        project_id: Optional project for affinity scoring
        capabilities: Comma-separated list of required capabilities
    """
    org = _require_org(auth)

    required_caps = []
    if capabilities:
        required_caps = [c.strip() for c in capabilities.split(",") if c.strip()]

    # Show scores for the current user's runners only
    task_router = TaskRouter(auth.session, org.id, auth.ctx.user.id)
    scores = await task_router.get_runner_scores(
        project_id=project_id,
        required_capabilities=required_caps,
    )

    return RunnerScoresResponse(
        scores=[RunnerScoreResponse.from_score(s) for s in scores],
        total=len(scores),
    )


# =============================================================================
# WebSocket Connection Manager for Runners
# =============================================================================


class RunnerConnection:
    """Active runner WebSocket connection."""

    def __init__(
        self,
        websocket: WebSocket,
        runner_id: UUID,
        org_id: UUID,
        user_id: UUID,
    ):
        self.websocket = websocket
        self.runner_id = runner_id
        self.org_id = org_id
        self.user_id = user_id
        self.connected_at = datetime.now(UTC)
        self.last_heartbeat = datetime.now(UTC)
        self.pending_tasks: dict[str, asyncio.Future] = {}


class RunnerConnectionManager:
    """Manages active runner WebSocket connections."""

    HEARTBEAT_INTERVAL = 30
    HEARTBEAT_TIMEOUT = 45

    def __init__(self) -> None:
        self.connections: dict[UUID, RunnerConnection] = {}
        self._lock = asyncio.Lock()
        self._heartbeat_task: asyncio.Task | None = None

    async def connect(
        self,
        websocket: WebSocket,
        runner_id: UUID,
        org_id: UUID,
        user_id: UUID,
        session: AsyncSession,
    ) -> RunnerConnection:
        """Accept and register a runner connection."""
        await websocket.accept()

        conn = RunnerConnection(
            websocket=websocket,
            runner_id=runner_id,
            org_id=org_id,
            user_id=user_id,
        )

        async with self._lock:
            # Disconnect existing connection for this runner
            if runner_id in self.connections:
                old = self.connections[runner_id]
                with contextlib.suppress(Exception):
                    await old.websocket.close(code=1000, reason="Replaced by new connection")

            self.connections[runner_id] = conn

            # Update runner status
            await session.execute(
                update(Runner)
                .where(Runner.id == runner_id)
                .values(
                    status=RunnerStatus.ONLINE.value,
                    last_heartbeat=datetime.now(UTC),
                    websocket_session_id=str(id(websocket)),
                )
            )
            await session.commit()

            # Start heartbeat task if needed
            if self._heartbeat_task is None or self._heartbeat_task.done():
                self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

        log.info(
            "runner_connected",
            runner_id=str(runner_id),
            org_id=str(org_id),
            total_runners=len(self.connections),
        )

        return conn

    async def disconnect(self, runner_id: UUID, session: AsyncSession) -> None:
        """Remove a runner connection."""
        async with self._lock:
            if runner_id in self.connections:
                del self.connections[runner_id]

            # Update runner status
            await session.execute(
                update(Runner)
                .where(Runner.id == runner_id)
                .values(
                    status=RunnerStatus.OFFLINE.value,
                    websocket_session_id=None,
                )
            )
            await session.commit()

        log.info(
            "runner_disconnected",
            runner_id=str(runner_id),
            total_runners=len(self.connections),
        )

    def get_connection(self, runner_id: UUID) -> RunnerConnection | None:
        """Get connection for a runner."""
        return self.connections.get(runner_id)

    def get_online_runners(self, org_id: UUID | None = None) -> list[RunnerConnection]:
        """Get all online runners, optionally filtered by org."""
        if org_id:
            return [c for c in self.connections.values() if c.org_id == org_id]
        return list(self.connections.values())

    async def send_to_runner(self, runner_id: UUID, message: dict) -> bool:
        """Send a message to a specific runner."""
        conn = self.connections.get(runner_id)
        if not conn:
            return False

        try:
            await conn.websocket.send_json(message)
            return True
        except Exception:
            return False

    async def _heartbeat_loop(self) -> None:
        """Background task for runner heartbeats."""
        from sibyl.db.connection import get_session

        while True:
            await asyncio.sleep(self.HEARTBEAT_INTERVAL)

            if not self.connections:
                break

            now = datetime.now(UTC)
            dead_runners: list[UUID] = []

            async with self._lock:
                for runner_id, conn in list(self.connections.items()):
                    # Check timeout
                    if (now - conn.last_heartbeat).total_seconds() > self.HEARTBEAT_TIMEOUT:
                        dead_runners.append(runner_id)
                        continue

                    # Send heartbeat
                    try:
                        await conn.websocket.send_json(
                            {
                                "type": "heartbeat",
                                "server_time": now.isoformat(),
                            }
                        )
                    except Exception:
                        dead_runners.append(runner_id)

            # Clean up dead connections
            for runner_id in dead_runners:
                async with get_session() as session:
                    await self.disconnect(runner_id, session)


# Module-level singleton (lazy init via dict to avoid global statement)
_runner_manager_cache: dict[str, RunnerConnectionManager] = {}


def get_runner_manager() -> RunnerConnectionManager:
    """Get or create the global runner connection manager."""
    if "instance" not in _runner_manager_cache:
        _runner_manager_cache["instance"] = RunnerConnectionManager()
    return _runner_manager_cache["instance"]


# =============================================================================
# WebSocket Handler
# =============================================================================


def _extract_runner_auth(websocket: WebSocket) -> RunnerAuthContext | None:
    """Extract runner WebSocket auth claims.

    Required claims:
    - org: organization ID
    - sub: user ID

    Optional claims:
    - rid: bound runner ID
    - sid: sandbox ID
    - scp/scope: scopes
    """
    if settings.disable_auth:
        return None

    auth_header = websocket.headers.get("authorization")
    token = extract_bearer_token(auth_header) or websocket.cookies.get("sibyl_access_token")

    if not token:
        return None

    try:
        claims = verify_access_token(token)
    except JwtError:
        return None

    org_id = claims.get("org")
    user_id = claims.get("sub")
    if not (org_id and user_id):
        return None

    rid_claim = claims.get("rid")
    sid_claim = claims.get("sid")
    scope_claim = claims.get("scp", claims.get("scope"))
    scopes: set[str] = set()
    if isinstance(scope_claim, str):
        scopes = {item.strip() for item in scope_claim.split() if item.strip()}
    elif isinstance(scope_claim, list):
        scopes = {str(item).strip() for item in scope_claim if str(item).strip()}

    runner_claim: UUID | None = None
    sandbox_claim: UUID | None = None
    try:
        if rid_claim:
            runner_claim = UUID(str(rid_claim))
        if sid_claim:
            sandbox_claim = UUID(str(sid_claim))
    except (TypeError, ValueError):
        return None

    try:
        return RunnerAuthContext(
            org_id=UUID(str(org_id)),
            user_id=UUID(str(user_id)),
            runner_id=runner_claim,
            sandbox_id=sandbox_claim,
            scopes=scopes,
        )
    except (TypeError, ValueError):
        return None


async def _handle_heartbeat(conn: RunnerConnection, runner_id: UUID, session: AsyncSession) -> None:
    """Handle heartbeat acknowledgment from runner."""
    conn.last_heartbeat = datetime.now(UTC)
    await session.execute(
        update(Runner).where(Runner.id == runner_id).values(last_heartbeat=datetime.now(UTC))
    )
    await session.commit()


async def _handle_status(runner_id: UUID, data: dict, session: AsyncSession) -> None:
    """Handle status update from runner."""
    new_status = data.get("status")
    if new_status in ("online", "busy", "draining"):
        await session.execute(
            update(Runner)
            .where(Runner.id == runner_id)
            .values(status=new_status, current_agent_count=data.get("agent_count", 0))
        )
        await session.commit()


async def _handle_project_register(
    runner_id: UUID, data: dict, websocket: WebSocket, session: AsyncSession
) -> None:
    """Handle warm worktree registration from runner."""
    project_id = data.get("project_id")
    worktree_path = data.get("worktree_path")
    worktree_branch = data.get("worktree_branch")

    if not (project_id and worktree_path):
        return

    # Upsert runner project
    existing = await session.execute(
        select(RunnerProject).where(
            RunnerProject.runner_id == runner_id,
            RunnerProject.project_id == UUID(project_id),
        )
    )
    rp = existing.scalar_one_or_none()
    if rp:
        rp.worktree_path = worktree_path
        rp.worktree_branch = worktree_branch
        rp.last_used_at = datetime.now(UTC)
    else:
        rp = RunnerProject(
            runner_id=runner_id,
            project_id=UUID(project_id),
            worktree_path=worktree_path,
            worktree_branch=worktree_branch,
            last_used_at=datetime.now(UTC),
        )
        session.add(rp)
    await session.commit()
    await websocket.send_json({"type": "project_registered", "project_id": project_id})


async def _handle_agent_update(data: dict, org_id: UUID) -> None:
    """Handle agent update and broadcast to listeners."""
    from sibyl.api.event_types import WSEvent
    from sibyl.api.websocket import broadcast_event

    await broadcast_event(
        WSEvent.AGENT_UPDATE,
        {
            "agent_id": data.get("agent_id"),
            "status": data.get("status"),
            "progress": data.get("progress"),
            "activity": data.get("activity"),
        },
        org_id=str(org_id),
    )


def _get_sandbox_dispatcher(websocket: WebSocket):
    return getattr(websocket.app.state, "sandbox_dispatcher", None)


def _get_sandbox_controller(websocket: WebSocket):
    return getattr(websocket.app.state, "sandbox_controller", None)


async def _handle_task_ack(runner_id: UUID, data: dict, websocket: WebSocket) -> None:
    """Handle task ack from runner and mark sandbox task acknowledged."""
    task_id = data.get("task_id")
    if not task_id:
        return

    dispatcher = _get_sandbox_dispatcher(websocket)
    if dispatcher is None:
        return

    try:
        await dispatcher.ack_task(task_id=task_id, runner_id=runner_id)
    except Exception as e:
        log.warning(
            "sandbox_task_ack_failed",
            runner_id=str(runner_id),
            task_id=str(task_id),
            error=str(e),
        )


async def _handle_task_complete(
    conn: RunnerConnection, runner_id: UUID, data: dict, websocket: WebSocket
) -> None:
    """Handle task completion from runner."""
    task_id = data.get("task_id")
    result = data.get("result", {})

    # Resolve any pending futures
    if task_id in conn.pending_tasks:
        conn.pending_tasks[task_id].set_result(result)
        del conn.pending_tasks[task_id]

    dispatcher = _get_sandbox_dispatcher(websocket)
    if dispatcher is not None and task_id:
        status_value = str((result or {}).get("status", "")).lower()
        canceled = status_value in {"cancelled", "canceled"}
        success = bool(data.get("success", status_value not in {"failed", "error", "cancelled", "canceled"}))
        retryable = bool(data.get("retryable", False))
        error = data.get("error") or ((result or {}).get("error") if isinstance(result, dict) else None)
        try:
            await dispatcher.complete_task(
                task_id=task_id,
                success=success,
                result=result if isinstance(result, dict) else {"result": result},
                error=str(error) if error else None,
                retryable=retryable,
                canceled=canceled,
            )
        except Exception as e:
            log.warning(
                "sandbox_task_complete_failed",
                runner_id=str(runner_id),
                task_id=str(task_id),
                error=str(e),
            )

    log.info("task_completed_by_runner", runner_id=str(runner_id), task_id=task_id)


async def _handle_ws_message(
    conn: RunnerConnection,
    runner_id: UUID,
    org_id: UUID,
    data: dict,
    websocket: WebSocket,
    session: AsyncSession,
) -> None:
    """Route incoming WebSocket message to appropriate handler."""
    msg_type = data.get("type")

    if msg_type == "heartbeat_ack":
        await _handle_heartbeat(conn, runner_id, session)
    elif msg_type == "status":
        await _handle_status(runner_id, data, session)
    elif msg_type == "project_register":
        await _handle_project_register(runner_id, data, websocket, session)
    elif msg_type == "agent_update":
        await _handle_agent_update(data, org_id)
    elif msg_type == "task_ack":
        await _handle_task_ack(runner_id, data, websocket)
    elif msg_type == "task_complete":
        await _handle_task_complete(conn, runner_id, data, websocket)
    else:
        await websocket.send_json({"type": "error", "message": f"Unknown message type: {msg_type}"})


@router.websocket("/ws/{runner_id}")
async def runner_websocket(websocket: WebSocket, runner_id: UUID) -> None:  # noqa: PLR0915
    """WebSocket endpoint for runner connections.

    Protocol:
        Runner -> Server:
            - {"type": "heartbeat_ack"} - Acknowledge server heartbeat
            - {"type": "status", "status": "busy|online|draining"}
            - {"type": "agent_update", "agent_id": "...", ...}
            - {"type": "task_ack", "task_id": "..."}
            - {"type": "task_complete", "task_id": "...", "result": {...}}
            - {"type": "project_register", "project_id": "...", "worktree_path": "..."}

        Server -> Runner:
            - {"type": "heartbeat", "server_time": "..."} - Keepalive
            - {"type": "task_assign", "task_id": "...", "config": {...}}
            - {"type": "task_cancel", "task_id": "..."}
    """
    from sibyl.db.connection import get_session

    # Authenticate
    auth_ctx: RunnerAuthContext | None = None
    if not settings.disable_auth:
        auth_ctx = _extract_runner_auth(websocket)
        if not auth_ctx:
            await websocket.accept()
            await websocket.close(code=1008, reason="Authentication required")
            return
        org_id = auth_ctx.org_id
        user_id = auth_ctx.user_id
    else:
        await websocket.accept()
        await websocket.send_json({"type": "error", "message": "Auth disabled in dev mode"})
        await websocket.close(code=1008)
        return

    # Optional claim bindings: rid/sid/scp
    if auth_ctx and auth_ctx.runner_id and auth_ctx.runner_id != runner_id:
        await websocket.accept()
        await websocket.close(code=1008, reason="Runner claim does not match requested runner")
        return
    if auth_ctx and auth_ctx.scopes:
        allowed = {"runner", "runner:connect", "sandbox:runner", "mcp"}
        if not (auth_ctx.scopes & allowed):
            await websocket.accept()
            await websocket.close(code=1008, reason="Insufficient runner scope")
            return

    # Verify runner exists and belongs to user's org
    async with get_session() as session:
        result = await session.execute(
            select(Runner).where(Runner.id == runner_id, Runner.organization_id == org_id)
        )
        runner = result.scalar_one_or_none()

        if not runner:
            await websocket.accept()
            await websocket.close(code=1008, reason="Runner not found or not in your organization")
            return

        if runner.user_id != user_id:
            await websocket.accept()
            await websocket.close(code=1008, reason="Not authorized for this runner")
            return

        manager = get_runner_manager()
        conn = await manager.connect(websocket, runner_id, org_id, user_id, session)

    # If this is a sandbox-bound runner, sync sandbox status and flush pending tasks.
    if auth_ctx and auth_ctx.sandbox_id:
        controller = _get_sandbox_controller(websocket)
        if controller is not None:
            try:
                await controller.sync_runner_connection(
                    sandbox_id=auth_ctx.sandbox_id,
                    runner_id=runner_id,
                    connected=True,
                )
            except Exception as e:
                log.warning(
                    "sandbox_runner_connect_sync_failed",
                    runner_id=str(runner_id),
                    sandbox_id=str(auth_ctx.sandbox_id),
                    error=str(e),
                )

        dispatcher = _get_sandbox_dispatcher(websocket)
        if dispatcher is not None:
            try:
                await dispatcher.dispatch_pending_for_sandbox(
                    sandbox_id=auth_ctx.sandbox_id,
                    runner_id=runner_id,
                    send_fn=lambda msg: manager.send_to_runner(runner_id, msg),
                )
            except Exception as e:
                log.warning(
                    "sandbox_dispatch_on_connect_failed",
                    runner_id=str(runner_id),
                    sandbox_id=str(auth_ctx.sandbox_id),
                    error=str(e),
                )

    try:
        while True:
            try:
                data = await websocket.receive_json()
                async with get_session() as session:
                    await _handle_ws_message(conn, runner_id, org_id, data, websocket, session)
            except ValueError:
                await websocket.send_json({"type": "error", "message": "Invalid JSON"})
    except WebSocketDisconnect:
        pass
    finally:
        async with get_session() as session:
            await manager.disconnect(runner_id, session)
        if auth_ctx and auth_ctx.sandbox_id:
            controller = _get_sandbox_controller(websocket)
            if controller is not None:
                try:
                    await controller.sync_runner_connection(
                        sandbox_id=auth_ctx.sandbox_id,
                        runner_id=runner_id,
                        connected=False,
                    )
                except Exception as e:
                    log.warning(
                        "sandbox_runner_disconnect_sync_failed",
                        runner_id=str(runner_id),
                        sandbox_id=str(auth_ctx.sandbox_id),
                        error=str(e),
                    )
