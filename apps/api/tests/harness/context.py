"""Context managers for mocking Sibyl dependencies.

Provides context managers that patch graph client and managers
for isolated tool testing.
"""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import patch

from tests.harness.mocks import (
    MockEntityManager,
    MockGraphClient,
    MockRelationshipManager,
)


class ToolTestContext:
    """Context for testing MCP tools with mocked dependencies.

    Provides pre-configured mocks for GraphClient, EntityManager,
    and RelationshipManager that can be customized per test.

    Example:
        ctx = ToolTestContext()
        ctx.entity_manager.add_entity(test_entity)
        ctx.entity_manager.set_search_results([(test_entity, 0.9)])

        async with ctx.patch():
            result = await search(query="test")
    """

    def __init__(self) -> None:
        """Initialize test context with fresh mocks."""
        self.graph_client = MockGraphClient()
        self.entity_manager = MockEntityManager()
        self.relationship_manager = MockRelationshipManager()

        # Track calls for assertion
        self._patches: list[Any] = []

    @asynccontextmanager
    async def patch(self) -> AsyncGenerator["ToolTestContext"]:
        """Context manager that patches all tool dependencies.

        Patches:
        - sibyl.tools.core.get_graph_client
        - sibyl.tools.core.EntityManager
        - sibyl.tools.core.RelationshipManager
        - sibyl.tools.manage.get_graph_client (same)
        - sibyl.tools.manage.EntityManager (same)
        - sibyl.tools.manage.RelationshipManager (same)

        Yields:
            Self, allowing access to mocks for assertions.
        """

        # Create mock constructors that return our mock instances
        def make_entity_manager(*args: Any, **kwargs: Any) -> MockEntityManager:
            return self.entity_manager

        def make_relationship_manager(*args: Any, **kwargs: Any) -> MockRelationshipManager:
            return self.relationship_manager

        # get_graph_client is async, so we need an async mock
        async def async_get_graph_client() -> MockGraphClient:
            return self.graph_client

        patches = [
            # Core tools
            patch("sibyl.tools.core.get_graph_client", async_get_graph_client),
            patch("sibyl.tools.core.EntityManager", make_entity_manager),
            patch("sibyl.tools.core.RelationshipManager", make_relationship_manager),
            # Manage tools
            patch("sibyl.tools.manage.get_graph_client", async_get_graph_client),
            patch("sibyl.tools.manage.EntityManager", make_entity_manager),
            patch("sibyl.tools.manage.RelationshipManager", make_relationship_manager),
        ]

        for p in patches:
            p.start()
            self._patches.append(p)

        try:
            yield self
        finally:
            for p in self._patches:
                p.stop()
            self._patches.clear()

    def reset(self) -> None:
        """Reset all mock data to empty state."""
        self.entity_manager._entities.clear()
        self.entity_manager._search_results.clear()
        self.relationship_manager._relationships.clear()


@asynccontextmanager
async def mock_tools() -> AsyncGenerator[ToolTestContext]:
    """Convenience context manager for tool testing.

    Example:
        async with mock_tools() as ctx:
            ctx.entity_manager.set_search_results([...])
            result = await search("test query")
    """
    ctx = ToolTestContext()
    async with ctx.patch():
        yield ctx


@asynccontextmanager
async def mock_graph_connected() -> AsyncGenerator[MockGraphClient]:
    """Simple context manager that mocks only the graph client.

    Useful for testing connection-dependent code paths.
    """
    client = MockGraphClient()

    async def async_get_client() -> MockGraphClient:
        return client

    with (
        patch("sibyl.tools.core.get_graph_client", async_get_client),
        patch("sibyl.tools.manage.get_graph_client", async_get_client),
        patch("sibyl.tools.admin.get_graph_client", async_get_client),
    ):
        yield client


@asynccontextmanager
async def mock_graph_disconnected() -> AsyncGenerator[MockGraphClient]:
    """Context manager that simulates disconnected graph.

    Useful for testing error handling.
    """
    client = MockGraphClient()
    client._connected = False

    async def async_get_client() -> MockGraphClient:
        return client

    with (
        patch("sibyl.tools.core.get_graph_client", async_get_client),
        patch("sibyl.tools.manage.get_graph_client", async_get_client),
        patch("sibyl.tools.admin.get_graph_client", async_get_client),
    ):
        yield client
