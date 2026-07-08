"""Provider model factory for Sibyl LLM calls."""

from __future__ import annotations

import os
from typing import Literal

from pydantic_ai.models import Model
from pydantic_ai.models.anthropic import AnthropicModel, AnthropicModelSettings
from pydantic_ai.models.google import GoogleModel, GoogleModelSettings
from pydantic_ai.models.openai import OpenAIResponsesModel, OpenAIResponsesModelSettings
from pydantic_ai.providers.anthropic import AnthropicProvider
from pydantic_ai.providers.google import GoogleProvider
from pydantic_ai.providers.openai import OpenAIProvider

from sibyl_core.ai.errors import LLMConfigError
from sibyl_core.ai.llm.config import LLMConfig
from sibyl_core.ai.registry import ModelKind, model_registry


def build_model(config: LLMConfig) -> Model:
    provider_model_id = resolve_provider_model_id(config)
    api_key = config.api_key.get_secret_value() if config.api_key else None

    match config.provider:
        case "anthropic":
            return AnthropicModel(
                provider_model_id,
                provider=AnthropicProvider(api_key=api_key),
                settings=AnthropicModelSettings(**_settings(config)),
            )
        case "gemini":
            return GoogleModel(
                provider_model_id,
                provider=_google_provider(api_key),
                settings=GoogleModelSettings(**_settings(config)),
            )
        case "openai":
            return OpenAIResponsesModel(
                provider_model_id,
                provider=OpenAIProvider(api_key=api_key),
                settings=OpenAIResponsesModelSettings(**_settings(config)),
            )


def resolve_provider_model_id(config: LLMConfig) -> str:
    entry = model_registry.get(config.model, kind=ModelKind.LLM)
    if entry is None:
        return config.model

    if entry.provider != config.provider:
        raise LLMConfigError(
            f"Model {config.model} belongs to provider {entry.provider}, not {config.provider}",
            provider=config.provider,
            model=config.model,
        )

    if _pin_snapshots():
        return entry.snapshot
    return entry.provider_model_id


def _settings(config: LLMConfig) -> dict[str, float | int]:
    settings: dict[str, float | int] = {
        "temperature": config.temperature,
        "timeout": config.timeout_seconds,
    }
    if config.max_tokens is not None:
        settings["max_tokens"] = config.max_tokens
    return settings


def _google_provider(api_key: str | None) -> GoogleProvider | Literal["google"]:
    if api_key is None:
        return "google"
    return GoogleProvider(api_key=api_key)


def _pin_snapshots() -> bool:
    return os.environ.get("SIBYL_LLM_PIN_SNAPSHOTS", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
