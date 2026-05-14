from __future__ import annotations

import ast

from tools.inventory.runtime_surface import (
    GRAPHITI_COMPATIBILITY_ALLOWLIST,
    GRAPHITI_EXIT_INVENTORY_PATH,
    PYPROJECT_PATHS,
    REPO_ROOT,
    SNAPSHOT_PATH,
    GraphitiImportRecord,
    RuntimeSurface,
    collect_runtime_surface,
    default_runtime_graphiti_imports,
    graphiti_allowlist_record,
    parse_dependency_name,
    render_markdown,
    unclassified_graphiti_imports,
)

EXPECTED_ROUTER_COUNT = 24
EXPECTED_HTTP_ROUTE_COUNT = 2
EXPECTED_WEBSOCKET_ROUTE_COUNT = 1
EXPECTED_MCP_TOOL_COUNT = 8
EXPECTED_MCP_RESOURCE_COUNT = 2
EXPECTED_SQLMODEL_TABLE_COUNT = 0
CORE_GRAPHITI_COMPATIBILITY_TESTS = (
    "tests/graph/surreal",
    "tests/test_graph_batch.py",
    "tests/test_graph_client.py",
    "tests/test_graph_entities.py",
    "tests/test_graph_relationships.py",
    "tests/test_graph_runtime_services.py",
    "tests/test_log_safety.py",
    "tests/test_migrate_archive.py",
    "tests/test_search_interface.py",
    "tests/test_surreal_authentication.py",
    "tests/test_surreal_observability.py",
)
CORE_GRAPHITI_COMPATIBILITY_MARKED_TESTS = (
    "tests/test_models.py",
    "tests/test_retrieval_advanced.py",
    "tests/test_tools_admin.py",
    "tests/test_tools_manage.py",
)
API_GRAPHITI_COMPATIBILITY_TESTS = (
    "tests/test_communities.py",
    "tests/test_e2e_workflows.py",
    "tests/test_graph_communities_lod.py",
    "tests/test_graph_entities.py",
    "tests/test_graph_relationships.py",
    "tests/test_harness.py",
    "tests/test_legacy_graph_persistence.py",
    "tests/test_tools_core.py",
)
API_GRAPHITI_COMPATIBILITY_MARKED_TESTS = (
    "tests/test_cli_db.py",
    "tests/test_cli_export.py",
    "tests/test_models.py",
    "tests/test_settings_api_key_loading.py",
    "tests/test_tools_manage.py",
)
GRAPHITI_OPS_ROOT = REPO_ROOT / "packages/python/sibyl-core/src/sibyl_core/graph/surreal/compat/ops"
GRAPHITI_OPS_CLASSIFICATIONS = (
    "delete",
    "migrate-to-native",
    "compatibility-retain",
    "admin-only",
    "benchmark-only",
    "historical migration",
)


def _embedded_no_graphiti_scripts() -> tuple[str, ...]:
    test_path = REPO_ROOT / "packages/python/sibyl-core/tests/test_no_graphiti_default_loop.py"
    tree = ast.parse(test_path.read_text(encoding="utf-8"), filename=str(test_path))
    return tuple(
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and "Graphiti import forbidden" in node.value
    )


def _script_imports(script: str) -> set[str]:
    tree = ast.parse(script)
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "import_module"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            imports.add(node.args[0].value)
    return imports


def runtime_surface_with_graphiti(
    *records: GraphitiImportRecord,
) -> RuntimeSurface:
    return RuntimeSurface(
        rest_routers=(),
        top_level_http_routes=(),
        websocket_routes=(),
        mcp_tools=(),
        mcp_resources=(),
        sqlmodel_tables=(),
        raw_sql_usage=(),
        session_storage_usage=(),
        graphiti_imports=records,
        dependencies=(),
    )


def test_dependency_parser_strips_extras_and_markers() -> None:
    requirement = 'graphiti-core[falkordb,anthropic]>=0.28.2 ; python_version >= "3.13"'
    assert parse_dependency_name(requirement) == "graphiti-core"


def test_runtime_surface_snapshot_is_current() -> None:
    surface = collect_runtime_surface()
    assert render_markdown(surface) == SNAPSHOT_PATH.read_text(encoding="utf-8")


def test_graphiti_exit_inventory_covers_runtime_imports() -> None:
    surface = collect_runtime_surface()

    assert GRAPHITI_EXIT_INVENTORY_PATH.exists()
    assert unclassified_graphiti_imports(surface) == ()
    assert default_runtime_graphiti_imports(surface) == ()


def test_graphiti_exit_inventory_rejects_docs_only_default_import(tmp_path) -> None:
    record = GraphitiImportRecord(
        path="apps/api/src/sibyl/api/routes/memory.py",
        imports=("graphiti_core.nodes",),
    )
    inventory_path = tmp_path / "inventory.md"
    inventory_path.write_text(f"`{record.path}`\n", encoding="utf-8")
    surface = runtime_surface_with_graphiti(record)

    assert default_runtime_graphiti_imports(surface) == (record,)
    assert unclassified_graphiti_imports(surface, inventory_path=inventory_path) == (record,)


def test_graphiti_exit_inventory_allows_named_compatibility_imports() -> None:
    record = GraphitiImportRecord(
        path="packages/python/sibyl-core/src/sibyl_core/graph/client.py",
        imports=("graphiti_core",),
    )
    surface = runtime_surface_with_graphiti(record)

    assert graphiti_allowlist_record(record.path) is not None
    assert default_runtime_graphiti_imports(surface) == ()


def test_graphiti_exit_inventory_detects_dynamic_imports() -> None:
    surface = collect_runtime_surface()
    record = next(
        record
        for record in surface.graphiti_imports
        if record.path == "packages/python/sibyl-core/src/sibyl_core/tools/admin.py"
    )

    assert record.imports == ("graphiti_core.edges", "graphiti_core.nodes")
    assert graphiti_allowlist_record(record.path) is not None


def test_graphiti_exit_inventory_documents_allowlist_ownership() -> None:
    inventory = GRAPHITI_EXIT_INVENTORY_PATH.read_text(encoding="utf-8")
    normalized_inventory = " ".join(inventory.split())

    for allowed in GRAPHITI_COMPATIBILITY_ALLOWLIST:
        assert f"`{allowed.path}`" in inventory
        assert f"Owner: {allowed.owner}" in normalized_inventory
        assert allowed.criteria in normalized_inventory


def test_graphiti_exit_inventory_classifies_each_ops_module() -> None:
    inventory = GRAPHITI_EXIT_INVENTORY_PATH.read_text(encoding="utf-8")
    ops_paths = tuple(
        path.relative_to(REPO_ROOT).as_posix() for path in sorted(GRAPHITI_OPS_ROOT.glob("*.py"))
    )

    assert ops_paths
    for ops_path in ops_paths:
        heading = f"#### `{ops_path}`"
        assert heading in inventory
        section = inventory.split(heading, maxsplit=1)[1].split("\n#### ", maxsplit=1)[0]
        assert any(
            f"- Classification: `{classification}`" in section
            for classification in GRAPHITI_OPS_CLASSIFICATIONS
        )
        assert "- Owner:" in section
        assert "- Removal condition:" in section
        assert "- Verify:" in section


def test_graphiti_compatibility_test_island_is_named() -> None:
    root_moon = (REPO_ROOT / "moon.yml").read_text(encoding="utf-8")
    core_moon = (REPO_ROOT / "packages/python/sibyl-core/moon.yml").read_text(encoding="utf-8")
    api_moon = (REPO_ROOT / "apps/api/moon.yml").read_text(encoding="utf-8")
    root_pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    api_pyproject = (REPO_ROOT / "apps/api/pyproject.toml").read_text(encoding="utf-8")
    inventory = GRAPHITI_EXIT_INVENTORY_PATH.read_text(encoding="utf-8")

    assert "graphiti-compatibility-test:" in root_moon
    assert "core:graphiti-compatibility-test" in root_moon
    assert "api:graphiti-compatibility-test" in root_moon
    assert "graphiti_compatibility:" in root_pyproject
    assert "graphiti_compatibility:" in api_pyproject
    assert '-m "not graphiti_compatibility"' in core_moon
    assert "-m graphiti_compatibility" in core_moon
    assert '-m "not graphiti_compatibility"' in api_moon
    assert "-m graphiti_compatibility" in api_moon

    for test_path in CORE_GRAPHITI_COMPATIBILITY_TESTS:
        assert f"--ignore={test_path}" in core_moon
        assert f"      {test_path}" in core_moon
        assert f"`packages/python/sibyl-core/{test_path}`" in inventory

    for test_path in CORE_GRAPHITI_COMPATIBILITY_MARKED_TESTS:
        assert f"      {test_path}" in core_moon
        assert f"`packages/python/sibyl-core/{test_path}`" in inventory

    for test_path in API_GRAPHITI_COMPATIBILITY_TESTS:
        assert f"--ignore={test_path}" in api_moon
        assert f"      {test_path}" in api_moon
        assert f"`apps/api/{test_path}`" in inventory

    for test_path in API_GRAPHITI_COMPATIBILITY_MARKED_TESTS:
        assert f"      {test_path}" in api_moon
        assert f"`apps/api/{test_path}`" in inventory


def test_graphiti_exit_inventory_tracks_no_graphiti_smoke_plan() -> None:
    inventory = GRAPHITI_EXIT_INVENTORY_PATH.read_text(encoding="utf-8")

    assert "## No-Graphiti Smoke Plan" in inventory
    assert "moon run core:no-graphiti-smoke" in inventory
    assert "tests/test_no_graphiti_default_loop.py" in inventory
    assert "blocks `graphiti_core` imports" in inventory
    for loop_name in ("remember", "recall", "context", "wake", "reflect"):
        assert f"- `{loop_name}`:" in inventory
    assert "Current blockers:" not in inventory


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
    raw_sql_paths = {record.path for record in surface.raw_sql_usage}
    assert raw_sql_paths == set()
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
        (record.project, record.scope, record.dependency, record.classification)
        for record in surface.dependencies
    }

    assert (
        "packages/python/sibyl-core/pyproject.toml",
        "optional:compatibility",
        "graphiti-core[anthropic,google-genai]>=0.28.2",
        "graph",
    ) in dependencies
    assert (
        "packages/python/sibyl-core/pyproject.toml",
        "dependency-group:dev",
        "graphiti-core[anthropic,google-genai]>=0.28.2",
        "graph",
    ) in dependencies
    assert (
        "apps/api/pyproject.toml",
        "default",
        "surrealdb>=1.0.8,<3.0",
        "target",
    ) in dependencies


def test_dependency_inventory_scans_all_repo_pyprojects() -> None:
    scanned = {path.relative_to(REPO_ROOT).as_posix() for path in PYPROJECT_PATHS}

    assert {
        "apps/api/pyproject.toml",
        "apps/cli/pyproject.toml",
        "apps/e2e/pyproject.toml",
        "hooks/pyproject.toml",
        "packages/python/sibyl-core/pyproject.toml",
        "pyproject.toml",
    } <= scanned


def test_graphiti_dependency_is_compatibility_only() -> None:
    surface = collect_runtime_surface()
    graphiti_dependencies = tuple(
        record
        for record in surface.dependencies
        if parse_dependency_name(record.dependency) == "graphiti-core"
    )

    assert graphiti_dependencies
    assert {record.project for record in graphiti_dependencies} == {
        "packages/python/sibyl-core/pyproject.toml"
    }
    assert all(record.scope != "default" for record in graphiti_dependencies)
    assert any(record.scope == "optional:compatibility" for record in graphiti_dependencies)
    assert all(
        record.scope == "optional:compatibility" or record.scope.startswith("dependency-group:")
        for record in graphiti_dependencies
    )
    assert all("anthropic" in record.dependency for record in graphiti_dependencies)
    assert all("google-genai" in record.dependency for record in graphiti_dependencies)
    assert all("falkordb" not in record.dependency for record in graphiti_dependencies)


def test_no_graphiti_smoke_covers_default_entrypoints() -> None:
    scripts = _embedded_no_graphiti_scripts()
    entrypoint_script = next(script for script in scripts if "create_api_app" in script)
    imports = _script_imports(entrypoint_script)

    for expected in (
        "sibyl.api.app",
        "sibyl.main",
        "sibyl.server",
        "sibyl.jobs.worker",
        "sibyl_core.retrieval.native",
        "sibyl_cli.main",
    ):
        assert expected in imports

    for expected in (
        "apps/cli/src/sibyl_cli/data/hooks/session-start.py",
        "apps/cli/src/sibyl_cli/data/hooks/user-prompt-submit.py",
    ):
        assert expected in entrypoint_script
