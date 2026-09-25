"""LLM provider and surface validation probes."""

from __future__ import annotations

import time
from typing import Literal

from pydantic import BaseModel, Field, SecretStr
from pydantic_ai import Agent, NativeOutput
from pydantic_ai.exceptions import ModelHTTPError

from sibyl_core.ai.bedrock import BedrockConfigError, resolve_bedrock_credentials
from sibyl_core.ai.clients import output_retry_budget
from sibyl_core.ai.errors import LLMConfigError, classify_llm_exception
from sibyl_core.ai.llm.config import LLMConfig, LLMConfigSource, LLMProviderName, LLMSurface
from sibyl_core.ai.providers import (
    BEDROCK_NOT_CONFIGURED,
    bedrock_settings,
    build_model,
    prefers_native_output,
    resolve_provider_model_id,
)
from sibyl_core.observability import telemetry_registry

ValidationStatus = Literal[
    "valid",
    "invalid_key",
    "network",
    "rate_limited",
    "model_not_found",
    "permission_denied",
    "missing_credentials",
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


async def check_provider_key(provider: LLMProviderName, key: str | None) -> KeyValidationResult:
    """Prove a provider answers with these credentials by making a minimal call.

    Bedrock needs no key: it signs with the AWS credential chain, so the probe
    first proves a region and credentials resolve, then sends the call.
    """
    model = _cheapest_probe_model(provider)
    started_at = time.perf_counter()
    try:
        config = LLMConfig(
            provider=provider,
            model=model,
            max_tokens=PROBE_MAX_TOKENS,
            timeout_seconds=10,
            api_key=SecretStr(key) if key else None,
        )
        await _require_provider_credentials(config)
        result = await _run_text_probe(config)
        latency_ms = _elapsed_ms(started_at)
        telemetry_registry().record_llm_call(
            surface="provider_key_validation",
            provider=provider,
            model=model,
            status="valid",
            duration_ms=latency_ms,
            input_tokens=_input_tokens(result),
            output_tokens=_output_tokens(result),
        )
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
        latency_ms = _elapsed_ms(started_at)
        telemetry_registry().record_llm_call(
            surface="provider_key_validation",
            provider=provider,
            model=model,
            status=_status_for_exception(exc),
            duration_ms=latency_ms,
        )
        return KeyValidationResult(
            provider=provider,
            model=model,
            status=_status_for_exception(exc),
            valid=False,
            latency_ms=latency_ms,
            error=str(exc),
        )


async def check_model_availability(
    provider: LLMProviderName,
    provider_model_id: str,
    key: str | None,
) -> ModelValidationResult:
    config = LLMConfig(
        provider=provider,
        model=provider_model_id,
        max_tokens=PROBE_MAX_TOKENS,
        timeout_seconds=10,
        api_key=SecretStr(key) if key else None,
    )
    started_at = time.perf_counter()
    try:
        await _require_provider_credentials(config)
        result = await _run_text_probe(config)
        latency_ms = _elapsed_ms(started_at)
        telemetry_registry().record_llm_call(
            surface="model_availability_validation",
            provider=provider,
            model=provider_model_id,
            status="valid",
            duration_ms=latency_ms,
            input_tokens=_input_tokens(result),
            output_tokens=_output_tokens(result),
        )
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
        latency_ms = _elapsed_ms(started_at)
        telemetry_registry().record_llm_call(
            surface="model_availability_validation",
            provider=provider,
            model=provider_model_id,
            status=_status_for_exception(exc),
            duration_ms=latency_ms,
        )
        return ModelValidationResult(
            provider=provider,
            requested_model=provider_model_id,
            status=_status_for_exception(exc),
            valid=False,
            latency_ms=latency_ms,
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
        agent = Agent[object, _SurfaceProbe](
            build_model(config),
            output_type=(
                NativeOutput(_SurfaceProbe, strict=True)
                if prefers_native_output(config)
                else _SurfaceProbe
            ),
            retries=output_retry_budget(1),
        )
        result = await agent.run(
            "Return ok=true and a short summary confirming this Sibyl LLM surface is ready."
        )
        latency_ms = _elapsed_ms(started_at)
        telemetry_registry().record_llm_call(
            surface=surface.value,
            provider=config.provider,
            model=resolve_provider_model_id(config),
            status="valid",
            duration_ms=latency_ms,
            input_tokens=_input_tokens(result),
            output_tokens=_output_tokens(result),
        )
        return SurfaceTestResult(
            surface=surface,
            provider=config.provider,
            model=resolve_provider_model_id(config),
            status="valid",
            valid=True,
            latency_ms=latency_ms,
            parsed_output=result.output.model_dump(),
            input_tokens=_input_tokens(result),
            output_tokens=_output_tokens(result),
        )
    except Exception as exc:
        latency_ms = _elapsed_ms(started_at)
        telemetry_registry().record_llm_call(
            surface=surface.value,
            provider=config.provider,
            model=config.model,
            status=_status_for_exception(exc),
            duration_ms=latency_ms,
        )
        return SurfaceTestResult(
            surface=surface,
            provider=config.provider,
            model=config.model,
            status=_status_for_exception(exc),
            valid=False,
            latency_ms=latency_ms,
            error=str(exc),
        )


async def _require_provider_credentials(config: LLMConfig) -> None:
    """Fail fast with a fix-it message when Bedrock has no region or credentials."""
    if config.provider != "bedrock":
        return
    api_key = config.api_key.get_secret_value() if config.api_key else None
    try:
        await resolve_bedrock_credentials(bedrock_settings(api_key=api_key))
    except BedrockConfigError as exc:
        raise LLMConfigError(
            str(exc),
            provider="bedrock",
            model=config.model,
            details={"reason": BEDROCK_NOT_CONFIGURED},
        ) from exc


async def _run_text_probe(config: LLMConfig):
    agent = Agent(build_model(config), output_type=str, retries=output_retry_budget(0))
    return await agent.run("Reply with the single word ok.")


def _cheapest_probe_model(provider: LLMProviderName) -> str:
    return {
        "anthropic": "claude-haiku-4-5",
        "bedrock": "claude-haiku-4-5",
        "gemini": "gemini-3-1-flash-lite",
        "openai": "gpt-5.4-nano",
    }[provider]


def _status_for_exception(exc: Exception) -> ValidationStatus:
    if _is_missing_aws_credentials(exc):
        return "missing_credentials"
    if isinstance(exc, ModelHTTPError):
        if exc.status_code == 400 and _is_bedrock_unknown_model(exc):
            return "model_not_found"
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


def _is_missing_aws_credentials(exc: BaseException) -> bool:
    """Bedrock settings or botocore credentials missing, however the SDK wraps them."""
    current: BaseException | None = exc
    while current is not None:
        if isinstance(current, LLMConfigError) and (
            current.details.get("reason") == BEDROCK_NOT_CONFIGURED
        ):
            return True
        if type(current).__name__ in {"NoCredentialsError", "PartialCredentialsError"}:
            return True
        if "Could not resolve AWS credentials" in str(current):
            return True
        current = current.__cause__ or current.__context__
    return False


def _is_bedrock_unknown_model(exc: ModelHTTPError) -> bool:
    """Bedrock answers an unknown or unroutable model ID with a 400 ValidationException."""
    body = exc.body if isinstance(exc.body, dict) else {}
    message = str(body.get("message") or "")
    return (
        "model identifier is invalid" in message
        or "on-demand throughput isn" in message
        or "inference profile" in message
    )


def _elapsed_ms(started_at: float) -> float:
    return round((time.perf_counter() - started_at) * 1000, 2)


def _input_tokens(result: object) -> int | None:
    usage = getattr(result, "usage", None)
    return getattr(usage, "input_tokens", None)


def _output_tokens(result: object) -> int | None:
    usage = getattr(result, "usage", None)
    return getattr(usage, "output_tokens", None)
