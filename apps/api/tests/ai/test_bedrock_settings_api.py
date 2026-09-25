"""Bedrock through the settings API, setup status and the DB config source."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from pydantic import SecretStr
from starlette.requests import Request

from sibyl.ai.llm import routes
from sibyl.ai.llm.config_source import DBSettingsConfigSource, resolve_provider_api_key
from sibyl.api.routes import setup as setup_routes
from sibyl_core.ai.llm.config import ConfigField, LLMSurface, ResolvedLLMConfig
from sibyl_core.ai.validation import KeyValidationResult, ModelValidationResult


class FakeConfigSource:
    def __init__(self, resolved: ResolvedLLMConfig) -> None:
        self.resolved = resolved

    async def resolve(self, surface: LLMSurface) -> ResolvedLLMConfig:
        return self.resolved.model_copy(update={"surface": surface})

    async def invalidate(self, surface: LLMSurface | None = None) -> None:
        return None


class FakeSettingsService:
    def __init__(self, values: dict[str, str] | None = None) -> None:
        self.values = values or {}
        self.database_reads: list[str] = []

    async def get_llm_setting(self, surface: str, field: str) -> str | None:
        return self.values.get(f"llm.{surface}.{field}")

    async def get_database_value(self, key: str, *, decrypt: bool = True) -> str | None:
        self.database_reads.append(key)
        return self.values.get(key)


def _request() -> Request:
    return Request({"type": "http", "method": "POST", "path": "/settings/ai", "headers": []})


def _resolved(provider: str = "bedrock", model: str = "claude-haiku-4-5") -> ResolvedLLMConfig:
    return ResolvedLLMConfig(
        surface=LLMSurface.DEFAULT,
        provider=ConfigField(value=provider, source="default"),
        model=ConfigField(value=model, source="default"),
        temperature=ConfigField(value=0.0, source="default"),
        max_tokens=ConfigField(value=None, source="default"),
        timeout_seconds=ConfigField(value=60.0, source="default"),
        api_key=ConfigField(value=None, source="default"),
    )


def _key_result(provider: str = "bedrock") -> KeyValidationResult:
    return KeyValidationResult(
        provider=provider, model="claude-haiku-4-5", status="valid", valid=True, latency_ms=1.0
    )


@pytest.fixture(autouse=True)
def no_bedrock_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "SIBYL_BEDROCK_API_KEY",
        "AWS_BEARER_TOKEN_BEDROCK",
        "SIBYL_BEDROCK_REGION",
        "AWS_REGION",
        "AWS_DEFAULT_REGION",
        "SIBYL_BEDROCK_PROFILE",
        "AWS_PROFILE",
        "AWS_ACCESS_KEY_ID",
        "AWS_WEB_IDENTITY_TOKEN_FILE",
        "AWS_CONTAINER_CREDENTIALS_FULL_URI",
        "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.asyncio
async def test_bedrock_key_test_needs_no_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    check = AsyncMock(return_value=_key_result())
    monkeypatch.setattr(routes, "require_settings_owner", AsyncMock())
    monkeypatch.setattr(routes, "get_settings_service", FakeSettingsService)
    monkeypatch.setattr(routes, "check_provider_key", check)

    result = await routes.test_provider_key(_request(), "bedrock")

    assert result.valid is True
    check.assert_awaited_once_with("bedrock", None)


@pytest.mark.asyncio
async def test_other_providers_still_require_a_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(routes, "require_settings_owner", AsyncMock())
    monkeypatch.setattr(routes, "get_settings_service", FakeSettingsService)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("SIBYL_ANTHROPIC_API_KEY", raising=False)

    with pytest.raises(HTTPException) as caught:
        await routes.test_provider_key(_request(), "anthropic")

    assert caught.value.status_code == 400


@pytest.mark.asyncio
async def test_model_test_can_route_a_claude_alias_through_bedrock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    check = AsyncMock(
        return_value=ModelValidationResult(
            provider="bedrock",
            requested_model="claude-opus-5-5",
            status="valid",
            valid=True,
            latency_ms=1.0,
        )
    )
    monkeypatch.setattr(routes, "require_settings_owner", AsyncMock())
    monkeypatch.setattr(routes, "get_settings_service", FakeSettingsService)
    monkeypatch.setattr(routes, "check_model_availability", check)

    await routes.test_model_availability(_request(), "claude-opus-5-5", provider="bedrock")

    check.assert_awaited_once_with("bedrock", "claude-opus-5-5", None)

    with pytest.raises(HTTPException) as caught:
        await routes.test_model_availability(_request(), "gemini-3-flash", provider="bedrock")
    assert caught.value.status_code == 400


def test_surface_updates_accept_claude_aliases_on_bedrock_only() -> None:
    resolved = _resolved()
    assert (
        routes._validate_model_selection(
            resolved, routes.UpdateLLMSurfaceRequest(provider="bedrock", model="claude-opus-5-5")
        )
        is None
    )
    with pytest.raises(HTTPException) as caught:
        routes._validate_model_selection(
            resolved, routes.UpdateLLMSurfaceRequest(provider="bedrock", model="gpt-5.4-mini")
        )
    assert caught.value.status_code == 422


@pytest.mark.asyncio
async def test_db_config_source_resolves_bedrock_without_a_stored_key() -> None:
    service = FakeSettingsService(
        {"llm.memory.provider": "bedrock", "llm.memory.model": "claude-opus-5-5"}
    )
    source = DBSettingsConfigSource(service, environ={})

    resolved = await source.resolve(LLMSurface.MEMORY)

    assert resolved.provider.value == "bedrock"
    assert resolved.api_key.value is None
    assert service.database_reads == []
    # Memory defaults key by Claude alias, so Bedrock keeps the Opus 5.5 budget.
    assert resolved.max_tokens.value == 32_768
    assert resolved.effort.value == "high"


@pytest.mark.asyncio
async def test_bedrock_api_key_comes_from_the_environment() -> None:
    field = await resolve_provider_api_key(
        FakeSettingsService(), "bedrock", environ={"AWS_BEARER_TOKEN_BEDROCK": "bedrock-key"}
    )
    assert field.value == SecretStr("bedrock-key")
    assert field.env_var == "AWS_BEARER_TOKEN_BEDROCK"


@pytest.mark.parametrize(
    ("environ", "expected"),
    [
        ({"AWS_REGION": "us-west-2", "AWS_WEB_IDENTITY_TOKEN_FILE": "/var/run/token"}, True),
        ({"AWS_REGION": "us-west-2", "AWS_CONTAINER_CREDENTIALS_FULL_URI": "http://x"}, True),
        ({"SIBYL_BEDROCK_REGION": "us-west-2", "SIBYL_BEDROCK_PROFILE": "dev"}, True),
        ({"AWS_REGION": "us-west-2", "SIBYL_BEDROCK_API_KEY": "k"}, True),
        ({"AWS_REGION": "us-west-2"}, False),
        ({"AWS_WEB_IDENTITY_TOKEN_FILE": "/var/run/token"}, False),
    ],
)
def test_setup_marks_bedrock_configured_from_region_and_credential_hints(environ, expected):
    assert setup_routes.bedrock_configured(environ) is expected


@pytest.mark.asyncio
async def test_validate_keys_skips_bedrock_when_it_is_not_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    check = AsyncMock(return_value=_key_result())
    monkeypatch.setattr(setup_routes, "check_provider_key", check)

    assert await setup_routes._check_bedrock() == (None, None)
    check.assert_not_awaited()

    monkeypatch.setenv("AWS_REGION", "us-west-2")
    monkeypatch.setenv("AWS_WEB_IDENTITY_TOKEN_FILE", "/var/run/token")
    assert await setup_routes._check_bedrock() == (True, None)
    check.assert_awaited_once_with("bedrock", None)
