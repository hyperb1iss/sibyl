"""Tests for metrics endpoints and computation functions."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from sibyl.api.routes.metrics import (
    _compute_assignee_stats,
    _compute_priority_distribution,
    _compute_status_distribution,
    _compute_velocity_trend,
    _count_recent_tasks,
    _parse_iso_date,
)
from sibyl.api.schemas import (
    AssigneeStats,
    TaskPriorityDistribution,
    TaskStatusDistribution,
    TimeSeriesPoint,
)
from sibyl.models.entities import Entity, EntityType


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


class TestGetProjectMetrics:
    """Tests for get_project_metrics endpoint."""

    @pytest.mark.asyncio
    async def test_project_not_found(self) -> None:
        """Returns 404 for non-existent project."""
        from sibyl.api.routes.metrics import get_project_metrics

        mock_org = create_mock_org()
        mock_client = AsyncMock()
        mock_entity_manager = AsyncMock()
        mock_entity_manager.get.return_value = None

        with (
            patch(
                "sibyl.api.routes.metrics.get_graph_client", return_value=mock_client
            ),
            patch(
                "sibyl.api.routes.metrics.EntityManager",
                return_value=mock_entity_manager,
            ),
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
        mock_client = AsyncMock()

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
            create_mock_entity(
                entity_type="task",
                name="Task 3",
                entity_id="task_3",
                metadata={
                    "status": "todo",
                    "priority": "low",
                    "project_id": "other_proj",  # Different project
                },
            ),
        ]

        mock_entity_manager = AsyncMock()
        mock_entity_manager.get.return_value = mock_project
        mock_entity_manager.list_by_type.return_value = mock_tasks

        with (
            patch(
                "sibyl.api.routes.metrics.get_graph_client", return_value=mock_client
            ),
            patch(
                "sibyl.api.routes.metrics.EntityManager",
                return_value=mock_entity_manager,
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

    @pytest.mark.asyncio
    async def test_project_metrics_empty_tasks(self) -> None:
        """Returns metrics with zero tasks."""
        from sibyl.api.routes.metrics import get_project_metrics

        mock_org = create_mock_org()
        mock_client = AsyncMock()

        mock_project = create_mock_entity(
            entity_type="project", name="Empty Project", entity_id="proj_empty"
        )

        mock_entity_manager = AsyncMock()
        mock_entity_manager.get.return_value = mock_project
        mock_entity_manager.list_by_type.return_value = []

        with (
            patch(
                "sibyl.api.routes.metrics.get_graph_client", return_value=mock_client
            ),
            patch(
                "sibyl.api.routes.metrics.EntityManager",
                return_value=mock_entity_manager,
            ),
        ):
            result = await get_project_metrics("proj_empty", org=mock_org)

            assert result.metrics.total_tasks == 0
            assert result.metrics.completion_rate == 0.0
            assert len(result.metrics.velocity_trend) == 14


class TestGetOrgMetrics:
    """Tests for get_org_metrics endpoint."""

    @pytest.mark.asyncio
    async def test_org_metrics_success(self) -> None:
        """Returns organization-wide metrics."""
        from sibyl.api.routes.metrics import get_org_metrics

        mock_org = create_mock_org()
        mock_client = AsyncMock()

        # Create mock projects
        mock_projects = [
            create_mock_entity(
                entity_type="project", name="Project A", entity_id="proj_a"
            ),
            create_mock_entity(
                entity_type="project", name="Project B", entity_id="proj_b"
            ),
        ]

        # Create mock tasks
        now = datetime.now(UTC)
        mock_tasks = [
            create_mock_entity(
                entity_type="task",
                name="Task 1",
                metadata={
                    "status": "done",
                    "priority": "critical",
                    "project_id": "proj_a",
                    "assignees": ["alice"],
                    "completed_at": (now - timedelta(days=2)).isoformat(),
                },
            ),
            create_mock_entity(
                entity_type="task",
                name="Task 2",
                metadata={
                    "status": "doing",
                    "priority": "high",
                    "project_id": "proj_a",
                    "assignees": ["alice"],
                },
            ),
            create_mock_entity(
                entity_type="task",
                name="Task 3",
                metadata={
                    "status": "todo",
                    "priority": "medium",
                    "project_id": "proj_b",
                },
            ),
        ]

        mock_entity_manager = AsyncMock()
        mock_entity_manager.list_by_type.side_effect = lambda t, **_: (
            mock_projects if t == "project" else mock_tasks
        )

        with (
            patch(
                "sibyl.api.routes.metrics.get_graph_client", return_value=mock_client
            ),
            patch(
                "sibyl.api.routes.metrics.EntityManager",
                return_value=mock_entity_manager,
            ),
        ):
            result = await get_org_metrics(org=mock_org)

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

    @pytest.mark.asyncio
    async def test_org_metrics_empty(self) -> None:
        """Returns metrics with no projects or tasks."""
        from sibyl.api.routes.metrics import get_org_metrics

        mock_org = create_mock_org()
        mock_client = AsyncMock()

        mock_entity_manager = AsyncMock()
        mock_entity_manager.list_by_type.return_value = []

        with (
            patch(
                "sibyl.api.routes.metrics.get_graph_client", return_value=mock_client
            ),
            patch(
                "sibyl.api.routes.metrics.EntityManager",
                return_value=mock_entity_manager,
            ),
        ):
            result = await get_org_metrics(org=mock_org)

            assert result.total_projects == 0
            assert result.total_tasks == 0
            assert result.completion_rate == 0.0

    @pytest.mark.asyncio
    async def test_org_metrics_projects_summary_sorted(self) -> None:
        """Projects summary is sorted by total tasks descending."""
        from sibyl.api.routes.metrics import get_org_metrics

        mock_org = create_mock_org()
        mock_client = AsyncMock()

        mock_projects = [
            create_mock_entity(entity_type="project", name="Small", entity_id="proj_s"),
            create_mock_entity(entity_type="project", name="Large", entity_id="proj_l"),
        ]

        # More tasks for proj_l
        mock_tasks = [
            create_mock_entity(
                entity_type="task",
                metadata={"status": "done", "project_id": "proj_l"},
            ),
            create_mock_entity(
                entity_type="task",
                metadata={"status": "todo", "project_id": "proj_l"},
            ),
            create_mock_entity(
                entity_type="task",
                metadata={"status": "todo", "project_id": "proj_s"},
            ),
        ]

        mock_entity_manager = AsyncMock()
        mock_entity_manager.list_by_type.side_effect = lambda t, **_: (
            mock_projects if t == "project" else mock_tasks
        )

        with (
            patch(
                "sibyl.api.routes.metrics.get_graph_client", return_value=mock_client
            ),
            patch(
                "sibyl.api.routes.metrics.EntityManager",
                return_value=mock_entity_manager,
            ),
        ):
            result = await get_org_metrics(org=mock_org)

            # First project should be the one with more tasks
            assert result.projects_summary[0]["id"] == "proj_l"
            assert result.projects_summary[0]["total"] == 2


class TestMetricsErrorHandling:
    """Tests for error handling in metrics endpoints."""

    @pytest.mark.asyncio
    async def test_project_metrics_internal_error(self) -> None:
        """Returns 500 for unexpected errors."""
        from sibyl.api.routes.metrics import get_project_metrics

        mock_org = create_mock_org()

        with patch(
            "sibyl.api.routes.metrics.get_graph_client",
            side_effect=Exception("Database error"),
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
            "sibyl.api.routes.metrics.get_graph_client",
            side_effect=Exception("Database error"),
        ):
            with pytest.raises(HTTPException) as exc_info:
                await get_org_metrics(org=mock_org)

            assert exc_info.value.status_code == 500
            assert "Failed to get organization metrics" in exc_info.value.detail
