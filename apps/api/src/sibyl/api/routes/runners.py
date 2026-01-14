"""Runner management endpoints.

REST API and WebSocket for managing distributed runners that execute agents.
"""

import asyncio
import contextlib
from datetime import UTC, datetime
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

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
        status=RunnerStatus.OFFLINE,
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
    if runner.status != RunnerStatus.OFFLINE:
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
                    status=RunnerStatus.ONLINE,
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
                    status=RunnerStatus.OFFLINE,
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

    async def send_to_runner(
        self, runner_id: UUID, message: dict
    ) -> bool:
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
                        await conn.websocket.send_json({
                            "type": "heartbeat",
                            "server_time": now.isoformat(),
                        })
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


def _extract_runner_auth(websocket: WebSocket) -> tuple[UUID, UUID] | None:
    """Extract org_id and user_id from WebSocket auth.

    Returns (org_id, user_id) or None if auth fails.
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

    if org_id and user_id:
        return UUID(str(org_id)), UUID(str(user_id))
    return None


async def _handle_heartbeat(
    conn: RunnerConnection, runner_id: UUID, session: AsyncSession
) -> None:
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
    from sibyl.api.websocket import broadcast_event

    await broadcast_event(
        "agent_update",
        {
            "agent_id": data.get("agent_id"),
            "status": data.get("status"),
            "progress": data.get("progress"),
            "activity": data.get("activity"),
        },
        org_id=str(org_id),
    )


async def _handle_task_complete(
    conn: RunnerConnection, runner_id: UUID, data: dict
) -> None:
    """Handle task completion from runner."""
    task_id = data.get("task_id")
    result = data.get("result", {})

    # Resolve any pending futures
    if task_id in conn.pending_tasks:
        conn.pending_tasks[task_id].set_result(result)
        del conn.pending_tasks[task_id]

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
    elif msg_type == "task_complete":
        await _handle_task_complete(conn, runner_id, data)
    else:
        await websocket.send_json({"type": "error", "message": f"Unknown message type: {msg_type}"})


@router.websocket("/ws/{runner_id}")
async def runner_websocket(websocket: WebSocket, runner_id: UUID) -> None:
    """WebSocket endpoint for runner connections.

    Protocol:
        Runner -> Server:
            - {"type": "heartbeat_ack"} - Acknowledge server heartbeat
            - {"type": "status", "status": "busy|online|draining"}
            - {"type": "agent_update", "agent_id": "...", ...}
            - {"type": "task_complete", "task_id": "...", "result": {...}}
            - {"type": "project_register", "project_id": "...", "worktree_path": "..."}

        Server -> Runner:
            - {"type": "heartbeat", "server_time": "..."} - Keepalive
            - {"type": "task_assign", "task_id": "...", "config": {...}}
            - {"type": "task_cancel", "task_id": "..."}
    """
    from sibyl.db.connection import get_session

    # Authenticate
    if not settings.disable_auth:
        auth = _extract_runner_auth(websocket)
        if not auth:
            await websocket.accept()
            await websocket.close(code=1008, reason="Authentication required")
            return
        org_id, user_id = auth
    else:
        await websocket.accept()
        await websocket.send_json({"type": "error", "message": "Auth disabled in dev mode"})
        await websocket.close(code=1008)
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
