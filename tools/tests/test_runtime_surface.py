from __future__ import annotations

import ast

import pytest
from tools.inventory.runtime_surface import (
    GRAPHITI_COMPATIBILITY_ALLOWLIST,
    GRAPHITI_EXIT_INVENTORY_PATH,
    LEGACY_TERM_ALLOWLIST,
    PYPROJECT_PATHS,
    REPO_ROOT,
    SNAPSHOT_PATH,
    GraphitiImportRecord,
    LegacyTermAllowlistRecord,
    LegacyTermRecord,
    RuntimeSurface,
    _path_matches_allowlist,
    check_legacy_term_inventory,
    collect_runtime_surface,
    default_runtime_graphiti_imports,
    graphiti_allowlist_record,
    graphiti_dynamic_import_name,
    iter_legacy_term_files,
    legacy_term_allowlist_record,
    parse_dependency_name,
    render_markdown,
    unclassified_graphiti_imports,
    unclassified_legacy_term_records,
)

EXPECTED_ROUTER_COUNT = 28
EXPECTED_HTTP_ROUTE_COUNT = 2
EXPECTED_WEBSOCKET_ROUTE_COUNT = 1
EXPECTED_MCP_TOOL_COUNT = 11
EXPECTED_MCP_RESOURCE_COUNT = 2
EXPECTED_SQLMODEL_TABLE_COUNT = 0
_GRAPHITI_PACKAGE = "graphiti" + "-core"
_GRAPHITI_MODULE = "graphiti" + "_core"
EXPECTED_LEGACY_TERM_SCAN_PATHS = {
    ".devcontainer/Dockerfile",
    ".devcontainer/devcontainer.json",
    ".env.example",
    ".env.quickstart.example",
    ".env.quickstart.test",
    ".env.test.example",
    "AGENTS.md",
    "CLAUDE.md",
    "Tiltfile",
    "apps/api/Dockerfile",
    "apps/api/pyproject.toml",
    "apps/web/Dockerfile",
    "apps/web/package.json",
    "charts/sibyl/templates/_helpers.tpl",
    "infra/local/README.md",
    "infra/local/secrets.yaml.example",
    "infra/local/sibyl-values.yaml",
    "infra/local/valkey-values.yaml",
    "moon.yml",
    "package.json",
    "packages/python/sibyl-core/README.md",
    "packages/python/sibyl-core/moon.yml",
    "pnpm-workspace.yaml",
    "pyproject.toml",
    "skills/agent-activity-audit/EXAMPLES.md",
    "setup-dev.sh",
    "tools/dev/run-surreal-dev.sh",
}
EXPECTED_LEGACY_TERM_RECORD_PATHS = EXPECTED_LEGACY_TERM_SCAN_PATHS - {
    ".devcontainer/Dockerfile",
    ".devcontainer/devcontainer.json",
    "apps/api/Dockerfile",
    "apps/web/Dockerfile",
    "apps/web/package.json",
    "charts/sibyl/templates/_helpers.tpl",
    "package.json",
    "pnpm-workspace.yaml",
    "pyproject.toml",
}
CORE_LEGACY_GRAPH_CONTRACT_TESTS = (
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


def test_install_surfaces_default_to_local_first_auth() -> None:
    env_example = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    ansible_defaults = (REPO_ROOT / "infra/ansible/roles/sibyl/defaults/main.yml").read_text(
        encoding="utf-8"
    )
    ansible_env = (REPO_ROOT / "infra/ansible/roles/sibyl/templates/env.j2").read_text(
        encoding="utf-8"
    )
    ansible_compose = (REPO_ROOT / "infra/ansible/roles/sibyl/files/docker-compose.yml").read_text(
        encoding="utf-8"
    )
    helm_values = (REPO_ROOT / "charts/sibyl/values.yaml").read_text(encoding="utf-8")

    assert "SIBYL_LOCAL_AUTH_ENABLED=true" in env_example
    assert "SIBYL_PUBLIC_SIGNUPS_ENABLED=false" in env_example
    assert "SIBYL_BREAK_GLASS_ENABLED=false" in env_example
    assert "SIBYL_MCP_AUTH_MODE=auto" in env_example

    assert "sibyl_local_auth_enabled: true" in ansible_defaults
    assert "sibyl_public_signups_enabled: false" in ansible_defaults
    assert "sibyl_break_glass_enabled: false" in ansible_defaults
    assert 'sibyl_mcp_auth_mode: "auto"' in ansible_defaults
    assert "SIBYL_LOCAL_AUTH_ENABLED={{ sibyl_local_auth_enabled | lower }}" in ansible_env
    assert "SIBYL_PUBLIC_SIGNUPS_ENABLED={{ sibyl_public_signups_enabled | lower }}" in ansible_env
    assert "SIBYL_BREAK_GLASS_ENABLED={{ sibyl_break_glass_enabled | lower }}" in ansible_env
    assert "SIBYL_LOCAL_AUTH_ENABLED: ${SIBYL_LOCAL_AUTH_ENABLED:-true}" in ansible_compose
    assert "SIBYL_PUBLIC_SIGNUPS_ENABLED: ${SIBYL_PUBLIC_SIGNUPS_ENABLED:-false}" in ansible_compose
    assert "SIBYL_BREAK_GLASS_ENABLED: ${SIBYL_BREAK_GLASS_ENABLED:-false}" in ansible_compose
    assert "SIBYL_MCP_AUTH_MODE: ${SIBYL_MCP_AUTH_MODE:-auto}" in ansible_compose

    assert "localAuthEnabled: true" in helm_values
    assert "publicSignupsEnabled: false" in helm_values
    assert "providers: []" in helm_values
    assert "silent_refresh_enabled: false" in helm_values
    assert "extra_providers_enabled: false" in helm_values


CORE_LEGACY_GRAPH_CONTRACT_MARKED_TESTS = (
    "tests/test_models.py",
    "tests/test_retrieval_advanced.py",
    "tests/test_tools_admin.py",
    "tests/test_tools_manage.py",
)
API_LEGACY_GRAPH_CONTRACT_TESTS = (
    "tests/test_communities.py",
    "tests/test_e2e_workflows.py",
    "tests/test_graph_communities_lod.py",
    "tests/test_graph_entities.py",
    "tests/test_graph_relationships.py",
    "tests/test_harness.py",
    "tests/test_legacy_graph_persistence.py",
    "tests/test_tools_core.py",
)
API_LEGACY_GRAPH_CONTRACT_MARKED_TESTS = (
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
GRAPHITI_OPS_IMPORT_ALLOWLIST = {
    "packages/python/sibyl-core/src/sibyl_core/backends/surreal/driver.py",
    "packages/python/sibyl-core/src/sibyl_core/graph/search_interface.py",
}
GRAPHITI_OPS_IMPORT_PREFIX = "sibyl_core.graph.surreal.compat.ops"


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


def _imports_graphiti_ops(path) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(
                alias.name == GRAPHITI_OPS_IMPORT_PREFIX
                or alias.name.startswith(f"{GRAPHITI_OPS_IMPORT_PREFIX}.")
                for alias in node.names
            ):
                return True
        elif (
            isinstance(node, ast.ImportFrom)
            and node.module
            and (
                node.module == GRAPHITI_OPS_IMPORT_PREFIX
                or node.module.startswith(f"{GRAPHITI_OPS_IMPORT_PREFIX}.")
            )
        ):
            return True
    return False


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
        legacy_term_records=(),
        dependencies=(),
    )


def runtime_surface_with_legacy_terms(
    *records: LegacyTermRecord,
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
        graphiti_imports=(),
        legacy_term_records=records,
        dependencies=(),
    )


def test_dependency_parser_strips_extras_and_markers() -> None:
    requirement = f'{_GRAPHITI_PACKAGE}[falkordb,anthropic]>=0.28.2 ; python_version >= "3.13"'
    assert parse_dependency_name(requirement) == _GRAPHITI_PACKAGE


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
        imports=(f"{_GRAPHITI_MODULE}.nodes",),
    )
    inventory_path = tmp_path / "inventory.md"
    inventory_path.write_text(f"`{record.path}`\n", encoding="utf-8")
    surface = runtime_surface_with_graphiti(record)

    assert default_runtime_graphiti_imports(surface) == (record,)
    assert unclassified_graphiti_imports(surface, inventory_path=inventory_path) == (record,)


def test_graphiti_exit_inventory_rejects_former_compatibility_imports() -> None:
    record = GraphitiImportRecord(
        path="packages/python/sibyl-core/src/sibyl_core/graph/surreal/compat/ops/entity_node_ops.py",
        imports=(_GRAPHITI_MODULE,),
    )
    surface = runtime_surface_with_graphiti(record)

    assert graphiti_allowlist_record(record.path) is None
    assert default_runtime_graphiti_imports(surface) == (record,)


def test_graphiti_exit_inventory_detects_dynamic_imports() -> None:
    tree = ast.parse(
        """
from importlib import import_module

import_module("GRAPHITI_MODULE.edges")
__import__("graphiti.nodes")
import_module("sibyl_core.graph")
""".replace("GRAPHITI_MODULE", _GRAPHITI_MODULE)
    )
    imports = tuple(
        dynamic_import
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        for dynamic_import in [graphiti_dynamic_import_name(node)]
        if dynamic_import is not None
    )

    assert imports == (f"{_GRAPHITI_MODULE}.edges", "graphiti.nodes")


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


def test_graphiti_ops_imports_stay_in_named_compatibility_island() -> None:
    source_roots = (
        REPO_ROOT / "apps/api/src",
        REPO_ROOT / "packages/python/sibyl-core/src",
    )
    ops_root = REPO_ROOT / "packages/python/sibyl-core/src/sibyl_core/graph/surreal/compat/ops"
    offenders: list[str] = []
    for source_root in source_roots:
        for path in sorted(source_root.rglob("*.py")):
            if path.is_relative_to(ops_root):
                continue
            relative_path = path.relative_to(REPO_ROOT).as_posix()
            if relative_path not in GRAPHITI_OPS_IMPORT_ALLOWLIST and _imports_graphiti_ops(path):
                offenders.append(relative_path)

    assert offenders == []


def test_legacy_graph_contract_test_island_is_named() -> None:
    root_moon = (REPO_ROOT / "moon.yml").read_text(encoding="utf-8")
    core_moon = (REPO_ROOT / "packages/python/sibyl-core/moon.yml").read_text(encoding="utf-8")
    api_moon = (REPO_ROOT / "apps/api/moon.yml").read_text(encoding="utf-8")
    root_pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    api_pyproject = (REPO_ROOT / "apps/api/pyproject.toml").read_text(encoding="utf-8")
    inventory = GRAPHITI_EXIT_INVENTORY_PATH.read_text(encoding="utf-8")

    assert "legacy-graph-contract-test:" in root_moon
    assert "core:legacy-graph-contract-test" in root_moon
    assert "api:legacy-graph-contract-test" in root_moon
    assert "legacy_graph_contract:" in root_pyproject
    assert "legacy_graph_contract:" in api_pyproject
    assert '-m "not legacy_graph_contract"' in core_moon
    assert "-m legacy_graph_contract" in core_moon
    assert '-m "not legacy_graph_contract"' in api_moon
    assert "-m legacy_graph_contract" in api_moon

    for test_path in CORE_LEGACY_GRAPH_CONTRACT_TESTS:
        assert f"--ignore={test_path}" in core_moon
        assert f"      {test_path}" in core_moon
        assert f"`packages/python/sibyl-core/{test_path}`" in inventory

    for test_path in CORE_LEGACY_GRAPH_CONTRACT_MARKED_TESTS:
        assert f"      {test_path}" in core_moon
        assert f"`packages/python/sibyl-core/{test_path}`" in inventory

    for test_path in API_LEGACY_GRAPH_CONTRACT_TESTS:
        assert f"--ignore={test_path}" in api_moon
        assert f"      {test_path}" in api_moon
        assert f"`apps/api/{test_path}`" in inventory

    for test_path in API_LEGACY_GRAPH_CONTRACT_MARKED_TESTS:
        assert f"      {test_path}" in api_moon
        assert f"`apps/api/{test_path}`" in inventory


def test_graphiti_exit_inventory_tracks_no_graphiti_smoke_plan() -> None:
    inventory = GRAPHITI_EXIT_INVENTORY_PATH.read_text(encoding="utf-8")

    assert "## No-Graphiti Smoke Plan" in inventory
    assert "moon run core:no-graphiti-smoke" in inventory
    assert "tests/test_no_graphiti_default_loop.py" in inventory
    assert f"blocks `{_GRAPHITI_MODULE}` imports" in inventory
    for loop_name in ("remember", "recall", "context", "wake", "reflect"):
        assert f"- `{loop_name}`:" in inventory
    assert "Current blockers:" not in inventory


def test_legacy_term_inventory_covers_active_docs_and_configs() -> None:
    surface = collect_runtime_surface()
    inventory = SNAPSHOT_PATH.read_text(encoding="utf-8")
    legacy_inventory = inventory.split("## Retained Legacy Term Inventory", maxsplit=1)[1].split(
        "\n## Dependency Inventory",
        maxsplit=1,
    )[0]
    record_paths = {record.path for record in surface.legacy_term_records}
    literal_allowlist_paths = {
        allowed.path for allowed in LEGACY_TERM_ALLOWLIST if not allowed.path.endswith("*")
    }

    assert "## Retained Legacy Term Inventory" in inventory
    assert unclassified_legacy_term_records(surface) == ()
    assert record_paths >= EXPECTED_LEGACY_TERM_RECORD_PATHS
    assert record_paths >= literal_allowlist_paths
    for record in surface.legacy_term_records:
        allowed = legacy_term_allowlist_record(record.path)
        assert allowed is not None
        matching_rows = [
            line
            for line in legacy_inventory.splitlines()
            if line.startswith(f"| `{record.path}` |")
        ]
        assert len(matching_rows) == 1
        row = matching_rows[0]
        assert f"| {allowed.owner} |" in row
        assert f"| {allowed.reason} |" in row


def test_legacy_term_scanner_covers_active_docs_and_configs() -> None:
    scanned_paths = {path.relative_to(REPO_ROOT).as_posix() for path in iter_legacy_term_files()}

    assert scanned_paths >= EXPECTED_LEGACY_TERM_SCAN_PATHS


def test_legacy_term_inventory_rejects_unowned_active_doc() -> None:
    record = LegacyTermRecord(
        path="docs/guide/new-default-postgres.md",
        terms=("postgres",),
        count=1,
    )
    surface = runtime_surface_with_legacy_terms(record)

    assert legacy_term_allowlist_record(record.path) is None
    assert unclassified_legacy_term_records(surface) == (record,)


def test_legacy_term_inventory_check_reports_unowned_doc(capsys) -> None:
    record = LegacyTermRecord(
        path="docs/guide/new-default-postgres.md",
        terms=("postgres",),
        count=1,
    )
    surface = runtime_surface_with_legacy_terms(record)

    assert check_legacy_term_inventory(surface) == 1
    captured = capsys.readouterr()
    assert "Legacy term inventory is missing 1 active doc/config files:" in captured.err
    assert "- docs/guide/new-default-postgres.md" in captured.err


def test_allowlist_matching_rejects_bare_wildcard() -> None:
    record = LegacyTermAllowlistRecord(path="*", owner="bad", reason="bad")

    with pytest.raises(ValueError, match="Bare wildcard"):
        _path_matches_allowlist("docs/guide/new-default-postgres.md", record)


def test_runtime_surface_finds_known_contracts() -> None:
    surface = collect_runtime_surface()

    assert len(surface.rest_routers) == EXPECTED_ROUTER_COUNT
    assert len(surface.top_level_http_routes) == EXPECTED_HTTP_ROUTE_COUNT
    assert len(surface.websocket_routes) == EXPECTED_WEBSOCKET_ROUTE_COUNT
    assert len(surface.mcp_tools) == EXPECTED_MCP_TOOL_COUNT
    assert len(surface.mcp_resources) == EXPECTED_MCP_RESOURCE_COUNT
    assert len(surface.sqlmodel_tables) == EXPECTED_SQLMODEL_TABLE_COUNT

    assert "search_router" in surface.rest_routers
    assert "synthesis_router" in surface.rest_routers
    assert "ai_settings_router" in surface.rest_routers
    assert surface.websocket_routes[0].path == "/ws"
    assert {record.name for record in surface.mcp_tools} >= {
        "search",
        "explore",
        "add",
        "synthesis_plan",
        "synthesis_draft",
        "synthesis_verify",
    }
    raw_sql_paths = {record.path for record in surface.raw_sql_usage}
    assert raw_sql_paths == set()
    assert not any(
        record.path == "apps/api/src/sibyl/server.py" for record in surface.raw_sql_usage
    )
    session_storage_paths = {record.path for record in surface.session_storage_usage}
    assert "apps/api/src/sibyl/persistence/content_runtime.py" not in session_storage_paths
    assert "apps/api/src/sibyl/persistence/settings_runtime.py" not in session_storage_paths
    assert session_storage_paths == set()
    assert surface.graphiti_imports == ()


def test_dependency_inventory_covers_legacy_and_target_stack() -> None:
    surface = collect_runtime_surface()
    dependencies = {
        (record.project, record.scope, record.dependency, record.classification)
        for record in surface.dependencies
    }

    assert (
        "apps/api/pyproject.toml",
        "default",
        "surrealdb>=2.0.0,<3.0",
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


def test_graphiti_dependency_is_absent() -> None:
    surface = collect_runtime_surface()
    graphiti_dependencies = tuple(
        record
        for record in surface.dependencies
        if parse_dependency_name(record.dependency) == _GRAPHITI_PACKAGE
    )

    assert graphiti_dependencies == ()


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

    for expected in ("apps/cli/src/sibyl_cli/data/hooks/session-start.py",):
        assert expected in entrypoint_script
