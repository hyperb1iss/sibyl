"""Tests for task dependency detection and cycle checking."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from sibyl.models.tasks import TaskStatus
from sibyl.tasks.dependencies import (
    CycleResult,
    DependencyResult,
    TaskOrderResult,
    detect_dependency_cycles,
    get_blocking_tasks,
    get_task_dependencies,
    suggest_task_order,
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


# =============================================================================
# Tests for async functions
# =============================================================================

TEST_ORG_ID = "org_test_123"


class TestGetTaskDependencies:
    """Tests for get_task_dependencies function."""

    @pytest.mark.asyncio
    async def test_returns_direct_dependencies(self) -> None:
        """Should return direct dependencies from graph query."""
        mock_client = MagicMock()
        mock_client.execute_read_org = AsyncMock(
            return_value=[
                ("dep-1", TaskStatus.TODO.value),
                ("dep-2", TaskStatus.DONE.value),
            ]
        )

        result = await get_task_dependencies(mock_client, "task-123", TEST_ORG_ID, depth=1)

        assert result.task_id == "task-123"
        assert "dep-1" in result.dependencies
        assert "dep-2" in result.dependencies
        assert len(result.dependencies) == 2

    @pytest.mark.asyncio
    async def test_identifies_blockers(self) -> None:
        """Should identify incomplete dependencies as blockers."""
        mock_client = MagicMock()
        mock_client.execute_read_org = AsyncMock(
            return_value=[
                ("dep-1", TaskStatus.TODO.value),  # Incomplete - blocker
                ("dep-2", TaskStatus.DOING.value),  # Incomplete - blocker
                ("dep-3", TaskStatus.DONE.value),  # Complete - not blocker
                ("dep-4", TaskStatus.ARCHIVED.value),  # Archived - not blocker
            ]
        )

        result = await get_task_dependencies(mock_client, "task-123", TEST_ORG_ID, depth=1)

        assert len(result.blockers) == 2
        assert "dep-1" in result.blockers
        assert "dep-2" in result.blockers
        assert "dep-3" not in result.blockers
        assert "dep-4" not in result.blockers

    @pytest.mark.asyncio
    async def test_handles_dict_records(self) -> None:
        """Should handle dict-style records from graph query."""
        mock_client = MagicMock()
        mock_client.execute_read_org = AsyncMock(
            return_value=[
                {"dep_id": "dep-1", "dep_status": TaskStatus.TODO.value},
                {"dep_id": "dep-2", "dep_status": TaskStatus.DONE.value},
            ]
        )

        result = await get_task_dependencies(mock_client, "task-123", TEST_ORG_ID, depth=1)

        assert len(result.dependencies) == 2
        assert "dep-1" in result.dependencies

    @pytest.mark.asyncio
    async def test_clamps_depth(self) -> None:
        """Should clamp depth to 1-5 range."""
        mock_client = MagicMock()
        mock_client.execute_read_org = AsyncMock(return_value=[])

        # Depth too low
        result = await get_task_dependencies(mock_client, "task-123", TEST_ORG_ID, depth=0)
        assert result.depth == 1

        # Depth too high
        result = await get_task_dependencies(mock_client, "task-123", TEST_ORG_ID, depth=10)
        assert result.depth == 1  # Not include_transitive, so depth stays 1

    @pytest.mark.asyncio
    async def test_transitive_dependencies(self) -> None:
        """Should use deeper traversal when include_transitive is True."""
        mock_client = MagicMock()
        mock_client.execute_read_org = AsyncMock(return_value=[])

        result = await get_task_dependencies(
            mock_client, "task-123", TEST_ORG_ID, depth=3, include_transitive=True
        )

        assert result.depth == 3

    @pytest.mark.asyncio
    async def test_handles_empty_results(self) -> None:
        """Should handle tasks with no dependencies."""
        mock_client = MagicMock()
        mock_client.execute_read_org = AsyncMock(return_value=[])

        result = await get_task_dependencies(mock_client, "task-standalone", TEST_ORG_ID)

        assert result.dependencies == []
        assert result.blockers == []

    @pytest.mark.asyncio
    async def test_handles_query_exception(self) -> None:
        """Should return empty result on query failure."""
        mock_client = MagicMock()
        mock_client.execute_read_org = AsyncMock(side_effect=RuntimeError("Connection failed"))

        result = await get_task_dependencies(mock_client, "task-123", TEST_ORG_ID)

        assert result.dependencies == []
        assert result.blockers == []

    @pytest.mark.asyncio
    async def test_skips_none_dep_id(self) -> None:
        """Should skip records with None dep_id."""
        mock_client = MagicMock()
        mock_client.execute_read_org = AsyncMock(
            return_value=[
                (None, TaskStatus.TODO.value),
                ("dep-1", TaskStatus.TODO.value),
            ]
        )

        result = await get_task_dependencies(mock_client, "task-123", TEST_ORG_ID)

        assert len(result.dependencies) == 1
        assert "dep-1" in result.dependencies


class TestGetBlockingTasks:
    """Tests for get_blocking_tasks function."""

    @pytest.mark.asyncio
    async def test_returns_dependent_tasks(self) -> None:
        """Should return tasks that depend on the given task."""
        mock_client = MagicMock()
        mock_client.execute_read_org = AsyncMock(
            return_value=[
                ("dependent-1", TaskStatus.TODO.value),
                ("dependent-2", TaskStatus.DOING.value),
            ]
        )

        result = await get_blocking_tasks(mock_client, "task-123", TEST_ORG_ID)

        assert result.task_id == "task-123"
        assert len(result.dependencies) == 2
        assert "dependent-1" in result.dependencies
        assert "dependent-2" in result.dependencies

    @pytest.mark.asyncio
    async def test_identifies_incomplete_dependents(self) -> None:
        """Should identify incomplete dependents as blockers."""
        mock_client = MagicMock()
        mock_client.execute_read_org = AsyncMock(
            return_value=[
                ("dep-1", TaskStatus.TODO.value),  # Incomplete
                ("dep-2", TaskStatus.DONE.value),  # Complete
            ]
        )

        result = await get_blocking_tasks(mock_client, "task-123", TEST_ORG_ID)

        assert len(result.blockers) == 1
        assert "dep-1" in result.blockers

    @pytest.mark.asyncio
    async def test_handles_dict_records(self) -> None:
        """Should handle dict-style records."""
        mock_client = MagicMock()
        mock_client.execute_read_org = AsyncMock(
            return_value=[
                {"dep_id": "dependent-1", "dep_status": TaskStatus.TODO.value},
            ]
        )

        result = await get_blocking_tasks(mock_client, "task-123", TEST_ORG_ID)

        assert "dependent-1" in result.dependencies

    @pytest.mark.asyncio
    async def test_clamps_depth(self) -> None:
        """Should clamp depth to 1-5 range."""
        mock_client = MagicMock()
        mock_client.execute_read_org = AsyncMock(return_value=[])

        result = await get_blocking_tasks(mock_client, "task-123", TEST_ORG_ID, depth=10)

        assert result.depth == 5

    @pytest.mark.asyncio
    async def test_handles_query_exception(self) -> None:
        """Should return empty result on query failure."""
        mock_client = MagicMock()
        mock_client.execute_read_org = AsyncMock(side_effect=RuntimeError("Connection failed"))

        result = await get_blocking_tasks(mock_client, "task-123", TEST_ORG_ID)

        assert result.dependencies == []
        assert result.blockers == []


class TestDetectDependencyCycles:
    """Tests for detect_dependency_cycles function."""

    @pytest.mark.asyncio
    async def test_no_cycles_detected(self) -> None:
        """Should detect no cycles in acyclic graph."""
        mock_client = MagicMock()
        # A -> B -> C (no cycle)
        mock_client.execute_read_org = AsyncMock(
            return_value=[
                ("task-a", "task-b"),
                ("task-b", "task-c"),
            ]
        )

        result = await detect_dependency_cycles(mock_client, TEST_ORG_ID)

        assert result.has_cycles is False
        assert result.cycles == []
        assert "No cycles" in result.message

    @pytest.mark.asyncio
    async def test_detects_simple_cycle(self) -> None:
        """Should detect a simple cycle."""
        mock_client = MagicMock()
        # A -> B -> C -> A (cycle)
        mock_client.execute_read_org = AsyncMock(
            return_value=[
                ("task-a", "task-b"),
                ("task-b", "task-c"),
                ("task-c", "task-a"),
            ]
        )

        result = await detect_dependency_cycles(mock_client, TEST_ORG_ID)

        assert result.has_cycles is True
        assert len(result.cycles) >= 1
        assert "Found" in result.message

    @pytest.mark.asyncio
    async def test_detects_self_cycle(self) -> None:
        """Should detect a task depending on itself."""
        mock_client = MagicMock()
        mock_client.execute_read_org = AsyncMock(
            return_value=[
                ("task-a", "task-a"),  # Self-dependency
            ]
        )

        result = await detect_dependency_cycles(mock_client, TEST_ORG_ID)

        assert result.has_cycles is True

    @pytest.mark.asyncio
    async def test_project_scoped_query(self) -> None:
        """Should use project-scoped query when project_id provided."""
        mock_client = MagicMock()
        mock_client.execute_read_org = AsyncMock(return_value=[])

        await detect_dependency_cycles(mock_client, TEST_ORG_ID, project_id="proj-123")

        # Verify query was called with project_id
        call_args = mock_client.execute_read_org.call_args
        assert call_args.kwargs.get("project_id") == "proj-123"

    @pytest.mark.asyncio
    async def test_handles_dict_records(self) -> None:
        """Should handle dict-style records."""
        mock_client = MagicMock()
        mock_client.execute_read_org = AsyncMock(
            return_value=[
                {"from_id": "task-a", "to_id": "task-b"},
            ]
        )

        result = await detect_dependency_cycles(mock_client, TEST_ORG_ID)

        assert result.has_cycles is False

    @pytest.mark.asyncio
    async def test_handles_empty_graph(self) -> None:
        """Should handle empty dependency graph."""
        mock_client = MagicMock()
        mock_client.execute_read_org = AsyncMock(return_value=[])

        result = await detect_dependency_cycles(mock_client, TEST_ORG_ID)

        assert result.has_cycles is False
        assert result.cycles == []

    @pytest.mark.asyncio
    async def test_handles_query_exception(self) -> None:
        """Should return safe result on query failure."""
        mock_client = MagicMock()
        mock_client.execute_read_org = AsyncMock(side_effect=RuntimeError("Connection failed"))

        result = await detect_dependency_cycles(mock_client, TEST_ORG_ID)

        assert result.has_cycles is False
        assert "failed" in result.message.lower()

    @pytest.mark.asyncio
    async def test_skips_none_ids(self) -> None:
        """Should skip records with None IDs."""
        mock_client = MagicMock()
        mock_client.execute_read_org = AsyncMock(
            return_value=[
                (None, "task-b"),
                ("task-a", None),
                ("task-a", "task-b"),
            ]
        )

        result = await detect_dependency_cycles(mock_client, TEST_ORG_ID)

        # Should only process the valid edge
        assert result.has_cycles is False


class TestSuggestTaskOrder:
    """Tests for suggest_task_order function."""

    @pytest.mark.asyncio
    async def test_orders_simple_chain(self) -> None:
        """Should order tasks in dependency order."""
        mock_client = MagicMock()

        # Tasks query
        task_results = [
            ("task-1", TaskStatus.TODO.value, 10),
            ("task-2", TaskStatus.TODO.value, 20),
            ("task-3", TaskStatus.TODO.value, 30),
        ]
        # Dependency edges: task-2 depends on task-1, task-3 depends on task-2
        dep_results = [
            ("task-2", "task-1"),
            ("task-3", "task-2"),
        ]

        mock_client.execute_read_org = AsyncMock(side_effect=[task_results, dep_results])

        result = await suggest_task_order(mock_client, TEST_ORG_ID)

        assert len(result.ordered_tasks) == 3
        # task-1 should come before task-2
        assert result.ordered_tasks.index("task-1") < result.ordered_tasks.index("task-2")
        # task-2 should come before task-3
        assert result.ordered_tasks.index("task-2") < result.ordered_tasks.index("task-3")

    @pytest.mark.asyncio
    async def test_handles_independent_tasks(self) -> None:
        """Should order independent tasks by priority."""
        mock_client = MagicMock()

        task_results = [
            ("task-low", TaskStatus.TODO.value, 10),
            ("task-high", TaskStatus.TODO.value, 100),
            ("task-med", TaskStatus.TODO.value, 50),
        ]
        dep_results = []  # No dependencies

        mock_client.execute_read_org = AsyncMock(side_effect=[task_results, dep_results])

        result = await suggest_task_order(mock_client, TEST_ORG_ID)

        assert len(result.ordered_tasks) == 3
        # Highest priority should come first
        assert result.ordered_tasks[0] == "task-high"

    @pytest.mark.asyncio
    async def test_identifies_cycle_tasks(self) -> None:
        """Should identify tasks in cycles as unordered."""
        mock_client = MagicMock()

        task_results = [
            ("task-ok", TaskStatus.TODO.value, 10),
            ("task-a", TaskStatus.TODO.value, 10),
            ("task-b", TaskStatus.TODO.value, 10),
        ]
        # task-a and task-b form a cycle
        dep_results = [
            ("task-a", "task-b"),
            ("task-b", "task-a"),
        ]

        mock_client.execute_read_org = AsyncMock(side_effect=[task_results, dep_results])

        result = await suggest_task_order(mock_client, TEST_ORG_ID)

        # task-ok should be ordered, cycle tasks should be unordered
        assert "task-ok" in result.ordered_tasks
        assert len(result.unordered_tasks) == 2
        assert "task-a" in result.unordered_tasks
        assert "task-b" in result.unordered_tasks
        assert len(result.warnings) >= 1

    @pytest.mark.asyncio
    async def test_status_filter(self) -> None:
        """Should filter tasks by status."""
        mock_client = MagicMock()

        task_results = [
            ("task-todo", TaskStatus.TODO.value, 10),
            ("task-done", TaskStatus.DONE.value, 10),
            ("task-doing", TaskStatus.DOING.value, 10),
        ]
        dep_results = []

        mock_client.execute_read_org = AsyncMock(side_effect=[task_results, dep_results])

        result = await suggest_task_order(
            mock_client,
            TEST_ORG_ID,
            status_filter=[TaskStatus.TODO, TaskStatus.DOING],
        )

        assert "task-todo" in result.ordered_tasks
        assert "task-doing" in result.ordered_tasks
        assert "task-done" not in result.ordered_tasks

    @pytest.mark.asyncio
    async def test_project_scoped_query(self) -> None:
        """Should use project-scoped queries when project_id provided."""
        mock_client = MagicMock()
        mock_client.execute_read_org = AsyncMock(
            side_effect=[[], []]  # Empty results
        )

        await suggest_task_order(mock_client, TEST_ORG_ID, project_id="proj-123")

        # Both calls should have project_id
        calls = mock_client.execute_read_org.call_args_list
        assert calls[0].kwargs.get("project_id") == "proj-123"
        assert calls[1].kwargs.get("project_id") == "proj-123"

    @pytest.mark.asyncio
    async def test_handles_dict_records(self) -> None:
        """Should handle dict-style records."""
        mock_client = MagicMock()

        task_results = [
            {"task_id": "task-1", "status": TaskStatus.TODO.value, "priority": 10},
        ]
        dep_results = []

        mock_client.execute_read_org = AsyncMock(side_effect=[task_results, dep_results])

        result = await suggest_task_order(mock_client, TEST_ORG_ID)

        assert "task-1" in result.ordered_tasks

    @pytest.mark.asyncio
    async def test_handles_empty_project(self) -> None:
        """Should handle project with no tasks."""
        mock_client = MagicMock()
        mock_client.execute_read_org = AsyncMock(
            side_effect=[[], []]  # No tasks, no deps
        )

        result = await suggest_task_order(mock_client, TEST_ORG_ID)

        assert result.ordered_tasks == []
        assert result.unordered_tasks == []

    @pytest.mark.asyncio
    async def test_handles_query_exception(self) -> None:
        """Should return safe result on query failure."""
        mock_client = MagicMock()
        mock_client.execute_read_org = AsyncMock(side_effect=RuntimeError("Connection failed"))

        result = await suggest_task_order(mock_client, TEST_ORG_ID)

        assert result.ordered_tasks == []
        assert "failed" in result.warnings[0].lower()

    @pytest.mark.asyncio
    async def test_ignores_external_dependencies(self) -> None:
        """Should ignore dependencies to tasks not in the task set."""
        mock_client = MagicMock()

        task_results = [
            ("task-1", TaskStatus.TODO.value, 10),
            ("task-2", TaskStatus.TODO.value, 20),
        ]
        # task-2 depends on task-external which is not in our task set
        dep_results = [
            ("task-2", "task-external"),
        ]

        mock_client.execute_read_org = AsyncMock(side_effect=[task_results, dep_results])

        result = await suggest_task_order(mock_client, TEST_ORG_ID)

        # Both tasks should be ordered (external dep ignored)
        assert len(result.ordered_tasks) == 2

    @pytest.mark.asyncio
    async def test_handles_none_priority(self) -> None:
        """Should handle tasks with None priority."""
        mock_client = MagicMock()

        task_results = [
            ("task-1", TaskStatus.TODO.value, None),
            ("task-2", TaskStatus.TODO.value, 50),
        ]
        dep_results = []

        mock_client.execute_read_org = AsyncMock(side_effect=[task_results, dep_results])

        result = await suggest_task_order(mock_client, TEST_ORG_ID)

        assert len(result.ordered_tasks) == 2
