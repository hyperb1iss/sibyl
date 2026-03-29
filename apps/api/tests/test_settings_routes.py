"""Tests for settings route auth gating."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from sibyl.api.routes import settings as settings_routes
from sibyl.db.models import OrganizationRole


def _request() -> Request:
    return Request({"type": "http", "method": "GET", "path": "/settings", "headers": []})


@pytest.mark.asyncio
async def test_require_settings_admin_allows_setup_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    session = AsyncMock()
    auth_mock = AsyncMock()

    monkeypatch.setattr(settings_routes, "_is_setup_mode", AsyncMock(return_value=True))
    monkeypatch.setattr(settings_routes, "build_auth_context", auth_mock)

    await settings_routes._require_settings_admin(_request(), session)

    auth_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_require_settings_admin_rejects_non_admin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = AsyncMock()

    monkeypatch.setattr(settings_routes, "_is_setup_mode", AsyncMock(return_value=False))
    monkeypatch.setattr(
        settings_routes,
        "build_auth_context",
        AsyncMock(
            return_value=SimpleNamespace(
                organization=object(),
                org_role=OrganizationRole.MEMBER,
            )
        ),
    )

    with pytest.raises(HTTPException, match="Admin or owner role required") as exc_info:
        await settings_routes._require_settings_admin(_request(), session)

    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_get_settings_requires_admin_and_returns_masked_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = AsyncMock()
    service = AsyncMock()
    service.get_all.return_value = {
        "openai_api_key": {
            "configured": True,
            "source": "database",
            "is_secret": True,
            "masked": "sk-***",
        }
    }

    monkeypatch.setattr(settings_routes, "_is_setup_mode", AsyncMock(return_value=False))
    monkeypatch.setattr(
        settings_routes,
        "build_auth_context",
        AsyncMock(
            return_value=SimpleNamespace(
                organization=object(),
                org_role=OrganizationRole.ADMIN,
            )
        ),
    )
    monkeypatch.setattr(settings_routes, "get_settings_service", lambda: service)

    response = await settings_routes.get_settings(_request(), session=session)

    assert response.settings["openai_api_key"].configured is True
    assert response.settings["openai_api_key"].masked == "sk-***"
    service.get_all.assert_awaited_once_with(include_secrets=False)
