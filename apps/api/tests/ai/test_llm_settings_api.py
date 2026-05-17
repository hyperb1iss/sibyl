from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from pydantic import SecretStr
from starlette.requests import Request

from sibyl.ai.llm import routes
from sibyl_core.ai.llm.config import ConfigField, LLMSurface, ResolvedLLMConfig
from sibyl_core.ai.validation import SurfaceTestResult


class FakeConfigSource:
    def __init__(self, resolved: ResolvedLLMConfig) -> None:
        self.resolved = resolved
        self.invalidated: list[LLMSurface | None] = []

    async def resolve(self, surface: LLMSurface) -> ResolvedLLMConfig:
        return self.resolved.model_copy(update={"surface": surface})

    async def invalidate(self, surface: LLMSurface | None = None) -> None:
        self.invalidated.append(surface)


def _request() -> Request:
    return Request({"type": "http", "method": "GET", "path": "/settings/ai/llm", "headers": []})


@pytest.mark.asyncio
async def test_get_llm_settings_returns_instance_wide_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(routes, "require_settings_owner", AsyncMock())
    monkeypatch.setattr(routes, "get_config_source", lambda: FakeConfigSource(_resolved()))

    response = await routes.get_llm_settings(_request())

    assert response.scope == "instance_wide"
    assert set(response.surfaces) == {
        LLMSurface.DEFAULT,
        LLMSurface.CRAWLER,
        LLMSurface.SYNTHESIS,
    }
    assert response.surfaces[LLMSurface.CRAWLER].api_key.configured is True
    assert response.surfaces[LLMSurface.CRAWLER].api_key.masked is not None


@pytest.mark.asyncio
async def test_update_llm_surface_writes_db_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = AsyncMock()
    monkeypatch.setattr(routes, "require_settings_owner", AsyncMock())
    monkeypatch.setattr(routes, "get_config_source", lambda: FakeConfigSource(_resolved()))
    monkeypatch.setattr(routes, "get_settings_service", lambda: service)
    monkeypatch.setattr(routes, "invalidate_llm_runtime", AsyncMock())

    response = await routes.update_llm_surface(
        _request(),
        LLMSurface.CRAWLER,
        routes.UpdateLLMSurfaceRequest(model="claude-sonnet-4-6", temperature=0.2),
    )

    service.set_llm_setting.assert_any_await("crawler", "model", "claude-sonnet-4-6")
    service.set_llm_setting.assert_any_await("crawler", "temperature", 0.2)
    assert response.warning is None


@pytest.mark.asyncio
async def test_update_llm_surface_rejects_env_locked_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(routes, "require_settings_owner", AsyncMock())
    resolved = _resolved(
        model=ConfigField(
            value="claude-haiku-4-5",
            source="env",
            locked_by_env=True,
            env_var="SIBYL_LLM_CRAWLER_MODEL",
        )
    )
    monkeypatch.setattr(routes, "get_config_source", lambda: FakeConfigSource(resolved))

    with pytest.raises(HTTPException) as exc_info:
        await routes.update_llm_surface(
            _request(),
            LLMSurface.CRAWLER,
            routes.UpdateLLMSurfaceRequest(model="claude-sonnet-4-6"),
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["code"] == "LOCKED_BY_ENV"


@pytest.mark.asyncio
async def test_llm_surface_test_delegates_to_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(routes, "require_settings_owner", AsyncMock())
    result = SurfaceTestResult(
        surface=LLMSurface.CRAWLER,
        provider="anthropic",
        model="claude-haiku-4-5-20251001",
        status="valid",
        valid=True,
        latency_ms=12.0,
        parsed_output={"ok": True},
    )
    probe = AsyncMock(return_value=result)
    monkeypatch.setattr(routes, "test_surface_config", probe)
    monkeypatch.setattr(routes, "get_config_source", lambda: FakeConfigSource(_resolved()))

    response = await routes.test_llm_surface(_request(), LLMSurface.CRAWLER)

    assert response is result
    probe.assert_awaited_once()


@pytest.mark.asyncio
async def test_llm_settings_routes_reject_non_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    async def reject(_: Request) -> None:
        raise HTTPException(status_code=403, detail="forbidden")

    monkeypatch.setattr(routes, "require_settings_owner", reject)

    with pytest.raises(HTTPException) as exc_info:
        await routes.get_llm_settings(_request())

    assert exc_info.value.status_code == 403


def _resolved(**overrides: ConfigField | object) -> ResolvedLLMConfig:
    values = {
        "surface": LLMSurface.CRAWLER,
        "provider": ConfigField(value="anthropic", source="default"),
        "model": ConfigField(value="claude-haiku-4-5", source="default"),
        "temperature": ConfigField(value=0.0, source="default"),
        "max_tokens": ConfigField(value=None, source="default"),
        "timeout_seconds": ConfigField(value=60.0, source="default"),
        "api_key": ConfigField(value=SecretStr("anthropic-key"), source="db"),
    }
    values.update(overrides)
    return ResolvedLLMConfig(**values)
