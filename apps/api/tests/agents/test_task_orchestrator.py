"""Tests for TaskOrchestratorService - per-task build loop coordinator.

Tests the implement → review → rework cycle with Ralph Loop safety controls.
"""

from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from sibyl.agents.task_orchestrator import (
    QualityGateResult,
    TaskOrchestratorError,
    TaskOrchestratorService,
)
from sibyl_core.models import (
    QualityGateType,
    Task,
    TaskOrchestratorPhase,
    TaskOrchestratorStatus,
    TaskPriority,
    TaskStatus,
)

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def org_id() -> str:
    """Test organization ID."""
    return str(uuid4())


@pytest.fixture
def project_id() -> str:
    """Test project ID."""
    return str(uuid4())


@pytest.fixture
def mock_entity_manager():
    """Mock EntityManager with common operations."""
    manager = MagicMock()
    manager.entities = {}

    async def mock_create(entity):
        manager.entities[entity.id] = entity
        return entity.id

    async def mock_get(entity_id):
        return manager.entities.get(entity_id)

    async def mock_update(entity_id, updates):
        if entity_id in manager.entities:
            entity = manager.entities[entity_id]
            for key, value in updates.items():
                if hasattr(entity, key):
                    setattr(entity, key, value)
            return entity
        return None

    async def mock_list_by_type(entity_type, **kwargs):
        return [e for e in manager.entities.values() if e.entity_type == entity_type]

    manager.create = mock_create
    manager.get = mock_get
    manager.update = mock_update
    manager.list_by_type = mock_list_by_type

    return manager


@pytest.fixture
def mock_relationship_manager():
    """Mock RelationshipManager."""
    manager = MagicMock()

    async def mock_create_rel(**kwargs):
        return f"rel_{uuid4().hex[:8]}"

    manager.create_relationship = mock_create_rel
    return manager


@pytest.fixture
def mock_agent_runner():
    """Mock AgentRunner with spawn capability."""
    runner = MagicMock()

    def make_agent_instance(org_id, project_id):
        """Create mock agent instance."""
        agent_id = f"agent_{uuid4().hex[:16]}"
        record = MagicMock()
        record.id = agent_id
        record.worktree_id = f"worktree_{uuid4().hex[:8]}"
        record.metadata = {}

        instance = MagicMock()
        instance.id = agent_id
        instance.record = record

        async def mock_send_message(msg):
            # Return empty async generator
            return
            yield  # Makes it a generator

        instance.send_message = mock_send_message

        async def mock_pause(reason):
            pass

        async def mock_resume():
            pass

        instance.pause = mock_pause
        instance.resume = mock_resume

        return instance

    runner.make_instance = make_agent_instance

    async def mock_get_agent(agent_id):
        return None

    runner.get_agent = mock_get_agent
    return runner


@pytest.fixture
def mock_worktree_manager():
    """Mock WorktreeManager."""
    return MagicMock()


@pytest.fixture
def sample_task(org_id, project_id) -> Task:
    """Create a sample task for testing."""
    return Task(
        id=f"task_{uuid4().hex[:16]}",
        organization_id=org_id,
        name="Test Task",
        title="Implement feature X",
        description="Add support for feature X with tests",
        status=TaskStatus.TODO,
        priority=TaskPriority.MEDIUM,
        project_id=project_id,
    )


@pytest.fixture
def service(
    mock_entity_manager,
    mock_relationship_manager,
    mock_agent_runner,
    mock_worktree_manager,
    org_id,
    project_id,
) -> TaskOrchestratorService:
    """Create TaskOrchestratorService with mocks."""
    return TaskOrchestratorService(
        entity_manager=mock_entity_manager,
        relationship_manager=mock_relationship_manager,
        agent_runner=mock_agent_runner,
        worktree_manager=mock_worktree_manager,
        org_id=org_id,
        project_id=project_id,
    )


# =============================================================================
# QualityGateResult Tests
# =============================================================================


class TestQualityGateResult:
    """Test QualityGateResult data class."""

    def test_create_passing_result(self):
        """Create a passing gate result."""
        result = QualityGateResult(
            gate_type=QualityGateType.LINT,
            passed=True,
            output="All checks passed",
        )
        assert result.passed
        assert result.gate_type == QualityGateType.LINT
        assert result.errors == []

    def test_create_failing_result(self):
        """Create a failing gate result with errors."""
        result = QualityGateResult(
            gate_type=QualityGateType.TEST,
            passed=False,
            output="3 tests failed",
            errors=["test_foo failed", "test_bar failed", "test_baz failed"],
            duration_ms=1500,
        )
        assert not result.passed
        assert len(result.errors) == 3
        assert result.duration_ms == 1500

    def test_to_dict_serialization(self):
        """Gate result serializes to dict."""
        result = QualityGateResult(
            gate_type=QualityGateType.TYPECHECK,
            passed=False,
            output="Type errors found",
            errors=["Missing return type"],
        )
        data = result.to_dict()

        assert data["gate_type"] == "typecheck"
        assert data["passed"] is False
        assert data["errors"] == ["Missing return type"]
        assert "timestamp" in data


# =============================================================================
# TaskOrchestratorService Create Tests
# =============================================================================


class TestTaskOrchestratorCreate:
    """Test orchestrator creation."""

    @pytest.mark.asyncio
    async def test_create_orchestrator(self, service, sample_task):
        """Create new TaskOrchestrator."""
        record = await service.create(sample_task)

        assert record.id.startswith("taskorch_")
        assert record.task_id == sample_task.id
        assert record.project_id == service.project_id
        assert record.status == TaskOrchestratorStatus.INITIALIZING
        assert record.current_phase == TaskOrchestratorPhase.IMPLEMENT
        assert record.rework_count == 0

    @pytest.mark.asyncio
    async def test_create_with_custom_gates(self, service, sample_task):
        """Create orchestrator with custom gate config."""
        gates = [QualityGateType.LINT, QualityGateType.TEST]
        record = await service.create(sample_task, gate_config=gates)

        assert record.gate_config == gates
        assert QualityGateType.AI_REVIEW not in record.gate_config

    @pytest.mark.asyncio
    async def test_create_with_meta_orchestrator(self, service, sample_task):
        """Create orchestrator linked to MetaOrchestrator."""
        meta_id = f"meta_{uuid4().hex[:16]}"
        record = await service.create(sample_task, meta_orchestrator_id=meta_id)

        assert record.meta_orchestrator_id == meta_id

    @pytest.mark.asyncio
    async def test_create_with_custom_max_rework(self, service, sample_task):
        """Create orchestrator with custom Ralph Loop limit."""
        record = await service.create(sample_task, max_rework_attempts=5)

        assert record.max_rework_attempts == 5


# =============================================================================
# Build Loop State Machine Tests
# =============================================================================


class TestBuildLoopStateMachine:
    """Test the implement → review → rework cycle."""

    @pytest.mark.asyncio
    async def test_start_spawns_worker(self, service, sample_task, mock_agent_runner, org_id):
        """Starting orchestrator spawns a worker agent."""
        # Create orchestrator
        record = await service.create(sample_task)

        # Store task in mock entity manager
        service.entity_manager.entities[sample_task.id] = sample_task

        # Setup agent runner to return mock instance
        mock_instance = mock_agent_runner.make_instance(service.org_id, service.project_id)

        async def mock_spawn(**kwargs):
            return mock_instance

        service.agent_runner.spawn = mock_spawn

        # Start
        updated = await service.start(record.id)

        assert updated.status == TaskOrchestratorStatus.IMPLEMENTING
        assert updated.worker_id is not None

    @pytest.mark.asyncio
    async def test_start_requires_initializing_state(self, service, sample_task):
        """Cannot start orchestrator that's not initializing."""
        record = await service.create(sample_task)

        # Manually set status to non-initializing
        record.status = TaskOrchestratorStatus.IMPLEMENTING

        with pytest.raises(TaskOrchestratorError, match="Cannot start"):
            await service.start(record.id)

    @pytest.mark.asyncio
    async def test_worker_complete_runs_gates(self, service, sample_task, org_id):
        """Worker completion triggers quality gates."""
        # Create orchestrator and set to implementing
        record = await service.create(sample_task)
        record.status = TaskOrchestratorStatus.IMPLEMENTING
        record.worker_id = "worker-123"
        record.gate_config = [QualityGateType.LINT, QualityGateType.TEST]

        # Store task
        service.entity_manager.entities[sample_task.id] = sample_task

        # Mock gates to all pass
        with patch.object(service, "_run_quality_gates") as mock_gates:
            mock_gates.return_value = [
                QualityGateResult(QualityGateType.LINT, True),
                QualityGateResult(QualityGateType.TEST, True),
            ]

            updated = await service.on_worker_complete(record.id)

        assert updated.status == TaskOrchestratorStatus.COMPLETE
        mock_gates.assert_called_once()

    @pytest.mark.asyncio
    async def test_gate_failure_triggers_rework(self, service, sample_task, mock_agent_runner):
        """Failed gate triggers rework cycle."""
        record = await service.create(sample_task)
        record.status = TaskOrchestratorStatus.IMPLEMENTING
        record.worker_id = "worker-123"

        # Mock agent instance for feedback
        mock_instance = mock_agent_runner.make_instance(service.org_id, service.project_id)

        async def mock_get_agent(agent_id):
            return mock_instance

        service.agent_runner.get_agent = mock_get_agent

        # Mock gates with failure
        with patch.object(service, "_run_quality_gates") as mock_gates:
            mock_gates.return_value = [
                QualityGateResult(QualityGateType.LINT, True),
                QualityGateResult(QualityGateType.TEST, False, errors=["test_foo failed"]),
            ]

            updated = await service.on_worker_complete(record.id)

        assert updated.status == TaskOrchestratorStatus.REWORKING
        assert updated.rework_count == 1


# =============================================================================
# Ralph Loop Safety Tests
# =============================================================================


class TestRalphLoopSafety:
    """Test Ralph Loop safety controls - max iterations before human escalation."""

    @pytest.mark.asyncio
    async def test_max_rework_triggers_failure(self, service, sample_task, mock_agent_runner):
        """Exceeding max rework attempts triggers failure and escalation."""
        record = await service.create(sample_task, max_rework_attempts=3)
        record.status = TaskOrchestratorStatus.REWORKING
        record.rework_count = 2  # At limit
        record.worker_id = "worker-123"

        # Mock failing gates
        with patch.object(service, "_run_quality_gates") as mock_gates:
            mock_gates.return_value = [
                QualityGateResult(QualityGateType.TEST, False, errors=["Still failing"]),
            ]

            updated = await service.on_worker_complete(record.id)

        assert updated.status == TaskOrchestratorStatus.FAILED

    @pytest.mark.asyncio
    async def test_rework_count_increments(self, service, sample_task, mock_agent_runner):
        """Each rework cycle increments the counter."""
        record = await service.create(sample_task)
        record.status = TaskOrchestratorStatus.IMPLEMENTING
        record.worker_id = "worker-123"
        record.rework_count = 0

        # Mock agent for feedback
        mock_instance = mock_agent_runner.make_instance(service.org_id, service.project_id)

        async def mock_get_agent(agent_id):
            return mock_instance

        service.agent_runner.get_agent = mock_get_agent

        # First failure
        with patch.object(service, "_run_quality_gates") as mock_gates:
            mock_gates.return_value = [
                QualityGateResult(QualityGateType.TEST, False),
            ]
            updated = await service.on_worker_complete(record.id)

        assert updated.rework_count == 1
        assert updated.status == TaskOrchestratorStatus.REWORKING

    @pytest.mark.asyncio
    async def test_successful_after_rework(self, service, sample_task, mock_agent_runner):
        """Task can succeed after rework iterations."""
        record = await service.create(sample_task)
        record.status = TaskOrchestratorStatus.REWORKING
        record.rework_count = 2  # Had some failures
        record.worker_id = "worker-123"
        record.gate_config = [QualityGateType.LINT, QualityGateType.TEST]

        # Store task
        service.entity_manager.entities[sample_task.id] = sample_task

        # Mock passing gates
        with patch.object(service, "_run_quality_gates") as mock_gates:
            mock_gates.return_value = [
                QualityGateResult(QualityGateType.LINT, True),
                QualityGateResult(QualityGateType.TEST, True),
            ]

            updated = await service.on_worker_complete(record.id)

        assert updated.status == TaskOrchestratorStatus.COMPLETE
        assert updated.rework_count == 2  # Count preserved


# =============================================================================
# Human Review Tests
# =============================================================================


class TestHumanReview:
    """Test human-in-the-loop review gate."""

    @pytest.mark.asyncio
    async def test_human_review_gate_creates_approval(
        self, service, sample_task, mock_agent_runner
    ):
        """Human review gate creates approval request."""
        gates = [QualityGateType.LINT, QualityGateType.HUMAN_REVIEW]
        record = await service.create(sample_task, gate_config=gates)
        record.status = TaskOrchestratorStatus.IMPLEMENTING
        record.worker_id = "worker-123"

        # Mock automated gates passing
        with patch.object(service, "_run_quality_gates") as mock_gates:
            mock_gates.return_value = [
                QualityGateResult(QualityGateType.LINT, True),
            ]

            updated = await service.on_worker_complete(record.id)

        assert updated.status == TaskOrchestratorStatus.HUMAN_REVIEW
        assert updated.pending_approval_id is not None

    @pytest.mark.asyncio
    async def test_human_approval_completes(self, service, sample_task):
        """Human approval completes the orchestrator."""
        record = await service.create(sample_task)
        record.status = TaskOrchestratorStatus.HUMAN_REVIEW
        record.pending_approval_id = "approval-123"
        record.gate_config = []  # No more gates

        # Store task
        service.entity_manager.entities[sample_task.id] = sample_task

        updated = await service.on_human_approval(record.id, approved=True)

        assert updated.status == TaskOrchestratorStatus.COMPLETE

    @pytest.mark.asyncio
    async def test_human_rejection_triggers_rework(
        self, service, sample_task, mock_agent_runner
    ):
        """Human rejection triggers rework cycle."""
        record = await service.create(sample_task)
        record.status = TaskOrchestratorStatus.HUMAN_REVIEW
        record.pending_approval_id = "approval-123"
        record.worker_id = "worker-123"

        # Mock agent for feedback
        mock_instance = mock_agent_runner.make_instance(service.org_id, service.project_id)

        async def mock_get_agent(agent_id):
            return mock_instance

        service.agent_runner.get_agent = mock_get_agent

        updated = await service.on_human_approval(
            record.id,
            approved=False,
            feedback="Please add more tests",
        )

        assert updated.status == TaskOrchestratorStatus.REWORKING
        assert updated.rework_count == 1


# =============================================================================
# Pause/Resume Tests
# =============================================================================


class TestPauseResume:
    """Test orchestrator pause and resume."""

    @pytest.mark.asyncio
    async def test_pause_orchestrator(self, service, sample_task, mock_agent_runner):
        """Pausing orchestrator pauses worker."""
        record = await service.create(sample_task)
        record.status = TaskOrchestratorStatus.IMPLEMENTING
        record.worker_id = "worker-123"

        # Mock worker
        mock_worker = mock_agent_runner.make_instance(service.org_id, service.project_id)
        pause_called = []

        async def mock_pause(reason):
            pause_called.append(reason)

        mock_worker.pause = mock_pause

        async def mock_get_agent(agent_id):
            return mock_worker

        service.agent_runner.get_agent = mock_get_agent

        result = await service.pause(record.id, reason="user requested")

        assert result is True
        assert "user requested" in pause_called

    @pytest.mark.asyncio
    async def test_resume_orchestrator(self, service, sample_task, mock_agent_runner):
        """Resuming orchestrator resumes worker."""
        record = await service.create(sample_task)
        record.status = TaskOrchestratorStatus.PAUSED
        record.current_phase = TaskOrchestratorPhase.IMPLEMENT
        record.worker_id = "worker-123"

        # Mock worker
        mock_worker = mock_agent_runner.make_instance(service.org_id, service.project_id)
        resume_called = []

        async def mock_resume():
            resume_called.append(True)

        mock_worker.resume = mock_resume

        async def mock_get_agent(agent_id):
            return mock_worker

        service.agent_runner.get_agent = mock_get_agent

        updated = await service.resume(record.id)

        assert updated.status == TaskOrchestratorStatus.IMPLEMENTING
        assert len(resume_called) == 1


# =============================================================================
# Feedback Compilation Tests
# =============================================================================


class TestFeedbackCompilation:
    """Test quality gate feedback formatting."""

    def test_compile_feedback_includes_failures(self, service):
        """Feedback includes failed gate details."""
        results = [
            QualityGateResult(QualityGateType.LINT, True, "All good"),
            QualityGateResult(
                QualityGateType.TEST,
                False,
                "3 failures",
                errors=["test_a", "test_b", "test_c"],
            ),
        ]

        feedback = service._compile_gate_feedback(results)

        assert "PASSED" in feedback
        assert "FAILED" in feedback
        assert "test_a" in feedback
        assert "test_b" in feedback
        assert "test_c" in feedback

    def test_compile_feedback_limits_errors(self, service):
        """Feedback truncates excessive error lists."""
        errors = [f"error_{i}" for i in range(20)]
        results = [
            QualityGateResult(QualityGateType.LINT, False, "Many errors", errors=errors),
        ]

        feedback = service._compile_gate_feedback(results)

        # Should show first 10 and indicate more
        assert "error_0" in feedback
        assert "error_9" in feedback
        assert "10 more" in feedback


# =============================================================================
# List/Get Tests
# =============================================================================


class TestListAndGet:
    """Test listing and retrieving orchestrators."""

    @pytest.mark.asyncio
    async def test_list_for_project(self, service, org_id, project_id):
        """List orchestrators for project."""
        # Create multiple orchestrators
        for i in range(3):
            task = Task(
                id=f"task_{i}_{uuid4().hex[:8]}",
                organization_id=org_id,
                name=f"Task {i}",
                title=f"Task {i}",
                status=TaskStatus.TODO,
                priority=TaskPriority.MEDIUM,
                project_id=project_id,
            )
            await service.create(task)

        results = await service.list_for_project()

        assert len(results) == 3

    @pytest.mark.asyncio
    async def test_list_filtered_by_status(self, service, org_id, project_id):
        """List orchestrators filtered by status."""
        task1 = Task(
            id=f"task_1_{uuid4().hex[:8]}",
            organization_id=org_id,
            name="Task 1",
            title="Task 1",
            status=TaskStatus.TODO,
            priority=TaskPriority.MEDIUM,
            project_id=project_id,
        )
        record1 = await service.create(task1)
        record1.status = TaskOrchestratorStatus.IMPLEMENTING

        task2 = Task(
            id=f"task_2_{uuid4().hex[:8]}",
            organization_id=org_id,
            name="Task 2",
            title="Task 2",
            status=TaskStatus.TODO,
            priority=TaskPriority.MEDIUM,
            project_id=project_id,
        )
        record2 = await service.create(task2)
        record2.status = TaskOrchestratorStatus.COMPLETE

        implementing = await service.list_for_project(status=TaskOrchestratorStatus.IMPLEMENTING)
        complete = await service.list_for_project(status=TaskOrchestratorStatus.COMPLETE)

        assert len(implementing) == 1
        assert len(complete) == 1

    @pytest.mark.asyncio
    async def test_get_nonexistent_returns_none(self, service):
        """Getting nonexistent orchestrator returns None."""
        result = await service.get("nonexistent-id")
        assert result is None
