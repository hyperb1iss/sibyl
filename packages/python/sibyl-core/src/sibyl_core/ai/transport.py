"""Safe, context-local receipts for SDK HTTP attempts during extraction."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from importlib.metadata import version
from typing import Any

from anthropic import DefaultAsyncHttpxClient as AnthropicHttpClient
from openai import DefaultAsyncHttpxClient
from pydantic import BaseModel, ConfigDict, Field

from sibyl_core.ai.llm.config import LLMConfig


class TransportAttempt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    status_code: int | None = None
    exception_type: str | None = None
    request_id: str | None = None
    usage_known: bool = False


class FailedExtractionUsage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    transport_attempts: list[TransportAttempt] = Field(default_factory=list)
    usage_complete: bool = False
    cost_usd: None = None
    cost_complete: bool = False


_attempts: ContextVar[list[TransportAttempt] | None] = ContextVar(
    "llm_transport_attempts", default=None
)

_reserve_attempt: ContextVar[Callable[[], Awaitable[None]] | None] = ContextVar(
    "llm_reserve_physical_attempt", default=None
)


@contextmanager
def reserve_uncovered_transport_attempts(
    covered: int, reserve_extra: Callable[[], Awaitable[None]]
) -> Iterator[None]:
    """Reserve unexpected HTTP hops before dispatch beyond the initial envelope."""
    claimed = 0
    denied: Exception | None = None

    async def claim() -> None:
        nonlocal claimed, denied
        if denied is not None:
            raise denied
        claimed += 1
        if claimed > covered:
            try:
                await reserve_extra()
            except Exception as exc:
                denied = exc
                raise

    token = _reserve_attempt.set(claim)
    try:
        yield
    except Exception:
        # SDKs wrap client-hook exceptions as connection errors. Preserve the
        # trusted local budget decision and prevent retries from reserving again.
        if denied is not None:
            raise denied from None
        raise
    finally:
        _reserve_attempt.reset(token)


@contextmanager
def collect_transport_attempts() -> Iterator[list[TransportAttempt]]:
    attempts: list[TransportAttempt] = []
    token = _attempts.set(attempts)
    try:
        yield attempts
    finally:
        _attempts.reset(token)


async def _record_send(
    send: Callable[..., Awaitable[Any]], request: Any, *, request_id_header: str, **kwargs: Any
) -> Any:
    attempts = _attempts.get()
    if reserve := _reserve_attempt.get():
        await reserve()
    try:
        response = await send(request, **kwargs)
    except (Exception, asyncio.CancelledError) as exc:
        if attempts is not None:
            # Exception messages and request/response bodies may contain secrets.
            attempts.append(TransportAttempt(exception_type=type(exc).__name__))
        raise
    if attempts is not None:
        request_id = response.headers.get(request_id_header)
        if request_id is not None and re.fullmatch(r"[a-zA-Z0-9_.:-]{1,200}", request_id) is None:
            request_id = None
        attempts.append(TransportAttempt(status_code=response.status_code, request_id=request_id))
    return response


class RecordingOpenAIClient(DefaultAsyncHttpxClient):
    async def _send_single_request(self, request: Any) -> Any:
        return await _record_send(
            super()._send_single_request, request, request_id_header="x-request-id"
        )


class RecordingAnthropicClient(AnthropicHttpClient):
    async def _send_single_request(self, request: Any) -> Any:
        return await _record_send(
            super()._send_single_request, request, request_id_header="request-id"
        )

    async def send(self, request: Any, **kwargs: Any) -> Any:
        response = await super().send(request, **kwargs)
        if _attempts.get() is not None and response.is_success and not kwargs.get("stream", False):
            _validate_anthropic_usage(response.json())
        return response


def _validate_anthropic_usage(payload: Any) -> None:
    """Reject malformed token counts before SDK usage normalization can erase them."""
    usage = payload.get("usage") if isinstance(payload, dict) else None
    if not isinstance(usage, dict):
        raise ValueError("Anthropic response requires usage")
    for field in ("input_tokens", "output_tokens"):
        if type(usage.get(field)) is not int or usage[field] < 0:
            raise ValueError("Anthropic response has invalid token usage")
    for field in ("cache_creation_input_tokens", "cache_read_input_tokens"):
        if field in usage and (type(usage[field]) is not int or usage[field] < 0):
            raise ValueError("Anthropic response has invalid cache token usage")
    cache = usage.get("cache_creation")
    if cache is not None:
        if not isinstance(cache, dict):
            raise ValueError("Anthropic response has invalid cache usage")
        for field in ("ephemeral_5m_input_tokens", "ephemeral_1h_input_tokens"):
            if field in cache and (type(cache[field]) is not int or cache[field] < 0):
                raise ValueError("Anthropic response has invalid cache token usage")


def transport_policy(config: LLMConfig) -> dict[str, str | int | None]:
    """Bind supported SDK retries and physical HTTP attempt recording."""
    recorded = config.provider in {"openai", "anthropic"}
    return {
        "provider": config.provider,
        "sdk_version": version(
            {"openai": "openai", "anthropic": "anthropic", "gemini": "google-genai"}[
                config.provider
            ]
        ),
        "pydantic_ai_version": version("pydantic-ai-slim"),
        "max_retries": config.transport_max_retries if recorded else None,
        "receipt_version": "sibyl-sdk-attempt-v2" if recorded else None,
    }
