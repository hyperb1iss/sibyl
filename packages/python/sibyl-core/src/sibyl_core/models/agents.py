"""Agent models for the Agent Harness system.

This module contains models for AI agent orchestration, including:
- AgentRecord: Persistent agent state for session management
- WorktreeRecord: Git worktree tracking for agent isolation
- ApprovalRecord: Human-in-the-loop approval requests
- AgentCheckpoint: Session state for resume capability
"""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import Field, model_validator

from sibyl_core.models.entities import Entity, EntityType


class AgentStatus(StrEnum):
    """Agent lifecycle states (persisted)."""

    INITIALIZING = "initializing"  # Setting up worktree and environment
    WORKING = "working"  # Actively executing tasks
    PAUSED = "paused"  # User-initiated pause
    WAITING_APPROVAL = "waiting_approval"  # Blocked on human approval
    WAITING_DEPENDENCY = "waiting_dependency"  # Blocked on another task
    RESUMING = "resuming"  # Recovering after restart
    COMPLETED = "completed"  # Finished successfully
    FAILED = "failed"  # Error state
    TERMINATED = "terminated"  # User stopped the agent


class AgentType(StrEnum):
    """Specialized agent types."""

    GENERAL = "general"  # General-purpose agent
    PLANNER = "planner"  # Breaks features into tasks
    IMPLEMENTER = "implementer"  # Implements code changes
    TESTER = "tester"  # Writes and runs tests
    REVIEWER = "reviewer"  # Reviews code for quality
    INTEGRATOR = "integrator"  # Merges worktrees and resolves conflicts
    ORCHESTRATOR = "orchestrator"  # Coordinates other agents


class AgentSpawnSource(StrEnum):
    """How an agent was created."""

    ORCHESTRATOR = "orchestrator"  # Spawned by orchestrator for task
    USER = "user"  # User-initiated via UI/CLI


class AgentRecord(Entity):
    """Persistent agent state stored in the knowledge graph.

    Tracks everything needed to monitor, pause, resume, and recover agents.
    """

    entity_type: EntityType = EntityType.AGENT

    # Identity
    agent_type: AgentType = Field(default=AgentType.GENERAL, description="Agent specialization")
    spawn_source: AgentSpawnSource = Field(
        default=AgentSpawnSource.USER, description="How agent was created"
    )

    # Organization context (organization_id inherited from Entity, validated below)
    project_id: str = Field(..., description="Project UUID")
    created_by: str | None = Field(default=None, description="User ID who spawned this agent")

    # Assignment
    task_id: str | None = Field(default=None, description="Assigned task UUID")
    worktree_path: str | None = Field(default=None, description="Path to isolated worktree")
    worktree_branch: str | None = Field(default=None, description="Git branch name")

    # Lifecycle
    status: AgentStatus = Field(default=AgentStatus.INITIALIZING, description="Current state")
    started_at: datetime | None = Field(default=None, description="When agent started working")
    last_heartbeat: datetime | None = Field(default=None, description="Last heartbeat timestamp")
    completed_at: datetime | None = Field(default=None, description="When agent finished")
    paused_reason: str | None = Field(default=None, description="Why agent was paused")

    # Session management (for resume)
    session_id: str | None = Field(default=None, description="Claude Agent SDK session ID")
    checkpoint_id: str | None = Field(default=None, description="Last checkpoint for resume")
    conversation_turns: int = Field(default=0, description="Number of conversation turns")

    # Context
    initial_prompt: str = Field(default="", description="Initial user/orchestrator prompt")
    system_prompt_hash: str | None = Field(
        default=None, description="Hash of system prompt (detect changes)"
    )

    # Cost tracking
    tokens_used: int = Field(default=0, description="Total tokens consumed")
    cost_usd: float = Field(default=0.0, description="Total cost in USD")

    # Organization
    tags: list[str] = Field(default_factory=list, description="Auto-derived and manual tags")

    @model_validator(mode="before")
    @classmethod
    def set_entity_fields(cls, data: dict[str, Any]) -> dict[str, Any]:
        """Set name and content from agent fields."""
        if isinstance(data, dict):
            # Require organization_id
            if not data.get("organization_id"):
                msg = "organization_id is required for AgentRecord"
                raise ValueError(msg)
            # Name: agent type + truncated ID
            if "name" not in data:
                agent_type = data.get("agent_type", "agent")
                agent_id = data.get("id", "unknown")
                data["name"] = f"{agent_type}-{agent_id[-8:]}"
            # Content: status and task info
            if "content" not in data:
                parts = [f"Status: {data.get('status', 'unknown')}"]
                if data.get("task_id"):
                    parts.append(f"Task: {data['task_id']}")
                if data.get("initial_prompt"):
                    parts.append(f"Prompt: {data['initial_prompt'][:100]}...")
                data["content"] = " | ".join(parts)
        return data

    @classmethod
    def from_entity(cls, entity: Entity, org_id: str) -> "AgentRecord":
        """Construct AgentRecord from a generic Entity.

        Use this when EntityManager.get() returns a generic Entity and you need
        a typed AgentRecord. Centralizes conversion logic to avoid missing fields.

        Args:
            entity: Generic Entity from EntityManager.get()
            org_id: Organization ID (fallback if not in entity)

        Returns:
            Typed AgentRecord with all fields populated from entity metadata.
        """
        meta = entity.metadata or {}
        return cls(
            id=entity.id,
            name=entity.name,
            organization_id=entity.organization_id or org_id,
            entity_type=EntityType.AGENT,
            agent_type=AgentType(meta.get("agent_type", "general")),
            status=AgentStatus(meta.get("status", "initializing")),
            spawn_source=AgentSpawnSource(meta.get("spawn_source", "user")),
            project_id=meta.get("project_id", ""),
            task_id=meta.get("task_id"),
            session_id=meta.get("session_id"),
            worktree_path=meta.get("worktree_path"),
            worktree_branch=meta.get("worktree_branch"),
            initial_prompt=meta.get("initial_prompt", ""),
            created_by=meta.get("created_by"),
            checkpoint_id=meta.get("checkpoint_id"),
            conversation_turns=meta.get("conversation_turns", 0),
            tokens_used=meta.get("tokens_used", 0),
            cost_usd=meta.get("cost_usd", 0.0),
            tags=meta.get("tags", []),
        )


class WorktreeStatus(StrEnum):
    """Worktree lifecycle states."""

    ACTIVE = "active"  # In use by an agent
    ORPHANED = "orphaned"  # Agent died, worktree remains
    MERGED = "merged"  # Successfully merged to target
    DELETED = "deleted"  # Cleaned up


class WorktreeRecord(Entity):
    """Persistent worktree registry for tracking agent workspaces."""

    entity_type: EntityType = EntityType.WORKTREE

    # Ownership
    task_id: str = Field(..., description="Task this worktree was created for")
    agent_id: str | None = Field(default=None, description="Agent currently using this worktree")

    # Git info
    path: str = Field(..., description="Filesystem path to worktree")
    branch: str = Field(..., description="Git branch name")
    base_commit: str = Field(..., description="Commit worktree was created from")

    # Lifecycle
    status: WorktreeStatus = Field(default=WorktreeStatus.ACTIVE, description="Current state")
    last_used: datetime = Field(default_factory=lambda: datetime.now(UTC))

    # Work state
    has_uncommitted: bool = Field(default=False, description="Has uncommitted changes")
    last_commit: str | None = Field(default=None, description="Latest commit SHA")

    @model_validator(mode="before")
    @classmethod
    def set_entity_fields(cls, data: dict[str, Any]) -> dict[str, Any]:
        """Set name and content from worktree fields."""
        if isinstance(data, dict):
            if "name" not in data and "branch" in data:
                data["name"] = data["branch"]
            if "content" not in data:
                parts = [f"Path: {data.get('path', 'unknown')}"]
                if data.get("task_id"):
                    parts.append(f"Task: {data['task_id']}")
                data["content"] = " | ".join(parts)
        return data


class ApprovalType(StrEnum):
    """Types of human approval requests."""

    # Risk-based triggers
    DESTRUCTIVE_COMMAND = "destructive_command"  # rm -rf, force push, etc.
    SENSITIVE_FILE = "sensitive_file"  # .env, secrets, credentials
    FILE_WRITE = "file_write"  # Any file write operation
    EXTERNAL_API = "external_api"  # Calling external services
    COST_THRESHOLD = "cost_threshold"  # Approaching budget limit

    # Agent-requested
    REVIEW_PHASE = "review_phase"  # Agent completed a round of work
    QUESTION = "question"  # Agent needs clarification
    SCOPE_CHANGE = "scope_change"  # Work exceeds original task

    # System-initiated
    MERGE_CONFLICT = "merge_conflict"  # Needs human resolution
    TEST_FAILURE = "test_failure"  # Tests failed after changes


class ApprovalStatus(StrEnum):
    """Approval request states."""

    PENDING = "pending"  # Awaiting human response
    APPROVED = "approved"  # Human approved
    DENIED = "denied"  # Human denied
    EDITED = "edited"  # Human modified and approved
    EXPIRED = "expired"  # Timed out without response


class ApprovalRecord(Entity):
    """Persistent approval queue item for human-in-the-loop."""

    entity_type: EntityType = EntityType.APPROVAL

    # Context (organization_id inherited from Entity, validated below)
    project_id: str = Field(..., description="Project UUID")
    agent_id: str = Field(..., description="Requesting agent UUID")
    task_id: str | None = Field(default=None, description="Related task UUID")

    # Request details
    approval_type: ApprovalType = Field(..., description="Type of approval needed")
    priority: str = Field(default="medium", description="Request priority")
    title: str = Field(..., description="Short description of what needs approval")
    summary: str = Field(default="", description="Detailed context")
    metadata: dict[str, Any] = Field(
        default_factory=dict, description="Type-specific metadata (command, files, etc.)"
    )
    actions: list[str] = Field(
        default_factory=lambda: ["approve", "deny"],
        description="Available response actions",
    )

    # Lifecycle
    status: ApprovalStatus = Field(default=ApprovalStatus.PENDING, description="Current state")
    expires_at: datetime | None = Field(default=None, description="When request expires")
    responded_at: datetime | None = Field(default=None, description="When human responded")
    response_by: str | None = Field(default=None, description="User who responded")
    response_message: str | None = Field(default=None, description="Human's message to agent")

    @model_validator(mode="before")
    @classmethod
    def set_entity_fields(cls, data: dict[str, Any]) -> dict[str, Any]:
        """Set name, description, and content from approval fields."""
        if isinstance(data, dict):
            # Require organization_id
            if not data.get("organization_id"):
                msg = "organization_id is required for ApprovalRecord"
                raise ValueError(msg)
            if "name" not in data and "title" in data:
                data["name"] = data["title"][:100]
            # Set description from summary (Entity base class requires it)
            if "description" not in data and "summary" in data:
                data["description"] = data["summary"]
            if "content" not in data and "summary" in data:
                data["content"] = data["summary"]
        return data


class AgentCheckpoint(Entity):
    """Snapshot of agent state for resume capability.

    Stores everything needed to resume an agent after system restart.
    """

    entity_type: EntityType = EntityType.CHECKPOINT

    # Ownership
    agent_id: str = Field(..., description="Agent this checkpoint belongs to")
    session_id: str = Field(..., description="Claude Agent SDK session ID")

    # Conversation state
    conversation_history: list[dict[str, Any]] = Field(
        default_factory=list, description="Serialized conversation messages"
    )
    pending_tool_calls: list[dict[str, Any]] = Field(
        default_factory=list, description="Tools waiting for execution"
    )

    # Work state
    files_modified: list[str] = Field(
        default_factory=list, description="Files changed since last checkpoint"
    )
    uncommitted_changes: str = Field(default="", description="Git diff of uncommitted work")

    # Progress tracking
    current_step: str | None = Field(default=None, description="Current step being executed")
    completed_steps: list[str] = Field(default_factory=list, description="Steps completed")

    # Blocking state
    pending_approval_id: str | None = Field(
        default=None, description="Approval request blocking agent"
    )
    waiting_for_task_id: str | None = Field(
        default=None, description="Task dependency blocking agent"
    )

    @model_validator(mode="before")
    @classmethod
    def set_entity_fields(cls, data: dict[str, Any]) -> dict[str, Any]:
        """Set name and content from checkpoint fields."""
        if isinstance(data, dict):
            if "name" not in data:
                agent_id = data.get("agent_id", "unknown")
                data["name"] = f"checkpoint-{agent_id[-8:]}"
            if "content" not in data:
                step = data.get("current_step", "unknown")
                data["content"] = f"Step: {step}"
        return data

    @classmethod
    def from_entity(cls, entity: Entity) -> "AgentCheckpoint":
        """Construct AgentCheckpoint from a generic Entity.

        Use this when EntityManager.list_by_type() returns generic Entities.

        Args:
            entity: Generic Entity from EntityManager

        Returns:
            Typed AgentCheckpoint with all fields populated from entity metadata.
        """
        meta = entity.metadata or {}
        return cls(
            id=entity.id,
            name=entity.name,
            agent_id=meta.get("agent_id", ""),
            session_id=meta.get("session_id", ""),
            conversation_history=meta.get("conversation_history", []),
            pending_tool_calls=meta.get("pending_tool_calls", []),
            files_modified=meta.get("files_modified", []),
            uncommitted_changes=meta.get("uncommitted_changes", ""),
            current_step=meta.get("current_step"),
            completed_steps=meta.get("completed_steps", []),
            pending_approval_id=meta.get("pending_approval_id"),
            waiting_for_task_id=meta.get("waiting_for_task_id"),
        )
