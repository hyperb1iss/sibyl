"""Tests for active graph runtime services."""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import sibyl_core.services as services
import sibyl_core.services.graph as public_graph
from sibyl_core.services import (
    GraphRuntime,
    execute_graph_query,
    get_graph_client,
    get_graph_runtime,
)

_GRAPH_DOMAIN_MODULES = {
    "graph_client",
    "graph_common",
    "graph_embeddings",
    "graph_entities",
    "graph_entity_search",
    "graph_entity_store",
    "graph_entity_work_items",
    "graph_records",
    "graph_relationships",
    "graph_runtime",
    "graph_search",
}


def test_public_graph_contract_contains_only_intentional_exports() -> None:
    assert public_graph.__all__ == [
        "CLEAR_MEMORY_SCOPE",
        "MAX_ENTITY_CONTENT_CHARS",
        "EntityManager",
        "GraphRuntime",
        "RelationshipManager",
        "SurrealGraphClient",
        "close_graph_clients",
        "entity_from_surreal_row",
        "get_surreal_graph_client",
        "get_surreal_graph_runtime",
        "heal_entity_metadata_snapshots",
        "mark_graph_schema_dirty",
        "normalize_records",
        "prepare_graph_schema",
        "relationship_from_surreal_row",
    ]


def test_graph_domain_imports_are_one_way_and_never_use_the_public_facade() -> None:
    services_dir = Path(__file__).parents[1] / "src" / "sibyl_core" / "services"
    dependencies: dict[str, set[str]] = {}
    for module_name in _GRAPH_DOMAIN_MODULES:
        tree = ast.parse((services_dir / f"{module_name}.py").read_text())
        imports = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module is not None
        }
        assert "sibyl_core.services.graph" not in imports
        dependencies[module_name] = {
            imported.rsplit(".", 1)[-1]
            for imported in imports
            if imported.rsplit(".", 1)[-1] in _GRAPH_DOMAIN_MODULES
        }

    visited: set[str] = set()
    active: set[str] = set()

    def visit(module_name: str) -> None:
        assert module_name not in active, f"graph service import cycle through {module_name}"
        if module_name in visited:
            return
        active.add(module_name)
        for dependency in dependencies[module_name]:
            visit(dependency)
        active.remove(module_name)
        visited.add(module_name)

    for module_name in dependencies:
        visit(module_name)


@pytest.mark.asyncio
async def test_get_graph_runtime_binds_native_store_managers() -> None:
    client = MagicMock()
    entity_manager = object()
    relationship_manager = object()
    native_runtime = SimpleNamespace(
        client=client,
        entity_manager=entity_manager,
        relationship_manager=relationship_manager,
    )

    with (
        patch(
            "sibyl_core.services.graph_runtime.get_surreal_graph_runtime",
            AsyncMock(return_value=native_runtime),
        ) as get_runtime,
        patch(
            "sibyl_core.services.graph_runtime.configured_embedding_provider",
            MagicMock(return_value=None),
        ),
    ):
        runtime = await get_graph_runtime("org-123")

    assert isinstance(runtime, GraphRuntime)
    assert runtime.client is client
    assert runtime.entity_manager is entity_manager
    assert runtime.relationship_manager is relationship_manager
    get_runtime.assert_awaited_once_with("org-123")


@pytest.mark.asyncio
async def test_get_graph_client_connects_native_client() -> None:
    client = MagicMock()
    client.connect = AsyncMock()

    with patch(
        "sibyl_core.services.graph_runtime.get_surreal_graph_client",
        AsyncMock(return_value=client),
    ) as get_client:
        result = await get_graph_client("org-123")

    assert result is client
    get_client.assert_awaited_once_with("org-123")
    client.connect.assert_awaited_once()


def test_services_package_exports_neutral_graph_helpers() -> None:
    assert services.GraphRuntime is GraphRuntime
    assert services.get_graph_client is get_graph_client
    assert services.get_graph_runtime is get_graph_runtime
    assert services.execute_graph_query is execute_graph_query


def test_graph_runtime_preserves_public_dataclass_mapping_surface() -> None:
    client = MagicMock()
    entity_manager = MagicMock()
    relationship_manager = MagicMock()

    runtime = GraphRuntime(
        client=client,
        entity_manager=entity_manager,
        relationship_manager=relationship_manager,
    )

    assert vars(runtime) == {
        "client": client,
        "entity_manager": entity_manager,
        "relationship_manager": relationship_manager,
    }


@pytest.mark.asyncio
async def test_execute_graph_query_normalizes_driver_result() -> None:
    client = MagicMock()
    client.execute_query = AsyncMock(return_value=[{"row": "value"}])
    runtime = SimpleNamespace(client=client)

    with patch(
        "sibyl_core.services.graph_runtime.get_graph_runtime",
        AsyncMock(return_value=runtime),
    ):
        result = await execute_graph_query("org-123", "RETURN $value", value="x")

    assert result == [{"row": "value"}]
    client.execute_query.assert_awaited_once_with(
        "RETURN $value",
        group_id="org-123",
        value="x",
    )


@pytest.mark.asyncio
async def test_execute_graph_query_rejects_cypher_on_native_runtime() -> None:
    client = MagicMock()
    client.execute_query = AsyncMock()
    runtime = SimpleNamespace(client=client)

    with (
        patch(
            "sibyl_core.services.graph_runtime.get_graph_runtime",
            AsyncMock(return_value=runtime),
        ),
        pytest.raises(ValueError, match="SurrealQL"),
    ):
        await execute_graph_query("org-123", "MATCH (n) RETURN n")

    client.execute_query.assert_not_awaited()


@pytest.mark.asyncio
async def test_execute_graph_query_allows_surrealql_on_surreal_driver() -> None:
    client = MagicMock()
    client.execute_query = AsyncMock(return_value=[{"row": "value"}])
    runtime = SimpleNamespace(client=client)

    with patch(
        "sibyl_core.services.graph_runtime.get_graph_runtime",
        AsyncMock(return_value=runtime),
    ):
        result = await execute_graph_query("org-123", "SELECT * FROM entity")

    assert result == [{"row": "value"}]
    client.execute_query.assert_awaited_once_with(
        "SELECT * FROM entity",
        group_id="org-123",
    )


@pytest.mark.asyncio
async def test_execute_graph_query_ignores_cypher_tokens_in_strings_and_comments() -> None:
    client = MagicMock()
    client.execute_query = AsyncMock(return_value=[{"row": "value"}])
    runtime = SimpleNamespace(client=client)
    query = """
        SELECT 'MATCH (n)', "CALL db.indexes", `UNWIND`
        FROM entity
        WHERE url = 'https://example.com//MATCH'
        /* UNWIND ignored */
        -- MATCH ignored
    """

    with patch(
        "sibyl_core.services.graph_runtime.get_graph_runtime",
        AsyncMock(return_value=runtime),
    ):
        result = await execute_graph_query("org-123", query)

    assert result == [{"row": "value"}]
    client.execute_query.assert_awaited_once_with(query, group_id="org-123")


@pytest.mark.asyncio
async def test_execute_graph_query_rejects_token_after_comment_quote() -> None:
    client = MagicMock()
    client.execute_query = AsyncMock()
    runtime = SimpleNamespace(client=client)
    query = """
        -- stray ' quote in comment
        MATCH (n) RETURN n
    """

    with (
        patch(
            "sibyl_core.services.graph_runtime.get_graph_runtime",
            AsyncMock(return_value=runtime),
        ),
        pytest.raises(ValueError, match="SurrealQL"),
    ):
        await execute_graph_query("org-123", query)

    client.execute_query.assert_not_awaited()
