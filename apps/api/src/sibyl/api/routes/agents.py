"""Agent management endpoints.

REST API for managing AI agents via the AgentOrchestrator.
"""

from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from sibyl_core.models.entities import Entity
from uuid import uuid4

import structlog
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlmodel import col

from sibyl.auth.dependencies import get_current_organization, get_current_user, require_org_role
from sibyl.db import AgentMessage as DbAgentMessage, get_session
from sibyl.db.models import Organization, OrganizationRole, User
from sibyl_core.graph.client import get_graph_client
from sibyl_core.graph.entities import EntityManager
from sibyl_core.models import (
    AgentCheckpoint,
    AgentStatus,
    AgentType,
    EntityType,
)

log = structlog.get_logger()
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
    limit: int = 50,
    user: User = Depends(get_current_user),
    org: Organization = Depends(get_current_organization),
) -> AgentListResponse:
    """List agents for the organization.

    Args:
        project: Filter by project ID
        status: Filter by agent status
        agent_type: Filter by agent type
        all_users: If True, show all agents in the org (default: only user's agents)
        limit: Maximum results
        user: Current user
        org: Current organization
    """
    client = await get_graph_client()
    manager = EntityManager(client, group_id=str(org.id))

    # Get all agents (returns generic Entity objects)
    results = await manager.list_by_type(
        entity_type=EntityType.AGENT,
        limit=limit * 2,  # Fetch extra for filtering
    )

    # Filter by user (default behavior - show only user's agents)
    agents = list(results)
    if not all_users:
        user_id_str = str(user.id)
        agents = [
            a
            for a in agents
            if (a.metadata or {}).get("created_by") == user_id_str or a.created_by == user_id_str
        ]

    # Apply filters (agents are generic Entity objects - fields in metadata)
    if project:
        agents = [a for a in agents if (a.metadata or {}).get("project_id") == project]
    if status:
        agents = [a for a in agents if (a.metadata or {}).get("status") == status.value]
    if agent_type:
        agents = [a for a in agents if (a.metadata or {}).get("agent_type") == agent_type.value]

    # Calculate stats
    by_status: dict[str, int] = {}
    by_type: dict[str, int] = {}
    for agent in agents:
        s = (agent.metadata or {}).get("status") or "initializing"
        by_status[s] = by_status.get(s, 0) + 1
        t = (agent.metadata or {}).get("agent_type") or "general"
        by_type[t] = by_type.get(t, 0) + 1

    return AgentListResponse(
        agents=[_entity_to_agent_response(a) for a in agents[:limit]],
        total=len(agents),
        by_status=by_status,
        by_type=by_type,
    )


@router.get("/{agent_id}", response_model=AgentResponse)
async def get_agent(
    agent_id: str,
    org: Organization = Depends(get_current_organization),
) -> AgentResponse:
    """Get a specific agent by ID."""
    client = await get_graph_client()
    manager = EntityManager(client, group_id=str(org.id))

    entity = await manager.get(agent_id)
    if not entity or entity.entity_type != EntityType.AGENT:
        raise HTTPException(status_code=404, detail=f"Agent not found: {agent_id}")

    return _entity_to_agent_response(entity)


@router.post("", response_model=SpawnAgentResponse)
async def spawn_agent(
    request: SpawnAgentRequest,
    user: User = Depends(get_current_user),
    org: Organization = Depends(get_current_organization),
) -> SpawnAgentResponse:
    """Spawn a new agent and enqueue execution.

    Validates the request and enqueues agent execution in the worker process.
    The worker creates the agent record and handles execution.
    """
    from sibyl.jobs.queue import enqueue_agent_execution

    # Validate task exists if specified
    if request.task_id:
        client = await get_graph_client()
        manager = EntityManager(client, group_id=str(org.id))
        entity = await manager.get(request.task_id)
        if not entity:
            raise HTTPException(status_code=404, detail=f"Task not found: {request.task_id}")

    # Generate agent ID upfront so we can return it immediately
    agent_id = f"agent_{uuid4().hex[:12]}"

    try:
        # Enqueue agent creation + execution in worker process
        await enqueue_agent_execution(
            agent_id=agent_id,
            org_id=str(org.id),
            project_id=request.project_id,
            prompt=request.prompt,
            agent_type=request.agent_type.value,
            task_id=request.task_id,
            created_by=str(user.id),
        )

        return SpawnAgentResponse(
            success=True,
            agent_id=agent_id,
            message=f"Agent {agent_id} queued for execution",
        )
    except Exception as e:
        log.exception("Failed to enqueue agent", error=str(e))
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/{agent_id}/pause", response_model=AgentActionResponse)
async def pause_agent(
    agent_id: str,
    request: AgentActionRequest,
    org: Organization = Depends(get_current_organization),
) -> AgentActionResponse:
    """Pause an agent's execution."""
    client = await get_graph_client()
    manager = EntityManager(client, group_id=str(org.id))

    entity = await manager.get(agent_id)
    if not entity or entity.entity_type != EntityType.AGENT:
        raise HTTPException(status_code=404, detail=f"Agent not found: {agent_id}")

    agent_status = (entity.metadata or {}).get("status", "initializing")
    if agent_status not in (AgentStatus.WORKING.value, AgentStatus.WAITING_APPROVAL.value):
        raise HTTPException(
            status_code=400,
            detail=f"Cannot pause agent in {agent_status} status",
        )

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
    org: Organization = Depends(get_current_organization),
) -> AgentActionResponse:
    """Resume a paused agent."""
    client = await get_graph_client()
    manager = EntityManager(client, group_id=str(org.id))

    entity = await manager.get(agent_id)
    if not entity or entity.entity_type != EntityType.AGENT:
        raise HTTPException(status_code=404, detail=f"Agent not found: {agent_id}")

    agent_status = (entity.metadata or {}).get("status", "initializing")
    if agent_status != AgentStatus.PAUSED.value:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot resume agent in {agent_status} status",
        )

    await manager.update(
        agent_id,
        {
            "status": AgentStatus.RESUMING.value,
            "paused_reason": None,
        },
    )

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
    org: Organization = Depends(get_current_organization),
) -> AgentActionResponse:
    """Terminate an agent."""
    client = await get_graph_client()
    manager = EntityManager(client, group_id=str(org.id))

    entity = await manager.get(agent_id)
    if not entity or entity.entity_type != EntityType.AGENT:
        raise HTTPException(status_code=404, detail=f"Agent not found: {agent_id}")

    agent_status = (entity.metadata or {}).get("status", "initializing")
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

    await manager.update(
        agent_id,
        {
            "status": AgentStatus.TERMINATED.value,
            "error_message": request.reason or "user_terminated",
        },
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
    limit: int = 100,
    org: Organization = Depends(get_current_organization),
) -> AgentMessagesResponse:
    """Get conversation messages for an agent.

    Messages are read from the agent_messages Postgres table.
    These are summaries stored during execution - full tool outputs
    are only available via real-time WebSocket streaming.
    """
    client = await get_graph_client()
    manager = EntityManager(client, group_id=str(org.id))

    # Verify agent exists
    entity = await manager.get(agent_id)
    if not entity or entity.entity_type != EntityType.AGENT:
        raise HTTPException(status_code=404, detail=f"Agent not found: {agent_id}")

    # Query messages from Postgres
    messages: list[AgentMessage] = []
    async with get_session() as session:
        result = await session.execute(
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

            messages.append(
                AgentMessage(
                    id=str(db_msg.id),
                    role=role,
                    content=db_msg.content,
                    timestamp=db_msg.created_at.isoformat() if db_msg.created_at else "",
                    type=msg_type,
                    metadata=db_msg.extra if db_msg.extra else None,
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
    org: Organization = Depends(get_current_organization),
) -> SendMessageResponse:
    """Send a message to an agent.

    The message will be added to the agent's message queue for processing.
    Note: This requires the agent to be in a state that accepts messages.
    """
    client = await get_graph_client()
    manager = EntityManager(client, group_id=str(org.id))

    # Verify agent exists
    entity = await manager.get(agent_id)
    if not entity or entity.entity_type != EntityType.AGENT:
        raise HTTPException(status_code=404, detail=f"Agent not found: {agent_id}")

    # Check agent is in a state that can accept messages
    agent_status = (entity.metadata or {}).get("status", "initializing")
    terminal_states = (
        AgentStatus.COMPLETED.value,
        AgentStatus.FAILED.value,
        AgentStatus.TERMINATED.value,
    )
    if agent_status in terminal_states:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot send message to agent in {agent_status} status",
        )

    # Generate message ID
    msg_id = f"user-{datetime.now(UTC).timestamp():.0f}"

    # Get or create checkpoint to store the message
    checkpoints = await manager.list_by_type(
        entity_type=EntityType.CHECKPOINT,
        limit=10,
    )

    agent_checkpoints = [
        c
        for c in checkpoints
        if c.entity_type == EntityType.CHECKPOINT and (c.metadata or {}).get("agent_id") == agent_id
    ]

    if agent_checkpoints:
        latest = max(
            agent_checkpoints, key=lambda c: c.created_at or datetime.min.replace(tzinfo=UTC)
        )
        # Add user message to conversation history (history is in metadata for Entity objects)
        current_history = (latest.metadata or {}).get("conversation_history", [])
        new_msg = {
            "role": "user",
            "content": request.content,
            "timestamp": datetime.now(UTC).isoformat(),
            "type": "text",
        }
        updated_history = [*current_history, new_msg]
        await manager.update(latest.id, {"conversation_history": updated_history})
    else:
        # Create initial checkpoint with user message (use create_direct to skip LLM extraction)
        checkpoint_id = f"chkpt_{uuid4().hex[:12]}"
        checkpoint = AgentCheckpoint(
            id=checkpoint_id,
            name=f"checkpoint-{agent_id[-8:]}",
            agent_id=agent_id,
            session_id="user-initiated",
            conversation_history=[
                {
                    "role": "user",
                    "content": request.content,
                    "timestamp": datetime.now(UTC).isoformat(),
                    "type": "text",
                }
            ],
        )
        await manager.create_direct(checkpoint)

    log.info(
        "User message sent to agent",
        agent_id=agent_id,
        message_id=msg_id,
        content_length=len(request.content),
    )

    return SendMessageResponse(
        success=True,
        message_id=msg_id,
    )


@router.get("/{agent_id}/workspace", response_model=AgentWorkspaceResponse)
async def get_agent_workspace(
    agent_id: str,
    org: Organization = Depends(get_current_organization),
) -> AgentWorkspaceResponse:
    """Get the workspace state for an agent.

    Returns file changes and progress information from the latest checkpoint.
    """
    client = await get_graph_client()
    manager = EntityManager(client, group_id=str(org.id))

    # Verify agent exists
    entity = await manager.get(agent_id)
    if not entity or entity.entity_type != EntityType.AGENT:
        raise HTTPException(status_code=404, detail=f"Agent not found: {agent_id}")

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
# Helpers
# =============================================================================


def _entity_to_agent_response(entity: "Entity") -> AgentResponse:
    """Convert Entity to AgentResponse by extracting attributes from metadata.

    Agents stored via create_direct() have their fields in metadata.
    """
    meta = entity.metadata or {}
    return AgentResponse(
        id=entity.id,
        name=entity.name,
        agent_type=meta.get("agent_type", "general"),
        status=meta.get("status", "initializing"),
        task_id=meta.get("task_id"),
        project_id=meta.get("project_id"),
        created_by=meta.get("created_by") or entity.created_by,
        spawn_source=meta.get("spawn_source"),
        started_at=meta.get("started_at"),
        completed_at=meta.get("completed_at"),
        last_heartbeat=meta.get("last_heartbeat"),
        tokens_used=meta.get("tokens_used", 0),
        cost_usd=meta.get("cost_usd", 0.0),
        worktree_path=meta.get("worktree_path"),
        worktree_branch=meta.get("worktree_branch"),
        error_message=meta.get("error_message") or meta.get("paused_reason"),
    )
