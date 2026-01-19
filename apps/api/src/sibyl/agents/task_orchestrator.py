"""TaskOrchestratorService - Per-task build loop coordinator.

Manages the implement → review → rework cycle for a single task,
with Ralph Loop safety controls to prevent iterative degradation.

Part of the three-tier orchestration model:
- Tier 1: MetaOrchestrator (project-level coordination)
- Tier 2: TaskOrchestrator (per-task build loop) <- THIS
- Tier 3: Worker Agents (actual implementation)
"""

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

import structlog

from sibyl_core.models import (
    AgentSpawnSource,
    AgentType,
    EntityType,
    QualityGateType,
    Relationship,
    RelationshipType,
    Task,
    TaskOrchestratorPhase,
    TaskOrchestratorRecord,
    TaskOrchestratorStatus,
    TaskStatus,
)

if TYPE_CHECKING:
    from sibyl.agents.runner import AgentInstance, AgentRunner
    from sibyl.agents.worktree import WorktreeManager
    from sibyl_core.graph import EntityManager, RelationshipManager

log = structlog.get_logger()


class TaskOrchestratorError(Exception):
    """Base exception for TaskOrchestrator operations."""


class MaxReworkExceededError(TaskOrchestratorError):
    """Raised when rework attempts exceed the Ralph Loop safety limit."""


class QualityGateResult:
    """Result from running a quality gate."""

    def __init__(
        self,
        gate_type: QualityGateType,
        passed: bool,
        output: str = "",
        errors: list[str] | None = None,
        duration_ms: int = 0,
    ):
        self.gate_type = gate_type
        self.passed = passed
        self.output = output
        self.errors = errors or []
        self.duration_ms = duration_ms

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dict for storage."""
        return {
            "gate_type": self.gate_type.value,
            "passed": self.passed,
            "output": self.output,
            "errors": self.errors,
            "duration_ms": self.duration_ms,
            "timestamp": datetime.now(UTC).isoformat(),
        }


class TaskOrchestratorService:
    """Per-task build loop coordinator.

    Manages the implement → review → rework cycle with:
    - Worker lifecycle (spawn, monitor, collect results)
    - Quality gate execution (lint, typecheck, test, AI review)
    - Rework tracking with Ralph Loop safety controls
    - State persistence via TaskOrchestratorRecord

    The build loop:
    1. IMPLEMENT: Worker implements the task
    2. REVIEW: Run quality gates on worker's changes
    3. REWORK: If gates fail, send feedback to worker (max 3 iterations)
    4. HUMAN_REVIEW: Optional human approval before merge
    5. COMPLETE: All gates passed, ready for merge
    """

    # Default quality gates in execution order
    DEFAULT_GATES = [
        QualityGateType.LINT,
        QualityGateType.TYPECHECK,
        QualityGateType.TEST,
        QualityGateType.AI_REVIEW,
    ]

    def __init__(
        self,
        entity_manager: "EntityManager",
        relationship_manager: "RelationshipManager",
        agent_runner: "AgentRunner",
        worktree_manager: "WorktreeManager",
        org_id: str,
        project_id: str,
    ):
        """Initialize the TaskOrchestratorService.

        Args:
            entity_manager: For persisting orchestrator state
            relationship_manager: For creating relationships
            agent_runner: For spawning/managing workers
            worktree_manager: For managing isolated workspaces
            org_id: Organization UUID
            project_id: Project UUID
        """
        self.entity_manager = entity_manager
        self.relationship_manager = relationship_manager
        self.agent_runner = agent_runner
        self.worktree_manager = worktree_manager
        self.org_id = org_id
        self.project_id = project_id

    # -------------------------------------------------------------------------
    # Orchestrator Lifecycle
    # -------------------------------------------------------------------------

    async def create(
        self,
        task: Task,
        meta_orchestrator_id: str | None = None,
        gate_config: list[QualityGateType] | None = None,
        max_rework_attempts: int = 3,
    ) -> TaskOrchestratorRecord:
        """Create a new TaskOrchestrator for a task.

        Args:
            task: The task to orchestrate
            meta_orchestrator_id: Parent MetaOrchestrator (if coordinated)
            gate_config: Quality gates to run (defaults to standard set)
            max_rework_attempts: Ralph Loop safety limit

        Returns:
            Created TaskOrchestratorRecord
        """
        orchestrator_id = f"taskorch_{uuid4().hex[:16]}"

        record = TaskOrchestratorRecord(
            id=orchestrator_id,
            name=f"TaskOrchestrator: {task.name[:50]}",
            organization_id=self.org_id,
            project_id=self.project_id,
            meta_orchestrator_id=meta_orchestrator_id,
            task_id=task.id,
            status=TaskOrchestratorStatus.INITIALIZING,
            current_phase=TaskOrchestratorPhase.IMPLEMENT,
            gate_config=gate_config or self.DEFAULT_GATES,
            max_rework_attempts=max_rework_attempts,
            started_at=datetime.now(UTC),
        )

        await self.entity_manager.create(record)

        # Create relationships
        # TaskOrchestrator -> Task (WORKS_ON)
        await self.relationship_manager.create(
            Relationship(
                id=f"rel_{uuid4().hex[:16]}",
                source_id=orchestrator_id,
                target_id=task.id,
                relationship_type=RelationshipType.WORKS_ON,
            )
        )

        # If managed by MetaOrchestrator, create relationship
        if meta_orchestrator_id:
            await self.relationship_manager.create(
                Relationship(
                    id=f"rel_{uuid4().hex[:16]}",
                    source_id=orchestrator_id,
                    target_id=meta_orchestrator_id,
                    relationship_type=RelationshipType.MANAGED_BY,
                )
            )

        log.info(
            "Created TaskOrchestrator",
            orchestrator_id=orchestrator_id,
            task_id=task.id,
            gates=len(record.gate_config),
        )

        return record

    async def get(self, orchestrator_id: str) -> TaskOrchestratorRecord | None:
        """Get a TaskOrchestrator by ID.

        Args:
            orchestrator_id: Orchestrator UUID

        Returns:
            TaskOrchestratorRecord or None if not found
        """
        entity = await self.entity_manager.get(orchestrator_id)
        if not entity or entity.entity_type != EntityType.TASK_ORCHESTRATOR:
            return None

        return cast("TaskOrchestratorRecord", entity)

    async def list_for_project(
        self,
        status: TaskOrchestratorStatus | None = None,
        limit: int = 50,
    ) -> list[TaskOrchestratorRecord]:
        """List TaskOrchestrators for this project.

        Args:
            status: Filter by status
            limit: Maximum results

        Returns:
            List of TaskOrchestratorRecords
        """
        entities = await self.entity_manager.list_by_type(
            EntityType.TASK_ORCHESTRATOR,
            limit=limit * 2,
        )

        # Convert to TaskOrchestratorRecord if needed
        records = [
            cast("TaskOrchestratorRecord", e)
            for e in entities
            if e.entity_type == EntityType.TASK_ORCHESTRATOR
        ]

        # Filter by project
        records = [r for r in records if r.project_id == self.project_id]

        # Filter by status
        if status:
            records = [r for r in records if r.status == status]

        return records[:limit]

    # -------------------------------------------------------------------------
    # Build Loop State Machine
    # -------------------------------------------------------------------------

    async def start(self, orchestrator_id: str) -> TaskOrchestratorRecord:
        """Start the build loop by spawning a worker.

        Args:
            orchestrator_id: Orchestrator to start

        Returns:
            Updated TaskOrchestratorRecord
        """
        record = await self.get(orchestrator_id)
        if not record:
            raise TaskOrchestratorError(f"Orchestrator not found: {orchestrator_id}")

        if record.status != TaskOrchestratorStatus.INITIALIZING:
            raise TaskOrchestratorError(f"Cannot start orchestrator in state: {record.status}")

        # Get the task
        task = await self.entity_manager.get(record.task_id)
        if not task:
            raise TaskOrchestratorError(f"Task not found: {record.task_id}")

        # Spawn worker agent
        worker = await self._spawn_worker(record, task)

        # Update record
        await self.entity_manager.update(
            orchestrator_id,
            {
                "worker_id": worker.id,
                "worktree_id": worker.record.worktree_id,
                "status": TaskOrchestratorStatus.IMPLEMENTING.value,
                "metadata": {
                    **record.metadata,
                    "worker_id": worker.id,
                    "worktree_id": worker.record.worktree_id,
                    "status": TaskOrchestratorStatus.IMPLEMENTING.value,
                },
            },
        )

        # Create orchestration relationship
        await self.relationship_manager.create(
            Relationship(
                id=f"rel_{uuid4().hex[:16]}",
                source_id=orchestrator_id,
                target_id=worker.id,
                relationship_type=RelationshipType.ORCHESTRATES,
            )
        )

        log.info(
            "Started build loop",
            orchestrator_id=orchestrator_id,
            worker_id=worker.id,
        )

        return await self.get(orchestrator_id)  # type: ignore[return-value]

    async def on_worker_complete(
        self,
        orchestrator_id: str,
    ) -> TaskOrchestratorRecord:
        """Handle worker completion - run quality gates.

        Called when the worker signals it has completed implementation.
        Transitions to REVIEWING phase and runs quality gates.

        Args:
            orchestrator_id: Orchestrator whose worker completed

        Returns:
            Updated TaskOrchestratorRecord
        """
        record = await self.get(orchestrator_id)
        if not record:
            raise TaskOrchestratorError(f"Orchestrator not found: {orchestrator_id}")

        if record.status not in (
            TaskOrchestratorStatus.IMPLEMENTING,
            TaskOrchestratorStatus.REWORKING,
        ):
            raise TaskOrchestratorError(f"Worker completion in unexpected state: {record.status}")

        # Transition to reviewing
        await self._update_status(
            orchestrator_id,
            TaskOrchestratorStatus.REVIEWING,
            TaskOrchestratorPhase.REVIEW,
        )

        # Run quality gates
        gate_results = await self._run_quality_gates(record)

        # Store results
        await self.entity_manager.update(
            orchestrator_id,
            {
                "gate_results": [r.to_dict() for r in gate_results],
                "metadata": {
                    **record.metadata,
                    "gate_results": [r.to_dict() for r in gate_results],
                },
            },
        )

        # Evaluate results
        all_passed = all(r.passed for r in gate_results)

        if all_passed:
            return await self._handle_gates_passed(orchestrator_id, record)
        return await self._handle_gates_failed(orchestrator_id, record, gate_results)

    async def _handle_gates_passed(
        self,
        orchestrator_id: str,
        record: TaskOrchestratorRecord,
    ) -> TaskOrchestratorRecord:
        """Handle all quality gates passing.

        Args:
            orchestrator_id: Orchestrator ID
            record: Current record

        Returns:
            Updated TaskOrchestratorRecord
        """
        # Check if human review is required
        if QualityGateType.HUMAN_REVIEW in record.gate_config:
            # Request human approval
            approval_id = await self._request_human_review(record)
            await self._update_status(
                orchestrator_id,
                TaskOrchestratorStatus.HUMAN_REVIEW,
                TaskOrchestratorPhase.HUMAN_REVIEW,
            )
            await self.entity_manager.update(
                orchestrator_id,
                {
                    "pending_approval_id": approval_id,
                    "metadata": {
                        **record.metadata,
                        "pending_approval_id": approval_id,
                    },
                },
            )
            log.info(
                "Gates passed, awaiting human review",
                orchestrator_id=orchestrator_id,
            )
        else:
            # All done!
            await self._complete(orchestrator_id)

        return await self.get(orchestrator_id)  # type: ignore[return-value]

    async def _handle_gates_failed(
        self,
        orchestrator_id: str,
        record: TaskOrchestratorRecord,
        results: list[QualityGateResult],
    ) -> TaskOrchestratorRecord:
        """Handle quality gate failures.

        Implements Ralph Loop safety - max 3 rework iterations.

        Args:
            orchestrator_id: Orchestrator ID
            record: Current record
            results: Gate results with failures

        Returns:
            Updated TaskOrchestratorRecord
        """
        new_rework_count = record.rework_count + 1

        # Ralph Loop safety check
        if new_rework_count >= record.max_rework_attempts:
            log.warning(
                "Max rework attempts exceeded (Ralph Loop safety)",
                orchestrator_id=orchestrator_id,
                rework_count=new_rework_count,
                max_attempts=record.max_rework_attempts,
            )
            # Escalate to human
            await self._fail_with_escalation(orchestrator_id, record, results)
            return await self.get(orchestrator_id)  # type: ignore[return-value]

        # Prepare feedback for worker
        feedback = self._compile_gate_feedback(results)

        # Update state
        await self.entity_manager.update(
            orchestrator_id,
            {
                "rework_count": new_rework_count,
                "status": TaskOrchestratorStatus.REWORKING.value,
                "current_phase": TaskOrchestratorPhase.REWORK.value,
                "metadata": {
                    **record.metadata,
                    "rework_count": new_rework_count,
                    "status": TaskOrchestratorStatus.REWORKING.value,
                    "current_phase": TaskOrchestratorPhase.REWORK.value,
                },
            },
        )

        # Send feedback to worker
        await self._send_rework_feedback(record, feedback)

        log.info(
            "Requesting rework",
            orchestrator_id=orchestrator_id,
            rework_count=new_rework_count,
            failed_gates=[r.gate_type.value for r in results if not r.passed],
        )

        return await self.get(orchestrator_id)  # type: ignore[return-value]

    async def on_human_approval(
        self,
        orchestrator_id: str,
        approved: bool,
        feedback: str | None = None,
    ) -> TaskOrchestratorRecord:
        """Handle human review response.

        Args:
            orchestrator_id: Orchestrator awaiting approval
            approved: Whether human approved
            feedback: Optional feedback message

        Returns:
            Updated TaskOrchestratorRecord
        """
        record = await self.get(orchestrator_id)
        if not record:
            raise TaskOrchestratorError(f"Orchestrator not found: {orchestrator_id}")

        if record.status != TaskOrchestratorStatus.HUMAN_REVIEW:
            raise TaskOrchestratorError(f"Human approval in unexpected state: {record.status}")

        if approved:
            await self._complete(orchestrator_id)
        else:
            # Treat rejection as a failed gate
            result = QualityGateResult(
                gate_type=QualityGateType.HUMAN_REVIEW,
                passed=False,
                output=feedback or "Human review rejected",
                errors=[feedback] if feedback else ["Review rejected"],
            )
            await self._handle_gates_failed(orchestrator_id, record, [result])

        return await self.get(orchestrator_id)  # type: ignore[return-value]

    async def pause(self, orchestrator_id: str, reason: str = "user_request") -> bool:
        """Pause the orchestrator.

        Args:
            orchestrator_id: Orchestrator to pause
            reason: Why it's being paused

        Returns:
            True if paused successfully
        """
        record = await self.get(orchestrator_id)
        if not record:
            return False

        # Pause the worker
        if record.worker_id:
            worker = await self.agent_runner.get_agent(record.worker_id)
            if worker:
                await worker.pause(reason)

        await self._update_status(
            orchestrator_id,
            TaskOrchestratorStatus.PAUSED,
            record.current_phase,
        )

        log.info("Paused orchestrator", orchestrator_id=orchestrator_id, reason=reason)
        return True

    async def resume(self, orchestrator_id: str) -> TaskOrchestratorRecord | None:
        """Resume a paused orchestrator.

        Args:
            orchestrator_id: Orchestrator to resume

        Returns:
            Resumed record or None if not found
        """
        record = await self.get(orchestrator_id)
        if not record or record.status != TaskOrchestratorStatus.PAUSED:
            return None

        # Resume worker
        if record.worker_id:
            worker = await self.agent_runner.get_agent(record.worker_id)
            if worker:
                await worker.resume()

        # Determine appropriate status based on phase
        status_map = {
            TaskOrchestratorPhase.IMPLEMENT: TaskOrchestratorStatus.IMPLEMENTING,
            TaskOrchestratorPhase.REVIEW: TaskOrchestratorStatus.REVIEWING,
            TaskOrchestratorPhase.REWORK: TaskOrchestratorStatus.REWORKING,
            TaskOrchestratorPhase.HUMAN_REVIEW: TaskOrchestratorStatus.HUMAN_REVIEW,
        }
        new_status = status_map.get(record.current_phase, TaskOrchestratorStatus.IMPLEMENTING)

        await self._update_status(orchestrator_id, new_status, record.current_phase)

        log.info("Resumed orchestrator", orchestrator_id=orchestrator_id)
        return await self.get(orchestrator_id)

    # -------------------------------------------------------------------------
    # Private Methods
    # -------------------------------------------------------------------------

    async def _spawn_worker(
        self,
        record: TaskOrchestratorRecord,
        task: Any,  # Task type
    ) -> "AgentInstance":
        """Spawn a worker agent for the task.

        Args:
            record: TaskOrchestrator record
            task: Task to work on

        Returns:
            Spawned AgentInstance
        """
        prompt = self._build_worker_prompt(task, record)

        worker = await self.agent_runner.spawn(
            prompt=prompt,
            agent_type=AgentType.IMPLEMENTER,
            task=task,
            spawn_source=AgentSpawnSource.ORCHESTRATOR,
            create_worktree=True,
            enable_approvals=True,
        )

        # Mark worker as managed (not standalone)
        await self.entity_manager.update(
            worker.id,
            {
                "task_orchestrator_id": record.id,
                "standalone": False,
                "metadata": {
                    **worker.record.metadata,
                    "task_orchestrator_id": record.id,
                    "standalone": False,
                },
            },
        )

        return worker

    def _build_worker_prompt(self, task: Any, record: TaskOrchestratorRecord) -> str:
        """Build the initial prompt for the worker.

        Args:
            task: Task to implement
            record: TaskOrchestrator for context

        Returns:
            Formatted prompt string
        """
        gates_desc = ", ".join(g.value for g in record.gate_config)

        return f"""You are implementing a task as part of an orchestrated build loop.

## Task
**{task.title}**

{task.description}

## Quality Gates
Your implementation will be reviewed by these automated gates: {gates_desc}

## Instructions
1. Implement the task completely
2. Ensure all quality gates can pass (lint, types, tests)
3. When finished, signal completion so gates can run
4. If gates fail, you'll receive feedback for rework

Focus on clean, well-tested implementation. You have up to {record.max_rework_attempts} rework iterations before human escalation.
"""

    async def _run_quality_gates(
        self,
        record: TaskOrchestratorRecord,
    ) -> list[QualityGateResult]:
        """Run configured quality gates.

        Args:
            record: TaskOrchestrator with gate config

        Returns:
            List of gate results
        """
        results = []

        for gate in record.gate_config:
            if gate == QualityGateType.HUMAN_REVIEW:
                # Human review handled separately
                continue

            result = await self._run_gate(gate, record)
            results.append(result)

            # Early exit on failure (configurable)
            if not result.passed:
                log.info(
                    "Gate failed",
                    gate=gate.value,
                    errors=result.errors[:3],  # First 3 errors
                )

        return results

    async def _run_gate(
        self,
        gate: QualityGateType,
        record: TaskOrchestratorRecord,
    ) -> QualityGateResult:
        """Run a single quality gate.

        Args:
            gate: Gate type to run
            record: TaskOrchestrator for context

        Returns:
            Gate result
        """
        start = datetime.now(UTC)

        # Gate implementations (to be expanded)
        try:
            if gate == QualityGateType.LINT:
                result = await self._run_lint_gate(record)
            elif gate == QualityGateType.TYPECHECK:
                result = await self._run_typecheck_gate(record)
            elif gate == QualityGateType.TEST:
                result = await self._run_test_gate(record)
            elif gate == QualityGateType.AI_REVIEW:
                result = await self._run_ai_review_gate(record)
            elif gate == QualityGateType.SECURITY_SCAN:
                result = await self._run_security_gate(record)
            else:
                result = QualityGateResult(
                    gate_type=gate,
                    passed=True,
                    output=f"Gate {gate.value} not implemented",
                )
        except Exception as e:
            log.exception(f"Gate {gate.value} failed with exception")
            result = QualityGateResult(
                gate_type=gate,
                passed=False,
                output=str(e),
                errors=[str(e)],
            )

        duration = int((datetime.now(UTC) - start).total_seconds() * 1000)
        result.duration_ms = duration

        return result

    async def _run_lint_gate(self, record: TaskOrchestratorRecord) -> QualityGateResult:
        """Run linting gate.

        TODO: Execute actual lint commands in worktree.
        """
        # Placeholder - integrate with actual lint runner
        return QualityGateResult(
            gate_type=QualityGateType.LINT,
            passed=True,
            output="Lint check passed (placeholder)",
        )

    async def _run_typecheck_gate(self, record: TaskOrchestratorRecord) -> QualityGateResult:
        """Run type checking gate.

        TODO: Execute actual typecheck commands in worktree.
        """
        return QualityGateResult(
            gate_type=QualityGateType.TYPECHECK,
            passed=True,
            output="Type check passed (placeholder)",
        )

    async def _run_test_gate(self, record: TaskOrchestratorRecord) -> QualityGateResult:
        """Run test gate.

        TODO: Execute actual test commands in worktree.
        """
        return QualityGateResult(
            gate_type=QualityGateType.TEST,
            passed=True,
            output="Tests passed (placeholder)",
        )

    async def _run_ai_review_gate(self, record: TaskOrchestratorRecord) -> QualityGateResult:
        """Run AI code review gate.

        TODO: Spawn reviewer agent to analyze changes.
        """
        return QualityGateResult(
            gate_type=QualityGateType.AI_REVIEW,
            passed=True,
            output="AI review passed (placeholder)",
        )

    async def _run_security_gate(self, record: TaskOrchestratorRecord) -> QualityGateResult:
        """Run security scan gate.

        TODO: Run security scanning tools.
        """
        return QualityGateResult(
            gate_type=QualityGateType.SECURITY_SCAN,
            passed=True,
            output="Security scan passed (placeholder)",
        )

    def _compile_gate_feedback(self, results: list[QualityGateResult]) -> str:
        """Compile gate results into feedback for worker.

        Args:
            results: Gate results (including failures)

        Returns:
            Formatted feedback string
        """
        lines = ["## Quality Gate Feedback\n"]

        for result in results:
            status = "✅ PASSED" if result.passed else "❌ FAILED"
            lines.append(f"### {result.gate_type.value}: {status}")

            if result.output:
                lines.append(f"\n{result.output}\n")

            if result.errors:
                lines.append("\n**Errors:**")
                lines.extend(f"- {error}" for error in result.errors[:10])
                if len(result.errors) > 10:
                    lines.append(f"- ... and {len(result.errors) - 10} more")

            lines.append("")

        lines.append("\nPlease address the issues above and signal completion when ready.")
        return "\n".join(lines)

    async def _send_rework_feedback(
        self,
        record: TaskOrchestratorRecord,
        feedback: str,
    ) -> None:
        """Send rework feedback to the worker.

        Args:
            record: TaskOrchestrator record
            feedback: Compiled feedback message
        """
        if not record.worker_id:
            log.warning("No worker to send feedback to", orchestrator_id=record.id)
            return

        worker = await self.agent_runner.get_agent(record.worker_id)
        if not worker:
            log.warning("Worker not found", worker_id=record.worker_id)
            return

        # Send feedback as message
        async for _ in worker.send_message(feedback):
            pass  # Consume response stream

    async def _request_human_review(self, record: TaskOrchestratorRecord) -> str:
        """Create human review approval request.

        Args:
            record: TaskOrchestrator record

        Returns:
            Approval request ID
        """
        from sibyl.agents.approval_queue import create_approval_queue
        from sibyl_core.models import ApprovalType

        agent_id = record.worker_id or record.id
        queue = await create_approval_queue(
            entity_manager=self.entity_manager,
            org_id=self.org_id,
            project_id=self.project_id,
            agent_id=agent_id,
            task_id=record.task_id,
        )

        approval = await queue.enqueue(
            approval_type=ApprovalType.REVIEW_PHASE,
            title=f"Review task {record.task_id[-8:]} implementation",
            summary=f"All automated gates passed. Rework count: {record.rework_count}",
            metadata={
                "tool_name": "human_review",
                "orchestrator_id": record.id,
                "task_id": record.task_id,
                "project_id": record.project_id,
                "rework_count": record.rework_count,
                "max_rework_attempts": record.max_rework_attempts,
            },
            priority="high",
        )

        return approval.id

    async def _fail_with_escalation(
        self,
        orchestrator_id: str,
        record: TaskOrchestratorRecord,
        results: list[QualityGateResult],
    ) -> None:
        """Mark orchestrator as failed and escalate to human.

        Args:
            orchestrator_id: Orchestrator ID
            record: Current record
            results: Latest gate results
        """
        # Create escalation approval
        from sibyl_core.models import ApprovalRecord, ApprovalType

        failed_gates = [r.gate_type.value for r in results if not r.passed]

        approval = ApprovalRecord(
            id=f"approval_{uuid4().hex[:16]}",
            name=f"Escalation: task {record.task_id[-8:]}",
            organization_id=self.org_id,
            project_id=self.project_id,
            agent_id=record.worker_id or record.id,
            task_id=record.task_id,
            approval_type=ApprovalType.QUESTION,
            priority="high",
            title=f"Task {record.task_id[-8:]} exceeded rework limit",
            summary=(
                f"Max rework attempts ({record.max_rework_attempts}) exceeded. "
                f"Failed gates: {', '.join(failed_gates)}. "
                "Human intervention required."
            ),
        )

        await self.entity_manager.create(approval)

        await self.entity_manager.update(
            orchestrator_id,
            {
                "status": TaskOrchestratorStatus.FAILED.value,
                "pending_approval_id": approval.id,
                "metadata": {
                    **record.metadata,
                    "status": TaskOrchestratorStatus.FAILED.value,
                    "pending_approval_id": approval.id,
                    "failure_reason": "max_rework_exceeded",
                    "failed_gates": failed_gates,
                },
            },
        )

        log.error(
            "Orchestrator failed - Ralph Loop safety triggered",
            orchestrator_id=orchestrator_id,
            rework_count=record.rework_count,
            failed_gates=failed_gates,
        )

    async def _complete(self, orchestrator_id: str) -> None:
        """Mark orchestrator as complete.

        Args:
            orchestrator_id: Orchestrator to complete
        """
        record = await self.get(orchestrator_id)
        if not record:
            return

        now = datetime.now(UTC)

        # Update orchestrator
        await self.entity_manager.update(
            orchestrator_id,
            {
                "status": TaskOrchestratorStatus.COMPLETE.value,
                "current_phase": TaskOrchestratorPhase.MERGE.value,
                "completed_at": now.isoformat(),
                "metadata": {
                    **record.metadata,
                    "status": TaskOrchestratorStatus.COMPLETE.value,
                    "current_phase": TaskOrchestratorPhase.MERGE.value,
                    "completed_at": now.isoformat(),
                },
            },
        )

        # Update task status
        await self.entity_manager.update(
            record.task_id,
            {
                "status": TaskStatus.REVIEW.value,  # Ready for merge review
            },
        )

        # Notify MetaOrchestrator if exists
        if record.meta_orchestrator_id:
            await self._notify_meta_orchestrator(record)

        log.info(
            "Orchestrator completed",
            orchestrator_id=orchestrator_id,
            rework_count=record.rework_count,
        )

    async def _notify_meta_orchestrator(self, record: TaskOrchestratorRecord) -> None:
        """Notify MetaOrchestrator of completion.

        Args:
            record: Completed TaskOrchestrator record
        """
        # TODO: Implement MetaOrchestrator notification
        # This will be implemented when we build the MetaOrchestrator tier
        log.info(
            "Would notify MetaOrchestrator",
            meta_id=record.meta_orchestrator_id,
            task_id=record.task_id,
        )

    async def _update_status(
        self,
        orchestrator_id: str,
        status: TaskOrchestratorStatus,
        phase: TaskOrchestratorPhase,
    ) -> None:
        """Update orchestrator status and phase.

        Args:
            orchestrator_id: Orchestrator to update
            status: New status
            phase: New phase
        """
        record = await self.get(orchestrator_id)
        if not record:
            return

        await self.entity_manager.update(
            orchestrator_id,
            {
                "status": status.value,
                "current_phase": phase.value,
                "metadata": {
                    **record.metadata,
                    "status": status.value,
                    "current_phase": phase.value,
                },
            },
        )
