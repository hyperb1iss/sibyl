"""Tests for sibyl_core.tools.admin."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, call, patch

import pytest

from sibyl_core.models.entities import EntityType
from sibyl_core.tools.admin import (
    backfill_task_project_relationships,
    get_stats,
    health_check,
    rebuild_indices,
)


class TestRebuildIndices:
    """Admin index rebuilds should report real behavior, not placeholder success."""

    @pytest.mark.asyncio
    async def test_rebuild_indices_reports_not_implemented(self) -> None:
        """The current runtime should fail honestly until rebuild support exists."""
        result = await rebuild_indices("search")

        assert result.success is False
        assert result.indices_rebuilt == []
        assert "not implemented" in result.message.lower()
        assert "search" in result.message

    @pytest.mark.asyncio
    async def test_rebuild_indices_rejects_unknown_target(self) -> None:
        """Unknown targets should return a clear validation error."""
        result = await rebuild_indices("mystery")

        assert result.success is False
        assert result.indices_rebuilt == []
        assert "unknown index type" in result.message.lower()

    @pytest.mark.asyncio
    async def test_rebuild_indices_normalizes_target_values(self) -> None:
        """Whitespace and casing should normalize before reporting."""
        result = await rebuild_indices(" ALL ")

        assert result.success is False
        assert result.indices_rebuilt == []
        assert "requested target: all" in result.message.lower()


class TestHealthAndStats:
    """Admin health/stat helpers should use aggregated counts."""

    @pytest.mark.asyncio
    async def test_health_check_uses_single_count_query(self) -> None:
        """health_check should aggregate entity counts instead of listing entities per type."""
        org_id = "00000000-0000-0000-0000-000000000111"
        client = AsyncMock()
        client.execute_read_org = AsyncMock(
            return_value=[
                {"type": "pattern", "count": 3},
                {"type": "task", "count": 2},
            ]
        )
        entity_manager = AsyncMock()
        entity_manager.search = AsyncMock(return_value=[])

        with (
            patch(
                "sibyl_core.tools.admin.get_graph_client",
                AsyncMock(return_value=client),
            ),
            patch(
                "sibyl_core.tools.admin.EntityManager",
                return_value=entity_manager,
            ),
        ):
            result = await health_check(organization_id=org_id)

        assert result.graph_connected is True
        assert result.entity_counts["pattern"] == 3
        assert result.entity_counts["task"] == 2
        assert result.entity_counts["episode"] == 0
        client.execute_read_org.assert_awaited_once()
        args = client.execute_read_org.await_args
        assert "toLower(n.status) <> 'archived'" in args.args[0]
        assert 'toLower(toString(n.metadata)) CONTAINS \'"status":"archived"\'' in args.args[0]
        assert args.args[1] == org_id
        assert args.kwargs["group_id"] == org_id
        entity_manager.search.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_get_stats_uses_aggregated_counts(self) -> None:
        """get_stats should sum the aggregation query results directly."""
        org_id = "00000000-0000-0000-0000-000000000111"
        client = AsyncMock()
        client.execute_read_org = AsyncMock(
            return_value=[
                {"type": "pattern", "count": 3},
                {"type": "task", "count": 2},
            ]
        )

        with patch(
            "sibyl_core.tools.admin.get_graph_client",
            AsyncMock(return_value=client),
        ):
            stats = await get_stats(organization_id=org_id)

        assert stats["entities"]["pattern"] == 3
        assert stats["entities"]["task"] == 2
        assert stats["entities"]["episode"] == 0
        assert stats["total_entities"] == 5
        client.execute_read_org.assert_awaited_once()
        query = client.execute_read_org.await_args.args[0]
        assert "toLower(n.status) <> 'archived'" in query


class TestBackfillTaskProjectRelationships:
    """Project validation should page through the full project set."""

    @pytest.mark.asyncio
    async def test_backfill_task_project_relationships_pages_task_batches(self) -> None:
        """Tasks beyond the first page should still be processed."""
        org_id = "00000000-0000-0000-0000-000000000111"
        client = AsyncMock()
        entity_manager = AsyncMock()
        relationship_manager = AsyncMock()

        page_size = 2
        task_page_one = [
            SimpleNamespace(id="task-1", metadata={"project_id": "project-1"}),
            SimpleNamespace(id="task-2", metadata={"project_id": "project-1"}),
        ]
        task_page_two = [SimpleNamespace(id="task-3", metadata={"project_id": "project-1"})]
        project_page_one = [SimpleNamespace(id="project-1")]

        async def list_by_type(
            entity_type: EntityType, limit: int = 50, offset: int = 0, **_: object
        ) -> list[SimpleNamespace]:
            assert limit == page_size
            if entity_type == EntityType.TASK:
                if offset == 0:
                    return task_page_one
                if offset == page_size:
                    return task_page_two
                return []
            if entity_type == EntityType.PROJECT:
                if offset == 0:
                    return project_page_one
                if offset == page_size:
                    return []
            raise AssertionError(f"Unexpected page request: {entity_type=} {offset=}")

        entity_manager.list_by_type = AsyncMock(side_effect=list_by_type)
        relationship_manager.get_for_entity = AsyncMock(return_value=[])

        with (
            patch(
                "sibyl_core.tools.admin.get_graph_client",
                AsyncMock(return_value=client),
            ),
            patch(
                "sibyl_core.tools.admin.EntityManager",
                return_value=entity_manager,
            ),
            patch(
                "sibyl_core.tools.admin.RelationshipManager",
                return_value=relationship_manager,
            ),
            patch("sibyl_core.tools.admin.BACKFILL_PAGE_SIZE", page_size),
        ):
            result = await backfill_task_project_relationships(organization_id=org_id, dry_run=True)

        assert result.relationships_created == 3
        assert result.errors == []
        assert result.tasks_without_project == 0
        assert result.tasks_already_linked == 0
        assert relationship_manager.get_for_entity.await_count == 3
        entity_manager.list_by_type.assert_has_awaits(
            [
                call(EntityType.TASK, limit=page_size, offset=0),
                call(EntityType.TASK, limit=page_size, offset=page_size),
                call(EntityType.PROJECT, limit=page_size, offset=0, include_archived=True),
            ],
            any_order=False,
        )

    @pytest.mark.asyncio
    async def test_backfill_task_project_relationships_pages_project_validation(self) -> None:
        """Projects beyond the first 1000 should still validate task metadata."""
        org_id = "00000000-0000-0000-0000-000000000111"
        client = AsyncMock()
        entity_manager = AsyncMock()
        relationship_manager = AsyncMock()

        task = SimpleNamespace(id="task-1", metadata={"project_id": "project-1001"})
        first_page = [SimpleNamespace(id=f"project-{i}") for i in range(1000)]
        second_page = [SimpleNamespace(id="project-1001")]
        entity_manager.list_by_type = AsyncMock(side_effect=[[task], first_page, second_page, []])
        relationship_manager.get_for_entity = AsyncMock(return_value=[])

        with (
            patch(
                "sibyl_core.tools.admin.get_graph_client",
                AsyncMock(return_value=client),
            ),
            patch(
                "sibyl_core.tools.admin.EntityManager",
                return_value=entity_manager,
            ),
            patch(
                "sibyl_core.tools.admin.RelationshipManager",
                return_value=relationship_manager,
            ),
        ):
            result = await backfill_task_project_relationships(organization_id=org_id, dry_run=True)

        assert result.relationships_created == 1
        assert result.errors == []
        assert result.tasks_without_project == 0
        assert result.tasks_already_linked == 0
        entity_manager.list_by_type.assert_any_await(
            ANY, limit=1000, offset=0, include_archived=True
        )
        entity_manager.list_by_type.assert_any_await(
            ANY, limit=1000, offset=1000, include_archived=True
        )
