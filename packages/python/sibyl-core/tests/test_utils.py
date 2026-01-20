"""Tests for sibyl-core utilities.

Covers:
- utils/resilience.py - Retry decorators, timeout handling
- tools/helpers.py - String utilities, formatting, auto-tagging
- tasks/dependencies.py - DAG operations, cycle detection (mocked)
"""

import asyncio
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from sibyl_core.tools.helpers import (
    MAX_CONTENT_LENGTH,
    MAX_TITLE_LENGTH,
    _build_entity_metadata,
    _generate_id,
    _get_field,
    _serialize_enum,
    auto_tag_task,
)
from sibyl_core.utils.resilience import (
    GRAPH_RETRY,
    RetryConfig,
    calculate_delay,
    retry,
    timeout,
    with_timeout,
)


# =============================================================================
# Resilience Tests
# =============================================================================
class TestRetryConfig:
    """Test RetryConfig initialization and defaults."""

    def test_default_values(self) -> None:
        """RetryConfig has sensible defaults."""
        config = RetryConfig()
        assert config.max_attempts == 3
        assert config.base_delay == 0.5
        assert config.max_delay == 10.0
        assert config.exponential_base == 2.0
        assert config.jitter is True
        assert ConnectionError in config.retryable_exceptions
        assert TimeoutError in config.retryable_exceptions

    def test_custom_values(self) -> None:
        """RetryConfig accepts custom values."""
        config = RetryConfig(
            max_attempts=5,
            base_delay=1.0,
            max_delay=30.0,
            exponential_base=3.0,
            jitter=False,
            retryable_exceptions=(ValueError, KeyError),
        )
        assert config.max_attempts == 5
        assert config.base_delay == 1.0
        assert config.max_delay == 30.0
        assert config.exponential_base == 3.0
        assert config.jitter is False
        assert config.retryable_exceptions == (ValueError, KeyError)

    def test_graph_retry_preset(self) -> None:
        """GRAPH_RETRY preset has expected configuration."""
        assert GRAPH_RETRY.max_attempts == 3
        assert GRAPH_RETRY.base_delay == 1.0  # Longer delay for graph ops under load
        assert GRAPH_RETRY.max_delay == 10.0  # More time between retries


class TestCalculateDelay:
    """Test exponential backoff delay calculation."""

    def test_first_attempt_delay(self) -> None:
        """First attempt (0) uses base delay."""
        config = RetryConfig(base_delay=1.0, jitter=False)
        delay = calculate_delay(0, config)
        assert delay == 1.0

    def test_exponential_increase(self) -> None:
        """Delay increases exponentially with attempt number."""
        config = RetryConfig(base_delay=1.0, exponential_base=2.0, jitter=False, max_delay=100.0)
        assert calculate_delay(0, config) == 1.0
        assert calculate_delay(1, config) == 2.0
        assert calculate_delay(2, config) == 4.0
        assert calculate_delay(3, config) == 8.0

    def test_max_delay_cap(self) -> None:
        """Delay is capped at max_delay."""
        config = RetryConfig(base_delay=1.0, exponential_base=2.0, max_delay=5.0, jitter=False)
        assert calculate_delay(10, config) == 5.0

    def test_jitter_adds_variance(self) -> None:
        """Jitter adds variance to delay (within bounds)."""
        config = RetryConfig(base_delay=4.0, jitter=True, max_delay=100.0)
        delays = [calculate_delay(0, config) for _ in range(20)]
        # With 25% jitter, values should be in range [3.0, 5.0]
        assert all(3.0 <= d <= 5.0 for d in delays)
        # Delays should have variance (not all identical)
        assert len(set(delays)) > 1

    def test_delay_never_negative(self) -> None:
        """Delay is never negative, even with jitter."""
        config = RetryConfig(base_delay=0.1, jitter=True)
        delays = [calculate_delay(0, config) for _ in range(100)]
        assert all(d >= 0 for d in delays)


class TestRetryDecorator:
    """Test retry decorator for async functions."""

    @pytest.mark.asyncio
    async def test_retry_success_first_try(self) -> None:
        """Function succeeds on first try, no retry needed."""
        call_count = 0

        @retry(config=RetryConfig(max_attempts=3))
        async def succeeds() -> str:
            nonlocal call_count
            call_count += 1
            return "success"

        result = await succeeds()
        assert result == "success"
        assert call_count == 1

    @pytest.mark.asyncio
    async def test_retry_success_after_failures(self) -> None:
        """Function retries then succeeds."""
        call_count = 0
        config = RetryConfig(max_attempts=3, base_delay=0.01, jitter=False)

        @retry(config=config)
        async def fails_twice() -> str:
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise ConnectionError("Network failure")
            return "success"

        result = await fails_twice()
        assert result == "success"
        assert call_count == 3

    @pytest.mark.asyncio
    async def test_retry_max_attempts_exceeded(self) -> None:
        """Raises after max attempts exhausted."""
        config = RetryConfig(max_attempts=2, base_delay=0.01, jitter=False)

        @retry(config=config)
        async def always_fails() -> str:
            raise ConnectionError("Always fails")

        with pytest.raises(ConnectionError, match="Always fails"):
            await always_fails()

    @pytest.mark.asyncio
    async def test_retry_exponential_backoff(self) -> None:
        """Delays increase exponentially between retries."""
        delays: list[float] = []
        config = RetryConfig(
            max_attempts=4, base_delay=0.1, exponential_base=2.0, jitter=False, max_delay=10.0
        )

        @retry(config=config)
        async def track_delays() -> str:
            raise ConnectionError("fail")

        original_sleep = asyncio.sleep

        async def mock_sleep(delay: float) -> None:
            delays.append(delay)
            await original_sleep(0.001)  # Minimal actual delay

        with (
            patch("sibyl_core.utils.resilience.asyncio.sleep", mock_sleep),
            pytest.raises(ConnectionError),
        ):
            await track_delays()

        # Should have 3 delays (attempts 1->2, 2->3, 3->4)
        assert len(delays) == 3
        assert delays[0] == pytest.approx(0.1)  # base_delay * 2^0
        assert delays[1] == pytest.approx(0.2)  # base_delay * 2^1
        assert delays[2] == pytest.approx(0.4)  # base_delay * 2^2

    @pytest.mark.asyncio
    async def test_retry_specific_exceptions(self) -> None:
        """Only retries specified exception types."""
        call_count = 0
        config = RetryConfig(
            max_attempts=3, base_delay=0.01, retryable_exceptions=(ConnectionError,)
        )

        @retry(config=config)
        async def raises_value_error() -> str:
            nonlocal call_count
            call_count += 1
            raise ValueError("Not retryable")

        # ValueError is not in retryable_exceptions, should not retry
        with pytest.raises(ValueError, match="Not retryable"):
            await raises_value_error()

        assert call_count == 1  # No retries

    @pytest.mark.asyncio
    async def test_retry_on_retry_callback(self) -> None:
        """on_retry callback is called for each retry."""
        retry_events: list[tuple[int, Exception]] = []

        def on_retry(attempt: int, exc: Exception) -> None:
            retry_events.append((attempt, exc))

        config = RetryConfig(max_attempts=3, base_delay=0.01, jitter=False)

        @retry(config=config, on_retry=on_retry)
        async def fails_twice() -> str:
            if len(retry_events) < 2:
                raise ConnectionError("temporary")
            return "success"

        result = await fails_twice()
        assert result == "success"
        assert len(retry_events) == 2
        assert retry_events[0][0] == 1
        assert retry_events[1][0] == 2

    @pytest.mark.asyncio
    async def test_retry_preserves_function_metadata(self) -> None:
        """Decorated function preserves __name__ and __doc__."""
        config = RetryConfig()

        @retry(config=config)
        async def documented_function() -> str:
            """This is a docstring."""
            return "ok"

        assert documented_function.__name__ == "documented_function"
        assert documented_function.__doc__ == "This is a docstring."


class TestTimeoutDecorator:
    """Test timeout decorator for async functions."""

    @pytest.mark.asyncio
    async def test_timeout_completes_in_time(self) -> None:
        """Function that completes within timeout succeeds."""

        @timeout(1.0)
        async def fast_function() -> str:
            return "fast"

        result = await fast_function()
        assert result == "fast"

    @pytest.mark.asyncio
    async def test_timeout_exceeds_limit(self) -> None:
        """Function that exceeds timeout raises TimeoutError."""

        @timeout(0.05, operation_name="slow_op")
        async def slow_function() -> str:
            await asyncio.sleep(1.0)
            return "slow"

        with pytest.raises(TimeoutError, match="slow_op timed out"):
            await slow_function()

    @pytest.mark.asyncio
    async def test_timeout_uses_function_name_as_default(self) -> None:
        """Operation name defaults to function name."""

        @timeout(0.05)
        async def my_slow_function() -> str:
            await asyncio.sleep(1.0)
            return "slow"

        with pytest.raises(TimeoutError, match="my_slow_function timed out"):
            await my_slow_function()


class TestWithTimeout:
    """Test with_timeout helper function."""

    @pytest.mark.asyncio
    async def test_with_timeout_success(self) -> None:
        """Coroutine completes within timeout."""

        async def fast_coro() -> str:
            return "done"

        result = await with_timeout(fast_coro(), 1.0, "fast_op")
        assert result == "done"

    @pytest.mark.asyncio
    async def test_with_timeout_exceeds(self) -> None:
        """Coroutine exceeds timeout."""

        async def slow_coro() -> str:
            await asyncio.sleep(1.0)
            return "never"

        with pytest.raises(TimeoutError, match=r"slow_op timed out after 0\.05s"):
            await with_timeout(slow_coro(), 0.05, "slow_op")


# =============================================================================
# Helpers Tests
# =============================================================================
class TestGetField:
    """Test _get_field helper for entity field access."""

    def test_get_direct_attribute(self) -> None:
        """Returns direct attribute value."""

        class Entity:
            def __init__(self) -> None:
                self.name = "test"
                self.metadata: dict[str, Any] = {}

        assert _get_field(Entity(), "name") == "test"

    def test_get_from_metadata(self) -> None:
        """Falls back to metadata when attribute is None."""

        class Entity:
            def __init__(self) -> None:
                self.name = None
                self.metadata = {"name": "from_metadata"}

        assert _get_field(Entity(), "name") == "from_metadata"

    def test_get_default_when_missing(self) -> None:
        """Returns default when field not found."""

        class Entity:
            def __init__(self) -> None:
                self.metadata: dict[str, Any] = {}

        assert _get_field(Entity(), "missing", "default") == "default"

    def test_get_none_returns_default(self) -> None:
        """Returns default when both attribute and metadata are None."""

        class Entity:
            def __init__(self) -> None:
                self.value = None
                self.metadata = {"value": None}

        assert _get_field(Entity(), "value", "fallback") == "fallback"


class TestSerializeEnum:
    """Test _serialize_enum helper."""

    def test_serialize_enum_value(self) -> None:
        """Extracts .value from enum."""
        from sibyl_core.models.tasks import TaskStatus

        assert _serialize_enum(TaskStatus.DONE) == "done"

    def test_serialize_non_enum(self) -> None:
        """Returns non-enum values as-is."""
        assert _serialize_enum("string") == "string"
        assert _serialize_enum(42) == 42

    def test_serialize_none(self) -> None:
        """Returns None as None."""
        assert _serialize_enum(None) is None


class TestGenerateId:
    """Test _generate_id for deterministic ID generation."""

    def test_generate_id_basic(self) -> None:
        """Generates ID with prefix and hash."""
        id1 = _generate_id("task", "project_1", "My Task")
        assert id1.startswith("task_")
        assert len(id1) == 5 + 12  # "task_" + 12 hex chars

    def test_generate_id_deterministic(self) -> None:
        """Same inputs produce same ID."""
        id1 = _generate_id("pattern", "foo", "bar")
        id2 = _generate_id("pattern", "foo", "bar")
        assert id1 == id2

    def test_generate_id_different_inputs(self) -> None:
        """Different inputs produce different IDs."""
        id1 = _generate_id("task", "a", "b")
        id2 = _generate_id("task", "a", "c")
        assert id1 != id2

    def test_generate_id_truncates_long_parts(self) -> None:
        """Long input parts are truncated to 100 chars."""
        long_part = "x" * 200
        id1 = _generate_id("task", long_part)
        id2 = _generate_id("task", "x" * 100)
        # Truncated to 100, so should match
        assert id1 == id2


class TestBuildEntityMetadata:
    """Test _build_entity_metadata helper."""

    def test_build_metadata_basic(self) -> None:
        """Builds metadata dict from entity."""
        from sibyl_core.models.tasks import TaskPriority, TaskStatus

        class MockEntity:
            def __init__(self) -> None:
                self.status = TaskStatus.DOING
                self.priority = TaskPriority.HIGH
                self.category = "backend"
                self.languages = None
                self.severity = None
                self.template_type = None
                self.project_id = "proj_123"
                self.assignees = ["alice"]
                self.metadata: dict[str, Any] = {}

        meta = _build_entity_metadata(MockEntity())
        assert meta["status"] == "doing"
        assert meta["priority"] == "high"
        assert meta["category"] == "backend"
        assert meta["project_id"] == "proj_123"
        assert meta["assignees"] == ["alice"]
        # None values are excluded
        assert "languages" not in meta
        assert "severity" not in meta

    def test_build_metadata_merges_existing(self) -> None:
        """Merges with existing entity.metadata."""

        class MockEntity:
            def __init__(self) -> None:
                self.status = None
                self.priority = None
                self.category = None
                self.languages = None
                self.severity = None
                self.template_type = None
                self.project_id = None
                self.assignees = None
                self.metadata = {"custom_field": "custom_value"}

        meta = _build_entity_metadata(MockEntity())
        assert meta["custom_field"] == "custom_value"


class TestAutoTagTask:
    """Test auto_tag_task for automatic tag generation."""

    def test_auto_tag_basic_frontend(self) -> None:
        """Detects frontend keywords."""
        tags = auto_tag_task(
            title="Build React component",
            description="Create a new button component with animations",
        )
        assert "frontend" in tags

    def test_auto_tag_basic_backend(self) -> None:
        """Detects backend keywords."""
        tags = auto_tag_task(
            title="Add API endpoint",
            description="Create REST endpoint for user authentication",
        )
        assert "backend" in tags

    def test_auto_tag_from_technologies(self) -> None:
        """Adds tags from technologies list."""
        tags = auto_tag_task(
            title="Setup project",
            description="Configure the codebase",
            technologies=["python", "fastapi", "postgres"],
        )
        assert "python" in tags
        assert "backend" in tags  # fastapi maps to backend
        assert "database" in tags  # postgres maps to database

    def test_auto_tag_explicit_tags_preserved(self) -> None:
        """Explicit tags are included and normalized."""
        tags = auto_tag_task(
            title="Generic task",
            description="No keywords here",
            explicit_tags=["MyCustomTag", "  spaced  "],
        )
        assert "mycustomtag" in tags
        assert "spaced" in tags

    def test_auto_tag_domain_added(self) -> None:
        """Domain parameter becomes a tag."""
        tags = auto_tag_task(
            title="Task",
            description="Description",
            domain="Infrastructure",
        )
        assert "infrastructure" in tags

    def test_auto_tag_type_detection_feature(self) -> None:
        """Detects 'feature' type from keywords."""
        tags = auto_tag_task(
            title="Add new dashboard",
            description="Implement a new analytics dashboard",
        )
        assert "feature" in tags

    def test_auto_tag_type_detection_bug(self) -> None:
        """Detects 'bug' type from keywords."""
        tags = auto_tag_task(
            title="Fix login crash",
            description="Application crashes when logging in",
        )
        assert "bug" in tags

    def test_auto_tag_type_detection_refactor(self) -> None:
        """Detects 'refactor' type from keywords."""
        tags = auto_tag_task(
            title="Refactor authentication module",
            description="Clean up and simplify the auth code",
        )
        assert "refactor" in tags

    def test_auto_tag_prefers_project_tags(self) -> None:
        """Uses existing project tags when they appear in text."""
        tags = auto_tag_task(
            title="Update auth flow",
            description="Change the auth process",
            project_tags=["auth", "security"],
        )
        # Should include existing project tag that appears in text
        assert "auth" in tags

    def test_auto_tag_empty_inputs(self) -> None:
        """Handles empty inputs gracefully."""
        tags = auto_tag_task(title="", description="")
        assert isinstance(tags, list)

    def test_auto_tag_filters_short_tags(self) -> None:
        """Filters out tags shorter than 2 chars."""
        tags = auto_tag_task(
            title="X Y Z task",
            description="A B C",
            explicit_tags=["a", "ab", "abc"],
        )
        assert "a" not in tags
        assert "ab" in tags
        assert "abc" in tags

    def test_auto_tag_sorted(self) -> None:
        """Tags are returned sorted."""
        tags = auto_tag_task(
            title="Add test endpoint",
            description="Create backend API test",
            explicit_tags=["zebra", "alpha"],
        )
        assert tags == sorted(tags)

    def test_auto_tag_deduplicates(self) -> None:
        """Duplicate tags are removed."""
        tags = auto_tag_task(
            title="Backend API",
            description="Backend service",
            explicit_tags=["backend", "BACKEND"],
        )
        assert tags.count("backend") == 1


class TestValidationConstants:
    """Test validation constants are defined."""

    def test_max_title_length(self) -> None:
        """MAX_TITLE_LENGTH is reasonable."""
        assert MAX_TITLE_LENGTH == 200

    def test_max_content_length(self) -> None:
        """MAX_CONTENT_LENGTH is reasonable."""
        assert MAX_CONTENT_LENGTH == 50000


# =============================================================================
# Dependencies Tests (with mocked GraphClient)
# =============================================================================
class TestDependencyResult:
    """Test DependencyResult dataclass."""

    def test_dependency_result_creation(self) -> None:
        """DependencyResult can be instantiated."""
        from sibyl_core.tasks.dependencies import DependencyResult

        result = DependencyResult(
            task_id="task_1",
            dependencies=["task_2", "task_3"],
            blockers=["task_2"],
            depth=2,
        )
        assert result.task_id == "task_1"
        assert result.dependencies == ["task_2", "task_3"]
        assert result.blockers == ["task_2"]
        assert result.depth == 2


class TestCycleResult:
    """Test CycleResult dataclass."""

    def test_cycle_result_no_cycles(self) -> None:
        """CycleResult with no cycles."""
        from sibyl_core.tasks.dependencies import CycleResult

        result = CycleResult(has_cycles=False, cycles=[], message="No cycles")
        assert result.has_cycles is False
        assert result.cycles == []

    def test_cycle_result_with_cycles(self) -> None:
        """CycleResult with detected cycles."""
        from sibyl_core.tasks.dependencies import CycleResult

        result = CycleResult(
            has_cycles=True,
            cycles=[["A", "B", "C", "A"]],
            message="Found 1 cycle(s)",
        )
        assert result.has_cycles is True
        assert len(result.cycles) == 1


class TestTaskOrderResult:
    """Test TaskOrderResult dataclass."""

    def test_task_order_result(self) -> None:
        """TaskOrderResult can be instantiated."""
        from sibyl_core.tasks.dependencies import TaskOrderResult

        result = TaskOrderResult(
            ordered_tasks=["task_1", "task_2", "task_3"],
            unordered_tasks=["task_4"],
            warnings=["1 task in cycle"],
        )
        assert result.ordered_tasks == ["task_1", "task_2", "task_3"]
        assert result.unordered_tasks == ["task_4"]
        assert "1 task in cycle" in result.warnings[0]


class TestGetTaskDependencies:
    """Test get_task_dependencies with mocked client."""

    @pytest.mark.asyncio
    async def test_get_task_dependencies_direct(self) -> None:
        """Gets direct dependencies for a task."""
        from sibyl_core.tasks.dependencies import get_task_dependencies

        mock_client = AsyncMock()
        mock_client.execute_read_org = AsyncMock(
            return_value=[
                ["dep_1", "todo"],
                ["dep_2", "done"],
            ]
        )

        result = await get_task_dependencies(
            client=mock_client,
            task_id="task_1",
            organization_id="org_1",
            depth=1,
        )

        assert result.task_id == "task_1"
        assert "dep_1" in result.dependencies
        assert "dep_2" in result.dependencies
        assert "dep_1" in result.blockers  # todo status is blocking
        assert "dep_2" not in result.blockers  # done status is not blocking

    @pytest.mark.asyncio
    async def test_get_task_dependencies_error_handling(self) -> None:
        """Returns empty result on error."""
        from sibyl_core.tasks.dependencies import get_task_dependencies

        mock_client = AsyncMock()
        mock_client.execute_read_org = AsyncMock(side_effect=Exception("DB error"))

        result = await get_task_dependencies(
            client=mock_client,
            task_id="task_1",
            organization_id="org_1",
        )

        assert result.dependencies == []
        assert result.blockers == []


class TestGetBlockingTasks:
    """Test get_blocking_tasks with mocked client."""

    @pytest.mark.asyncio
    async def test_get_blocking_tasks(self) -> None:
        """Gets tasks that are blocked by the given task."""
        from sibyl_core.tasks.dependencies import get_blocking_tasks

        mock_client = AsyncMock()
        mock_client.execute_read_org = AsyncMock(
            return_value=[
                ["blocked_1", "todo"],
                ["blocked_2", "doing"],
            ]
        )

        result = await get_blocking_tasks(
            client=mock_client,
            task_id="blocker_task",
            organization_id="org_1",
        )

        assert result.task_id == "blocker_task"
        assert "blocked_1" in result.dependencies
        assert "blocked_2" in result.dependencies

    @pytest.mark.asyncio
    async def test_get_blocking_tasks_error(self) -> None:
        """Returns empty result on error."""
        from sibyl_core.tasks.dependencies import get_blocking_tasks

        mock_client = AsyncMock()
        mock_client.execute_read_org = AsyncMock(side_effect=Exception("Network error"))

        result = await get_blocking_tasks(
            client=mock_client,
            task_id="task_1",
            organization_id="org_1",
        )

        assert result.dependencies == []


class TestDetectDependencyCycles:
    """Test detect_dependency_cycles with mocked client."""

    @pytest.mark.asyncio
    async def test_detect_cycles_none(self) -> None:
        """Detects no cycles in acyclic graph."""
        from sibyl_core.tasks.dependencies import detect_dependency_cycles

        mock_client = AsyncMock()
        # A -> B -> C (no cycle)
        mock_client.execute_read_org = AsyncMock(
            return_value=[
                ["A", "B"],
                ["B", "C"],
            ]
        )

        result = await detect_dependency_cycles(
            client=mock_client,
            organization_id="org_1",
        )

        assert result.has_cycles is False
        assert result.cycles == []

    @pytest.mark.asyncio
    async def test_detect_cycles_found(self) -> None:
        """Detects cycles in graph."""
        from sibyl_core.tasks.dependencies import detect_dependency_cycles

        mock_client = AsyncMock()
        # A -> B -> C -> A (cycle)
        mock_client.execute_read_org = AsyncMock(
            return_value=[
                ["A", "B"],
                ["B", "C"],
                ["C", "A"],
            ]
        )

        result = await detect_dependency_cycles(
            client=mock_client,
            organization_id="org_1",
        )

        assert result.has_cycles is True
        assert len(result.cycles) > 0

    @pytest.mark.asyncio
    async def test_detect_cycles_error(self) -> None:
        """Returns safe result on error."""
        from sibyl_core.tasks.dependencies import detect_dependency_cycles

        mock_client = AsyncMock()
        mock_client.execute_read_org = AsyncMock(side_effect=Exception("Query failed"))

        result = await detect_dependency_cycles(
            client=mock_client,
            organization_id="org_1",
        )

        assert result.has_cycles is False
        assert "failed" in result.message.lower()


class TestSuggestTaskOrder:
    """Test suggest_task_order (topological sort) with mocked client."""

    @pytest.mark.asyncio
    async def test_topological_sort_basic(self) -> None:
        """Sorts tasks in dependency order."""
        from sibyl_core.tasks.dependencies import suggest_task_order

        mock_client = AsyncMock()
        # Tasks
        mock_client.execute_read_org = AsyncMock(
            side_effect=[
                # First call: get tasks
                [["task_a", "todo", 1], ["task_b", "todo", 2], ["task_c", "todo", 3]],
                # Second call: get dependencies (B depends on A, C depends on B)
                [["task_b", "task_a"], ["task_c", "task_b"]],
            ]
        )

        result = await suggest_task_order(
            client=mock_client,
            organization_id="org_1",
        )

        # A should come before B, B before C
        assert result.ordered_tasks.index("task_a") < result.ordered_tasks.index("task_b")
        assert result.ordered_tasks.index("task_b") < result.ordered_tasks.index("task_c")
        assert result.unordered_tasks == []

    @pytest.mark.asyncio
    async def test_topological_sort_with_cycle(self) -> None:
        """Tasks in cycles are reported as unordered."""
        from sibyl_core.tasks.dependencies import suggest_task_order

        mock_client = AsyncMock()
        mock_client.execute_read_org = AsyncMock(
            side_effect=[
                # Tasks
                [["A", "todo", 0], ["B", "todo", 0], ["C", "todo", 0]],
                # Dependencies: A->B->C->A (cycle)
                [["A", "B"], ["B", "C"], ["C", "A"]],
            ]
        )

        result = await suggest_task_order(
            client=mock_client,
            organization_id="org_1",
        )

        # All tasks should be unordered due to cycle
        assert len(result.unordered_tasks) > 0
        assert len(result.warnings) > 0

    @pytest.mark.asyncio
    async def test_topological_sort_error(self) -> None:
        """Returns empty result on error."""
        from sibyl_core.tasks.dependencies import suggest_task_order

        mock_client = AsyncMock()
        mock_client.execute_read_org = AsyncMock(side_effect=Exception("DB error"))

        result = await suggest_task_order(
            client=mock_client,
            organization_id="org_1",
        )

        assert result.ordered_tasks == []
        assert "failed" in result.warnings[0].lower()

    @pytest.mark.asyncio
    async def test_topological_sort_independent_tasks(self) -> None:
        """Independent tasks can be in any order."""
        from sibyl_core.tasks.dependencies import suggest_task_order

        mock_client = AsyncMock()
        mock_client.execute_read_org = AsyncMock(
            side_effect=[
                # Tasks with no dependencies
                [["task_1", "todo", 0], ["task_2", "todo", 0], ["task_3", "todo", 0]],
                # No dependency edges
                [],
            ]
        )

        result = await suggest_task_order(
            client=mock_client,
            organization_id="org_1",
        )

        # All tasks should be ordered (no cycles)
        assert len(result.ordered_tasks) == 3
        assert result.unordered_tasks == []


class TestDepthClamping:
    """Test that depth is clamped to valid range."""

    @pytest.mark.asyncio
    async def test_depth_clamped_minimum(self) -> None:
        """Depth is clamped to minimum of 1."""
        from sibyl_core.tasks.dependencies import get_task_dependencies

        mock_client = AsyncMock()
        mock_client.execute_read_org = AsyncMock(return_value=[])

        result = await get_task_dependencies(
            client=mock_client,
            task_id="task_1",
            organization_id="org_1",
            depth=-5,
        )

        assert result.depth == 1

    @pytest.mark.asyncio
    async def test_depth_clamped_maximum(self) -> None:
        """Depth is clamped to maximum of 5 when include_transitive=True."""
        from sibyl_core.tasks.dependencies import get_task_dependencies

        mock_client = AsyncMock()
        mock_client.execute_read_org = AsyncMock(return_value=[])

        result = await get_task_dependencies(
            client=mock_client,
            task_id="task_1",
            organization_id="org_1",
            depth=100,
            include_transitive=True,  # Must be True to use depth > 1
        )

        assert result.depth == 5

    @pytest.mark.asyncio
    async def test_depth_ignored_without_transitive(self) -> None:
        """Depth is 1 when include_transitive=False, regardless of depth param."""
        from sibyl_core.tasks.dependencies import get_task_dependencies

        mock_client = AsyncMock()
        mock_client.execute_read_org = AsyncMock(return_value=[])

        result = await get_task_dependencies(
            client=mock_client,
            task_id="task_1",
            organization_id="org_1",
            depth=5,
            include_transitive=False,
        )

        assert result.depth == 1  # include_transitive=False forces depth to 1
