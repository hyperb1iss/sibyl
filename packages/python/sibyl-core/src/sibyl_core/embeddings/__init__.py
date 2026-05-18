"""Embedding provider helpers."""

from sibyl_core.embeddings.gemini import (
    GeminiInputKind,
    build_gemini_contents,
    format_gemini_embedding_text,
    is_gemini_embedding_2,
)
from sibyl_core.embeddings.native import (
    CachedNativeEmbeddingProvider,
    DeterministicNativeEmbeddingProvider,
    GeminiNativeEmbeddingProvider,
    NativeEmbeddingInputKind,
    NativeEmbeddingMetadata,
    NativeEmbeddingProvider,
    NativeEmbeddingProviderName,
    OpenAINativeEmbeddingProvider,
    create_native_embedding_provider,
    native_embedding_cache_key,
    native_entity_embedding_text,
    native_relationship_embedding_text,
)

__all__ = [
    "CachedNativeEmbeddingProvider",
    "DeterministicNativeEmbeddingProvider",
    "GeminiInputKind",
    "GeminiNativeEmbeddingProvider",
    "NativeEmbeddingInputKind",
    "NativeEmbeddingMetadata",
    "NativeEmbeddingProvider",
    "NativeEmbeddingProviderName",
    "OpenAINativeEmbeddingProvider",
    "build_gemini_contents",
    "create_native_embedding_provider",
    "format_gemini_embedding_text",
    "is_gemini_embedding_2",
    "native_embedding_cache_key",
    "native_entity_embedding_text",
    "native_relationship_embedding_text",
]
