"""Tests for admin routes."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest
from fastapi import HTTPException

from sibyl.api.routes.admin import DebugQueryRequest, debug_query, dev_status, health, stats


@pytest.mark.asyncio
async def test_health_passes_org_context_to_core_health() -> None:
    org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
    mock_get_health = AsyncMock(
        return_value={
            "status": "healthy",
            "server_name": "sibyl",
            "uptime_seconds": 42,
            "graph_connected": True,
            "entity_counts": {"task": 3},
            "errors": [],
        }
    )

    with patch("sibyl_core.tools.core.get_health", mock_get_health):
        response = await health(org=org)

    assert response.status == "healthy"
    assert response.entity_counts == {"task": 3}
    mock_get_health.assert_awaited_once_with(organization_id=str(org.id))


@pytest.mark.asyncio
async def test_stats_uses_graph_stats_payload() -> None:
    org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
    mock_get_stats = AsyncMock(
        return_value={
            "entity_counts": {"task": 4, "pattern": 1},
            "total_entities": 5,
        }
    )

    with patch("sibyl.api.routes.admin.get_graph_stats_payload", mock_get_stats):
        response = await stats(org=org)

    assert response.total_entities == 5
    assert response.entity_counts == {"task": 4, "pattern": 1}
    mock_get_stats.assert_awaited_once_with(str(org.id))


@pytest.mark.asyncio
async def test_debug_query_uses_debug_runner_for_surrealql() -> None:
    org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
    request = DebugQueryRequest(cypher="SELECT * FROM entity LIMIT $limit", params={"limit": 1})
    mock_execute = AsyncMock(return_value=[{"name": "node"}])

    with patch("sibyl.api.routes.admin.execute_debug_query", mock_execute):
        response = await debug_query(request=request, org=org)

    assert response.rows == [{"name": "node"}]
    assert response.row_count == 1
    mock_execute.assert_awaited_once_with(
        "SELECT * FROM entity LIMIT $limit",
        group_id=str(org.id),
        limit=1,
    )


@pytest.mark.asyncio
async def test_debug_query_allows_legacy_cypher_in_legacy_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sibyl.api.routes.admin.settings.store", "legacy")
    org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
    request = DebugQueryRequest(cypher="MATCH (n) RETURN n LIMIT 1")
    mock_execute = AsyncMock(return_value=[{"value": ("node",)}])

    with patch("sibyl.api.routes.admin.execute_debug_query", mock_execute):
        response = await debug_query(request=request, org=org)

    assert response.rows == [{"value": ("node",)}]
    assert response.row_count == 1
    mock_execute.assert_awaited_once_with(
        "MATCH (n) RETURN n LIMIT 1",
        group_id=str(org.id),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query",
    [
        "MATCH (n) RETURN n LIMIT 1",
        "OPTIONAL MATCH (n) RETURN n LIMIT 1",
        "WITH 1 AS x MATCH (n) RETURN n LIMIT 1",
        "// read only\nMATCH (n) RETURN n LIMIT 1",
        "CALL db.labels()",
    ],
)
async def test_debug_query_blocks_cypher_in_surreal_runtime(query: str) -> None:
    org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
    request = DebugQueryRequest(cypher=query)

    with (
        patch("sibyl.api.routes.admin.execute_debug_query", AsyncMock()) as mock_execute,
        pytest.raises(HTTPException) as exc_info,
    ):
        await debug_query(request=request, org=org)

    assert exc_info.value.status_code == 400
    assert "Surreal runtime" in str(exc_info.value.detail)
    mock_execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_debug_query_allows_read_only_names_containing_mutation_words() -> None:
    org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
    request = DebugQueryRequest(cypher="SELECT * FROM system_settings LIMIT 1")
    mock_execute = AsyncMock(return_value=[{"key": "theme"}])

    with patch("sibyl.api.routes.admin.execute_debug_query", mock_execute):
        response = await debug_query(request=request, org=org)

    assert response.row_count == 1
    mock_execute.assert_awaited_once_with(
        "SELECT * FROM system_settings LIMIT 1",
        group_id=str(org.id),
    )


@pytest.mark.asyncio
async def test_debug_query_allows_keywords_inside_string_literals() -> None:
    org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
    request = DebugQueryRequest(cypher="SELECT * FROM entity WHERE name = 'MATCH UPDATE'")
    mock_execute = AsyncMock(return_value=[])

    with patch("sibyl.api.routes.admin.execute_debug_query", mock_execute):
        response = await debug_query(request=request, org=org)

    assert response.row_count == 0
    mock_execute.assert_awaited_once_with(
        "SELECT * FROM entity WHERE name = 'MATCH UPDATE'",
        group_id=str(org.id),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query",
    [
        "UPDATE entity SET name = 'oops'",
        "SELECT * FROM entity WHERE url = 'https://example.test'; UPDATE entity SET name = 'oops'",
        "DEFINE TABLE temp SCHEMALESS",
        "RELATE entity:one->relates_to->entity:two",
    ],
)
async def test_debug_query_blocks_surreal_mutations(query: str) -> None:
    org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
    request = DebugQueryRequest(cypher=query)

    with (
        patch("sibyl.api.routes.admin.execute_debug_query", AsyncMock()) as mock_execute,
        pytest.raises(HTTPException) as exc_info,
    ):
        await debug_query(request=request, org=org)

    assert exc_info.value.status_code == 400
    mock_execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_dev_status_reports_coordination_backend() -> None:
    org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
    mock_get_health = AsyncMock(
        return_value={
            "status": "healthy",
            "graph_connected": True,
            "uptime_seconds": 42,
        }
    )
    mock_stats = AsyncMock(return_value={"total_entities": 5})
    mock_coordination = AsyncMock(
        return_value={
            "backend": "local",
            "status": "unavailable",
            "durable": False,
            "error": "Local coordination backend is not implemented yet",
            "queue_healthy": False,
            "worker_healthy": False,
            "queue_depth": 0,
        }
    )
    mock_buffer = SimpleNamespace(tail=lambda **_: [])

    with (
        patch("sibyl_core.tools.core.get_health", mock_get_health),
        patch("sibyl.api.routes.admin.get_graph_stats_payload", mock_stats),
        patch("sibyl.api.routes.admin.get_coordination_health", mock_coordination),
        patch("sibyl_core.logging.LogBuffer.get", return_value=mock_buffer),
    ):
        response = await dev_status(org=org)

    assert response.api_healthy is True
    assert response.graph_healthy is True
    assert response.coordination_backend == "local"
    assert response.coordination_status == "unavailable"
    assert response.coordination_durable is False
    assert response.coordination_error == "Local coordination backend is not implemented yet"
