"""Tests for metrics endpoints and computation functions."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, call, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from sibyl.api.routes.metrics import (
    _compute_assignee_stats,
    _compute_priority_distribution,
    _compute_status_distribution,
    _compute_velocity_trend,
    _count_recent_tasks,
    _normalize_metric_task_row,
    _parse_iso_date,
)
from sibyl_core.models.entities import Entity, EntityType
from sibyl.auth.context import AuthContext
from sibyl_core.storage import Page

# =============================================================================
# Helper Function Tests
# =============================================================================


class TestParseIsoDate:
    """Tests for _parse_iso_date helper."""

    def test_valid_iso_date(self) -> None:
        """Parse valid ISO date string."""
        result = _parse_iso_date("2024-12-24T10:30:00")
        assert result is not None
        assert result.year == 2024
        assert result.month == 12
        assert result.day == 24

    def test_valid_iso_date_with_timezone(self) -> None:
        """Parse ISO date with timezone."""
        result = _parse_iso_date("2024-12-24T10:30:00+00:00")
        assert result is not None
        assert result.year == 2024

    def test_datetime_input(self) -> None:
        """Datetime input is normalized to UTC."""
        source = datetime(2024, 12, 24, 10, 30, 0, tzinfo=UTC)
        result = _parse_iso_date(source)
        assert result == source

    def test_none_input(self) -> None:
        """None input returns None."""
        assert _parse_iso_date(None) is None

    def test_empty_string(self) -> None:
        """Empty string returns None."""
        assert _parse_iso_date("") is None

    def test_invalid_format(self) -> None:
        """Invalid format returns None."""
        assert _parse_iso_date("not-a-date") is None
        assert _parse_iso_date("2024/12/24") is None


class TestComputeStatusDistribution:
    """Tests for _compute_status_distribution helper."""

    def test_empty_tasks(self) -> None:
        """Empty list returns all zeros."""
        result = _compute_status_distribution([])
        assert result.backlog == 0
        assert result.todo == 0
        assert result.doing == 0
        assert result.blocked == 0
        assert result.review == 0
        assert result.done == 0

    def test_single_status(self) -> None:
        """Count tasks with single status."""
        tasks = [
            {"metadata": {"status": "todo"}},
            {"metadata": {"status": "todo"}},
            {"metadata": {"status": "todo"}},
        ]
        result = _compute_status_distribution(tasks)
        assert result.todo == 3
        assert result.done == 0

    def test_mixed_statuses(self) -> None:
        """Count tasks with mixed statuses."""
        tasks = [
            {"metadata": {"status": "todo"}},
            {"metadata": {"status": "doing"}},
            {"metadata": {"status": "done"}},
            {"metadata": {"status": "done"}},
            {"metadata": {"status": "review"}},
        ]
        result = _compute_status_distribution(tasks)
        assert result.todo == 1
        assert result.doing == 1
        assert result.done == 2
        assert result.review == 1

    def test_missing_status_defaults_to_backlog(self) -> None:
        """Tasks without status default to backlog."""
        tasks = [
            {"metadata": {}},
            {"metadata": {"other_field": "value"}},
        ]
        result = _compute_status_distribution(tasks)
        assert result.backlog == 2

    def test_unknown_status_ignored(self) -> None:
        """Unknown status values are ignored."""
        tasks = [
            {"metadata": {"status": "unknown_status"}},
            {"metadata": {"status": "todo"}},
        ]
        result = _compute_status_distribution(tasks)
        assert result.todo == 1
        # unknown_status doesn't match any attribute, so only todo counted


class TestComputePriorityDistribution:
    """Tests for _compute_priority_distribution helper."""

    def test_empty_tasks(self) -> None:
        """Empty list returns all zeros."""
        result = _compute_priority_distribution([])
        assert result.critical == 0
        assert result.high == 0
        assert result.medium == 0
        assert result.low == 0
        assert result.someday == 0


class TestNormalizeMetricTaskRow:
    """Tests for raw metric-row normalization."""

    def test_prefers_metadata_values_over_top_level_duplicates(self) -> None:
        """Metadata remains the canonical source when both representations exist."""
        normalized = _normalize_metric_task_row(
            {
                "project_id": "proj_top_level",
                "status": "todo",
                "priority": "medium",
                "assignees": ["top-level"],
                "metadata": {
                    "project_id": "proj_meta",
                    "status": "doing",
                    "priority": "critical",
                    "assignees": ["meta"],
                },
            }
        )

        assert normalized["metadata"]["project_id"] == "proj_meta"
        assert normalized["metadata"]["status"] == "doing"
        assert normalized["metadata"]["priority"] == "critical"
        assert normalized["metadata"]["assignees"] == ["meta"]

    def test_falls_back_to_valid_metadata_datetime_when_top_level_is_malformed(self) -> None:
        """Malformed top-level timestamps should not hide valid metadata values."""
        valid_created_at = "2026-04-13T12:00:00+00:00"
        normalized = _normalize_metric_task_row(
            {
                "created_at": "not-a-date",
                "completed_at": "still-not-a-date",
                "metadata": {
                    "created_at": valid_created_at,
                    "completed_at": valid_created_at,
                },
            }
        )

        assert normalized["created_at"] == valid_created_at
        assert normalized["metadata"]["created_at"] == valid_created_at
        assert normalized["completed_at"] == valid_created_at
        assert normalized["metadata"]["completed_at"] == valid_created_at

    def test_mixed_priorities(self) -> None:
        """Count tasks with mixed priorities."""
        tasks = [
            {"metadata": {"priority": "critical"}},
            {"metadata": {"priority": "high"}},
            {"metadata": {"priority": "high"}},
            {"metadata": {"priority": "medium"}},
            {"metadata": {"priority": "low"}},
        ]
        result = _compute_priority_distribution(tasks)
        assert result.critical == 1
        assert result.high == 2
        assert result.medium == 1
        assert result.low == 1
        assert result.someday == 0

    def test_missing_priority_defaults_to_medium(self) -> None:
        """Tasks without priority default to medium."""
        tasks = [
            {"metadata": {}},
            {"metadata": {"status": "todo"}},
        ]
        result = _compute_priority_distribution(tasks)
        assert result.medium == 2


class TestComputeAssigneeStats:
    """Tests for _compute_assignee_stats helper."""

    def test_empty_tasks(self) -> None:
        """Empty list returns empty stats."""
        result = _compute_assignee_stats([])
        assert result == []

    def test_single_assignee(self) -> None:
        """Stats for single assignee."""
        tasks = [
            {"metadata": {"assignees": ["alice"], "status": "todo"}},
            {"metadata": {"assignees": ["alice"], "status": "doing"}},
            {"metadata": {"assignees": ["alice"], "status": "done"}},
        ]
        result = _compute_assignee_stats(tasks)
        assert len(result) == 1
        assert result[0].name == "alice"
        assert result[0].total == 3
        assert result[0].completed == 1
        assert result[0].in_progress == 1

    def test_multiple_assignees(self) -> None:
        """Stats for multiple assignees."""
        tasks = [
            {"metadata": {"assignees": ["alice"], "status": "done"}},
            {"metadata": {"assignees": ["bob"], "status": "doing"}},
            {"metadata": {"assignees": ["alice"], "status": "todo"}},
        ]
        result = _compute_assignee_stats(tasks)
        assert len(result) == 2
        # Sorted by total descending
        alice_stats = next(s for s in result if s.name == "alice")
        bob_stats = next(s for s in result if s.name == "bob")
        assert alice_stats.total == 2
        assert alice_stats.completed == 1
        assert bob_stats.total == 1
        assert bob_stats.in_progress == 1

    def test_task_with_multiple_assignees(self) -> None:
        """Task assigned to multiple people counts for each."""
        tasks = [
            {"metadata": {"assignees": ["alice", "bob"], "status": "done"}},
        ]
        result = _compute_assignee_stats(tasks)
        assert len(result) == 2
        assert all(s.total == 1 and s.completed == 1 for s in result)

    def test_string_assignee_converted_to_list(self) -> None:
        """String assignee is handled (legacy format)."""
        tasks = [
            {"metadata": {"assignees": "alice", "status": "todo"}},
        ]
        result = _compute_assignee_stats(tasks)
        assert len(result) == 1
        assert result[0].name == "alice"

    def test_empty_assignee_ignored(self) -> None:
        """Empty assignee values are ignored."""
        tasks = [
            {"metadata": {"assignees": [""], "status": "todo"}},
            {"metadata": {"assignees": [], "status": "todo"}},
        ]
        result = _compute_assignee_stats(tasks)
        assert result == []


class TestComputeVelocityTrend:
    """Tests for _compute_velocity_trend helper."""

    def test_empty_tasks(self) -> None:
        """Empty list returns trend with zeros."""
        result = _compute_velocity_trend([], days=7)
        assert len(result) == 7
        assert all(p.value == 0 for p in result)

    def test_trend_sorted_by_date(self) -> None:
        """Trend is sorted by date ascending."""
        result = _compute_velocity_trend([], days=3)
        dates = [p.date for p in result]
        assert dates == sorted(dates)

    def test_completed_tasks_counted(self) -> None:
        """Completed tasks are counted on correct day."""
        now = datetime.now(UTC)
        yesterday = (now - timedelta(days=1)).isoformat()

        tasks = [
            {"metadata": {"status": "done", "completed_at": yesterday}},
            {"metadata": {"status": "done", "completed_at": yesterday}},
        ]
        result = _compute_velocity_trend(tasks, days=7)

        yesterday_date = (now - timedelta(days=1)).strftime("%Y-%m-%d")
        yesterday_point = next((p for p in result if p.date == yesterday_date), None)
        assert yesterday_point is not None
        assert yesterday_point.value == 2

    def test_non_done_tasks_ignored(self) -> None:
        """Non-done tasks are not counted."""
        now = datetime.now(UTC)
        today = now.isoformat()

        tasks = [
            {"metadata": {"status": "todo", "completed_at": today}},
            {"metadata": {"status": "doing", "completed_at": today}},
        ]
        result = _compute_velocity_trend(tasks, days=7)
        assert all(p.value == 0 for p in result)

    def test_old_completions_ignored(self) -> None:
        """Completions older than days are ignored."""
        now = datetime.now(UTC)
        old_date = (now - timedelta(days=30)).isoformat()

        tasks = [
            {"metadata": {"status": "done", "completed_at": old_date}},
        ]
        result = _compute_velocity_trend(tasks, days=7)
        assert all(p.value == 0 for p in result)


class TestCountRecentTasks:
    """Tests for _count_recent_tasks helper."""

    def test_empty_tasks(self) -> None:
        """Empty list returns zero."""
        assert _count_recent_tasks([], days=7) == 0

    def test_recent_tasks_counted(self) -> None:
        """Tasks within window are counted."""
        now = datetime.now(UTC)
        recent = (now - timedelta(days=3)).isoformat()
        old = (now - timedelta(days=30)).isoformat()

        tasks = [
            {"created_at": recent},
            {"created_at": recent},
            {"created_at": old},
        ]
        assert _count_recent_tasks(tasks, days=7, field="created_at") == 2

    def test_metadata_field_checked(self) -> None:
        """Field can be in metadata."""
        now = datetime.now(UTC)
        recent = (now - timedelta(days=1)).isoformat()

        tasks = [
            {"metadata": {"created_at": recent}},
        ]
        assert _count_recent_tasks(tasks, days=7, field="created_at") == 1

    def test_datetime_objects_are_counted(self) -> None:
        """Native datetime values count as recent activity."""
        now = datetime.now(UTC)

        tasks = [
            {"created_at": now - timedelta(days=1)},
            {"created_at": now - timedelta(days=2)},
        ]
        assert _count_recent_tasks(tasks, days=7, field="created_at") == 2


# =============================================================================
# API Endpoint Tests
# =============================================================================


def create_mock_entity(
    entity_type: str = "task",
    name: str = "Test",
    entity_id: str | None = None,
    metadata: dict | None = None,
) -> MagicMock:
    """Create a mock entity for testing."""
    entity = MagicMock(spec=Entity)
    entity.id = entity_id or f"{entity_type}_{uuid4().hex[:8]}"
    entity.name = name
    entity.entity_type = entity_type
    entity.metadata = metadata or {}
    entity.created_at = datetime.now(UTC).isoformat()
    entity.updated_at = datetime.now(UTC).isoformat()

    def model_dump() -> dict:
        return {
            "id": entity.id,
            "name": entity.name,
            "entity_type": entity.entity_type,
            "metadata": entity.metadata,
            "created_at": entity.created_at,
            "updated_at": entity.updated_at,
        }

    entity.model_dump = model_dump
    return entity


def create_mock_org(org_id: str = "test-org-123") -> MagicMock:
    """Create a mock organization."""
    org = MagicMock()
    org.id = org_id
    return org


def create_metric_task_row(
    *,
    project_id: str | None = None,
    status: str | None = None,
    priority: str | None = None,
    assignees: list[str] | str | None = None,
    created_at: str | None = None,
    completed_at: str | None = None,
    updated_at: str | None = None,
    due_date: str | None = None,
    metadata: dict | str | None = None,
) -> dict[str, object]:
    """Create a raw task row for org metrics queries."""
    timestamp = updated_at or datetime.now(UTC).isoformat()
    return {
        "project_id": project_id,
        "status": status,
        "priority": priority,
        "assignees": assignees,
        "created_at": created_at,
        "completed_at": completed_at,
        "updated_at": timestamp,
        "due_date": due_date,
        "metadata": metadata or {},
    }


class TestGetProjectMetrics:
    """Tests for get_project_metrics endpoint."""

    @pytest.mark.asyncio
    async def test_project_not_found(self) -> None:
        """Returns 404 for non-existent project."""
        from sibyl.api.routes.metrics import get_project_metrics

        mock_org = create_mock_org()
        mock_service = AsyncMock()
        mock_service.get_entity.return_value = None

        with (
            patch(
                "sibyl.api.routes.metrics.get_knowledge_read_adapter",
                AsyncMock(return_value=mock_service),
            ),
            patch("sibyl.api.routes.metrics.get_entity_graph_runtime", AsyncMock()),
        ):
            with pytest.raises(HTTPException) as exc_info:
                await get_project_metrics("nonexistent", org=mock_org)

            assert exc_info.value.status_code == 404
            assert "not found" in exc_info.value.detail.lower()

    @pytest.mark.asyncio
    async def test_project_metrics_success(self) -> None:
        """Returns metrics for valid project."""
        from sibyl.api.routes.metrics import get_project_metrics

        mock_org = create_mock_org()
        mock_service = AsyncMock()
        mock_runtime = MagicMock()

        # Create mock project
        mock_project = create_mock_entity(
            entity_type="project", name="Test Project", entity_id="proj_123"
        )

        # Create mock tasks
        now = datetime.now(UTC)
        mock_tasks = [
            create_mock_entity(
                entity_type="task",
                name="Task 1",
                entity_id="task_1",
                metadata={
                    "status": "done",
                    "priority": "high",
                    "project_id": "proj_123",
                    "assignees": ["alice"],
                    "completed_at": (now - timedelta(days=1)).isoformat(),
                },
            ),
            create_mock_entity(
                entity_type="task",
                name="Task 2",
                entity_id="task_2",
                metadata={
                    "status": "doing",
                    "priority": "medium",
                    "project_id": "proj_123",
                    "assignees": ["bob"],
                },
            ),
        ]

        mock_runtime.entity_manager.list_by_type = AsyncMock(return_value=mock_tasks)
        mock_service.get_entity.return_value = mock_project

        with (
            patch(
                "sibyl.api.routes.metrics.get_knowledge_read_adapter",
                AsyncMock(return_value=mock_service),
            ),
            patch(
                "sibyl.api.routes.metrics.get_entity_graph_runtime",
                AsyncMock(return_value=mock_runtime),
            ),
        ):
            result = await get_project_metrics("proj_123", org=mock_org)

            assert result.metrics.project_id == "proj_123"
            assert result.metrics.project_name == "Test Project"
            assert result.metrics.total_tasks == 2  # Only proj_123 tasks
            assert result.metrics.status_distribution.done == 1
            assert result.metrics.status_distribution.doing == 1
            assert result.metrics.priority_distribution.high == 1
            assert result.metrics.priority_distribution.medium == 1
            assert len(result.metrics.assignees) == 2
            assert result.metrics.completion_rate == 50.0
            assert mock_runtime.entity_manager.list_by_type.await_args_list == [
                call(
                    EntityType.TASK,
                    limit=1000,
                    offset=0,
                    project_id="proj_123",
                )
            ]

    @pytest.mark.asyncio
    async def test_project_metrics_empty_tasks(self) -> None:
        """Returns metrics with zero tasks."""
        from sibyl.api.routes.metrics import get_project_metrics

        mock_org = create_mock_org()
        mock_service = AsyncMock()
        mock_runtime = MagicMock()

        mock_project = create_mock_entity(
            entity_type="project", name="Empty Project", entity_id="proj_empty"
        )

        mock_runtime.entity_manager.list_by_type = AsyncMock(return_value=[])
        mock_service.get_entity.return_value = mock_project

        with (
            patch(
                "sibyl.api.routes.metrics.get_knowledge_read_adapter",
                AsyncMock(return_value=mock_service),
            ),
            patch(
                "sibyl.api.routes.metrics.get_entity_graph_runtime",
                AsyncMock(return_value=mock_runtime),
            ),
        ):
            result = await get_project_metrics("proj_empty", org=mock_org)

            assert result.metrics.total_tasks == 0
            assert result.metrics.completion_rate == 0.0
            assert len(result.metrics.velocity_trend) == 14

    @pytest.mark.asyncio
    async def test_project_metrics_pages_past_first_1000_tasks(self) -> None:
        """Project metrics should keep loading tasks after the first page."""
        from sibyl.api.routes.metrics import get_project_metrics

        mock_org = create_mock_org()
        mock_service = AsyncMock()
        mock_runtime = MagicMock()
        mock_project = create_mock_entity(
            entity_type="project", name="Big Project", entity_id="proj_big"
        )
        first_page = [
            create_mock_entity(
                entity_type="task",
                name=f"Task {index}",
                entity_id=f"task_{index:04}",
                metadata={
                    "status": "todo",
                    "priority": "low",
                    "project_id": "proj_big",
                },
            )
            for index in range(1000)
        ]
        second_page = [
            create_mock_entity(
                entity_type="task",
                name="Done task",
                entity_id="task_done",
                metadata={
                    "status": "done",
                    "priority": "high",
                    "project_id": "proj_big",
                },
            )
        ]

        mock_runtime.entity_manager.list_by_type = AsyncMock(side_effect=[first_page, second_page])
        mock_service.get_entity.return_value = mock_project

        with (
            patch(
                "sibyl.api.routes.metrics.get_knowledge_read_adapter",
                AsyncMock(return_value=mock_service),
            ),
            patch(
                "sibyl.api.routes.metrics.get_entity_graph_runtime",
                AsyncMock(return_value=mock_runtime),
            ),
        ):
            result = await get_project_metrics("proj_big", org=mock_org)

        assert result.metrics.total_tasks == 1001
        assert result.metrics.status_distribution.done == 1
        assert result.metrics.status_distribution.todo == 1000
        assert result.metrics.priority_distribution.high == 1
        assert result.metrics.priority_distribution.low == 1000
        assert mock_runtime.entity_manager.list_by_type.await_args_list == [
            call(
                EntityType.TASK,
                limit=1000,
                offset=0,
                project_id="proj_big",
            ),
            call(
                EntityType.TASK,
                limit=1000,
                offset=1000,
                project_id="proj_big",
            ),
        ]


class TestGetOrgMetrics:
    """Tests for get_org_metrics endpoint."""

    @pytest.mark.asyncio
    async def test_org_metrics_success(self) -> None:
        """Returns organization-wide metrics."""
        from sibyl.api.routes.metrics import get_org_metrics

        mock_org = create_mock_org()
        mock_service = AsyncMock()

        # Create mock projects
        mock_projects = [
            create_mock_entity(entity_type="project", name="Project A", entity_id="proj_a"),
            create_mock_entity(entity_type="project", name="Project B", entity_id="proj_b"),
        ]

        now = datetime.now(UTC)
        recent = now.isoformat()
        mock_tasks = [
            create_mock_entity(
                entity_type="task",
                name="Task A1",
                entity_id="task_a1",
                metadata={
                    "project_id": "proj_a",
                    "status": "done",
                    "priority": "critical",
                    "assignees": ["alice"],
                    "created_at": recent,
                    "completed_at": recent,
                },
            ),
            create_mock_entity(
                entity_type="task",
                name="Task A2",
                entity_id="task_a2",
                metadata={
                    "project_id": "proj_a",
                    "status": "doing",
                    "priority": "high",
                    "assignees": ["alice"],
                    "created_at": recent,
                },
            ),
            create_mock_entity(
                entity_type="task",
                name="Task B1",
                entity_id="task_b1",
                metadata={
                    "project_id": "proj_b",
                    "status": "todo",
                    "priority": "medium",
                    "assignees": [],
                    "created_at": recent,
                },
            ),
        ]

        mock_service.list_entities = AsyncMock(
            side_effect=[
                Page(items=mock_projects, next_cursor=None),
                Page(items=mock_tasks, next_cursor=None),
            ]
        )

        with (
            patch(
                "sibyl.api.routes.metrics.get_knowledge_read_adapter",
                AsyncMock(return_value=mock_service),
            ),
            patch(
                "sibyl.api.routes.metrics._list_surreal_metric_task_rows",
                AsyncMock(return_value=None),
            ),
        ):
            result = await get_org_metrics(org=mock_org)

            assert mock_service.list_entities.await_args_list == [
                call(
                    EntityType.PROJECT,
                    limit=500,
                    cursor=None,
                ),
                call(
                    EntityType.TASK,
                    limit=1000,
                    cursor=None,
                ),
            ]
            assert result.total_projects == 2
            assert result.total_tasks == 3
            assert result.status_distribution.done == 1
            assert result.status_distribution.doing == 1
            assert result.status_distribution.todo == 1
            assert result.priority_distribution.critical == 1
            assert result.priority_distribution.high == 1
            assert len(result.top_assignees) == 1
            assert result.top_assignees[0].name == "alice"
            assert result.top_assignees[0].total == 2
            assert len(result.projects_summary) == 2
            assert result.projects_summary[0].doing == 1
            assert result.projects_summary[0].high == 1

    @pytest.mark.asyncio
    async def test_org_metrics_uses_surreal_metric_task_fast_path(self) -> None:
        """Organization metrics reuse lean Surreal task rows when available."""
        from sibyl.api.routes.metrics import get_org_metrics

        mock_org = create_mock_org()
        mock_service = AsyncMock()
        recent = datetime.now(UTC).isoformat()
        mock_projects = [
            create_mock_entity(entity_type="project", name="Project A", entity_id="proj_a"),
            create_mock_entity(entity_type="project", name="Project B", entity_id="proj_b"),
        ]
        mock_tasks = [
            _normalize_metric_task_row(
                create_metric_task_row(
                    project_id="proj_a",
                    status="done",
                    priority="critical",
                    assignees=["alice"],
                    created_at=recent,
                    completed_at=recent,
                )
            ),
            _normalize_metric_task_row(
                create_metric_task_row(
                    project_id="proj_b",
                    status="doing",
                    priority="high",
                    assignees=["bob"],
                    created_at=recent,
                )
            ),
        ]
        metric_task_rows = AsyncMock(return_value=mock_tasks)

        mock_service.list_entities = AsyncMock(
            return_value=Page(items=mock_projects, next_cursor=None)
        )

        with (
            patch(
                "sibyl.api.routes.metrics.get_knowledge_read_adapter",
                AsyncMock(return_value=mock_service),
            ),
            patch(
                "sibyl.api.routes.metrics._list_surreal_metric_task_rows",
                metric_task_rows,
            ),
        ):
            result = await get_org_metrics(org=mock_org)

        assert mock_service.list_entities.await_args_list == [
            call(
                EntityType.PROJECT,
                limit=500,
                cursor=None,
            ),
        ]
        metric_task_rows.assert_awaited_once_with(str(mock_org.id))
        assert result.total_projects == 2
        assert result.total_tasks == 2
        assert result.status_distribution.done == 1
        assert result.status_distribution.doing == 1
        assert result.priority_distribution.critical == 1
        assert result.priority_distribution.high == 1
        assert result.top_assignees[0].name == "alice"
        assert result.projects_summary[0].id in {"proj_a", "proj_b"}

    @pytest.mark.asyncio
    async def test_org_metrics_empty(self) -> None:
        """Returns metrics with no projects or tasks."""
        from sibyl.api.routes.metrics import get_org_metrics

        mock_org = create_mock_org()
        mock_service = AsyncMock()

        mock_service.list_entities = AsyncMock(
            side_effect=[
                Page(items=[], next_cursor=None),
                Page(items=[], next_cursor=None),
            ]
        )

        with (
            patch(
                "sibyl.api.routes.metrics.get_knowledge_read_adapter",
                AsyncMock(return_value=mock_service),
            ),
            patch(
                "sibyl.api.routes.metrics._list_surreal_metric_task_rows",
                AsyncMock(return_value=None),
            ),
        ):
            result = await get_org_metrics(org=mock_org)

            assert mock_service.list_entities.await_args_list == [
                call(
                    EntityType.PROJECT,
                    limit=500,
                    cursor=None,
                ),
                call(
                    EntityType.TASK,
                    limit=1000,
                    cursor=None,
                ),
            ]
            assert result.total_projects == 0
            assert result.total_tasks == 0
            assert result.completion_rate == 0.0
            assert result.top_assignees == []
            assert len(result.velocity_trend) == 14

    @pytest.mark.asyncio
    async def test_org_metrics_projects_summary_sorted(self) -> None:
        """Projects summary is sorted by total tasks descending."""
        from sibyl.api.routes.metrics import get_org_metrics

        mock_org = create_mock_org()
        mock_service = AsyncMock()

        mock_projects = [
            create_mock_entity(entity_type="project", name="Small", entity_id="proj_s"),
            create_mock_entity(entity_type="project", name="Large", entity_id="proj_l"),
        ]
        mock_tasks = [
            create_mock_entity(
                entity_type="task",
                name="Large done",
                entity_id="task_large_done",
                metadata={"project_id": "proj_l", "status": "done"},
            ),
            create_mock_entity(
                entity_type="task",
                name="Large todo",
                entity_id="task_large_todo",
                metadata={"project_id": "proj_l", "status": "todo"},
            ),
            create_mock_entity(
                entity_type="task",
                name="Small todo",
                entity_id="task_small_todo",
                metadata={"project_id": "proj_s", "status": "todo"},
            ),
        ]

        mock_service.list_entities = AsyncMock(
            side_effect=[
                Page(items=mock_projects, next_cursor=None),
                Page(items=mock_tasks, next_cursor=None),
            ]
        )

        with (
            patch(
                "sibyl.api.routes.metrics.get_knowledge_read_adapter",
                AsyncMock(return_value=mock_service),
            ),
            patch(
                "sibyl.api.routes.metrics._list_surreal_metric_task_rows",
                AsyncMock(return_value=None),
            ),
        ):
            result = await get_org_metrics(org=mock_org)

            assert mock_service.list_entities.await_args_list == [
                call(
                    EntityType.PROJECT,
                    limit=500,
                    cursor=None,
                ),
                call(
                    EntityType.TASK,
                    limit=1000,
                    cursor=None,
                ),
            ]
            # First project should be the one with more tasks
            assert result.projects_summary[0].id == "proj_l"
            assert result.projects_summary[0].total == 2

    @pytest.mark.asyncio
    async def test_org_metrics_projects_summary_includes_open_priority_and_overdue_counts(
        self,
    ) -> None:
        """Project summaries include the counts used by the projects view."""
        from sibyl.api.routes.metrics import get_org_metrics

        mock_org = create_mock_org()
        mock_service = AsyncMock()

        mock_projects = [
            create_mock_entity(entity_type="project", name="Alpha", entity_id="proj_a"),
        ]
        mock_tasks = [
            create_mock_entity(
                entity_type="task",
                name="Critical doing",
                entity_id="task_a_doing",
                metadata={
                    "project_id": "proj_a",
                    "status": "doing",
                    "priority": "critical",
                    "due_date": (datetime.now(UTC) - timedelta(days=1)).isoformat(),
                },
            ),
            create_mock_entity(
                entity_type="task",
                name="High blocked",
                entity_id="task_a_blocked",
                metadata={
                    "project_id": "proj_a",
                    "status": "blocked",
                    "priority": "high",
                },
            ),
            create_mock_entity(
                entity_type="task",
                name="High review",
                entity_id="task_a_review",
                metadata={
                    "project_id": "proj_a",
                    "status": "review",
                    "priority": "high",
                },
            ),
            create_mock_entity(
                entity_type="task",
                name="Critical done",
                entity_id="task_a_done",
                metadata={
                    "project_id": "proj_a",
                    "status": "done",
                    "priority": "critical",
                },
            ),
        ]

        mock_service.list_entities = AsyncMock(
            side_effect=[
                Page(items=mock_projects, next_cursor=None),
                Page(items=mock_tasks, next_cursor=None),
            ]
        )

        with (
            patch(
                "sibyl.api.routes.metrics.get_knowledge_read_adapter",
                AsyncMock(return_value=mock_service),
            ),
            patch(
                "sibyl.api.routes.metrics._list_surreal_metric_task_rows",
                AsyncMock(return_value=None),
            ),
        ):
            result = await get_org_metrics(org=mock_org)

            assert mock_service.list_entities.await_args_list == [
                call(
                    EntityType.PROJECT,
                    limit=500,
                    cursor=None,
                ),
                call(
                    EntityType.TASK,
                    limit=1000,
                    cursor=None,
                ),
            ]
            summary = result.projects_summary[0]
            assert summary.total == 4
            assert summary.completed == 1
            assert summary.doing == 1
            assert summary.review == 1
            assert summary.blocked == 1
            assert summary.critical == 1
            assert summary.high == 2
            assert summary.overdue == 1

    @pytest.mark.asyncio
    async def test_org_metrics_preserves_metadata_fallbacks_and_bad_row_tolerance(self) -> None:
        """Legacy metadata-backed rows still contribute correctly without crashing metrics."""
        from sibyl.api.routes.metrics import get_org_metrics

        mock_org = create_mock_org()
        mock_service = AsyncMock()

        mock_projects = [
            create_mock_entity(entity_type="project", name="Legacy", entity_id="proj_legacy"),
        ]

        recent = datetime.now(UTC).isoformat()
        overdue = (datetime.now(UTC) - timedelta(days=2)).isoformat()

        mock_tasks = [
            create_mock_entity(
                entity_type="task",
                name="Legacy done",
                entity_id="task_legacy_done",
                metadata={
                    "project_id": "proj_legacy",
                    "status": "done",
                    "priority": "critical",
                    "assignees": "alice",
                    "created_at": recent,
                    "completed_at": recent,
                },
            ),
            create_mock_entity(
                entity_type="task",
                name="Legacy doing",
                entity_id="task_legacy_doing",
                metadata={
                    "project_id": "proj_legacy",
                    "status": "doing",
                    "priority": "critical",
                    "assignees": ["bob"],
                    "created_at": recent,
                    "due_date": overdue,
                },
            ),
            create_mock_entity(
                entity_type="task",
                name="Legacy todo",
                entity_id="task_legacy_todo",
                metadata={
                    "project_id": "proj_legacy",
                    "status": "todo",
                    "priority": "high",
                    "created_at": "not-a-date",
                    "assignees": "carol",
                },
            ),
        ]
        mock_tasks[2].created_at = "not-a-date"

        mock_service.list_entities = AsyncMock(
            side_effect=[
                Page(items=mock_projects, next_cursor=None),
                Page(items=mock_tasks, next_cursor=None),
            ]
        )

        with (
            patch(
                "sibyl.api.routes.metrics.get_knowledge_read_adapter",
                AsyncMock(return_value=mock_service),
            ),
            patch(
                "sibyl.api.routes.metrics._list_surreal_metric_task_rows",
                AsyncMock(return_value=None),
            ),
        ):
            result = await get_org_metrics(org=mock_org)

            assert result.total_tasks == 3
            assert result.status_distribution.done == 1
            assert result.status_distribution.doing == 1
            assert result.status_distribution.todo == 1
            assert result.priority_distribution.critical == 2
            assert result.priority_distribution.high == 1
            assert result.tasks_created_last_7d == 2
            assert result.tasks_completed_last_7d == 1
            assert [assignee.name for assignee in result.top_assignees[:2]] == ["alice", "bob"]

            summary = result.projects_summary[0]
            assert summary.total == 3
            assert summary.completed == 1
            assert summary.doing == 1
            assert summary.todo == 1
            assert summary.critical == 1
            assert summary.high == 1
            assert summary.overdue == 1

    @pytest.mark.asyncio
    async def test_org_metrics_pages_past_first_500_projects(self) -> None:
        """Organization metrics should keep loading project pages after the first 500."""
        from sibyl.api.routes.metrics import get_org_metrics

        mock_org = create_mock_org()
        mock_service = AsyncMock()
        first_page = [
            create_mock_entity(
                entity_type="project", name=f"Project {index}", entity_id=f"proj_{index}"
            )
            for index in range(500)
        ]
        second_page = [
            create_mock_entity(entity_type="project", name="Project 500", entity_id="proj_500")
        ]

        mock_service.list_entities = AsyncMock(
            side_effect=[
                Page(items=first_page, next_cursor="500"),
                Page(items=second_page, next_cursor=None),
                Page(items=[], next_cursor=None),
            ]
        )

        with (
            patch(
                "sibyl.api.routes.metrics.get_knowledge_read_adapter",
                AsyncMock(return_value=mock_service),
            ),
            patch(
                "sibyl.api.routes.metrics._list_surreal_metric_task_rows",
                AsyncMock(return_value=None),
            ),
        ):
            result = await get_org_metrics(org=mock_org)

        assert result.total_projects == 501
        assert mock_service.list_entities.await_args_list == [
            call(
                EntityType.PROJECT,
                limit=500,
                cursor=None,
            ),
            call(
                EntityType.PROJECT,
                limit=500,
                cursor="500",
            ),
            call(
                EntityType.TASK,
                limit=1000,
                cursor=None,
            ),
        ]



    @pytest.mark.asyncio
    async def test_org_metrics_filters_to_accessible_projects(self) -> None:
        """Org metrics includes only projects/tasks in the caller access set."""
        from sibyl.api.routes.metrics import get_org_metrics

        mock_org = create_mock_org()
        mock_service = AsyncMock()

        mock_projects = [
            create_mock_entity(entity_type="project", name="Public", entity_id="proj_public"),
            create_mock_entity(entity_type="project", name="Secret", entity_id="proj_secret"),
        ]
        mock_tasks = [
            create_mock_entity(
                entity_type="task",
                name="Public Task",
                entity_id="task_public",
                metadata={"project_id": "proj_public", "status": "doing", "priority": "high"},
            ),
            create_mock_entity(
                entity_type="task",
                name="Secret Task",
                entity_id="task_secret",
                metadata={"project_id": "proj_secret", "status": "done", "priority": "critical"},
            ),
        ]

        mock_service.list_entities = AsyncMock(
            side_effect=[
                Page(items=mock_projects, next_cursor=None),
                Page(items=mock_tasks, next_cursor=None),
            ]
        )
        mock_ctx = MagicMock(spec=AuthContext)

        with (
            patch(
                "sibyl.api.routes.metrics.get_knowledge_read_adapter",
                AsyncMock(return_value=mock_service),
            ),
            patch(
                "sibyl.api.routes.metrics._list_surreal_metric_task_rows",
                AsyncMock(return_value=None),
            ),
            patch(
                "sibyl.api.routes.metrics.list_accessible_project_graph_ids",
                AsyncMock(return_value={"proj_public"}),
            ),
        ):
            result = await get_org_metrics(org=mock_org, ctx=mock_ctx)

        assert result.total_projects == 1
        assert result.total_tasks == 1
        assert [summary.id for summary in result.projects_summary] == ["proj_public"]
        assert result.status_distribution.doing == 1
        assert result.status_distribution.done == 0

class TestGetProjectSummaries:
    """Tests for get_project_summaries endpoint."""

    @pytest.mark.asyncio
    async def test_project_summaries_success(self) -> None:
        """Returns the lean per-project summary payload."""
        from sibyl.api.routes.metrics import get_project_summaries

        mock_org = create_mock_org()
        mock_service = AsyncMock()

        mock_projects = [
            create_mock_entity(entity_type="project", name="Project A", entity_id="proj_a"),
            create_mock_entity(entity_type="project", name="Project B", entity_id="proj_b"),
        ]
        mock_tasks = [
            create_mock_entity(
                entity_type="task",
                name="Proj B doing",
                entity_id="task_proj_b",
                metadata={
                    "project_id": "proj_b",
                    "status": "doing",
                    "priority": "critical",
                },
            ),
            create_mock_entity(
                entity_type="task",
                name="Proj A done",
                entity_id="task_proj_a_done",
                metadata={
                    "project_id": "proj_a",
                    "status": "done",
                    "priority": "high",
                },
            ),
            create_mock_entity(
                entity_type="task",
                name="Proj A todo",
                entity_id="task_proj_a_todo",
                metadata={
                    "project_id": "proj_a",
                    "status": "todo",
                    "priority": "high",
                },
            ),
        ]

        mock_service.list_entities = AsyncMock(
            side_effect=[
                Page(items=mock_projects, next_cursor=None),
                Page(items=mock_tasks, next_cursor=None),
            ]
        )

        with (
            patch(
                "sibyl.api.routes.metrics.get_knowledge_read_adapter",
                AsyncMock(return_value=mock_service),
            ),
            patch(
                "sibyl.api.routes.metrics._list_surreal_metric_task_rows",
                AsyncMock(return_value=None),
            ),
        ):
            result = await get_project_summaries(org=mock_org)

            assert mock_service.list_entities.await_args_list == [
                call(
                    EntityType.PROJECT,
                    limit=500,
                    cursor=None,
                ),
                call(
                    EntityType.TASK,
                    limit=1000,
                    cursor=None,
                ),
            ]
            assert len(result.projects_summary) == 2
            assert result.projects_summary[0].id == "proj_a"
            assert result.projects_summary[0].total == 2
            assert result.projects_summary[0].completed == 1
            assert result.projects_summary[0].high == 1
            assert result.projects_summary[1].id == "proj_b"
            assert result.projects_summary[1].doing == 1
            assert result.projects_summary[1].critical == 1

    @pytest.mark.asyncio
    async def test_project_summaries_uses_surreal_metric_task_fast_path(self) -> None:
        """Surreal-backed summaries fetch lean task rows without paging task entities."""
        from sibyl.api.routes.metrics import get_project_summaries

        mock_org = create_mock_org()
        mock_service = AsyncMock()
        execute_surreal_query = AsyncMock(
            return_value=[
                create_metric_task_row(
                    project_id="proj_b",
                    status="doing",
                    priority="critical",
                ),
                create_metric_task_row(
                    project_id="proj_a",
                    status="done",
                    priority="high",
                ),
                create_metric_task_row(
                    project_id="proj_a",
                    status="todo",
                    priority="high",
                ),
            ]
        )

        mock_projects = [
            create_mock_entity(entity_type="project", name="Project A", entity_id="proj_a"),
            create_mock_entity(entity_type="project", name="Project B", entity_id="proj_b"),
        ]
        mock_service.list_entities = AsyncMock(
            return_value=Page(items=mock_projects, next_cursor=None)
        )

        with (
            patch(
                "sibyl.api.routes.metrics.get_knowledge_read_adapter",
                AsyncMock(return_value=mock_service),
            ),
            patch(
                "sibyl.api.routes.metrics.execute_surreal_graph_query",
                execute_surreal_query,
            ),
        ):
            result = await get_project_summaries(org=mock_org)

        assert mock_service.list_entities.await_args_list == [
            call(
                EntityType.PROJECT,
                limit=500,
                cursor=None,
            ),
        ]
        assert execute_surreal_query.await_count == 1
        assert execute_surreal_query.await_args.args[0] == str(mock_org.id)
        assert "FROM entity" in execute_surreal_query.await_args.args[1]
        assert (
            "string::lowercase(status ?? attributes.status ?? '') != 'archived'"
            in execute_surreal_query.await_args.args[1]
        )
        assert execute_surreal_query.await_args.kwargs == {
            "task_type": EntityType.TASK.value,
        }
        assert result.projects_summary[0].id == "proj_a"
        assert result.projects_summary[0].total == 2
        assert result.projects_summary[0].completed == 1
        assert result.projects_summary[0].high == 1
        assert result.projects_summary[1].id == "proj_b"
        assert result.projects_summary[1].doing == 1
        assert result.projects_summary[1].critical == 1

    @pytest.mark.asyncio
    async def test_project_summaries_pages_past_first_500_projects(self) -> None:
        """Project summaries should keep loading projects after the first page."""
        from sibyl.api.routes.metrics import get_project_summaries

        mock_org = create_mock_org()
        mock_service = AsyncMock()
        first_page = [
            create_mock_entity(
                entity_type="project", name=f"Project {index}", entity_id=f"proj_{index}"
            )
            for index in range(500)
        ]
        second_page = [
            create_mock_entity(entity_type="project", name="Project 500", entity_id="proj_500")
        ]

        mock_service.list_entities = AsyncMock(
            side_effect=[
                Page(items=first_page, next_cursor="500"),
                Page(items=second_page, next_cursor=None),
                Page(items=[], next_cursor=None),
            ]
        )

        with (
            patch(
                "sibyl.api.routes.metrics.get_knowledge_read_adapter",
                AsyncMock(return_value=mock_service),
            ),
            patch(
                "sibyl.api.routes.metrics._list_surreal_metric_task_rows",
                AsyncMock(return_value=None),
            ),
        ):
            result = await get_project_summaries(org=mock_org)

        assert len(result.projects_summary) == 501
        assert mock_service.list_entities.await_args_list == [
            call(
                EntityType.PROJECT,
                limit=500,
                cursor=None,
            ),
            call(
                EntityType.PROJECT,
                limit=500,
                cursor="500",
            ),
            call(
                EntityType.TASK,
                limit=1000,
                cursor=None,
            ),
        ]


class TestMetricsErrorHandling:
    """Tests for error handling in metrics endpoints."""

    @pytest.mark.asyncio
    async def test_project_metrics_internal_error(self) -> None:
        """Returns 500 for unexpected errors."""
        from sibyl.api.routes.metrics import get_project_metrics

        mock_org = create_mock_org()
        mock_service = AsyncMock()
        mock_service.get_entity.return_value = create_mock_entity(
            entity_type="project", name="Project", entity_id="proj_123"
        )

        with (
            patch(
                "sibyl.api.routes.metrics.get_knowledge_read_adapter",
                AsyncMock(return_value=mock_service),
            ),
            patch(
                "sibyl.api.routes.metrics.get_entity_graph_runtime",
                side_effect=Exception("Database error"),
            ),
        ):
            with pytest.raises(HTTPException) as exc_info:
                await get_project_metrics("proj_123", org=mock_org)

            assert exc_info.value.status_code == 500
            assert "Failed to get project metrics" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_org_metrics_internal_error(self) -> None:
        """Returns 500 for unexpected errors."""
        from sibyl.api.routes.metrics import get_org_metrics

        mock_org = create_mock_org()

        with patch(
            "sibyl.api.routes.metrics.get_knowledge_read_adapter",
            side_effect=Exception("Database error"),
        ):
            with pytest.raises(HTTPException) as exc_info:
                await get_org_metrics(org=mock_org)

            assert exc_info.value.status_code == 500
            assert "Failed to get organization metrics" in exc_info.value.detail
