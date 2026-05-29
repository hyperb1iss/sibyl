"""Tests for sibyl-core models."""

from sibyl_core.models import (
    EntityType,
    Epic,
    EpicStatus,
    Task,
    TaskComplexity,
    TaskPriority,
    TaskStatus,
    derive_container_status,
)


def test_entity_type_accepts_guide_alias() -> None:
    assert EntityType.GUIDE.value == "guide"
    assert EntityType("guide") is EntityType.GUIDE
    assert EntityType("GUIDE") is EntityType.GUIDE


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

    def test_task_parent_task_id_defaults_none_and_coexists_with_epic(self) -> None:
        """parent_task_id is optional and independent from epic_id."""
        minimal = Task(id="task_min", name="Minimal", title="Minimal")
        assert minimal.parent_task_id is None
        assert minimal.epic_id is None

        linked = Task(
            id="task_child",
            name="Child task",
            title="Child task",
            epic_id="epic_legacy",
            parent_task_id="task_parent",
        )
        assert linked.parent_task_id == "task_parent"
        assert linked.epic_id == "epic_legacy"

        dumped = linked.model_dump()
        assert dumped["parent_task_id"] == "task_parent"
        assert dumped["epic_id"] == "epic_legacy"
        assert Task.model_validate(dumped).parent_task_id == "task_parent"

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


class TestDeriveContainerStatus:
    """The unified container-status rule (W14) derived from child statuses."""

    def test_no_children_is_planning(self) -> None:
        assert derive_container_status([]) == EpicStatus.PLANNING

    def test_doing_child_is_in_progress(self) -> None:
        assert (
            derive_container_status([TaskStatus.TODO, TaskStatus.DOING]) == EpicStatus.IN_PROGRESS
        )

    def test_review_child_is_in_progress(self) -> None:
        assert (
            derive_container_status([TaskStatus.DONE, TaskStatus.REVIEW]) == EpicStatus.IN_PROGRESS
        )

    def test_doing_outranks_blocked(self) -> None:
        # Live work wins even when another child is blocked.
        assert (
            derive_container_status([TaskStatus.BLOCKED, TaskStatus.DOING])
            == EpicStatus.IN_PROGRESS
        )

    def test_blocked_child_is_blocked_without_active_work(self) -> None:
        assert derive_container_status([TaskStatus.TODO, TaskStatus.BLOCKED]) == EpicStatus.BLOCKED

    def test_all_terminal_with_a_done_is_completed(self) -> None:
        assert (
            derive_container_status([TaskStatus.DONE, TaskStatus.ARCHIVED]) == EpicStatus.COMPLETED
        )

    def test_all_done_is_completed(self) -> None:
        assert derive_container_status([TaskStatus.DONE, TaskStatus.DONE]) == EpicStatus.COMPLETED

    def test_all_archived_is_archived(self) -> None:
        assert (
            derive_container_status([TaskStatus.ARCHIVED, TaskStatus.ARCHIVED])
            == EpicStatus.ARCHIVED
        )

    def test_only_todo_backlog_is_planning(self) -> None:
        assert derive_container_status([TaskStatus.TODO, TaskStatus.BACKLOG]) == EpicStatus.PLANNING

    def test_terminal_free_mix_is_planning(self) -> None:
        # TODO/BACKLOG with no doing, blocked, or done falls through to planning.
        assert derive_container_status([TaskStatus.BACKLOG]) == EpicStatus.PLANNING


class TestEpicDerivedFromTask:
    """Project a parent task plus its subtasks into an Epic-shaped view."""

    def _parent(self) -> Task:
        return Task(
            id="parent_task",
            name="Parent work item",
            title="Parent work item",
            description="A task that acts as an epic",
            project_id="project_xyz",
            priority=TaskPriority.HIGH,
            assignees=["alice"],
            tags=["backend"],
        )

    def test_projects_identity_status_and_progress(self) -> None:
        parent = self._parent()
        children = [
            Task(id="c1", name="c1", title="c1", status=TaskStatus.DONE),
            Task(id="c2", name="c2", title="c2", status=TaskStatus.DOING),
            Task(id="c3", name="c3", title="c3", status=TaskStatus.TODO),
        ]

        epic = Epic.derived_from_task(parent, children)

        assert isinstance(epic, Epic)
        assert epic.id == "parent_task"
        assert epic.title == "Parent work item"
        assert epic.description == "A task that acts as an epic"
        assert epic.project_id == "project_xyz"
        assert epic.priority == TaskPriority.HIGH
        assert epic.assignees == ["alice"]
        assert epic.tags == ["backend"]
        # One DOING child makes the container IN_PROGRESS; one DONE of three.
        assert epic.status == EpicStatus.IN_PROGRESS
        assert epic.total_tasks == 3
        assert epic.completed_tasks == 1

    def test_childless_parent_projects_to_planning(self) -> None:
        epic = Epic.derived_from_task(self._parent(), [])
        assert epic.status == EpicStatus.PLANNING
        assert epic.total_tasks == 0
        assert epic.completed_tasks == 0

    def test_projection_does_not_mutate_parent_collections(self) -> None:
        parent = self._parent()
        epic = Epic.derived_from_task(parent, [])
        epic.assignees.append("bob")
        epic.tags.append("frontend")
        # Copies, not aliases: the parent's lists are untouched.
        assert parent.assignees == ["alice"]
        assert parent.tags == ["backend"]
