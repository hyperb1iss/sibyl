from __future__ import annotations

import pytest

from sibyl.ai.llm.config_source import DBSettingsConfigSource
from sibyl_core.ai.llm.config import LLMSurface


class FakeSettingsService:
    def __init__(self, values: dict[str, str] | None = None) -> None:
        self.values = values or {}

    async def get_llm_setting(self, surface: str, field: str) -> str | None:
        return self.values.get(f"llm.{surface}.{field}")

    async def get_database_value(self, key: str, *, decrypt: bool = True) -> str | None:
        return self.values.get(key)


@pytest.mark.asyncio
async def test_db_config_source_uses_db_values_when_env_is_unset() -> None:
    source = DBSettingsConfigSource(
        FakeSettingsService(
            {
                "llm.crawler.provider": "gemini",
                "llm.crawler.model": "gemini-3-flash",
                "llm.crawler.temperature": "0.4",
                "llm.crawler.max_tokens": "512",
                "llm.crawler.timeout_seconds": "15",
                "gemini_api_key": "gemini-db-key",
            }
        ),
        environ={},
    )

    resolved = await source.resolve(LLMSurface.CRAWLER)

    assert resolved.provider.value == "gemini"
    assert resolved.provider.source == "db"
    assert resolved.model.value == "gemini-3-flash"
    assert resolved.temperature.value == 0.4
    assert resolved.max_tokens.value == 512
    assert resolved.timeout_seconds.value == 15
    assert resolved.api_key.source == "db"
    assert resolved.api_key.value is not None
    assert resolved.api_key.value.get_secret_value() == "gemini-db-key"


@pytest.mark.asyncio
async def test_db_config_source_marks_env_overrides_as_locked() -> None:
    source = DBSettingsConfigSource(
        FakeSettingsService(
            {
                "llm.crawler.provider": "anthropic",
                "llm.crawler.model": "claude-sonnet-4-6",
                "anthropic_api_key": "db-key",
            }
        ),
        environ={
            "SIBYL_LLM_CRAWLER_PROVIDER": "openai",
            "SIBYL_LLM_CRAWLER_MODEL": "gpt-5.4-nano",
            "SIBYL_OPENAI_API_KEY": "env-openai-key",
        },
    )

    resolved = await source.resolve(LLMSurface.CRAWLER)

    assert resolved.provider.value == "openai"
    assert resolved.provider.source == "env"
    assert resolved.provider.locked_by_env is True
    assert resolved.provider.env_var == "SIBYL_LLM_CRAWLER_PROVIDER"
    assert resolved.model.value == "gpt-5.4-nano"
    assert resolved.api_key.source == "env"
    assert resolved.api_key.env_var == "SIBYL_OPENAI_API_KEY"


@pytest.mark.asyncio
async def test_db_config_source_supports_memory_surface_db_values() -> None:
    source = DBSettingsConfigSource(
        FakeSettingsService(
            {
                "llm.memory.provider": "anthropic",
                "llm.memory.model": "claude-haiku-4-5",
                "llm.memory.temperature": "0",
            }
        ),
        environ={},
    )

    resolved = await source.resolve(LLMSurface.MEMORY)

    assert resolved.surface is LLMSurface.MEMORY
    assert resolved.provider.value == "anthropic"
    assert resolved.provider.source == "db"
    assert resolved.model.value == "claude-haiku-4-5"
    assert resolved.model.source == "db"
    assert resolved.temperature.value == 0.0


@pytest.mark.asyncio
async def test_db_config_source_caches_until_invalidated() -> None:
    settings = FakeSettingsService({"llm.default.model": "claude-haiku-4-5"})
    source = DBSettingsConfigSource(settings, environ={})

    first = await source.resolve(LLMSurface.DEFAULT)
    settings.values["llm.default.model"] = "claude-sonnet-4-6"
    second = await source.resolve(LLMSurface.DEFAULT)
    await source.invalidate(LLMSurface.DEFAULT)
    third = await source.resolve(LLMSurface.DEFAULT)

    assert first.model.value == "claude-haiku-4-5"
    assert second.model.value == "claude-haiku-4-5"
    assert third.model.value == "claude-sonnet-4-6"


@pytest.mark.parametrize(
    ("model", "override", "expected"),
    [
        ("claude-opus-5", None, 32768),
        ("claude-opus-5", "8192", 8192),
        ("claude-haiku-4-5", None, None),
    ],
)
async def test_opus_memory_output_default_uses_final_db_model(model, override, expected):
    values = {"llm.memory.provider": "anthropic", "llm.memory.model": model}
    if override is not None:
        values["llm.memory.max_tokens"] = override
    settings = FakeSettingsService(values)
    source = DBSettingsConfigSource(settings, environ={})
    result = await source.resolve(LLMSurface.MEMORY)
    assert result.max_tokens.value == expected
    assert result.max_tokens.source == ("db" if override is not None else "default")
    settings.values["llm.memory.model"] = "claude-haiku-4-5"
    await source.invalidate()
    changed = await source.resolve(LLMSurface.MEMORY)
    assert changed.max_tokens.value == (8192 if override is not None else None)
