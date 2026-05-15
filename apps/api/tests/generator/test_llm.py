from __future__ import annotations

import pytest
from pydantic import SecretStr

from sibyl.generator.config import GeneratorConfig, ModelType
from sibyl.generator.llm import LLMContentGenerator
from sibyl_core.ai.llm import ConfigField, LLMSurface, ResolvedLLMConfig


async def _resolved_synthesis_config() -> ResolvedLLMConfig:
    return ResolvedLLMConfig(
        surface=LLMSurface.SYNTHESIS,
        provider=ConfigField(value="anthropic", source="default"),
        model=ConfigField(value="claude-sonnet-4-6", source="default"),
        temperature=ConfigField(value=0.2, source="default"),
        max_tokens=ConfigField(value=None, source="default"),
        timeout_seconds=ConfigField(value=60.0, source="default"),
        api_key=ConfigField(value=SecretStr("test-key"), source="db"),
    )


@pytest.mark.asyncio
async def test_generate_content_uses_synthesis_surface(monkeypatch, tmp_path) -> None:
    calls: list[tuple[LLMSurface, str, int | None]] = []

    class FakeGenerator:
        def __init__(self, *, surface: LLMSurface) -> None:
            self.surface = surface

        async def generate(self, prompt: str, *, max_tokens: int | None = None) -> str:
            calls.append((self.surface, prompt, max_tokens))
            return "generated text"

    async def fake_resolve(surface: LLMSurface) -> ResolvedLLMConfig:
        assert surface is LLMSurface.SYNTHESIS
        return await _resolved_synthesis_config()

    monkeypatch.setattr("sibyl.generator.llm.Generator", FakeGenerator)
    monkeypatch.setattr("sibyl.generator.llm.resolve_llm_config", fake_resolve)
    monkeypatch.setattr("sibyl.generator.llm.CACHE_DIR", tmp_path)

    generator = LLMContentGenerator(GeneratorConfig())

    result = await generator._generate_content("hello", max_tokens=123)

    assert result == "generated text"
    assert calls == [(LLMSurface.SYNTHESIS, "hello", 123)]


@pytest.mark.asyncio
async def test_generate_content_uses_cache_without_second_llm_call(monkeypatch, tmp_path) -> None:
    calls = 0

    class FakeGenerator:
        def __init__(self, *, surface: LLMSurface) -> None:
            assert surface is LLMSurface.SYNTHESIS

        async def generate(self, prompt: str, *, max_tokens: int | None = None) -> str:
            nonlocal calls
            calls += 1
            return f"{prompt}:{max_tokens}"

    async def fake_resolve(_: LLMSurface) -> ResolvedLLMConfig:
        return await _resolved_synthesis_config()

    monkeypatch.setattr("sibyl.generator.llm.CACHE_DIR", tmp_path)
    monkeypatch.setattr("sibyl.generator.llm.Generator", FakeGenerator)
    monkeypatch.setattr("sibyl.generator.llm.resolve_llm_config", fake_resolve)

    generator = LLMContentGenerator(GeneratorConfig(model=ModelType.OPUS))

    first = await generator._generate_content("hello", max_tokens=12)
    second = await generator._generate_content("hello", max_tokens=12)

    assert first == "hello:12"
    assert second == "hello:12"
    assert calls == 1
