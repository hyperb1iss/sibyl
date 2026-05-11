from __future__ import annotations

from tools.inventory.runtime_surface import (
    SNAPSHOT_PATH,
    collect_runtime_surface,
    parse_dependency_name,
    render_markdown,
)

EXPECTED_ROUTER_COUNT = 24
EXPECTED_HTTP_ROUTE_COUNT = 2
EXPECTED_WEBSOCKET_ROUTE_COUNT = 1
EXPECTED_MCP_TOOL_COUNT = 8
EXPECTED_MCP_RESOURCE_COUNT = 2
EXPECTED_SQLMODEL_TABLE_COUNT = 24


def test_dependency_parser_strips_extras_and_markers() -> None:
    requirement = 'graphiti-core[falkordb,anthropic]>=0.28.2 ; python_version >= "3.13"'
    assert parse_dependency_name(requirement) == "graphiti-core"


def test_runtime_surface_snapshot_is_current() -> None:
    surface = collect_runtime_surface()
    assert render_markdown(surface) == SNAPSHOT_PATH.read_text(encoding="utf-8")


def test_runtime_surface_finds_known_contracts() -> None:
    surface = collect_runtime_surface()

    assert len(surface.rest_routers) == EXPECTED_ROUTER_COUNT
    assert len(surface.top_level_http_routes) == EXPECTED_HTTP_ROUTE_COUNT
    assert len(surface.websocket_routes) == EXPECTED_WEBSOCKET_ROUTE_COUNT
    assert len(surface.mcp_tools) == EXPECTED_MCP_TOOL_COUNT
    assert len(surface.mcp_resources) == EXPECTED_MCP_RESOURCE_COUNT
    assert len(surface.sqlmodel_tables) == EXPECTED_SQLMODEL_TABLE_COUNT

    assert "search_router" in surface.rest_routers
    assert surface.websocket_routes[0].path == "/ws"
    assert {record.name for record in surface.mcp_tools} >= {"search", "explore", "add"}
    assert "User" in surface.sqlmodel_tables
    assert any(
        record.path == "apps/api/src/sibyl/persistence/legacy/crawler.py"
        for record in surface.raw_sql_usage
    )
    assert not any(
        record.path == "apps/api/src/sibyl/server.py" for record in surface.raw_sql_usage
    )
    session_storage_paths = {record.path for record in surface.session_storage_usage}
    assert "apps/api/src/sibyl/persistence/content_runtime.py" not in session_storage_paths
    assert "apps/api/src/sibyl/persistence/settings_runtime.py" not in session_storage_paths
    assert session_storage_paths == set()
    assert any(
        record.path == "packages/python/sibyl-core/src/sibyl_core/graph/entities.py"
        for record in surface.graphiti_imports
    )


def test_dependency_inventory_covers_legacy_and_target_stack() -> None:
    surface = collect_runtime_surface()
    dependencies = {
        (record.project, record.dependency, record.classification)
        for record in surface.dependencies
    }

    assert (
        "packages/python/sibyl-core/pyproject.toml",
        "graphiti-core[falkordb,anthropic,google-genai]>=0.28.2",
        "legacy",
    ) in dependencies
    assert (
        "apps/api/pyproject.toml",
        "surrealdb>=1.0.8,<3.0",
        "target",
    ) in dependencies
