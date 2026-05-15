from __future__ import annotations

import pytest
from pydantic import SecretStr

from sibyl_core.ai.errors import LLMConfigError
from sibyl_core.ai.llm.config import (
    ConfigField,
    EnvConfigSource,
    LLMConfig,
    LLMSurface,
    get_config_source,
    invalidate_llm_config,
    resolve_llm_config,
    set_config_source,
)


@pytest.mark.asyncio
async def test_env_config_source_uses_defaults_without_env() -> None:
    source = EnvConfigSource({})

    resolved = await source.resolve(LLMSurface.CRAWLER)

    assert resolved.surface is LLMSurface.CRAWLER
    assert resolved.provider.value == "anthropic"
    assert resolved.provider.source == "default"
    assert resolved.model.value == "claude-haiku-4-5"
    assert resolved.temperature.value == 0.0
    assert resolved.max_tokens.value is None
    assert resolved.timeout_seconds.value == 60.0
    assert resolved.api_key.value is None


@pytest.mark.asyncio
async def test_env_config_source_surface_env_wins_over_global_env() -> None:
    source = EnvConfigSource(
        {
            "SIBYL_LLM_PROVIDER": "anthropic",
            "SIBYL_LLM_MODEL": "claude-haiku-4-5",
            "SIBYL_LLM_CRAWLER_PROVIDER": "gemini",
            "SIBYL_LLM_CRAWLER_MODEL": "gemini-3-flash",
            "SIBYL_LLM_CRAWLER_TEMPERATURE": "0.4",
            "SIBYL_LLM_CRAWLER_MAX_TOKENS": "1024",
            "SIBYL_LLM_CRAWLER_TIMEOUT_SECONDS": "7.5",
            "SIBYL_GEMINI_API_KEY": "gemini-key",
        }
    )

    resolved = await source.resolve(LLMSurface.CRAWLER)

    assert resolved.provider.value == "gemini"
    assert resolved.provider.source == "env"
    assert resolved.provider.locked_by_env is True
    assert resolved.provider.env_var == "SIBYL_LLM_CRAWLER_PROVIDER"
    assert resolved.model.value == "gemini-3-flash"
    assert resolved.temperature.value == 0.4
    assert resolved.max_tokens.value == 1024
    assert resolved.timeout_seconds.value == 7.5
    assert isinstance(resolved.api_key.value, SecretStr)
    assert resolved.api_key.value.get_secret_value() == "gemini-key"
    assert resolved.api_key.locked_by_env is True


@pytest.mark.asyncio
async def test_env_config_source_uses_google_api_key_fallback_for_gemini() -> None:
    source = EnvConfigSource({"SIBYL_LLM_PROVIDER": "gemini", "GOOGLE_API_KEY": "google-key"})

    resolved = await source.resolve(LLMSurface.DEFAULT)

    assert resolved.provider.value == "gemini"
    assert resolved.api_key.value is not None
    assert resolved.api_key.value.get_secret_value() == "google-key"
    assert resolved.api_key.env_var == "GOOGLE_API_KEY"


@pytest.mark.asyncio
async def test_env_config_source_rejects_unsupported_provider() -> None:
    source = EnvConfigSource({"SIBYL_LLM_PROVIDER": "ollama"})

    with pytest.raises(LLMConfigError, match="Unsupported LLM provider"):
        await source.resolve(LLMSurface.DEFAULT)


@pytest.mark.asyncio
async def test_env_config_source_rejects_invalid_numeric_env() -> None:
    source = EnvConfigSource({"SIBYL_LLM_TEMPERATURE": "warm"})

    with pytest.raises(LLMConfigError, match="Invalid float"):
        await source.resolve(LLMSurface.DEFAULT)


def test_resolved_config_converts_to_llm_config() -> None:
    config = LLMConfig(
        provider="openai",
        model="gpt-5.4-mini",
        temperature=0.1,
        max_tokens=123,
        timeout_seconds=12.0,
        api_key=SecretStr("openai-key"),
    )

    assert config.api_key is not None
    assert config.api_key.get_secret_value() == "openai-key"


@pytest.mark.asyncio
async def test_global_config_source_can_be_swapped() -> None:
    original = get_config_source()
    source = EnvConfigSource({"SIBYL_LLM_PROVIDER": "openai", "OPENAI_API_KEY": "key"})

    try:
        set_config_source(source)
        resolved = await resolve_llm_config(LLMSurface.DEFAULT)
        await invalidate_llm_config()
    finally:
        set_config_source(original)

    assert resolved.provider.value == "openai"
    assert resolved.api_key.value is not None
    assert resolved.api_key.value.get_secret_value() == "key"


def test_config_field_tracks_env_lock_metadata() -> None:
    field = ConfigField(value="gpt-5.4-mini", source="env", locked_by_env=True, env_var="MODEL")

    assert field.locked_by_env is True
    assert field.env_var == "MODEL"
