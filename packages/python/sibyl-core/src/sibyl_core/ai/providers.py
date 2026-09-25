"""Provider model factory for Sibyl LLM calls."""

from __future__ import annotations

import os
import re
from contextlib import AsyncExitStack
from dataclasses import replace
from typing import Literal

from anthropic import AsyncAnthropic
from openai import AsyncOpenAI
from pydantic_ai.models import Model
from pydantic_ai.models.anthropic import AnthropicModel, AnthropicModelSettings
from pydantic_ai.models.google import GoogleModel, GoogleModelSettings
from pydantic_ai.models.openai import OpenAIResponsesModel, OpenAIResponsesModelSettings
from pydantic_ai.profiles import ModelProfile, merge_profile
from pydantic_ai.profiles.anthropic import AnthropicModelProfile
from pydantic_ai.providers.anthropic import AnthropicProvider
from pydantic_ai.providers.google import GoogleProvider
from pydantic_ai.providers.openai import OpenAIProvider

from sibyl_core.ai.bedrock import (
    BedrockConfigError,
    BedrockSettings,
    anthropic_bedrock_client,
    apply_inference_scope,
    arn_model_id,
    has_geo_prefix,
    is_arn,
    resolve_bedrock_settings,
)
from sibyl_core.ai.errors import LLMConfigError
from sibyl_core.ai.llm.config import ANTHROPIC_FAMILY, AnthropicEffort, LLMConfig
from sibyl_core.ai.registry import ModelKind, canonical_model_alias, model_registry
from sibyl_core.ai.transport import RecordingAnthropicClient, RecordingOpenAIClient


def build_model(config: LLMConfig, *, resources: AsyncExitStack | None = None) -> Model:
    provider_model_id = resolve_provider_model_id(config)
    api_key = config.api_key.get_secret_value() if config.api_key else None

    match config.provider:
        case "anthropic":
            http_client = RecordingAnthropicClient()
            if resources is not None:
                resources.push_async_callback(http_client.aclose)
            return AnthropicModel(
                provider_model_id,
                provider=AnthropicProvider(
                    anthropic_client=AsyncAnthropic(
                        api_key=api_key,
                        max_retries=config.transport_max_retries,
                        http_client=http_client,
                    )
                ),
                settings=AnthropicModelSettings(**_anthropic_settings(config)),
            )
        case "bedrock":
            bedrock = bedrock_settings(api_key=api_key)
            http_client = RecordingAnthropicClient()
            if resources is not None:
                resources.push_async_callback(http_client.aclose)
            return AnthropicModel(
                provider_model_id,
                provider=AnthropicProvider(
                    anthropic_client=anthropic_bedrock_client(
                        bedrock,
                        http_client=http_client,
                        max_retries=config.transport_max_retries,
                    )
                ),
                settings=AnthropicModelSettings(**_anthropic_settings(config)),
                profile=_bedrock_profile(config, provider_model_id, bedrock),
            )
        case "gemini":
            return GoogleModel(
                provider_model_id,
                provider=_google_provider(api_key),
                settings=GoogleModelSettings(**_settings(config)),
            )
        case "openai":
            http_client = RecordingOpenAIClient()
            if resources is not None:
                resources.push_async_callback(http_client.aclose)
            return OpenAIResponsesModel(
                provider_model_id,
                provider=OpenAIProvider(
                    openai_client=AsyncOpenAI(
                        api_key=api_key,
                        max_retries=config.transport_max_retries,
                        http_client=http_client,
                    )
                ),
                settings=OpenAIResponsesModelSettings(**_settings(config)),
            )


#: Anthropic models that reject a forced tool choice (``tool_choice`` of type
#: ``any`` or ``tool``). Tool output forces its output tool, so structured
#: output on these models has to go through native structured output.
ANTHROPIC_MODELS_WITHOUT_FORCED_TOOLS = frozenset(
    {"claude-opus-5-5", "claude-fable-5-1", "claude-mythos-5-1"}
)

#: Claude models whose Bedrock InvokeModel route accepts native structured
#: output (``output_config.format``). AWS documents Sonnet 4.5, Haiku 4.5,
#: Opus 4.5 and Opus 4.6; Sonnet 4.6 also answers it live. Opus 4.8, Opus 5,
#: Opus 5.5 and Sonnet 5 reject it with ``output_config.format: Extra inputs
#: are not permitted``, and bedrock-mantle rejects it for every model.
BEDROCK_JSON_SCHEMA_OUTPUT_MODELS = (
    "claude-haiku-4-5",
    "claude-sonnet-4-5",
    "claude-sonnet-4-6",
    "claude-opus-4-5",
    "claude-opus-4-6",
)


#: Bedrock foundation IDs for Claude models outside the curated registry whose
#: IDs carry a date or version suffix, from ``aws bedrock
#: list-inference-profiles``. Newer models follow ``anthropic.<alias>``.
_DATE_SUFFIX = re.compile(r"-\d{8}$")
BEDROCK_CLAUDE_MODEL_IDS = {
    "claude-opus-4-1": "anthropic.claude-opus-4-1-20250805-v1:0",
    "claude-opus-4-5": "anthropic.claude-opus-4-5-20251101-v1:0",
    "claude-opus-4-6": "anthropic.claude-opus-4-6-v1",
    "claude-sonnet-4": "anthropic.claude-sonnet-4-20250514-v1:0",
    "claude-sonnet-4-5": "anthropic.claude-sonnet-4-5-20250929-v1:0",
}


def rejects_forced_tool_choice(config: LLMConfig) -> bool:
    return (
        config.provider in ANTHROPIC_FAMILY
        and canonical_model_alias(config.model) in ANTHROPIC_MODELS_WITHOUT_FORCED_TOOLS
    )


def prefers_native_output(config: LLMConfig) -> bool:
    """Whether structured output should go through native strict output.

    A model that rejects forced tools upgrades to native output wherever the
    route supports it. Where it does not (Opus 5.5 on Bedrock), tool output
    stays and the profile degrades its tool choice to ``auto``.
    """
    return rejects_forced_tool_choice(config) and bool(
        resolved_model_profile(config).get("supports_json_schema_output", False)
    )


def anthropic_effort(config: LLMConfig) -> AnthropicEffort | None:
    """The configured effort as this model accepts it.

    A model without effort support gets none, so one process-wide effort cannot
    break a surface that runs an older model. ``xhigh`` falls back to ``high``
    where the model tops out there.
    """
    if config.provider not in ANTHROPIC_FAMILY or config.effort is None:
        return None
    profile = resolved_model_profile(config)
    if not profile.get("anthropic_supports_effort", False):
        return None
    if config.effort == "xhigh" and not profile.get("anthropic_supports_xhigh_effort", False):
        return "high"
    return config.effort


def resolved_model_profile(config: LLMConfig) -> ModelProfile:
    """Resolve provider schema capabilities without creating a network client."""
    provider_model_id = resolve_provider_model_id(config)
    if config.provider == "bedrock":
        return _bedrock_profile(config, provider_model_id, bedrock_settings())
    provider = {"anthropic": AnthropicProvider, "openai": OpenAIProvider, "gemini": GoogleProvider}[
        config.provider
    ]
    return provider.model_profile(provider_model_id) or {}


def _bedrock_profile(
    config: LLMConfig, provider_model_id: str, bedrock: BedrockSettings
) -> ModelProfile:
    """The full Anthropic profile for the model behind a Bedrock ID or ARN.

    pydantic-ai cannot see through an ARN, so the profile resolves from the
    model ID the ARN names and is handed to ``AnthropicModel`` whole.
    """
    profile = AnthropicProvider.model_profile(arn_model_id(provider_model_id) or provider_model_id)
    return merge_profile(profile or {}, bedrock_profile_overrides(config, bedrock))


def bedrock_profile_overrides(config: LLMConfig, bedrock: BedrockSettings) -> AnthropicModelProfile:
    """Turn off the Anthropic features a Bedrock route rejects for this model."""
    alias = canonical_model_alias(config.model)
    overrides = AnthropicModelProfile()
    if bedrock.api == "mantle" or not alias.startswith(BEDROCK_JSON_SCHEMA_OUTPUT_MODELS):
        overrides["supports_json_schema_output"] = False
    if alias in ANTHROPIC_MODELS_WITHOUT_FORCED_TOOLS:
        # With native output unavailable too, tool output has to ask with
        # ``tool_choice=auto``, which these models accept.
        overrides["anthropic_supports_forced_tool_choice"] = False
    return overrides


#: ``LLMConfigError.details["reason"]`` when Bedrock has no usable region,
#: settings or credentials, as opposed to a bad model choice.
BEDROCK_NOT_CONFIGURED = "bedrock_not_configured"


def bedrock_settings(*, api_key: str | None = None) -> BedrockSettings:
    """Resolved Bedrock settings; an explicit API key replaces the environment's."""
    try:
        resolved = resolve_bedrock_settings()
    except BedrockConfigError as exc:
        raise LLMConfigError(
            str(exc), provider="bedrock", details={"reason": BEDROCK_NOT_CONFIGURED}
        ) from exc
    if api_key is None or api_key == resolved.api_key:
        return resolved
    if resolved.profile:
        raise LLMConfigError(
            "A Bedrock API key cannot be combined with SIBYL_BEDROCK_PROFILE",
            provider="bedrock",
            details={"reason": BEDROCK_NOT_CONFIGURED},
        )
    return replace(resolved, api_key=api_key)


def resolve_provider_model_id(config: LLMConfig) -> str:
    if config.provider == "bedrock":
        return _bedrock_model_id(config)

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


def _bedrock_model_id(config: LLMConfig) -> str:
    """Map a Claude alias, snapshot or Bedrock ID to the ID Bedrock routes.

    An ID that already names an inference profile (``us.`` or ``global.``)
    is sent as given. Everything else resolves to a foundation-model ID and
    takes the configured inference scope, or the in-Region Mantle ID.
    """
    model = config.model.strip()
    if is_arn(model):
        # Sent as given. An inference-profile or foundation-model ARN still has
        # to name a Claude model; an opaque one cannot be checked.
        named = arn_model_id(model)
        if named is not None and not canonical_model_alias(named).startswith("claude-"):
            raise LLMConfigError(
                f"Bedrock serves Claude models here; {config.model} names {named}",
                provider="bedrock",
                model=config.model,
            )
        return model
    alias = canonical_model_alias(model)
    entry = model_registry.get(alias, kind=ModelKind.LLM)
    if entry is not None and entry.provider not in ANTHROPIC_FAMILY:
        raise LLMConfigError(
            f"Model {config.model} belongs to provider {entry.provider}, not bedrock",
            provider="bedrock",
            model=config.model,
        )
    if not alias.startswith("claude-"):
        raise LLMConfigError(
            f"Bedrock serves Claude models here; {config.model} is not one",
            provider="bedrock",
            model=config.model,
        )
    bedrock = bedrock_settings()
    if bedrock.api == "mantle":
        # bedrock-mantle serves in-Region IDs without a version suffix.
        return f"anthropic.{alias}"
    if has_geo_prefix(model):
        return model
    if model.startswith("anthropic."):
        return apply_inference_scope(model, bedrock.inference_scope)
    base = (
        (entry.platform_model_ids.get("bedrock") if entry else None)
        or BEDROCK_CLAUDE_MODEL_IDS.get(_DATE_SUFFIX.sub("", alias))
        or f"anthropic.{alias}"
    )
    return apply_inference_scope(base, bedrock.inference_scope)


def _anthropic_settings(config: LLMConfig) -> dict[str, float | int | str]:
    settings = _settings(config)
    if resolved_model_profile(config).get("anthropic_disallows_sampling_settings", False):
        settings.pop("temperature", None)
    if (effort := anthropic_effort(config)) is not None:
        settings["anthropic_effort"] = effort
    return settings


def _settings(config: LLMConfig) -> dict[str, float | int | str]:
    settings: dict[str, float | int | str] = {
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
