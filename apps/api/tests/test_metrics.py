"""Tests for metrics endpoints and computation functions."""

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, call, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from sibyl.api.routes.metrics import (
    TASK_ROLLUP_STATEMENT,
    TaskRollup,
    _assignee_stats,
    _created_last_week,
    _parse_iso_date,
    _priority_distribution,
    _status_distribution,
    _velocity_trend,
    rollups_from_surreal_payload,
    rollups_from_task_dicts,
)
from sibyl.api.websocket import ConnectionManager
from sibyl.auth.context import AuthContext
from sibyl.persistence.read_memo import reset_org_read_memos
from sibyl_core.models.entities import Entity, EntityType
from sibyl_core.storage import Page

NOW = datetime.now(UTC)


# The runtime that cannot run the grouped statement renders from paged task
# rows grouped in Python, so the fixtures below feed that path and the
# renderers the grouped statement shares with it.
def status_distribution_of(tasks: list[dict[str, Any]]):
    return _status_distribution(rollups_from_task_dicts(tasks, now=NOW))


def priority_distribution_of(tasks: list[dict[str, Any]]):
    return _priority_distribution(rollups_from_task_dicts(tasks, now=NOW))


def assignee_stats_of(tasks: list[dict[str, Any]]):
    return _assignee_stats(rollups_from_task_dicts(tasks, now=NOW))


def velocity_trend_of(tasks: list[dict[str, Any]], *, days: int):
    return _velocity_trend(rollups_from_task_dicts(tasks, now=NOW), now=NOW, days=days)


def created_last_week_of(tasks: list[dict[str, Any]]) -> int:
    return _created_last_week(rollups_from_task_dicts(tasks, now=NOW))


@pytest.fixture(autouse=True)
def _fresh_read_memos():
    reset_org_read_memos()
    yield
    reset_org_read_memos()


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
    """Status counts rendered from grouped task rows."""

    def test_empty_tasks(self) -> None:
        """Empty list returns all zeros."""
        result = status_distribution_of([])
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
        result = status_distribution_of(tasks)
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
        result = status_distribution_of(tasks)
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
        result = status_distribution_of(tasks)
        assert result.backlog == 2

    def test_unknown_status_ignored(self) -> None:
        """Unknown status values are ignored."""
        tasks = [
            {"metadata": {"status": "unknown_status"}},
            {"metadata": {"status": "todo"}},
        ]
        result = status_distribution_of(tasks)
        assert result.todo == 1
        # unknown_status doesn't match any attribute, so only todo counted


class TestComputePriorityDistribution:
    """Priority counts rendered from grouped task rows."""

    def test_empty_tasks(self) -> None:
        """Empty list returns all zeros."""
        result = priority_distribution_of([])
        assert result.critical == 0
        assert result.high == 0
        assert result.medium == 0
        assert result.low == 0
        assert result.someday == 0


class TestComputeAssigneeStats:
    """Assignee totals rendered from grouped task rows."""

    def test_empty_tasks(self) -> None:
        """Empty list returns empty stats."""
        result = assignee_stats_of([])
        assert result == []

    def test_single_assignee(self) -> None:
        """Stats for single assignee."""
        tasks = [
            {"metadata": {"assignees": ["alice"], "status": "todo"}},
            {"metadata": {"assignees": ["alice"], "status": "doing"}},
            {"metadata": {"assignees": ["alice"], "status": "done"}},
        ]
        result = assignee_stats_of(tasks)
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
        result = assignee_stats_of(tasks)
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
        result = assignee_stats_of(tasks)
        assert len(result) == 2
        assert all(s.total == 1 and s.completed == 1 for s in result)

    def test_string_assignee_converted_to_list(self) -> None:
        """String assignee is handled (legacy format)."""
        tasks = [
            {"metadata": {"assignees": "alice", "status": "todo"}},
        ]
        result = assignee_stats_of(tasks)
        assert len(result) == 1
        assert result[0].name == "alice"

    def test_empty_assignee_ignored(self) -> None:
        """Empty assignee values are ignored."""
        tasks = [
            {"metadata": {"assignees": [""], "status": "todo"}},
            {"metadata": {"assignees": [], "status": "todo"}},
        ]
        result = assignee_stats_of(tasks)
        assert result == []


class TestComputeVelocityTrend:
    """Daily completions rendered from grouped task rows."""

    def test_empty_tasks(self) -> None:
        """Empty list returns trend with zeros."""
        result = velocity_trend_of([], days=7)
        assert len(result) == 7
        assert all(p.value == 0 for p in result)

    def test_trend_sorted_by_date(self) -> None:
        """Trend is sorted by date ascending."""
        result = velocity_trend_of([], days=3)
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
        result = velocity_trend_of(tasks, days=7)

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
        result = velocity_trend_of(tasks, days=7)
        assert all(p.value == 0 for p in result)

    def test_old_completions_ignored(self) -> None:
        """Completions older than days are ignored."""
        now = datetime.now(UTC)
        old_date = (now - timedelta(days=30)).isoformat()

        tasks = [
            {"metadata": {"status": "done", "completed_at": old_date}},
        ]
        result = velocity_trend_of(tasks, days=7)
        assert all(p.value == 0 for p in result)


class TestCountRecentTasks:
    """Tasks created in the last week, from grouped task rows."""

    def test_empty_tasks(self) -> None:
        """Empty list returns zero."""
        assert created_last_week_of([]) == 0

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
        assert created_last_week_of(tasks) == 2

    def test_metadata_field_checked(self) -> None:
        """Field can be in metadata."""
        now = datetime.now(UTC)
        recent = (now - timedelta(days=1)).isoformat()

        tasks = [
            {"metadata": {"created_at": recent}},
        ]
        assert created_last_week_of(tasks) == 1

    def test_datetime_objects_are_counted(self) -> None:
        """Native datetime values count as recent activity."""
        now = datetime.now(UTC)

        tasks = [
            {"created_at": now - timedelta(days=1)},
            {"created_at": now - timedelta(days=2)},
        ]
        assert created_last_week_of(tasks) == 2


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


@pytest.fixture(autouse=True)
def _allow_project_access():
    """Project membership is asserted in test_wire_scope; stub it here."""
    with patch(
        "sibyl.api.routes.metrics.verify_entity_project_access",
        AsyncMock(return_value=None),
    ):
        yield


def create_mock_org(org_id: str = "test-org-123") -> MagicMock:
    """Create a mock organization."""
    org = MagicMock()
    org.id = org_id
    return org


def rollup_payload(
    *,
    tasks: list[tuple[str | None, str | None, str | None, int]] = (),
    assignees: list[tuple[str | None, object, str | None, int]] = (),
    created: list[tuple[str | None, int]] = (),
    completions: list[tuple[str | None, object, object]] = (),
    due_dates: list[tuple[str | None, str | None, object]] = (),
) -> list[dict[str, object]]:
    """Shape the grouped statement's RETURN payload the way the driver hands it back."""
    return [
        {
            "tasks": [
                {"project_id": project, "status": status, "priority": priority, "n": count}
                for project, status, priority, count in tasks
            ],
            "assignees": [
                {"project_id": project, "assignees": names, "status": status, "n": count}
                for project, names, status, count in assignees
            ],
            "created": [{"project_id": project, "n": count} for project, count in created],
            "completions": [
                {"project_id": project, "completed_at": completed_at, "updated_at": updated_at}
                for project, completed_at, updated_at in completions
            ],
            "due_dates": [
                {"project_id": project, "status": status, "due_date": due_date}
                for project, status, due_date in due_dates
            ],
        }
    ]


def mock_project_service(*projects: Any) -> AsyncMock:
    service = AsyncMock()
    service.list_entities = AsyncMock(return_value=Page(items=list(projects), next_cursor=None))
    return service


class TestRollupPayload:
    """The grouped statement's payload becomes typed rollups."""

    def test_reads_every_section_and_defaults_missing_values(self) -> None:
        now = datetime.now(UTC)
        rollups = rollups_from_surreal_payload(
            rollup_payload(
                tasks=[("proj_a", None, None, 2), (None, "Doing", "HIGH", 1)],
                assignees=[("proj_a", ["alice", ""], "done", 2), ("proj_a", "bob", "doing", 1)],
                created=[("proj_a", 4)],
                completions=[("proj_a", "not-a-date", now), ("proj_a", None, None)],
                due_dates=[("proj_a", "todo", "not-a-date"), ("proj_a", "todo", now)],
            )[0]
        )

        assert rollups.tasks == (
            TaskRollup("proj_a", "backlog", "medium", 2),
            TaskRollup("", "doing", "high", 1),
        )
        assert [(row.assignee, row.count) for row in rollups.assignees] == [
            ("alice", 2),
            ("bob", 1),
        ]
        assert rollups.created == (("proj_a", 4),)
        assert [row.completed_at for row in rollups.completions] == [now]
        assert [row.due_date for row in rollups.due_dates] == [now]
        assert rollups.total_tasks == 3

    def test_scoping_keeps_only_accessible_projects(self) -> None:
        rollups = rollups_from_surreal_payload(
            rollup_payload(
                tasks=[
                    ("proj_a", "todo", "high", 2),
                    ("proj_b", "todo", "high", 3),
                    (None, "todo", "low", 1),
                ],
                created=[("proj_a", 1), ("proj_b", 1)],
            )[0]
        )

        scoped = rollups.scoped({"proj_a"})

        assert scoped.total_tasks == 2
        assert scoped.created == (("proj_a", 1),)
        assert rollups.scoped(None) is rollups


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
        ):
            with pytest.raises(HTTPException) as exc_info:
                await get_project_metrics(
                    "nonexistent", org=mock_org, ctx=MagicMock(spec=AuthContext)
                )

            assert exc_info.value.status_code == 404
            assert "not found" in exc_info.value.detail.lower()

    @pytest.mark.asyncio
    async def test_project_metrics_success(self) -> None:
        """Returns metrics for valid project from the organization rollups."""
        from sibyl.api.routes.metrics import get_project_metrics

        mock_org = create_mock_org()
        mock_service = AsyncMock()
        mock_service.get_entity.return_value = create_mock_entity(
            entity_type="project", name="Test Project", entity_id="proj_123"
        )
        now = datetime.now(UTC)
        payload = rollup_payload(
            tasks=[
                ("proj_123", "done", "high", 1),
                ("proj_123", "doing", "medium", 1),
                ("proj_other", "todo", "critical", 7),
            ],
            assignees=[
                ("proj_123", ["alice"], "done", 1),
                ("proj_123", ["bob"], "doing", 1),
                ("proj_other", ["carol"], "todo", 7),
            ],
            completions=[("proj_123", (now - timedelta(days=1)).isoformat(), now)],
        )
        execute = AsyncMock(return_value=payload)

        with (
            patch(
                "sibyl.api.routes.metrics.get_knowledge_read_adapter",
                AsyncMock(return_value=mock_service),
            ),
            patch("sibyl.api.routes.metrics.execute_surreal_graph_query", execute),
        ):
            result = await get_project_metrics(
                "proj_123", org=mock_org, ctx=MagicMock(spec=AuthContext)
            )

        assert result.metrics.project_id == "proj_123"
        assert result.metrics.project_name == "Test Project"
        assert result.metrics.total_tasks == 2  # Only proj_123 tasks
        assert result.metrics.status_distribution.done == 1
        assert result.metrics.status_distribution.doing == 1
        assert result.metrics.priority_distribution.high == 1
        assert result.metrics.priority_distribution.medium == 1
        assert result.metrics.priority_distribution.critical == 0
        assert [assignee.name for assignee in result.metrics.assignees] == ["alice", "bob"]
        assert result.metrics.completion_rate == 50.0
        assert result.metrics.tasks_completed_last_7d == 1
        execute.assert_awaited_once()
        mock_service.list_entities.assert_not_called()

    @pytest.mark.asyncio
    async def test_project_metrics_empty_tasks(self) -> None:
        """Returns metrics with zero tasks."""
        from sibyl.api.routes.metrics import get_project_metrics

        mock_org = create_mock_org()
        mock_service = AsyncMock()
        mock_service.get_entity.return_value = create_mock_entity(
            entity_type="project", name="Empty Project", entity_id="proj_empty"
        )

        with (
            patch(
                "sibyl.api.routes.metrics.get_knowledge_read_adapter",
                AsyncMock(return_value=mock_service),
            ),
            patch(
                "sibyl.api.routes.metrics.execute_surreal_graph_query",
                AsyncMock(return_value=rollup_payload()),
            ),
        ):
            result = await get_project_metrics(
                "proj_empty", org=mock_org, ctx=MagicMock(spec=AuthContext)
            )

        assert result.metrics.total_tasks == 0
        assert result.metrics.completion_rate == 0.0
        assert len(result.metrics.velocity_trend) == 14

    @pytest.mark.asyncio
    async def test_project_metrics_fallback_pages_every_task_row(self) -> None:
        """Without the grouped statement, project metrics group paged rows."""
        from sibyl.api.routes.metrics import get_project_metrics

        mock_org = create_mock_org()
        mock_service = AsyncMock()
        mock_service.get_entity.return_value = create_mock_entity(
            entity_type="project", name="Big Project", entity_id="proj_big"
        )
        first_page = [
            create_mock_entity(
                entity_type="task",
                name=f"Task {index}",
                entity_id=f"task_{index:04}",
                metadata={"status": "todo", "priority": "low", "project_id": "proj_big"},
            )
            for index in range(1000)
        ]
        second_page = [
            create_mock_entity(
                entity_type="task",
                name="Done task",
                entity_id="task_done",
                metadata={"status": "done", "priority": "high", "project_id": "proj_big"},
            ),
            create_mock_entity(
                entity_type="task",
                name="Other project",
                entity_id="task_other",
                metadata={"status": "done", "priority": "high", "project_id": "proj_other"},
            ),
        ]
        mock_service.list_entities = AsyncMock(
            side_effect=[
                Page(items=first_page, next_cursor="cursor-2"),
                Page(items=second_page, next_cursor=None),
            ]
        )

        with (
            patch(
                "sibyl.api.routes.metrics.get_knowledge_read_adapter",
                AsyncMock(return_value=mock_service),
            ),
            patch(
                "sibyl.api.routes.metrics._load_surreal_task_rollups",
                AsyncMock(return_value=None),
            ),
        ):
            result = await get_project_metrics(
                "proj_big", org=mock_org, ctx=MagicMock(spec=AuthContext)
            )

        assert result.metrics.total_tasks == 1001
        assert result.metrics.status_distribution.done == 1
        assert result.metrics.status_distribution.todo == 1000
        assert result.metrics.priority_distribution.high == 1
        assert result.metrics.priority_distribution.low == 1000
        assert mock_service.list_entities.await_args_list == [
            call(EntityType.TASK, limit=1000, cursor=None),
            call(EntityType.TASK, limit=1000, cursor="cursor-2"),
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
                "sibyl.api.routes.metrics._load_surreal_task_rollups",
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
    async def test_org_metrics_renders_the_grouped_statement(self) -> None:
        """Organization metrics come from grouped rows, never from task rows."""
        from sibyl.api.routes.metrics import get_org_metrics

        mock_org = create_mock_org()
        now = datetime.now(UTC)
        mock_service = mock_project_service(
            create_mock_entity(entity_type="project", name="Project A", entity_id="proj_a"),
            create_mock_entity(entity_type="project", name="Project B", entity_id="proj_b"),
        )
        execute = AsyncMock(
            return_value=rollup_payload(
                tasks=[
                    ("proj_a", "done", "critical", 3),
                    ("proj_a", "todo", "high", 2),
                    ("proj_b", "doing", "high", 1),
                    (None, "backlog", None, 4),
                ],
                assignees=[
                    ("proj_a", ["alice"], "done", 3),
                    ("proj_a", ["alice", "bob"], "todo", 2),
                    ("proj_b", "bob", "doing", 1),
                ],
                created=[("proj_a", 2), (None, 1)],
                completions=[
                    ("proj_a", (now - timedelta(days=1)).isoformat(), now),
                    ("proj_a", None, now - timedelta(days=2)),
                    ("proj_a", (now - timedelta(days=30)).isoformat(), now),
                ],
                due_dates=[
                    ("proj_a", "todo", (now - timedelta(days=1)).isoformat()),
                    ("proj_a", "todo", (now + timedelta(days=1)).isoformat()),
                    ("proj_b", "done", (now - timedelta(days=1)).isoformat()),
                ],
            )
        )

        with (
            patch(
                "sibyl.api.routes.metrics.get_knowledge_read_adapter",
                AsyncMock(return_value=mock_service),
            ),
            patch("sibyl.api.routes.metrics.execute_surreal_graph_query", execute),
        ):
            result = await get_org_metrics(org=mock_org)

        assert mock_service.list_entities.await_args_list == [
            call(EntityType.PROJECT, limit=500, cursor=None),
        ]
        execute.assert_awaited_once()
        assert result.total_projects == 2
        assert result.total_tasks == 10
        assert result.status_distribution.done == 3
        assert result.status_distribution.todo == 2
        assert result.status_distribution.doing == 1
        assert result.status_distribution.backlog == 4
        assert result.priority_distribution.critical == 3
        assert result.priority_distribution.high == 3
        assert result.priority_distribution.medium == 4
        assert result.completion_rate == 30.0
        assert [(a.name, a.total, a.completed, a.in_progress) for a in result.top_assignees] == [
            ("alice", 5, 3, 0),
            ("bob", 3, 0, 1),
        ]
        assert result.tasks_created_last_7d == 3
        assert result.tasks_completed_last_7d == 2
        assert sum(point.value for point in result.velocity_trend) == 2
        summary = {item.id: item for item in result.projects_summary}
        assert summary["proj_a"].total == 5
        assert summary["proj_a"].completed == 3
        assert summary["proj_a"].high == 2
        assert summary["proj_a"].critical == 0
        assert summary["proj_a"].overdue == 1
        assert summary["proj_b"].doing == 1
        assert summary["proj_b"].overdue == 0

    @pytest.mark.asyncio
    async def test_org_metrics_issues_one_grouped_statement(self) -> None:
        """The task scan is gone: one grouped statement, no row cap, bounded output."""
        from sibyl.api.routes.metrics import get_org_metrics

        mock_org = create_mock_org()
        execute = AsyncMock(return_value=rollup_payload(tasks=[("proj_a", "todo", "high", 2500)]))

        with (
            patch(
                "sibyl.api.routes.metrics.get_knowledge_read_adapter",
                AsyncMock(return_value=mock_project_service()),
            ),
            patch("sibyl.api.routes.metrics.execute_surreal_graph_query", execute),
        ):
            result = await get_org_metrics(org=mock_org)

        assert result.total_tasks == 2500
        execute.assert_awaited_once()
        group_id, statement = execute.await_args.args
        assert group_id == str(mock_org.id)
        assert statement == TASK_ROLLUP_STATEMENT
        assert "GROUP BY project_id, status, priority" in statement
        assert "LIMIT" not in statement
        kwargs = execute.await_args.kwargs
        assert kwargs["task_type"] == EntityType.TASK.value
        assert isinstance(kwargs["created_cutoff"], datetime)
        assert isinstance(kwargs["completed_cutoff"], datetime)
        assert kwargs["created_cutoff"] > kwargs["completed_cutoff"]

    @pytest.mark.asyncio
    async def test_concurrent_dashboards_compute_rollups_once(self) -> None:
        """Ten tabs refetching at once share one grouped statement."""
        from sibyl.api.routes.metrics import get_org_metrics

        mock_org = create_mock_org()
        gate = asyncio.Event()

        async def execute(*_args: Any, **_kwargs: Any) -> list[dict[str, object]]:
            await gate.wait()
            return rollup_payload(tasks=[("proj_a", "todo", "high", 1)])

        execute_mock = AsyncMock(side_effect=execute)

        with (
            patch(
                "sibyl.api.routes.metrics.get_knowledge_read_adapter",
                AsyncMock(return_value=mock_project_service()),
            ),
            patch("sibyl.api.routes.metrics.execute_surreal_graph_query", execute_mock),
        ):
            requests = [asyncio.create_task(get_org_metrics(org=mock_org)) for _ in range(10)]
            await asyncio.sleep(0)
            gate.set()
            results = await asyncio.gather(*requests)
            # A later refetch inside the window is served from the memo too.
            later = await get_org_metrics(org=mock_org)

        assert execute_mock.await_count == 1
        assert all(result.total_tasks == 1 for result in results)
        assert later.total_tasks == 1

    @pytest.mark.asyncio
    async def test_task_broadcast_drops_the_memoized_rollups(self) -> None:
        """The same broadcast that makes tabs refetch outdates the memo first."""
        from sibyl.api.routes.metrics import get_org_metrics

        mock_org = create_mock_org()
        execute = AsyncMock(
            side_effect=[
                rollup_payload(tasks=[("proj_a", "todo", "high", 1)]),
                rollup_payload(tasks=[("proj_a", "todo", "high", 2)]),
            ]
        )

        with (
            patch(
                "sibyl.api.routes.metrics.get_knowledge_read_adapter",
                AsyncMock(return_value=mock_project_service()),
            ),
            patch("sibyl.api.routes.metrics.execute_surreal_graph_query", execute),
        ):
            before = await get_org_metrics(org=mock_org)
            await ConnectionManager().broadcast(
                "health_update", {"status": "ok"}, org_id=str(mock_org.id)
            )
            unchanged = await get_org_metrics(org=mock_org)
            await ConnectionManager().broadcast(
                "entity_updated", {"id": "task_1", "entity_type": "task"}, org_id=str(mock_org.id)
            )
            after = await get_org_metrics(org=mock_org)

        assert (before.total_tasks, unchanged.total_tasks, after.total_tasks) == (1, 1, 2)
        assert execute.await_count == 2

    @pytest.mark.asyncio
    async def test_org_metrics_rejects_unbounded_service_task_enumeration(self) -> None:
        """Returns 413 when paged task enumeration exceeds metrics safety cap."""
        from sibyl.api.routes.metrics import METRICS_MAX_TASKS, get_org_metrics

        mock_org = create_mock_org()
        mock_service = AsyncMock()

        mock_projects = [
            create_mock_entity(entity_type="project", name="Project A", entity_id="proj_a"),
        ]
        first_page = [
            create_mock_entity(
                entity_type="task",
                name=f"Task {index}",
                entity_id=f"task_{index}",
                metadata={"project_id": "proj_a", "status": "todo"},
            )
            for index in range(METRICS_MAX_TASKS)
        ]
        overflow_page = [
            create_mock_entity(
                entity_type="task",
                name="Overflow Task",
                entity_id="task_overflow",
                metadata={"project_id": "proj_a", "status": "todo"},
            )
        ]

        mock_service.list_entities = AsyncMock(
            side_effect=[
                Page(items=mock_projects, next_cursor=None),
                Page(items=first_page, next_cursor="cursor-2"),
                Page(items=overflow_page, next_cursor=None),
            ]
        )

        with (
            patch(
                "sibyl.api.routes.metrics.get_knowledge_read_adapter",
                AsyncMock(return_value=mock_service),
            ),
            patch(
                "sibyl.api.routes.metrics._load_surreal_task_rollups",
                AsyncMock(return_value=None),
            ),
            pytest.raises(HTTPException) as exc_info,
        ):
            await get_org_metrics(org=mock_org)

        assert exc_info.value.status_code == 413

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
                "sibyl.api.routes.metrics._load_surreal_task_rollups",
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
                "sibyl.api.routes.metrics._load_surreal_task_rollups",
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
                "sibyl.api.routes.metrics._load_surreal_task_rollups",
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
                "sibyl.api.routes.metrics._load_surreal_task_rollups",
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
                "sibyl.api.routes.metrics._load_surreal_task_rollups",
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
                "sibyl.api.routes.metrics._load_surreal_task_rollups",
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
                "sibyl.api.routes.metrics._load_surreal_task_rollups",
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
    async def test_project_summaries_render_the_grouped_statement(self) -> None:
        """Surreal-backed summaries read grouped rows without paging task entities."""
        from sibyl.api.routes.metrics import get_project_summaries

        mock_org = create_mock_org()
        mock_service = mock_project_service(
            create_mock_entity(entity_type="project", name="Project A", entity_id="proj_a"),
            create_mock_entity(entity_type="project", name="Project B", entity_id="proj_b"),
        )
        execute = AsyncMock(
            return_value=rollup_payload(
                tasks=[
                    ("proj_b", "doing", "critical", 1),
                    ("proj_a", "done", "high", 1),
                    ("proj_a", "todo", "high", 1),
                ]
            )
        )

        with (
            patch(
                "sibyl.api.routes.metrics.get_knowledge_read_adapter",
                AsyncMock(return_value=mock_service),
            ),
            patch("sibyl.api.routes.metrics.execute_surreal_graph_query", execute),
        ):
            result = await get_project_summaries(org=mock_org)

        assert mock_service.list_entities.await_args_list == [
            call(EntityType.PROJECT, limit=500, cursor=None),
        ]
        execute.assert_awaited_once()
        assert execute.await_args.args == (str(mock_org.id), TASK_ROLLUP_STATEMENT)
        assert "GROUP BY project_id, status, priority" in TASK_ROLLUP_STATEMENT
        assert (
            "string::lowercase(attributes.status ?? status ?? '') != 'archived'"
            in TASK_ROLLUP_STATEMENT
        )
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
                "sibyl.api.routes.metrics._load_surreal_task_rollups",
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
                "sibyl.api.routes.metrics._load_surreal_task_rollups",
                side_effect=Exception("Database error"),
            ),
        ):
            with pytest.raises(HTTPException) as exc_info:
                await get_project_metrics("proj_123", org=mock_org, ctx=MagicMock(spec=AuthContext))

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
