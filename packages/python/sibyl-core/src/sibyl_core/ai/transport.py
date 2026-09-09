"""Safe, context-local receipts for SDK HTTP attempts during extraction."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from importlib.metadata import version
from typing import Any

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


@contextmanager
def collect_transport_attempts() -> Iterator[list[TransportAttempt]]:
    attempts: list[TransportAttempt] = []
    token = _attempts.set(attempts)
    try:
        yield attempts
    finally:
        _attempts.reset(token)


class RecordingOpenAIClient(DefaultAsyncHttpxClient):
    async def send(self, request: Any, **kwargs: Any) -> Any:
        attempts = _attempts.get()
        try:
            response = await super().send(request, **kwargs)
        except (Exception, asyncio.CancelledError) as exc:
            if attempts is not None:
                # Exception messages and request/response bodies may contain secrets.
                attempts.append(TransportAttempt(exception_type=type(exc).__name__))
            raise
        if attempts is not None:
            request_id = response.headers.get("x-request-id")
            if (
                request_id is not None
                and re.fullmatch(r"[a-zA-Z0-9_.:-]{1,200}", request_id) is None
            ):
                request_id = None
            attempts.append(
                TransportAttempt(status_code=response.status_code, request_id=request_id)
            )
        return response


def transport_policy(config: LLMConfig) -> dict[str, str | int | None]:
    """Bind the supported SDK behavior; other providers remain uninstrumented."""
    return {
        "provider": config.provider,
        "sdk_version": version(
            {"openai": "openai", "anthropic": "anthropic", "gemini": "google-genai"}[
                config.provider
            ]
        ),
        "pydantic_ai_version": version("pydantic-ai-slim"),
        "max_retries": config.transport_max_retries if config.provider == "openai" else None,
        "receipt_version": "sibyl-sdk-attempt-v1" if config.provider == "openai" else None,
    }
