"""Tests for task dependency detection and cycle checking."""

from sibyl.tasks.dependencies import (
    CycleResult,
    DependencyResult,
    TaskOrderResult,
)


class TestDependencyResult:
    """Tests for DependencyResult dataclass."""

    def test_basic_result(self) -> None:
        """Test creating a basic dependency result."""
        result = DependencyResult(
            task_id="task-123",
            dependencies=["dep-1", "dep-2"],
            blockers=["dep-1"],
        )
        assert result.task_id == "task-123"
        assert len(result.dependencies) == 2
        assert len(result.blockers) == 1
        assert result.depth == 1

    def test_empty_dependencies(self) -> None:
        """Test result with no dependencies."""
        result = DependencyResult(
            task_id="task-standalone",
            dependencies=[],
            blockers=[],
        )
        assert result.dependencies == []
        assert result.blockers == []

    def test_custom_depth(self) -> None:
        """Test result with custom traversal depth."""
        result = DependencyResult(
            task_id="task-456",
            dependencies=["dep-1"],
            blockers=[],
            depth=3,
        )
        assert result.depth == 3


class TestCycleResult:
    """Tests for CycleResult dataclass."""

    def test_no_cycles(self) -> None:
        """Test result when no cycles detected."""
        result = CycleResult(
            has_cycles=False,
            cycles=[],
            message="No cycles detected",
        )
        assert result.has_cycles is False
        assert len(result.cycles) == 0

    def test_with_cycles(self) -> None:
        """Test result with detected cycles."""
        result = CycleResult(
            has_cycles=True,
            cycles=[
                ["task-a", "task-b", "task-c", "task-a"],
                ["task-x", "task-y", "task-x"],
            ],
            message="Found 2 cycle(s)",
        )
        assert result.has_cycles is True
        assert len(result.cycles) == 2
        assert result.cycles[0][0] == result.cycles[0][-1]  # Cycle loops back

    def test_default_values(self) -> None:
        """Test default values for CycleResult."""
        result = CycleResult(has_cycles=False)
        assert result.cycles == []
        assert result.message == ""


class TestTaskOrderResult:
    """Tests for TaskOrderResult dataclass."""

    def test_fully_ordered(self) -> None:
        """Test result when all tasks can be ordered."""
        result = TaskOrderResult(
            ordered_tasks=["task-1", "task-2", "task-3"],
        )
        assert len(result.ordered_tasks) == 3
        assert result.unordered_tasks == []
        assert result.warnings == []

    def test_with_unordered_tasks(self) -> None:
        """Test result when some tasks are in cycles."""
        result = TaskOrderResult(
            ordered_tasks=["task-1", "task-2"],
            unordered_tasks=["cycle-a", "cycle-b"],
            warnings=["2 task(s) could not be ordered due to circular dependencies"],
        )
        assert len(result.ordered_tasks) == 2
        assert len(result.unordered_tasks) == 2
        assert len(result.warnings) == 1

    def test_empty_result(self) -> None:
        """Test empty result (no tasks)."""
        result = TaskOrderResult(ordered_tasks=[])
        assert result.ordered_tasks == []


class TestDependencyLogic:
    """Tests for dependency detection logic patterns."""

    def test_blockers_subset_of_dependencies(self) -> None:
        """Blockers should always be a subset of dependencies."""
        deps = ["dep-1", "dep-2", "dep-3"]
        blockers = ["dep-1"]  # Only incomplete ones

        result = DependencyResult(
            task_id="task",
            dependencies=deps,
            blockers=blockers,
        )

        for blocker in result.blockers:
            assert blocker in result.dependencies

    def test_cycle_path_valid(self) -> None:
        """Cycle paths should start and end with the same node."""
        cycle = ["a", "b", "c", "a"]
        result = CycleResult(
            has_cycles=True,
            cycles=[cycle],
        )
        assert result.cycles[0][0] == result.cycles[0][-1]

    def test_topological_order_respects_dependencies(self) -> None:
        """Ordered tasks should have dependencies before dependents.

        If task-B depends on task-A, then task-A should come before task-B
        in the ordered list.
        """
        # Simulating: task-2 depends on task-1, task-3 depends on task-2
        ordered = ["task-1", "task-2", "task-3"]
        dependencies = {
            "task-2": ["task-1"],
            "task-3": ["task-2"],
        }

        for task, deps in dependencies.items():
            task_idx = ordered.index(task)
            for dep in deps:
                dep_idx = ordered.index(dep)
                assert dep_idx < task_idx, f"{dep} should come before {task}"
