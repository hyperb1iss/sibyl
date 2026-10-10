"""Tests for settings route auth gating."""

from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from sibyl.api.routes import settings as settings_routes
from sibyl.services import settings as settings_module


@pytest.fixture(autouse=True)
def _no_deployment_locks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing in these tests is pinned by the deployment environment."""
    monkeypatch.setattr(settings_module, "_deployment_environment", {})


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


@pytest.fixture
def applied(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Stand in for the shared runtime-settings sync and its announcement."""
    sync = AsyncMock()
    monkeypatch.setattr(settings_routes, "sync_runtime_settings", sync)
    monkeypatch.setattr(settings_routes, "announce_runtime_settings_changed", AsyncMock())
    return sync


@pytest.mark.asyncio
async def test_update_settings_saves_every_key_then_applies_them_once(
    monkeypatch: pytest.MonkeyPatch, applied: AsyncMock
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
    events: list[str] = []
    service = AsyncMock()
    service.set.side_effect = lambda key, *args, **kwargs: events.append(f"save:{key}")
    applied.side_effect = lambda **kwargs: events.append("apply")
    monkeypatch.setattr(settings_routes, "get_settings_service", lambda: service)

    body = settings_routes.UpdateSettingsRequest(
        openai_api_key="sk-openai-test",
        anthropic_api_key="sk-ant-test",
        gemini_api_key="gemini-test",
        graph_embedding_provider="gemini",
    )

    response = await settings_routes.update_settings(_request(), body=body)

    assert response.updated == [
        "openai_api_key",
        "anthropic_api_key",
        "gemini_api_key",
        "graph_embedding_provider",
    ]
    # Every value is stored before the runtimes rebuild around them, once.
    assert events == [
        "save:openai_api_key",
        "save:anthropic_api_key",
        "save:gemini_api_key",
        "save:graph_embedding_provider",
        "apply",
    ]
    service.set.assert_any_await(
        "graph_embedding_provider",
        "gemini",
        is_secret=False,
        description="Graph embedding provider",
    )
    settings_routes.announce_runtime_settings_changed.assert_awaited_once_with(  # type: ignore[attr-defined]
        response.updated
    )


@pytest.mark.asyncio
async def test_update_settings_accepts_local_graph_embedding_provider(
    monkeypatch: pytest.MonkeyPatch, applied: AsyncMock
) -> None:
    monkeypatch.setattr(settings_routes, "require_settings_owner", AsyncMock())
    service = AsyncMock()
    monkeypatch.setattr(settings_routes, "get_settings_service", lambda: service)

    body = settings_routes.UpdateSettingsRequest(graph_embedding_provider="local")

    response = await settings_routes.update_settings(_request(), body=body)

    assert response.updated == ["graph_embedding_provider"]
    applied.assert_awaited_once()


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
