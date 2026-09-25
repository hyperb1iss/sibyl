"""Canonical configuration for content embeddings."""

import hashlib
import os
from dataclasses import dataclass

from sibyl_core.ai.bedrock import (
    DEFAULT_BEDROCK_EMBEDDING_MODEL,
    bedrock_region_configured,
    resolve_bedrock_settings,
)
from sibyl_core.config import settings
from sibyl_core.embeddings.providers import EmbeddingProviderName

_OPENAI_CONTENT_EMBEDDING_MODEL = "text-embedding-3-small"


@dataclass(frozen=True, slots=True)
class ContentEmbeddingConfig:
    """Resolved provider settings shared by every content embedding path.

    ``bedrock_identity`` fingerprints the Bedrock region, profile, scope and
    optional API key, or stays ``None`` when Bedrock has no region. Bedrock
    needs no API key, so it is ready whenever that identity exists.
    """

    provider: EmbeddingProviderName
    model: str
    dimensions: int
    api_key: str | None
    bedrock_identity: str | None = None

    @property
    def ready(self) -> bool:
        if self.provider == "bedrock":
            return self.bedrock_identity is not None
        return bool(self.api_key)

    @property
    def fingerprint(self) -> tuple[EmbeddingProviderName, str, int, str]:
        secret = self.api_key or ""
        secret_fingerprint = hashlib.sha256(secret.encode()).hexdigest() if secret else ""
        if self.provider == "bedrock":
            secret_fingerprint = self.bedrock_identity or ""
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
        bedrock_identity=_bedrock_identity() if provider == "bedrock" else None,
    )


def _configured_provider() -> EmbeddingProviderName:
    provider = (os.getenv("SIBYL_EMBEDDING_PROVIDER") or settings.embedding_provider).strip()
    if provider == "openai":
        return "openai"
    if provider == "gemini":
        return "gemini"
    if provider == "bedrock":
        return "bedrock"
    raise ValueError(f"unsupported content embedding provider: {provider}")


def _configured_model(provider: EmbeddingProviderName) -> str:
    model = os.getenv("SIBYL_EMBEDDING_MODEL", "").strip()
    if model:
        return model
    if provider == "gemini" and settings.embedding_model == _OPENAI_CONTENT_EMBEDDING_MODEL:
        return "gemini-embedding-2"
    if provider == "bedrock" and settings.embedding_model == _OPENAI_CONTENT_EMBEDDING_MODEL:
        return DEFAULT_BEDROCK_EMBEDDING_MODEL
    return settings.embedding_model


def _configured_api_key(provider: EmbeddingProviderName) -> str | None:
    if provider == "bedrock":
        return None
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


def _bedrock_identity() -> str | None:
    """``None`` without a region, like a missing key; other bad settings raise."""
    if not bedrock_region_configured():
        return None
    return resolve_bedrock_settings().fingerprint


__all__ = [
    "ContentEmbeddingConfig",
    "configured_content_embedding",
    "configured_content_embedding_dimensions",
]
