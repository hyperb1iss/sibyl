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
        "SIBYL_EMBEDDING_PROVIDER",
        "SIBYL_GRAPH_EMBEDDING_PROVIDER",
        "SIBYL_EMBEDDING_DIMENSIONS",
        "SIBYL_GRAPH_EMBEDDING_DIMENSIONS",
        "SIBYL_BEDROCK_API",
        "ANTHROPIC_AWS_API_KEY",
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


class FakeSetupSettings:
    def __init__(self, values: dict[str, str] | None = None) -> None:
        self.values = values or {}

    async def get(self, key: str) -> str | None:
        return self.values.get(key)


def _use_providers(
    monkeypatch, *, llm: str, embeddings: str | None, graph: str | None = None
) -> None:
    monkeypatch.setattr(setup_routes, "get_config_source", lambda: FakeConfigSource(_resolved(llm)))
    values = {"embedding_provider": embeddings} if embeddings else {}
    if graph or embeddings:
        values["graph_embedding_provider"] = graph or embeddings or ""
    monkeypatch.setattr(setup_routes, "get_settings_service", lambda: FakeSetupSettings(values))


@pytest.mark.asyncio
async def test_an_aws_environment_alone_does_not_mark_bedrock_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    monkeypatch.setenv("AWS_PROFILE", "dev")
    _use_providers(monkeypatch, llm="anthropic", embeddings=None)

    assert await setup_routes.bedrock_selection() == (False, False)

    _use_providers(monkeypatch, llm="bedrock", embeddings="bedrock")
    assert await setup_routes.bedrock_selection() == (True, True)

    # A graph plane left on a keyed provider means embeddings are not covered.
    _use_providers(monkeypatch, llm="bedrock", embeddings="bedrock", graph="openai")
    assert await setup_routes.bedrock_selection() == (True, False)

    monkeypatch.delenv("AWS_REGION")
    assert await setup_routes.bedrock_selection() == (False, False)


@pytest.mark.asyncio
@pytest.mark.parametrize("installed", [True, False])
async def test_a_local_graph_plane_counts_only_with_its_dependency(
    monkeypatch: pytest.MonkeyPatch, installed: bool
) -> None:
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    monkeypatch.setattr(setup_routes, "sentence_transformers_available", lambda: installed)
    _use_providers(monkeypatch, llm="bedrock", embeddings="bedrock", graph="local")

    assert await setup_routes.bedrock_selection() == (True, installed)


@pytest.mark.asyncio
async def test_validate_keys_proves_sigv4_when_embeddings_use_bedrock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    monkeypatch.setenv("SIBYL_BEDROCK_API", "mantle")
    monkeypatch.setenv("ANTHROPIC_AWS_API_KEY", "claude-only-key")
    monkeypatch.setattr(setup_routes, "check_provider_key", AsyncMock(return_value=_key_result()))
    proved: list[object] = []

    async def prove(settings):
        proved.append(settings)
        raise RuntimeError("No AWS credentials found for Amazon Bedrock")

    monkeypatch.setattr(setup_routes, "resolve_bedrock_credentials", prove)
    _use_providers(monkeypatch, llm="bedrock", embeddings="bedrock")

    valid, error = await setup_routes._check_bedrock()

    assert valid is False
    assert "No AWS credentials" in (error or "")
    assert proved
    assert proved[0].claude_api_key is None


@pytest.mark.asyncio
async def test_an_instance_role_needs_only_a_region_once_bedrock_is_selected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    _use_providers(monkeypatch, llm="bedrock", embeddings="bedrock")

    assert setup_routes.bedrock_configured() is False
    assert await setup_routes.bedrock_selection() == (True, True)


@pytest.mark.asyncio
async def test_the_environment_wins_over_a_stale_stored_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    monkeypatch.setenv("SIBYL_EMBEDDING_PROVIDER", "bedrock")
    monkeypatch.setenv("SIBYL_GRAPH_EMBEDDING_PROVIDER", "bedrock")
    _use_providers(monkeypatch, llm="bedrock", embeddings="openai")

    assert await setup_routes.bedrock_selection() == (True, True)


@pytest.mark.asyncio
async def test_validate_keys_probes_bedrock_only_when_something_uses_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    check = AsyncMock(return_value=_key_result())
    monkeypatch.setattr(setup_routes, "check_provider_key", check)
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    monkeypatch.setenv("AWS_WEB_IDENTITY_TOKEN_FILE", "/var/run/token")

    _use_providers(monkeypatch, llm="anthropic", embeddings="openai")
    assert await setup_routes._check_bedrock() == (None, None)
    check.assert_not_awaited()

    _use_providers(monkeypatch, llm="bedrock", embeddings="openai")
    assert await setup_routes._check_bedrock() == (True, None)
    check.assert_awaited_once_with("bedrock", None)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("update", "stored", "rejected"),
    [
        ({"embedding_provider": "bedrock", "embedding_dimensions": 768}, {}, True),
        ({"embedding_provider": "bedrock"}, {"embedding_dimensions": "768"}, True),
        ({"embedding_dimensions": 3072}, {"embedding_provider": "bedrock"}, True),
        ({"graph_embedding_provider": "bedrock"}, {}, False),
        ({"embedding_provider": "bedrock", "embedding_dimensions": 1536}, {}, False),
        ({"embedding_dimensions": 768}, {"embedding_provider": "gemini"}, False),
    ],
)
async def test_settings_refuse_sizes_cohere_cannot_produce(
    monkeypatch: pytest.MonkeyPatch, update, stored, rejected
) -> None:
    from sibyl.api.routes import settings as settings_routes

    monkeypatch.setattr(setup_routes, "get_settings_service", lambda: FakeSetupSettings(stored))
    body = settings_routes.UpdateSettingsRequest(**update)
    check = settings_routes._reject_unservable_bedrock_dimensions(body)
    if rejected:
        with pytest.raises(HTTPException) as caught:
            await check
        assert caught.value.status_code == 422
    else:
        await check
