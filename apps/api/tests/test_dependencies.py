"""Tests for graph-related FastAPI dependencies.

These tests verify the dependency injection patterns work correctly
for EntityManager and RelationshipManager provisioning.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from sibyl.api.dependencies import (
    get_entity_manager,
    get_graph,
    get_group_id,
    get_relationship_manager,
)


# =============================================================================
# Fixtures
# =============================================================================
@pytest.fixture
def mock_org() -> MagicMock:
    """Create a mock Organization."""
    org = MagicMock()
    org.id = uuid4()
    return org


@pytest.fixture
def mock_graph_client() -> AsyncMock:
    """Create a mock GraphClient."""
    return AsyncMock()


# =============================================================================
# get_graph Tests
# =============================================================================
class TestGetGraph:
    """Tests for get_graph dependency."""

    @pytest.mark.asyncio
    async def test_returns_graph_client(self) -> None:
        """Returns a GraphClient from get_graph_client."""
        mock_client = AsyncMock()

        with patch(
            "sibyl.api.dependencies.get_graph_client",
            return_value=mock_client,
        ):
            result = await get_graph()
            assert result is mock_client

    @pytest.mark.asyncio
    async def test_calls_get_graph_client(self) -> None:
        """Calls get_graph_client to obtain client."""
        mock_client = AsyncMock()

        with patch(
            "sibyl.api.dependencies.get_graph_client",
            return_value=mock_client,
        ) as mock_get:
            await get_graph()
            mock_get.assert_called_once()


# =============================================================================
# get_entity_manager Tests
# =============================================================================
class TestGetEntityManager:
    """Tests for get_entity_manager dependency."""

    @pytest.mark.asyncio
    async def test_returns_entity_manager(
        self,
        mock_org: MagicMock,
        mock_graph_client: AsyncMock,
    ) -> None:
        """Returns an EntityManager instance."""
        with patch(
            "sibyl.api.dependencies.get_graph_client",
            return_value=mock_graph_client,
        ):
            result = await get_entity_manager(org=mock_org)

        # Import here to avoid circular import issues
        from sibyl_core.graph import EntityManager

        assert isinstance(result, EntityManager)

    @pytest.mark.asyncio
    async def test_uses_org_id_as_group_id(
        self,
        mock_org: MagicMock,
        mock_graph_client: AsyncMock,
    ) -> None:
        """Uses organization ID as the group_id for graph scoping."""
        with patch(
            "sibyl.api.dependencies.get_graph_client",
            return_value=mock_graph_client,
        ):
            result = await get_entity_manager(org=mock_org)

        assert result._group_id == str(mock_org.id)

    @pytest.mark.asyncio
    async def test_uses_provided_graph_client(
        self,
        mock_org: MagicMock,
        mock_graph_client: AsyncMock,
    ) -> None:
        """EntityManager is configured with the graph client."""
        with patch(
            "sibyl.api.dependencies.get_graph_client",
            return_value=mock_graph_client,
        ):
            result = await get_entity_manager(org=mock_org)

        assert result._client is mock_graph_client


# =============================================================================
# get_relationship_manager Tests
# =============================================================================
class TestGetRelationshipManager:
    """Tests for get_relationship_manager dependency."""

    @pytest.mark.asyncio
    async def test_returns_relationship_manager(
        self,
        mock_org: MagicMock,
        mock_graph_client: AsyncMock,
    ) -> None:
        """Returns a RelationshipManager instance."""
        with patch(
            "sibyl.api.dependencies.get_graph_client",
            return_value=mock_graph_client,
        ):
            result = await get_relationship_manager(org=mock_org)

        from sibyl_core.graph import RelationshipManager

        assert isinstance(result, RelationshipManager)

    @pytest.mark.asyncio
    async def test_uses_org_id_as_group_id(
        self,
        mock_org: MagicMock,
        mock_graph_client: AsyncMock,
    ) -> None:
        """Uses organization ID as the group_id for graph scoping."""
        with patch(
            "sibyl.api.dependencies.get_graph_client",
            return_value=mock_graph_client,
        ):
            result = await get_relationship_manager(org=mock_org)

        assert result._group_id == str(mock_org.id)


# =============================================================================
# get_group_id Tests
# =============================================================================
class TestGetGroupId:
    """Tests for get_group_id dependency."""

    @pytest.mark.asyncio
    async def test_returns_string(self, mock_org: MagicMock) -> None:
        """Returns organization ID as string."""
        result = await get_group_id(org=mock_org)
        assert isinstance(result, str)

    @pytest.mark.asyncio
    async def test_returns_org_id(self, mock_org: MagicMock) -> None:
        """Returns the organization's ID."""
        result = await get_group_id(org=mock_org)
        assert result == str(mock_org.id)


# =============================================================================
# Integration Pattern Tests
# =============================================================================
class TestDependencyPatterns:
    """Tests demonstrating intended usage patterns."""

    @pytest.mark.asyncio
    async def test_multiple_managers_same_org(
        self,
        mock_org: MagicMock,
        mock_graph_client: AsyncMock,
    ) -> None:
        """Multiple manager types can be created for the same org."""
        with patch(
            "sibyl.api.dependencies.get_graph_client",
            return_value=mock_graph_client,
        ):
            entity_mgr = await get_entity_manager(org=mock_org)
            rel_mgr = await get_relationship_manager(org=mock_org)

        # Both should have same group_id
        assert entity_mgr._group_id == rel_mgr._group_id
        assert entity_mgr._group_id == str(mock_org.id)

    @pytest.mark.asyncio
    async def test_different_orgs_different_scopes(
        self,
        mock_graph_client: AsyncMock,
    ) -> None:
        """Different orgs get different manager scopes."""
        org1 = MagicMock()
        org1.id = uuid4()
        org2 = MagicMock()
        org2.id = uuid4()

        with patch(
            "sibyl.api.dependencies.get_graph_client",
            return_value=mock_graph_client,
        ):
            manager1 = await get_entity_manager(org=org1)
            manager2 = await get_entity_manager(org=org2)

        assert manager1._group_id != manager2._group_id
        assert manager1._group_id == str(org1.id)
        assert manager2._group_id == str(org2.id)
