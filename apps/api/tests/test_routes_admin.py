"""Tests for admin routes."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest
from fastapi import HTTPException
from fastapi.routing import APIRoute
from surrealdb import RecordID
from surrealdb.data.types.datetime import Datetime as SurrealDatetime

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
async def test_admin_audit_exports_json() -> None:
    org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
    rows = [
        {
            "uuid": "audit-1",
            "organization_id": str(org.id),
            "user_id": "00000000-0000-0000-0000-000000000222",
            "action": "auth.login",
            "details": {"resource": "user:00000000-0000-0000-0000-000000000222"},
            "created_at": datetime(2026, 5, 22, 9, 30, tzinfo=UTC),
        }
    ]

    with patch("sibyl.api.routes.admin.list_audit_events", AsyncMock(return_value=(rows, 1))):
        response = await export_admin_audit(org=org, export_format="json", limit=1000)

    payload = json.loads(response.body)
    assert response.media_type == "application/json"
    assert "attachment; filename=" in response.headers["content-disposition"]
    assert payload["total"] == 1
    assert payload["events"][0]["action"] == "auth.login"
    assert payload["events"][0]["resource"] == "user:00000000-0000-0000-0000-000000000222"


@pytest.mark.asyncio
async def test_admin_audit_exports_enterprise_event_classes() -> None:
    org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
    actions = [
        "auth.oidc.login",
        "auth.break_glass.login",
        "auth.api_key.create",
        "auth.api_key.revoke",
        "memory.remember",
        "memory.recall",
        "org.member.update_role",
        "project.member.update_role",
    ]
    rows = [
        {
            "uuid": f"audit-{index}",
            "organization_id": str(org.id),
            "user_id": "00000000-0000-0000-0000-000000000222",
            "action": action,
            "details": {"resource": f"event:{index}"},
            "created_at": datetime(2026, 5, 22, 9, 30, tzinfo=UTC),
        }
        for index, action in enumerate(actions)
    ]

    with patch(
        "sibyl.api.routes.admin.list_audit_events", AsyncMock(return_value=(rows, len(rows)))
    ):
        response = await export_admin_audit(org=org, export_format="json", limit=1000)

    payload = json.loads(response.body)
    assert {event["action"] for event in payload["events"]} == set(actions)


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
async def test_debug_query_serializes_surreal_native_values() -> None:
    org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
    request = DebugQueryRequest(cypher="SELECT id, metadata FROM entity")
    mock_execute = AsyncMock(
        return_value=[
            {
                "id": RecordID("entity", "00f2y7aye1xkzr1zdusw"),
                "metadata": {
                    "source": RecordID("raw_captures", "capture_1"),
                    "observed_at": SurrealDatetime("2026-07-10T16:48:19Z"),
                },
            }
        ]
    )

    with patch("sibyl.api.routes.admin.execute_debug_query", mock_execute):
        response = await debug_query(request=request, org=org)

    assert json.loads(response.model_dump_json()) == {
        "rows": [
            {
                "id": "entity:00f2y7aye1xkzr1zdusw",
                "metadata": {
                    "source": "raw_captures:capture_1",
                    "observed_at": "2026-07-10T16:48:19Z",
                },
            }
        ],
        "row_count": 1,
        "error": None,
    }


@pytest.mark.asyncio
async def test_admin_execute_debug_query_routes_content_tables_to_content_runtime() -> None:
    content_execute = AsyncMock(return_value=[{"uuid": "raw-1"}])
    graph_execute = AsyncMock(return_value=[])
    query = "SELECT * FROM raw_captures WHERE organization_id = $group_id LIMIT $limit"

    with (
        patch("sibyl.persistence.content_runtime.execute_debug_query", content_execute),
        patch("sibyl.persistence.graph_runtime.execute_debug_query", graph_execute),
    ):
        rows = await admin_routes.execute_debug_query(
            query,
            group_id="org-1",
            limit=1,
        )

    assert rows == [{"uuid": "raw-1"}]
    content_execute.assert_awaited_once_with(
        query,
        organization_id="org-1",
        group_id="org-1",
        org_id="org-1",
        limit=1,
    )
    graph_execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_admin_execute_debug_query_routes_unscoped_content_queries() -> None:
    content_execute = AsyncMock(return_value=[{"uuid": "raw-1"}])

    with patch("sibyl.persistence.content_runtime.execute_debug_query", content_execute):
        rows = await admin_routes.execute_debug_query(
            "SELECT * FROM raw_captures LIMIT $limit",
            group_id="org-1",
            limit=1,
        )

    assert rows == [{"uuid": "raw-1"}]
    content_execute.assert_awaited_once_with(
        "SELECT * FROM raw_captures LIMIT $limit",
        organization_id="org-1",
        group_id="org-1",
        org_id="org-1",
        limit=1,
    )


@pytest.mark.asyncio
async def test_admin_execute_debug_query_routes_memory_usage_events_to_content_runtime() -> None:
    content_execute = AsyncMock(return_value=[{"uuid": "usage-1"}])
    graph_execute = AsyncMock(return_value=[])
    query = "SELECT * FROM memory_usage_events ORDER BY event_at DESC LIMIT 1"

    with (
        patch("sibyl.persistence.content_runtime.execute_debug_query", content_execute),
        patch("sibyl.persistence.graph_runtime.execute_debug_query", graph_execute),
    ):
        rows = await admin_routes.execute_debug_query(query, group_id="org-1")

    assert rows == [{"uuid": "usage-1"}]
    content_execute.assert_awaited_once_with(
        query,
        organization_id="org-1",
        group_id="org-1",
        org_id="org-1",
    )
    graph_execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_admin_execute_debug_query_rejects_cross_content_table_reads() -> None:
    content_execute = AsyncMock(return_value=[])

    with (
        patch("sibyl.persistence.content_runtime.execute_debug_query", content_execute),
        pytest.raises(ValueError, match="one content table"),
    ):
        await admin_routes.execute_debug_query(
            """
            SELECT * FROM raw_captures
            WHERE organization_id = $group_id
              AND source_id IN (SELECT VALUE uuid FROM crawl_sources)
            """,
            group_id="org-1",
        )

    content_execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_admin_execute_debug_query_rejects_content_table_subqueries() -> None:
    content_execute = AsyncMock(return_value=[])

    with (
        patch("sibyl.persistence.content_runtime.execute_debug_query", content_execute),
        pytest.raises(ValueError, match="one content table"),
    ):
        await admin_routes.execute_debug_query(
            """
            SELECT *, (SELECT * FROM raw_captures) AS sibling_rows
            FROM raw_captures
            WHERE organization_id = $group_id
            """,
            group_id="org-1",
        )

    content_execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_admin_execute_debug_query_rejects_dynamic_content_tables() -> None:
    content_execute = AsyncMock(return_value=[])
    graph_execute = AsyncMock(return_value=[])

    with (
        patch("sibyl.persistence.content_runtime.execute_debug_query", content_execute),
        patch("sibyl.persistence.graph_runtime.execute_debug_query", graph_execute),
        pytest.raises(ValueError, match="literal content table"),
    ):
        await admin_routes.execute_debug_query(
            'SELECT * FROM type::table("raw_captures") WHERE organization_id = $group_id',
            group_id="org-1",
        )

    content_execute.assert_not_awaited()
    graph_execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_admin_execute_debug_query_rejects_dynamic_content_tables_with_comments() -> None:
    content_execute = AsyncMock(return_value=[])
    graph_execute = AsyncMock(return_value=[])

    with (
        patch("sibyl.persistence.content_runtime.execute_debug_query", content_execute),
        patch("sibyl.persistence.graph_runtime.execute_debug_query", graph_execute),
        pytest.raises(ValueError, match="literal content table"),
    ):
        await admin_routes.execute_debug_query(
            'SELECT * FROM type/*x*/::/*x*/table/*x*/("raw_captures")',
            group_id="org-1",
        )

    content_execute.assert_not_awaited()
    graph_execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_admin_execute_debug_query_ignores_dynamic_content_table_in_literals() -> None:
    graph_execute = AsyncMock(return_value=[{"uuid": "entity-1"}])

    with patch("sibyl.persistence.graph_runtime.execute_debug_query", graph_execute):
        rows = await admin_routes.execute_debug_query(
            """
            SELECT *
            FROM entity
            WHERE note = 'type::table("raw_captures")'
              AND comment = "type::table('crawl_sources')"
              -- type::table("raw_captures")
              /* type::table("crawl_sources") */
            """,
            group_id="org-1",
        )

    assert rows == [{"uuid": "entity-1"}]
    graph_execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_debug_query_rejects_mismatched_scope_params() -> None:
    org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
    request = DebugQueryRequest(
        cypher="SELECT * FROM raw_captures WHERE organization_id = $group_id",
        params={"group_id": "00000000-0000-0000-0000-000000000222"},
    )

    with (
        patch("sibyl.api.routes.admin.execute_debug_query", AsyncMock()) as mock_execute,
        pytest.raises(HTTPException) as exc_info,
    ):
        await debug_query(request=request, org=org)

    assert exc_info.value.status_code == 400
    mock_execute.assert_not_awaited()


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
        "INFO FOR ROOT",
        "SELECT http::get('http://169.254.169.254/latest/meta-data') AS body",
        "SELECT * FROM entity LIMIT 1; INFO FOR ROOT",
        "SELECT * FROM entity LIMIT 1; SELECT * FROM users LIMIT 1",
    ],
)
async def test_debug_query_blocks_unsafe_debug_statements(query: str) -> None:
    org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
    request = DebugQueryRequest(cypher=query)

    with (
        patch("sibyl.api.routes.admin.execute_debug_query", AsyncMock()) as mock_execute,
        pytest.raises(HTTPException) as exc_info,
    ):
        await debug_query(request=request, org=org)

    assert exc_info.value.status_code == 400
    assert "single read-only SELECT" in str(exc_info.value.detail)
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
        patch(
            "sibyl.api.routes.admin.get_embedding_sweep_status",
            AsyncMock(return_value={"graph": {"state": "complete"}}),
        ) as sweep_status,
    ):
        response = await dev_status(org=org, user=SimpleNamespace(is_admin=False))

    # An organization owner is not a deployment admin.
    assert sweep_status.await_args.kwargs == {"deployment_admin": False}
    assert response.api_healthy is True
    assert response.graph_healthy is True
    assert response.embedding_sweep == {"graph": {"state": "complete"}}
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


class TestDebugDialectBuiltins:
    """The graph debug gate allowlists read-only builtins instead of banning ::."""

    @pytest.mark.parametrize(
        "query",
        [
            "SELECT string::len(name) FROM entity LIMIT 5",
            "SELECT math::mean(scores) FROM entity GROUP ALL",
            "SELECT time::now() FROM entity LIMIT 1",
            "SELECT type::thing('entity', id) FROM entity LIMIT 1",
            "SELECT string::semver::compare(a, b) FROM entity LIMIT 1",
        ],
    )
    def test_read_only_builtins_are_allowed(self, query: str) -> None:
        from sibyl.api.routes import admin

        assert admin._is_supported_debug_dialect(query)

    @pytest.mark.parametrize(
        "query",
        [
            "SELECT http::get('https://attacker.example') FROM entity",
            "SELECT function::custom(name) FROM entity",
            "SELECT ::orphan(name) FROM entity",
        ],
    )
    def test_unlisted_namespaces_are_rejected(self, query: str) -> None:
        from sibyl.api.routes import admin

        assert not admin._is_supported_debug_dialect(query)

    def test_separator_inside_string_literal_is_ignored(self) -> None:
        from sibyl.api.routes import admin

        assert admin._is_supported_debug_dialect(
            "SELECT name FROM entity WHERE name = 'http::get' LIMIT 1"
        )

    def test_mutating_statements_stay_rejected(self) -> None:
        from sibyl.api.routes import admin

        assert not admin._is_supported_debug_dialect("DELETE entity")
        assert not admin._is_supported_debug_dialect("SELECT * FROM entity; DELETE entity")


@pytest.mark.asyncio
async def test_surreal_observability_never_returns_or_requests_url_credentials(
    monkeypatch,
) -> None:
    requests: list[tuple[str, object]] = []

    class Response:
        status_code = 200
        text = "surrealdb_up 1\n"

    class Client:
        def __init__(self, **_kwargs: object) -> None:
            pass

        async def __aenter__(self) -> Client:
            return self

        async def __aexit__(self, *_exc: object) -> None:
            return None

        async def get(self, url: str, auth: object = None) -> Response:
            requests.append((url, auth))
            return Response()

    monkeypatch.setattr(admin_routes.httpx, "AsyncClient", Client)
    monkeypatch.setattr(
        admin_routes,
        "settings",
        SimpleNamespace(
            resolved_surreal_url="wss://admin:Hunter2@surreal.example.com:8443/rpc?t=Hunter2",
            surreal_username="",
            surreal_password=SimpleNamespace(get_secret_value=lambda: ""),
        ),
    )

    status = await admin_routes.get_surreal_observability_status()

    assert status["base_url"] == "https://surreal.example.com:8443"
    assert "hunter2" not in json.dumps(status).lower()
    assert [url for url, _auth in requests] == [
        "https://surreal.example.com:8443/health",
        "https://surreal.example.com:8443/metrics",
    ]
    # The URL's own userinfo still authenticates both reads, percent-decoded.
    assert [auth for _url, auth in requests] == [("admin", "Hunter2"), ("admin", "Hunter2")]


@pytest.mark.asyncio
async def test_surreal_observability_shows_no_path_and_authenticates_health(
    monkeypatch,
) -> None:
    requests: list[tuple[str, object]] = []

    class Client:
        def __init__(self, **_kwargs: object) -> None:
            pass

        async def __aenter__(self) -> Client:
            return self

        async def __aexit__(self, *_exc: object) -> None:
            return None

        async def get(self, url: str, auth: object = None) -> object:
            requests.append((url, auth))
            status = 200 if auth == ("a@b", "p:w") else 401
            return SimpleNamespace(status_code=status, text="")

    monkeypatch.setattr(admin_routes.httpx, "AsyncClient", Client)
    monkeypatch.setattr(
        admin_routes,
        "settings",
        SimpleNamespace(
            resolved_surreal_url="wss://a%40b:p%3Aw@host:8443/private/proxy/rpc",
            surreal_username="",
            surreal_password=SimpleNamespace(get_secret_value=lambda: ""),
        ),
    )

    status = await admin_routes.get_surreal_observability_status()

    # Requests keep the proxy path; the payload shows scheme, host, and port.
    assert [url for url, _auth in requests] == [
        "https://host:8443/private/proxy/health",
        "https://host:8443/private/proxy/metrics",
    ]
    assert status["base_url"] == "https://host:8443"
    assert status["health_http_status"] == 200
    assert status["metrics_http_status"] == 200


@pytest.mark.asyncio
async def test_embedding_sweep_status_reads_each_plane_and_survives_a_dead_store(
    monkeypatch,
) -> None:
    from sibyl.api.routes.admin import get_embedding_sweep_status

    states = {
        "graph": {
            "legacy_decision": "reembed",
            "legacy_basis": "prior_stamps_differ",
            "active_metadata": {"provider": "bedrock"},
            "complete_metadata": None,
            "last_run": {"status": "partial", "recovered": 384, "pending": 1200},
        }
    }

    async def read_state(plane, _organization_id, _execute):
        if plane == "document_chunks":
            raise ConnectionError("content store down")
        return states[plane]

    monkeypatch.setattr(
        "sibyl_core.services.graph_runtime.get_graph_client",
        AsyncMock(return_value=SimpleNamespace(execute_query=AsyncMock())),
    )
    monkeypatch.setattr(
        "sibyl_core.services.embedding_sweep.read_embedding_sweep_state", read_state
    )
    monkeypatch.setattr(
        "sibyl_core.backends.surreal.schema_embedding_states.embedding_sweep_schema_ready",
        AsyncMock(return_value=True),
    )

    status = await get_embedding_sweep_status("org")

    assert status["graph"]["state"] == "sweeping"
    assert status["graph"]["last_run"]["pending"] == 1200
    assert status["document_chunks"] == {"state": "unavailable", "error_type": "ConnectionError"}


async def test_embedding_sweep_status_names_schema_waits_and_evidence_waits(monkeypatch) -> None:
    from sibyl.api.routes.admin import get_embedding_sweep_status

    async def ready(_execute, *, graph):
        return graph

    async def read_state(plane, _organization_id, _execute):
        return {"legacy_deferred_at": "2026-09-26T00:00:00Z", "deferred_age_seconds": 120}

    monkeypatch.setattr(
        "sibyl_core.services.graph_runtime.get_graph_client",
        AsyncMock(return_value=SimpleNamespace(execute_query=AsyncMock())),
    )
    monkeypatch.setattr(
        "sibyl_core.backends.surreal.schema_embedding_states.embedding_sweep_schema_ready", ready
    )
    monkeypatch.setattr(
        "sibyl_core.services.embedding_sweep.read_embedding_sweep_state", read_state
    )
    monkeypatch.setattr(
        "sibyl_core.services.embedding_evidence.read_evidence_wait",
        AsyncMock(return_value={"organizations": ["broken-org"], "count": 1}),
    )

    owner = await get_embedding_sweep_status("org")
    admin = await get_embedding_sweep_status("org", deployment_admin=True)

    # The content schema has not upgraded; the graph plane waits on evidence.
    assert owner["document_chunks"] == {"state": "awaiting_schema_upgrade"}
    assert owner["graph"]["state"] == "awaiting_evidence"
    assert owner["graph"]["waiting_on_count"] == 1
    # Another tenant's identifier reaches only a deployment admin.
    assert "waiting_on_organizations" not in owner["graph"]
    assert admin["graph"]["waiting_on_organizations"] == ["broken-org"]


def test_embedding_plane_state_reports_only_the_current_model_as_complete() -> None:
    from sibyl.api.routes.admin import _embedding_plane_state

    first = {"provider": "openai", "model": "first"}
    second = {"provider": "bedrock", "model": "second"}

    assert _embedding_plane_state({"complete_metadata": first, "active_metadata": first}) == (
        "complete"
    )
    assert _embedding_plane_state({"complete_metadata": first, "active_metadata": second}) == (
        "sweeping"
    )
    assert (
        _embedding_plane_state(
            {"active_metadata": second, "last_run": {"status": "skipped_dimension_mismatch"}}
        )
        == "skipped_dimension_mismatch"
    )


def test_embedding_plane_state_never_calls_an_unproven_adoption_complete() -> None:
    from sibyl.api.routes.admin import _embedding_plane_state

    stamp = {"provider": "bedrock", "model": "cohere.embed-v4:0", "dimensions": 1024}

    assert (
        _embedding_plane_state(
            {
                "complete_metadata": stamp,
                "active_metadata": stamp,
                "legacy_warning": "adopted_without_evidence",
            }
        )
        == "adopted_without_evidence"
    )
    # A stamp that differs only in bookkeeping is still the model swept toward.
    assert (
        _embedding_plane_state(
            {
                "complete_metadata": {**stamp, "cache_namespace": "old"},
                "active_metadata": {**stamp, "cache_namespace": "new"},
            }
        )
        == "complete"
    )
