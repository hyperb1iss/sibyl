"""Tests for the task workflow engine and state machine."""

import pytest

from sibyl.errors import InvalidTransitionError
from sibyl.models.tasks import Task, TaskComplexity, TaskStatus
from sibyl.tasks.workflow import (
    VALID_TRANSITIONS,
    get_allowed_transitions,
    is_valid_transition,
)


class TestStateMachineDefinition:
    """Tests for the state machine constants and structure."""

    def test_all_statuses_have_transitions_defined(self) -> None:
        """Verify every TaskStatus has a transition entry."""
        for status in TaskStatus:
            assert status in VALID_TRANSITIONS, f"Missing transition entry for {status}"

    def test_archived_is_terminal_state(self) -> None:
        """Verify ARCHIVED has no outgoing transitions."""
        assert VALID_TRANSITIONS[TaskStatus.ARCHIVED] == set()

    def test_done_only_allows_archive(self) -> None:
        """Verify DONE can only transition to ARCHIVED."""
        assert VALID_TRANSITIONS[TaskStatus.DONE] == {TaskStatus.ARCHIVED}

    def test_backlog_can_transition_to_todo(self) -> None:
        """Verify backlog can be promoted to todo."""
        assert TaskStatus.TODO in VALID_TRANSITIONS[TaskStatus.BACKLOG]

    def test_todo_can_transition_to_doing(self) -> None:
        """Verify todo can be started."""
        assert TaskStatus.DOING in VALID_TRANSITIONS[TaskStatus.TODO]

    def test_doing_can_transition_to_blocked(self) -> None:
        """Verify doing can be blocked."""
        assert TaskStatus.BLOCKED in VALID_TRANSITIONS[TaskStatus.DOING]

    def test_doing_can_transition_to_review(self) -> None:
        """Verify doing can be submitted for review."""
        assert TaskStatus.REVIEW in VALID_TRANSITIONS[TaskStatus.DOING]

    def test_blocked_can_transition_to_doing(self) -> None:
        """Verify blocked can be unblocked."""
        assert TaskStatus.DOING in VALID_TRANSITIONS[TaskStatus.BLOCKED]

    def test_review_can_transition_to_done(self) -> None:
        """Verify review can be completed."""
        assert TaskStatus.DONE in VALID_TRANSITIONS[TaskStatus.REVIEW]

    def test_review_can_transition_to_doing(self) -> None:
        """Verify review can be sent back for revision."""
        assert TaskStatus.DOING in VALID_TRANSITIONS[TaskStatus.REVIEW]

    def test_all_active_states_can_archive(self) -> None:
        """Verify all non-terminal states can transition to archived."""
        non_terminal = [s for s in TaskStatus if s != TaskStatus.ARCHIVED]
        for status in non_terminal:
            allowed = VALID_TRANSITIONS[status]
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

    def test_todo_to_done_invalid(self) -> None:
        """Test todo -> done is invalid (can't skip work)."""
        assert not is_valid_transition(TaskStatus.TODO, TaskStatus.DONE)

    def test_backlog_to_done_invalid(self) -> None:
        """Test backlog -> done is invalid (can't skip work)."""
        assert not is_valid_transition(TaskStatus.BACKLOG, TaskStatus.DONE)

    def test_done_to_doing_invalid(self) -> None:
        """Test done -> doing is invalid (can't reopen)."""
        assert not is_valid_transition(TaskStatus.DONE, TaskStatus.DOING)

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
        assert allowed == {TaskStatus.TODO, TaskStatus.ARCHIVED}

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
        assert allowed == {TaskStatus.DOING, TaskStatus.ARCHIVED}

    def test_review_allowed_transitions(self) -> None:
        """Test allowed transitions from review."""
        allowed = get_allowed_transitions(TaskStatus.REVIEW)
        assert TaskStatus.DOING in allowed
        assert TaskStatus.DONE in allowed
        assert TaskStatus.ARCHIVED in allowed

    def test_done_allowed_transitions(self) -> None:
        """Test allowed transitions from done."""
        allowed = get_allowed_transitions(TaskStatus.DONE)
        assert allowed == {TaskStatus.ARCHIVED}

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
    engine = TaskWorkflowEngine(entity_manager, relationship_manager, _FakeGraphClient())

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
