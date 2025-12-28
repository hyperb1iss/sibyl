"""Tests for the MCP test harness module.

Verifies that the test harness correctly mocks dependencies
and provides useful helpers for testing MCP tools.
"""

import pytest

from sibyl.models.entities import EntityType
from sibyl.tools.core import AddResponse, ExploreResponse, SearchResponse
from sibyl.tools.manage import ManageResponse
from tests.harness import (
    MockEntityManager,
    MockGraphClient,
    MockRelationshipManager,
    ToolTestContext,
    create_test_entity,
    create_test_relationship,
    mock_tools,
    setup_entity_graph,
    setup_search_results,
    setup_task_workflow,
    validate_add_response,
    validate_explore_response,
    validate_manage_response,
    validate_search_response,
)


class TestMockGraphClient:
    """Tests for MockGraphClient."""

    def test_starts_connected(self) -> None:
        """Client should start in connected state."""
        client = MockGraphClient()
        assert client.connected is True

    @pytest.mark.asyncio
    async def test_disconnect(self) -> None:
        """Disconnect should update connection state."""
        client = MockGraphClient()
        await client.disconnect()
        assert client.connected is False

    @pytest.mark.asyncio
    async def test_reconnect(self) -> None:
        """Connect should restore connection state."""
        client = MockGraphClient()
        await client.disconnect()
        await client.connect()
        assert client.connected is True

    def test_has_graphiti_client(self) -> None:
        """Client should have a mock Graphiti client."""
        client = MockGraphClient()
        assert client.client is not None
        assert client.client.driver is not None


class TestMockEntityManager:
    """Tests for MockEntityManager."""

    @pytest.mark.asyncio
    async def test_create_entity(self) -> None:
        """Create should store entity and return ID."""
        manager = MockEntityManager()
        entity = create_test_entity(name="Test")

        entity_id = await manager.create(entity)

        assert entity_id is not None
        assert entity.id == entity_id

    @pytest.mark.asyncio
    async def test_get_entity(self) -> None:
        """Get should retrieve stored entity."""
        manager = MockEntityManager()
        entity = create_test_entity(name="Test")
        await manager.create(entity)

        retrieved = await manager.get(entity.id)

        assert retrieved.name == "Test"

    @pytest.mark.asyncio
    async def test_get_missing_entity_raises(self) -> None:
        """Get should raise for missing entity."""
        from sibyl.errors import EntityNotFoundError

        manager = MockEntityManager()

        with pytest.raises(EntityNotFoundError):
            await manager.get("nonexistent-id")

    @pytest.mark.asyncio
    async def test_search_returns_configured_results(self) -> None:
        """Search should return pre-configured results."""
        manager = MockEntityManager()
        entity = create_test_entity()
        manager.set_search_results([(entity, 0.9)])

        results = await manager.search("query")

        assert len(results) == 1
        assert results[0][0] == entity
        assert results[0][1] == 0.9

    @pytest.mark.asyncio
    async def test_update_entity(self) -> None:
        """Update should modify entity fields."""
        manager = MockEntityManager()
        entity = create_test_entity(name="Original")
        await manager.create(entity)

        updated = await manager.update(entity.id, {"name": "Updated"})

        assert updated.name == "Updated"

    @pytest.mark.asyncio
    async def test_delete_entity(self) -> None:
        """Delete should remove entity."""
        manager = MockEntityManager()
        entity = create_test_entity()
        await manager.create(entity)

        result = await manager.delete(entity.id)

        assert result is True
        with pytest.raises(Exception):
            await manager.get(entity.id)


class TestMockRelationshipManager:
    """Tests for MockRelationshipManager."""

    @pytest.mark.asyncio
    async def test_create_relationship(self) -> None:
        """Create should store relationship."""
        manager = MockRelationshipManager()
        rel = create_test_relationship("source-1", "target-1")

        rel_id = await manager.create(rel)

        assert rel_id is not None

    @pytest.mark.asyncio
    async def test_get_for_entity_outgoing(self) -> None:
        """Get for entity should return outgoing relationships."""
        manager = MockRelationshipManager()
        rel = create_test_relationship("source-1", "target-1")
        await manager.create(rel)

        results = await manager.get_for_entity("source-1", direction="outgoing")

        assert len(results) == 1

    @pytest.mark.asyncio
    async def test_get_for_entity_incoming(self) -> None:
        """Get for entity should return incoming relationships."""
        manager = MockRelationshipManager()
        rel = create_test_relationship("source-1", "target-1")
        await manager.create(rel)

        results = await manager.get_for_entity("target-1", direction="incoming")

        assert len(results) == 1


class TestToolTestContext:
    """Tests for ToolTestContext."""

    def test_has_all_managers(self) -> None:
        """Context should have all required managers."""
        ctx = ToolTestContext()

        assert ctx.graph_client is not None
        assert ctx.entity_manager is not None
        assert ctx.relationship_manager is not None

    def test_reset_clears_data(self) -> None:
        """Reset should clear all mock data."""
        ctx = ToolTestContext()
        ctx.entity_manager.add_entity(create_test_entity())

        ctx.reset()

        assert len(ctx.entity_manager._entities) == 0


class TestCreateTestEntity:
    """Tests for create_test_entity helper."""

    def test_creates_with_defaults(self) -> None:
        """Should create entity with sensible defaults."""
        entity = create_test_entity()

        assert entity.id is not None
        assert entity.name == "Test Entity"
        assert entity.entity_type == EntityType.EPISODE

    def test_accepts_custom_values(self) -> None:
        """Should accept custom field values."""
        entity = create_test_entity(
            entity_type=EntityType.TASK,
            name="Custom Name",
            description="Custom description",
        )

        assert entity.entity_type == EntityType.TASK
        assert entity.name == "Custom Name"
        assert entity.description == "Custom description"


class TestSetupHelpers:
    """Tests for setup helper functions."""

    def test_setup_search_results(self) -> None:
        """Should configure search results on context."""
        ctx = ToolTestContext()

        entities = setup_search_results(ctx, count=5)

        assert len(entities) == 5
        assert len(ctx.entity_manager._search_results) == 5

    def test_setup_entity_graph(self) -> None:
        """Should create entity graph structure."""
        ctx = ToolTestContext()

        root, children = setup_entity_graph(ctx, child_count=3)

        assert root is not None
        assert len(children) == 3
        assert len(ctx.entity_manager._entities) == 4  # root + 3 children
        assert len(ctx.relationship_manager._relationships) == 3

    def test_setup_task_workflow(self) -> None:
        """Should create task entities for workflow testing."""
        ctx = ToolTestContext()

        tasks = setup_task_workflow(ctx, task_count=2)

        assert len(tasks) == 2
        for task in tasks:
            assert task.entity_type == EntityType.TASK


class TestValidators:
    """Tests for response validators."""

    def test_validate_search_response_valid(self) -> None:
        """Should return empty list for valid response."""

        response = SearchResponse(results=[], total=0, query="test", filters={})

        errors = validate_search_response(response)

        assert errors == []

    def test_validate_explore_response_valid(self) -> None:
        """Should return empty list for valid response."""
        response = ExploreResponse(mode="list", entities=[], total=0, filters={})

        errors = validate_explore_response(response)

        assert errors == []

    def test_validate_add_response_valid(self) -> None:
        """Should return empty list for valid response."""
        from datetime import UTC, datetime

        response = AddResponse(
            success=True,
            id="test-123",
            message="Created",
            timestamp=datetime.now(UTC),
        )

        errors = validate_add_response(response)

        assert errors == []

    def test_validate_manage_response_valid(self) -> None:
        """Should return empty list for valid response."""
        response = ManageResponse(success=True, action="test", message="Done")

        errors = validate_manage_response(response)

        assert errors == []


class TestMockToolsContextManager:
    """Tests for mock_tools context manager."""

    @pytest.mark.asyncio
    async def test_provides_context(self) -> None:
        """Should yield ToolTestContext."""
        async with mock_tools() as ctx:
            assert isinstance(ctx, ToolTestContext)

    @pytest.mark.asyncio
    async def test_context_is_usable(self) -> None:
        """Context should be configured for testing."""
        async with mock_tools() as ctx:
            entity = create_test_entity()
            ctx.entity_manager.add_entity(entity)

            retrieved = await ctx.entity_manager.get(entity.id)
            assert retrieved == entity
