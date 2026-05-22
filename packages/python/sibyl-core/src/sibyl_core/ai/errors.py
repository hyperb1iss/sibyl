"""Shared AI substrate exceptions."""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from sibyl_core.errors import SibylError


class AIError(SibylError):
    """Base exception for AI substrate failures."""


class LLMError(AIError):
    """Base exception for language model failures."""

    def __init__(
        self,
        message: str,
        *,
        provider: str | None = None,
        model: str | None = None,
        surface: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        merged_details: dict[str, Any] = details.copy() if details else {}
        if provider is not None:
            merged_details["provider"] = provider
        if model is not None:
            merged_details["model"] = model
        if surface is not None:
            merged_details["surface"] = surface
        super().__init__(message, details=merged_details)
        self.provider = provider
        self.model = model
        self.surface = surface


class LLMConfigError(LLMError):
    """Raised when LLM configuration cannot be resolved."""


class LLMValidationError(LLMError):
    """Raised when provider output cannot satisfy a requested schema."""


class LLMRateLimitError(LLMError):
    """Raised when a provider rate limit is hit."""


class LLMBudgetExceededError(LLMError):
    """Raised when a local LLM budget would be exceeded."""


class LLMProviderError(LLMError):
    """Raised when a provider rejects or fails a request."""


class LLMTimeoutError(LLMError):
    """Raised when a provider request times out."""


@lru_cache(maxsize=1)
def _pydantic_ai_error_types() -> tuple[type[Exception] | None, tuple[type[Exception], ...]]:
    try:
        from pydantic_ai.exceptions import (
            ModelHTTPError,
            ModelRetry,
            UnexpectedModelBehavior,
        )
    except ImportError:
        return None, ()
    return ModelHTTPError, (ModelRetry, UnexpectedModelBehavior)


def classify_llm_exception(
    exc: Exception,
    *,
    provider: str | None = None,
    model: str | None = None,
    surface: str | None = None,
) -> LLMError:
    """Map provider and PydanticAI failures into Sibyl's error taxonomy."""
    if isinstance(exc, LLMError):
        return exc

    if _is_timeout(exc):
        return LLMTimeoutError(
            "LLM provider request timed out",
            provider=provider,
            model=model,
            surface=surface,
            details={"cause": str(exc)},
        )

    model_http_error, validation_error_types = _pydantic_ai_error_types()
    if model_http_error is not None and isinstance(exc, model_http_error):
        status_code = getattr(exc, "status_code", None)
        body = getattr(exc, "body", None)
        model_name = getattr(exc, "model_name", None)
        details = {"status_code": status_code, "body": body}
        if status_code == 429:
            return LLMRateLimitError(
                "LLM provider rate limit exceeded",
                provider=provider,
                model=model_name or model,
                surface=surface,
                details=details,
            )
        return LLMProviderError(
            f"LLM provider request failed with HTTP {status_code}",
            provider=provider,
            model=model_name or model,
            surface=surface,
            details=details,
        )

    if validation_error_types and isinstance(exc, validation_error_types):
        return LLMValidationError(
            "LLM output could not be validated",
            provider=provider,
            model=model,
            surface=surface,
            details={"cause": str(exc)},
        )

    return LLMProviderError(
        "LLM provider request failed",
        provider=provider,
        model=model,
        surface=surface,
        details={"cause": str(exc), "exception_type": type(exc).__name__},
    )


def _is_timeout(exc: Exception) -> bool:
    if isinstance(exc, TimeoutError):
        return True
    try:
        import httpx
    except ImportError:  # pragma: no cover - httpx is already a core dependency
        return False
    return isinstance(exc, httpx.TimeoutException)
