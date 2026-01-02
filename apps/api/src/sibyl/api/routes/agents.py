"""Agent management endpoints.

REST API for managing AI agents via the AgentOrchestrator.
"""

from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from sibyl.agents import AgentInstance
from uuid import uuid4

import structlog
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel

from sibyl.auth.dependencies import get_current_organization, get_current_user, require_org_role
from sibyl.db.models import Organization, OrganizationRole, User
from sibyl_core.graph.client import get_graph_client
from sibyl_core.graph.entities import EntityManager
from sibyl_core.models import (
    AgentCheckpoint,
    AgentRecord,
    AgentSpawnSource,
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

    # Get all agents
    results = await manager.list_by_type(
        entity_type=EntityType.AGENT,
        limit=limit * 2,  # Fetch extra for filtering
    )

    agents = [r for r in results if isinstance(r, AgentRecord)]

    # Filter by user (default behavior - show only user's agents)
    if not all_users:
        agents = [a for a in agents if a.created_by == str(user.id)]

    # Apply filters
    if project:
        agents = [a for a in agents if a.project_id == project]
    if status:
        agents = [a for a in agents if a.status == status]
    if agent_type:
        agents = [a for a in agents if a.agent_type == agent_type]

    # Calculate stats
    by_status: dict[str, int] = {}
    by_type: dict[str, int] = {}
    for agent in agents:
        s = agent.status.value
        by_status[s] = by_status.get(s, 0) + 1
        t = agent.agent_type.value
        by_type[t] = by_type.get(t, 0) + 1

    return AgentListResponse(
        agents=[_agent_to_response(a) for a in agents[:limit]],
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
    if not entity or not isinstance(entity, AgentRecord):
        raise HTTPException(status_code=404, detail=f"Agent not found: {agent_id}")

    return _agent_to_response(entity)


@router.post("", response_model=SpawnAgentResponse)
async def spawn_agent(
    request: SpawnAgentRequest,
    background_tasks: BackgroundTasks,
    user: User = Depends(get_current_user),
    org: Organization = Depends(get_current_organization),
) -> SpawnAgentResponse:
    """Spawn a new agent and start execution.

    Creates an agent record and starts background execution using Claude SDK.
    The agent will stream messages to its checkpoint for real-time updates.
    """
    from sibyl.agents import AgentRunner, WorktreeManager

    client = await get_graph_client()
    manager = EntityManager(client, group_id=str(org.id))
    user_id = str(user.id)

    # Get task if specified
    task = None
    if request.task_id:
        from sibyl_core.models import Task

        entity = await manager.get(request.task_id)
        if entity and isinstance(entity, Task):
            task = entity

    # Create worktree manager and agent runner
    worktree_manager = WorktreeManager(
        entity_manager=manager,
        org_id=str(org.id),
        project_id=request.project_id,
        repo_path=".",  # Would come from project config
    )

    runner = AgentRunner(
        entity_manager=manager,
        worktree_manager=worktree_manager,
        org_id=str(org.id),
        project_id=request.project_id,
    )

    try:
        instance = await runner.spawn(
            prompt=request.prompt,
            agent_type=request.agent_type,
            task=task,
            spawn_source=AgentSpawnSource.USER,
            create_worktree=False,  # Don't create worktree via API
            enable_approvals=True,
        )

        # Associate agent with current user
        await manager.update(instance.id, {"created_by": user_id})

        # Start agent execution in background
        background_tasks.add_task(
            _run_agent_execution,
            instance=instance,
            manager=manager,
            org_id=str(org.id),
        )

        return SpawnAgentResponse(
            success=True,
            agent_id=instance.id,
            message=f"Agent {instance.id} spawned and starting execution",
        )
    except Exception as e:
        log.exception("Failed to spawn agent", error=str(e))
        raise HTTPException(status_code=500, detail=str(e)) from e


async def _run_agent_execution(
    instance: "AgentInstance",
    manager: EntityManager,
    org_id: str,
) -> None:
    """Run agent execution in background.

    Only stores summary checkpoint at completion, not every message.
    WebSocket events are emitted for status changes only.

    Args:
        instance: The spawned AgentInstance to execute
        manager: EntityManager for persisting state
        org_id: Organization ID for WebSocket broadcasts
    """
    from sibyl.api.pubsub import publish_event

    agent_id = instance.id
    log.info("Starting agent execution", agent_id=agent_id)

    # Track execution state (in memory only until completion)
    message_count = 0
    session_id = ""
    last_content = ""
    tool_calls: list[str] = []

    try:
        # Execute agent - process messages without storing each one
        async for message in instance.execute():
            message_count += 1
            msg_content = str(getattr(message, "content", ""))
            msg_class = type(message).__name__

            # Track session ID
            if sid := getattr(message, "session_id", None):
                session_id = sid

            # Track tool calls for summary
            if "Tool" in msg_class and msg_content:
                tool_name = msg_content.split("(")[0] if "(" in msg_content else msg_content[:50]
                tool_calls.append(tool_name)

            # Keep last meaningful content for summary
            if msg_content and "Result" not in msg_class:
                last_content = msg_content[:500]

        # Create checkpoint only on completion (with summary, not full history)
        checkpoint_id = f"chkpt_{uuid4().hex[:12]}"
        summary = f"Completed {message_count} turns. Tools: {', '.join(tool_calls[-5:]) or 'none'}"
        checkpoint = AgentCheckpoint(
            id=checkpoint_id,
            name=f"checkpoint-{agent_id[-8:]}",
            agent_id=agent_id,
            session_id=session_id,
            conversation_history=[
                {
                    "role": "user",
                    "content": instance.initial_prompt,
                    "timestamp": datetime.now(UTC).isoformat(),
                    "type": "text",
                },
                {
                    "role": "system",
                    "content": summary,
                    "timestamp": datetime.now(UTC).isoformat(),
                    "type": "text",
                },
            ],
            current_step=last_content[:200] if last_content else None,
        )
        await manager.create_direct(checkpoint)

        # Update agent status to completed
        await manager.update(
            agent_id,
            {
                "status": AgentStatus.COMPLETED.value,
                "conversation_turns": message_count,
            },
        )

        # Emit completion via WebSocket
        await publish_event(
            "agent_status",
            {"agent_id": agent_id, "status": "completed", "turns": message_count},
            org_id=org_id,
        )
        log.info("Agent execution completed", agent_id=agent_id, turns=message_count)

    except Exception as e:
        log.exception("Agent execution failed", agent_id=agent_id, error=str(e))
        # Update agent status to failed
        await manager.update(
            agent_id,
            {
                "status": AgentStatus.FAILED.value,
                "error_message": str(e),
            },
        )
        # Emit failure via WebSocket
        await publish_event(
            "agent_status",
            {"agent_id": agent_id, "status": "failed", "error": str(e)},
            org_id=org_id,
        )


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
    if not entity or not isinstance(entity, AgentRecord):
        raise HTTPException(status_code=404, detail=f"Agent not found: {agent_id}")

    if entity.status not in (AgentStatus.WORKING, AgentStatus.WAITING_APPROVAL):
        raise HTTPException(
            status_code=400,
            detail=f"Cannot pause agent in {entity.status} status",
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
    if not entity or not isinstance(entity, AgentRecord):
        raise HTTPException(status_code=404, detail=f"Agent not found: {agent_id}")

    if entity.status != AgentStatus.PAUSED:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot resume agent in {entity.status} status",
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
    if not entity or not isinstance(entity, AgentRecord):
        raise HTTPException(status_code=404, detail=f"Agent not found: {agent_id}")

    terminal_states = (AgentStatus.COMPLETED, AgentStatus.FAILED, AgentStatus.TERMINATED)
    if entity.status in terminal_states:
        raise HTTPException(
            status_code=400,
            detail=f"Agent already in terminal state: {entity.status}",
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

    Messages are retrieved from the agent's latest checkpoint.
    """
    client = await get_graph_client()
    manager = EntityManager(client, group_id=str(org.id))

    # Verify agent exists
    entity = await manager.get(agent_id)
    if not entity or not isinstance(entity, AgentRecord):
        raise HTTPException(status_code=404, detail=f"Agent not found: {agent_id}")

    # Get latest checkpoint for this agent
    checkpoints = await manager.list_by_type(
        entity_type=EntityType.CHECKPOINT,
        limit=10,
    )

    # Find checkpoints for this agent
    agent_checkpoints = [
        c for c in checkpoints if isinstance(c, AgentCheckpoint) and c.agent_id == agent_id
    ]

    messages: list[AgentMessage] = []
    if agent_checkpoints:
        # Sort by created_at descending and get the latest
        latest = max(
            agent_checkpoints, key=lambda c: c.created_at or datetime.min.replace(tzinfo=UTC)
        )

        # Convert conversation history to messages
        for i, msg in enumerate(latest.conversation_history[-limit:]):
            role_str = msg.get("role", "agent")
            role = MessageRole.AGENT
            if role_str == "user":
                role = MessageRole.USER
            elif role_str == "system":
                role = MessageRole.SYSTEM

            msg_type = MessageType.TEXT
            if msg.get("type") == "tool_call":
                msg_type = MessageType.TOOL_CALL
            elif msg.get("type") == "tool_result":
                msg_type = MessageType.TOOL_RESULT
            elif msg.get("type") == "error":
                msg_type = MessageType.ERROR

            messages.append(
                AgentMessage(
                    id=f"msg-{agent_id[-8:]}-{i}",
                    role=role,
                    content=msg.get("content", ""),
                    timestamp=msg.get("timestamp", datetime.now(UTC).isoformat()),
                    type=msg_type,
                    metadata=msg.get("metadata"),
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
    if not entity or not isinstance(entity, AgentRecord):
        raise HTTPException(status_code=404, detail=f"Agent not found: {agent_id}")

    # Check agent is in a state that can accept messages
    terminal_states = (AgentStatus.COMPLETED, AgentStatus.FAILED, AgentStatus.TERMINATED)
    if entity.status in terminal_states:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot send message to agent in {entity.status} status",
        )

    # Generate message ID
    msg_id = f"user-{datetime.now(UTC).timestamp():.0f}"

    # Get or create checkpoint to store the message
    checkpoints = await manager.list_by_type(
        entity_type=EntityType.CHECKPOINT,
        limit=10,
    )

    agent_checkpoints = [
        c for c in checkpoints if isinstance(c, AgentCheckpoint) and c.agent_id == agent_id
    ]

    if agent_checkpoints:
        latest = max(
            agent_checkpoints, key=lambda c: c.created_at or datetime.min.replace(tzinfo=UTC)
        )
        # Add user message to conversation history
        new_msg = {
            "role": "user",
            "content": request.content,
            "timestamp": datetime.now(UTC).isoformat(),
            "type": "text",
        }
        updated_history = [*latest.conversation_history, new_msg]
        await manager.update(latest.id, {"conversation_history": updated_history})
    else:
        # Create initial checkpoint with user message
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
        await manager.create(checkpoint)

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
    if not entity or not isinstance(entity, AgentRecord):
        raise HTTPException(status_code=404, detail=f"Agent not found: {agent_id}")

    # Get latest checkpoint for this agent
    checkpoints = await manager.list_by_type(
        entity_type=EntityType.CHECKPOINT,
        limit=10,
    )

    agent_checkpoints = [
        c for c in checkpoints if isinstance(c, AgentCheckpoint) and c.agent_id == agent_id
    ]

    files: list[FileChange] = []
    current_step: str | None = None
    completed_steps: list[str] = []

    if agent_checkpoints:
        latest = max(
            agent_checkpoints, key=lambda c: c.created_at or datetime.min.replace(tzinfo=UTC)
        )
        current_step = latest.current_step
        completed_steps = latest.completed_steps

        # Parse files_modified into FileChange objects
        # Default to modified status; would need git status for accuracy
        files = [
            FileChange(path=path, status="modified", diff=None) for path in latest.files_modified
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


def _agent_to_response(agent: AgentRecord) -> AgentResponse:
    """Convert AgentRecord to response model."""
    return AgentResponse(
        id=agent.id,
        name=agent.name,
        agent_type=agent.agent_type.value,
        status=agent.status.value,
        task_id=agent.task_id,
        project_id=agent.project_id,
        created_by=agent.created_by,
        spawn_source=agent.spawn_source.value if agent.spawn_source else None,
        started_at=agent.started_at.isoformat() if agent.started_at else None,
        completed_at=agent.completed_at.isoformat() if agent.completed_at else None,
        last_heartbeat=agent.last_heartbeat.isoformat() if agent.last_heartbeat else None,
        tokens_used=agent.tokens_used or 0,
        cost_usd=agent.cost_usd or 0.0,
        worktree_path=agent.worktree_path,
        worktree_branch=agent.worktree_branch,
        error_message=agent.paused_reason,  # Use paused_reason for error context
    )
