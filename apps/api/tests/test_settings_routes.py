"""Tests for settings route auth gating."""

import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from sibyl.api.routes import settings as settings_routes
from sibyl.db.models import OrganizationRole
from sibyl.persistence.legacy import settings as legacy_settings


def _request() -> Request:
    return Request({"type": "http", "method": "GET", "path": "/settings", "headers": []})


@pytest.mark.asyncio
async def test_require_legacy_settings_admin_allows_setup_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    auth_mock = AsyncMock()

    monkeypatch.setattr(legacy_settings, "is_legacy_setup_mode", AsyncMock(return_value=True))
    monkeypatch.setattr(legacy_settings, "build_auth_context", auth_mock)

    await legacy_settings.require_legacy_settings_admin(_request())

    auth_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_require_legacy_settings_admin_rejects_non_admin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = AsyncMock()
    session_manager = AsyncMock()
    session_manager.__aenter__.return_value = session
    session_manager.__aexit__.return_value = False

    monkeypatch.setattr(legacy_settings, "is_legacy_setup_mode", AsyncMock(return_value=False))
    monkeypatch.setattr(
        legacy_settings,
        "build_auth_context",
        AsyncMock(
            return_value=SimpleNamespace(
                organization=object(),
                org_role=OrganizationRole.MEMBER,
            )
        ),
    )
    monkeypatch.setattr(legacy_settings, "get_session", lambda: session_manager)

    with pytest.raises(HTTPException, match="Admin or owner role required") as exc_info:
        await legacy_settings.require_legacy_settings_admin(_request())

    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_get_settings_requires_admin_and_returns_masked_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = AsyncMock()
    service.get_all.return_value = {
        "openai_api_key": {
            "configured": True,
            "source": "database",
            "is_secret": True,
            "masked": "sk-***",
        }
    }

    monkeypatch.setattr(settings_routes, "require_settings_admin", AsyncMock())
    monkeypatch.setattr(settings_routes, "get_settings_service", lambda: service)

    response = await settings_routes.get_settings(_request())

    assert response.settings["openai_api_key"].configured is True
    assert response.settings["openai_api_key"].masked == "sk-***"
    service.get_all.assert_awaited_once_with(include_secrets=False)


@pytest.mark.asyncio
async def test_update_settings_uses_request_body_for_environment_updates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings_routes, "require_settings_admin", AsyncMock())
    monkeypatch.setattr(settings_routes, "_validate_openai_key", AsyncMock(return_value=(True, None)))
    monkeypatch.setattr(
        settings_routes,
        "_validate_anthropic_key",
        AsyncMock(return_value=(True, None)),
    )
    monkeypatch.setattr(settings_routes, "_try_reset_graph_client", AsyncMock())

    service = AsyncMock()
    monkeypatch.setattr(settings_routes, "get_settings_service", lambda: service)

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    body = settings_routes.UpdateSettingsRequest(
        openai_api_key="sk-openai-test",
        anthropic_api_key="sk-ant-test",
    )

    response = await settings_routes.update_settings(_request(), body=body)

    assert os.environ["OPENAI_API_KEY"] == "sk-openai-test"
    assert os.environ["ANTHROPIC_API_KEY"] == "sk-ant-test"
    assert response.updated == ["openai_api_key", "anthropic_api_key"]


@pytest.mark.asyncio
async def test_delete_setting_rejects_setup_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings_routes, "is_setup_mode", AsyncMock(return_value=True))

    with pytest.raises(HTTPException, match="Cannot delete settings during setup mode") as exc_info:
        await settings_routes.delete_setting(_request(), key="openai_api_key")

    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_try_reset_graph_client_uses_runtime_helper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reset_runtime = AsyncMock()
    monkeypatch.setattr(settings_routes, "reset_graph_runtime", reset_runtime)

    await settings_routes._try_reset_graph_client("test context")

    reset_runtime.assert_awaited_once_with()
