"""Context managers for mocking Sibyl dependencies.

Provides context managers that patch graph client and managers
for isolated tool testing.
"""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, patch

from sibyl_core.services import ActiveGraphRuntime
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
        - sibyl_core.tools.core.get_graph_client
        - sibyl_core.tools.core.EntityManager
        - sibyl_core.tools.core.RelationshipManager
        - sibyl_core.tools.manage.get_graph_client (same)
        - sibyl_core.tools.manage.EntityManager (same)
        - sibyl_core.tools.manage.RelationshipManager (same)

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

        async def async_get_graph_runtime(group_id: str) -> ActiveGraphRuntime:
            return ActiveGraphRuntime(
                client=self.graph_client,
                entity_manager=self.entity_manager,
                relationship_manager=self.relationship_manager,
            )

        async def async_execute_graph_query(
            group_id: str,
            query: str,
            **params: Any,
        ) -> list[dict[str, Any]]:
            return await self.graph_client.execute_read(query, **params)

        patches = [
            # Patch at the module level where tools import from
            # Search tool
            patch("sibyl_core.tools.search.get_graph_runtime", async_get_graph_runtime),
            # Explore tool
            patch("sibyl_core.tools.explore.get_graph_runtime", async_get_graph_runtime),
            # Add tool
            patch("sibyl_core.tools.add.get_graph_runtime", async_get_graph_runtime),
            # Manage tool
            patch("sibyl_core.tools.manage.get_graph_client", async_get_graph_client),
            patch("sibyl_core.tools.manage.get_graph_runtime", async_get_graph_runtime),
            # Health tool
            patch("sibyl_core.tools.health.get_graph_client", async_get_graph_client),
            patch("sibyl_core.tools.health.get_graph_runtime", async_get_graph_runtime),
            patch("sibyl_core.tools.health.execute_graph_query", async_execute_graph_query),
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
        patch(
            "sibyl_core.tools.search.get_graph_runtime",
            AsyncMock(
                return_value=ActiveGraphRuntime(
                    client=client,
                    entity_manager=MockEntityManager(),
                    relationship_manager=MockRelationshipManager(),
                )
            ),
        ),
        patch(
            "sibyl_core.tools.explore.get_graph_runtime",
            AsyncMock(
                return_value=ActiveGraphRuntime(
                    client=client,
                    entity_manager=MockEntityManager(),
                    relationship_manager=MockRelationshipManager(),
                )
            ),
        ),
        patch(
            "sibyl_core.tools.add.get_graph_runtime",
            AsyncMock(
                return_value=ActiveGraphRuntime(
                    client=client,
                    entity_manager=MockEntityManager(),
                    relationship_manager=MockRelationshipManager(),
                )
            ),
        ),
        patch("sibyl_core.tools.manage.get_graph_client", async_get_client),
        patch(
            "sibyl_core.tools.manage.get_graph_runtime",
            AsyncMock(
                return_value=ActiveGraphRuntime(
                    client=client,
                    entity_manager=MockEntityManager(),
                    relationship_manager=MockRelationshipManager(),
                )
            ),
        ),
        patch("sibyl_core.tools.health.get_graph_client", async_get_client),
        patch(
            "sibyl_core.tools.health.get_graph_runtime",
            AsyncMock(
                return_value=ActiveGraphRuntime(
                    client=client,
                    entity_manager=MockEntityManager(),
                    relationship_manager=MockRelationshipManager(),
                )
            ),
        ),
        patch("sibyl_core.tools.health.execute_graph_query", AsyncMock(return_value=[])),
        patch("sibyl_core.tools.admin.get_graph_client", async_get_client),
        patch(
            "sibyl_core.tools.admin.get_graph_runtime",
            AsyncMock(
                return_value=ActiveGraphRuntime(
                    client=client,
                    entity_manager=MockEntityManager(),
                    relationship_manager=MockRelationshipManager(),
                )
            ),
        ),
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
        patch(
            "sibyl_core.tools.search.get_graph_runtime",
            AsyncMock(
                return_value=ActiveGraphRuntime(
                    client=client,
                    entity_manager=MockEntityManager(),
                    relationship_manager=MockRelationshipManager(),
                )
            ),
        ),
        patch(
            "sibyl_core.tools.explore.get_graph_runtime",
            AsyncMock(
                return_value=ActiveGraphRuntime(
                    client=client,
                    entity_manager=MockEntityManager(),
                    relationship_manager=MockRelationshipManager(),
                )
            ),
        ),
        patch(
            "sibyl_core.tools.add.get_graph_runtime",
            AsyncMock(
                return_value=ActiveGraphRuntime(
                    client=client,
                    entity_manager=MockEntityManager(),
                    relationship_manager=MockRelationshipManager(),
                )
            ),
        ),
        patch("sibyl_core.tools.manage.get_graph_client", async_get_client),
        patch(
            "sibyl_core.tools.manage.get_graph_runtime",
            AsyncMock(
                return_value=ActiveGraphRuntime(
                    client=client,
                    entity_manager=MockEntityManager(),
                    relationship_manager=MockRelationshipManager(),
                )
            ),
        ),
        patch("sibyl_core.tools.health.get_graph_client", async_get_client),
        patch(
            "sibyl_core.tools.health.get_graph_runtime",
            AsyncMock(
                return_value=ActiveGraphRuntime(
                    client=client,
                    entity_manager=MockEntityManager(),
                    relationship_manager=MockRelationshipManager(),
                )
            ),
        ),
        patch("sibyl_core.tools.health.execute_graph_query", AsyncMock(return_value=[])),
        patch("sibyl_core.tools.admin.get_graph_client", async_get_client),
        patch(
            "sibyl_core.tools.admin.get_graph_runtime",
            AsyncMock(
                return_value=ActiveGraphRuntime(
                    client=client,
                    entity_manager=MockEntityManager(),
                    relationship_manager=MockRelationshipManager(),
                )
            ),
        ),
    ):
        yield client
