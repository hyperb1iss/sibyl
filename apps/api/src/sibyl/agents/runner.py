"""AgentRunner for Claude Agent SDK integration.

Provides the bridge between Sibyl's agent harness and Claude's Agent SDK,
enabling agent spawning, lifecycle management, and tool integration.
"""

import asyncio
import hashlib
import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from claude_agent_sdk import query
from claude_agent_sdk.types import (
    AssistantMessage,
    ClaudeAgentOptions,
    HookEvent,
    HookMatcher,
    Message,
    ResultMessage,
    UserMessage,
)

from sibyl.agents.approvals import ApprovalService
from sibyl.agents.worktree import WorktreeManager
from sibyl_core.models import (
    AgentCheckpoint,
    AgentRecord,
    AgentSpawnSource,
    AgentStatus,
    AgentType,
    Task,
)

if TYPE_CHECKING:
    from sibyl_core.graph import EntityManager

logger = logging.getLogger(__name__)


def _generate_agent_id(org_id: str, project_id: str, timestamp: str) -> str:
    """Generate a unique agent ID."""
    combined = f"{org_id}:{project_id}:{timestamp}"
    hash_bytes = hashlib.sha256(combined.encode()).hexdigest()[:12]
    return f"agent_{hash_bytes}"


class AgentRunnerError(Exception):
    """Base exception for agent runner operations."""


class AgentRunner:
    """Runs Claude agents with Sibyl integration.

    Handles agent lifecycle from spawning through execution:
    - Creates isolated worktrees for code tasks
    - Registers agents in the knowledge graph
    - Streams conversation and tool events
    - Manages heartbeats and checkpointing
    """

    # Default system prompt preamble for all agents
    SYSTEM_PROMPT_PREAMBLE = """You are an AI agent working on a software development task.
You have access to Sibyl's knowledge graph for:
- Searching past learnings and patterns
- Tracking task progress
- Capturing new insights

Guidelines:
- Search Sibyl for relevant patterns before implementing
- Update task status as you progress
- Capture non-obvious learnings when you discover them
- Request human review when completing significant milestones
"""

    AGENT_TYPE_PROMPTS = {
        AgentType.GENERAL: "You are a general-purpose agent.",
        AgentType.PLANNER: (
            "You are a senior software architect. Break features into "
            "implementable tasks with clear scope and dependencies."
        ),
        AgentType.IMPLEMENTER: (
            "You are a senior developer. Write clean, tested code that follows "
            "existing patterns in the codebase."
        ),
        AgentType.TESTER: (
            "You are a QA engineer. Write comprehensive tests that cover edge cases "
            "and ensure code correctness."
        ),
        AgentType.REVIEWER: (
            "You are a code reviewer. Analyze code for bugs, security issues, "
            "performance problems, and style violations."
        ),
        AgentType.INTEGRATOR: (
            "You are a git expert. Merge branches, resolve conflicts, and ensure "
            "clean integration of parallel work."
        ),
        AgentType.ORCHESTRATOR: (
            "You are a project coordinator. Manage multiple agents, track dependencies, "
            "and ensure work completes efficiently."
        ),
    }

    def __init__(
        self,
        entity_manager: "EntityManager",
        worktree_manager: WorktreeManager,
        org_id: str,
        project_id: str,
    ):
        """Initialize AgentRunner.

        Args:
            entity_manager: Graph client for agent persistence
            worktree_manager: Worktree manager for agent isolation
            org_id: Organization UUID
            project_id: Project UUID
        """
        self.entity_manager = entity_manager
        self.worktree_manager = worktree_manager
        self.org_id = org_id
        self.project_id = project_id

        # Active agent instances (in-memory during execution)
        self._active_agents: dict[str, AgentInstance] = {}

    def _build_system_prompt(
        self,
        agent_type: AgentType,
        task: Task | None = None,
        custom_instructions: str | None = None,
    ) -> str:
        """Build the system prompt for an agent.

        Args:
            agent_type: Type of agent being created
            task: Optional task for context
            custom_instructions: Additional instructions

        Returns:
            Complete system prompt string
        """
        parts = [self.SYSTEM_PROMPT_PREAMBLE]

        # Add agent-type-specific instructions
        type_prompt = self.AGENT_TYPE_PROMPTS.get(agent_type, "")
        if type_prompt:
            parts.append(f"\n## Role\n{type_prompt}")

        # Add task context if provided
        if task:
            task_context = f"""
## Current Task
Title: {task.title}
Description: {task.description}
Status: {task.status}
Priority: {task.priority}
"""
            if task.technologies:
                task_context += f"Technologies: {', '.join(task.technologies)}\n"
            if task.domain:
                task_context += f"Domain: {task.domain}\n"

            parts.append(task_context)

        # Add custom instructions
        if custom_instructions:
            parts.append(f"\n## Additional Instructions\n{custom_instructions}")

        return "\n".join(parts)

    async def spawn(
        self,
        prompt: str,
        agent_type: AgentType = AgentType.GENERAL,
        task: Task | None = None,
        spawn_source: AgentSpawnSource = AgentSpawnSource.USER,
        create_worktree: bool = True,
        custom_instructions: str | None = None,
        enable_approvals: bool = True,
    ) -> "AgentInstance":
        """Spawn a new Claude agent instance.

        Args:
            prompt: Initial prompt for the agent
            agent_type: Type of specialized agent
            task: Optional task to assign
            spawn_source: How this agent was created
            create_worktree: Whether to create an isolated worktree
            custom_instructions: Additional system prompt instructions
            enable_approvals: Enable human-in-the-loop approval hooks

        Returns:
            AgentInstance ready for execution
        """
        logger.info(f"Spawning {agent_type} agent for task {task.id if task else 'adhoc'}")

        # Generate agent ID
        timestamp = datetime.now(UTC).isoformat()
        agent_id = _generate_agent_id(self.org_id, self.project_id, timestamp)

        # Create agent record
        record = AgentRecord(
            id=agent_id,
            name=f"{agent_type}-{agent_id[-8:]}",
            organization_id=self.org_id,
            project_id=self.project_id,
            agent_type=agent_type,
            spawn_source=spawn_source,
            task_id=task.id if task else None,
            status=AgentStatus.INITIALIZING,
            initial_prompt=prompt[:500],  # Truncate for storage
        )

        # Persist to graph
        await self.entity_manager.create(record)

        # Create worktree if requested
        worktree_path: Path | None = None
        if create_worktree:
            branch_name = f"agent/{record.id[-12:]}"
            if task:
                # Use task-derived branch name
                safe_title = task.title[:30].lower().replace(" ", "-")
                branch_name = f"agent/{record.id[-8:]}-{safe_title}"

            worktree = await self.worktree_manager.create(
                task_id=task.id if task else record.id,
                branch_name=branch_name,
                agent_id=record.id,
            )
            worktree_path = Path(worktree.path)

            # Update record with worktree info
            await self.entity_manager.update(
                record.id,
                {
                    "worktree_path": worktree.path,
                    "worktree_branch": worktree.branch,
                },
            )
            record.worktree_path = worktree.path
            record.worktree_branch = worktree.branch

        # Build system prompt
        system_prompt = self._build_system_prompt(
            agent_type=agent_type,
            task=task,
            custom_instructions=custom_instructions,
        )

        # Create approval service if enabled
        approval_service: ApprovalService | None = None
        hooks: dict[HookEvent, list[HookMatcher]] | None = None

        if enable_approvals:
            approval_service = ApprovalService(
                entity_manager=self.entity_manager,
                org_id=self.org_id,
                project_id=self.project_id,
                agent_id=record.id,
                task_id=task.id if task else None,
            )
            hooks = approval_service.create_hook_matchers()  # type: ignore[assignment]

        # Create SDK options
        sdk_options = ClaudeAgentOptions(
            cwd=str(worktree_path) if worktree_path else None,
            system_prompt=system_prompt,
            hooks=hooks,
        )

        # Create instance
        instance = AgentInstance(
            record=record,
            sdk_options=sdk_options,
            entity_manager=self.entity_manager,
            initial_prompt=prompt,
            worktree_path=worktree_path,
            task=task,
            approval_service=approval_service,
        )

        # Register as active
        self._active_agents[record.id] = instance

        # Update status
        await self.entity_manager.update(
            record.id,
            {
                "status": AgentStatus.WORKING.value,
                "started_at": datetime.now(UTC).isoformat(),
            },
        )

        logger.info(f"Agent {record.id} spawned and ready")
        return instance

    async def spawn_for_task(
        self,
        task: Task,
        agent_type: AgentType = AgentType.IMPLEMENTER,
    ) -> "AgentInstance":
        """Convenience method to spawn an agent for a specific task.

        Args:
            task: Task to work on
            agent_type: Type of agent (defaults to implementer)

        Returns:
            AgentInstance assigned to the task
        """
        prompt = f"Please work on this task:\n\n{task.title}\n\n{task.description}"
        return await self.spawn(
            prompt=prompt,
            agent_type=agent_type,
            task=task,
            spawn_source=AgentSpawnSource.ORCHESTRATOR,
            create_worktree=True,
        )

    async def get_agent(self, agent_id: str) -> "AgentInstance | None":
        """Get an active agent instance by ID."""
        return self._active_agents.get(agent_id)

    async def list_active(self) -> list["AgentInstance"]:
        """List all active agent instances."""
        return list(self._active_agents.values())

    async def stop_agent(self, agent_id: str, reason: str = "user_request") -> bool:
        """Stop an active agent.

        Args:
            agent_id: Agent to stop
            reason: Why the agent is being stopped

        Returns:
            True if agent was stopped
        """
        instance = self._active_agents.pop(agent_id, None)
        if not instance:
            return False

        await instance.stop(reason)
        return True

    async def stop_all(self, reason: str = "shutdown") -> int:
        """Stop all active agents.

        Returns:
            Number of agents stopped
        """
        agent_ids = list(self._active_agents.keys())
        for agent_id in agent_ids:
            await self.stop_agent(agent_id, reason)
        return len(agent_ids)

    async def resume_from_checkpoint(
        self,
        checkpoint: AgentCheckpoint,
        prompt: str = "Continue from where you left off.",
        enable_approvals: bool = True,
    ) -> "AgentInstance":
        """Resume an agent from a checkpoint.

        Uses the Claude SDK's session resume to restore conversation history
        and continue execution from the checkpoint state.

        Args:
            checkpoint: Checkpoint to resume from
            prompt: Prompt for the resumed session (e.g., "continue working")
            enable_approvals: Enable human-in-the-loop approval hooks

        Returns:
            Resumed AgentInstance

        Raises:
            AgentRunnerError: If checkpoint cannot be restored
        """
        from sibyl.agents.checkpoints import (
            CheckpointRestoreError,
            restore_from_checkpoint,
        )

        logger.info(f"Resuming agent {checkpoint.agent_id} from checkpoint {checkpoint.id}")

        # Validate checkpoint and get restoration data
        try:
            restore_result = await restore_from_checkpoint(self.entity_manager, checkpoint)
        except CheckpointRestoreError as e:
            raise AgentRunnerError(f"Cannot restore checkpoint: {e}") from e

        # Get original agent record
        agent = await self.entity_manager.get(checkpoint.agent_id)
        if not agent or not isinstance(agent, AgentRecord):
            raise AgentRunnerError(f"Agent record not found: {checkpoint.agent_id}")

        # Update agent status
        await self.entity_manager.update(
            agent.id,
            {
                "status": AgentStatus.WORKING.value,
                "resumed_from_checkpoint": checkpoint.id,
            },
        )

        # Recreate approval service if enabled
        approval_service: ApprovalService | None = None
        hooks: dict[HookEvent, list[HookMatcher]] | None = None

        if enable_approvals:
            approval_service = ApprovalService(
                entity_manager=self.entity_manager,
                org_id=self.org_id,
                project_id=self.project_id,
                agent_id=agent.id,
                task_id=agent.task_id,
            )
            hooks = approval_service.create_hook_matchers()  # type: ignore[assignment]

        # Build SDK options with resume session
        sdk_options = ClaudeAgentOptions(
            cwd=str(restore_result.worktree_path) if restore_result.worktree_path else None,
            hooks=hooks,
            resume=restore_result.session_id,  # Resume from checkpoint session
        )

        # Get task if assigned
        task: Task | None = None
        if agent.task_id:
            task_entity = await self.entity_manager.get(agent.task_id)
            if task_entity and isinstance(task_entity, Task):
                task = task_entity

        # Create resumed instance
        instance = AgentInstance(
            record=agent,
            sdk_options=sdk_options,
            entity_manager=self.entity_manager,
            initial_prompt=prompt,
            worktree_path=restore_result.worktree_path,
            task=task,
            approval_service=approval_service,
        )

        # Restore session ID
        instance.set_session_id(restore_result.session_id)

        # Register as active
        self._active_agents[agent.id] = instance

        logger.info(
            f"Agent {agent.id} resumed from checkpoint {checkpoint.id} "
            f"(session: {restore_result.session_id})"
        )

        return instance


class AgentInstance:
    """A running Claude agent instance.

    Wraps the Claude SDK with Sibyl-specific functionality:
    - Heartbeat updates
    - Progress tracking
    - Checkpoint management
    - Event streaming
    """

    HEARTBEAT_INTERVAL = 30  # seconds

    def __init__(
        self,
        record: AgentRecord,
        sdk_options: ClaudeAgentOptions,
        entity_manager: "EntityManager",
        initial_prompt: str,
        worktree_path: Path | None = None,
        task: Task | None = None,
        approval_service: ApprovalService | None = None,
    ):
        """Initialize agent instance.

        Args:
            record: Persistent agent record
            sdk_options: Claude SDK options
            entity_manager: Graph client
            initial_prompt: Prompt to execute
            worktree_path: Working directory
            task: Assigned task
            approval_service: Optional approval service for human-in-the-loop
        """
        self.record = record
        self.sdk_options = sdk_options
        self.entity_manager = entity_manager
        self.initial_prompt = initial_prompt
        self.worktree_path = worktree_path
        self.task = task
        self.approval_service = approval_service

        # Runtime state
        self._running = False
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._conversation_history: list[Message] = []
        self._tokens_used = 0
        self._cost_usd = 0.0
        self._session_id: str | None = None

    @property
    def id(self) -> str:
        """Agent ID."""
        return self.record.id

    @property
    def status(self) -> AgentStatus:
        """Current agent status."""
        return self.record.status

    @property
    def session_id(self) -> str | None:
        """Claude SDK session ID."""
        return self._session_id

    def set_session_id(self, session_id: str) -> None:
        """Set the session ID (used during resume)."""
        self._session_id = session_id

    async def execute(self) -> AsyncIterator[Message]:
        """Execute the agent with the initial prompt.

        Yields:
            Message objects from the Claude SDK
        """
        self._running = True

        # Start heartbeat task
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

        try:
            # Execute via SDK query function
            async for message in query(
                prompt=self.initial_prompt,
                options=self.sdk_options,
            ):
                self._conversation_history.append(message)

                # Track usage from ResultMessage
                if isinstance(message, ResultMessage):
                    if message.usage:
                        self._tokens_used += getattr(message.usage, "input_tokens", 0)
                        self._tokens_used += getattr(message.usage, "output_tokens", 0)
                    if message.total_cost_usd:
                        self._cost_usd = message.total_cost_usd
                    if message.session_id:
                        self._session_id = message.session_id

                yield message

        except Exception as e:
            logger.exception(f"Agent {self.id} execution failed")
            await self._update_status(AgentStatus.FAILED, error=str(e))
            raise

        finally:
            self._running = False
            if self._heartbeat_task:
                self._heartbeat_task.cancel()

        # Mark completed
        await self._update_status(AgentStatus.COMPLETED)

    async def send_message(self, content: str) -> AsyncIterator[Message]:
        """Send a follow-up message to the agent.

        Args:
            content: Message content from user

        Yields:
            Response messages from the agent
        """
        async for message in query(
            prompt=content,
            options=self.sdk_options,
        ):
            self._conversation_history.append(message)
            yield message

    async def stop(self, reason: str = "user_request"):
        """Stop agent execution.

        Args:
            reason: Why the agent is being stopped
        """
        self._running = False
        if self._heartbeat_task:
            self._heartbeat_task.cancel()

        # Cancel any pending approvals
        if self.approval_service:
            await self.approval_service.cancel_all(reason)

        await self._update_status(
            AgentStatus.TERMINATED,
            metadata={"stop_reason": reason},
        )

    async def pause(self, reason: str = "user_request"):
        """Pause agent execution.

        Args:
            reason: Why the agent is being paused
        """
        self._running = False
        if self._heartbeat_task:
            self._heartbeat_task.cancel()

        # Note: We don't cancel approvals on pause - they remain pending

        await self._update_status(
            AgentStatus.PAUSED,
            metadata={"paused_reason": reason},
        )

    async def _heartbeat_loop(self):
        """Background task to update heartbeat."""
        while self._running:
            try:
                await asyncio.sleep(self.HEARTBEAT_INTERVAL)
                if self._running:
                    await self.entity_manager.update(
                        self.record.id,
                        {
                            "heartbeat_at": datetime.now(UTC).isoformat(),
                            "tokens_used": self._tokens_used,
                            "cost_usd": self._cost_usd,
                        },
                    )
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception(f"Heartbeat failed for agent {self.id}")

    async def _update_status(
        self,
        status: AgentStatus,
        error: str | None = None,
        metadata: dict[str, Any] | None = None,
    ):
        """Update agent status in the graph."""
        updates: dict[str, Any] = {"status": status.value}

        if status == AgentStatus.COMPLETED:
            updates["completed_at"] = datetime.now(UTC).isoformat()
            updates["tokens_used"] = self._tokens_used
            updates["cost_usd"] = self._cost_usd

        if self._session_id:
            updates["session_id"] = self._session_id

        if error:
            updates["error_message"] = error

        if metadata:
            updates.update(metadata)

        await self.entity_manager.update(self.record.id, updates)
        self.record.status = status

    def get_conversation_history(self) -> list[dict[str, Any]]:
        """Get serializable conversation history for checkpointing."""
        return [self._serialize_message(m) for m in self._conversation_history]

    async def checkpoint(
        self,
        current_step: str | None = None,
        pending_approval_id: str | None = None,
    ) -> "AgentCheckpoint":
        """Create a checkpoint of the current agent state.

        Args:
            current_step: Optional description of current step
            pending_approval_id: Optional approval blocking the agent

        Returns:
            Created AgentCheckpoint record
        """
        from sibyl.agents.checkpoints import CheckpointManager

        manager = CheckpointManager(self.entity_manager, self.id)
        return await manager.checkpoint(
            self,
            current_step=current_step,
            pending_approval_id=pending_approval_id,
        )

    def _serialize_message(self, message: Message) -> dict[str, Any]:
        """Serialize a Message for storage."""
        result: dict[str, Any] = {}

        if isinstance(message, UserMessage):
            result["type"] = "user"
            result["content"] = message.content
        elif isinstance(message, AssistantMessage):
            result["type"] = "assistant"
            result["content"] = message.content
            if message.model:
                result["model"] = message.model
        elif isinstance(message, ResultMessage):
            result["type"] = "result"
            result["subtype"] = message.subtype
            if message.duration_ms:
                result["duration_ms"] = message.duration_ms
            if message.total_cost_usd:
                result["total_cost_usd"] = message.total_cost_usd
        else:
            # StreamEvent or other
            result["type"] = "event"

        return result
