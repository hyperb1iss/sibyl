"""Tests for graph-related FastAPI dependencies.

These tests verify the dependency injection patterns work correctly
for EntityManager and RelationshipManager provisioning.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from sibyl.api.dependencies import (
    get_entity_manager,
    get_graph_client,
    get_graph_store,
    get_knowledge_read_service,
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


@pytest.fixture
def mock_graph_runtime(mock_org: MagicMock) -> SimpleNamespace:
    """Create a mock native graph runtime scoped to the test org."""
    entity_manager = MagicMock()
    entity_manager._group_id = str(mock_org.id)
    relationship_manager = MagicMock()
    relationship_manager._group_id = str(mock_org.id)
    return SimpleNamespace(
        entity_manager=entity_manager,
        relationship_manager=relationship_manager,
    )


# =============================================================================
# get_graph_client Tests
# =============================================================================
class TestGetGraphClient:
    """Tests for get_graph_client dependency."""

    @pytest.mark.asyncio
    async def test_graph_client_uses_native_service(self) -> None:
        """get_graph_client delegates to the native runtime service."""
        mock_client = AsyncMock()

        with patch(
            "sibyl_core.services.graph_runtime.get_graph_client",
            return_value=mock_client,
        ) as mock_get:
            result = await get_graph_client()

        assert result is mock_client
        mock_get.assert_awaited_once_with()


# =============================================================================
# get_entity_manager Tests
# =============================================================================
class TestGetEntityManager:
    """Tests for get_entity_manager dependency."""

    @pytest.mark.asyncio
    async def test_returns_entity_manager(
        self,
        mock_org: MagicMock,
        mock_graph_runtime: SimpleNamespace,
    ) -> None:
        """Returns the native runtime's entity manager."""
        with patch(
            "sibyl.persistence.graph_runtime.get_entity_graph_runtime",
            return_value=mock_graph_runtime,
        ):
            result = await get_entity_manager(org=mock_org)

        assert result is mock_graph_runtime.entity_manager

    @pytest.mark.asyncio
    async def test_uses_org_id_as_group_id(
        self,
        mock_org: MagicMock,
        mock_graph_runtime: SimpleNamespace,
    ) -> None:
        """Uses organization ID as the group_id for graph scoping."""
        with patch(
            "sibyl.persistence.graph_runtime.get_entity_graph_runtime",
            return_value=mock_graph_runtime,
        ):
            result = await get_entity_manager(org=mock_org)

        assert result._group_id == str(mock_org.id)

    @pytest.mark.asyncio
    async def test_uses_runtime_factory(
        self,
        mock_org: MagicMock,
        mock_graph_runtime: SimpleNamespace,
    ) -> None:
        """EntityManager comes from the org-scoped native runtime."""
        with patch(
            "sibyl.persistence.graph_runtime.get_entity_graph_runtime",
            return_value=mock_graph_runtime,
        ) as get_runtime:
            result = await get_entity_manager(org=mock_org)

        assert result is mock_graph_runtime.entity_manager
        get_runtime.assert_awaited_once_with(str(mock_org.id))


# =============================================================================
# Integration Pattern Tests
# =============================================================================
class TestDependencyPatterns:
    """Tests demonstrating intended usage patterns."""

    @pytest.mark.asyncio
    async def test_different_orgs_different_scopes(
        self,
    ) -> None:
        """Different orgs get different manager scopes."""
        org1 = MagicMock()
        org1.id = uuid4()
        org2 = MagicMock()
        org2.id = uuid4()

        async def runtime_for_group(group_id: str) -> SimpleNamespace:
            entity_manager = MagicMock()
            entity_manager._group_id = group_id
            relationship_manager = MagicMock()
            relationship_manager._group_id = group_id
            return SimpleNamespace(
                entity_manager=entity_manager,
                relationship_manager=relationship_manager,
            )

        with patch(
            "sibyl.persistence.graph_runtime.get_entity_graph_runtime",
            side_effect=runtime_for_group,
        ):
            manager1 = await get_entity_manager(org=org1)
            manager2 = await get_entity_manager(org=org2)

        assert manager1._group_id != manager2._group_id
        assert manager1._group_id == str(org1.id)
        assert manager2._group_id == str(org2.id)


class TestGraphStoreDependencies:
    """Tests for graph store and knowledge read dependencies."""

    @pytest.mark.asyncio
    async def test_get_graph_store_scopes_to_org(self, mock_org: MagicMock) -> None:
        store = MagicMock()

        with patch(
            "sibyl.persistence.graph_runtime.get_graph_store",
            return_value=store,
        ) as factory:
            result = await get_graph_store(org=mock_org)

        assert result is store
        factory.assert_awaited_once_with(str(mock_org.id))

    @pytest.mark.asyncio
    async def test_get_knowledge_read_service_wraps_store(self) -> None:
        store = MagicMock()

        service = await get_knowledge_read_service(graph_store=store)

        assert service._store is store
