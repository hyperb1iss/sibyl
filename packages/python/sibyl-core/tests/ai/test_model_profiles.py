from __future__ import annotations

from contextlib import AsyncExitStack

import pytest
from pydantic import SecretStr
from pydantic_ai.providers.anthropic import AnthropicProvider

from sibyl_core.ai import providers
from sibyl_core.ai.llm.config import LLMConfig


def anthropic(model: str, **kwargs) -> LLMConfig:
    return LLMConfig(provider="anthropic", model=model, api_key=SecretStr("fixture"), **kwargs)


def test_haiku_5_5_profile_corrects_what_pydantic_ai_does_not_know() -> None:
    upstream = AnthropicProvider.model_profile("claude-haiku-5-5") or {}
    corrected = providers.resolved_model_profile(anthropic("claude-haiku-5-5"))
    # pydantic-ai matches no rule for Haiku 5.5 and would send temperature.
    assert upstream.get("anthropic_disallows_sampling_settings") is False
    assert corrected["anthropic_disallows_sampling_settings"] is True
    assert corrected["anthropic_supports_effort"] is True
    assert corrected["anthropic_supports_adaptive_thinking"] is True
    assert corrected["supports_json_schema_output"] is True
    assert corrected.get("supports_forced_tool_choice", True) is True


async def test_haiku_5_5_settings_and_model_carry_the_corrected_profile() -> None:
    config = anthropic("claude-haiku-5-5", effort="low")
    async with AsyncExitStack() as resources:
        model = providers.build_model(config, resources=resources)
    assert "temperature" not in (model.settings or {})
    assert (model.settings or {}).get("anthropic_effort") == "low"
    assert model.profile.get("anthropic_disallows_sampling_settings") is True


@pytest.mark.parametrize("model", ["claude-opus-5-5", "claude-sonnet-5-5", "claude-opus-5"])
def test_models_pydantic_ai_already_profiles_are_left_as_they_are(model: str) -> None:
    upstream = AnthropicProvider.model_profile(model) or {}
    assert providers.anthropic_profile(model) == upstream


@pytest.mark.parametrize("model", ["gpt-6-luna", "gpt-6.1-sol"])
def test_gpt_6_models_are_profiled_as_the_reasoning_models_they_are(model: str) -> None:
    # A non-reasoning profile would send temperature, which both reject.
    config = LLMConfig(provider="openai", model=model, api_key=SecretStr("fixture"))
    assert providers.resolved_model_profile(config)["openai_supports_reasoning"] is True


@pytest.mark.parametrize(
    ("configured", "sent"), [(None, providers.ANTHROPIC_DEFAULT_MAX_TOKENS), (32_768, 32_768)]
)
async def test_claude_requests_keep_sibyls_output_ceiling(configured, sent) -> None:
    config = anthropic("claude-opus-5-5", max_tokens=configured)
    async with AsyncExitStack() as resources:
        model = providers.build_model(config, resources=resources)
    assert (model.settings or {})["max_tokens"] == sent


async def test_openai_models_are_built_with_the_resolved_profile() -> None:
    for model in ("gpt-6-luna", "gpt-6.1-sol", "gpt-5.4-mini"):
        config = LLMConfig(provider="openai", model=model, api_key=SecretStr("fixture"))
        async with AsyncExitStack() as resources:
            built = providers.build_model(config, resources=resources)
        resolved = providers.resolved_model_profile(config)
        assert built.model_name == model
        # The built model layers provider defaults over the profile it is handed.
        assert {key: built.profile.get(key) for key in resolved} == resolved
        assert built.profile["openai_supports_reasoning"] is True
