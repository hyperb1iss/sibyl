"""Tests for the task workflow engine and state machine."""

import pytest

from sibyl.errors import InvalidTransitionError
from sibyl.models.tasks import Task, TaskComplexity, TaskStatus
from sibyl.tasks.workflow import (
    ALL_STATUSES,
    get_allowed_transitions,
    is_valid_transition,
)


class TestStateMachineDefinition:
    """Tests for the state machine constants and structure.

    The workflow uses a permissive state machine - any transition is allowed
    EXCEPT from ARCHIVED (which is terminal).
    """

    def test_archived_is_terminal_state(self) -> None:
        """Verify ARCHIVED has no outgoing transitions."""
        assert get_allowed_transitions(TaskStatus.ARCHIVED) == set()

    def test_all_active_states_can_transition_anywhere(self) -> None:
        """Verify non-archived states can transition to any status."""
        for status in TaskStatus:
            if status != TaskStatus.ARCHIVED:
                allowed = get_allowed_transitions(status)
                # All statuses plus archived
                assert allowed == ALL_STATUSES | {TaskStatus.ARCHIVED}

    def test_all_active_states_can_archive(self) -> None:
        """Verify all non-terminal states can transition to archived."""
        non_terminal = [s for s in TaskStatus if s != TaskStatus.ARCHIVED]
        for status in non_terminal:
            allowed = get_allowed_transitions(status)
            assert TaskStatus.ARCHIVED in allowed, f"{status} cannot archive"


class TestIsValidTransition:
    """Tests for is_valid_transition function."""

    def test_same_status_is_valid(self) -> None:
        """Verify transitioning to same status is valid (no-op)."""
        for status in TaskStatus:
            assert is_valid_transition(status, status)

    def test_backlog_to_todo_valid(self) -> None:
        """Test backlog -> todo is valid."""
        assert is_valid_transition(TaskStatus.BACKLOG, TaskStatus.TODO)

    def test_todo_to_doing_valid(self) -> None:
        """Test todo -> doing is valid."""
        assert is_valid_transition(TaskStatus.TODO, TaskStatus.DOING)

    def test_doing_to_blocked_valid(self) -> None:
        """Test doing -> blocked is valid."""
        assert is_valid_transition(TaskStatus.DOING, TaskStatus.BLOCKED)

    def test_blocked_to_doing_valid(self) -> None:
        """Test blocked -> doing is valid."""
        assert is_valid_transition(TaskStatus.BLOCKED, TaskStatus.DOING)

    def test_doing_to_review_valid(self) -> None:
        """Test doing -> review is valid."""
        assert is_valid_transition(TaskStatus.DOING, TaskStatus.REVIEW)

    def test_review_to_done_valid(self) -> None:
        """Test review -> done is valid."""
        assert is_valid_transition(TaskStatus.REVIEW, TaskStatus.DONE)

    def test_todo_to_done_valid_permissive(self) -> None:
        """Test todo -> done is valid (permissive state machine allows skipping)."""
        assert is_valid_transition(TaskStatus.TODO, TaskStatus.DONE)

    def test_backlog_to_done_valid_permissive(self) -> None:
        """Test backlog -> done is valid (permissive state machine allows skipping)."""
        assert is_valid_transition(TaskStatus.BACKLOG, TaskStatus.DONE)

    def test_done_to_doing_valid_permissive(self) -> None:
        """Test done -> doing is valid (permissive state machine allows reopening)."""
        assert is_valid_transition(TaskStatus.DONE, TaskStatus.DOING)

    def test_archived_to_anything_invalid(self) -> None:
        """Test archived can't transition to any state."""
        for status in TaskStatus:
            if status != TaskStatus.ARCHIVED:
                assert not is_valid_transition(TaskStatus.ARCHIVED, status)


class TestGetAllowedTransitions:
    """Tests for get_allowed_transitions function."""

    def test_backlog_allowed_transitions(self) -> None:
        """Test allowed transitions from backlog."""
        allowed = get_allowed_transitions(TaskStatus.BACKLOG)
        # Permissive - all statuses
        assert TaskStatus.TODO in allowed
        assert TaskStatus.DOING in allowed
        assert TaskStatus.ARCHIVED in allowed

    def test_todo_allowed_transitions(self) -> None:
        """Test allowed transitions from todo."""
        allowed = get_allowed_transitions(TaskStatus.TODO)
        assert TaskStatus.DOING in allowed
        assert TaskStatus.ARCHIVED in allowed

    def test_doing_allowed_transitions(self) -> None:
        """Test allowed transitions from doing."""
        allowed = get_allowed_transitions(TaskStatus.DOING)
        assert TaskStatus.BLOCKED in allowed
        assert TaskStatus.REVIEW in allowed
        assert TaskStatus.DONE in allowed
        assert TaskStatus.ARCHIVED in allowed

    def test_blocked_allowed_transitions(self) -> None:
        """Test allowed transitions from blocked."""
        allowed = get_allowed_transitions(TaskStatus.BLOCKED)
        assert TaskStatus.DOING in allowed
        assert TaskStatus.ARCHIVED in allowed

    def test_review_allowed_transitions(self) -> None:
        """Test allowed transitions from review."""
        allowed = get_allowed_transitions(TaskStatus.REVIEW)
        assert TaskStatus.DOING in allowed
        assert TaskStatus.DONE in allowed
        assert TaskStatus.ARCHIVED in allowed

    def test_done_allowed_transitions(self) -> None:
        """Test allowed transitions from done."""
        allowed = get_allowed_transitions(TaskStatus.DONE)
        # Permissive - can reopen
        assert TaskStatus.DOING in allowed
        assert TaskStatus.ARCHIVED in allowed

    def test_archived_no_allowed_transitions(self) -> None:
        """Test archived has no allowed transitions."""
        allowed = get_allowed_transitions(TaskStatus.ARCHIVED)
        assert allowed == set()


class TestInvalidTransitionError:
    """Tests for InvalidTransitionError."""

    def test_error_message_format(self) -> None:
        """Test error message includes from/to statuses."""
        error = InvalidTransitionError(
            from_status="todo",
            to_status="done",
            allowed=["doing", "archived"],
        )
        assert "todo" in error.message
        assert "done" in error.message
        assert "doing" in str(error.details["allowed_transitions"])

    def test_error_without_allowed_list(self) -> None:
        """Test error works without allowed list."""
        error = InvalidTransitionError(
            from_status="archived",
            to_status="doing",
        )
        assert "archived" in error.message
        assert "doing" in error.message
        assert error.details["allowed_transitions"] == []


class TestBranchNameGeneration:
    """Tests for branch name generation helper."""

    def test_basic_branch_name(self) -> None:
        """Test basic branch name generation."""
        from sibyl.tasks.workflow import TaskWorkflowEngine

        # Create a minimal task for testing
        task = Task(
            id="abc12345-test-id",
            title="Fix authentication bug",
            description="Auth is broken",
            project_id="proj-001",
            technologies=["python"],
        )

        # Use the static method directly
        engine = TaskWorkflowEngine.__new__(TaskWorkflowEngine)
        branch = engine._generate_branch_name(task)

        assert branch.startswith("task/")
        assert "abc12345" in branch
        assert "fix-authentication-bug" in branch

    def test_feature_prefix_with_feature(self) -> None:
        """Test feature prefix when task has feature."""
        from sibyl.tasks.workflow import TaskWorkflowEngine

        task = Task(
            id="def67890-test-id",
            title="Add OAuth support",
            description="Add OAuth2",
            project_id="proj-001",
            feature="authentication",
            technologies=["python"],
        )

        engine = TaskWorkflowEngine.__new__(TaskWorkflowEngine)
        branch = engine._generate_branch_name(task)

        assert branch.startswith("feature/")

    def test_epic_prefix_for_epic_complexity(self) -> None:
        """Test epic prefix for epic complexity tasks."""
        from sibyl.tasks.workflow import TaskWorkflowEngine

        task = Task(
            id="ghi11111-test-id",
            title="Major refactoring",
            description="Big refactor",
            project_id="proj-001",
            complexity=TaskComplexity.EPIC,
            technologies=["python"],
        )

        engine = TaskWorkflowEngine.__new__(TaskWorkflowEngine)
        branch = engine._generate_branch_name(task)

        assert branch.startswith("epic/")

    def test_branch_name_slug_sanitization(self) -> None:
        """Test that special characters are sanitized from branch names."""
        from sibyl.tasks.workflow import TaskWorkflowEngine

        task = Task(
            id="jkl22222-test-id",
            title="Fix: Auth (Bug #123) & More!",
            description="Complex title",
            project_id="proj-001",
            technologies=["python"],
        )

        engine = TaskWorkflowEngine.__new__(TaskWorkflowEngine)
        branch = engine._generate_branch_name(task)

        # Should not contain special characters
        assert ":" not in branch
        assert "(" not in branch
        assert ")" not in branch
        assert "&" not in branch
        assert "!" not in branch
        # Should have normalized dashes
        assert "fix-auth-bug-123-more" in branch

    def test_branch_name_truncation(self) -> None:
        """Test that long titles are truncated."""
        from sibyl.tasks.workflow import TaskWorkflowEngine

        long_title = "A" * 100  # Very long title
        task = Task(
            id="mno33333-test-id",
            title=long_title,
            description="Long title test",
            project_id="proj-001",
            technologies=["python"],
        )

        engine = TaskWorkflowEngine.__new__(TaskWorkflowEngine)
        branch = engine._generate_branch_name(task)

        # Slug portion should be max 50 chars
        parts = branch.split("-", 1)  # Split after task_num
        if len(parts) > 1:
            slug_part = parts[1]
            assert len(slug_part) <= 50


class TestWorkflowTransitionCoverage:
    """Tests to verify full transition coverage."""

    def test_happy_path_workflow(self) -> None:
        """Verify the happy path: backlog -> todo -> doing -> review -> done."""
        statuses = [
            TaskStatus.BACKLOG,
            TaskStatus.TODO,
            TaskStatus.DOING,
            TaskStatus.REVIEW,
            TaskStatus.DONE,
        ]

        for i in range(len(statuses) - 1):
            from_status = statuses[i]
            to_status = statuses[i + 1]
            assert is_valid_transition(from_status, to_status), (
                f"Expected valid: {from_status} -> {to_status}"
            )

    def test_blocked_recovery_path(self) -> None:
        """Verify: doing -> blocked -> doing -> review -> done."""
        path = [
            (TaskStatus.DOING, TaskStatus.BLOCKED),
            (TaskStatus.BLOCKED, TaskStatus.DOING),
            (TaskStatus.DOING, TaskStatus.REVIEW),
            (TaskStatus.REVIEW, TaskStatus.DONE),
        ]

        for from_status, to_status in path:
            assert is_valid_transition(from_status, to_status)

    def test_revision_path(self) -> None:
        """Verify: doing -> review -> doing (revision) -> review -> done."""
        path = [
            (TaskStatus.DOING, TaskStatus.REVIEW),
            (TaskStatus.REVIEW, TaskStatus.DOING),  # Sent back for revision
            (TaskStatus.DOING, TaskStatus.REVIEW),  # Fixed and resubmitted
            (TaskStatus.REVIEW, TaskStatus.DONE),
        ]

        for from_status, to_status in path:
            assert is_valid_transition(from_status, to_status)

    def test_early_cancellation_paths(self) -> None:
        """Verify tasks can be archived from any active state."""
        active_states = [
            TaskStatus.BACKLOG,
            TaskStatus.TODO,
            TaskStatus.DOING,
            TaskStatus.BLOCKED,
            TaskStatus.REVIEW,
            TaskStatus.DONE,
        ]

        for status in active_states:
            assert is_valid_transition(status, TaskStatus.ARCHIVED)


# =============================================================================
# Integration-ish workflow tests with in-memory fakes
# =============================================================================


class _FakeEntityManager:
    """Lightweight in-memory entity manager for workflow tests."""

    def __init__(self, task: Task) -> None:
        self.task = task

    async def get(self, entity_id: str) -> Task:
        return self.task

    async def create(self, entity) -> str:  # type: ignore[override]
        # Accept creation of derived episodes; return provided id
        return entity.id

    async def create_direct(self, entity) -> str:  # type: ignore[override]
        # Mirror real EntityManager API used by TaskWorkflowEngine
        return await self.create(entity)

    async def update(self, entity_id: str, updates: dict) -> Task:
        # Persist status and arbitrary fields into metadata for parity with real manager
        meta = {
            **self.task.metadata,
            **{k: v for k, v in updates.items() if k not in {"name", "description", "content"}},
        }
        self.task.metadata = meta
        if "status" in updates:
            self.task.status = updates["status"]
            self.task.metadata["status"] = (
                updates["status"].value
                if hasattr(updates["status"], "value")
                else updates["status"]
            )
        if "branch_name" in updates:
            self.task.branch_name = updates["branch_name"]
        if "blockers_encountered" in updates:
            self.task.blockers_encountered = updates["blockers_encountered"]
        if "learnings" in updates:
            self.task.learnings = updates["learnings"]
        if "actual_hours" in updates:
            self.task.actual_hours = updates["actual_hours"]
        if "completed_at" in updates:
            self.task.completed_at = updates["completed_at"]
        if "started_at" in updates:
            self.task.started_at = updates["started_at"]
        if "assignees" in updates:
            self.task.assignees = updates["assignees"]
        return self.task


class _FakeRelationshipManager:
    async def get_for_entity(
        self, entity_id: str, relationship_types=None, direction: str = "outgoing"
    ):
        return []

    async def create(self, relationship):
        return relationship.id


class _FakeDriver:
    async def execute_query(self, query: str, **kwargs):
        # Minimal project progress payload
        return [{"total": 1, "done": 1, "doing": 0}]


class _FakeGraphClient:
    def __init__(self) -> None:
        self.client = type("Graph", (), {"driver": _FakeDriver()})()

    @staticmethod
    def normalize_result(result):
        """Normalize query results."""
        if result is None:
            return []
        if isinstance(result, tuple):
            return result[0] if result else []
        if isinstance(result, list):
            return result
        return []

    async def execute_read(self, query: str, **params):
        """Execute a read query."""
        result = await self.client.driver.execute_query(query, **params)
        return self.normalize_result(result)

    async def execute_read_org(self, query: str, organization_id: str, **params):
        """Execute a read query with organization context."""
        result = await self.client.driver.execute_query(query, **params)
        return self.normalize_result(result)

    async def execute_write_org(self, query: str, organization_id: str, **params):
        """Execute a write query with organization context."""
        result = await self.client.driver.execute_query(query, **params)
        return self.normalize_result(result)


@pytest.mark.asyncio
async def test_workflow_transitions_persist_status_and_branch() -> None:
    """Ensure workflow updates status and branch using the managers."""
    from sibyl.tasks.workflow import TaskWorkflowEngine

    task = Task(
        id="task-1",
        title="Implement feature X",
        description="Do the work",
        project_id="proj-001",
        status=TaskStatus.TODO,
    )
    entity_manager = _FakeEntityManager(task)
    relationship_manager = _FakeRelationshipManager()
    engine = TaskWorkflowEngine(
        entity_manager, relationship_manager, _FakeGraphClient(), organization_id="test-org"
    )

    started = await engine.start_task(task.id, assignee="alice")
    assert started.status == TaskStatus.DOING
    assert "alice" in started.assignees
    assert started.branch_name is not None

    blocked = await engine.block_task(task.id, blocker_description="waiting on review")
    assert blocked.status == TaskStatus.BLOCKED
    assert "waiting on review" in blocked.blockers_encountered

    unblocked = await engine.unblock_task(task.id)
    assert unblocked.status == TaskStatus.DOING

    completed = await engine.complete_task(task.id, actual_hours=3.5, learnings="use cache")
    assert completed.status == TaskStatus.DONE
    assert completed.learnings == "use cache"
    assert completed.actual_hours == 3.5


@pytest.mark.asyncio
async def test_submit_for_review_transitions_to_review() -> None:
    """Test submit_for_review transitions task to review status."""
    from sibyl.tasks.workflow import TaskWorkflowEngine

    task = Task(
        id="task-review-1",
        title="Feature for review",
        description="Ready for review",
        project_id="proj-001",
        status=TaskStatus.DOING,
    )
    entity_manager = _FakeEntityManager(task)
    relationship_manager = _FakeRelationshipManager()
    engine = TaskWorkflowEngine(
        entity_manager, relationship_manager, _FakeGraphClient(), organization_id="test-org"
    )

    reviewed = await engine.submit_for_review(
        task.id, commit_shas=["abc123", "def456"], pr_url="https://github.com/org/repo/pull/42"
    )
    assert reviewed.status == TaskStatus.REVIEW


@pytest.mark.asyncio
async def test_submit_for_review_without_pr_url() -> None:
    """Test submit_for_review works without PR URL."""
    from sibyl.tasks.workflow import TaskWorkflowEngine

    task = Task(
        id="task-review-2",
        title="Feature no PR",
        description="Review without PR",
        project_id="proj-001",
        status=TaskStatus.DOING,
    )
    entity_manager = _FakeEntityManager(task)
    relationship_manager = _FakeRelationshipManager()
    engine = TaskWorkflowEngine(
        entity_manager, relationship_manager, _FakeGraphClient(), organization_id="test-org"
    )

    reviewed = await engine.submit_for_review(task.id, commit_shas=["abc123"])
    assert reviewed.status == TaskStatus.REVIEW


@pytest.mark.asyncio
async def test_archive_task_transitions_to_archived() -> None:
    """Test archive_task transitions task to archived status."""
    from sibyl.tasks.workflow import TaskWorkflowEngine

    task = Task(
        id="task-archive-1",
        title="Task to archive",
        description="Will be archived",
        project_id="proj-001",
        status=TaskStatus.TODO,
    )
    entity_manager = _FakeEntityManager(task)
    relationship_manager = _FakeRelationshipManager()
    engine = TaskWorkflowEngine(
        entity_manager, relationship_manager, _FakeGraphClient(), organization_id="test-org"
    )

    archived = await engine.archive_task(task.id, reason="No longer needed")
    assert archived.status == TaskStatus.ARCHIVED


@pytest.mark.asyncio
async def test_archive_task_without_reason() -> None:
    """Test archive_task works without a reason."""
    from sibyl.tasks.workflow import TaskWorkflowEngine

    task = Task(
        id="task-archive-2",
        title="Task to archive quietly",
        description="Archive without reason",
        project_id="proj-001",
        status=TaskStatus.DONE,
    )
    entity_manager = _FakeEntityManager(task)
    relationship_manager = _FakeRelationshipManager()
    engine = TaskWorkflowEngine(
        entity_manager, relationship_manager, _FakeGraphClient(), organization_id="test-org"
    )

    archived = await engine.archive_task(task.id)
    assert archived.status == TaskStatus.ARCHIVED


@pytest.mark.asyncio
async def test_cannot_transition_from_archived() -> None:
    """Test that transitions from ARCHIVED status raise error."""
    from sibyl.tasks.workflow import TaskWorkflowEngine

    task = Task(
        id="task-archived",
        title="Archived task",
        description="Already archived",
        project_id="proj-001",
        status=TaskStatus.ARCHIVED,
    )
    entity_manager = _FakeEntityManager(task)
    relationship_manager = _FakeRelationshipManager()
    engine = TaskWorkflowEngine(
        entity_manager, relationship_manager, _FakeGraphClient(), organization_id="test-org"
    )

    with pytest.raises(InvalidTransitionError):
        await engine.start_task(task.id, assignee="bob")


@pytest.mark.asyncio
async def test_complete_task_with_learnings_creates_episode() -> None:
    """Test that completing task with learnings triggers episode creation."""
    from sibyl.tasks.workflow import TaskWorkflowEngine

    task = Task(
        id="task-learning",
        title="Task with learnings",
        description="Do something complex",
        project_id="proj-001",
        status=TaskStatus.DOING,
        feature="authentication",
        technologies=["python", "redis"],
    )

    class TrackingEntityManager(_FakeEntityManager):
        """Entity manager that tracks create calls."""

        def __init__(self, task: Task) -> None:
            super().__init__(task)
            self.episodes_created: list = []

        async def create(self, entity) -> str:
            self.episodes_created.append(entity)
            return entity.id

    entity_manager = TrackingEntityManager(task)
    relationship_manager = _FakeRelationshipManager()
    engine = TaskWorkflowEngine(
        entity_manager, relationship_manager, _FakeGraphClient(), organization_id="test-org"
    )

    completed = await engine.complete_task(
        task.id, actual_hours=5.0, learnings="Redis connection pooling is essential"
    )
    assert completed.status == TaskStatus.DONE
    assert completed.learnings == "Redis connection pooling is essential"
    # Verify episode was created
    assert len(entity_manager.episodes_created) == 1
    episode = entity_manager.episodes_created[0]
    assert "task-learning" in episode.metadata.get("task_id", "")


@pytest.mark.asyncio
async def test_transition_task_no_op() -> None:
    """Test that transition to same status is a no-op."""
    from sibyl.tasks.workflow import TaskWorkflowEngine

    task = Task(
        id="task-noop",
        title="Task same status",
        description="Will stay the same",
        project_id="proj-001",
        status=TaskStatus.TODO,
    )
    entity_manager = _FakeEntityManager(task)
    relationship_manager = _FakeRelationshipManager()
    engine = TaskWorkflowEngine(
        entity_manager, relationship_manager, _FakeGraphClient(), organization_id="test-org"
    )

    result = await engine.transition_task(task.id, TaskStatus.TODO)
    assert result.status == TaskStatus.TODO


@pytest.mark.asyncio
async def test_transition_task_with_updates() -> None:
    """Test transition_task applies additional updates."""
    from sibyl.tasks.workflow import TaskWorkflowEngine

    task = Task(
        id="task-updates",
        title="Task with updates",
        description="Will get updates",
        project_id="proj-001",
        status=TaskStatus.TODO,
    )
    entity_manager = _FakeEntityManager(task)
    relationship_manager = _FakeRelationshipManager()
    engine = TaskWorkflowEngine(
        entity_manager, relationship_manager, _FakeGraphClient(), organization_id="test-org"
    )

    result = await engine.transition_task(
        task.id, TaskStatus.DOING, updates={"branch_name": "feature/custom-branch"}
    )
    assert result.status == TaskStatus.DOING
    assert result.branch_name == "feature/custom-branch"


@pytest.mark.asyncio
async def test_start_task_adds_assignee() -> None:
    """Test start_task adds assignee to task."""
    from sibyl.tasks.workflow import TaskWorkflowEngine

    task = Task(
        id="task-assignee",
        title="Task for assignee test",
        description="Will be assigned",
        project_id="proj-001",
        status=TaskStatus.TODO,
        assignees=["alice"],
    )
    entity_manager = _FakeEntityManager(task)
    relationship_manager = _FakeRelationshipManager()
    engine = TaskWorkflowEngine(
        entity_manager, relationship_manager, _FakeGraphClient(), organization_id="test-org"
    )

    started = await engine.start_task(task.id, assignee="bob")
    assert "alice" in started.assignees
    assert "bob" in started.assignees


@pytest.mark.asyncio
async def test_start_task_does_not_duplicate_assignee() -> None:
    """Test start_task doesn't add duplicate assignee."""
    from sibyl.tasks.workflow import TaskWorkflowEngine

    task = Task(
        id="task-dup-assignee",
        title="Task dedup test",
        description="Already assigned to alice",
        project_id="proj-001",
        status=TaskStatus.TODO,
        assignees=["alice"],
    )
    entity_manager = _FakeEntityManager(task)
    relationship_manager = _FakeRelationshipManager()
    engine = TaskWorkflowEngine(
        entity_manager, relationship_manager, _FakeGraphClient(), organization_id="test-org"
    )

    started = await engine.start_task(task.id, assignee="alice")
    # Should only have one "alice"
    assert started.assignees.count("alice") == 1
