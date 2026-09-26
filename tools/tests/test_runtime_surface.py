from __future__ import annotations

import ast
import ipaddress
import json
import re
import shutil
import subprocess

import pytest
import yaml
from tools.inventory import runtime_surface
from tools.inventory.runtime_surface import (
    PYPROJECT_PATHS,
    REPO_ROOT,
    RUNTIME_IMPORT_ROOTS,
    DependencyRecord,
    GraphitiImportRecord,
    RuntimeSurface,
    SqlUsageRecord,
    check_runtime_purity,
    classify_dependency,
    collect_graphiti_imports,
    collect_runtime_surface,
    graphiti_imports_in,
    main,
    parse_dependency_name,
)
from tools.trust.enterprise_readiness_evidence import SIBYL_HELM_RENDER_ARGS

from sibyl.config import parse_forwarded_allow_ips

EXPECTED_ROUTER_COUNT = 31
EXPECTED_HTTP_ROUTE_COUNT = 3
EXPECTED_WEBSOCKET_ROUTE_COUNT = 1
EXPECTED_MCP_TOOL_COUNT = 13
EXPECTED_MCP_RESOURCE_COUNT = 2
API_SERVICE_GID = 10001
ANSIBLE_NETWORK_PREFIXLEN = 24
GRAPHITI_PACKAGE = "graphiti" + "-core"
GRAPHITI_MODULE = "graphiti" + "_core"


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


def _render_role_values(templates: dict[str, str], variables: dict[str, object]) -> dict[str, str]:
    """Resolve the plain `{{ name }}` references these role files use, recursively."""

    def render(template: str) -> str:
        return re.sub(
            r"\{\{\s*(\w+)\s*\}\}", lambda match: render(str(variables[match.group(1)])), template
        )

    return {key: render(template) for key, template in templates.items()}


def _compose_fallback(value: str, variable: str) -> str:
    """The value a compose `${VAR:-fallback}` or `${VAR-fallback}` substitution falls back to."""
    match = re.fullmatch(rf"\$\{{{variable}:?-(.*)\}}", value)
    assert match, value
    return match.group(1)


def test_ansible_stack_trusts_only_caddys_pinned_address() -> None:
    defaults = yaml.safe_load(
        (REPO_ROOT / "infra/ansible/roles/sibyl/defaults/main.yml").read_text(encoding="utf-8")
    )
    env_lines = (
        (REPO_ROOT / "infra/ansible/roles/sibyl/templates/env.j2")
        .read_text(encoding="utf-8")
        .splitlines()
    )
    compose = yaml.safe_load(
        (REPO_ROOT / "infra/ansible/roles/sibyl/files/docker-compose.yml").read_text(
            encoding="utf-8"
        )
    )
    helm_values = yaml.safe_load((REPO_ROOT / "charts/sibyl/values.yaml").read_text("utf-8"))

    plan_keys = (
        "SIBYL_FORWARDED_ALLOW_IPS",
        "SIBYL_NETWORK_SUBNET",
        "SIBYL_NETWORK_GATEWAY",
        "SIBYL_NETWORK_DYNAMIC_RANGE",
        "SIBYL_CADDY_IPV4",
    )
    env_templates = dict(line.split("=", 1) for line in env_lines if "=" in line)
    rendered = _render_role_values({key: env_templates[key] for key in plan_keys}, defaults)

    # The compose fallbacks match what the role renders, so the stack keeps the
    # same address plan when a variable is missing from the env file. The
    # trust list uses "-" rather than ":-", so an explicitly empty value
    # really means loopback only.
    backend_env = compose["services"]["backend"]["environment"]
    assert backend_env["SIBYL_FORWARDED_ALLOW_IPS"] == (
        f"${{SIBYL_FORWARDED_ALLOW_IPS-{rendered['SIBYL_FORWARDED_ALLOW_IPS']}}}"
    )
    caddy_network = compose["services"]["caddy"]["networks"]["default"]
    caddy_fallback = _compose_fallback(caddy_network["ipv4_address"], "SIBYL_CADDY_IPV4")
    assert caddy_fallback == rendered["SIBYL_CADDY_IPV4"]
    [ipam] = compose["networks"]["default"]["ipam"]["config"]
    for field, key in (
        ("subnet", "SIBYL_NETWORK_SUBNET"),
        ("gateway", "SIBYL_NETWORK_GATEWAY"),
        ("ip_range", "SIBYL_NETWORK_DYNAMIC_RANGE"),
    ):
        assert _compose_fallback(ipam[field], key) == rendered[key]
    assert helm_values["backend"]["forwardedAllowIps"] == ""

    subnet = ipaddress.IPv4Network(rendered["SIBYL_NETWORK_SUBNET"])
    gateway = ipaddress.IPv4Address(rendered["SIBYL_NETWORK_GATEWAY"])
    dynamic_range = ipaddress.IPv4Network(rendered["SIBYL_NETWORK_DYNAMIC_RANGE"])
    caddy = ipaddress.IPv4Address(rendered["SIBYL_CADDY_IPV4"])
    trusted = [
        ipaddress.ip_network(entry)
        for entry in parse_forwarded_allow_ips(rendered["SIBYL_FORWARDED_ALLOW_IPS"])
    ]

    # Exactly Caddy's /32, inside the pinned subnet and outside the range
    # Docker hands the other containers (the frontend included), so nothing
    # else on the network can hold the trusted address.
    assert subnet.prefixlen == ANSIBLE_NETWORK_PREFIXLEN
    assert subnet.is_private
    assert trusted == [ipaddress.IPv4Network(f"{caddy}/32")]
    assert caddy in subnet
    assert dynamic_range.subnet_of(subnet)
    assert caddy not in dynamic_range

    # Host processes reach the backend from the bridge gateway, so it must stay untrusted.
    assert gateway == subnet.network_address + 1
    assert gateway not in dynamic_range
    assert not any(gateway in network for network in trusted)

    # Clear of Docker's default address pools, so it never collides with a
    # network Docker assigned on its own, and of the tailnet's CGNAT range.
    for reserved in ("172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10"):
        assert not subnet.overlaps(ipaddress.IPv4Network(reserved)), reserved


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
        [
            _HELM_BINARY,
            "template",
            "sibyl",
            "charts/sibyl",
            "--set",
            "backend.validationReceipts.existingClaim=validation-receipts",
            *overrides,
        ],
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
def test_helm_bedrock_render_needs_irsa_and_a_region_not_provider_keys() -> None:
    role = "arn:aws:iam::123456789012:role/sibyl-bedrock"
    result = _helm_template(
        "--set",
        "backend.existingSecret=sibyl-secrets",
        "--set",
        "backend.env.SIBYL_LLM_PROVIDER=bedrock",
        "--set",
        "backend.env.SIBYL_EMBEDDING_PROVIDER=bedrock",
        "--set",
        "backend.env.SIBYL_GRAPH_EMBEDDING_PROVIDER=bedrock",
        "--set",
        "backend.env.AWS_REGION=us-west-2",
        "--set-string",
        f"serviceAccount.annotations.eks\\.amazonaws\\.com/role-arn={role}",
    )

    assert result.returncode == 0, result.stderr
    documents = [doc for doc in yaml.safe_load_all(result.stdout) if doc]
    account = next(doc for doc in documents if doc["kind"] == "ServiceAccount")
    assert account["metadata"]["annotations"] == {"eks.amazonaws.com/role-arn": role}
    config = next(doc for doc in documents if doc["kind"] == "ConfigMap")
    assert config["data"]["SIBYL_LLM_PROVIDER"] == "bedrock"
    assert config["data"]["AWS_REGION"] == "us-west-2"
    deployments = [
        doc
        for doc in documents
        if doc["kind"] == "Deployment" and doc["metadata"]["name"].endswith(("backend", "worker"))
    ]
    assert deployments
    for deployment in deployments:
        pod = deployment["spec"]["template"]["spec"]
        assert pod["serviceAccountName"] == account["metadata"]["name"]
        provider_keys = [
            env
            for container in pod["containers"]
            for env in container.get("env", [])
            if env["name"] in {"SIBYL_OPENAI_API_KEY", "SIBYL_ANTHROPIC_API_KEY"}
        ]
        # Bedrock signs with the IRSA role, so provider API keys stay optional.
        assert provider_keys
        assert all(env["valueFrom"]["secretKeyRef"]["optional"] is True for env in provider_keys)


@requires_helm
def test_helm_public_url_reaches_backend_and_frontend_consumers() -> None:
    result = _helm_template(
        "--set",
        "backend.existingSecret=sibyl-secrets",
        "--set",
        "publicUrl=https://sibyl.example.test/",
    )

    assert result.returncode == 0, result.stderr
    assert 'SIBYL_PUBLIC_URL: "https://sibyl.example.test"' in result.stdout
    assert "name: NEXT_PUBLIC_API_URL" in result.stdout
    assert 'value: "https://sibyl.example.test/api"' in result.stdout


@requires_helm
def test_helm_explicit_frontend_public_api_url_wins_over_public_url() -> None:
    result = _helm_template(
        "--set",
        "backend.existingSecret=sibyl-secrets",
        "--set",
        "publicUrl=https://sibyl.example.test",
        "--set",
        "frontend.publicApiUrl=https://api.sibyl.example.test/v1",
    )

    assert result.returncode == 0, result.stderr
    assert "name: NEXT_PUBLIC_API_URL" in result.stdout
    assert 'value: "https://api.sibyl.example.test/v1"' in result.stdout
    assert 'value: "https://sibyl.example.test/api"' not in result.stdout


def _rendered_config_data(manifests: str) -> dict[str, str]:
    for document in yaml.safe_load_all(manifests):
        if (
            isinstance(document, dict)
            and document.get("kind") == "ConfigMap"
            and document["metadata"]["name"] == "sibyl-config"
        ):
            return document["data"]
    raise AssertionError("sibyl-config ConfigMap was not rendered")


@requires_helm
def test_helm_forwarded_allow_ips_is_unset_by_default() -> None:
    result = _helm_template("--set", "backend.existingSecret=sibyl-secrets")

    assert result.returncode == 0, result.stderr
    assert "SIBYL_FORWARDED_ALLOW_IPS" not in _rendered_config_data(result.stdout)


@pytest.mark.parametrize(
    ("override", "expected"),
    [
        ("backend.forwardedAllowIps=10.244.0.0/16", "10.244.0.0/16"),
        ("backend.forwardedAllowIps={10.244.0.0/16,172.31.0.0/16}", "10.244.0.0/16,172.31.0.0/16"),
        ("backend.env.SIBYL_FORWARDED_ALLOW_IPS=10.9.0.0/16", "10.9.0.0/16"),
        ("backend.forwardedAllowIps=*", "*"),
    ],
)
@requires_helm
def test_helm_forwarded_allow_ips_reaches_the_backend_env(override: str, expected: str) -> None:
    result = _helm_template("--set", "backend.existingSecret=sibyl-secrets", "--set", override)

    assert result.returncode == 0, result.stderr
    assert _rendered_config_data(result.stdout)["SIBYL_FORWARDED_ALLOW_IPS"] == expected


@requires_helm
def test_helm_forwarded_allow_ips_value_wins_over_backend_env() -> None:
    result = _helm_template(
        "--set",
        "backend.existingSecret=sibyl-secrets",
        "--set",
        "backend.env.SIBYL_FORWARDED_ALLOW_IPS=10.9.0.0/16",
        "--set",
        "backend.forwardedAllowIps=10.244.0.0/16",
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.count("SIBYL_FORWARDED_ALLOW_IPS:") == 1
    assert _rendered_config_data(result.stdout)["SIBYL_FORWARDED_ALLOW_IPS"] == "10.244.0.0/16"


@requires_helm
def test_helm_non_production_render_keeps_development_jwt_autogeneration() -> None:
    result = _helm_template("--set", "backend.env.SIBYL_ENVIRONMENT=development")

    assert result.returncode == 0, result.stderr
    assert 'SIBYL_ENVIRONMENT: "development"' in result.stdout
    assert "key: SIBYL_JWT_SECRET" not in result.stdout
    assert "name: sibyl-worker" not in result.stdout


@requires_helm
def test_helm_production_redis_render_includes_the_worker() -> None:
    result = _helm_template(
        "--set",
        "backend.existingSecret=sibyl-secrets",
        "--set",
        "coordinationBackend=redis",
        "--set",
        "backend.redis.password=ci-only",
    )

    assert result.returncode == 0, result.stderr
    assert "name: sibyl-worker" in result.stdout
    assert 'SIBYL_COORDINATION_BACKEND: "redis"' in result.stdout


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


def runtime_surface_with_findings(
    *,
    raw_sql_usage: tuple[SqlUsageRecord, ...] = (),
    session_storage_usage: tuple[SqlUsageRecord, ...] = (),
    graphiti_imports: tuple[GraphitiImportRecord, ...] = (),
    dependencies: tuple[DependencyRecord, ...] = (),
) -> RuntimeSurface:
    return RuntimeSurface(
        rest_routers=(),
        top_level_http_routes=(),
        websocket_routes=(),
        mcp_tools=(),
        mcp_resources=(),
        raw_sql_usage=raw_sql_usage,
        session_storage_usage=session_storage_usage,
        graphiti_imports=graphiti_imports,
        dependencies=dependencies,
    )


def test_dependency_parser_strips_extras_and_markers() -> None:
    requirement = f'{GRAPHITI_PACKAGE}[falkordb,anthropic]>=0.28.2 ; python_version >= "3.13"'
    assert parse_dependency_name(requirement) == GRAPHITI_PACKAGE


@pytest.mark.parametrize(
    "requirement",
    [
        f"{GRAPHITI_PACKAGE}>=0.28.2",
        f'{GRAPHITI_PACKAGE}[anthropic]>=0.28 ; python_version >= "3.13"',
        f"{GRAPHITI_PACKAGE} @ git+https://example.test/graphiti.git@v0.28.2",
        f"{GRAPHITI_PACKAGE} (>=0.28)",
        GRAPHITI_MODULE,
        "Graphiti.Core==0.28",
        "graphiti",
        "falkordb>=1.0",
    ],
)
def test_graphiti_and_falkordb_dependencies_classify_as_legacy(requirement: str) -> None:
    assert classify_dependency(requirement) == "legacy"


def test_dependency_classifier_keeps_target_and_ignores_unrelated_packages() -> None:
    assert classify_dependency("surrealdb>=2.0.0,<3.0") == "target"
    assert classify_dependency("httpx>=0.28") is None
    assert classify_dependency("graphviz>=0.20") is None


def test_graphiti_import_scan_sees_static_and_dynamic_imports() -> None:
    tree = ast.parse(
        f"""
import graphiti
import {GRAPHITI_MODULE}.nodes as nodes
from {GRAPHITI_MODULE}.edges import EntityEdge
from importlib import import_module

import_module("{GRAPHITI_MODULE}.search")
__import__("graphiti.llm")
import_module("sibyl_core.services.graph")
from .graphiti_shim import local_helper
import graphitize
"""
    )

    assert graphiti_imports_in(tree) == (
        "graphiti",
        "graphiti.llm",
        f"{GRAPHITI_MODULE}.edges",
        f"{GRAPHITI_MODULE}.nodes",
        f"{GRAPHITI_MODULE}.search",
    )


def test_graphiti_import_scan_covers_every_shipped_python_root() -> None:
    assert {root.relative_to(REPO_ROOT).as_posix() for root in RUNTIME_IMPORT_ROOTS} == {
        "apps/api/src",
        "apps/cli/src",
        "hooks",
        "packages/python/sibyl-core/src",
    }


def test_graphiti_import_scan_reads_nested_imports_and_skips_virtualenvs(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(runtime_surface, "REPO_ROOT", tmp_path)
    hooks = tmp_path / "hooks"
    (hooks / ".venv/lib").mkdir(parents=True)
    (hooks / ".venv/lib/vendored.py").write_text(f"import {GRAPHITI_MODULE}\n", encoding="utf-8")
    (hooks / "session-start.py").write_text(
        "from typing import TYPE_CHECKING\n\n"
        "if TYPE_CHECKING:\n"
        f"    from {GRAPHITI_MODULE}.nodes import EntityNode\n\n"
        "def load():\n"
        "    import graphiti\n",
        encoding="utf-8",
    )

    assert collect_graphiti_imports(roots=(hooks,)) == (
        GraphitiImportRecord(
            path="hooks/session-start.py",
            imports=("graphiti", f"{GRAPHITI_MODULE}.nodes"),
        ),
    )


def test_runtime_purity_rejects_graphiti_imports(capsys) -> None:
    record = GraphitiImportRecord(
        path="apps/api/src/sibyl/api/routes/memory.py",
        imports=(f"{GRAPHITI_MODULE}.nodes",),
    )
    surface = runtime_surface_with_findings(graphiti_imports=(record,))

    assert check_runtime_purity(surface) == 1
    captured = capsys.readouterr()
    assert "Runtime imports Graphiti in 1 files:" in captured.err
    assert f"- apps/api/src/sibyl/api/routes/memory.py: {GRAPHITI_MODULE}.nodes" in captured.err


def test_runtime_purity_rejects_graphiti_dependency(capsys) -> None:
    requirement = f"{GRAPHITI_PACKAGE}>=0.28.2"
    record = DependencyRecord(
        project="packages/python/sibyl-core/pyproject.toml",
        dependency=requirement,
        classification=classify_dependency(requirement) or "",
        scope="default",
    )
    surface = runtime_surface_with_findings(dependencies=(record,))

    assert check_runtime_purity(surface) == 1
    captured = capsys.readouterr()
    assert "Runtime declares 1 legacy dependencies outside the frozen allowlist:" in captured.err
    assert f"- packages/python/sibyl-core/pyproject.toml: {requirement} (default)" in captured.err


def test_runtime_purity_cli_rejects_unknown_flags() -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["--output", "report.md"])

    assert exit_info.value.code != 0


def test_runtime_purity_rejects_raw_sql_usage(capsys) -> None:
    record = SqlUsageRecord(
        path="apps/api/src/sibyl/db/queries.py",
        session_imports=(),
        query_imports=("select",),
        session_calls=(),
        query_calls=("select",),
    )
    surface = runtime_surface_with_findings(raw_sql_usage=(record,))

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
    surface = runtime_surface_with_findings(session_storage_usage=(record,))

    assert check_runtime_purity(surface) == 1
    captured = capsys.readouterr()
    assert "Runtime contains 1 session-backed storage access files:" in captured.err
    assert "- apps/api/src/sibyl/persistence/content_runtime.py" in captured.err


def test_runtime_purity_rejects_unpinned_legacy_dependency(capsys) -> None:
    record = DependencyRecord(
        project="apps/api/pyproject.toml",
        dependency="sqlalchemy>=2.0",
        classification="legacy",
        scope="default",
    )
    surface = runtime_surface_with_findings(dependencies=(record,))

    assert check_runtime_purity(surface) == 1
    captured = capsys.readouterr()
    assert "Runtime declares 1 legacy dependencies outside the frozen allowlist:" in captured.err
    assert "- apps/api/pyproject.toml: sqlalchemy>=2.0 (default)" in captured.err


def test_runtime_purity_holds_on_real_surface(capsys) -> None:
    surface = collect_runtime_surface()

    assert check_runtime_purity(surface) == 0
    captured = capsys.readouterr()
    assert "Runtime purity holds" in captured.out


def test_runtime_surface_finds_known_contracts() -> None:
    surface = collect_runtime_surface()

    assert len(surface.rest_routers) == EXPECTED_ROUTER_COUNT
    assert len(surface.top_level_http_routes) == EXPECTED_HTTP_ROUTE_COUNT
    assert len(surface.websocket_routes) == EXPECTED_WEBSOCKET_ROUTE_COUNT
    assert len(surface.mcp_tools) == EXPECTED_MCP_TOOL_COUNT
    assert len(surface.mcp_resources) == EXPECTED_MCP_RESOURCE_COUNT

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
    assert "helm install sibyl charts/sibyl --namespace sibyl-prod" in result.stderr


@requires_helm
def test_helm_guard_remediation_separates_install_from_upgrade() -> None:
    """`helm upgrade --set` without --reuse-values resets a release to chart defaults."""
    result = _helm_template()

    assert result.returncode != 0
    assert "for a first install:" in result.stderr
    assert "--reuse-values --set backend.existingSecret=" in result.stderr


@requires_helm
def test_helm_guard_remediation_refuses_to_interpolate_shell_metacharacters() -> None:
    """The remediation is a snippet operators paste, so an unvalidated name executes."""
    for hostile in ("$(id)", "`id`", "a;rm -rf /", "UPPERCASE"):
        result = _helm_template(
            "--set-string",
            f"backend.existingSecret={hostile}",
            "--set",
            "backend.env.SIBYL_JWT_SECRET=x",
        )

        assert result.returncode != 0
        assert "<your-secret-name>" in result.stderr
        assert hostile not in result.stderr

    valid = _helm_template(
        "--set-string",
        "backend.existingSecret=ok-name-123",
        "--set",
        "backend.env.SIBYL_JWT_SECRET=x",
    )

    assert valid.returncode != 0
    assert "kubectl create secret generic ok-name-123" in valid.stderr


def _surrealdb_chart_containers() -> list[dict]:
    """Render the surrealdb wrapper with every ops surface enabled and
    return each (init)container spec from the manifests."""
    assert _HELM_BINARY is not None
    result = subprocess.run(  # noqa: S603
        [
            _HELM_BINARY,
            "template",
            "drill",
            "charts/surrealdb",
            "--set",
            "export.enabled=true",
            "--set",
            "restoreDrill.enabled=true",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    containers: list[dict] = []
    for document in yaml.safe_load_all(result.stdout):
        if not document:
            continue
        pod_spec = (
            document.get("spec", {}).get("template", {}).get("spec")
            or document.get("spec", {})
            .get("jobTemplate", {})
            .get("spec", {})
            .get("template", {})
            .get("spec")
            or (document.get("spec") if document.get("kind") == "Pod" else None)
        )
        if not pod_spec:
            continue
        containers.extend(pod_spec.get("initContainers", []))
        containers.extend(pod_spec.get("containers", []))
    assert containers
    return containers


@requires_helm
def test_helm_surreal_image_never_hosts_a_shell() -> None:
    """The official surrealdb image is distroless: any container that
    invokes /bin/sh on it fails at OCI create in a real cluster while
    rendering green, which is exactly how the strict-bootstrap hook
    shipped broken. Shell workloads must run on the ops image."""
    for container in _surrealdb_chart_containers():
        command = container.get("command") or []
        args = container.get("args") or []
        uses_shell = any("/bin/sh" in str(part) for part in [*command, *args])
        if uses_shell:
            assert "surrealdb/surrealdb" not in container["image"], (
                f"container {container['name']} runs a shell on the "
                f"distroless surreal image {container['image']}"
            )


@requires_helm
def test_helm_restore_drill_splits_server_and_drill_logic() -> None:
    """The drill's scratch server is the one piece that needs the surreal
    binary: it must run as a native sidecar with the plain entrypoint,
    while the drill logic talks HTTP from the ops image."""
    containers = {c["name"]: c for c in _surrealdb_chart_containers()}

    server = containers["restore-server"]
    assert "surrealdb/surrealdb" in server["image"]
    # No command override: the distroless image resolves the surreal
    # binary only through its entrypoint, never via $PATH.
    assert "command" not in server
    assert (server.get("args") or [])[0] == "start"
    assert server.get("restartPolicy") == "Always"

    drill = containers["restore-drill"]
    assert "surrealdb/surrealdb" not in drill["image"]
    drill_script = "".join(drill.get("args") or [])
    assert "/import" in drill_script
    assert "/health" in drill_script


def _surrealdb_template(*overrides: str) -> subprocess.CompletedProcess[str]:
    assert _HELM_BINARY is not None
    return subprocess.run(  # noqa: S603
        [_HELM_BINARY, "template", "drill", "charts/surrealdb", *overrides],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


@requires_helm
def test_helm_ops_endpoint_normalizes_ws_and_rejects_non_http() -> None:
    """The CLI accepted ws(s) endpoints, but the ops jobs speak the HTTP
    API. ws maps to http (same port serves both), and anything else
    non-HTTP fails at render instead of inside a PostSync hook."""
    normalized = _surrealdb_template("--set", "connection.scheme=ws")
    assert normalized.returncode == 0, normalized.stderr
    assert 'value: "http://drill-surrealdb:8000"' in normalized.stdout

    for spelling in (
        "wss://db.example.test:8000/rpc",
        "wss://db.example.test:8000/rpc/",
        "WSS://db.example.test:8000/rpc",
        "https://db.example.test:8000/",
        "https://db.example.test:8000//",
    ):
        explicit = _surrealdb_template("--set", f"connection.endpoint={spelling}")
        assert explicit.returncode == 0, f"{spelling}: {explicit.stderr}"
        assert 'value: "https://db.example.test:8000"' in explicit.stdout, spelling

    rejected = _surrealdb_template("--set", "connection.endpoint=tikv://db:2379")
    assert rejected.returncode != 0
    assert "must be http(s)" in rejected.stderr


@requires_helm
def test_helm_restore_drill_pod_never_restarts_in_place() -> None:
    """A container-level restart would reuse the sidecar's dirty scratch
    state, so every drill retry must be a fresh Pod."""
    result = _surrealdb_template("--set", "restoreDrill.enabled=true")
    assert result.returncode == 0, result.stderr
    for document in yaml.safe_load_all(result.stdout):
        if not document or document.get("kind") != "CronJob":
            continue
        if "restore-drill" not in document["metadata"]["name"]:
            continue
        pod_spec = document["spec"]["jobTemplate"]["spec"]["template"]["spec"]
        assert pod_spec["restartPolicy"] == "Never"
        break
    else:
        pytest.fail("restore-drill CronJob not rendered")


@requires_helm
def test_helm_validation_receipts_require_and_share_persistent_claim() -> None:
    missing = _helm_template(
        "--set",
        "backend.existingSecret=runtime-secret",
        "--set",
        "backend.validationReceipts.existingClaim=",
    )
    assert missing.returncode != 0
    assert "validationReceipts.existingClaim is required" in missing.stderr
    rendered = _helm_template(
        "--set",
        "backend.existingSecret=runtime-secret",
        "--set",
        "coordinationBackend=redis",
        "--set",
        "backend.redis.password=fixture-only",
        "--set",
        "worker.enabled=true",
    )
    assert rendered.returncode == 0, rendered.stderr
    deployments = [
        d for d in yaml.safe_load_all(rendered.stdout) if d and d.get("kind") == "Deployment"
    ]
    checked = set()
    for deployment in deployments:
        spec = deployment["spec"]["template"]["spec"]
        for container in spec["containers"]:
            if container["name"] not in {"backend", "worker"}:
                continue
            mount = next(m for m in container["volumeMounts"] if m["name"] == "validation-receipts")
            assert mount["mountPath"] == "/var/lib/sibyl-receipts"
            volume = next(v for v in spec["volumes"] if v["name"] == "validation-receipts")
            assert volume["persistentVolumeClaim"]["claimName"] == "validation-receipts"
            assert "emptyDir" not in volume
            checked.add(container["name"])
    assert checked == {"backend", "worker"}


def _receipt_deployments(*overrides: str) -> dict[str, dict]:
    rendered = _helm_template(
        "--set",
        "backend.existingSecret=runtime-secret",
        "--set",
        "coordinationBackend=redis",
        "--set",
        "backend.redis.password=fixture-only",
        "--set",
        "worker.enabled=true",
        *overrides,
    )
    assert rendered.returncode == 0, rendered.stderr
    return {
        d["metadata"]["name"]: d
        for d in yaml.safe_load_all(rendered.stdout)
        if d and d.get("kind") == "Deployment"
    }


@requires_helm
def test_helm_receipt_pods_keep_claim_private_and_attachable() -> None:
    """Block-storage CSI drivers re-apply fsGroup on every mount, adding group
    bits the private receipts directory refuses, and an RWO claim cannot
    attach to a rolling-update surge pod scheduled onto another node. The
    default keeps type RollingUpdate: switching an existing Deployment to
    Recreate is rejected under server-side apply, because the API-defaulted
    rollingUpdate field has no owner and can never be removed."""
    deployments = _receipt_deployments()
    for name in ("sibyl-backend", "sibyl-worker"):
        deployment = deployments[name]
        security = deployment["spec"]["template"]["spec"]["securityContext"]
        assert security["fsGroup"] == API_SERVICE_GID
        assert security["fsGroupChangePolicy"] == "OnRootMismatch"
        assert deployment["spec"]["strategy"] == {
            "type": "RollingUpdate",
            "rollingUpdate": {"maxSurge": 0, "maxUnavailable": 1},
        }

    scaled = _receipt_deployments(
        "--set", "backend.replicaCount=2", "--set", "worker.autoscaling.enabled=true"
    )
    assert "strategy" not in scaled["sibyl-backend"]["spec"]
    assert "strategy" not in scaled["sibyl-worker"]["spec"]

    explicit = _receipt_deployments("--set", "backend.strategy.type=Recreate")
    assert explicit["sibyl-backend"]["spec"]["strategy"] == {"type": "Recreate"}

    invalid = _helm_template(
        "--set",
        "backend.existingSecret=runtime-secret",
        "--set",
        "backend.strategy.type=Recreate",
        "--set",
        "backend.strategy.rollingUpdate.maxSurge=1",
    )
    assert invalid.returncode != 0
    assert "may not be set when strategy.type is Recreate" in invalid.stderr


@requires_helm
def test_helm_receipt_strategy_leaves_no_whitespace_lines() -> None:
    rendered = _helm_template(
        "--set",
        "backend.existingSecret=runtime-secret",
        "--set",
        "coordinationBackend=redis",
        "--set",
        "backend.redis.password=fixture-only",
        "--set",
        "worker.enabled=true",
    )
    assert rendered.returncode == 0, rendered.stderr
    blank_with_spaces = [
        number
        for number, line in enumerate(rendered.stdout.splitlines(), start=1)
        if line and not line.strip()
    ]
    assert blank_with_spaces == []


def test_production_compose_validation_receipts_share_durable_state() -> None:
    compose = yaml.safe_load((REPO_ROOT / "docker-compose.prod.yml").read_text())
    for owner in ("backend", "worker"):
        service = compose["services"][owner]
        assert "validation_receipts:/home/sibyl/.sibyl" in service["volumes"]
        assert (
            service["depends_on"]["receipts-init"]["condition"] == "service_completed_successfully"
        )
    assert "validation_receipts" in compose["volumes"]
    assert (
        "validation_receipts:/home/sibyl/.sibyl" in compose["services"]["receipts-init"]["volumes"]
    )
    assert compose["services"]["receipts-init"]["command"] == [
        "chown",
        "10001:10001",
        "/home/sibyl/.sibyl",
    ]


@requires_helm
@pytest.mark.parametrize("profile", ["defaults", "production-redis"])
def test_helm_ci_profile_renders_exact_workflow_arguments(profile: str) -> None:
    workflow = yaml.safe_load((REPO_ROOT / ".github/workflows/ci.yml").read_text())
    profiles = workflow["jobs"]["helm"]["strategy"]["matrix"]["include"]
    selected = next(item for item in profiles if item["profile"] == profile)
    assert _HELM_BINARY is not None
    result = subprocess.run(  # noqa: S603
        [_HELM_BINARY, "template", "sibyl", "charts/sibyl", *json.loads(selected["values"])],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    deployments = [
        item
        for item in yaml.safe_load_all(result.stdout)
        if item and item.get("kind") == "Deployment"
    ]
    backend_pods = [
        item["spec"]["template"]["spec"]
        for item in deployments
        if item["metadata"]["name"] in {"sibyl-backend", "sibyl-worker"}
    ]
    assert backend_pods
    for pod in backend_pods:
        volume = next(item for item in pod["volumes"] if item["name"] == "validation-receipts")
        assert volume["persistentVolumeClaim"]["claimName"] == "ci-validation-receipts"
    assert ("name: sibyl-worker" in result.stdout) == (selected["worker"] == "present")
