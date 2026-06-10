"""Tests for settings route auth gating."""

import os
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from sibyl.api.routes import settings as settings_routes


def _request() -> Request:
    return Request({"type": "http", "method": "GET", "path": "/settings", "headers": []})


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

    monkeypatch.setattr(settings_routes, "require_settings_owner", AsyncMock())
    monkeypatch.setattr(settings_routes, "get_settings_service", lambda: service)

    response = await settings_routes.get_settings(_request())

    assert response.settings["openai_api_key"].configured is True
    assert response.settings["openai_api_key"].masked == "sk-***"
    service.get_all.assert_awaited_once_with(include_secrets=False)


@pytest.mark.asyncio
async def test_update_settings_uses_request_body_for_environment_updates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings_routes, "require_settings_owner", AsyncMock())
    monkeypatch.setattr(
        settings_routes, "_validate_openai_key", AsyncMock(return_value=(True, None))
    )
    monkeypatch.setattr(
        settings_routes,
        "_validate_anthropic_key",
        AsyncMock(return_value=(True, None)),
    )
    monkeypatch.setattr(
        settings_routes, "_validate_gemini_key", AsyncMock(return_value=(True, None))
    )
    monkeypatch.setattr(settings_routes, "_try_reset_graph_client", AsyncMock())

    service = AsyncMock()
    monkeypatch.setattr(settings_routes, "get_settings_service", lambda: service)

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("SIBYL_GRAPH_EMBEDDING_PROVIDER", raising=False)

    body = settings_routes.UpdateSettingsRequest(
        openai_api_key="sk-openai-test",
        anthropic_api_key="sk-ant-test",
        gemini_api_key="gemini-test",
        graph_embedding_provider="gemini",
    )

    response = await settings_routes.update_settings(_request(), body=body)

    assert os.environ["OPENAI_API_KEY"] == "sk-openai-test"
    assert os.environ["ANTHROPIC_API_KEY"] == "sk-ant-test"
    assert os.environ["GEMINI_API_KEY"] == "gemini-test"
    assert os.environ["GOOGLE_API_KEY"] == "gemini-test"
    assert os.environ["SIBYL_GRAPH_EMBEDDING_PROVIDER"] == "gemini"
    assert response.updated == [
        "openai_api_key",
        "anthropic_api_key",
        "gemini_api_key",
        "graph_embedding_provider",
    ]


@pytest.mark.asyncio
async def test_delete_setting_rejects_setup_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings_routes, "is_setup_mode", AsyncMock(return_value=True))

    with pytest.raises(HTTPException, match="Cannot delete settings during setup mode") as exc_info:
        await settings_routes.delete_setting(_request(), key="openai_api_key")

    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_update_settings_uses_global_admin_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    async def reject_global_admin(_request: Request) -> None:
        raise HTTPException(status_code=403, detail="Global admin required")

    monkeypatch.setattr(settings_routes, "require_settings_owner", reject_global_admin)

    with pytest.raises(HTTPException) as exc_info:
        await settings_routes.update_settings(
            _request(),
            body=settings_routes.UpdateSettingsRequest(openai_api_key="sk-openai-test"),
        )

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail == "Global admin required"


@pytest.mark.asyncio
async def test_try_reset_graph_client_uses_runtime_helper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reset_runtime = AsyncMock()
    reset_embeddings = MagicMock()
    monkeypatch.setattr(settings_routes, "reset_graph_runtime", reset_runtime)
    monkeypatch.setattr(settings_routes, "reset_document_embedding_runtime", reset_embeddings)

    await settings_routes._try_reset_graph_client("test context")

    reset_runtime.assert_awaited_once_with()
    reset_embeddings.assert_called_once_with()
