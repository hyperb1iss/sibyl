"""MetaOrchestratorService - Project-level coordination (Tier 1).

Manages the overall project development by spawning and coordinating
TaskOrchestrators for selected tasks.

Part of the three-tier orchestration model:
- Tier 1: MetaOrchestrator (project-level coordination) <- THIS
- Tier 2: TaskOrchestrator (per-task build loop)
- Tier 3: Worker Agents (actual implementation)
"""

from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast
from uuid import uuid4

import structlog

from sibyl_core.models import (
    EntityType,
    MetaOrchestratorRecord,
    MetaOrchestratorStatus,
    QualityGateType,
    SprintStrategy,
    Task,
)

if TYPE_CHECKING:
    from sibyl.agents.agent_runner import AgentRunner
    from sibyl.agents.worktrees import WorktreeManager
    from sibyl_core.graph import EntityManager, RelationshipManager

log = structlog.get_logger()


class MetaOrchestratorError(Exception):
    """Base exception for MetaOrchestrator operations."""


class BudgetExceededError(MetaOrchestratorError):
    """Raised when cost budget would be exceeded."""


# Default quality gates for spawned TaskOrchestrators
DEFAULT_GATE_CONFIG = [
    QualityGateType.LINT,
    QualityGateType.TYPECHECK,
    QualityGateType.TEST,
    QualityGateType.AI_REVIEW,
]


class MetaOrchestratorService:
    """Project-level orchestration coordinator.

    Manages sprint execution by:
    - Maintaining a queue of tasks to process
    - Spawning TaskOrchestrators according to strategy
    - Tracking budget and cost across tasks
    - Aggregating metrics for reporting
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
        """Initialize MetaOrchestratorService.

        Args:
            entity_manager: Graph client for persistence
            org_id: Organization UUID
            project_id: Project UUID being orchestrated
            relationship_manager: For creating relationships (optional for read-only ops)
            agent_runner: For spawning workers (optional for read-only ops)
            worktree_manager: For worktree management (optional for read-only ops)
        """
        self.entity_manager = entity_manager
        self.org_id = org_id
        self.project_id = project_id
        self.relationship_manager = relationship_manager
        self.agent_runner = agent_runner
        self.worktree_manager = worktree_manager

    async def get_or_create(self) -> MetaOrchestratorRecord:
        """Get existing or create new MetaOrchestrator for this project.

        MetaOrchestrator is a singleton per project.

        Returns:
            MetaOrchestratorRecord for this project
        """
        # Try to find existing
        existing = await self._find_existing()
        if existing:
            return existing

        # Create new
        record = MetaOrchestratorRecord(
            id=str(uuid4()),
            name=f"MetaOrchestrator: {self.project_id[:8]}",
            organization_id=self.org_id,
            project_id=self.project_id,
            status=MetaOrchestratorStatus.IDLE,
            strategy=SprintStrategy.SEQUENTIAL,
        )

        await self.entity_manager.create_direct(record, generate_embedding=False)

        log.info(
            "Created MetaOrchestrator",
            meta_orchestrator_id=record.id,
            project_id=self.project_id,
        )

        return record

    async def _find_existing(self) -> MetaOrchestratorRecord | None:
        """Find existing MetaOrchestrator for this project."""
        results = await self.entity_manager.list_by_type(
            entity_type=EntityType.META_ORCHESTRATOR,
            limit=10,
        )

        for entity in results:
            if entity.entity_type != EntityType.META_ORCHESTRATOR:
                continue
            record = cast("MetaOrchestratorRecord", entity)
            if record.project_id == self.project_id:
                return record

        return None

    async def get(self, orchestrator_id: str) -> MetaOrchestratorRecord | None:
        """Get MetaOrchestrator by ID.

        Args:
            orchestrator_id: UUID to fetch

        Returns:
            MetaOrchestratorRecord or None if not found
        """
        entity = await self.entity_manager.get(orchestrator_id)
        if not entity or entity.entity_type != EntityType.META_ORCHESTRATOR:
            return None
        return cast("MetaOrchestratorRecord", entity)

    async def queue_task(
        self,
        orchestrator_id: str,
        task_id: str,
    ) -> MetaOrchestratorRecord:
        """Add a task to the processing queue.

        Args:
            orchestrator_id: MetaOrchestrator UUID
            task_id: Task UUID to queue

        Returns:
            Updated MetaOrchestratorRecord

        Raises:
            MetaOrchestratorError: If orchestrator not found
        """
        record = await self.get(orchestrator_id)
        if not record:
            raise MetaOrchestratorError(f"MetaOrchestrator not found: {orchestrator_id}")

        # Avoid duplicates
        if task_id not in record.task_queue:
            task_queue = [*record.task_queue, task_id]
            await self.entity_manager.update(
                orchestrator_id,
                {"task_queue": task_queue},
            )

            log.info(
                "Task queued",
                meta_orchestrator_id=orchestrator_id,
                task_id=task_id,
                queue_size=len(task_queue),
            )

        return await self.get(orchestrator_id)  # type: ignore[return-value]

    async def queue_tasks(
        self,
        orchestrator_id: str,
        task_ids: list[str],
    ) -> MetaOrchestratorRecord:
        """Add multiple tasks to the processing queue.

        Args:
            orchestrator_id: MetaOrchestrator UUID
            task_ids: Task UUIDs to queue

        Returns:
            Updated MetaOrchestratorRecord
        """
        record = await self.get(orchestrator_id)
        if not record:
            raise MetaOrchestratorError(f"MetaOrchestrator not found: {orchestrator_id}")

        # Add new tasks, avoiding duplicates
        current_queue = set(record.task_queue)
        new_tasks = [t for t in task_ids if t not in current_queue]
        task_queue = [*record.task_queue, *new_tasks]

        await self.entity_manager.update(
            orchestrator_id,
            {"task_queue": task_queue},
        )

        log.info(
            "Tasks queued",
            meta_orchestrator_id=orchestrator_id,
            added=len(new_tasks),
            queue_size=len(task_queue),
        )

        return await self.get(orchestrator_id)  # type: ignore[return-value]

    async def start(
        self,
        orchestrator_id: str,
        gate_config: list[QualityGateType] | None = None,
    ) -> MetaOrchestratorRecord:
        """Start processing the task queue.

        Spawns TaskOrchestrators according to the configured strategy.

        Args:
            orchestrator_id: MetaOrchestrator UUID
            gate_config: Quality gates for spawned orchestrators

        Returns:
            Updated MetaOrchestratorRecord

        Raises:
            MetaOrchestratorError: If no tasks queued or already running
        """
        record = await self.get(orchestrator_id)
        if not record:
            raise MetaOrchestratorError(f"MetaOrchestrator not found: {orchestrator_id}")

        if record.status == MetaOrchestratorStatus.RUNNING:
            raise MetaOrchestratorError("MetaOrchestrator is already running")

        if not record.task_queue:
            raise MetaOrchestratorError("No tasks queued")

        # Update status to running
        await self.entity_manager.update(
            orchestrator_id,
            {
                "status": MetaOrchestratorStatus.RUNNING.value,
                "sprint_started_at": datetime.now(UTC).isoformat(),
            },
        )

        # Spawn initial TaskOrchestrators based on strategy
        await self._spawn_orchestrators(
            orchestrator_id,
            record,
            gate_config or DEFAULT_GATE_CONFIG,
        )

        log.info(
            "MetaOrchestrator started",
            meta_orchestrator_id=orchestrator_id,
            queue_size=len(record.task_queue),
            strategy=record.strategy.value,
        )

        return await self.get(orchestrator_id)  # type: ignore[return-value]

    async def _spawn_orchestrators(
        self,
        orchestrator_id: str,
        record: MetaOrchestratorRecord,
        gate_config: list[QualityGateType],
    ) -> None:
        """Spawn TaskOrchestrators according to strategy."""
        from sibyl.agents.task_orchestrator import TaskOrchestratorService

        # Validate we have required dependencies for spawning
        if not all([self.relationship_manager, self.agent_runner, self.worktree_manager]):
            raise MetaOrchestratorError(
                "Cannot spawn orchestrators: missing required dependencies "
                "(relationship_manager, agent_runner, worktree_manager)"
            )

        task_service = TaskOrchestratorService(
            entity_manager=self.entity_manager,
            relationship_manager=self.relationship_manager,  # type: ignore[arg-type]
            agent_runner=self.agent_runner,  # type: ignore[arg-type]
            worktree_manager=self.worktree_manager,  # type: ignore[arg-type]
            org_id=self.org_id,
            project_id=self.project_id,
        )

        # Determine how many to spawn
        if record.strategy == SprintStrategy.SEQUENTIAL:
            spawn_count = 1
        elif record.strategy == SprintStrategy.PARALLEL:
            available_slots = record.max_concurrent - len(record.active_orchestrators)
            spawn_count = min(available_slots, len(record.task_queue))
        else:  # PRIORITY - spawn one at a time, highest priority first
            spawn_count = 1

        # Get tasks to spawn
        tasks_to_spawn = record.task_queue[:spawn_count]
        active_orchestrators = list(record.active_orchestrators)
        remaining_queue = record.task_queue[spawn_count:]

        for task_id in tasks_to_spawn:
            # Check budget
            if record.spent_usd >= record.budget_usd:
                log.warning(
                    "Budget exhausted, pausing orchestration",
                    meta_orchestrator_id=orchestrator_id,
                    spent=record.spent_usd,
                    budget=record.budget_usd,
                )
                await self.pause(orchestrator_id, "Budget exhausted")
                return

            # Fetch the Task object
            task_entity = await self.entity_manager.get(task_id)
            if not task_entity or task_entity.entity_type != EntityType.TASK:
                log.warning("Task not found, skipping", task_id=task_id)
                continue

            # Spawn TaskOrchestrator
            task_orch = await task_service.create(
                task=cast("Task", task_entity),
                meta_orchestrator_id=orchestrator_id,
                gate_config=gate_config,
            )

            active_orchestrators.append(task_orch.id)

            log.info(
                "Spawned TaskOrchestrator",
                meta_orchestrator_id=orchestrator_id,
                task_orchestrator_id=task_orch.id,
                task_id=task_id,
            )

        # Update MetaOrchestrator state
        await self.entity_manager.update(
            orchestrator_id,
            {
                "task_queue": remaining_queue,
                "active_orchestrators": active_orchestrators,
            },
        )

    async def on_task_complete(
        self,
        orchestrator_id: str,
        task_orchestrator_id: str,
        success: bool,
        cost_usd: float = 0.0,
        rework_cycles: int = 0,
    ) -> MetaOrchestratorRecord:
        """Handle completion of a TaskOrchestrator.

        Updates metrics and potentially spawns next task.

        Args:
            orchestrator_id: MetaOrchestrator UUID
            task_orchestrator_id: Completed TaskOrchestrator UUID
            success: Whether task completed successfully
            cost_usd: Cost incurred by this task
            rework_cycles: Number of rework cycles this task needed

        Returns:
            Updated MetaOrchestratorRecord
        """
        record = await self.get(orchestrator_id)
        if not record:
            raise MetaOrchestratorError(f"MetaOrchestrator not found: {orchestrator_id}")

        # Update metrics
        tasks_completed = record.tasks_completed + (1 if success else 0)
        tasks_failed = record.tasks_failed + (0 if success else 1)
        spent_usd = record.spent_usd + cost_usd
        total_rework = record.total_rework_cycles + rework_cycles

        # Remove from active list
        active_orchestrators = [o for o in record.active_orchestrators if o != task_orchestrator_id]

        await self.entity_manager.update(
            orchestrator_id,
            {
                "tasks_completed": tasks_completed,
                "tasks_failed": tasks_failed,
                "spent_usd": spent_usd,
                "total_rework_cycles": total_rework,
                "active_orchestrators": active_orchestrators,
            },
        )

        # Check if we should alert on budget
        if spent_usd >= record.budget_usd * record.cost_alert_threshold:
            log.warning(
                "Budget threshold reached",
                meta_orchestrator_id=orchestrator_id,
                spent=spent_usd,
                threshold=record.budget_usd * record.cost_alert_threshold,
            )

        # Spawn next task if queue not empty and still running
        record = await self.get(orchestrator_id)
        if not record:
            raise MetaOrchestratorError(f"MetaOrchestrator not found: {orchestrator_id}")
        if record.status == MetaOrchestratorStatus.RUNNING and record.task_queue:
            await self._spawn_orchestrators(
                orchestrator_id,
                record,
                DEFAULT_GATE_CONFIG,
            )
        elif not record.task_queue and not record.active_orchestrators:
            # All done
            await self.entity_manager.update(
                orchestrator_id,
                {"status": MetaOrchestratorStatus.IDLE.value},
            )
            log.info(
                "Sprint complete",
                meta_orchestrator_id=orchestrator_id,
                completed=tasks_completed,
                failed=tasks_failed,
            )

        return await self.get(orchestrator_id)  # type: ignore[return-value]

    async def pause(
        self,
        orchestrator_id: str,
        reason: str = "user_requested",
    ) -> MetaOrchestratorRecord:
        """Pause orchestration.

        Active TaskOrchestrators continue but no new ones spawn.

        Args:
            orchestrator_id: MetaOrchestrator UUID
            reason: Why pausing

        Returns:
            Updated MetaOrchestratorRecord
        """
        await self.entity_manager.update(
            orchestrator_id,
            {"status": MetaOrchestratorStatus.PAUSED.value},
        )

        log.info("MetaOrchestrator paused", meta_orchestrator_id=orchestrator_id, reason=reason)

        return await self.get(orchestrator_id)  # type: ignore[return-value]

    async def resume(self, orchestrator_id: str) -> MetaOrchestratorRecord:
        """Resume paused orchestration.

        Args:
            orchestrator_id: MetaOrchestrator UUID

        Returns:
            Updated MetaOrchestratorRecord
        """
        record = await self.get(orchestrator_id)
        if not record:
            raise MetaOrchestratorError(f"MetaOrchestrator not found: {orchestrator_id}")

        if record.status != MetaOrchestratorStatus.PAUSED:
            raise MetaOrchestratorError(
                f"Can only resume paused orchestrator, current status: {record.status.value}"
            )

        await self.entity_manager.update(
            orchestrator_id,
            {"status": MetaOrchestratorStatus.RUNNING.value},
        )

        # Spawn if there's capacity
        record = await self.get(orchestrator_id)
        if not record:
            raise MetaOrchestratorError(f"MetaOrchestrator not found: {orchestrator_id}")
        if record.task_queue:
            available_slots = record.max_concurrent - len(record.active_orchestrators)
            if available_slots > 0:
                await self._spawn_orchestrators(
                    orchestrator_id,
                    record,
                    DEFAULT_GATE_CONFIG,
                )

        log.info("MetaOrchestrator resumed", meta_orchestrator_id=orchestrator_id)

        return await self.get(orchestrator_id)  # type: ignore[return-value]

    async def set_strategy(
        self,
        orchestrator_id: str,
        strategy: SprintStrategy,
        max_concurrent: int | None = None,
    ) -> MetaOrchestratorRecord:
        """Update orchestration strategy.

        Args:
            orchestrator_id: MetaOrchestrator UUID
            strategy: New strategy
            max_concurrent: Max concurrent tasks (for parallel)

        Returns:
            Updated MetaOrchestratorRecord
        """
        updates: dict[str, str | int] = {"strategy": strategy.value}
        if max_concurrent is not None:
            updates["max_concurrent"] = max_concurrent

        await self.entity_manager.update(orchestrator_id, updates)

        log.info(
            "Strategy updated",
            meta_orchestrator_id=orchestrator_id,
            strategy=strategy.value,
        )

        return await self.get(orchestrator_id)  # type: ignore[return-value]

    async def set_budget(
        self,
        orchestrator_id: str,
        budget_usd: float,
        alert_threshold: float = 0.8,
    ) -> MetaOrchestratorRecord:
        """Update budget settings.

        Args:
            orchestrator_id: MetaOrchestrator UUID
            budget_usd: New budget limit
            alert_threshold: When to alert (0.0-1.0)

        Returns:
            Updated MetaOrchestratorRecord
        """
        await self.entity_manager.update(
            orchestrator_id,
            {
                "budget_usd": budget_usd,
                "cost_alert_threshold": alert_threshold,
            },
        )

        log.info(
            "Budget updated",
            meta_orchestrator_id=orchestrator_id,
            budget=budget_usd,
            threshold=alert_threshold,
        )

        return await self.get(orchestrator_id)  # type: ignore[return-value]

    async def get_status(self, orchestrator_id: str) -> dict:
        """Get detailed status information.

        Args:
            orchestrator_id: MetaOrchestrator UUID

        Returns:
            Status dict with metrics and state
        """
        record = await self.get(orchestrator_id)
        if not record:
            raise MetaOrchestratorError(f"MetaOrchestrator not found: {orchestrator_id}")

        return {
            "id": record.id,
            "status": record.status.value,
            "strategy": record.strategy.value,
            "queue_size": len(record.task_queue),
            "active_count": len(record.active_orchestrators),
            "tasks_completed": record.tasks_completed,
            "tasks_failed": record.tasks_failed,
            "total_rework_cycles": record.total_rework_cycles,
            "budget_usd": record.budget_usd,
            "spent_usd": record.spent_usd,
            "budget_remaining": record.budget_usd - record.spent_usd,
            "budget_utilization": record.spent_usd / record.budget_usd
            if record.budget_usd > 0
            else 0,
        }


async def create_meta_orchestrator_service(
    entity_manager: "EntityManager",
    org_id: str,
    project_id: str,
    relationship_manager: "RelationshipManager | None" = None,
    agent_runner: "AgentRunner | None" = None,
    worktree_manager: "WorktreeManager | None" = None,
) -> MetaOrchestratorService:
    """Factory function for creating MetaOrchestratorService.

    Args:
        entity_manager: Graph client
        org_id: Organization UUID
        project_id: Project UUID
        relationship_manager: For creating relationships (optional for read-only ops)
        agent_runner: For spawning workers (optional for read-only ops)
        worktree_manager: For worktree management (optional for read-only ops)

    Returns:
        Configured MetaOrchestratorService
    """
    return MetaOrchestratorService(
        entity_manager=entity_manager,
        org_id=org_id,
        project_id=project_id,
        relationship_manager=relationship_manager,
        agent_runner=agent_runner,
        worktree_manager=worktree_manager,
    )
