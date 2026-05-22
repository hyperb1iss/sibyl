from unittest.mock import AsyncMock

import pytest
from fastapi.routing import APIRoute

from sibyl.api.routes import setup as setup_routes
from sibyl.persistence.setup_common import SetupStatus
from sibyl.persistence.surreal import setup as surreal_setup


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
async def test_get_setup_status_uses_surreal_setup_runtime_in_surreal_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = AsyncMock()
    service.get_openai_key.return_value = None
    service.get_anthropic_key.return_value = None
    service.get_gemini_key.return_value = None

    surreal_status = AsyncMock(
        return_value=SetupStatus(has_users=True, has_orgs=True, setup_complete=True)
    )

    monkeypatch.setattr(surreal_setup, "get_setup_status", surreal_status)
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


@pytest.mark.asyncio
async def test_get_integration_returns_client_agnostic_payload() -> None:
    response = await setup_routes.get_integration()

    assert response.server_url
    assert response.mcp_url.endswith("/mcp")
    assert response.cli_install.startswith("curl -fsSL")
    assert [client.id for client in response.mcp_clients] == [
        "claude",
        "codex",
        "opencode",
        "generic",
    ]
    for client in response.mcp_clients:
        assert response.mcp_url in client.snippet
    assert "memory loop" in response.prompt_snippet


def test_integration_route_requires_setup_mode_or_auth() -> None:
    routes = [route for route in setup_routes.router.routes if isinstance(route, APIRoute)]
    route = next(route for route in routes if route.path.endswith("/integration"))

    assert route.dependencies[0].dependency is setup_routes.require_setup_mode_or_auth
