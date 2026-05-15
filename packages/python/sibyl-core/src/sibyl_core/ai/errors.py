"""Shared AI substrate exceptions."""

from __future__ import annotations

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


class LLMProviderError(LLMError):
    """Raised when a provider rejects or fails a request."""


class LLMTimeoutError(LLMError):
    """Raised when a provider request times out."""
