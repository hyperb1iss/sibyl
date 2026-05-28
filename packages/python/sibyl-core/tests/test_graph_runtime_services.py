"""Tests for active graph runtime services."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import sibyl_core.services as services
from sibyl_core.services import (
    ActiveGraphRuntime,
    execute_graph_query,
    get_graph_client,
    get_graph_runtime,
)


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

    assert isinstance(runtime, ActiveGraphRuntime)
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
    assert services.ActiveGraphRuntime is ActiveGraphRuntime
    assert services.get_graph_client is get_graph_client
    assert services.get_graph_runtime is get_graph_runtime
    assert services.execute_graph_query is execute_graph_query


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
