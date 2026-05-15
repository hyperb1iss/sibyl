"""LLM provider and surface validation probes."""

from __future__ import annotations

import time
from typing import Literal, cast

from pydantic import BaseModel, Field, SecretStr
from pydantic_ai import Agent
from pydantic_ai.exceptions import ModelHTTPError

from sibyl_core.ai.errors import classify_llm_exception
from sibyl_core.ai.llm.config import LLMConfig, LLMConfigSource, LLMProviderName, LLMSurface
from sibyl_core.ai.providers import build_model, resolve_provider_model_id

ValidationStatus = Literal[
    "valid",
    "invalid_key",
    "network",
    "rate_limited",
    "model_not_found",
    "permission_denied",
]

PROBE_MAX_TOKENS = 128


class KeyValidationResult(BaseModel):
    provider: LLMProviderName
    model: str
    status: ValidationStatus
    valid: bool
    latency_ms: float
    input_tokens: int | None = None
    output_tokens: int | None = None
    error: str | None = None


class ModelValidationResult(BaseModel):
    provider: LLMProviderName
    requested_model: str
    resolved_model: str | None = None
    status: ValidationStatus
    valid: bool
    latency_ms: float
    input_tokens: int | None = None
    output_tokens: int | None = None
    error: str | None = None


class SurfaceTestResult(BaseModel):
    surface: LLMSurface
    provider: LLMProviderName
    model: str
    status: ValidationStatus
    valid: bool
    latency_ms: float
    parsed_output: dict[str, object] | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    error: str | None = None


class _SurfaceProbe(BaseModel):
    ok: bool = Field(description="Whether the probe succeeded.")
    summary: str = Field(description="Short confirmation text.")


async def check_provider_key(provider: LLMProviderName, key: str) -> KeyValidationResult:
    model = _cheapest_probe_model(provider)
    started_at = time.perf_counter()
    try:
        result = await _run_text_probe(
            LLMConfig(
                provider=provider,
                model=model,
                max_tokens=PROBE_MAX_TOKENS,
                timeout_seconds=10,
                api_key=SecretStr(key),
            )
        )
        latency_ms = _elapsed_ms(started_at)
        return KeyValidationResult(
            provider=provider,
            model=model,
            status="valid",
            valid=True,
            latency_ms=latency_ms,
            input_tokens=_input_tokens(result),
            output_tokens=_output_tokens(result),
        )
    except Exception as exc:
        return KeyValidationResult(
            provider=provider,
            model=model,
            status=_status_for_exception(exc),
            valid=False,
            latency_ms=_elapsed_ms(started_at),
            error=str(exc),
        )


async def check_model_availability(
    provider: LLMProviderName,
    provider_model_id: str,
    key: str,
) -> ModelValidationResult:
    config = LLMConfig(
        provider=provider,
        model=provider_model_id,
        max_tokens=PROBE_MAX_TOKENS,
        timeout_seconds=10,
        api_key=SecretStr(key),
    )
    started_at = time.perf_counter()
    try:
        result = await _run_text_probe(config)
        latency_ms = _elapsed_ms(started_at)
        return ModelValidationResult(
            provider=provider,
            requested_model=provider_model_id,
            resolved_model=resolve_provider_model_id(config),
            status="valid",
            valid=True,
            latency_ms=latency_ms,
            input_tokens=_input_tokens(result),
            output_tokens=_output_tokens(result),
        )
    except Exception as exc:
        return ModelValidationResult(
            provider=provider,
            requested_model=provider_model_id,
            status=_status_for_exception(exc),
            valid=False,
            latency_ms=_elapsed_ms(started_at),
            error=str(exc),
        )


async def test_surface_config(
    surface: LLMSurface,
    source: LLMConfigSource,
) -> SurfaceTestResult:
    resolved = await source.resolve(surface)
    config = resolved.to_llm_config()
    started_at = time.perf_counter()
    try:
        agent = Agent(build_model(config), output_type=_SurfaceProbe, output_retries=1)
        result = await agent.run(
            "Return ok=true and a short summary confirming this Sibyl LLM surface is ready."
        )
        latency_ms = _elapsed_ms(started_at)
        return SurfaceTestResult(
            surface=surface,
            provider=config.provider,
            model=resolve_provider_model_id(config),
            status="valid",
            valid=True,
            latency_ms=latency_ms,
            parsed_output=cast(_SurfaceProbe, result.output).model_dump(),
            input_tokens=_input_tokens(result),
            output_tokens=_output_tokens(result),
        )
    except Exception as exc:
        return SurfaceTestResult(
            surface=surface,
            provider=config.provider,
            model=config.model,
            status=_status_for_exception(exc),
            valid=False,
            latency_ms=_elapsed_ms(started_at),
            error=str(exc),
        )


async def _run_text_probe(config: LLMConfig):
    agent = Agent(build_model(config), output_type=str, output_retries=0)
    return await agent.run("Reply with the single word ok.")


def _cheapest_probe_model(provider: LLMProviderName) -> str:
    return {
        "anthropic": "claude-haiku-4-5",
        "gemini": "gemini-3-1-flash-lite",
        "openai": "gpt-5.4-nano",
    }[provider]


def _status_for_exception(exc: Exception) -> ValidationStatus:
    if isinstance(exc, ModelHTTPError):
        return _status_for_http_code(exc.status_code)

    error = classify_llm_exception(exc)
    if error.__class__.__name__ == "LLMRateLimitError":
        return "rate_limited"
    return "network"


def _status_for_http_code(status_code: int) -> ValidationStatus:
    if status_code == 401:
        return "invalid_key"
    if status_code == 403:
        return "permission_denied"
    if status_code == 404:
        return "model_not_found"
    if status_code == 429:
        return "rate_limited"
    return "network"


def _elapsed_ms(started_at: float) -> float:
    return round((time.perf_counter() - started_at) * 1000, 2)


def _input_tokens(result: object) -> int | None:
    usage = getattr(result, "usage", None)
    return getattr(usage, "input_tokens", None)


def _output_tokens(result: object) -> int | None:
    usage = getattr(result, "usage", None)
    return getattr(usage, "output_tokens", None)
