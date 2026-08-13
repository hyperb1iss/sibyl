from __future__ import annotations

import ast
import shutil
import subprocess

import pytest
from tools.inventory.runtime_surface import (
    GRAPHITI_COMPATIBILITY_ALLOWLIST,
    GRAPHITI_EXIT_INVENTORY_PATH,
    PYPROJECT_PATHS,
    REPO_ROOT,
    DependencyRecord,
    GraphitiCompatibilityRecord,
    GraphitiImportRecord,
    RuntimeSurface,
    SqlUsageRecord,
    _path_matches_allowlist,
    check_runtime_purity,
    collect_runtime_surface,
    default_runtime_graphiti_imports,
    graphiti_allowlist_record,
    graphiti_dynamic_import_name,
    parse_dependency_name,
    unclassified_graphiti_imports,
)
from tools.trust.enterprise_readiness_evidence import SIBYL_HELM_RENDER_ARGS

EXPECTED_ROUTER_COUNT = 31
EXPECTED_HTTP_ROUTE_COUNT = 3
EXPECTED_WEBSOCKET_ROUTE_COUNT = 1
EXPECTED_MCP_TOOL_COUNT = 13
EXPECTED_MCP_RESOURCE_COUNT = 2
EXPECTED_SQLMODEL_TABLE_COUNT = 0
_GRAPHITI_PACKAGE = "graphiti" + "-core"
_GRAPHITI_MODULE = "graphiti" + "_core"
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


def test_helm_runtime_secret_requires_stable_settings_key() -> None:
    backend = (REPO_ROOT / "charts/sibyl/templates/backend-deployment.yaml").read_text(
        encoding="utf-8"
    )
    worker = (REPO_ROOT / "charts/sibyl/templates/worker-deployment.yaml").read_text(
        encoding="utf-8"
    )

    assert "key: SIBYL_SETTINGS_KEY" in backend
    assert "key: SIBYL_SETTINGS_KEY" in worker


_HELM_BINARY = shutil.which("helm")
requires_helm = pytest.mark.skipif(_HELM_BINARY is None, reason="helm CLI is not installed")


def _helm_template(*overrides: str) -> subprocess.CompletedProcess[str]:
    assert _HELM_BINARY is not None
    return subprocess.run(  # noqa: S603
        [_HELM_BINARY, "template", "sibyl", "charts/sibyl", *overrides],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def test_helm_production_auth_secret_guard_is_wired() -> None:
    helpers = (REPO_ROOT / "charts/sibyl/templates/_helpers.tpl").read_text(encoding="utf-8")
    configmap = (REPO_ROOT / "charts/sibyl/templates/configmap.yaml").read_text(encoding="utf-8")

    assert 'define "sibyl.validateProductionAuthSecret"' in helpers
    assert 'include "sibyl.validateProductionAuthSecret" .' in configmap


@requires_helm
def test_helm_production_render_fails_without_a_jwt_secret_source() -> None:
    result = _helm_template()

    assert result.returncode != 0
    assert "backend.existingSecret is required" in result.stderr
    assert "SIBYL_JWT_SECRET" in result.stderr


@requires_helm
def test_helm_production_render_succeeds_with_an_existing_secret() -> None:
    result = _helm_template("--set", "backend.existingSecret=sibyl-secrets")

    assert result.returncode == 0, result.stderr
    assert "key: SIBYL_JWT_SECRET" in result.stdout
    assert "key: SIBYL_SETTINGS_KEY" in result.stdout


@requires_helm
def test_helm_non_production_render_keeps_development_jwt_autogeneration() -> None:
    result = _helm_template("--set", "backend.env.SIBYL_ENVIRONMENT=development")

    assert result.returncode == 0, result.stderr
    assert 'SIBYL_ENVIRONMENT: "development"' in result.stdout
    assert "key: SIBYL_JWT_SECRET" not in result.stdout


@requires_helm
def test_helm_production_render_rejects_a_configmap_resident_jwt_secret() -> None:
    """An inline env secret satisfies neither guard, with or without a Secret alongside it."""
    inline_only = _helm_template("--set", "backend.env.SIBYL_JWT_SECRET=hunter2")

    assert inline_only.returncode != 0
    assert "backend.existingSecret is required" in inline_only.stderr
    assert "does not satisfy this" in inline_only.stderr

    alongside_secret = _helm_template(
        "--set",
        "backend.existingSecret=sibyl-secrets",
        "--set",
        "backend.env.SIBYL_JWT_SECRET=hunter2",
    )

    assert alongside_secret.returncode != 0
    assert "backend.env.SIBYL_JWT_SECRET must not be used in production" in alongside_secret.stderr


@requires_helm
def test_helm_production_guard_matches_env_keys_case_insensitively() -> None:
    """pydantic-settings resolves env vars case-insensitively, so the guard must too."""
    for key in ("sibyl_jwt_secret", "Sibyl_Jwt_Secret", "JWT_SECRET", "jwt_secret"):
        result = _helm_template(
            "--set",
            "backend.existingSecret=sibyl-secrets",
            "--set",
            f"backend.env.{key}=hunter2",
        )

        assert result.returncode != 0, f"{key} rendered instead of failing"
        assert f"backend.env.{key} must not be used in production" in result.stderr

    lowercase_production = _helm_template(
        "--set",
        "backend.env.SIBYL_ENVIRONMENT=null",
        "--set",
        "backend.env.sibyl_environment=production",
    )

    assert lowercase_production.returncode != 0
    assert "backend.existingSecret is required" in lowercase_production.stderr


@requires_helm
def test_helm_enterprise_evidence_render_args_still_render() -> None:
    """The readiness evidence tool renders charts/sibyl with these exact overrides."""
    assert _HELM_BINARY is not None
    result = subprocess.run(  # noqa: S603
        [_HELM_BINARY, *SIBYL_HELM_RENDER_ARGS],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "key: SIBYL_JWT_SECRET" in result.stdout


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
    test_path = REPO_ROOT / "packages/python/sibyl-core/tests/test_default_memory_loop.py"
    tree = ast.parse(test_path.read_text(encoding="utf-8"), filename=str(test_path))
    return tuple(
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and ("async def main" in node.value or "create_api_app" in node.value)
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
        dependencies=(),
    )


def runtime_surface_with_storage(
    *,
    sqlmodel_tables: tuple[str, ...] = (),
    raw_sql_usage: tuple[SqlUsageRecord, ...] = (),
    session_storage_usage: tuple[SqlUsageRecord, ...] = (),
    dependencies: tuple[DependencyRecord, ...] = (),
) -> RuntimeSurface:
    return RuntimeSurface(
        rest_routers=(),
        top_level_http_routes=(),
        websocket_routes=(),
        mcp_tools=(),
        mcp_resources=(),
        sqlmodel_tables=sqlmodel_tables,
        raw_sql_usage=raw_sql_usage,
        session_storage_usage=session_storage_usage,
        graphiti_imports=(),
        dependencies=dependencies,
    )


def test_dependency_parser_strips_extras_and_markers() -> None:
    requirement = f'{_GRAPHITI_PACKAGE}[falkordb,anthropic]>=0.28.2 ; python_version >= "3.13"'
    assert parse_dependency_name(requirement) == _GRAPHITI_PACKAGE


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


def test_graphiti_ops_modules_are_deleted() -> None:
    ops_paths = tuple(
        path.relative_to(REPO_ROOT).as_posix() for path in sorted(GRAPHITI_OPS_ROOT.glob("*.py"))
    )

    assert ops_paths == ()


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


def test_legacy_graph_contract_test_island_is_retired() -> None:
    root_moon = (REPO_ROOT / "moon.yml").read_text(encoding="utf-8")
    core_moon = (REPO_ROOT / "packages/python/sibyl-core/moon.yml").read_text(encoding="utf-8")
    api_moon = (REPO_ROOT / "apps/api/moon.yml").read_text(encoding="utf-8")
    root_pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    api_pyproject = (REPO_ROOT / "apps/api/pyproject.toml").read_text(encoding="utf-8")

    assert "legacy-graph-contract-test" not in root_moon
    assert "legacy-graph-contract-test" not in core_moon
    assert "legacy-graph-contract-test" not in api_moon
    assert "legacy_graph_contract" not in root_pyproject
    assert "legacy_graph_contract" not in api_pyproject
    assert "legacy_graph_contract" not in core_moon
    assert "legacy_graph_contract" not in api_moon


def test_graphiti_exit_inventory_tracks_no_graphiti_smoke_plan() -> None:
    inventory = GRAPHITI_EXIT_INVENTORY_PATH.read_text(encoding="utf-8")

    assert "## No-Graphiti Smoke Plan" in inventory
    assert "moon run core:no-graphiti-smoke" in inventory
    assert "tests/test_default_memory_loop.py" in inventory
    assert f"blocks `{_GRAPHITI_MODULE}` imports" in inventory
    for loop_name in ("remember", "recall", "context", "wake", "reflect"):
        assert f"- `{loop_name}`:" in inventory
    assert "Current blockers:" not in inventory


def test_runtime_purity_rejects_raw_sql_usage(capsys) -> None:
    record = SqlUsageRecord(
        path="apps/api/src/sibyl/db/queries.py",
        session_imports=(),
        query_imports=("select",),
        session_calls=(),
        query_calls=("select",),
    )
    surface = runtime_surface_with_storage(raw_sql_usage=(record,))

    assert check_runtime_purity(surface) == 1
    captured = capsys.readouterr()
    assert "Runtime contains 1 raw SQL query usage files:" in captured.err
    assert "- apps/api/src/sibyl/db/queries.py" in captured.err


def test_runtime_purity_rejects_session_storage_usage(capsys) -> None:
    record = SqlUsageRecord(
        path="apps/api/src/sibyl/persistence/content_runtime.py",
        session_imports=("AsyncSession",),
        query_imports=(),
        session_calls=("commit",),
        query_calls=(),
    )
    surface = runtime_surface_with_storage(session_storage_usage=(record,))

    assert check_runtime_purity(surface) == 1
    captured = capsys.readouterr()
    assert "Runtime contains 1 session-backed storage access files:" in captured.err
    assert "- apps/api/src/sibyl/persistence/content_runtime.py" in captured.err


def test_runtime_purity_rejects_sqlmodel_tables(capsys) -> None:
    surface = runtime_surface_with_storage(sqlmodel_tables=("User",))

    assert check_runtime_purity(surface) == 1
    captured = capsys.readouterr()
    assert "Runtime declares 1 SQLModel tables:" in captured.err
    assert "- User" in captured.err


def test_runtime_purity_rejects_unpinned_legacy_dependency(capsys) -> None:
    record = DependencyRecord(
        project="apps/api/pyproject.toml",
        dependency="sqlalchemy>=2.0",
        classification="legacy",
        scope="default",
    )
    surface = runtime_surface_with_storage(dependencies=(record,))

    assert check_runtime_purity(surface) == 1
    captured = capsys.readouterr()
    assert "Runtime declares 1 legacy dependencies outside the frozen allowlist:" in captured.err
    assert "- apps/api/pyproject.toml: sqlalchemy>=2.0 (default)" in captured.err


def test_runtime_purity_holds_on_real_surface(capsys) -> None:
    surface = collect_runtime_surface()

    assert check_runtime_purity(surface) == 0
    captured = capsys.readouterr()
    assert "Runtime purity holds" in captured.out


def test_allowlist_matching_rejects_bare_wildcard() -> None:
    record = GraphitiCompatibilityRecord(
        path="*", classification="test", owner="bad", criteria="bad"
    )

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
        "expand_neighbors",
        "fetch_slice",
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
        "sibyl_core.retrieval.search",
        "sibyl_cli.main",
    ):
        assert expected in imports

    for expected in ("apps/cli/src/sibyl_cli/data/hooks/session-start.py",):
        assert expected in entrypoint_script


@requires_helm
def test_helm_production_render_rejects_mcp_auth_mode_off() -> None:
    """auth_mode off serves every MCP tool unauthenticated regardless of the secret."""
    for key, value in (
        ("SIBYL_MCP_AUTH_MODE", "off"),
        ("sibyl_mcp_auth_mode", "OFF"),
    ):
        result = _helm_template(
            "--set",
            "backend.existingSecret=sibyl-secrets",
            "--set",
            f"backend.env.{key}={value}",
        )

        assert result.returncode != 0, f"{key}={value} rendered instead of failing"
        assert "SIBYL_MCP_AUTH_MODE=off is forbidden in production" in result.stderr

    enforcing = _helm_template(
        "--set",
        "backend.existingSecret=sibyl-secrets",
        "--set",
        "backend.env.SIBYL_MCP_AUTH_MODE=on",
    )

    assert enforcing.returncode == 0, enforcing.stderr

    development = _helm_template(
        "--set",
        "backend.env.SIBYL_ENVIRONMENT=development",
        "--set",
        "backend.env.SIBYL_MCP_AUTH_MODE=off",
    )

    assert development.returncode == 0, development.stderr


@requires_helm
def test_helm_guard_remediation_names_the_release_namespace() -> None:
    """A namespace-free kubectl line would create the Secret in `default` instead."""
    assert _HELM_BINARY is not None
    result = subprocess.run(  # noqa: S603
        [_HELM_BINARY, "template", "sibyl", "charts/sibyl", "--namespace", "sibyl-prod"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "--namespace sibyl-prod" in result.stderr
    assert "helm upgrade --install sibyl charts/sibyl --namespace sibyl-prod" in result.stderr
