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


PROVIDER_ERROR_MESSAGE_LIMIT = 200


def provider_error_detail(exc: BaseException) -> dict[str, Any] | None:
    """Summarize a provider refusal so operators never need a live replay.

    Only the HTTP status and the provider's own ``error.type`` and
    ``error.message`` survive, with the message truncated. Request and prompt
    text, and every other body field, are deliberately excluded: these receipts
    are stored alongside transport rows that stay body-free because bodies can
    echo secrets. A failure the provider never answered falls back to the
    classified exception type, and to its message only when the failure is a
    timeout, whose message is the client's own fixed text. Returns ``None``
    when the exception carries nothing usable.
    """
    details = getattr(exc, "details", None)
    if not isinstance(details, dict):
        return None
    status_code = details.get("status_code")
    if isinstance(status_code, bool) or not isinstance(status_code, int):
        status_code = None
    error_type, message = _provider_error_body(details.get("body"))
    if status_code is None and error_type is None and message is None:
        error_type, message = _provider_error_cause(exc, details)
    if status_code is None and error_type is None and message is None:
        return None
    return {"status_code": status_code, "type": error_type, "message": message}


def _provider_error_cause(
    exc: BaseException, details: dict[str, Any]
) -> tuple[str | None, str | None]:
    """Name a failure the provider never answered, so a timeout is diagnosable.

    A read timeout has no HTTP status and no body, and without this the receipt
    said only that the request failed. The exception type is a class name and
    always safe. Its message is not: an unclassified model failure carries the
    response body in ``str(exc)``, so only a timeout's message is kept, and
    that one is the client's own fixed wording.
    """
    exception_type = details.get("exception_type")
    cause = details.get("cause") if isinstance(exc, LLMTimeoutError) else None
    return (
        exception_type if isinstance(exception_type, str) and exception_type else None,
        cause[:PROVIDER_ERROR_MESSAGE_LIMIT] or None if isinstance(cause, str) else None,
    )


def _provider_error_body(body: Any) -> tuple[str | None, str | None]:
    """Read only the provider's declared error type and message from a body."""
    if isinstance(body, str):
        return None, body[:PROVIDER_ERROR_MESSAGE_LIMIT] or None
    if not isinstance(body, dict):
        return None, None
    error = body.get("error")
    if isinstance(error, str):
        return None, error[:PROVIDER_ERROR_MESSAGE_LIMIT] or None
    if not isinstance(error, dict):
        return None, None
    error_type = error.get("type")
    message = error.get("message")
    return (
        error_type if isinstance(error_type, str) and error_type else None,
        message[:PROVIDER_ERROR_MESSAGE_LIMIT] or None if isinstance(message, str) else None,
    )


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
            details={"cause": str(exc), "exception_type": type(exc).__name__},
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
            details={"cause": str(exc), "exception_type": type(exc).__name__},
        )

    return LLMProviderError(
        "LLM provider request failed",
        provider=provider,
        model=model,
        surface=surface,
        details={"cause": str(exc), "exception_type": type(exc).__name__},
    )


#: The Anthropic and OpenAI SDKs wrap a read timeout in ``APITimeoutError``,
#: which subclasses their own connection error rather than ``TimeoutError`` or
#: ``httpx.TimeoutException``. Matching the class name keeps both SDKs out of
#: this module's imports and still recognizes their subclasses.
TIMEOUT_EXCEPTION_NAMES = frozenset({"APITimeoutError"})


def _is_timeout(exc: Exception) -> bool:
    if isinstance(exc, TimeoutError):
        return True
    if any(base.__name__ in TIMEOUT_EXCEPTION_NAMES for base in type(exc).__mro__):
        return True
    try:
        import httpx
    except ImportError:  # pragma: no cover - httpx is already a core dependency
        return False
    return isinstance(exc, httpx.TimeoutException)
