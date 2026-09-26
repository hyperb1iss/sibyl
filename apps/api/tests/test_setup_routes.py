from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.routing import APIRoute
from pydantic import SecretStr

from sibyl.api.routes import setup as setup_routes
from sibyl.persistence.setup_common import SetupStatus
from sibyl_core.ai.errors import LLMConfigError
from sibyl_core.ai.llm.config import LLMSurface


@pytest.mark.asyncio
async def test_get_setup_status_skips_provider_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = AsyncMock()
    service.get_openai_key.return_value = "sk-openai"
    service.get_anthropic_key.return_value = None
    service.get_gemini_key.return_value = "gemini-key"

    monkeypatch.setattr(
        setup_routes,
        "get_runtime_setup_status",
        AsyncMock(return_value=SetupStatus(has_users=False, has_orgs=False)),
    )
    monkeypatch.setattr(setup_routes, "get_settings_service", lambda: service)
    openai_check = AsyncMock(return_value=(True, None))
    anthropic_check = AsyncMock(return_value=(False, None))
    gemini_check = AsyncMock(return_value=(True, None))
    monkeypatch.setattr(setup_routes, "_check_openai_key", openai_check)
    monkeypatch.setattr(setup_routes, "_check_anthropic_key", anthropic_check)
    monkeypatch.setattr(setup_routes, "_check_gemini_key", gemini_check)

    response = await setup_routes.get_setup_status(validate_keys=True)

    assert response.needs_setup is True
    assert response.has_users is False
    assert response.has_orgs is False
    assert response.setup_complete is False
    assert response.public_signups_enabled is False
    assert response.openai_configured is True
    assert response.anthropic_configured is False
    assert response.gemini_configured is True
    # The public endpoint never validates keys: no provider probe runs,
    # so every *_valid stays None regardless of validate_keys.
    assert response.openai_valid is None
    assert response.anthropic_valid is None
    assert response.gemini_valid is None
    openai_check.assert_not_awaited()
    anthropic_check.assert_not_awaited()
    gemini_check.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_setup_status_uses_runtime_setup_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = AsyncMock()
    service.get_openai_key.return_value = None
    service.get_anthropic_key.return_value = None
    service.get_gemini_key.return_value = None

    surreal_status = AsyncMock(
        return_value=SetupStatus(has_users=True, has_orgs=True, setup_complete=True)
    )

    monkeypatch.setattr(setup_routes, "get_runtime_setup_status", surreal_status)
    monkeypatch.setattr(setup_routes, "get_settings_service", lambda: service)

    response = await setup_routes.get_setup_status(validate_keys=False)

    assert response.needs_setup is False
    assert response.has_users is True
    assert response.has_orgs is True
    assert response.setup_complete is True
    surreal_status.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_get_setup_status_remains_needed_until_owner_admin_org_initialized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = AsyncMock()
    service.get_openai_key.return_value = None
    service.get_anthropic_key.return_value = None
    service.get_gemini_key.return_value = None

    monkeypatch.setattr(
        setup_routes,
        "get_runtime_setup_status",
        AsyncMock(return_value=SetupStatus(has_users=True, has_orgs=True)),
    )
    monkeypatch.setattr(setup_routes, "get_settings_service", lambda: service)

    response = await setup_routes.get_setup_status(validate_keys=False)

    assert response.needs_setup is True
    assert response.setup_complete is False


@pytest.mark.asyncio
async def test_get_setup_status_skips_public_key_validation_after_setup_complete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = AsyncMock()
    service.get_openai_key.return_value = "sk-openai"
    service.get_anthropic_key.return_value = "sk-anthropic"
    service.get_gemini_key.return_value = "gemini-key"
    openai_check = AsyncMock(return_value=(True, None))
    anthropic_check = AsyncMock(return_value=(True, None))
    gemini_check = AsyncMock(return_value=(True, None))

    monkeypatch.setattr(
        setup_routes,
        "get_runtime_setup_status",
        AsyncMock(return_value=SetupStatus(has_users=True, has_orgs=True, setup_complete=True)),
    )
    monkeypatch.setattr(setup_routes, "get_settings_service", lambda: service)
    monkeypatch.setattr(setup_routes, "_check_openai_key", openai_check)
    monkeypatch.setattr(setup_routes, "_check_anthropic_key", anthropic_check)
    monkeypatch.setattr(setup_routes, "_check_gemini_key", gemini_check)

    response = await setup_routes.get_setup_status(validate_keys=True)

    assert response.needs_setup is False
    assert response.setup_complete is True
    assert response.openai_valid is None
    assert response.anthropic_valid is None
    assert response.gemini_valid is None
    openai_check.assert_not_awaited()
    anthropic_check.assert_not_awaited()
    gemini_check.assert_not_awaited()


def test_validate_keys_route_requires_setup_mode_or_admin() -> None:
    routes = [route for route in setup_routes.router.routes if isinstance(route, APIRoute)]
    route = next(route for route in routes if route.path.endswith("/validate-keys"))

    assert route.dependencies[0].dependency is setup_routes.require_setup_mode_or_admin


@pytest.mark.asyncio
async def test_update_config_persists_and_reports_current_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = AsyncMock()
    service.get_openai_key.return_value = "sk-openai"
    service.get_anthropic_key.return_value = "sk-anthropic"
    service.get_gemini_key.return_value = "gemini-key"

    monkeypatch.setattr(setup_routes, "get_settings_service", lambda: service)
    monkeypatch.setattr(setup_routes, "_check_openai_key", AsyncMock(return_value=(True, None)))
    monkeypatch.setattr(
        setup_routes,
        "_check_anthropic_key",
        AsyncMock(return_value=(False, "Invalid API key")),
    )
    monkeypatch.setattr(setup_routes, "_check_gemini_key", AsyncMock(return_value=(True, None)))

    response = await setup_routes.update_config(
        setup_routes.ConfigUpdateRequest(
            openai_api_key="sk-openai",
            anthropic_api_key="sk-anthropic",
            gemini_api_key="gemini-key",
        )
    )

    assert response.success is True
    assert response.openai_valid is True
    assert response.anthropic_valid is False
    assert response.gemini_valid is True
    assert response.anthropic_error == "Invalid API key"
    service.set.assert_any_await(
        "openai_api_key",
        "sk-openai",
        is_secret=True,
        description="OpenAI API key for embeddings and entity extraction",
    )
    service.set.assert_any_await(
        "anthropic_api_key",
        "sk-anthropic",
        is_secret=True,
        description="Anthropic API key for Claude-powered extraction workflows",
    )
    service.set.assert_any_await(
        "gemini_api_key",
        "gemini-key",
        is_secret=True,
        description="Gemini API key for Google embeddings",
    )


@pytest.mark.asyncio
async def test_get_config_status_uses_settings_service(monkeypatch: pytest.MonkeyPatch) -> None:
    service = AsyncMock()
    service.get_with_source = AsyncMock(
        side_effect=[("sk-openai", "database"), (None, "none"), ("gemini-key", "environment")]
    )

    monkeypatch.setattr(setup_routes, "get_settings_service", lambda: service)

    response = await setup_routes.get_config_status()

    assert response.openai_configured is True
    assert response.anthropic_configured is False
    assert response.gemini_configured is True
    assert response.openai_source == "database"
    assert response.anthropic_source == "none"
    assert response.gemini_source == "environment"


def _status_service(**keys: str | None) -> AsyncMock:
    """A settings service whose `get` answers from `keys` (missing means unset)."""
    service = AsyncMock()
    service.get_openai_key.return_value = keys.get("openai_api_key")
    service.get_anthropic_key.return_value = keys.get("anthropic_api_key")
    service.get_gemini_key.return_value = keys.get("gemini_api_key")
    service.get.side_effect = keys.get
    return service


class _PerSurfaceSource:
    """A config source answering each LLM surface from a mapping (default otherwise)."""

    def __init__(self, default: str, **surfaces: str) -> None:
        self.default = default
        self.surfaces = surfaces

    async def resolve(self, surface: LLMSurface) -> SimpleNamespace:
        provider = self.surfaces.get(surface.value, self.default)
        return SimpleNamespace(provider=SimpleNamespace(value=provider))


def _server(
    monkeypatch: pytest.MonkeyPatch,
    *,
    llm_provider: str,
    region: str | None = None,
    local_embeddings_installed: bool = False,
    surfaces: dict[str, str] | None = None,
    **keys: str | None,
) -> None:
    """Point every readiness input at a fake server: config, settings, region, deps."""
    monkeypatch.setattr(
        setup_routes,
        "get_runtime_setup_status",
        AsyncMock(return_value=SetupStatus(has_users=True, has_orgs=True, setup_complete=True)),
    )
    monkeypatch.setattr(setup_routes, "get_settings_service", lambda: _status_service(**keys))
    source = _PerSurfaceSource(llm_provider, **(surfaces or {}))
    monkeypatch.setattr(setup_routes, "get_config_source", lambda: source)
    monkeypatch.setattr(
        setup_routes, "sentence_transformers_available", lambda: local_embeddings_installed
    )
    for name in (
        "SIBYL_EMBEDDING_PROVIDER",
        "SIBYL_GRAPH_EMBEDDING_PROVIDER",
        "SIBYL_BEDROCK_REGION",
        "AWS_REGION",
        "AWS_DEFAULT_REGION",
    ):
        monkeypatch.delenv(name, raising=False)
    if region:
        monkeypatch.setenv("SIBYL_BEDROCK_REGION", region)


async def _status_with(
    monkeypatch: pytest.MonkeyPatch, *, llm_provider: str, **options: object
) -> setup_routes.SetupStatus:
    _server(monkeypatch, llm_provider=llm_provider, **options)  # type: ignore[arg-type]
    return await setup_routes.get_setup_status()


@pytest.mark.asyncio
async def test_status_reports_providers_unconfigured_on_a_fresh_install(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    status = await _status_with(monkeypatch, llm_provider="anthropic")

    assert status.providers_configured is False
    assert status.configured_providers == []


@pytest.mark.asyncio
async def test_status_reports_keyed_providers_configured_when_keys_are_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    status = await _status_with(
        monkeypatch,
        llm_provider="anthropic",
        anthropic_api_key="sk-ant",
        openai_api_key="sk-openai",
    )

    assert status.providers_configured is True
    assert status.configured_providers == ["anthropic", "openai"]


@pytest.mark.asyncio
async def test_status_counts_bedrock_as_ready_with_a_region_and_no_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    status = await _status_with(
        monkeypatch,
        llm_provider="bedrock",
        region="us-east-1",
        embedding_provider="bedrock",
        graph_embedding_provider="bedrock",
    )

    assert status.providers_configured is True
    assert status.configured_providers == ["bedrock"]
    assert (status.bedrock_llm, status.bedrock_embeddings) == (True, True)
    assert status.anthropic_configured is False
    assert status.openai_configured is False


@pytest.mark.asyncio
async def test_status_never_counts_bedrock_without_a_region(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An AWS profile alone proves nothing: Bedrock needs a Region to be ready.
    monkeypatch.setenv("AWS_PROFILE", "dev")
    status = await _status_with(
        monkeypatch,
        llm_provider="bedrock",
        embedding_provider="bedrock",
        graph_embedding_provider="bedrock",
    )

    assert status.providers_configured is False
    assert status.configured_providers == []
    assert (status.bedrock_llm, status.bedrock_embeddings) == (False, False)


@pytest.mark.asyncio
@pytest.mark.parametrize("installed", [True, False])
async def test_status_counts_local_graph_embeddings_only_with_their_dependency(
    monkeypatch: pytest.MonkeyPatch, installed: bool
) -> None:
    status = await _status_with(
        monkeypatch,
        llm_provider="bedrock",
        region="us-east-1",
        local_embeddings_installed=installed,
        embedding_provider="bedrock",
        graph_embedding_provider="local",
    )

    assert status.providers_configured is installed
    assert status.bedrock_embeddings is installed
    assert ("local" in status.configured_providers) is installed


@pytest.mark.asyncio
async def test_status_reports_partial_providers_as_unconfigured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    status = await _status_with(monkeypatch, llm_provider="bedrock", region="us-east-1")

    # Bedrock is ready, but default OpenAI embeddings still need a key.
    assert status.providers_configured is False
    assert status.configured_providers == ["bedrock"]


@pytest.mark.asyncio
@pytest.mark.parametrize("region", [None, "us-east-1"])
@pytest.mark.parametrize("graph", ["bedrock", "local"])
@pytest.mark.parametrize("installed", [True, False])
async def test_an_all_bedrock_server_agrees_with_bedrock_selection(
    monkeypatch: pytest.MonkeyPatch, region: str | None, graph: str, installed: bool
) -> None:
    # One rule: with Bedrock on every plane, readiness is exactly what the
    # keys step's Bedrock check says.
    status = await _status_with(
        monkeypatch,
        llm_provider="bedrock",
        region=region,
        local_embeddings_installed=installed,
        embedding_provider="bedrock",
        graph_embedding_provider=graph,
    )

    assert status.providers_configured is (status.bedrock_llm and status.bedrock_embeddings)


@pytest.mark.asyncio
async def test_status_checks_the_provider_of_every_model_surface(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    status = await _status_with(
        monkeypatch,
        llm_provider="anthropic",
        surfaces={"memory": "gemini"},
        anthropic_api_key="sk-ant",
        openai_api_key="sk-openai",
    )

    # Memory runs on Gemini, which has no key, so the server is not ready.
    assert status.providers_configured is False
    assert status.configured_providers == ["anthropic", "openai"]


@pytest.mark.asyncio
async def test_status_reports_unresolvable_llm_config_as_unconfigured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _server(monkeypatch, llm_provider="anthropic", openai_api_key="sk")

    class Broken:
        async def resolve(self, surface: LLMSurface) -> None:
            raise LLMConfigError("Unsupported LLM provider: nope")

    monkeypatch.setattr(setup_routes, "get_config_source", Broken)

    status = await setup_routes.get_setup_status()

    assert status.providers_configured is False
    assert status.configured_providers == []


SECRET_SENTINEL = "sk-live-secret-value-that-must-never-leak"


def _configure_server(
    monkeypatch: pytest.MonkeyPatch,
    *,
    server_url: str,
    minimum: str | None,
    oidc: bool,
    local_auth: bool,
    frontend_url: str | None = None,
) -> None:
    providers = [SimpleNamespace(name="entra")] if oidc else []
    monkeypatch.setattr(setup_routes.settings, "server_url", server_url)
    monkeypatch.setattr(setup_routes.settings, "frontend_url", frontend_url or server_url)
    monkeypatch.setattr(setup_routes.settings, "minimum_client_version", minimum)
    monkeypatch.setattr(setup_routes.settings, "local_auth_enabled", local_auth)
    monkeypatch.setattr(setup_routes.settings, "oidc", SimpleNamespace(providers=providers))
    # Present on the settings object so a leak would show up in the payload.
    monkeypatch.setenv("SIBYL_OPENAI_API_KEY", SECRET_SENTINEL)
    monkeypatch.setattr(
        setup_routes.settings, "jwt_secret", SecretStr(SECRET_SENTINEL), raising=False
    )


@pytest.mark.asyncio
async def test_connect_info_for_a_default_local_install(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_server(
        monkeypatch,
        server_url="http://localhost:3334/",
        minimum=None,
        oidc=False,
        local_auth=True,
    )

    info = await setup_routes.get_connect_info()

    assert info.server_url == "http://localhost:3334"
    assert info.minimum_client_version is None
    assert info.sso_enabled is False
    assert info.local_auth_enabled is True
    assert info.setup_command == "sibyl setup http://localhost:3334"
    assert info.agent_url == "http://localhost:3334/agent"
    assert info.install["macos"] == (
        "brew install hyperb1iss/tap/sibyl && sibyl setup http://localhost:3334"
    )
    assert info.install["linux"] == (
        "uv tool install --upgrade sibyl-dev && sibyl setup http://localhost:3334"
    )
    assert SECRET_SENTINEL not in info.model_dump_json()


@pytest.mark.asyncio
async def test_connect_info_for_a_team_sso_server(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_server(
        monkeypatch,
        server_url="https://sibyl.example.com",
        minimum=" 1.5.0 ",
        oidc=True,
        local_auth=False,
    )

    info = await setup_routes.get_connect_info()

    assert info.server_url == "https://sibyl.example.com"
    assert info.minimum_client_version == "1.5.0"
    assert info.sso_enabled is True
    assert info.local_auth_enabled is False
    assert all(
        line.endswith("sibyl setup https://sibyl.example.com") for line in info.install.values()
    )
    assert SECRET_SENTINEL not in info.model_dump_json()


@pytest.mark.asyncio
async def test_agent_setup_is_markdown_tailored_to_the_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_server(
        monkeypatch,
        server_url="https://sibyl.example.com/",
        minimum="1.5.0",
        oidc=True,
        local_auth=False,
    )

    response = await setup_routes.get_agent_setup()
    body = bytes(response.body).decode()

    assert response.media_type == "text/markdown; charset=utf-8"
    assert "`sibyl setup https://sibyl.example.com --yes`" in body
    assert "version 1.5.0 or newer" in body
    assert "company SSO" in body
    assert SECRET_SENTINEL not in body
    assert "token" not in body.lower()


@pytest.mark.asyncio
async def test_agent_setup_for_a_local_auth_server(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_server(
        monkeypatch,
        server_url="http://localhost:3334",
        minimum=None,
        oidc=False,
        local_auth=True,
    )

    body = bytes((await setup_routes.get_agent_setup()).body).decode()

    assert "`sibyl setup http://localhost:3334 --yes`" in body
    assert "email and password" in body
    assert "or newer" not in body


@pytest.mark.asyncio
async def test_agent_url_points_at_the_api_when_the_web_app_lives_elsewhere(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Local dev default: API on :3334, web app on :3337, so /agent is not on the API.
    _configure_server(
        monkeypatch,
        server_url="http://localhost:3334",
        minimum=None,
        oidc=False,
        local_auth=True,
        frontend_url="http://localhost:3337/",
    )

    info = await setup_routes.get_connect_info()

    assert info.agent_url == "http://localhost:3334/api/setup/agent.md"


@pytest.mark.asyncio
async def test_agent_url_uses_the_short_page_behind_one_ingress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_server(
        monkeypatch,
        server_url="https://sibyl.example.com",
        minimum=None,
        oidc=True,
        local_auth=False,
        frontend_url="https://sibyl.example.com/",
    )

    info = await setup_routes.get_connect_info()

    assert info.agent_url == "https://sibyl.example.com/agent"


def test_connect_routes_are_public() -> None:
    routes = {
        route.path: route for route in setup_routes.router.routes if isinstance(route, APIRoute)
    }

    assert routes["/setup/connect"].dependencies == []
    assert routes["/setup/agent.md"].dependencies == []
    assert "/setup/integration" not in routes
