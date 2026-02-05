"""AgentOrchestrator for multi-agent coordination.

Central coordinator that manages agent lifecycles, routes messages,
and distributes tasks across multiple concurrent agents.
"""

import asyncio
import contextlib
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

import structlog

from sibyl.agents.runner import AgentInstance, AgentRunner
from sibyl.agents.state_sync import update_agent_state
from sibyl.agents.worktree import WorktreeManager
from sibyl_core.models import (
    AgentRecord,
    AgentSpawnSource,
    AgentStatus,
    AgentType,
    EntityType,
    Task,
    TaskStatus,
)

if TYPE_CHECKING:
    from sibyl_core.graph import EntityManager

log = structlog.get_logger()


class OrchestratorError(Exception):
    """Base exception for orchestrator operations."""


class AgentOrchestrator:
    """Central coordinator for multi-agent workflows.

    The orchestrator manages:
    - Agent registry: tracks all agents for an org/project
    - Lifecycle: spawn, pause, resume, terminate agents
    - Task distribution: assign tasks to appropriate agents
    - Message routing: inter-agent communication
    - Recovery: restore agent state on startup

    Unlike AgentRunner which manages individual agent execution,
    the orchestrator coordinates across multiple agents and persists
    state to survive process restarts.
    """

    # How often to check for stale agents (no heartbeat)
    HEALTH_CHECK_INTERVAL = 60  # seconds

    # Agent is considered stale if no heartbeat for this long
    STALE_HEARTBEAT_THRESHOLD = 120  # seconds

    def __init__(
        self,
        entity_manager: "EntityManager",
        org_id: str,
        project_id: str,
        repo_path: str,
    ):
        """Initialize the orchestrator.

        Args:
            entity_manager: Graph client for persistence
            org_id: Organization UUID
            project_id: Project UUID
            repo_path: Path to the git repository
        """
        self.entity_manager = entity_manager
        self.org_id = org_id
        self.project_id = project_id

        # Create managers
        self.worktree_manager = WorktreeManager(
            entity_manager=entity_manager,
            org_id=org_id,
            project_id=project_id,
            repo_path=repo_path,
        )
        self.runner = AgentRunner(
            entity_manager=entity_manager,
            worktree_manager=self.worktree_manager,
            org_id=org_id,
            project_id=project_id,
        )

        # Runtime state
        self._running = False
        self._health_check_task: asyncio.Task[None] | None = None

        # Message queues for inter-agent communication
        self._message_queues: dict[str, asyncio.Queue[AgentMessage]] = {}

    async def start(self) -> None:
        """Start the orchestrator.

        Recovers state from the graph and begins health monitoring.
        """
        log.info(f"Starting orchestrator for project {self.project_id}")
        self._running = True

        # Recover any agents that were running before restart
        await self._recover_agents()

        # Start health check loop
        self._health_check_task = asyncio.create_task(self._health_check_loop())

        log.info("Orchestrator started")

    async def stop(self) -> None:
        """Stop the orchestrator gracefully.

        Checkpoints all running agents and cleans up resources.
        """
        log.info("Stopping orchestrator")
        self._running = False

        # Cancel health check and await cleanup
        if self._health_check_task:
            self._health_check_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._health_check_task
            self._health_check_task = None

        # Checkpoint and stop all active agents
        active_agents = await self.runner.list_active()
        for agent in active_agents:
            try:
                await agent.checkpoint(current_step="orchestrator_shutdown")
                await agent.stop(reason="orchestrator_shutdown")
            except Exception:
                log.exception(f"Error stopping agent {agent.id}")

        # Clean up orphaned worktrees
        await self.worktree_manager.cleanup_orphaned()

        log.info("Orchestrator stopped")

    # -------------------------------------------------------------------------
    # Agent Lifecycle
    # -------------------------------------------------------------------------

    async def spawn_agent(
        self,
        prompt: str,
        agent_type: AgentType = AgentType.GENERAL,
        task: Task | None = None,
        spawn_source: AgentSpawnSource = AgentSpawnSource.ORCHESTRATOR,
    ) -> AgentInstance:
        """Spawn a new agent.

        Args:
            prompt: Initial prompt for the agent
            agent_type: Type of specialized agent
            task: Optional task to assign
            spawn_source: How this agent was created

        Returns:
            The spawned AgentInstance
        """
        instance = await self.runner.spawn(
            prompt=prompt,
            agent_type=agent_type,
            task=task,
            spawn_source=spawn_source,
            create_worktree=True,
            enable_approvals=True,
        )

        # Create message queue for this agent
        self._message_queues[instance.id] = asyncio.Queue()

        log.info(f"Orchestrator spawned agent {instance.id}")
        return instance

    async def spawn_for_task(
        self,
        task: Task,
        agent_type: AgentType | None = None,
    ) -> AgentInstance:
        """Spawn an agent to work on a specific task.

        Automatically selects agent type based on task characteristics
        if not specified.

        Args:
            task: Task to work on
            agent_type: Optional agent type override

        Returns:
            The spawned AgentInstance
        """
        # Auto-select agent type based on task
        if agent_type is None:
            agent_type = self._select_agent_type_for_task(task)

        prompt = f"Please work on this task:\n\n{task.title}\n\n{task.description}"

        instance = await self.spawn_agent(
            prompt=prompt,
            agent_type=agent_type,
            task=task,
            spawn_source=AgentSpawnSource.ORCHESTRATOR,
        )

        # Update task with agent assignment
        await self.entity_manager.update(
            task.id,
            {
                "assigned_agent": instance.id,
                "claimed_at": datetime.now(UTC).isoformat(),
                "status": TaskStatus.DOING.value,
            },
        )

        return instance

    async def terminate_agent(
        self,
        agent_id: str,
        reason: str = "orchestrator_request",
        checkpoint: bool = True,
    ) -> bool:
        """Terminate an agent.

        Args:
            agent_id: Agent to terminate
            reason: Why the agent is being terminated
            checkpoint: Whether to checkpoint before terminating

        Returns:
            True if agent was terminated
        """
        instance = await self.runner.get_agent(agent_id)
        if not instance:
            return False

        if checkpoint:
            try:
                await instance.checkpoint(current_step=f"terminated: {reason}")
            except Exception:
                log.exception(f"Failed to checkpoint agent {agent_id}")

        # Clean up message queue
        self._message_queues.pop(agent_id, None)

        return await self.runner.stop_agent(agent_id, reason)

    async def pause_agent(self, agent_id: str, reason: str = "user_request") -> bool:
        """Pause an agent's execution.

        Args:
            agent_id: Agent to pause
            reason: Why the agent is being paused

        Returns:
            True if agent was paused
        """
        instance = await self.runner.get_agent(agent_id)
        if not instance:
            return False

        # Checkpoint before pausing
        await instance.checkpoint(current_step=f"paused: {reason}")
        await instance.pause(reason)
        return True

    async def resume_agent(
        self,
        agent_id: str,
        prompt: str = "Continue from where you left off.",
    ) -> AgentInstance | None:
        """Resume a paused or crashed agent.

        Args:
            agent_id: Agent to resume
            prompt: Prompt for resumed session

        Returns:
            Resumed AgentInstance or None if not found
        """
        from sibyl.agents.checkpoints import CheckpointManager

        # Get latest checkpoint
        manager = CheckpointManager(self.entity_manager, agent_id)
        checkpoint = await manager.get_latest()

        if not checkpoint:
            log.warning("No checkpoint found for agent", agent_id=agent_id)
            return None

        if not checkpoint.session_id:
            log.warning("No session_id in checkpoint, cannot resume", agent_id=agent_id)
            return None

        instance = await self.runner.resume_agent(
            agent_id=agent_id,
            session_id=checkpoint.session_id,
            prompt=prompt,
        )

        # Recreate message queue
        self._message_queues[instance.id] = asyncio.Queue()

        return instance

    # -------------------------------------------------------------------------
    # Agent Registry
    # -------------------------------------------------------------------------

    async def list_agents(
        self,
        status: AgentStatus | None = None,
        agent_type: AgentType | None = None,
        limit: int = 50,
    ) -> list[AgentRecord]:
        """List agents for this project.

        Args:
            status: Filter by status
            agent_type: Filter by type
            limit: Maximum results

        Returns:
            List of AgentRecord objects
        """
        results = await self.entity_manager.list_by_type(
            entity_type=EntityType.AGENT,
            limit=limit * 2,  # Fetch extra for filtering
        )

        agents = [cast("AgentRecord", r) for r in results if r.entity_type == EntityType.AGENT]

        # Filter by project
        agents = [a for a in agents if a.project_id == self.project_id]

        # Apply filters
        if status:
            agents = [a for a in agents if a.status == status]
        if agent_type:
            agents = [a for a in agents if a.agent_type == agent_type]

        return agents[:limit]

    async def get_agent_status(self, agent_id: str) -> dict[str, Any]:
        """Get detailed status for an agent.

        Returns:
            Dict with agent status, metrics, and current state
        """
        # Check if active in memory
        instance = await self.runner.get_agent(agent_id)

        # Get record from graph
        record = await self.entity_manager.get(agent_id)
        if not record or record.entity_type != EntityType.AGENT:
            raise OrchestratorError(f"Agent not found: {agent_id}")
        record = cast("AgentRecord", record)

        return {
            "id": agent_id,
            "status": record.status.value,
            "agent_type": record.agent_type.value,
            "is_active": instance is not None,
            "task_id": record.task_id,
            "tokens_used": record.tokens_used,
            "cost_usd": record.cost_usd,
            "started_at": record.started_at,
            "last_heartbeat": record.last_heartbeat,
            "worktree_path": record.worktree_path,
            "session_id": instance.session_id if instance else None,
        }

    # -------------------------------------------------------------------------
    # Task Distribution
    # -------------------------------------------------------------------------

    async def assign_task(self, task: Task) -> AgentInstance:
        """Assign a task to an available agent or spawn a new one.

        Args:
            task: Task to assign

        Returns:
            AgentInstance working on the task
        """
        # Check for idle agents of matching type
        preferred_type = self._select_agent_type_for_task(task)
        available = await self._find_available_agent(preferred_type)

        if available:
            # Assign task to existing agent
            return await self._reassign_agent(available, task)

        # Spawn new agent for task
        return await self.spawn_for_task(task)

    async def unassign_task(self, task_id: str) -> bool:
        """Unassign a task from its agent.

        Args:
            task_id: Task to unassign

        Returns:
            True if task was unassigned
        """
        task = await self.entity_manager.get(task_id)
        if not task or task.entity_type != EntityType.TASK:
            return False
        task = cast("Task", task)

        if task.assigned_agent:
            # Stop the agent working on this task
            await self.terminate_agent(
                task.assigned_agent,
                reason="task_unassigned",
                checkpoint=True,
            )

        # Clear assignment
        await self.entity_manager.update(
            task_id,
            {
                "assigned_agent": None,
                "claimed_at": None,
                "status": TaskStatus.TODO.value,
            },
        )

        return True

    async def get_workload(self) -> dict[str, Any]:
        """Get current workload statistics.

        Returns:
            Dict with agent counts by status and type
        """
        agents = await self.list_agents()

        by_status: dict[str, int] = {}
        by_type: dict[str, int] = {}

        for agent in agents:
            status = agent.status.value
            by_status[status] = by_status.get(status, 0) + 1

            atype = agent.agent_type.value
            by_type[atype] = by_type.get(atype, 0) + 1

        active = await self.runner.list_active()

        return {
            "total_agents": len(agents),
            "active_agents": len(active),
            "by_status": by_status,
            "by_type": by_type,
        }

    # -------------------------------------------------------------------------
    # Inter-Agent Messaging
    # -------------------------------------------------------------------------

    async def send_message(
        self,
        from_agent: str,
        to_agent: str,
        content: str,
        message_type: str = "text",
        metadata: dict[str, Any] | None = None,
    ) -> bool:
        """Send a message from one agent to another.

        Args:
            from_agent: Sender agent ID
            to_agent: Recipient agent ID
            content: Message content
            message_type: Type of message
            metadata: Additional message data

        Returns:
            True if message was queued
        """
        queue = self._message_queues.get(to_agent)
        if not queue:
            log.warning(f"No message queue for agent {to_agent}")
            return False

        message = AgentMessage(
            from_agent=from_agent,
            to_agent=to_agent,
            content=content,
            message_type=message_type,
            metadata=metadata or {},
            timestamp=datetime.now(UTC),
        )

        await queue.put(message)
        log.debug(f"Message queued: {from_agent} -> {to_agent}")
        return True

    async def receive_messages(
        self,
        agent_id: str,
        wait_timeout: float = 0.1,
    ) -> list["AgentMessage"]:
        """Receive pending messages for an agent.

        Args:
            agent_id: Agent to receive messages for
            wait_timeout: How long to wait for messages

        Returns:
            List of pending messages
        """
        queue = self._message_queues.get(agent_id)
        if not queue:
            return []

        messages: list[AgentMessage] = []

        try:
            while True:
                message = await asyncio.wait_for(
                    queue.get(),
                    timeout=wait_timeout,
                )
                messages.append(message)
        except TimeoutError:
            pass

        return messages

    async def broadcast(
        self,
        from_agent: str,
        content: str,
        message_type: str = "broadcast",
        exclude: list[str] | None = None,
    ) -> int:
        """Broadcast a message to all active agents.

        Args:
            from_agent: Sender agent ID
            content: Message content
            message_type: Type of message
            exclude: Agent IDs to exclude

        Returns:
            Number of agents that received the message
        """
        exclude = exclude or []
        count = 0

        for agent_id in self._message_queues:
            if agent_id != from_agent and agent_id not in exclude:
                if await self.send_message(
                    from_agent=from_agent,
                    to_agent=agent_id,
                    content=content,
                    message_type=message_type,
                ):
                    count += 1

        return count

    # -------------------------------------------------------------------------
    # Internal Methods
    # -------------------------------------------------------------------------

    def _select_agent_type_for_task(self, task: Task) -> AgentType:
        """Select appropriate agent type based on task characteristics."""
        # Check task tags/domain for hints
        tags = task.tags or []
        domain = task.domain or ""

        if "test" in tags or "testing" in domain.lower():
            return AgentType.TESTER
        if "review" in tags or "code review" in domain.lower():
            return AgentType.REVIEWER
        if "plan" in tags or "design" in domain.lower():
            return AgentType.PLANNER
        if "merge" in tags or "integration" in domain.lower():
            return AgentType.INTEGRATOR

        # Default to implementer for most tasks
        return AgentType.IMPLEMENTER

    async def _find_available_agent(
        self,
        preferred_type: AgentType,
    ) -> AgentInstance | None:
        """Find a paused agent of the preferred type that can be resumed.

        Note: In practice, we usually spawn new agents for each task rather
        than reusing paused ones, since agents maintain task-specific context.
        """
        # Check for paused agents that could be resumed
        agents = await self.list_agents(status=AgentStatus.PAUSED)

        for record in agents:
            if record.agent_type == preferred_type:
                # This agent could be resumed
                # For now, return None to always spawn fresh agents
                pass

        return None

    async def _reassign_agent(
        self,
        instance: AgentInstance,
        task: Task,
    ) -> AgentInstance:
        """Reassign an idle agent to a new task."""
        # Update agent record
        await self.entity_manager.update(
            instance.id,
            {
                "task_id": task.id,
                "status": AgentStatus.WORKING.value,
            },
        )
        await update_agent_state(
            org_id=self.org_id,
            agent_id=instance.id,
            status=AgentStatus.WORKING.value,
            task_id=task.id,
        )

        # Update task
        await self.entity_manager.update(
            task.id,
            {
                "assigned_agent": instance.id,
                "claimed_at": datetime.now(UTC).isoformat(),
                "status": TaskStatus.DOING.value,
            },
        )

        # Send new task prompt
        prompt = f"New task assigned:\n\n{task.title}\n\n{task.description}"
        async for _ in instance.send_message(prompt):
            pass  # Consume messages

        return instance

    async def _recover_agents(self) -> None:
        """Recover agents that were running before restart."""
        # Find agents that were working/paused/waiting
        agents = await self.list_agents()
        recoverable_statuses = (
            AgentStatus.WORKING,
            AgentStatus.PAUSED,
            AgentStatus.WAITING_APPROVAL,
            AgentStatus.WAITING_DEPENDENCY,
        )
        recoverable = [a for a in agents if a.status in recoverable_statuses]

        log.info(f"Found {len(recoverable)} agents to recover")

        for record in recoverable:
            try:
                instance = await self.resume_agent(record.id)
                if instance:
                    log.info(f"Recovered agent {record.id}")
                else:
                    # Mark as failed if can't recover
                    await self.entity_manager.update(
                        record.id,
                        {
                            "status": AgentStatus.FAILED.value,
                            "error_message": "Failed to recover after restart",
                        },
                    )
                    await update_agent_state(
                        org_id=self.org_id,
                        agent_id=record.id,
                        status=AgentStatus.FAILED.value,
                        error_message="Failed to recover after restart",
                    )
            except Exception:
                log.exception(f"Failed to recover agent {record.id}")

    async def _health_check_loop(self) -> None:
        """Background task to monitor agent health."""
        while self._running:
            try:
                await asyncio.sleep(self.HEALTH_CHECK_INTERVAL)
                await self._check_agent_health()
            except asyncio.CancelledError:
                break
            except Exception:
                log.exception("Health check failed")

    async def _check_agent_health(self) -> None:
        """Check for stale or unhealthy agents."""
        now = datetime.now(UTC)
        active = await self.runner.list_active()

        for instance in active:
            record = instance.record

            # Check heartbeat
            if record.last_heartbeat:
                heartbeat = record.last_heartbeat
                age = (now - heartbeat).total_seconds()

                if age > self.STALE_HEARTBEAT_THRESHOLD:
                    log.warning(f"Agent {instance.id} stale (no heartbeat for {age:.0f}s)")
                    # Checkpoint and mark as stale
                    try:
                        await instance.checkpoint(current_step="stale_heartbeat")
                        await self.entity_manager.update(
                            instance.id,
                            {"status": AgentStatus.FAILED.value},
                        )
                        await update_agent_state(
                            org_id=self.org_id,
                            agent_id=instance.id,
                            status=AgentStatus.FAILED.value,
                        )
                    except Exception:
                        log.exception(f"Failed to handle stale agent {instance.id}")


class AgentMessage:
    """Message passed between agents."""

    def __init__(
        self,
        from_agent: str,
        to_agent: str,
        content: str,
        message_type: str,
        metadata: dict[str, Any],
        timestamp: datetime,
    ):
        self.from_agent = from_agent
        self.to_agent = to_agent
        self.content = content
        self.message_type = message_type
        self.metadata = metadata
        self.timestamp = timestamp

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dict."""
        return {
            "from_agent": self.from_agent,
            "to_agent": self.to_agent,
            "content": self.content,
            "message_type": self.message_type,
            "metadata": self.metadata,
            "timestamp": self.timestamp.isoformat(),
        }
