"""Tests for sibyl_core.tools.admin."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from sibyl_core.tools.admin import get_stats, health_check, rebuild_indices


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
