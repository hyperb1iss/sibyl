from __future__ import annotations

import asyncio

import pytest
from pydantic import BaseModel, SecretStr
from pydantic_ai.output import PromptedOutput

from sibyl_core.ai.clients import (
    _provider_output_type,
    agent_cache_size,
    get_agent,
    invalidate_agent_cache,
)
from sibyl_core.ai.errors import LLMConfigError
from sibyl_core.ai.llm.config import (
    ConfigField,
    EnvConfigSource,
    LLMConfig,
    LLMSurface,
    ResolvedLLMConfig,
    set_config_source,
)
from sibyl_core.ai.providers import build_model


class StaticConfigSource:
    def __init__(self, config: LLMConfig) -> None:
        self.config = config
        self.invalidated: list[LLMSurface | None] = []

    async def resolve(self, surface: LLMSurface) -> ResolvedLLMConfig:
        return ResolvedLLMConfig(
            surface=surface,
            provider=ConfigField(value=self.config.provider, source="env", locked_by_env=True),
            model=ConfigField(value=self.config.model, source="env", locked_by_env=True),
            temperature=ConfigField(
                value=self.config.temperature, source="env", locked_by_env=True
            ),
            max_tokens=ConfigField(value=self.config.max_tokens, source="env", locked_by_env=True),
            timeout_seconds=ConfigField(
                value=self.config.timeout_seconds,
                source="env",
                locked_by_env=True,
            ),
            api_key=ConfigField(value=self.config.api_key, source="env", locked_by_env=True),
        )

    async def invalidate(self, surface: LLMSurface | None = None) -> None:
        self.invalidated.append(surface)


@pytest.fixture(autouse=True)
def reset_ai_state() -> None:
    invalidate_agent_cache()
    set_config_source(EnvConfigSource({}))
    yield
    invalidate_agent_cache()
    set_config_source(EnvConfigSource({}))


@pytest.mark.asyncio
async def test_get_agent_reuses_cache_for_same_loop_and_fingerprint() -> None:
    set_config_source(_source(model="claude-haiku-4-5"))

    first = await get_agent(LLMSurface.CRAWLER, system_prompt="extract facts")
    second = await get_agent(LLMSurface.CRAWLER, system_prompt="extract facts")

    assert first is second
    assert agent_cache_size() == 1


@pytest.mark.asyncio
async def test_get_agent_misses_cache_when_config_changes() -> None:
    source = _source(model="claude-haiku-4-5")
    set_config_source(source)
    first = await get_agent(LLMSurface.CRAWLER)

    source.config = LLMConfig(
        provider="anthropic",
        model="claude-sonnet-4-6",
        api_key=SecretStr("anthropic-key"),
    )
    second = await get_agent(LLMSurface.CRAWLER)

    assert first is not second
    assert agent_cache_size() == 2


@pytest.mark.asyncio
async def test_invalidate_agent_cache_can_clear_one_surface() -> None:
    set_config_source(_source(model="claude-haiku-4-5"))
    await get_agent(LLMSurface.CRAWLER)
    await get_agent(LLMSurface.SYNTHESIS)

    invalidate_agent_cache(LLMSurface.CRAWLER)

    assert agent_cache_size() == 1


def test_agent_cache_is_scoped_per_event_loop() -> None:
    set_config_source(_source(model="claude-haiku-4-5"))

    first = _run_in_new_loop(get_agent(LLMSurface.CRAWLER))
    second = _run_in_new_loop(get_agent(LLMSurface.CRAWLER))

    assert first is not second


def test_build_model_resolves_registry_alias_and_settings() -> None:
    model = build_model(
        LLMConfig(
            provider="anthropic",
            model="claude-haiku-4-5",
            temperature=0.2,
            max_tokens=42,
            timeout_seconds=3,
            api_key=SecretStr("anthropic-key"),
        )
    )

    assert model.model_name == "claude-haiku-4-5-20251001"
    assert model.system == "anthropic"
    assert model.settings == {"temperature": 0.2, "timeout": 3, "max_tokens": 42}


def test_build_model_uses_google_env_fallback_for_gemini(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GOOGLE_API_KEY", "google-key")

    model = build_model(LLMConfig(provider="gemini", model="gemini-3-1-flash-lite"))

    assert model.model_name == "gemini-3.1-flash-lite-preview"
    assert model.system == "google"


def test_provider_output_type_uses_prompted_output_for_gemini_structured_schema() -> None:
    output_type = _provider_output_type(
        LLMConfig(provider="gemini", model="gemini-3-1-flash-lite"),
        ClientPayload,
    )

    assert isinstance(output_type, PromptedOutput)


def test_provider_output_type_keeps_text_output_native() -> None:
    output_type = _provider_output_type(
        LLMConfig(provider="gemini", model="gemini-3-1-flash-lite"),
        str,
    )

    assert output_type is str


def test_build_model_rejects_registry_provider_mismatch() -> None:
    with pytest.raises(LLMConfigError, match="belongs to provider"):
        build_model(LLMConfig(provider="anthropic", model="gpt-5.4-nano"))


def _source(*, model: str) -> StaticConfigSource:
    return StaticConfigSource(
        LLMConfig(
            provider="anthropic",
            model=model,
            api_key=SecretStr("anthropic-key"),
        )
    )


def _run_in_new_loop(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class ClientPayload(BaseModel):
    name: str
