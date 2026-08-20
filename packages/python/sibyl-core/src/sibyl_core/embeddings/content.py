"""Canonical configuration for content embeddings."""

import hashlib
import os
from dataclasses import dataclass

from sibyl_core.config import settings
from sibyl_core.embeddings.providers import EmbeddingProviderName

_OPENAI_CONTENT_EMBEDDING_MODEL = "text-embedding-3-small"


@dataclass(frozen=True, slots=True)
class ContentEmbeddingConfig:
    """Resolved provider settings shared by every content embedding path."""

    provider: EmbeddingProviderName
    model: str
    dimensions: int
    api_key: str | None

    @property
    def fingerprint(self) -> tuple[EmbeddingProviderName, str, int, str]:
        secret = self.api_key or ""
        secret_fingerprint = hashlib.sha256(secret.encode()).hexdigest() if secret else ""
        return (self.provider, self.model, self.dimensions, secret_fingerprint)


def configured_content_embedding_dimensions() -> int:
    """Resolve the vector size without requiring a provider or credential."""
    raw_dimensions = os.getenv("SIBYL_EMBEDDING_DIMENSIONS", "").strip()
    if raw_dimensions:
        return int(raw_dimensions)
    return settings.embedding_dimensions


def configured_content_embedding() -> ContentEmbeddingConfig:
    """Resolve content embedding settings from the single supported contract."""
    provider = _configured_provider()
    return ContentEmbeddingConfig(
        provider=provider,
        model=_configured_model(provider),
        dimensions=configured_content_embedding_dimensions(),
        api_key=_configured_api_key(provider),
    )


def _configured_provider() -> EmbeddingProviderName:
    provider = (os.getenv("SIBYL_EMBEDDING_PROVIDER") or settings.embedding_provider).strip()
    if provider == "openai":
        return "openai"
    if provider == "gemini":
        return "gemini"
    raise ValueError(f"unsupported content embedding provider: {provider}")


def _configured_model(provider: EmbeddingProviderName) -> str:
    model = os.getenv("SIBYL_EMBEDDING_MODEL", "").strip()
    if model:
        return model
    if provider == "gemini" and settings.embedding_model == _OPENAI_CONTENT_EMBEDDING_MODEL:
        return "gemini-embedding-2"
    return settings.embedding_model


def _configured_api_key(provider: EmbeddingProviderName) -> str | None:
    if provider == "gemini":
        return (
            os.getenv("SIBYL_GEMINI_API_KEY", "")
            or os.getenv("GEMINI_API_KEY", "")
            or os.getenv("GOOGLE_API_KEY", "")
            or settings.gemini_api_key.get_secret_value()
            or None
        )
    return (
        os.getenv("SIBYL_OPENAI_API_KEY", "")
        or os.getenv("OPENAI_API_KEY", "")
        or settings.openai_api_key.get_secret_value()
        or None
    )


__all__ = [
    "ContentEmbeddingConfig",
    "configured_content_embedding",
    "configured_content_embedding_dimensions",
]
