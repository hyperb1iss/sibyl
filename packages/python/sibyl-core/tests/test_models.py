"""Tests for sibyl-core models."""

from sibyl_core.models import Task, TaskComplexity, TaskPriority, TaskStatus


class TestTaskModel:
    """Test Task model instantiation and defaults."""

    def test_task_creation_minimal(self) -> None:
        """Task can be created with required fields."""
        task = Task(
            id="task_abc123",
            name="Test task",
            title="Test task title",
        )
        assert task.id == "task_abc123"
        assert task.name == "Test task"
        assert task.title == "Test task title"
        assert task.status == TaskStatus.TODO
        assert task.priority == TaskPriority.MEDIUM
        assert task.complexity == TaskComplexity.MEDIUM
        assert task.project_id is None

    def test_task_creation_full(self) -> None:
        """Task can be created with all fields."""
        task = Task(
            id="task_xyz789",
            name="Full task",
            title="Full task title",
            project_id="project_xyz789",
            description="A detailed description",
            status=TaskStatus.DOING,
            priority=TaskPriority.HIGH,
            complexity=TaskComplexity.COMPLEX,
            feature="auth",
            tags=["backend", "security"],
            technologies=["python", "fastapi"],
            assignees=["alice", "bob"],
        )
        assert task.name == "Full task"
        assert task.title == "Full task title"
        assert task.status == TaskStatus.DOING
        assert task.priority == TaskPriority.HIGH
        assert task.complexity == TaskComplexity.COMPLEX
        assert task.feature == "auth"
        assert task.tags == ["backend", "security"]
        assert task.assignees == ["alice", "bob"]

    def test_task_status_enum_values(self) -> None:
        """TaskStatus enum has expected values."""
        assert TaskStatus.BACKLOG.value == "backlog"
        assert TaskStatus.TODO.value == "todo"
        assert TaskStatus.DOING.value == "doing"
        assert TaskStatus.BLOCKED.value == "blocked"
        assert TaskStatus.REVIEW.value == "review"
        assert TaskStatus.DONE.value == "done"
        assert TaskStatus.ARCHIVED.value == "archived"

    def test_task_priority_enum_values(self) -> None:
        """TaskPriority enum has expected values."""
        assert TaskPriority.CRITICAL.value == "critical"
        assert TaskPriority.HIGH.value == "high"
        assert TaskPriority.MEDIUM.value == "medium"
        assert TaskPriority.LOW.value == "low"
        assert TaskPriority.SOMEDAY.value == "someday"

    def test_task_complexity_enum_values(self) -> None:
        """TaskComplexity enum has expected values."""
        assert TaskComplexity.TRIVIAL.value == "trivial"
        assert TaskComplexity.SIMPLE.value == "simple"
        assert TaskComplexity.MEDIUM.value == "medium"
        assert TaskComplexity.COMPLEX.value == "complex"
        assert TaskComplexity.EPIC.value == "epic"
