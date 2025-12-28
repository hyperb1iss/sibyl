"""MCP Test Harness for Sibyl tools.

Provides mock implementations and helpers for testing MCP tools
without requiring a real FalkorDB connection.

Example usage:

    from tests.harness import ToolTestContext, setup_search_results

    async def test_search_returns_results():
        ctx = ToolTestContext()
        setup_search_results(ctx, count=5)

        async with ctx.patch():
            result = await search("test query")
            assert len(result.results) == 5

Or using the convenience context manager:

    from tests.harness import mock_tools

    async def test_search():
        async with mock_tools() as ctx:
            ctx.entity_manager.set_search_results([...])
            result = await search("query")
"""

# Mocks
# Context managers
from tests.harness.context import (
    ToolTestContext,
    mock_graph_connected,
    mock_graph_disconnected,
    mock_tools,
)

# Helpers
from tests.harness.helpers import (
    # Add
    call_add,
    # Explore
    call_explore,
    # Manage
    call_manage,
    # Search
    call_search,
    setup_entity_graph,
    setup_search_results,
    setup_task_workflow,
    validate_add_response,
    validate_explore_response,
    validate_manage_response,
    validate_search_response,
)
from tests.harness.mocks import (
    MockEntityManager,
    MockGraphClient,
    MockRelationshipManager,
    create_test_entity,
    create_test_relationship,
)

__all__ = [
    # Mocks
    "MockGraphClient",
    "MockEntityManager",
    "MockRelationshipManager",
    "create_test_entity",
    "create_test_relationship",
    # Context
    "ToolTestContext",
    "mock_tools",
    "mock_graph_connected",
    "mock_graph_disconnected",
    # Search helpers
    "call_search",
    "setup_search_results",
    "validate_search_response",
    # Explore helpers
    "call_explore",
    "setup_entity_graph",
    "validate_explore_response",
    # Add helpers
    "call_add",
    "validate_add_response",
    # Manage helpers
    "call_manage",
    "setup_task_workflow",
    "validate_manage_response",
]
