"""Worker promotion flow for standalone -> managed transition.

When a standalone agent (created directly by user) completes work that should be
reviewed by a TaskOrchestrator, this module handles the promotion process:

1. Validate the standalone agent is eligible for promotion
2. Create or assign a TaskOrchestrator for the task
3. Update the agent to be managed by the orchestrator
4. Optionally migrate agent to isolated worktree
5. Trigger orchestrator review of the work
"""

from typing import TYPE_CHECKING, cast

import structlog

from sibyl_core.models import (
    AgentRecord,
    AgentStatus,
    EntityType,
    QualityGateType,
    Task,
    TaskOrchestratorPhase,
    TaskOrchestratorRecord,
    TaskOrchestratorStatus,
)

if TYPE_CHECKING:
    from sibyl.agents.runner import AgentRunner
    from sibyl.agents.worktree import WorktreeManager
    from sibyl_core.graph import EntityManager
    from sibyl_core.graph.relationships import RelationshipManager

log = structlog.get_logger()

# Default quality gates for promoted workers
DEFAULT_PROMOTION_GATES = [
    QualityGateType.LINT,
    QualityGateType.TYPECHECK,
    QualityGateType.TEST,
    QualityGateType.HUMAN_REVIEW,
]


class WorkerPromotionError(Exception):
    """Error during worker promotion."""


class WorkerPromotionService:
    """Manages promotion of standalone workers to managed workers.

    When a user-created agent completes work, they may want to run it through
    the full orchestration loop (quality gates, review, etc.). This service
    handles that transition.
    """

    def __init__(
        self,
        entity_manager: "EntityManager",
        org_id: str,
        project_id: str,
        relationship_manager: "RelationshipManager | None" = None,
        agent_runner: "AgentRunner | None" = None,
        worktree_manager: "WorktreeManager | None" = None,
    ):
        """Initialize promotion service.

        Args:
            entity_manager: Graph client for persistence
            org_id: Organization UUID
            project_id: Project UUID
            relationship_manager: For creating relationships (optional, created if needed)
            agent_runner: For spawning agents (optional, created if needed)
            worktree_manager: For managing worktrees (optional, created if needed)
        """
        self.entity_manager = entity_manager
        self.org_id = org_id
        self.project_id = project_id
        self._relationship_manager = relationship_manager
        self._agent_runner = agent_runner
        self._worktree_manager = worktree_manager

    async def can_promote(self, agent_id: str) -> tuple[bool, str]:
        """Check if an agent can be promoted.

        Args:
            agent_id: Agent UUID to check

        Returns:
            Tuple of (can_promote, reason)
        """
        try:
            entity = await self.entity_manager.get(agent_id)
            if not entity or entity.entity_type != EntityType.AGENT:
                return False, "Agent not found"

            agent = cast("AgentRecord", entity)

            # Must be standalone
            if not agent.standalone:
                return False, "Agent is already managed by an orchestrator"

            # Must be in a promotable state
            if agent.status not in (
                AgentStatus.COMPLETED,
                AgentStatus.PAUSED,
                AgentStatus.WORKING,
            ):
                return False, f"Agent in invalid state for promotion: {agent.status.value}"

            # Must have a task assigned
            if not agent.task_id:
                return False, "Agent has no task assigned"

            return True, "Agent is eligible for promotion"

        except Exception as e:
            log.exception("Error checking promotion eligibility", agent_id=agent_id)
            return False, f"Error: {e}"

    async def promote(
        self,
        agent_id: str,
        orchestrator_id: str | None = None,
        gate_config: list[QualityGateType] | None = None,
        create_worktree: bool = True,
    ) -> tuple[AgentRecord, TaskOrchestratorRecord]:
        """Promote a standalone agent to managed status.

        Either assigns to an existing orchestrator or creates a new one.

        Args:
            agent_id: Agent UUID to promote
            orchestrator_id: Optional existing TaskOrchestrator to assign to
            gate_config: Quality gates for new orchestrator (if creating)
            create_worktree: Whether to create isolated worktree

        Returns:
            Tuple of (updated AgentRecord, TaskOrchestratorRecord)

        Raises:
            WorkerPromotionError: If promotion fails
        """
        # Validate eligibility
        can_promote, reason = await self.can_promote(agent_id)
        if not can_promote:
            raise WorkerPromotionError(f"Cannot promote agent: {reason}")

        # Get the agent
        entity = await self.entity_manager.get(agent_id)
        if not entity or entity.entity_type != EntityType.AGENT:
            raise WorkerPromotionError("Agent not found")
        agent = cast("AgentRecord", entity)

        # Get or create orchestrator
        if orchestrator_id:
            orchestrator = await self._get_orchestrator(orchestrator_id)
        else:
            orchestrator = await self._create_orchestrator(
                agent=agent,
                gate_config=gate_config or DEFAULT_PROMOTION_GATES,
            )

        # Update agent to be managed
        await self._update_agent_as_managed(agent, orchestrator)

        # Optionally create worktree
        if create_worktree and not agent.worktree_path:
            await self._setup_worktree(agent, orchestrator)

        # Update orchestrator state
        await self._update_orchestrator_with_worker(orchestrator, agent)

        # Refresh records
        agent_entity = await self.entity_manager.get(agent_id)
        orch_entity = await self.entity_manager.get(orchestrator.id)

        if not agent_entity or agent_entity.entity_type != EntityType.AGENT:
            raise WorkerPromotionError("Failed to refresh agent")
        if not orch_entity or orch_entity.entity_type != EntityType.TASK_ORCHESTRATOR:
            raise WorkerPromotionError("Failed to refresh orchestrator")

        log.info(
            "Worker promoted successfully",
            agent_id=agent_id,
            orchestrator_id=orchestrator.id,
        )

        return cast("AgentRecord", agent_entity), cast("TaskOrchestratorRecord", orch_entity)

    async def _get_orchestrator(self, orchestrator_id: str) -> TaskOrchestratorRecord:
        """Get existing orchestrator."""
        entity = await self.entity_manager.get(orchestrator_id)
        if not entity or entity.entity_type != EntityType.TASK_ORCHESTRATOR:
            raise WorkerPromotionError(f"Orchestrator not found: {orchestrator_id}")

        # Validate orchestrator status
        if entity.status in (
            TaskOrchestratorStatus.COMPLETE,
            TaskOrchestratorStatus.FAILED,
        ):
            raise WorkerPromotionError(f"Orchestrator in terminal status: {entity.status.value}")

        return cast("TaskOrchestratorRecord", entity)

    async def _create_orchestrator(
        self,
        agent: AgentRecord,
        gate_config: list[QualityGateType],
    ) -> TaskOrchestratorRecord:
        """Create new orchestrator for the agent's task."""
        from sibyl.agents.task_orchestrator import TaskOrchestratorService

        if not agent.task_id:
            raise WorkerPromotionError("Agent has no task to orchestrate")

        # Get managers (lazy-create if not provided)
        relationship_manager = await self._get_relationship_manager()
        agent_runner = await self._get_agent_runner()
        worktree_manager = await self._get_worktree_manager()

        service = TaskOrchestratorService(
            entity_manager=self.entity_manager,
            relationship_manager=relationship_manager,
            agent_runner=agent_runner,
            worktree_manager=worktree_manager,
            org_id=self.org_id,
            project_id=self.project_id,
        )

        # Fetch the task
        task_entity = await self.entity_manager.get(agent.task_id)
        if not task_entity or task_entity.entity_type != EntityType.TASK:
            raise WorkerPromotionError(f"Task not found: {agent.task_id}")

        orchestrator = await service.create(
            task=cast("Task", task_entity),
            gate_config=gate_config,
        )

        log.info(
            "Created orchestrator for promotion",
            orchestrator_id=orchestrator.id,
            task_id=agent.task_id,
        )

        return orchestrator

    async def _get_relationship_manager(self) -> "RelationshipManager":
        """Get or create relationship manager."""
        if self._relationship_manager:
            return self._relationship_manager

        from sibyl_core.graph.client import get_graph_client
        from sibyl_core.graph.relationships import RelationshipManager

        client = await get_graph_client()
        return RelationshipManager(client, group_id=self.org_id)

    async def _get_agent_runner(self) -> "AgentRunner":
        """Get or create agent runner."""
        if self._agent_runner:
            return self._agent_runner

        from sibyl.agents.runner import AgentRunner

        worktree_manager = await self._get_worktree_manager()
        return AgentRunner(
            entity_manager=self.entity_manager,
            worktree_manager=worktree_manager,
            org_id=self.org_id,
            project_id=self.project_id,
        )

    async def _get_worktree_manager(self) -> "WorktreeManager":
        """Get or create worktree manager."""
        if self._worktree_manager:
            return self._worktree_manager

        raise WorkerPromotionError(
            "WorktreeManager required but not provided. "
            "Pass worktree_manager to WorkerPromotionService constructor."
        )

    async def _update_agent_as_managed(
        self,
        agent: AgentRecord,
        orchestrator: TaskOrchestratorRecord,
    ) -> None:
        """Update agent to be managed by orchestrator."""
        await self.entity_manager.update(
            agent.id,
            {
                "standalone": False,
                "task_orchestrator_id": orchestrator.id,
            },
        )

        log.debug(
            "Agent updated as managed",
            agent_id=agent.id,
            orchestrator_id=orchestrator.id,
        )

    async def _setup_worktree(
        self,
        agent: AgentRecord,
        orchestrator: TaskOrchestratorRecord,
    ) -> None:
        """Create isolated worktree for the agent.

        Uses WorktreeManager to create a git worktree for isolation.
        """
        try:
            manager = await self._get_worktree_manager()

            worktree = await manager.create_worktree(
                agent_id=agent.id,
                task_id=agent.task_id,
            )

            # Update agent with worktree info
            await self.entity_manager.update(
                agent.id,
                {
                    "worktree_id": worktree.id,
                    "worktree_path": worktree.path,
                    "worktree_branch": worktree.branch_name,
                },
            )

            log.info(
                "Worktree created for promoted agent",
                agent_id=agent.id,
                worktree_id=worktree.id,
            )

        except Exception as e:
            # Non-fatal - agent can work without worktree
            log.warning(
                "Failed to create worktree for promoted agent",
                agent_id=agent.id,
                error=str(e),
            )

    async def _update_orchestrator_with_worker(
        self,
        orchestrator: TaskOrchestratorRecord,
        agent: AgentRecord,
    ) -> None:
        """Update orchestrator to reference the promoted worker."""
        await self.entity_manager.update(
            orchestrator.id,
            {
                "worker_id": agent.id,
                "current_phase": TaskOrchestratorPhase.REVIEW.value,  # Skip to review
            },
        )

        log.debug(
            "Orchestrator updated with worker",
            orchestrator_id=orchestrator.id,
            worker_id=agent.id,
        )

    async def demote(self, agent_id: str) -> AgentRecord:
        """Demote a managed agent back to standalone.

        Used when removing an agent from orchestrator control.

        Args:
            agent_id: Agent UUID to demote

        Returns:
            Updated AgentRecord

        Raises:
            WorkerPromotionError: If demotion fails
        """
        entity = await self.entity_manager.get(agent_id)
        if not entity or entity.entity_type != EntityType.AGENT:
            raise WorkerPromotionError("Agent not found")

        agent = cast("AgentRecord", entity)

        if agent.standalone:
            raise WorkerPromotionError("Agent is already standalone")

        # Update agent
        await self.entity_manager.update(
            agent_id,
            {
                "standalone": True,
                "task_orchestrator_id": None,
            },
        )

        log.info("Agent demoted to standalone", agent_id=agent_id)

        entity = await self.entity_manager.get(agent_id)
        if not entity or entity.entity_type != EntityType.AGENT:
            raise WorkerPromotionError("Failed to refresh agent")

        return cast("AgentRecord", entity)


async def create_promotion_service(
    entity_manager: "EntityManager",
    org_id: str,
    project_id: str,
) -> WorkerPromotionService:
    """Factory function for creating WorkerPromotionService.

    Args:
        entity_manager: Graph client
        org_id: Organization UUID
        project_id: Project UUID

    Returns:
        Configured WorkerPromotionService
    """
    return WorkerPromotionService(
        entity_manager=entity_manager,
        org_id=org_id,
        project_id=project_id,
    )
