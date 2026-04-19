"""Tests for task dependency detection and cycle checking."""

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sibyl_core.models.entities import EntityType, RelationshipType
from sibyl_core.models.tasks import TaskStatus
from sibyl_core.tasks.dependencies import (
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


def _make_task(
    task_id: str,
    *,
    status: TaskStatus | str = TaskStatus.TODO,
    task_order: int | None = 0,
    metadata: dict[str, Any] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=task_id,
        status=status,
        task_order=task_order,
        metadata=metadata or {},
    )


def _make_relationship(
    source_id: str | None,
    target_id: str | None,
    *,
    relationship_type: RelationshipType = RelationshipType.DEPENDS_ON,
) -> SimpleNamespace:
    return SimpleNamespace(
        source_id=source_id,
        target_id=target_id,
        relationship_type=relationship_type,
    )


def _make_dependency_managers(
    *,
    entity_lookup: dict[str, Any] | None = None,
    relationships_by_entity: dict[tuple[str, str], list[Any]] | None = None,
    tasks: list[Any] | None = None,
    all_relationships: list[Any] | None = None,
) -> tuple[SimpleNamespace, SimpleNamespace]:
    entity_lookup = entity_lookup or {}
    relationships_by_entity = relationships_by_entity or {}
    tasks = tasks or []
    all_relationships = all_relationships or []

    async def get(entity_id: str) -> Any:
        value = entity_lookup.get(entity_id)
        if isinstance(value, Exception):
            raise value
        return value

    async def list_by_type(
        _entity_type: Any,
        *,
        limit: int = 50,
        offset: int = 0,
        **_kwargs: Any,
    ) -> list[Any]:
        return tasks[offset : offset + limit]

    async def get_for_entity(
        entity_id: str,
        *,
        direction: str = "both",
        **_kwargs: Any,
    ) -> list[Any]:
        return relationships_by_entity.get((entity_id, direction), [])

    async def list_all(
        *,
        limit: int = 100,
        offset: int = 0,
        **_kwargs: Any,
    ) -> list[Any]:
        return all_relationships[offset : offset + limit]

    entity_manager = SimpleNamespace(
        get=AsyncMock(side_effect=get),
        list_by_type=AsyncMock(side_effect=list_by_type),
    )
    relationship_manager = SimpleNamespace(
        get_for_entity=AsyncMock(side_effect=get_for_entity),
        list_all=AsyncMock(side_effect=list_all),
    )
    return entity_manager, relationship_manager


class TestGetTaskDependencies:
    """Tests for get_task_dependencies function."""

    @pytest.mark.asyncio
    async def test_surreal_runtime_uses_graph_managers(self) -> None:
        """Should use entity/relationship managers in surreal mode."""
        entity_manager, relationship_manager = _make_dependency_managers(
            entity_lookup={
                "dep-1": _make_task("dep-1", status=TaskStatus.TODO, metadata={"status": "todo"}),
                "dep-2": _make_task("dep-2", status=TaskStatus.DONE, metadata={"status": "done"}),
            },
            relationships_by_entity={
                ("task-123", "outgoing"): [
                    _make_relationship("task-123", "dep-1"),
                    _make_relationship("task-123", "dep-2"),
                ]
            },
        )

        with patch(
            "sibyl_core.tasks.dependencies._get_graph_managers",
            return_value=(entity_manager, relationship_manager),
        ):
            result = await get_task_dependencies(MagicMock(), "task-123", TEST_ORG_ID)

        assert result.dependencies == ["dep-1", "dep-2"]
        assert result.blockers == ["dep-1"]
        relationship_manager.get_for_entity.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_returns_direct_dependencies(self) -> None:
        """Should return direct dependencies from relationship seams."""
        entity_manager, relationship_manager = _make_dependency_managers(
            entity_lookup={
                "dep-1": _make_task("dep-1", status=TaskStatus.TODO),
                "dep-2": _make_task("dep-2", status=TaskStatus.DONE),
            },
            relationships_by_entity={
                ("task-123", "outgoing"): [
                    _make_relationship("task-123", "dep-1"),
                    _make_relationship("task-123", "dep-2"),
                ]
            },
        )

        with patch(
            "sibyl_core.tasks.dependencies._get_graph_managers",
            return_value=(entity_manager, relationship_manager),
        ):
            result = await get_task_dependencies(MagicMock(), "task-123", TEST_ORG_ID, depth=1)

        assert result.task_id == "task-123"
        assert "dep-1" in result.dependencies
        assert "dep-2" in result.dependencies
        assert len(result.dependencies) == 2

    @pytest.mark.asyncio
    async def test_identifies_blockers(self) -> None:
        """Should identify incomplete dependencies as blockers."""
        entity_manager, relationship_manager = _make_dependency_managers(
            entity_lookup={
                "dep-1": _make_task("dep-1", status=TaskStatus.TODO),
                "dep-2": _make_task("dep-2", status=TaskStatus.DOING),
                "dep-3": _make_task("dep-3", status=TaskStatus.DONE),
                "dep-4": _make_task("dep-4", status=TaskStatus.ARCHIVED),
            },
            relationships_by_entity={
                ("task-123", "outgoing"): [
                    _make_relationship("task-123", "dep-1"),
                    _make_relationship("task-123", "dep-2"),
                    _make_relationship("task-123", "dep-3"),
                    _make_relationship("task-123", "dep-4"),
                ]
            },
        )

        with patch(
            "sibyl_core.tasks.dependencies._get_graph_managers",
            return_value=(entity_manager, relationship_manager),
        ):
            result = await get_task_dependencies(MagicMock(), "task-123", TEST_ORG_ID, depth=1)

        assert len(result.blockers) == 2
        assert "dep-1" in result.blockers
        assert "dep-2" in result.blockers
        assert "dep-3" not in result.blockers
        assert "dep-4" not in result.blockers

    @pytest.mark.asyncio
    async def test_handles_dict_records(self) -> None:
        """Should handle task-like objects with string statuses."""
        entity_manager, relationship_manager = _make_dependency_managers(
            entity_lookup={
                "dep-1": _make_task("dep-1", status=TaskStatus.TODO.value),
                "dep-2": _make_task("dep-2", status=TaskStatus.DONE.value),
            },
            relationships_by_entity={
                ("task-123", "outgoing"): [
                    _make_relationship("task-123", "dep-1"),
                    _make_relationship("task-123", "dep-2"),
                ]
            },
        )

        with patch(
            "sibyl_core.tasks.dependencies._get_graph_managers",
            return_value=(entity_manager, relationship_manager),
        ):
            result = await get_task_dependencies(MagicMock(), "task-123", TEST_ORG_ID, depth=1)

        assert len(result.dependencies) == 2
        assert "dep-1" in result.dependencies

    @pytest.mark.asyncio
    async def test_clamps_depth(self) -> None:
        """Should clamp depth to 1-5 range."""
        entity_manager, relationship_manager = _make_dependency_managers()

        with patch(
            "sibyl_core.tasks.dependencies._get_graph_managers",
            return_value=(entity_manager, relationship_manager),
        ):
            result = await get_task_dependencies(MagicMock(), "task-123", TEST_ORG_ID, depth=0)
        assert result.depth == 1

        with patch(
            "sibyl_core.tasks.dependencies._get_graph_managers",
            return_value=(entity_manager, relationship_manager),
        ):
            result = await get_task_dependencies(MagicMock(), "task-123", TEST_ORG_ID, depth=10)
        assert result.depth == 1  # Not include_transitive, so depth stays 1

    @pytest.mark.asyncio
    async def test_transitive_dependencies(self) -> None:
        """Should use deeper traversal when include_transitive is True."""
        entity_manager, relationship_manager = _make_dependency_managers()

        with patch(
            "sibyl_core.tasks.dependencies._get_graph_managers",
            return_value=(entity_manager, relationship_manager),
        ):
            result = await get_task_dependencies(
                MagicMock(), "task-123", TEST_ORG_ID, depth=3, include_transitive=True
            )

        assert result.depth == 3

    @pytest.mark.asyncio
    async def test_handles_empty_results(self) -> None:
        """Should handle tasks with no dependencies."""
        entity_manager, relationship_manager = _make_dependency_managers()

        with patch(
            "sibyl_core.tasks.dependencies._get_graph_managers",
            return_value=(entity_manager, relationship_manager),
        ):
            result = await get_task_dependencies(MagicMock(), "task-standalone", TEST_ORG_ID)

        assert result.dependencies == []
        assert result.blockers == []

    @pytest.mark.asyncio
    async def test_handles_query_exception(self) -> None:
        """Should return empty result on query failure."""
        with patch(
            "sibyl_core.tasks.dependencies._get_graph_managers",
            side_effect=RuntimeError("Connection failed"),
        ):
            result = await get_task_dependencies(MagicMock(), "task-123", TEST_ORG_ID)

        assert result.dependencies == []
        assert result.blockers == []

    @pytest.mark.asyncio
    async def test_skips_none_dep_id(self) -> None:
        """Should skip records with None dep_id."""
        entity_manager, relationship_manager = _make_dependency_managers(
            entity_lookup={
                "dep-1": _make_task("dep-1", status=TaskStatus.TODO),
            },
            relationships_by_entity={
                ("task-123", "outgoing"): [
                    _make_relationship("task-123", None),
                    _make_relationship("task-123", "dep-1"),
                ]
            },
        )

        with patch(
            "sibyl_core.tasks.dependencies._get_graph_managers",
            return_value=(entity_manager, relationship_manager),
        ):
            result = await get_task_dependencies(MagicMock(), "task-123", TEST_ORG_ID)

        assert len(result.dependencies) == 1
        assert "dep-1" in result.dependencies


class TestGetBlockingTasks:
    """Tests for get_blocking_tasks function."""

    @pytest.mark.asyncio
    async def test_surreal_runtime_uses_graph_managers(self) -> None:
        """Should use entity/relationship managers in surreal mode."""
        entity_manager, relationship_manager = _make_dependency_managers(
            entity_lookup={
                "blocked-1": _make_task(
                    "blocked-1", status=TaskStatus.TODO, metadata={"status": "todo"}
                ),
                "blocked-2": _make_task(
                    "blocked-2", status=TaskStatus.DONE, metadata={"status": "done"}
                ),
            },
            relationships_by_entity={
                ("task-123", "incoming"): [
                    _make_relationship("blocked-1", "task-123"),
                    _make_relationship("blocked-2", "task-123"),
                ]
            },
        )

        with patch(
            "sibyl_core.tasks.dependencies._get_graph_managers",
            return_value=(entity_manager, relationship_manager),
        ):
            result = await get_blocking_tasks(MagicMock(), "task-123", TEST_ORG_ID)

        assert result.dependencies == ["blocked-1", "blocked-2"]
        assert result.blockers == ["blocked-1"]
        relationship_manager.get_for_entity.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_returns_dependent_tasks(self) -> None:
        """Should return tasks that depend on the given task."""
        entity_manager, relationship_manager = _make_dependency_managers(
            entity_lookup={
                "dependent-1": _make_task("dependent-1", status=TaskStatus.TODO),
                "dependent-2": _make_task("dependent-2", status=TaskStatus.DOING),
            },
            relationships_by_entity={
                ("task-123", "incoming"): [
                    _make_relationship("dependent-1", "task-123"),
                    _make_relationship("dependent-2", "task-123"),
                ]
            },
        )

        with patch(
            "sibyl_core.tasks.dependencies._get_graph_managers",
            return_value=(entity_manager, relationship_manager),
        ):
            result = await get_blocking_tasks(MagicMock(), "task-123", TEST_ORG_ID)

        assert result.task_id == "task-123"
        assert len(result.dependencies) == 2
        assert "dependent-1" in result.dependencies
        assert "dependent-2" in result.dependencies

    @pytest.mark.asyncio
    async def test_identifies_incomplete_dependents(self) -> None:
        """Should identify incomplete dependents as blockers."""
        entity_manager, relationship_manager = _make_dependency_managers(
            entity_lookup={
                "dep-1": _make_task("dep-1", status=TaskStatus.TODO),
                "dep-2": _make_task("dep-2", status=TaskStatus.DONE),
            },
            relationships_by_entity={
                ("task-123", "incoming"): [
                    _make_relationship("dep-1", "task-123"),
                    _make_relationship("dep-2", "task-123"),
                ]
            },
        )

        with patch(
            "sibyl_core.tasks.dependencies._get_graph_managers",
            return_value=(entity_manager, relationship_manager),
        ):
            result = await get_blocking_tasks(MagicMock(), "task-123", TEST_ORG_ID)

        assert len(result.blockers) == 1
        assert "dep-1" in result.blockers

    @pytest.mark.asyncio
    async def test_handles_dict_records(self) -> None:
        """Should handle task-like objects with string statuses."""
        entity_manager, relationship_manager = _make_dependency_managers(
            entity_lookup={
                "dependent-1": _make_task("dependent-1", status=TaskStatus.TODO.value),
            },
            relationships_by_entity={
                ("task-123", "incoming"): [
                    _make_relationship("dependent-1", "task-123"),
                ]
            },
        )

        with patch(
            "sibyl_core.tasks.dependencies._get_graph_managers",
            return_value=(entity_manager, relationship_manager),
        ):
            result = await get_blocking_tasks(MagicMock(), "task-123", TEST_ORG_ID)

        assert "dependent-1" in result.dependencies

    @pytest.mark.asyncio
    async def test_clamps_depth(self) -> None:
        """Should clamp depth to 1-5 range."""
        entity_manager, relationship_manager = _make_dependency_managers()

        with patch(
            "sibyl_core.tasks.dependencies._get_graph_managers",
            return_value=(entity_manager, relationship_manager),
        ):
            result = await get_blocking_tasks(MagicMock(), "task-123", TEST_ORG_ID, depth=10)

        assert result.depth == 5

    @pytest.mark.asyncio
    async def test_handles_query_exception(self) -> None:
        """Should return empty result on query failure."""
        with patch(
            "sibyl_core.tasks.dependencies._get_graph_managers",
            side_effect=RuntimeError("Connection failed"),
        ):
            result = await get_blocking_tasks(MagicMock(), "task-123", TEST_ORG_ID)

        assert result.dependencies == []
        assert result.blockers == []


class TestDetectDependencyCycles:
    """Tests for detect_dependency_cycles function."""

    @pytest.mark.asyncio
    async def test_surreal_runtime_uses_graph_managers(self) -> None:
        """Should build the cycle graph from manager-backed relationships."""
        entity_manager, relationship_manager = _make_dependency_managers(
            tasks=[
                _make_task("task-a"),
                _make_task("task-b"),
                _make_task("task-c"),
            ],
            all_relationships=[
                _make_relationship("task-a", "task-b"),
                _make_relationship("task-b", "task-c"),
                _make_relationship("task-c", "task-a"),
            ],
        )

        with patch(
            "sibyl_core.tasks.dependencies._get_graph_managers",
            return_value=(entity_manager, relationship_manager),
        ):
            result = await detect_dependency_cycles(MagicMock(), TEST_ORG_ID)

        assert result.has_cycles is True
        relationship_manager.list_all.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_no_cycles_detected(self) -> None:
        """Should detect no cycles in acyclic graph."""
        entity_manager, relationship_manager = _make_dependency_managers(
            tasks=[
                _make_task("task-a"),
                _make_task("task-b"),
                _make_task("task-c"),
            ],
            all_relationships=[
                _make_relationship("task-a", "task-b"),
                _make_relationship("task-b", "task-c"),
            ],
        )

        with patch(
            "sibyl_core.tasks.dependencies._get_graph_managers",
            return_value=(entity_manager, relationship_manager),
        ):
            result = await detect_dependency_cycles(MagicMock(), TEST_ORG_ID)

        assert result.has_cycles is False
        assert result.cycles == []
        assert "No cycles" in result.message

    @pytest.mark.asyncio
    async def test_detects_simple_cycle(self) -> None:
        """Should detect a simple cycle."""
        entity_manager, relationship_manager = _make_dependency_managers(
            tasks=[
                _make_task("task-a"),
                _make_task("task-b"),
                _make_task("task-c"),
            ],
            all_relationships=[
                _make_relationship("task-a", "task-b"),
                _make_relationship("task-b", "task-c"),
                _make_relationship("task-c", "task-a"),
            ],
        )

        with patch(
            "sibyl_core.tasks.dependencies._get_graph_managers",
            return_value=(entity_manager, relationship_manager),
        ):
            result = await detect_dependency_cycles(MagicMock(), TEST_ORG_ID)

        assert result.has_cycles is True
        assert len(result.cycles) >= 1
        assert "Found" in result.message

    @pytest.mark.asyncio
    async def test_detects_self_cycle(self) -> None:
        """Should detect a task depending on itself."""
        entity_manager, relationship_manager = _make_dependency_managers(
            tasks=[_make_task("task-a")],
            all_relationships=[_make_relationship("task-a", "task-a")],
        )

        with patch(
            "sibyl_core.tasks.dependencies._get_graph_managers",
            return_value=(entity_manager, relationship_manager),
        ):
            result = await detect_dependency_cycles(MagicMock(), TEST_ORG_ID)

        assert result.has_cycles is True

    @pytest.mark.asyncio
    async def test_project_scoped_query(self) -> None:
        """Should pass project scope through the entity seam when project_id provided."""
        entity_manager, relationship_manager = _make_dependency_managers()

        with patch(
            "sibyl_core.tasks.dependencies._get_graph_managers",
            return_value=(entity_manager, relationship_manager),
        ):
            await detect_dependency_cycles(MagicMock(), TEST_ORG_ID, project_id="proj-123")

        entity_manager.list_by_type.assert_awaited_once_with(
            EntityType.TASK,
            project_id="proj-123",
            limit=500,
            offset=0,
            include_archived=True,
        )

    @pytest.mark.asyncio
    async def test_handles_dict_records(self) -> None:
        """Should handle plain manager relationship objects."""
        entity_manager, relationship_manager = _make_dependency_managers(
            tasks=[_make_task("task-a"), _make_task("task-b")],
            all_relationships=[_make_relationship("task-a", "task-b")],
        )

        with patch(
            "sibyl_core.tasks.dependencies._get_graph_managers",
            return_value=(entity_manager, relationship_manager),
        ):
            result = await detect_dependency_cycles(MagicMock(), TEST_ORG_ID)

        assert result.has_cycles is False

    @pytest.mark.asyncio
    async def test_handles_empty_graph(self) -> None:
        """Should handle empty dependency graph."""
        entity_manager, relationship_manager = _make_dependency_managers()

        with patch(
            "sibyl_core.tasks.dependencies._get_graph_managers",
            return_value=(entity_manager, relationship_manager),
        ):
            result = await detect_dependency_cycles(MagicMock(), TEST_ORG_ID)

        assert result.has_cycles is False
        assert result.cycles == []

    @pytest.mark.asyncio
    async def test_handles_query_exception(self) -> None:
        """Should return safe result on query failure."""
        with patch(
            "sibyl_core.tasks.dependencies._get_graph_managers",
            side_effect=RuntimeError("Connection failed"),
        ):
            result = await detect_dependency_cycles(MagicMock(), TEST_ORG_ID)

        assert result.has_cycles is False
        assert "failed" in result.message.lower()

    @pytest.mark.asyncio
    async def test_skips_none_ids(self) -> None:
        """Should skip records with None IDs."""
        entity_manager, relationship_manager = _make_dependency_managers(
            tasks=[_make_task("task-a"), _make_task("task-b")],
            all_relationships=[
                _make_relationship(None, "task-b"),
                _make_relationship("task-a", None),
                _make_relationship("task-a", "task-b"),
            ],
        )

        with patch(
            "sibyl_core.tasks.dependencies._get_graph_managers",
            return_value=(entity_manager, relationship_manager),
        ):
            result = await detect_dependency_cycles(MagicMock(), TEST_ORG_ID)

        # Should only process the valid edge
        assert result.has_cycles is False


class TestSuggestTaskOrder:
    """Tests for suggest_task_order function."""

    @pytest.mark.asyncio
    async def test_surreal_runtime_uses_graph_managers(self) -> None:
        """Should derive order from manager-backed tasks and relationships."""
        entity_manager, relationship_manager = _make_dependency_managers(
            tasks=[
                _make_task("task-1", task_order=10),
                _make_task("task-2", task_order=20),
                _make_task("task-3", task_order=30),
            ],
            all_relationships=[
                _make_relationship("task-2", "task-1"),
                _make_relationship("task-3", "task-2"),
            ],
        )

        with patch(
            "sibyl_core.tasks.dependencies._get_graph_managers",
            return_value=(entity_manager, relationship_manager),
        ):
            result = await suggest_task_order(MagicMock(), TEST_ORG_ID)

        assert result.ordered_tasks == ["task-1", "task-2", "task-3"]
        relationship_manager.list_all.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_orders_simple_chain(self) -> None:
        """Should order tasks in dependency order."""
        entity_manager, relationship_manager = _make_dependency_managers(
            tasks=[
                _make_task("task-1", task_order=10),
                _make_task("task-2", task_order=20),
                _make_task("task-3", task_order=30),
            ],
            all_relationships=[
                _make_relationship("task-2", "task-1"),
                _make_relationship("task-3", "task-2"),
            ],
        )

        with patch(
            "sibyl_core.tasks.dependencies._get_graph_managers",
            return_value=(entity_manager, relationship_manager),
        ):
            result = await suggest_task_order(MagicMock(), TEST_ORG_ID)

        assert len(result.ordered_tasks) == 3
        # task-1 should come before task-2
        assert result.ordered_tasks.index("task-1") < result.ordered_tasks.index("task-2")
        # task-2 should come before task-3
        assert result.ordered_tasks.index("task-2") < result.ordered_tasks.index("task-3")

    @pytest.mark.asyncio
    async def test_handles_independent_tasks(self) -> None:
        """Should order independent tasks by priority."""
        entity_manager, relationship_manager = _make_dependency_managers(
            tasks=[
                _make_task("task-low", task_order=10),
                _make_task("task-high", task_order=100),
                _make_task("task-med", task_order=50),
            ],
        )

        with patch(
            "sibyl_core.tasks.dependencies._get_graph_managers",
            return_value=(entity_manager, relationship_manager),
        ):
            result = await suggest_task_order(MagicMock(), TEST_ORG_ID)

        assert len(result.ordered_tasks) == 3
        # Highest priority should come first
        assert result.ordered_tasks[0] == "task-high"

    @pytest.mark.asyncio
    async def test_identifies_cycle_tasks(self) -> None:
        """Should identify tasks in cycles as unordered."""
        entity_manager, relationship_manager = _make_dependency_managers(
            tasks=[
                _make_task("task-ok", task_order=10),
                _make_task("task-a", task_order=10),
                _make_task("task-b", task_order=10),
            ],
            all_relationships=[
                _make_relationship("task-a", "task-b"),
                _make_relationship("task-b", "task-a"),
            ],
        )

        with patch(
            "sibyl_core.tasks.dependencies._get_graph_managers",
            return_value=(entity_manager, relationship_manager),
        ):
            result = await suggest_task_order(MagicMock(), TEST_ORG_ID)

        # task-ok should be ordered, cycle tasks should be unordered
        assert "task-ok" in result.ordered_tasks
        assert len(result.unordered_tasks) == 2
        assert "task-a" in result.unordered_tasks
        assert "task-b" in result.unordered_tasks
        assert len(result.warnings) >= 1

    @pytest.mark.asyncio
    async def test_status_filter(self) -> None:
        """Should filter tasks by status."""
        entity_manager, relationship_manager = _make_dependency_managers(
            tasks=[
                _make_task("task-todo", status=TaskStatus.TODO, task_order=10),
                _make_task("task-done", status=TaskStatus.DONE, task_order=10),
                _make_task("task-doing", status=TaskStatus.DOING, task_order=10),
            ],
        )

        with patch(
            "sibyl_core.tasks.dependencies._get_graph_managers",
            return_value=(entity_manager, relationship_manager),
        ):
            result = await suggest_task_order(
                MagicMock(),
                TEST_ORG_ID,
                status_filter=[TaskStatus.TODO, TaskStatus.DOING],
            )

        assert "task-todo" in result.ordered_tasks
        assert "task-doing" in result.ordered_tasks
        assert "task-done" not in result.ordered_tasks

    @pytest.mark.asyncio
    async def test_project_scoped_query(self) -> None:
        """Should pass project scope through the task seam when project_id provided."""
        entity_manager, relationship_manager = _make_dependency_managers()

        with patch(
            "sibyl_core.tasks.dependencies._get_graph_managers",
            return_value=(entity_manager, relationship_manager),
        ):
            await suggest_task_order(MagicMock(), TEST_ORG_ID, project_id="proj-123")

        entity_manager.list_by_type.assert_awaited_once_with(
            EntityType.TASK,
            project_id="proj-123",
            limit=500,
            offset=0,
            include_archived=True,
        )

    @pytest.mark.asyncio
    async def test_handles_dict_records(self) -> None:
        """Should handle task-like objects with string statuses and priorities."""
        entity_manager, relationship_manager = _make_dependency_managers(
            tasks=[_make_task("task-1", status=TaskStatus.TODO.value, task_order=10)],
        )

        with patch(
            "sibyl_core.tasks.dependencies._get_graph_managers",
            return_value=(entity_manager, relationship_manager),
        ):
            result = await suggest_task_order(MagicMock(), TEST_ORG_ID)

        assert "task-1" in result.ordered_tasks

    @pytest.mark.asyncio
    async def test_handles_empty_project(self) -> None:
        """Should handle project with no tasks."""
        entity_manager, relationship_manager = _make_dependency_managers()

        with patch(
            "sibyl_core.tasks.dependencies._get_graph_managers",
            return_value=(entity_manager, relationship_manager),
        ):
            result = await suggest_task_order(MagicMock(), TEST_ORG_ID)

        assert result.ordered_tasks == []
        assert result.unordered_tasks == []

    @pytest.mark.asyncio
    async def test_handles_query_exception(self) -> None:
        """Should return safe result on query failure."""
        with patch(
            "sibyl_core.tasks.dependencies._get_graph_managers",
            side_effect=RuntimeError("Connection failed"),
        ):
            result = await suggest_task_order(MagicMock(), TEST_ORG_ID)

        assert result.ordered_tasks == []
        assert "failed" in result.warnings[0].lower()

    @pytest.mark.asyncio
    async def test_ignores_external_dependencies(self) -> None:
        """Should ignore dependencies to tasks not in the task set."""
        entity_manager, relationship_manager = _make_dependency_managers(
            tasks=[
                _make_task("task-1", task_order=10),
                _make_task("task-2", task_order=20),
            ],
            all_relationships=[
                _make_relationship("task-2", "task-external"),
            ],
        )

        with patch(
            "sibyl_core.tasks.dependencies._get_graph_managers",
            return_value=(entity_manager, relationship_manager),
        ):
            result = await suggest_task_order(MagicMock(), TEST_ORG_ID)

        # Both tasks should be ordered (external dep ignored)
        assert len(result.ordered_tasks) == 2

    @pytest.mark.asyncio
    async def test_handles_none_priority(self) -> None:
        """Should handle tasks with None priority."""
        entity_manager, relationship_manager = _make_dependency_managers(
            tasks=[
                _make_task("task-1", task_order=None),
                _make_task("task-2", task_order=50),
            ],
        )

        with patch(
            "sibyl_core.tasks.dependencies._get_graph_managers",
            return_value=(entity_manager, relationship_manager),
        ):
            result = await suggest_task_order(MagicMock(), TEST_ORG_ID)

        assert len(result.ordered_tasks) == 2
