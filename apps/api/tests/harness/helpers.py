"""Helper functions for testing Sibyl MCP tools.

Provides high-level helper functions for calling each tool and
validating response schemas.
"""

from typing import Any

from sibyl.models.entities import EntityType
from sibyl.tools.core import (
    AddResponse,
    ExploreResponse,
    SearchResponse,
    add,
    explore,
    search,
)
from sibyl.tools.manage import ManageResponse, manage
from tests.harness.context import ToolTestContext
from tests.harness.mocks import (
    create_test_entity,
    create_test_relationship,
)

# =============================================================================
# Search Tool Helpers
# =============================================================================


async def call_search(
    ctx: ToolTestContext,
    query: str,
    types: list[str] | None = None,
    **kwargs: Any,
) -> SearchResponse:
    """Execute search tool with mocked dependencies.

    Args:
        ctx: ToolTestContext with configured mocks.
        query: Search query string.
        types: Entity types to search.
        **kwargs: Additional search parameters.

    Returns:
        SearchResponse from the tool.
    """
    async with ctx.patch():
        return await search(query=query, types=types, **kwargs)


def setup_search_results(
    ctx: ToolTestContext,
    count: int = 3,
    entity_type: EntityType = EntityType.EPISODE,
    base_score: float = 0.9,
) -> list[Any]:
    """Set up mock search results on context.

    Args:
        ctx: ToolTestContext to configure.
        count: Number of results to create.
        entity_type: Type of entities to create.
        base_score: Starting score (decreases by 0.05 per result).

    Returns:
        List of created entities.
    """
    entities = []
    results = []

    for i in range(count):
        entity = create_test_entity(
            entity_type=entity_type,
            name=f"Test Result {i + 1}",
            description=f"Description for test result {i + 1}",
        )
        score = max(0.1, base_score - (i * 0.05))
        entities.append(entity)
        results.append((entity, score))

    ctx.entity_manager.set_search_results(results)
    return entities


# =============================================================================
# Explore Tool Helpers
# =============================================================================


async def call_explore(
    ctx: ToolTestContext,
    mode: str = "list",
    entity_id: str | None = None,
    types: list[str] | None = None,
    **kwargs: Any,
) -> ExploreResponse:
    """Execute explore tool with mocked dependencies.

    Args:
        ctx: ToolTestContext with configured mocks.
        mode: Exploration mode (list, related, traverse).
        entity_id: Entity ID for related/traverse modes.
        types: Entity types to explore.
        **kwargs: Additional explore parameters.

    Returns:
        ExploreResponse from the tool.
    """
    async with ctx.patch():
        return await explore(mode=mode, entity_id=entity_id, types=types, **kwargs)


def setup_entity_graph(
    ctx: ToolTestContext,
    root_type: EntityType = EntityType.PROJECT,
    child_count: int = 3,
) -> tuple[Any, list[Any]]:
    """Set up a simple entity graph for testing.

    Creates a root entity with child entities and relationships.

    Args:
        ctx: ToolTestContext to configure.
        root_type: Entity type for root node.
        child_count: Number of child entities.

    Returns:
        Tuple of (root_entity, list_of_children).
    """
    root = create_test_entity(
        entity_type=root_type,
        name="Root Entity",
        description="Root of test graph",
    )
    ctx.entity_manager.add_entity(root)

    children = []
    for i in range(child_count):
        child = create_test_entity(
            entity_type=EntityType.TASK,
            name=f"Child {i + 1}",
            description=f"Child entity {i + 1}",
        )
        ctx.entity_manager.add_entity(child)
        children.append(child)

        # Create relationship
        rel = create_test_relationship(
            source_id=child.id,
            target_id=root.id,
        )
        ctx.relationship_manager.add_relationship(rel)

    return root, children


# =============================================================================
# Add Tool Helpers
# =============================================================================


async def call_add(
    ctx: ToolTestContext,
    title: str,
    content: str,
    entity_type: str = "episode",
    **kwargs: Any,
) -> AddResponse:
    """Execute add tool with mocked dependencies.

    Args:
        ctx: ToolTestContext with configured mocks.
        title: Entity title.
        content: Entity content/description.
        entity_type: Type of entity to create.
        **kwargs: Additional add parameters.

    Returns:
        AddResponse from the tool.
    """
    async with ctx.patch():
        return await add(
            title=title,
            content=content,
            entity_type=entity_type,
            **kwargs,
        )


# =============================================================================
# Manage Tool Helpers
# =============================================================================


async def call_manage(
    ctx: ToolTestContext,
    action: str,
    entity_id: str | None = None,
    data: dict[str, Any] | None = None,
) -> ManageResponse:
    """Execute manage tool with mocked dependencies.

    Args:
        ctx: ToolTestContext with configured mocks.
        action: Action to perform.
        entity_id: Target entity ID.
        data: Action-specific data.

    Returns:
        ManageResponse from the tool.
    """
    async with ctx.patch():
        return await manage(action=action, entity_id=entity_id, data=data)


def setup_task_workflow(
    ctx: ToolTestContext,
    task_count: int = 1,
    initial_status: str = "todo",
) -> list[Any]:
    """Set up task entities for workflow testing.

    Args:
        ctx: ToolTestContext to configure.
        task_count: Number of tasks to create.
        initial_status: Initial task status.

    Returns:
        List of created task entities.
    """
    tasks = []
    for i in range(task_count):
        task = create_test_entity(
            entity_type=EntityType.TASK,
            name=f"Test Task {i + 1}",
            description=f"Task {i + 1} description",
            metadata={"status": initial_status},
        )
        ctx.entity_manager.add_entity(task)
        tasks.append(task)

    return tasks


# =============================================================================
# Response Validation
# =============================================================================


def validate_search_response(response: SearchResponse) -> list[str]:
    """Validate SearchResponse schema.

    Returns:
        List of validation errors (empty if valid).
    """
    errors = []

    if not hasattr(response, "results"):
        errors.append("Missing 'results' field")
    elif not isinstance(response.results, list):
        errors.append("'results' should be a list")

    if not hasattr(response, "total"):
        errors.append("Missing 'total' field")
    elif not isinstance(response.total, int):
        errors.append("'total' should be an int")

    return errors


def validate_explore_response(response: ExploreResponse) -> list[str]:
    """Validate ExploreResponse schema.

    Returns:
        List of validation errors (empty if valid).
    """
    errors = []

    if not hasattr(response, "entities"):
        errors.append("Missing 'entities' field")
    elif not isinstance(response.entities, list):
        errors.append("'entities' should be a list")

    if not hasattr(response, "mode"):
        errors.append("Missing 'mode' field")

    return errors


def validate_add_response(response: AddResponse) -> list[str]:
    """Validate AddResponse schema.

    Returns:
        List of validation errors (empty if valid).
    """
    errors = []

    if not hasattr(response, "success"):
        errors.append("Missing 'success' field")
    elif not isinstance(response.success, bool):
        errors.append("'success' should be a bool")

    if not hasattr(response, "message"):
        errors.append("Missing 'message' field")

    return errors


def validate_manage_response(response: ManageResponse) -> list[str]:
    """Validate ManageResponse schema.

    Returns:
        List of validation errors (empty if valid).
    """
    errors = []

    if not hasattr(response, "success"):
        errors.append("Missing 'success' field")

    if not hasattr(response, "action"):
        errors.append("Missing 'action' field")

    if not hasattr(response, "message"):
        errors.append("Missing 'message' field")

    return errors
