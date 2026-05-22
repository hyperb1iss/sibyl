"""Tests for admin routes."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest
from fastapi import HTTPException
from fastapi.routing import APIRoute

from sibyl.api.routes import admin as admin_routes
from sibyl.api.routes.admin import (
    DebugQueryRequest,
    backfill_project_records,
    debug_query,
    dev_status,
    export_admin_audit,
    health,
    list_admin_audit,
    stats,
)
from sibyl.api.schemas import ProjectRecordBackfillRequest
from sibyl_core.auth import OrganizationRole


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
async def test_admin_audit_lists_filtered_events() -> None:
    org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
    user_id = "00000000-0000-0000-0000-000000000222"
    created_at = datetime(2026, 5, 22, 9, 30, tzinfo=UTC)
    rows = [
        {
            "uuid": "audit-1",
            "organization_id": str(org.id),
            "user_id": user_id,
            "action": "api_key.create",
            "ip_address": "127.0.0.1",
            "user_agent": "pytest",
            "details": {"resource": "api_key:abc", "scope": "api:read"},
            "created_at": created_at,
        }
    ]
    list_events = AsyncMock(return_value=(rows, 7))

    with patch("sibyl.api.routes.admin.list_audit_events", list_events):
        response = await list_admin_audit(
            org=org,
            user_id=user_id,
            action="api_key.create",
            resource="api_key:abc",
            start_time=created_at,
            end_time=created_at,
            limit=25,
            offset=5,
        )

    assert response.total == 7
    assert response.has_more is True
    assert response.events[0].id == "audit-1"
    assert response.events[0].resource == "api_key:abc"
    assert response.events[0].details == {"resource": "api_key:abc", "scope": "api:read"}
    list_events.assert_awaited_once_with(
        organization_id=org.id,
        user_id=user_id,
        action="api_key.create",
        resource="api_key:abc",
        start_time=created_at,
        end_time=created_at,
        limit=25,
        offset=5,
    )


@pytest.mark.asyncio
async def test_admin_audit_exports_csv() -> None:
    org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
    rows = [
        {
            "uuid": "audit-1",
            "organization_id": str(org.id),
            "user_id": "00000000-0000-0000-0000-000000000222",
            "action": "memory.recall",
            "details": {"project_id": "sibyl"},
            "created_at": datetime(2026, 5, 22, 9, 30, tzinfo=UTC),
        }
    ]

    with patch("sibyl.api.routes.admin.list_audit_events", AsyncMock(return_value=(rows, 1))):
        response = await export_admin_audit(org=org, export_format="csv", limit=1000)

    assert response.media_type == "text/csv"
    assert "attachment; filename=" in response.headers["content-disposition"]
    assert b"memory.recall" in response.body
    assert b"sibyl" in response.body


@pytest.mark.asyncio
async def test_admin_audit_route_rejects_member_role() -> None:
    routes = [route for route in admin_routes.router.routes if isinstance(route, APIRoute)]
    route = next(
        route for route in routes if route.path.endswith("/audit") and "GET" in route.methods
    )
    dependency = route.dependencies[0].dependency

    with pytest.raises(HTTPException) as exc_info:
        await dependency(OrganizationRole.MEMBER)

    assert exc_info.value.status_code == 403
    await dependency(OrganizationRole.ADMIN)


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
async def test_debug_query_allows_legacy_cypher_in_legacy_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
async def test_debug_query_blocks_cypher_after_unbalanced_quote_in_comment() -> None:
    org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
    request = DebugQueryRequest(cypher="-- stray ' quote\nMATCH (n) RETURN n LIMIT 1")

    with (
        patch("sibyl.api.routes.admin.execute_debug_query", AsyncMock()) as mock_execute,
        pytest.raises(HTTPException) as exc_info,
    ):
        await debug_query(request=request, org=org)

    assert exc_info.value.status_code == 400
    assert "Surreal runtime" in str(exc_info.value.detail)
    mock_execute.assert_not_awaited()


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
async def test_project_record_backfill_dry_run_reports_missing_records() -> None:
    org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
    user = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000222"))
    request = SimpleNamespace(client=None, headers={})
    projects = [
        SimpleNamespace(
            id="project_existing",
            name="Existing",
            description="Existing project",
            metadata={},
        ),
        SimpleNamespace(
            id="project_missing",
            name="Missing",
            description="Missing project",
            metadata={},
        ),
        SimpleNamespace(
            id="project_archived",
            name="Archived",
            description="Archived project",
            metadata={"status": "archived"},
        ),
    ]

    async def get_project_record(*, graph_project_id: str, **_: object) -> object:
        if graph_project_id == "project_existing":
            return SimpleNamespace(graph_project_id=graph_project_id)
        raise HTTPException(status_code=404, detail="Project not found")

    with (
        patch(
            "sibyl.api.routes.admin.list_graph_projects_for_record_backfill",
            AsyncMock(return_value=projects),
        ),
        patch(
            "sibyl.api.routes.admin.get_project_record_by_graph_id",
            AsyncMock(side_effect=get_project_record),
        ),
        patch("sibyl.api.routes.admin.create_project_record", AsyncMock()) as create_record,
        patch("sibyl.api.routes.admin.log_audit_event", AsyncMock()) as log_event,
    ):
        response = await backfill_project_records(
            backfill_request=ProjectRecordBackfillRequest(),
            request=request,
            org=org,
            user=user,
        )

    assert response.success is True
    assert response.dry_run is True
    assert response.existing == 1
    assert response.would_create == 1
    assert response.created == 0
    assert response.skipped == 1
    assert response.failed == 0
    assert [(item.graph_project_id, item.status) for item in response.projects] == [
        ("project_existing", "existing"),
        ("project_missing", "would_create"),
        ("project_archived", "skipped"),
    ]
    create_record.assert_not_awaited()
    log_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_project_record_backfill_apply_creates_missing_records() -> None:
    org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
    user = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000222"))
    request = SimpleNamespace(client=None, headers={})
    projects = [
        SimpleNamespace(
            id="project_missing",
            name="Missing",
            description="Missing project",
            metadata={},
        )
    ]

    with (
        patch(
            "sibyl.api.routes.admin.list_graph_projects_for_record_backfill",
            AsyncMock(return_value=projects),
        ),
        patch(
            "sibyl.api.routes.admin.get_project_record_by_graph_id",
            AsyncMock(side_effect=HTTPException(status_code=404, detail="Project not found")),
        ),
        patch("sibyl.api.routes.admin.create_project_record", AsyncMock()) as create_record,
        patch("sibyl.api.routes.admin.log_audit_event", AsyncMock()) as log_event,
    ):
        response = await backfill_project_records(
            backfill_request=ProjectRecordBackfillRequest(dry_run=False),
            request=request,
            org=org,
            user=user,
        )

    assert response.success is True
    assert response.created == 1
    assert response.would_create == 0
    assert response.projects[0].status == "created"
    create_record.assert_awaited_once_with(
        organization_id=org.id,
        owner_user_id=user.id,
        graph_project_id="project_missing",
        name="Missing",
        description="Missing project",
    )
    log_event.assert_awaited_once()
    assert log_event.await_args.kwargs["details"]["created_project_ids"] == ["project_missing"]


@pytest.mark.asyncio
async def test_project_record_backfill_marks_project_failures_without_aborting() -> None:
    org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
    user = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000222"))
    request = SimpleNamespace(client=None, headers={})
    projects = [
        SimpleNamespace(
            id="project_missing",
            name="Missing",
            description="Missing project",
            metadata={},
        )
    ]

    with (
        patch(
            "sibyl.api.routes.admin.list_graph_projects_for_record_backfill",
            AsyncMock(return_value=projects),
        ),
        patch(
            "sibyl.api.routes.admin.get_project_record_by_graph_id",
            AsyncMock(side_effect=HTTPException(status_code=404, detail="Project not found")),
        ),
        patch(
            "sibyl.api.routes.admin.create_project_record",
            AsyncMock(side_effect=RuntimeError("boom")),
        ),
        patch("sibyl.api.routes.admin.log_audit_event", AsyncMock()) as log_event,
    ):
        response = await backfill_project_records(
            backfill_request=ProjectRecordBackfillRequest(dry_run=False),
            request=request,
            org=org,
            user=user,
        )

    assert response.success is False
    assert response.created == 0
    assert response.failed == 1
    assert response.projects[0].status == "failed"
    assert response.projects[0].reason == "RuntimeError"
    assert response.errors == ["project_missing: RuntimeError"]
    log_event.assert_not_awaited()


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
        patch(
            "sibyl.api.routes.admin.get_surreal_observability_status",
            AsyncMock(
                return_value={
                    "configured": True,
                    "health_http_status": 200,
                    "metrics_http_status": 404,
                    "metrics_available": False,
                    "metric_count": 0,
                    "metrics_sample": {},
                    "error": None,
                }
            ),
        ),
        patch("sibyl_core.logging.LogBuffer.get", return_value=mock_buffer),
    ):
        response = await dev_status(org=org)

    assert response.api_healthy is True
    assert response.graph_healthy is True
    assert response.coordination_backend == "local"
    assert response.coordination_status == "unavailable"
    assert response.coordination_durable is False
    assert response.coordination_error == "Local coordination backend is not implemented yet"
    assert response.surreal_observability["metrics_http_status"] == 404


def test_parse_surreal_metric_names_and_sample() -> None:
    from sibyl.api.routes.admin import (
        _parse_surreal_metric_names,
        _surreal_metrics_sample,
    )

    metric_names = _parse_surreal_metric_names(
        "# HELP surrealdb_statement_total Statements\n"
        'surrealdb_statement_total{outcome="ok"} 12\n'
        "surrealdb_transaction_conflicts_total 2\n"
        "surrealdb_http_request_duration_seconds_bucket 1"
    )

    assert metric_names == [
        "surrealdb_http_request_duration_seconds_bucket",
        "surrealdb_statement_total",
        "surrealdb_transaction_conflicts_total",
    ]
    sample = _surreal_metrics_sample(metric_names)
    assert sample["surrealdb_statement_total"] is True
    assert sample["surrealdb_transaction_conflicts_total"] is True
    assert sample["surrealdb_http_request_duration_seconds"] is True
    assert sample["surrealdb_query_duration_seconds"] is False
