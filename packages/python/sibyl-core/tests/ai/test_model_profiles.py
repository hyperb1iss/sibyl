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
    assert corrected.get("anthropic_supports_forced_tool_choice", True) is True


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
