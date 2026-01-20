"""Agent management endpoints.

REST API for managing AI agents via the AgentOrchestrator.
"""

import contextlib
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Literal
from uuid import UUID, uuid4

import structlog
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from sibyl.api.decorators import handle_not_found
from sibyl.auth.authorization import (
    list_accessible_project_graph_ids,
    verify_entity_project_access,
)
from sibyl.auth.context import AuthContext
from sibyl.auth.dependencies import require_org_role
from sibyl.auth.rls import AuthSession, get_auth_session
from sibyl.db import AgentMessage as DbAgentMessage, AgentState
from sibyl.db.models import Organization, OrganizationRole, ProjectRole, utcnow_naive
from sibyl_core.errors import EntityNotFoundError
from sibyl_core.graph.client import get_graph_client
from sibyl_core.graph.entities import EntityManager
from sibyl_core.models import (
    AgentRecord,
    AgentSpawnSource,
    AgentStatus,
    AgentType,
    EntityType,
)

if TYPE_CHECKING:
    from sibyl_core.models.entities import Entity

log = structlog.get_logger()


def _require_org(ctx: AuthContext) -> Organization:
    """Require organization context for multi-tenant routes.

    Raises:
        HTTPException 403: If no organization in context
    """
    if not ctx.organization:
        raise HTTPException(status_code=403, detail="Organization context required")
    return ctx.organization


def _is_valid_uuid(value: str | None) -> bool:
    """Check if a string is a valid UUID."""
    if not value:
        return False
    try:
        UUID(value)
        return True
    except (ValueError, TypeError):
        return False


async def _check_agent_control_permission(
    ctx: AuthContext,
    session: AsyncSession,
    entity: "Entity",
) -> None:
    """Verify user can control (modify/terminate) an agent.

    Control is allowed if:
    - User is org admin/owner
    - User created the agent (ownership)
    - User has CONTRIBUTOR+ access to agent's project

    Raises:
        HTTPException 403: If user lacks control permission
    """
    meta = entity.metadata or {}
    agent_project_id = meta.get("project_id")
    agent_created_by = meta.get("created_by") or entity.created_by

    # Org admins can control any agent
    if ctx.org_role in (OrganizationRole.OWNER, OrganizationRole.ADMIN):
        return

    # Agent creator can control their own agent
    if agent_created_by and str(ctx.user.id) == agent_created_by:
        return

    # Check project access (CONTRIBUTOR+ required for control operations)
    if agent_project_id:
        await verify_entity_project_access(
            session,
            ctx,
            agent_project_id,
            required_role=ProjectRole.CONTRIBUTOR,
        )
        return

    # No project - only creator or admin can control
    raise HTTPException(
        status_code=403,
        detail="You don't have permission to control this agent",
    )


async def _check_agent_view_permission(
    ctx: AuthContext,
    session: AsyncSession,
    entity: "Entity",
) -> None:
    """Verify user can view an agent.

    View is allowed if:
    - User is org admin/owner
    - User created the agent
    - User has VIEWER+ access to agent's project (includes org visibility)

    Raises:
        HTTPException 403: If user lacks view permission
    """
    meta = entity.metadata or {}
    agent_project_id = meta.get("project_id")
    agent_created_by = meta.get("created_by") or entity.created_by

    # Org admins can view any agent
    if ctx.org_role in (OrganizationRole.OWNER, OrganizationRole.ADMIN):
        return

    # Agent creator can view their own agent
    if agent_created_by and str(ctx.user.id) == agent_created_by:
        return

    # Check project access (VIEWER+ required for read operations)
    if agent_project_id:
        await verify_entity_project_access(
            session,
            ctx,
            agent_project_id,
            required_role=ProjectRole.VIEWER,
        )
        return

    # No project - only creator or admin can view
    raise HTTPException(
        status_code=403,
        detail="You don't have permission to view this agent",
    )


_WRITE_ROLES = (
    OrganizationRole.OWNER,
    OrganizationRole.ADMIN,
    OrganizationRole.MEMBER,
)

router = APIRouter(
    prefix="/agents",
    tags=["agents"],
    dependencies=[Depends(require_org_role(*_WRITE_ROLES))],
)


# =============================================================================
# Request/Response Models
# =============================================================================


class AgentResponse(BaseModel):
    """Agent record response."""

    id: str
    name: str
    agent_type: str
    status: str
    task_id: str | None = None
    project_id: str | None = None
    created_by: str | None = None
    spawn_source: str | None = None
    started_at: str | None = None
    completed_at: str | None = None
    last_heartbeat: str | None = None
    tokens_used: int = 0
    cost_usd: float = 0.0
    worktree_path: str | None = None
    worktree_branch: str | None = None
    error_message: str | None = None
    tags: list[str] = []


class AgentListResponse(BaseModel):
    """Response containing list of agents."""

    agents: list[AgentResponse]
    total: int
    by_status: dict[str, int] = {}
    by_type: dict[str, int] = {}


class SpawnAgentRequest(BaseModel):
    """Request to spawn a new agent."""

    prompt: str
    agent_type: AgentType = AgentType.GENERAL
    project_id: str
    task_id: str | None = None
    create_worktree: bool = True  # Enable git worktree isolation


class SpawnAgentResponse(BaseModel):
    """Response from spawning an agent."""

    success: bool
    agent_id: str
    message: str


class AgentActionRequest(BaseModel):
    """Request for agent actions (pause, resume, terminate)."""

    reason: str | None = None


class AgentActionResponse(BaseModel):
    """Response from agent action."""

    success: bool
    agent_id: str
    action: str
    message: str


class MessageRole(StrEnum):
    """Message sender role."""

    AGENT = "agent"
    USER = "user"
    SYSTEM = "system"


class MessageType(StrEnum):
    """Message content type."""

    TEXT = "text"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    ERROR = "error"


class AgentMessage(BaseModel):
    """A single message in the agent conversation."""

    id: str
    role: MessageRole
    content: str
    timestamp: str
    type: MessageType = MessageType.TEXT
    metadata: dict | None = None


class AgentMessagesResponse(BaseModel):
    """Response containing agent conversation messages."""

    agent_id: str
    messages: list[AgentMessage]
    total: int


class SendMessageRequest(BaseModel):
    """Request to send a message to an agent."""

    content: str


class SendMessageResponse(BaseModel):
    """Response from sending a message."""

    success: bool
    message_id: str


class FileChange(BaseModel):
    """A file change in the agent workspace."""

    path: str
    status: Literal["added", "modified", "deleted"]
    diff: str | None = None


class AgentWorkspaceResponse(BaseModel):
    """Agent workspace state."""

    agent_id: str
    files: list[FileChange]
    current_step: str | None = None
    completed_steps: list[str] = []


# =============================================================================
# Endpoints
# =============================================================================


@router.get("", response_model=AgentListResponse)
async def list_agents(
    project: str | None = None,
    status: AgentStatus | None = None,
    agent_type: AgentType | None = None,
    all_users: bool = False,
    include_archived: bool = False,
    limit: int = 50,
    auth: AuthSession = Depends(get_auth_session),
) -> AgentListResponse:
    """List agents for the organization.

    Args:
        project: Filter by project ID
        status: Filter by agent status
        agent_type: Filter by agent type
        all_users: If True, show agents in accessible projects (default: only user's agents)
        include_archived: If True, include archived agents (default: exclude)
        limit: Maximum results

    Results are filtered to agents the user can access (own agents + projects with VIEWER+).
    Identity comes from graph (name, type, project), operational state from Postgres.
    """
    ctx = auth.ctx
    org = _require_org(ctx)
    client = await get_graph_client()
    manager = EntityManager(client, group_id=str(org.id))

    # Get accessible project IDs for permission filtering
    is_admin = ctx.org_role in (OrganizationRole.OWNER, OrganizationRole.ADMIN)
    accessible_projects: set[str] = set()
    if not is_admin:
        accessible_projects = await list_accessible_project_graph_ids(auth.session, ctx) or set()

    user_id_str = str(ctx.user.id)

    # Get all agents from graph (identity data)
    results = await manager.list_by_type(
        entity_type=EntityType.AGENT,
        limit=limit * 3,  # Fetch extra for filtering
    )

    # Get all AgentState records from Postgres (operational data)
    state_result = await auth.session.execute(
        select(AgentState).where(col(AgentState.organization_id) == org.id)
    )
    states_by_id = {s.graph_agent_id: s for s in state_result.scalars().all()}

    # Filter agents based on access control and preferences
    agents: list = []
    for a in results:
        meta = a.metadata or {}
        agent_project_id = meta.get("project_id")
        agent_created_by = meta.get("created_by") or a.created_by

        # Access control: skip agents user cannot view (unless admin)
        if not is_admin:
            is_owner = agent_created_by == user_id_str
            has_project_access = agent_project_id and agent_project_id in accessible_projects
            if not (is_owner or has_project_access):
                continue

        # all_users=False: only show user's own agents
        # all_users=True: show all accessible agents
        if not all_users:
            if agent_created_by != user_id_str:
                continue

        agents.append(a)

    # Filter out archived agents (unless explicitly requested)
    if not include_archived:
        agents = [a for a in agents if not (a.metadata or {}).get("archived")]

    # Apply filters - use AgentState for status, graph for other fields
    if project:
        agents = [a for a in agents if (a.metadata or {}).get("project_id") == project]
    if status:
        # Use AgentState status if available, fall back to graph metadata
        agents = [
            a
            for a in agents
            if (states_by_id.get(a.id) and states_by_id[a.id].status == status.value)
            or (a.id not in states_by_id and (a.metadata or {}).get("status") == status.value)
        ]
    if agent_type:
        agents = [a for a in agents if (a.metadata or {}).get("agent_type") == agent_type.value]

    # Calculate stats using AgentState for status
    by_status: dict[str, int] = {}
    by_type: dict[str, int] = {}
    for agent in agents:
        state = states_by_id.get(agent.id)
        s = state.status if state else (agent.metadata or {}).get("status") or "initializing"
        by_status[s] = by_status.get(s, 0) + 1
        t = (agent.metadata or {}).get("agent_type") or "general"
        by_type[t] = by_type.get(t, 0) + 1

    return AgentListResponse(
        agents=[_entity_to_agent_response(a, states_by_id.get(a.id)) for a in agents[:limit]],
        total=len(agents),
        by_status=by_status,
        by_type=by_type,
    )


@router.get("/{agent_id}", response_model=AgentResponse)
@handle_not_found("Agent", "agent_id")
async def get_agent(
    agent_id: str,
    auth: AuthSession = Depends(get_auth_session),
) -> AgentResponse:
    """Get a specific agent by ID.

    Identity comes from graph, operational state from Postgres.
    """
    ctx = auth.ctx
    org = _require_org(ctx)
    client = await get_graph_client()
    manager = EntityManager(client, group_id=str(org.id))

    entity = await manager.get(agent_id)

    if entity.entity_type != EntityType.AGENT:
        raise HTTPException(status_code=404, detail=f"Agent not found: {agent_id}")

    # Check view permission (creator, org admin, or project VIEWER+)
    await _check_agent_view_permission(ctx, auth.session, entity)

    # Fetch AgentState for operational data
    state_result = await auth.session.execute(
        select(AgentState).where(col(AgentState.graph_agent_id) == agent_id)
    )
    state = state_result.scalar_one_or_none()

    return _entity_to_agent_response(entity, state)


@router.post("", response_model=SpawnAgentResponse)
async def spawn_agent(
    request: SpawnAgentRequest,
    auth: AuthSession = Depends(get_auth_session),
) -> SpawnAgentResponse:
    """Spawn a new agent and enqueue execution.

    Creates the agent entity synchronously (so UI can poll immediately),
    then enqueues execution in the worker process.

    Requires CONTRIBUTOR+ access to the target project.
    """
    from sibyl.jobs.queue import enqueue_agent_execution

    ctx = auth.ctx
    if ctx.organization is None:
        raise HTTPException(status_code=403, detail="No organization context")

    org = ctx.organization
    user = ctx.user

    # Verify project access (CONTRIBUTOR+ required to spawn agents)
    await verify_entity_project_access(
        auth.session, ctx, request.project_id, required_role=ProjectRole.CONTRIBUTOR
    )

    client = await get_graph_client()
    manager = EntityManager(client, group_id=str(org.id))

    # Validate task exists if specified
    if request.task_id:
        try:
            entity = await manager.get(request.task_id)
            if not entity:
                raise HTTPException(status_code=404, detail=f"Task not found: {request.task_id}")
        except EntityNotFoundError:
            raise HTTPException(
                status_code=404, detail=f"Task not found: {request.task_id}"
            ) from None

    # Generate agent ID upfront
    agent_id = f"agent_{uuid4().hex[:12]}"

    # Derive name from prompt (first line, truncated)
    name = request.prompt.split("\n")[0][:100].strip() or "Agent"

    # Create agent record in graph (identity)
    record = AgentRecord(
        id=agent_id,
        name=name,
        organization_id=str(org.id),
        project_id=request.project_id,
        agent_type=request.agent_type,
        spawn_source=AgentSpawnSource.USER,
        task_id=request.task_id,
        status=AgentStatus.INITIALIZING,
        initial_prompt=request.prompt[:500],
        created_by=str(user.id),
    )
    await manager.create_direct(record)

    # Create AgentState in Postgres (operational state)
    agent_state = AgentState(
        organization_id=org.id,
        graph_agent_id=agent_id,
        task_id=request.task_id,
        status="initializing",
    )
    auth.session.add(agent_state)
    await auth.session.commit()

    try:
        # Get project repo_path if available
        repo_path: str | None = None
        try:
            project = await manager.get(request.project_id)
            if project and hasattr(project, "repo_path"):
                repo_path = getattr(project, "repo_path", None)
        except Exception:  # noqa: S110
            pass  # Fallback to cwd if project lookup fails

        # Enqueue execution in worker process
        await enqueue_agent_execution(
            agent_id=agent_id,
            org_id=str(org.id),
            project_id=request.project_id,
            prompt=request.prompt,
            agent_type=request.agent_type.value,
            task_id=request.task_id,
            created_by=str(user.id),
            create_worktree=request.create_worktree,
            repo_path=repo_path,
        )

        return SpawnAgentResponse(
            success=True,
            agent_id=agent_id,
            message=f"Agent {agent_id} queued for execution",
        )
    except Exception as e:
        log.exception("Failed to enqueue agent", error=str(e))
        # Try to clean up the records we created
        with contextlib.suppress(Exception):
            await manager.delete(agent_id)
        with contextlib.suppress(Exception):
            await auth.session.delete(agent_state)
            await auth.session.commit()
        raise HTTPException(status_code=500, detail="Failed to spawn agent") from e


@router.post("/{agent_id}/pause", response_model=AgentActionResponse)
async def pause_agent(
    agent_id: str,
    request: AgentActionRequest,
    auth: AuthSession = Depends(get_auth_session),
) -> AgentActionResponse:
    """Pause an agent's execution.

    Requires ownership or CONTRIBUTOR+ project access.
    Updates AgentState in Postgres (primary) and graph metadata (for legacy compatibility).
    """
    ctx = auth.ctx
    org = _require_org(ctx)

    client = await get_graph_client()
    manager = EntityManager(client, group_id=str(org.id))

    entity = await manager.get(agent_id)
    if not entity or entity.entity_type != EntityType.AGENT:
        raise HTTPException(status_code=404, detail=f"Agent not found: {agent_id}")

    # Check control permission
    await _check_agent_control_permission(ctx, auth.session, entity)

    # Get AgentState from Postgres (primary) or fall back to graph
    state_result = await auth.session.execute(
        select(AgentState).where(col(AgentState.graph_agent_id) == agent_id)
    )
    state = state_result.scalar_one_or_none()
    agent_status = state.status if state else (entity.metadata or {}).get("status", "initializing")

    if agent_status not in (AgentStatus.WORKING.value, AgentStatus.WAITING_APPROVAL.value):
        raise HTTPException(
            status_code=400,
            detail=f"Cannot pause agent in {agent_status} status",
        )

    # Update AgentState in Postgres (primary source of truth)
    if state:
        state.status = AgentStatus.PAUSED.value
        state.error_message = request.reason or "user_request"
        await auth.session.commit()

    # Also update graph metadata for legacy compatibility
    await manager.update(
        agent_id,
        {
            "status": AgentStatus.PAUSED.value,
            "paused_reason": request.reason or "user_request",
        },
    )

    return AgentActionResponse(
        success=True,
        agent_id=agent_id,
        action="pause",
        message=f"Agent {agent_id} paused",
    )


@router.post("/{agent_id}/resume", response_model=AgentActionResponse)
async def resume_agent(
    agent_id: str,
    auth: AuthSession = Depends(get_auth_session),
) -> AgentActionResponse:
    """Resume an agent from paused or terminal state.

    Allows continuing sessions even after completion, failure, or termination.
    The agent will be restarted from its last checkpoint.

    Requires ownership or CONTRIBUTOR+ project access.
    Updates AgentState in Postgres (primary) and graph metadata (for legacy compatibility).
    """
    from sibyl.jobs.queue import enqueue_agent_resume

    ctx = auth.ctx
    org = _require_org(ctx)

    client = await get_graph_client()
    manager = EntityManager(client, group_id=str(org.id))

    entity = await manager.get(agent_id)
    if not entity or entity.entity_type != EntityType.AGENT:
        raise HTTPException(status_code=404, detail=f"Agent not found: {agent_id}")

    # Check control permission
    await _check_agent_control_permission(ctx, auth.session, entity)

    # Get AgentState from Postgres (primary) or fall back to graph
    state_result = await auth.session.execute(
        select(AgentState).where(col(AgentState.graph_agent_id) == agent_id)
    )
    state = state_result.scalar_one_or_none()
    agent_status = state.status if state else (entity.metadata or {}).get("status", "initializing")

    # Allow resuming from paused or terminal states
    resumable_states = (
        AgentStatus.PAUSED.value,
        AgentStatus.COMPLETED.value,
        AgentStatus.FAILED.value,
        AgentStatus.TERMINATED.value,
    )
    if agent_status not in resumable_states:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot resume agent in {agent_status} status",
        )

    # Update AgentState in Postgres (primary source of truth)
    if state:
        state.status = AgentStatus.RESUMING.value
        state.error_message = None
        state.completed_at = None
        await auth.session.commit()

    # Also update graph metadata for legacy compatibility
    await manager.update(
        agent_id,
        {
            "status": AgentStatus.RESUMING.value,
            "paused_reason": None,
            "error": None,
            "completed_at": None,
        },
    )

    # Enqueue resume job for worker
    await enqueue_agent_resume(agent_id, str(org.id))

    return AgentActionResponse(
        success=True,
        agent_id=agent_id,
        action="resume",
        message=f"Agent {agent_id} resuming",
    )


@router.post("/{agent_id}/terminate", response_model=AgentActionResponse)
async def terminate_agent(
    agent_id: str,
    request: AgentActionRequest,
    auth: AuthSession = Depends(get_auth_session),
) -> AgentActionResponse:
    """Terminate an agent.

    Requires ownership or CONTRIBUTOR+ project access.
    Updates AgentState in Postgres (primary) and graph metadata (for legacy compatibility).

    This endpoint:
    1. Updates the agent status to 'terminated'
    2. Signals the worker to stop execution via Redis

    The worker checks for stop signals between message iterations and will
    gracefully terminate when it sees the signal.
    """
    from sibyl.api.pubsub import publish_event, request_agent_stop

    ctx = auth.ctx
    org = _require_org(ctx)

    client = await get_graph_client()
    manager = EntityManager(client, group_id=str(org.id))

    entity = await manager.get(agent_id)
    if not entity or entity.entity_type != EntityType.AGENT:
        raise HTTPException(status_code=404, detail=f"Agent not found: {agent_id}")

    # Check control permission
    await _check_agent_control_permission(ctx, auth.session, entity)

    # Get AgentState from Postgres (primary) or fall back to graph
    state_result = await auth.session.execute(
        select(AgentState).where(col(AgentState.graph_agent_id) == agent_id)
    )
    state = state_result.scalar_one_or_none()
    agent_status = state.status if state else (entity.metadata or {}).get("status", "initializing")

    terminal_states = (
        AgentStatus.COMPLETED.value,
        AgentStatus.FAILED.value,
        AgentStatus.TERMINATED.value,
    )
    if agent_status in terminal_states:
        raise HTTPException(
            status_code=400,
            detail=f"Agent already in terminal state: {agent_status}",
        )

    # Signal the worker to stop execution
    await request_agent_stop(agent_id)

    # Update AgentState in Postgres (primary source of truth)
    if state:
        state.status = AgentStatus.TERMINATED.value
        state.error_message = request.reason or "user_terminated"
        state.completed_at = utcnow_naive()
        await auth.session.commit()

    # Also update graph metadata for legacy compatibility
    await manager.update(
        agent_id,
        {
            "status": AgentStatus.TERMINATED.value,
            "error_message": request.reason or "user_terminated",
        },
    )

    # Broadcast termination via WebSocket for UI update
    await publish_event(
        "agent_status",
        {"agent_id": agent_id, "status": "terminated"},
        org_id=str(org.id),
    )

    return AgentActionResponse(
        success=True,
        agent_id=agent_id,
        action="terminate",
        message=f"Agent {agent_id} terminated",
    )


@router.get("/{agent_id}/messages", response_model=AgentMessagesResponse)
async def get_agent_messages(
    agent_id: str,
    limit: int = 500,
    auth: AuthSession = Depends(get_auth_session),
) -> AgentMessagesResponse:
    """Get conversation messages for an agent.

    Messages are read from the agent_messages Postgres table.
    These are summaries stored during execution - full tool outputs
    are only available via real-time WebSocket streaming.
    """
    ctx = auth.ctx
    org = _require_org(ctx)
    client = await get_graph_client()
    manager = EntityManager(client, group_id=str(org.id))

    # Verify agent exists
    entity = await manager.get(agent_id)
    if not entity or entity.entity_type != EntityType.AGENT:
        raise HTTPException(status_code=404, detail=f"Agent not found: {agent_id}")

    # Check view permission (creator, org admin, or project VIEWER+)
    await _check_agent_view_permission(ctx, auth.session, entity)

    # Query messages from Postgres (use injected session)
    messages: list[AgentMessage] = []
    result = await auth.session.execute(
        select(DbAgentMessage)
        .where(col(DbAgentMessage.agent_id) == agent_id)
        .where(col(DbAgentMessage.organization_id) == org.id)
        .order_by(col(DbAgentMessage.message_num))
        .limit(limit)
    )
    db_messages = result.scalars().all()

    for db_msg in db_messages:
        # Map DB enums to response enums
        role = MessageRole(db_msg.role.value)
        msg_type = MessageType(db_msg.type.value)

        # Build metadata from extra + indexed columns
        metadata = dict(db_msg.extra) if db_msg.extra else {}
        if db_msg.tool_id:
            metadata["tool_id"] = db_msg.tool_id
        if db_msg.parent_tool_use_id:
            metadata["parent_tool_use_id"] = db_msg.parent_tool_use_id

        messages.append(
            AgentMessage(
                id=str(db_msg.id),
                role=role,
                content=db_msg.content,
                timestamp=db_msg.created_at.isoformat() if db_msg.created_at else "",
                type=msg_type,
                metadata=metadata if metadata else None,
            )
        )

    return AgentMessagesResponse(
        agent_id=agent_id,
        messages=messages,
        total=len(messages),
    )


@router.post("/{agent_id}/messages", response_model=SendMessageResponse)
async def send_agent_message(
    agent_id: str,
    request: SendMessageRequest,
    auth: AuthSession = Depends(get_auth_session),
) -> SendMessageResponse:
    """Send a message to an agent.

    If the agent is in a terminal state (completed/failed/terminated),
    this will resume it using Claude's session management.

    Requires ownership or CONTRIBUTOR+ project access.
    """
    ctx = auth.ctx
    org = _require_org(ctx)

    client = await get_graph_client()
    manager = EntityManager(client, group_id=str(org.id))

    # Verify agent exists and has a session_id
    entity = await manager.get(agent_id)
    if not entity or entity.entity_type != EntityType.AGENT:
        raise HTTPException(status_code=404, detail=f"Agent not found: {agent_id}")

    # Check control permission
    await _check_agent_control_permission(ctx, auth.session, entity)

    # Get status from Postgres (primary) or fall back to graph metadata
    state_result = await auth.session.execute(
        select(AgentState).where(col(AgentState.graph_agent_id) == agent_id)
    )
    state = state_result.scalar_one_or_none()
    agent_meta = entity.metadata or {}
    agent_status = state.status if state else agent_meta.get("status", "initializing")

    # Check if agent can be resumed
    terminal_states = (
        AgentStatus.COMPLETED.value,
        AgentStatus.FAILED.value,
        AgentStatus.TERMINATED.value,
    )
    needs_resume = agent_status in terminal_states

    # Generate message ID
    msg_id = f"user-{datetime.now(UTC).timestamp():.0f}"

    # Store the user message for UI display (worker will also store agent responses)
    from sqlalchemy import func

    from sibyl.db.models import AgentMessage, AgentMessageRole, AgentMessageType

    # Get next message number
    result = await auth.session.execute(
        select(func.coalesce(func.max(AgentMessage.message_num), 0)).where(
            AgentMessage.agent_id == agent_id  # type: ignore[arg-type]
        )
    )
    max_num: int = result.scalar() or 0
    next_num = max_num + 1

    db_message = AgentMessage(
        agent_id=agent_id,
        organization_id=org.id,
        message_num=next_num,
        role=AgentMessageRole.user,
        type=AgentMessageType.text,
        content=request.content[:500],  # Summary only
        extra={"full_content": request.content} if len(request.content) > 500 else {},
    )
    auth.session.add(db_message)
    await auth.session.commit()

    log.info(
        "User message stored",
        agent_id=agent_id,
        message_id=msg_id,
        content_length=len(request.content),
    )

    # Resume the agent with the user's message
    if needs_resume:
        from sibyl.api.pubsub import publish_event
        from sibyl.jobs.queue import enqueue_agent_resume

        # Update AgentState in Postgres (primary source of truth)
        state_result = await auth.session.execute(
            select(AgentState).where(col(AgentState.graph_agent_id) == agent_id)
        )
        state = state_result.scalar_one_or_none()
        if state:
            state.status = AgentStatus.RESUMING.value
            state.error_message = None
            state.completed_at = None
            await auth.session.commit()

        # Update graph metadata
        await manager.update(
            agent_id,
            {
                "status": AgentStatus.RESUMING.value,
                "error": None,
                "completed_at": None,
            },
        )

        # Broadcast status change for immediate UI update
        await publish_event(
            "agent_status",
            {"agent_id": agent_id, "status": AgentStatus.RESUMING.value},
            org_id=str(org.id),
        )

        # Pass the message directly - Claude handles conversation history
        await enqueue_agent_resume(agent_id, str(org.id), prompt=request.content)

        log.info(
            "Agent resume enqueued",
            agent_id=agent_id,
            previous_status=agent_status,
        )

    return SendMessageResponse(
        success=True,
        message_id=msg_id,
    )


@router.get("/{agent_id}/workspace", response_model=AgentWorkspaceResponse)
async def get_agent_workspace(
    agent_id: str,
    auth: AuthSession = Depends(get_auth_session),
) -> AgentWorkspaceResponse:
    """Get the workspace state for an agent.

    Returns file changes and progress information from the latest checkpoint.
    """
    ctx = auth.ctx
    org = _require_org(ctx)
    client = await get_graph_client()
    manager = EntityManager(client, group_id=str(org.id))

    # Verify agent exists
    entity = await manager.get(agent_id)
    if not entity or entity.entity_type != EntityType.AGENT:
        raise HTTPException(status_code=404, detail=f"Agent not found: {agent_id}")

    # Check view permission (creator, org admin, or project VIEWER+)
    await _check_agent_view_permission(ctx, auth.session, entity)

    # Get latest checkpoint for this agent
    checkpoints = await manager.list_by_type(
        entity_type=EntityType.CHECKPOINT,
        limit=10,
    )

    agent_checkpoints = [
        c
        for c in checkpoints
        if c.entity_type == EntityType.CHECKPOINT and (c.metadata or {}).get("agent_id") == agent_id
    ]

    files: list[FileChange] = []
    current_step: str | None = None
    completed_steps: list[str] = []

    if agent_checkpoints:
        latest = max(
            agent_checkpoints, key=lambda c: c.created_at or datetime.min.replace(tzinfo=UTC)
        )
        meta = latest.metadata or {}
        current_step = meta.get("current_step")
        completed_steps = meta.get("completed_steps", [])

        # Parse files_modified into FileChange objects
        # Default to modified status; would need git status for accuracy
        files = [
            FileChange(path=path, status="modified", diff=None)
            for path in meta.get("files_modified", [])
        ]

    return AgentWorkspaceResponse(
        agent_id=agent_id,
        files=files,
        current_step=current_step,
        completed_steps=completed_steps,
    )


# =============================================================================
# Heartbeat & Health Monitoring
# =============================================================================


class HeartbeatRequest(BaseModel):
    """Request to record agent heartbeat."""

    tokens_delta: int = 0
    cost_delta: float = 0.0
    current_step: str | None = None


class HeartbeatResponse(BaseModel):
    """Response from heartbeat."""

    success: bool
    agent_id: str
    last_heartbeat: str


class AgentHealthStatus(StrEnum):
    """Agent health based on heartbeat recency."""

    HEALTHY = "healthy"
    STALE = "stale"
    UNRESPONSIVE = "unresponsive"


class AgentHealth(BaseModel):
    """Health status for a single agent."""

    agent_id: str
    agent_name: str
    status: str  # AgentHealthStatus value
    agent_status: str  # The agent's actual status (working, paused, etc.)
    last_heartbeat: str | None = None
    seconds_since_heartbeat: int | None = None
    project_id: str | None = None


class HealthOverviewResponse(BaseModel):
    """Overview of agent health across the system."""

    agents: list[AgentHealth]
    total: int
    healthy: int
    stale: int
    unresponsive: int


@router.post("/{agent_id}/heartbeat", response_model=HeartbeatResponse)
async def record_heartbeat(
    agent_id: str,
    request: HeartbeatRequest,
    auth: AuthSession = Depends(get_auth_session),
) -> HeartbeatResponse:
    """Record a heartbeat from an agent.

    Called periodically by running agents to indicate liveness.
    Updates AgentState in Postgres (primary) and graph metadata (for legacy compatibility).
    """
    ctx = auth.ctx
    org = _require_org(ctx)
    client = await get_graph_client()
    manager = EntityManager(client, group_id=str(org.id))

    entity = await manager.get(agent_id)
    if not entity or entity.entity_type != EntityType.AGENT:
        raise HTTPException(status_code=404, detail=f"Agent not found: {agent_id}")

    # Check control permission (heartbeats modify agent state)
    await _check_agent_control_permission(ctx, auth.session, entity)

    now = utcnow_naive()

    # Update AgentState in Postgres (primary source of truth)
    state_result = await auth.session.execute(
        select(AgentState).where(col(AgentState.graph_agent_id) == agent_id)
    )
    state = state_result.scalar_one_or_none()
    if state:
        state.last_heartbeat = now
        state.tokens_used += request.tokens_delta
        state.cost_usd += request.cost_delta
        if request.current_step:
            state.current_activity = request.current_step
        await auth.session.commit()

    # Also update graph metadata for legacy compatibility
    meta = entity.metadata or {}
    updates = {
        "last_heartbeat": now.isoformat(),
        "tokens_used": meta.get("tokens_used", 0) + request.tokens_delta,
        "cost_usd": meta.get("cost_usd", 0.0) + request.cost_delta,
    }
    if request.current_step:
        updates["current_step"] = request.current_step

    await manager.update(agent_id, updates)

    log.debug(
        "Agent heartbeat recorded",
        agent_id=agent_id,
        tokens_delta=request.tokens_delta,
        cost_delta=request.cost_delta,
    )

    return HeartbeatResponse(
        success=True,
        agent_id=agent_id,
        last_heartbeat=now.isoformat(),
    )


# Thresholds for health status (in seconds)
# Note: Claude API calls can take several minutes for complex tasks
HEARTBEAT_STALE_THRESHOLD = 120  # 2 minutes without heartbeat = stale
HEARTBEAT_UNRESPONSIVE_THRESHOLD = 600  # 10 minutes = unresponsive
STARTUP_GRACE_PERIOD = 60  # 60 seconds grace period for agent startup


def _calculate_health_status(
    state: AgentState | None,
    meta: dict,
    agent_status: str,
    now: datetime,
) -> tuple[AgentHealthStatus, str | None, int | None]:
    """Calculate agent health status from heartbeat data.

    Returns: (health_status, last_heartbeat_str, seconds_since_heartbeat)

    Handles startup grace period: newly started agents without heartbeat
    are considered healthy for the first 60 seconds.
    """
    active_states = (
        AgentStatus.WORKING.value,
        AgentStatus.WAITING_APPROVAL.value,
        AgentStatus.RESUMING.value,
    )

    # Get heartbeat from AgentState (primary) or graph metadata (fallback)
    last_heartbeat_dt: datetime | None = None
    last_heartbeat_str: str | None = None
    if state and state.last_heartbeat:
        last_heartbeat_dt = state.last_heartbeat
        last_heartbeat_str = state.last_heartbeat.isoformat()
    elif meta.get("last_heartbeat"):
        last_heartbeat_str = str(meta["last_heartbeat"])
        last_heartbeat_dt = datetime.fromisoformat(last_heartbeat_str).replace(tzinfo=None)

    # Get started_at for grace period calculation
    started_at_dt: datetime | None = None
    if state and state.started_at:
        started_at_dt = state.started_at
    elif meta.get("started_at"):
        started_at_str = str(meta["started_at"])
        try:  # noqa: SIM105
            started_at_dt = datetime.fromisoformat(started_at_str).replace(tzinfo=None)
        except ValueError:
            pass  # Malformed timestamp - leave as None

    seconds_since: int | None = None
    health_status = AgentHealthStatus.UNRESPONSIVE

    if last_heartbeat_dt:
        seconds_since = int((now - last_heartbeat_dt).total_seconds())

        if seconds_since <= HEARTBEAT_STALE_THRESHOLD:
            health_status = AgentHealthStatus.HEALTHY
        elif seconds_since <= HEARTBEAT_UNRESPONSIVE_THRESHOLD:
            health_status = AgentHealthStatus.STALE
        else:
            health_status = AgentHealthStatus.UNRESPONSIVE
    # No heartbeat ever recorded - check startup grace period
    elif started_at_dt:
        # Agent just started - give it time to initialize
        seconds_since_start = int((now - started_at_dt).total_seconds())
        if seconds_since_start <= STARTUP_GRACE_PERIOD:
            # Within startup grace period - agent is starting up
            health_status = AgentHealthStatus.HEALTHY
        elif agent_status == AgentStatus.INITIALIZING.value:
            # Still initializing but past grace period - getting slow
            health_status = AgentHealthStatus.STALE
        elif agent_status not in active_states:
            # Paused/completed agents without heartbeat are healthy
            health_status = AgentHealthStatus.HEALTHY
        # else: active but no heartbeat past grace = UNRESPONSIVE (default)
    elif agent_status not in active_states:
        # No started_at, no heartbeat, but not active = healthy
        health_status = AgentHealthStatus.HEALTHY

    return health_status, last_heartbeat_str, seconds_since


@router.get("/health/overview", response_model=HealthOverviewResponse)
async def get_health_overview(
    project_id: str | None = None,
    auth: AuthSession = Depends(get_auth_session),
) -> HealthOverviewResponse:
    """Get health overview for all running agents.

    Returns health status based on heartbeat recency:
    - healthy: heartbeat within last 2 minutes
    - stale: heartbeat 2-10 minutes ago
    - unresponsive: no heartbeat for 10+ minutes

    Results are filtered to agents the user can access (own agents + projects with VIEWER+).
    Uses AgentState from Postgres for heartbeat data (primary) with graph metadata fallback.
    """
    ctx = auth.ctx
    org = _require_org(ctx)
    client = await get_graph_client()
    manager = EntityManager(client, group_id=str(org.id))

    # Get accessible project IDs for permission filtering
    is_admin = ctx.org_role in (OrganizationRole.OWNER, OrganizationRole.ADMIN)
    accessible_projects: set[str] = set()
    if not is_admin:
        accessible_projects = await list_accessible_project_graph_ids(auth.session, ctx) or set()

    # Get all agents from graph and states from Postgres
    agents = await manager.list_by_type(entity_type=EntityType.AGENT, limit=100)
    state_result = await auth.session.execute(
        select(AgentState).where(col(AgentState.organization_id) == org.id)
    )
    states_by_id = {s.graph_agent_id: s for s in state_result.scalars().all()}

    now = utcnow_naive()
    agent_healths: list[AgentHealth] = []
    healthy, stale, unresponsive = 0, 0, 0
    terminal_states = (
        AgentStatus.COMPLETED.value,
        AgentStatus.FAILED.value,
        AgentStatus.TERMINATED.value,
    )
    user_id_str = str(ctx.user.id)

    for agent in agents:
        meta = agent.metadata or {}
        agent_project_id = meta.get("project_id")
        agent_created_by = meta.get("created_by") or agent.created_by
        state = states_by_id.get(agent.id)
        agent_status = state.status if state else meta.get("status", "initializing")

        # Access control and filtering
        if not is_admin:
            is_owner = agent_created_by == user_id_str
            if not (is_owner or (agent_project_id and agent_project_id in accessible_projects)):
                continue
        if project_id and agent_project_id != project_id:
            continue
        if agent_status in terminal_states:
            continue

        # Calculate health status
        health_status, last_heartbeat_str, seconds_since = _calculate_health_status(
            state, meta, agent_status, now
        )

        # Count health states
        if health_status == AgentHealthStatus.HEALTHY:
            healthy += 1
        elif health_status == AgentHealthStatus.STALE:
            stale += 1
        else:
            unresponsive += 1

        agent_healths.append(
            AgentHealth(
                agent_id=agent.id,
                agent_name=agent.name,
                status=health_status.value,
                agent_status=agent_status,
                last_heartbeat=last_heartbeat_str,
                seconds_since_heartbeat=seconds_since,
                project_id=agent_project_id,
            )
        )

    return HealthOverviewResponse(
        agents=agent_healths,
        total=len(agent_healths),
        healthy=healthy,
        stale=stale,
        unresponsive=unresponsive,
    )


# =============================================================================
# Activity Feed
# =============================================================================


class ActivityEventType(StrEnum):
    """Type of activity event."""

    AGENT_SPAWNED = "agent_spawned"
    AGENT_STARTED = "agent_started"
    AGENT_COMPLETED = "agent_completed"
    AGENT_FAILED = "agent_failed"
    AGENT_PAUSED = "agent_paused"
    AGENT_TERMINATED = "agent_terminated"
    AGENT_MESSAGE = "agent_message"
    APPROVAL_REQUESTED = "approval_requested"
    APPROVAL_RESPONDED = "approval_responded"


class ActivityEvent(BaseModel):
    """A single activity event."""

    id: str
    event_type: str
    agent_id: str
    agent_name: str | None = None
    project_id: str | None = None
    summary: str
    timestamp: str
    metadata: dict | None = None


class ActivityFeedResponse(BaseModel):
    """Activity feed response."""

    events: list[ActivityEvent]
    total: int


@router.get("/activity/feed", response_model=ActivityFeedResponse)
async def get_activity_feed(
    project_id: str | None = None,
    limit: int = 50,
    auth: AuthSession = Depends(get_auth_session),
) -> ActivityFeedResponse:
    """Get recent activity across all agents.

    Returns a chronological feed of agent events including status changes,
    messages, and approval activity.

    Results are filtered to agents the user can access (own agents + projects with VIEWER+).
    """
    ctx = auth.ctx
    org = _require_org(ctx)
    events: list[ActivityEvent] = []

    # Get accessible project IDs and agents for permission filtering
    is_admin = ctx.org_role in (OrganizationRole.OWNER, OrganizationRole.ADMIN)
    accessible_projects: set[str] = set()
    if not is_admin:
        accessible_projects = await list_accessible_project_graph_ids(auth.session, ctx) or set()

    user_id_str = str(ctx.user.id)

    # Get recent agent status changes from graph (need this first to filter messages)
    client = await get_graph_client()
    manager = EntityManager(client, group_id=str(org.id))

    agents = await manager.list_by_type(entity_type=EntityType.AGENT, limit=100)

    # Build set of accessible agent IDs and map for metadata
    accessible_agent_ids: set[str] = set()
    agent_meta_map: dict[str, dict] = {}

    for agent in agents:
        meta = agent.metadata or {}
        agent_project_id = meta.get("project_id")
        agent_created_by = meta.get("created_by") or agent.created_by

        # Check if user can access this agent
        if not is_admin:
            is_owner = agent_created_by == user_id_str
            has_project_access = agent_project_id and agent_project_id in accessible_projects
            if not (is_owner or has_project_access):
                continue

        accessible_agent_ids.add(agent.id)
        agent_meta_map[agent.id] = {
            "name": agent.name,
            "project_id": agent_project_id,
            "status": meta.get("status", "initializing"),
            "agent_type": meta.get("agent_type"),
            "created_at": agent.created_at,
            "completed_at": meta.get("completed_at"),
            "started_at": meta.get("started_at"),
        }

    # Get recent agent messages from Postgres (filtered to accessible agents)
    if accessible_agent_ids:
        # Build agent filter - use IN clause for accessible agents
        stmt = (
            select(DbAgentMessage)
            .where(col(DbAgentMessage.organization_id) == org.id)
            .where(col(DbAgentMessage.agent_id).in_(accessible_agent_ids))
            .order_by(col(DbAgentMessage.created_at).desc())
            .limit(limit)
        )
        result = await auth.session.execute(stmt)
        db_messages = result.scalars().all()

        for msg in db_messages:
            agent_info = agent_meta_map.get(msg.agent_id, {})

            # Filter by project if specified
            if project_id and agent_info.get("project_id") != project_id:
                continue

            # Summarize message content
            content_preview = msg.content[:100] + "..." if len(msg.content) > 100 else msg.content

            events.append(
                ActivityEvent(
                    id=str(msg.id),
                    event_type=ActivityEventType.AGENT_MESSAGE.value,
                    agent_id=msg.agent_id,
                    agent_name=agent_info.get("name"),
                    project_id=agent_info.get("project_id"),
                    summary=f"[{msg.role.value}] {content_preview}",
                    timestamp=msg.created_at.isoformat() if msg.created_at else "",
                    metadata={"type": msg.type.value},
                )
            )

    # Add agent status events from graph
    for agent_id, meta in agent_meta_map.items():
        agent_project_id = meta.get("project_id")

        # Filter by project if specified
        if project_id and agent_project_id != project_id:
            continue

        status = meta.get("status", "initializing")
        agent_name = meta.get("name", "Agent")

        # Map status to event type
        status_to_event = {
            "initializing": ActivityEventType.AGENT_SPAWNED,
            "working": ActivityEventType.AGENT_STARTED,
            "completed": ActivityEventType.AGENT_COMPLETED,
            "failed": ActivityEventType.AGENT_FAILED,
            "paused": ActivityEventType.AGENT_PAUSED,
            "terminated": ActivityEventType.AGENT_TERMINATED,
        }
        event_type = status_to_event.get(status, ActivityEventType.AGENT_SPAWNED)

        created_at = meta.get("created_at")
        timestamp = (
            meta.get("completed_at")
            or meta.get("started_at")
            or (created_at.isoformat() if created_at else None)
            or ""
        )

        events.append(
            ActivityEvent(
                id=f"{agent_id}-status",
                event_type=event_type.value,
                agent_id=agent_id,
                agent_name=agent_name,
                project_id=agent_project_id,
                summary=f"{agent_name} - {status}",
                timestamp=timestamp,
                metadata={"status": status, "agent_type": meta.get("agent_type")},
            )
        )

    # Sort by timestamp descending (most recent first)
    events.sort(key=lambda e: e.timestamp or "", reverse=True)

    return ActivityFeedResponse(events=events[:limit], total=len(events))


# =============================================================================
# Agent Management (Rename, Archive)
# =============================================================================


class RenameAgentRequest(BaseModel):
    """Request to rename an agent."""

    name: str


@router.patch("/{agent_id}/rename", response_model=AgentActionResponse)
async def rename_agent(
    agent_id: str,
    request: RenameAgentRequest,
    auth: AuthSession = Depends(get_auth_session),
) -> AgentActionResponse:
    """Rename an agent."""
    from sibyl.api.pubsub import publish_event

    ctx = auth.ctx
    org = _require_org(ctx)
    client = await get_graph_client()
    manager = EntityManager(client, group_id=str(org.id))

    entity = await manager.get(agent_id)
    if not entity or entity.entity_type != EntityType.AGENT:
        raise HTTPException(status_code=404, detail=f"Agent not found: {agent_id}")

    # Check control permission (creator, org admin, or project CONTRIBUTOR+)
    await _check_agent_control_permission(ctx, auth.session, entity)

    # Update agent name
    await manager.update(agent_id, {"name": request.name})

    # Publish event for real-time UI updates
    await publish_event(
        "agent_status",
        {
            "agent_id": agent_id,
            "name": request.name,
            "timestamp": datetime.now(UTC).isoformat(),
        },
    )

    log.info("Agent renamed", agent_id=agent_id, new_name=request.name)

    return AgentActionResponse(
        success=True,
        agent_id=agent_id,
        action="rename",
        message=f"Agent renamed to '{request.name}'",
    )


@router.post("/{agent_id}/archive", response_model=AgentActionResponse)
async def archive_agent(
    agent_id: str,
    auth: AuthSession = Depends(get_auth_session),
) -> AgentActionResponse:
    """Archive an agent (soft delete).

    Archived agents stay in the graph but are hidden from UI by default.
    Only terminal states (completed, failed, terminated) can be archived.
    """
    from sibyl.api.pubsub import publish_event

    ctx = auth.ctx
    org = _require_org(ctx)
    client = await get_graph_client()
    manager = EntityManager(client, group_id=str(org.id))

    entity = await manager.get(agent_id)
    if not entity or entity.entity_type != EntityType.AGENT:
        raise HTTPException(status_code=404, detail=f"Agent not found: {agent_id}")

    # Check control permission (creator, org admin, or project CONTRIBUTOR+)
    await _check_agent_control_permission(ctx, auth.session, entity)

    agent_status = (entity.metadata or {}).get("status", "initializing")
    terminal_states = (
        AgentStatus.COMPLETED.value,
        AgentStatus.FAILED.value,
        AgentStatus.TERMINATED.value,
    )
    if agent_status not in terminal_states:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot archive agent in {agent_status} status. "
            "Only terminal states can be archived.",
        )

    # Mark as archived
    await manager.update(agent_id, {"archived": True, "archived_at": datetime.now(UTC).isoformat()})

    # Publish event for real-time UI updates
    await publish_event(
        "agent_status",
        {
            "agent_id": agent_id,
            "archived": True,
            "timestamp": datetime.now(UTC).isoformat(),
        },
    )

    log.info("Agent archived", agent_id=agent_id, agent_name=entity.name)

    return AgentActionResponse(
        success=True,
        agent_id=agent_id,
        action="archive",
        message="Agent archived successfully",
    )


# =============================================================================
# Helpers
# =============================================================================


def _entity_to_agent_response(
    entity: "Entity",
    state: AgentState | None = None,
) -> AgentResponse:
    """Convert Entity to AgentResponse, merging with AgentState.

    Identity (name, type, project) comes from graph entity metadata.
    Operational state (status, heartbeat, metrics) comes from AgentState if available.
    Falls back to graph metadata for agents without AgentState (legacy).
    """
    meta = entity.metadata or {}

    # Use AgentState for operational fields if available
    if state:
        status = state.status
        last_heartbeat = state.last_heartbeat.isoformat() if state.last_heartbeat else None
        started_at = state.started_at.isoformat() if state.started_at else None
        completed_at = state.completed_at.isoformat() if state.completed_at else None
        tokens_used = state.tokens_used
        cost_usd = state.cost_usd
        error_message = state.error_message
    else:
        # Fall back to graph metadata for legacy agents
        status = meta.get("status", "initializing")
        last_heartbeat = meta.get("last_heartbeat")
        started_at = meta.get("started_at")
        completed_at = meta.get("completed_at")
        tokens_used = meta.get("tokens_used", 0)
        cost_usd = meta.get("cost_usd", 0.0)
        error_message = meta.get("error_message") or meta.get("paused_reason")

    return AgentResponse(
        id=entity.id,
        name=entity.name,
        agent_type=meta.get("agent_type", "general"),
        status=status,
        task_id=meta.get("task_id"),
        project_id=meta.get("project_id"),
        created_by=meta.get("created_by") or entity.created_by,
        spawn_source=meta.get("spawn_source"),
        started_at=started_at,
        completed_at=completed_at,
        last_heartbeat=last_heartbeat,
        tokens_used=tokens_used,
        cost_usd=cost_usd,
        worktree_path=meta.get("worktree_path"),
        worktree_branch=meta.get("worktree_branch"),
        error_message=error_message,
        tags=meta.get("tags", []),
    )
