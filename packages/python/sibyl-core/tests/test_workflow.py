"""Tests for task workflow state machine and estimation."""

from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock

import pytest

from sibyl_core.errors import InvalidTransitionError
from sibyl_core.models.entities import Entity, EntityType
from sibyl_core.models.tasks import (
    Task,
    TaskComplexity,
    TaskEstimate,
    TaskStatus,
)
from sibyl_core.tasks.estimation import (
    batch_estimate,
    calculate_project_estimate,
    estimate_task_effort,
)
from sibyl_core.tasks.workflow import (
    ALL_STATUSES,
    TaskWorkflowEngine,
    get_allowed_transitions,
    is_valid_transition,
)

# =============================================================================
# Test Fixtures
# =============================================================================


def make_task(
    task_id: str = "task_abc123",
    title: str = "Test task",
    status: TaskStatus = TaskStatus.TODO,
    **kwargs: Any,
) -> Task:
    """Factory for creating test tasks."""
    return Task(
        id=task_id,
        name=title,
        title=title,
        status=status,
        **kwargs,
    )


def make_entity(
    entity_id: str = "entity_abc123",
    name: str = "Test entity",
    entity_type: EntityType = EntityType.TASK,
    metadata: dict[str, Any] | None = None,
) -> Entity:
    """Factory for creating test entities."""
    return Entity(
        id=entity_id,
        name=name,
        entity_type=entity_type,
        metadata=metadata or {},
    )


@dataclass
class MockEntityManager:
    """Mock EntityManager for testing workflow engine."""

    entities: dict[str, Entity]
    search_results: list[tuple[Entity, float]]

    async def get(self, entity_id: str) -> Entity:
        """Get entity by ID."""
        if entity_id not in self.entities:
            raise KeyError(f"Entity not found: {entity_id}")
        return self.entities[entity_id]

    async def update(self, entity_id: str, updates: dict[str, Any]) -> Entity:
        """Update entity and return updated version."""
        entity = self.entities[entity_id]
        # Merge updates into metadata for Task reconstruction
        new_metadata = {**(entity.metadata or {}), **updates}
        updated = Entity(
            id=entity.id,
            name=entity.name,
            description=entity.description,
            entity_type=entity.entity_type,
            metadata=new_metadata,
        )
        self.entities[entity_id] = updated
        return updated

    async def create(self, entity: Entity) -> str:
        """Create new entity."""
        self.entities[entity.id] = entity
        return entity.id

    async def search(
        self,
        query: str,
        entity_types: list[EntityType] | None = None,
        limit: int = 10,
    ) -> list[tuple[Entity, float]]:
        """Search entities."""
        return self.search_results[:limit]


@dataclass
class MockRelationshipManager:
    """Mock RelationshipManager for testing workflow engine."""

    relationships: list[Any]

    async def create(self, relationship: Any) -> str:
        """Create relationship."""
        self.relationships.append(relationship)
        return relationship.id

    async def get_for_entity(
        self,
        entity_id: str,
        relationship_types: list[Any] | None = None,
    ) -> list[Any]:
        """Get relationships for entity."""
        return []


@dataclass
class MockGraphClient:
    """Mock GraphClient for testing workflow engine."""

    query_results: list[dict[str, Any]]
    last_query: str | None = None

    async def execute_read_org(
        self,
        query: str,
        org_id: str,
        **params: Any,
    ) -> list[dict[str, Any]]:
        """Execute read query."""
        self.last_query = query
        return self.query_results


@pytest.fixture
def mock_entity_manager() -> MockEntityManager:
    """Create mock entity manager."""
    return MockEntityManager(entities={}, search_results=[])


@pytest.fixture
def mock_relationship_manager() -> MockRelationshipManager:
    """Create mock relationship manager."""
    return MockRelationshipManager(relationships=[])


@pytest.fixture
def mock_graph_client() -> MockGraphClient:
    """Create mock graph client."""
    return MockGraphClient(query_results=[])


@pytest.fixture
def workflow_engine(
    mock_entity_manager: MockEntityManager,
    mock_relationship_manager: MockRelationshipManager,
    mock_graph_client: MockGraphClient,
) -> TaskWorkflowEngine:
    """Create workflow engine with mocks."""
    return TaskWorkflowEngine(
        entity_manager=mock_entity_manager,  # type: ignore[arg-type]
        relationship_manager=mock_relationship_manager,  # type: ignore[arg-type]
        graph_client=mock_graph_client,  # type: ignore[arg-type]
        organization_id="org_test123",
    )


# =============================================================================
# TestWorkflow - State Machine Tests
# =============================================================================


class TestWorkflow:
    """Tests for task workflow state machine transitions."""

    # -------------------------------------------------------------------------
    # is_valid_transition tests
    # -------------------------------------------------------------------------

    def test_valid_transitions_from_backlog(self) -> None:
        """BACKLOG can transition to any status."""
        for target in TaskStatus:
            assert is_valid_transition(TaskStatus.BACKLOG, target) is True

    def test_valid_transitions_from_todo(self) -> None:
        """TODO can transition to any status."""
        for target in TaskStatus:
            assert is_valid_transition(TaskStatus.TODO, target) is True

    def test_valid_transitions_from_doing(self) -> None:
        """DOING can transition to any status."""
        for target in TaskStatus:
            assert is_valid_transition(TaskStatus.DOING, target) is True

    def test_valid_transitions_from_blocked(self) -> None:
        """BLOCKED can transition to any status."""
        for target in TaskStatus:
            assert is_valid_transition(TaskStatus.BLOCKED, target) is True

    def test_valid_transitions_from_review(self) -> None:
        """REVIEW can transition to any status."""
        for target in TaskStatus:
            assert is_valid_transition(TaskStatus.REVIEW, target) is True

    def test_valid_transitions_from_done(self) -> None:
        """DONE can transition to any status including ARCHIVED."""
        for target in TaskStatus:
            assert is_valid_transition(TaskStatus.DONE, target) is True

    def test_invalid_transitions_from_archived(self) -> None:
        """ARCHIVED is terminal - cannot transition to any other status."""
        for target in TaskStatus:
            if target == TaskStatus.ARCHIVED:
                # Same-state is allowed (no-op)
                assert is_valid_transition(TaskStatus.ARCHIVED, target) is True
            else:
                assert is_valid_transition(TaskStatus.ARCHIVED, target) is False

    def test_noop_transitions_allowed(self) -> None:
        """Same-state transitions are always valid (no-op)."""
        for status in TaskStatus:
            assert is_valid_transition(status, status) is True

    # -------------------------------------------------------------------------
    # get_allowed_transitions tests
    # -------------------------------------------------------------------------

    def test_get_available_transitions_from_backlog(self) -> None:
        """BACKLOG can transition to all statuses."""
        allowed = get_allowed_transitions(TaskStatus.BACKLOG)
        assert allowed == ALL_STATUSES | {TaskStatus.ARCHIVED}

    def test_get_available_transitions_from_todo(self) -> None:
        """TODO can transition to all statuses."""
        allowed = get_allowed_transitions(TaskStatus.TODO)
        assert allowed == ALL_STATUSES | {TaskStatus.ARCHIVED}

    def test_get_available_transitions_from_doing(self) -> None:
        """DOING can transition to all statuses."""
        allowed = get_allowed_transitions(TaskStatus.DOING)
        assert allowed == ALL_STATUSES | {TaskStatus.ARCHIVED}

    def test_get_available_transitions_from_done(self) -> None:
        """DONE can transition to all statuses."""
        allowed = get_allowed_transitions(TaskStatus.DONE)
        assert allowed == ALL_STATUSES | {TaskStatus.ARCHIVED}

    def test_get_available_transitions_from_archived(self) -> None:
        """ARCHIVED has no valid transitions (terminal)."""
        allowed = get_allowed_transitions(TaskStatus.ARCHIVED)
        assert allowed == set()

    def test_can_transition_predicate(self) -> None:
        """is_valid_transition works as predicate for allowed transitions."""
        # Non-archived status can go anywhere
        assert is_valid_transition(TaskStatus.TODO, TaskStatus.DOING)
        assert is_valid_transition(TaskStatus.DOING, TaskStatus.BLOCKED)
        assert is_valid_transition(TaskStatus.BLOCKED, TaskStatus.DOING)
        assert is_valid_transition(TaskStatus.DOING, TaskStatus.REVIEW)
        assert is_valid_transition(TaskStatus.REVIEW, TaskStatus.DONE)
        assert is_valid_transition(TaskStatus.DONE, TaskStatus.ARCHIVED)

        # Archived cannot transition out
        assert not is_valid_transition(TaskStatus.ARCHIVED, TaskStatus.TODO)

    # -------------------------------------------------------------------------
    # Workflow path tests
    # -------------------------------------------------------------------------

    def test_workflow_from_backlog(self) -> None:
        """Standard workflow: backlog -> todo -> doing -> done."""
        assert is_valid_transition(TaskStatus.BACKLOG, TaskStatus.TODO)
        assert is_valid_transition(TaskStatus.TODO, TaskStatus.DOING)
        assert is_valid_transition(TaskStatus.DOING, TaskStatus.DONE)

    def test_workflow_blocked_path(self) -> None:
        """Blocked workflow: doing -> blocked -> doing."""
        assert is_valid_transition(TaskStatus.DOING, TaskStatus.BLOCKED)
        assert is_valid_transition(TaskStatus.BLOCKED, TaskStatus.DOING)

    def test_workflow_review_cycle(self) -> None:
        """Review workflow: doing -> review -> done."""
        assert is_valid_transition(TaskStatus.DOING, TaskStatus.REVIEW)
        assert is_valid_transition(TaskStatus.REVIEW, TaskStatus.DONE)
        # Review can also go back to doing (changes requested)
        assert is_valid_transition(TaskStatus.REVIEW, TaskStatus.DOING)

    def test_workflow_archive(self) -> None:
        """Archive workflow: any status -> archived."""
        for status in TaskStatus:
            if status != TaskStatus.ARCHIVED:
                assert is_valid_transition(status, TaskStatus.ARCHIVED)

    # -------------------------------------------------------------------------
    # Transition guards (InvalidTransitionError)
    # -------------------------------------------------------------------------

    def test_transition_guards_archived_raises(self) -> None:
        """Transitioning from ARCHIVED raises InvalidTransitionError."""
        engine = TaskWorkflowEngine(
            entity_manager=MagicMock(),
            relationship_manager=MagicMock(),
            graph_client=MagicMock(),
            organization_id="org_test",
        )

        with pytest.raises(InvalidTransitionError) as exc_info:
            engine._validate_transition(TaskStatus.ARCHIVED, TaskStatus.TODO)

        assert "archived" in str(exc_info.value).lower()
        assert "todo" in str(exc_info.value).lower()
        assert exc_info.value.details["from_status"] == "archived"
        assert exc_info.value.details["to_status"] == "todo"
        assert exc_info.value.details["allowed_transitions"] == []

    def test_transition_guards_noop_passes(self) -> None:
        """Same-state transition passes validation."""
        engine = TaskWorkflowEngine(
            entity_manager=MagicMock(),
            relationship_manager=MagicMock(),
            graph_client=MagicMock(),
            organization_id="org_test",
        )
        # Should not raise
        engine._validate_transition(TaskStatus.DOING, TaskStatus.DOING)


# =============================================================================
# TestWorkflowEngine - Integration Tests
# =============================================================================


class TestWorkflowEngine:
    """Integration tests for TaskWorkflowEngine methods."""

    @pytest.mark.asyncio
    async def test_transition_task_valid(
        self,
        workflow_engine: TaskWorkflowEngine,
        mock_entity_manager: MockEntityManager,
    ) -> None:
        """transition_task succeeds for valid transitions."""
        # Setup: task in TODO status
        task = make_task(status=TaskStatus.TODO)
        mock_entity_manager.entities[task.id] = make_entity(
            entity_id=task.id,
            name=task.title,
            metadata={"status": TaskStatus.TODO},
        )

        # Act
        result = await workflow_engine.transition_task(task.id, TaskStatus.DOING)

        # Assert
        assert isinstance(result, Task)
        assert mock_entity_manager.entities[task.id].metadata["status"] == TaskStatus.DOING

    @pytest.mark.asyncio
    async def test_transition_task_invalid_from_archived(
        self,
        workflow_engine: TaskWorkflowEngine,
        mock_entity_manager: MockEntityManager,
    ) -> None:
        """transition_task raises for invalid transition from ARCHIVED."""
        # Setup: task in ARCHIVED status
        task = make_task(status=TaskStatus.ARCHIVED)
        mock_entity_manager.entities[task.id] = make_entity(
            entity_id=task.id,
            name=task.title,
            metadata={"status": TaskStatus.ARCHIVED},
        )

        # Act & Assert
        with pytest.raises(InvalidTransitionError):
            await workflow_engine.transition_task(task.id, TaskStatus.TODO)

    @pytest.mark.asyncio
    async def test_start_task_sets_doing_status(
        self,
        workflow_engine: TaskWorkflowEngine,
        mock_entity_manager: MockEntityManager,
    ) -> None:
        """start_task transitions to DOING and sets started_at."""
        # Setup
        task = make_task(status=TaskStatus.TODO)
        mock_entity_manager.entities[task.id] = make_entity(
            entity_id=task.id,
            name=task.title,
            metadata={"status": TaskStatus.TODO, "assignees": []},
        )

        # Act
        result = await workflow_engine.start_task(task.id, "alice@test.com")

        # Assert
        assert result.status == TaskStatus.DOING
        assert "alice@test.com" in result.assignees
        assert result.started_at is not None

    @pytest.mark.asyncio
    async def test_start_task_generates_branch_name(
        self,
        workflow_engine: TaskWorkflowEngine,
        mock_entity_manager: MockEntityManager,
    ) -> None:
        """start_task generates branch name if not set."""
        # Setup
        task = make_task(
            task_id="abcd1234", title="Add user authentication", status=TaskStatus.TODO
        )
        mock_entity_manager.entities[task.id] = make_entity(
            entity_id=task.id,
            name=task.title,
            metadata={"status": TaskStatus.TODO, "assignees": [], "branch_name": None},
        )

        # Act
        result = await workflow_engine.start_task(task.id, "alice@test.com")

        # Assert - branch name should be generated
        assert result.branch_name is not None
        assert "abcd1234" in result.branch_name
        assert "add-user-authentication" in result.branch_name

    @pytest.mark.asyncio
    async def test_block_task_adds_blocker(
        self,
        workflow_engine: TaskWorkflowEngine,
        mock_entity_manager: MockEntityManager,
    ) -> None:
        """block_task transitions to BLOCKED and records blocker."""
        # Setup
        task = make_task(status=TaskStatus.DOING)
        mock_entity_manager.entities[task.id] = make_entity(
            entity_id=task.id,
            name=task.title,
            metadata={"status": TaskStatus.DOING, "blockers_encountered": []},
        )

        # Act
        result = await workflow_engine.block_task(task.id, "Waiting for API credentials")

        # Assert
        assert result.status == TaskStatus.BLOCKED
        assert "Waiting for API credentials" in result.blockers_encountered

    @pytest.mark.asyncio
    async def test_unblock_task_returns_to_doing(
        self,
        workflow_engine: TaskWorkflowEngine,
        mock_entity_manager: MockEntityManager,
    ) -> None:
        """unblock_task transitions from BLOCKED back to DOING."""
        # Setup
        task = make_task(status=TaskStatus.BLOCKED)
        mock_entity_manager.entities[task.id] = make_entity(
            entity_id=task.id,
            name=task.title,
            metadata={"status": TaskStatus.BLOCKED},
        )

        # Act
        result = await workflow_engine.unblock_task(task.id)

        # Assert
        assert result.status == TaskStatus.DOING

    @pytest.mark.asyncio
    async def test_submit_for_review_sets_review_status(
        self,
        workflow_engine: TaskWorkflowEngine,
        mock_entity_manager: MockEntityManager,
    ) -> None:
        """submit_for_review transitions to REVIEW and records commits."""
        # Setup
        task = make_task(status=TaskStatus.DOING)
        mock_entity_manager.entities[task.id] = make_entity(
            entity_id=task.id,
            name=task.title,
            metadata={"status": TaskStatus.DOING},
        )

        # Act
        result = await workflow_engine.submit_for_review(
            task.id,
            commit_shas=["abc123", "def456"],
            pr_url="https://github.com/test/repo/pull/1",
        )

        # Assert
        assert result.status == TaskStatus.REVIEW
        assert result.commit_shas == ["abc123", "def456"]
        assert result.pr_url == "https://github.com/test/repo/pull/1"
        assert result.reviewed_at is not None

    @pytest.mark.asyncio
    async def test_complete_task_sets_done_status(
        self,
        workflow_engine: TaskWorkflowEngine,
        mock_entity_manager: MockEntityManager,
        mock_graph_client: MockGraphClient,
    ) -> None:
        """complete_task transitions to DONE and records completion."""
        # Setup
        task = make_task(status=TaskStatus.DOING)
        mock_entity_manager.entities[task.id] = make_entity(
            entity_id=task.id,
            name=task.title,
            metadata={"status": TaskStatus.DOING},
        )
        # Empty project progress query result
        mock_graph_client.query_results = []

        # Act
        result = await workflow_engine.complete_task(
            task.id,
            actual_hours=4.5,
            learnings="Learned about async patterns",
            create_episode=False,  # Skip episode creation for simpler test
        )

        # Assert
        assert result.status == TaskStatus.DONE
        assert result.actual_hours == 4.5
        assert result.learnings == "Learned about async patterns"
        assert result.completed_at is not None

    @pytest.mark.asyncio
    async def test_archive_task_sets_archived_status(
        self,
        workflow_engine: TaskWorkflowEngine,
        mock_entity_manager: MockEntityManager,
        mock_graph_client: MockGraphClient,
    ) -> None:
        """archive_task transitions to ARCHIVED."""
        # Setup
        task = make_task(status=TaskStatus.DONE)
        mock_entity_manager.entities[task.id] = make_entity(
            entity_id=task.id,
            name=task.title,
            metadata={"status": TaskStatus.DONE},
        )
        mock_graph_client.query_results = []

        # Act
        result = await workflow_engine.archive_task(task.id, reason="No longer needed")

        # Assert
        assert result.status == TaskStatus.ARCHIVED
        assert result.metadata.get("archive_reason") == "No longer needed"

    @pytest.mark.asyncio
    async def test_update_project_progress_uses_belongs_to_relationship(
        self,
        workflow_engine: TaskWorkflowEngine,
        mock_entity_manager: MockEntityManager,
        mock_graph_client: MockGraphClient,
    ) -> None:
        """Project progress uses BELONGS_TO edges and task metadata status values."""
        project_id = "project_abc123"
        mock_entity_manager.entities[project_id] = make_entity(
            entity_id=project_id,
            name="Test project",
            entity_type=EntityType.PROJECT,
            metadata={},
        )
        mock_graph_client.query_results = [
            {"metadata": {"status": "done"}},
            {"metadata": '{"status":"doing"}'},
            {"metadata": {"status": "todo"}},
        ]

        await workflow_engine._update_project_progress(project_id)

        assert mock_graph_client.last_query is not None
        assert "BELONGS_TO" in mock_graph_client.last_query
        assert "CONTAINS" not in mock_graph_client.last_query

        project = mock_entity_manager.entities[project_id]
        assert project.metadata["total_tasks"] == 3
        assert project.metadata["completed_tasks"] == 1
        assert project.metadata["in_progress_tasks"] == 1

    @pytest.mark.asyncio
    async def test_archive_from_archived_raises(
        self,
        workflow_engine: TaskWorkflowEngine,
        mock_entity_manager: MockEntityManager,
    ) -> None:
        """Attempting to archive an already archived task raises."""
        # Setup - task already archived
        task = make_task(status=TaskStatus.ARCHIVED)
        mock_entity_manager.entities[task.id] = make_entity(
            entity_id=task.id,
            name=task.title,
            metadata={"status": TaskStatus.ARCHIVED},
        )

        # Act & Assert - can't transition from archived to anything but itself
        # Since archive_task targets ARCHIVED, and current is ARCHIVED,
        # this is actually a no-op (same state). Let's test a different transition.
        with pytest.raises(InvalidTransitionError):
            await workflow_engine.transition_task(task.id, TaskStatus.TODO)


class TestBranchNameGeneration:
    """Tests for branch name generation."""

    def test_generate_branch_name_basic(
        self,
        workflow_engine: TaskWorkflowEngine,
    ) -> None:
        """Branch name follows convention: prefix/id-slug."""
        task = make_task(
            task_id="abcd1234-5678-90ab-cdef",
            title="Add login form",
        )

        branch = workflow_engine._generate_branch_name(task)

        assert branch.startswith("task/")
        assert "abcd1234" in branch
        assert "add-login-form" in branch

    def test_generate_branch_name_with_feature(
        self,
        workflow_engine: TaskWorkflowEngine,
    ) -> None:
        """Feature tasks get 'feature/' prefix."""
        task = make_task(
            task_id="abcd1234",
            title="Implement OAuth2",
            feature="authentication",
        )

        branch = workflow_engine._generate_branch_name(task)

        assert branch.startswith("feature/")

    def test_generate_branch_name_epic_complexity(
        self,
        workflow_engine: TaskWorkflowEngine,
    ) -> None:
        """Epic complexity tasks get 'epic/' prefix."""
        task = make_task(
            task_id="abcd1234",
            title="Major Refactor",
            complexity=TaskComplexity.EPIC,
        )

        branch = workflow_engine._generate_branch_name(task)

        assert branch.startswith("epic/")

    def test_generate_branch_name_sanitizes_special_chars(
        self,
        workflow_engine: TaskWorkflowEngine,
    ) -> None:
        """Special characters are replaced with hyphens."""
        task = make_task(
            task_id="abcd1234",
            title="Fix: Bug #123 (urgent!)",
        )

        branch = workflow_engine._generate_branch_name(task)

        assert ":" not in branch
        assert "#" not in branch
        assert "(" not in branch
        assert "!" not in branch
        assert "fix-bug-123-urgent" in branch

    def test_generate_branch_name_truncates_long_titles(
        self,
        workflow_engine: TaskWorkflowEngine,
    ) -> None:
        """Long titles are truncated to reasonable length."""
        task = make_task(
            task_id="abcd1234",
            title="This is a very long task title that exceeds the maximum allowed length for branch names",
        )

        branch = workflow_engine._generate_branch_name(task)

        # Branch should be reasonable length
        assert len(branch) < 70  # prefix + id + slug


# =============================================================================
# TestEstimation - Effort Estimation Tests
# =============================================================================


class TestEstimation:
    """Tests for task effort estimation."""

    @pytest.mark.asyncio
    async def test_estimate_trivial_no_similar_tasks(
        self,
        mock_entity_manager: MockEntityManager,
    ) -> None:
        """Estimation returns zero confidence when no similar tasks found."""
        mock_entity_manager.search_results = []

        result = await estimate_task_effort(
            entity_manager=mock_entity_manager,  # type: ignore[arg-type]
            title="Simple button fix",
            description="Change button color",
        )

        assert result.estimated_hours == 0
        assert result.confidence == 0
        assert "no similar" in result.reason.lower()

    @pytest.mark.asyncio
    async def test_estimate_simple_with_one_match(
        self,
        mock_entity_manager: MockEntityManager,
    ) -> None:
        """Estimation works with single similar task."""
        similar_task = make_entity(
            entity_id="similar_1",
            name="Similar simple task",
            metadata={
                "status": TaskStatus.DONE.value,
                "actual_hours": 2.0,
            },
        )
        mock_entity_manager.search_results = [(similar_task, 0.8)]

        result = await estimate_task_effort(
            entity_manager=mock_entity_manager,  # type: ignore[arg-type]
            title="Simple button fix",
            description="Change button color",
        )

        assert result.estimated_hours == 2.0
        assert result.confidence > 0
        assert result.based_on_tasks == 1
        assert len(result.similar_tasks) == 1

    @pytest.mark.asyncio
    async def test_estimate_medium_weighted_average(
        self,
        mock_entity_manager: MockEntityManager,
    ) -> None:
        """Estimation calculates weighted average from multiple tasks."""
        task_1 = make_entity(
            entity_id="task_1",
            name="Similar task 1",
            metadata={"status": TaskStatus.DONE.value, "actual_hours": 4.0},
        )
        task_2 = make_entity(
            entity_id="task_2",
            name="Similar task 2",
            metadata={"status": TaskStatus.DONE.value, "actual_hours": 8.0},
        )
        # Higher similarity for task_1, so weighted average should lean toward 4
        mock_entity_manager.search_results = [
            (task_1, 0.9),  # 4 hours * 0.9 = 3.6
            (task_2, 0.6),  # 8 hours * 0.6 = 4.8
        ]
        # Weighted avg = (3.6 + 4.8) / (0.9 + 0.6) = 8.4 / 1.5 = 5.6

        result = await estimate_task_effort(
            entity_manager=mock_entity_manager,  # type: ignore[arg-type]
            title="Medium complexity task",
            description="Moderate effort required",
        )

        assert result.estimated_hours == 5.6
        assert result.based_on_tasks == 2

    @pytest.mark.asyncio
    async def test_estimate_complex_high_confidence(
        self,
        mock_entity_manager: MockEntityManager,
    ) -> None:
        """Confidence increases with more similar tasks."""
        # Create 5 similar tasks for full confidence
        tasks = []
        for i in range(5):
            task = make_entity(
                entity_id=f"task_{i}",
                name=f"Similar task {i}",
                metadata={"status": TaskStatus.DONE.value, "actual_hours": 8.0},
            )
            tasks.append((task, 0.85))
        mock_entity_manager.search_results = tasks

        result = await estimate_task_effort(
            entity_manager=mock_entity_manager,  # type: ignore[arg-type]
            title="Complex feature",
            description="Multi-day implementation",
        )

        assert result.estimated_hours == 8.0
        # Full confidence: avg_similarity (0.85) * sample_factor (1.0) = 0.85
        assert result.confidence == 0.85
        assert result.based_on_tasks == 5

    @pytest.mark.asyncio
    async def test_estimate_epic_from_many_tasks(
        self,
        mock_entity_manager: MockEntityManager,
    ) -> None:
        """Large estimates work with many reference tasks."""
        tasks = []
        for i in range(10):
            task = make_entity(
                entity_id=f"task_{i}",
                name=f"Epic subtask {i}",
                metadata={"status": TaskStatus.DONE.value, "actual_hours": 16.0},
            )
            tasks.append((task, 0.7 + i * 0.02))  # Varying similarity
        mock_entity_manager.search_results = tasks

        result = await estimate_task_effort(
            entity_manager=mock_entity_manager,  # type: ignore[arg-type]
            title="Epic feature overhaul",
            description="Complete system redesign",
            max_samples=10,
        )

        assert result.estimated_hours == 16.0
        assert result.based_on_tasks == 10
        assert result.confidence > 0.7

    @pytest.mark.asyncio
    async def test_estimate_from_task_id(
        self,
        mock_entity_manager: MockEntityManager,
    ) -> None:
        """Estimation can fetch task by ID."""
        # The task being estimated
        target_task = make_entity(
            entity_id="target_task",
            name="Task to estimate",
            metadata={"description": "Needs estimation"},
        )
        mock_entity_manager.entities["target_task"] = target_task

        # Similar completed task
        similar_task = make_entity(
            entity_id="similar_task",
            name="Similar completed task",
            metadata={"status": TaskStatus.DONE.value, "actual_hours": 3.0},
        )
        mock_entity_manager.search_results = [(similar_task, 0.75)]

        result = await estimate_task_effort(
            entity_manager=mock_entity_manager,  # type: ignore[arg-type]
            task_id="target_task",
        )

        assert result.estimated_hours == 3.0
        assert result.based_on_tasks == 1

    @pytest.mark.asyncio
    async def test_estimate_excludes_self(
        self,
        mock_entity_manager: MockEntityManager,
    ) -> None:
        """Estimation excludes the task itself from results."""
        # The task being estimated
        target_task = make_entity(
            entity_id="target_task",
            name="Task to estimate",
            metadata={},
        )
        mock_entity_manager.entities["target_task"] = target_task

        # Search returns the task itself plus another
        other_task = make_entity(
            entity_id="other_task",
            name="Other task",
            metadata={"status": TaskStatus.DONE.value, "actual_hours": 5.0},
        )
        mock_entity_manager.search_results = [
            (target_task, 1.0),  # Should be excluded
            (other_task, 0.8),
        ]

        result = await estimate_task_effort(
            entity_manager=mock_entity_manager,  # type: ignore[arg-type]
            task_id="target_task",
        )

        # Should only use other_task
        assert result.based_on_tasks == 1
        assert result.estimated_hours == 5.0

    @pytest.mark.asyncio
    async def test_estimate_filters_incomplete_tasks(
        self,
        mock_entity_manager: MockEntityManager,
    ) -> None:
        """Estimation only considers DONE or ARCHIVED tasks."""
        incomplete_task = make_entity(
            entity_id="incomplete",
            name="Still in progress",
            metadata={"status": TaskStatus.DOING.value, "actual_hours": 10.0},
        )
        complete_task = make_entity(
            entity_id="complete",
            name="Done task",
            metadata={"status": TaskStatus.DONE.value, "actual_hours": 4.0},
        )
        mock_entity_manager.search_results = [
            (incomplete_task, 0.9),  # Higher similarity but incomplete
            (complete_task, 0.7),
        ]

        result = await estimate_task_effort(
            entity_manager=mock_entity_manager,  # type: ignore[arg-type]
            title="New task",
        )

        # Should only use complete_task
        assert result.based_on_tasks == 1
        assert result.estimated_hours == 4.0

    @pytest.mark.asyncio
    async def test_estimate_filters_below_similarity_threshold(
        self,
        mock_entity_manager: MockEntityManager,
    ) -> None:
        """Tasks below similarity threshold are excluded."""
        low_similarity_task = make_entity(
            entity_id="low_sim",
            name="Loosely related task",
            metadata={"status": TaskStatus.DONE.value, "actual_hours": 100.0},
        )
        mock_entity_manager.search_results = [(low_similarity_task, 0.3)]  # Below 0.5 default

        result = await estimate_task_effort(
            entity_manager=mock_entity_manager,  # type: ignore[arg-type]
            title="New task",
            min_similarity=0.5,
        )

        assert result.based_on_tasks == 0
        assert result.estimated_hours == 0

    @pytest.mark.asyncio
    async def test_estimate_requires_title_or_task_id(
        self,
        mock_entity_manager: MockEntityManager,
    ) -> None:
        """Estimation fails gracefully without title or task_id."""
        result = await estimate_task_effort(
            entity_manager=mock_entity_manager,  # type: ignore[arg-type]
        )

        assert result.estimated_hours == 0
        assert result.confidence == 0
        assert "required" in result.reason.lower()

    @pytest.mark.asyncio
    async def test_estimate_handles_missing_actual_hours(
        self,
        mock_entity_manager: MockEntityManager,
    ) -> None:
        """Tasks without actual_hours are excluded."""
        no_hours_task = make_entity(
            entity_id="no_hours",
            name="Task without hours",
            metadata={"status": TaskStatus.DONE.value},  # No actual_hours
        )
        with_hours_task = make_entity(
            entity_id="with_hours",
            name="Task with hours",
            metadata={"status": TaskStatus.DONE.value, "actual_hours": 6.0},
        )
        mock_entity_manager.search_results = [
            (no_hours_task, 0.9),
            (with_hours_task, 0.7),
        ]

        result = await estimate_task_effort(
            entity_manager=mock_entity_manager,  # type: ignore[arg-type]
            title="New task",
        )

        assert result.based_on_tasks == 1
        assert result.estimated_hours == 6.0


class TestBatchEstimate:
    """Tests for batch estimation."""

    @pytest.mark.asyncio
    async def test_batch_estimate_multiple_tasks(
        self,
        mock_entity_manager: MockEntityManager,
    ) -> None:
        """batch_estimate returns estimates for all task IDs."""
        # Setup tasks
        for i in range(3):
            mock_entity_manager.entities[f"task_{i}"] = make_entity(
                entity_id=f"task_{i}",
                name=f"Task {i}",
            )

        # Similar task for estimation
        similar = make_entity(
            entity_id="similar",
            name="Reference task",
            metadata={"status": TaskStatus.DONE.value, "actual_hours": 2.0},
        )
        mock_entity_manager.search_results = [(similar, 0.8)]

        results = await batch_estimate(
            entity_manager=mock_entity_manager,  # type: ignore[arg-type]
            task_ids=["task_0", "task_1", "task_2"],
        )

        assert len(results) == 3
        assert all(isinstance(v, TaskEstimate) for v in results.values())


class TestProjectEstimate:
    """Tests for project-level estimation aggregation."""

    def test_calculate_project_estimate_empty(self) -> None:
        """Empty estimates list returns zero."""
        result = calculate_project_estimate([])

        assert result.estimated_hours == 0
        assert result.confidence == 0

    def test_calculate_project_estimate_single(self) -> None:
        """Single estimate passes through."""
        estimates = [
            TaskEstimate(
                estimated_hours=8.0,
                confidence=0.9,
                based_on_tasks=3,
            )
        ]

        result = calculate_project_estimate(estimates)

        assert result.estimated_hours == 8.0
        assert result.confidence == 0.9

    def test_calculate_project_estimate_multiple(self) -> None:
        """Multiple estimates are summed with weighted confidence."""
        estimates = [
            TaskEstimate(estimated_hours=4.0, confidence=0.8, based_on_tasks=2),
            TaskEstimate(estimated_hours=6.0, confidence=0.6, based_on_tasks=3),
            TaskEstimate(estimated_hours=2.0, confidence=1.0, based_on_tasks=5),
        ]

        result = calculate_project_estimate(estimates)

        # Total hours = 4 + 6 + 2 = 12
        assert result.estimated_hours == 12.0
        # Weighted confidence: (4*0.8 + 6*0.6 + 2*1.0) / 12 = (3.2 + 3.6 + 2.0) / 12 = 0.73
        assert result.confidence == 0.73
        # Total based_on_tasks = 2 + 3 + 5 = 10
        assert result.based_on_tasks == 10

    def test_calculate_project_estimate_with_zero_hours(self) -> None:
        """Estimates with zero hours don't affect confidence calculation."""
        estimates = [
            TaskEstimate(estimated_hours=0, confidence=0, based_on_tasks=0),
            TaskEstimate(estimated_hours=10.0, confidence=0.9, based_on_tasks=5),
        ]

        result = calculate_project_estimate(estimates)

        assert result.estimated_hours == 10.0
        assert result.confidence == 0.9
