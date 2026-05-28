"""Embedding provider helpers."""

from importlib import import_module
from typing import Any

_EXPORTS = {
    "CachedEmbeddingProvider": (
        "sibyl_core.embeddings.providers",
        "CachedEmbeddingProvider",
    ),
    "DeterministicEmbeddingProvider": (
        "sibyl_core.embeddings.providers",
        "DeterministicEmbeddingProvider",
    ),
    "GeminiInputKind": ("sibyl_core.embeddings.gemini", "GeminiInputKind"),
    "GeminiEmbeddingProvider": (
        "sibyl_core.embeddings.providers",
        "GeminiEmbeddingProvider",
    ),
    "EmbeddingInputKind": ("sibyl_core.embeddings.providers", "EmbeddingInputKind"),
    "EmbeddingMetadata": ("sibyl_core.embeddings.providers", "EmbeddingMetadata"),
    "EmbeddingProvider": ("sibyl_core.embeddings.providers", "EmbeddingProvider"),
    "EmbeddingProviderName": (
        "sibyl_core.embeddings.providers",
        "EmbeddingProviderName",
    ),
    "OpenAIEmbeddingProvider": (
        "sibyl_core.embeddings.providers",
        "OpenAIEmbeddingProvider",
    ),
    "build_gemini_contents": ("sibyl_core.embeddings.gemini", "build_gemini_contents"),
    "create_embedding_provider": (
        "sibyl_core.embeddings.providers",
        "create_embedding_provider",
    ),
    "format_gemini_embedding_text": (
        "sibyl_core.embeddings.gemini",
        "format_gemini_embedding_text",
    ),
    "is_gemini_embedding_2": ("sibyl_core.embeddings.gemini", "is_gemini_embedding_2"),
    "embedding_cache_key": (
        "sibyl_core.embeddings.providers",
        "embedding_cache_key",
    ),
    "entity_embedding_text": (
        "sibyl_core.embeddings.providers",
        "entity_embedding_text",
    ),
    "relationship_embedding_text": (
        "sibyl_core.embeddings.providers",
        "relationship_embedding_text",
    ),
}

__all__ = sorted(_EXPORTS)


def __getattr__(name: str) -> Any:
    try:
        module_name, attr_name = _EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(name) from exc
    module = import_module(module_name)
    value = getattr(module, attr_name)
    globals()[name] = value
    return value
