"""Embedding generation for document chunks.

Supports multiple embedding providers with batching for efficiency.
Uses OpenAI's text-embedding-3-small by default (1536 dimensions).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, cast

import structlog
from google import genai
from google.genai import types

from sibyl.config import settings
from sibyl.services.settings import get_settings_service
from sibyl_core.ai.bedrock import DEFAULT_BEDROCK_EMBEDDING_MODEL
from sibyl_core.embeddings.gemini import (
    build_gemini_contents,
    format_gemini_embedding_text,
)
from sibyl_core.embeddings.provenance import document_chunk_embedding_metadata

if TYPE_CHECKING:
    from sibyl.crawler.chunker import Chunk

log = structlog.get_logger()

# Type alias for embeddings
Embedding = list[float]
EmbeddingProvider = Literal["openai", "gemini", "bedrock"]
EmbeddingInputKind = Literal["query", "document"]


@dataclass(frozen=True)
class ResolvedEmbeddingConfig:
    provider: EmbeddingProvider
    model: str
    dimensions: int

    def chunk_metadata(self) -> dict[str, str | int]:
        """The provenance stamped beside a chunk vector this config produced."""
        return document_chunk_embedding_metadata(
            provider=self.provider, model=self.model, dimensions=self.dimensions
        )


class EmbeddingService:
    """Service for generating embeddings from text.

    Uses OpenAI's embedding API with batching for efficiency.
    Supports configurable models and dimensions.
    """

    def __init__(
        self,
        *,
        model: str | None = None,
        dimensions: int | None = None,
        batch_size: int = 100,
    ) -> None:
        """Initialize the embedding service.

        Args:
            model: Embedding model name (default from settings)
            dimensions: Embedding dimensions (default from settings)
            batch_size: Number of texts to embed in parallel
        """
        self.model = model
        self.dimensions = dimensions
        self.batch_size = batch_size
        self._client: object | None = None
        self._client_provider: EmbeddingProvider | None = None
        self._client_identity: tuple[object, ...] | None = None

    async def _resolve_config(self) -> ResolvedEmbeddingConfig:
        service = get_settings_service()
        raw_provider = await service.get("embedding_provider")
        provider = self._normalize_provider(raw_provider or settings.embedding_provider)

        raw_model = self.model or await service.get("embedding_model")
        model = raw_model or self._default_model(provider)

        raw_dimensions = self.dimensions or await service.get("embedding_dimensions")
        dimensions = int(raw_dimensions or settings.embedding_dimensions)

        return ResolvedEmbeddingConfig(
            provider=provider,
            model=model,
            dimensions=dimensions,
        )

    @staticmethod
    def _normalize_provider(provider: str) -> EmbeddingProvider:
        if provider == "openai":
            return "openai"
        if provider == "gemini":
            return "gemini"
        if provider == "bedrock":
            return "bedrock"
        raise ValueError(f"Unsupported embedding provider: {provider}")

    @staticmethod
    def _default_model(provider: EmbeddingProvider) -> str:
        if provider == "gemini":
            return "gemini-embedding-2"
        if provider == "bedrock":
            return DEFAULT_BEDROCK_EMBEDDING_MODEL
        return settings.embedding_model

    async def _get_client(self, config: ResolvedEmbeddingConfig) -> object:
        """Lazily initialize the configured provider client."""
        if config.provider == "bedrock":
            return self._bedrock_provider(config)
        if self._client is None or self._client_provider != config.provider:
            service = get_settings_service()
            self._client_provider = config.provider
            self._client_identity = None

            if config.provider == "gemini":
                api_key = await service.get_gemini_key()
                if not api_key:
                    raise ValueError(
                        "Gemini API key not configured (set via UI or SIBYL_GEMINI_API_KEY)"
                    )

                self._client = genai.Client(api_key=api_key)
                return self._client

            from openai import AsyncOpenAI

            api_key = await service.get_openai_key()
            if not api_key:
                raise ValueError(
                    "OpenAI API key not configured (set via UI or SIBYL_OPENAI_API_KEY)"
                )

            self._client = AsyncOpenAI(api_key=api_key)

        return self._client

    def _batch_size(self, config: ResolvedEmbeddingConfig, total: int) -> int:
        """Bedrock batches to Cohere's limits and runs them concurrently itself."""
        return max(total, 1) if config.provider == "bedrock" else self.batch_size

    def _bedrock_provider(self, config: ResolvedEmbeddingConfig) -> Any:
        """Cohere on Bedrock, rebuilt when the model, size or AWS settings change."""
        from sibyl_core.ai.bedrock import resolve_bedrock_settings
        from sibyl_core.embeddings.bedrock import BedrockEmbeddingProvider
        from sibyl_core.embeddings.providers import EmbeddingMetadata

        bedrock = resolve_bedrock_settings()
        identity = (config.model, config.dimensions, bedrock.fingerprint)
        if (
            self._client is None
            or self._client_provider != "bedrock"
            or self._client_identity != identity
        ):
            self._client = BedrockEmbeddingProvider(
                metadata=EmbeddingMetadata(
                    provider="bedrock",
                    model=config.model,
                    dimensions=config.dimensions,
                    cache_namespace="document",
                    tokenizer_estimate_method="provider-default",
                ),
                settings=bedrock,
            )
            self._client_provider = "bedrock"
            self._client_identity = identity
        return self._client

    async def embed_text(self, text: str) -> Embedding:
        """Generate embedding for a single text.

        Args:
            text: Text to embed

        Returns:
            Embedding vector
        """
        embedding, _metadata = await self.embed_query_with_metadata(text)
        return embedding

    async def embed_query_with_metadata(self, text: str) -> tuple[Embedding, dict[str, str | int]]:
        """Embed a search query and name the chunk vector space it can be scored against."""
        config = await self._resolve_config()
        embedding = (await self._embed_texts_with_config([text], config, kind="query"))[0]
        return embedding, config.chunk_metadata()

    async def chunk_embedding_metadata(self) -> tuple[dict[str, str | int], bool]:
        """The stamp chunk writes carry today, and whether this service can embed.

        The stamp comes from the configured values even when no client can be
        built for them (a missing credential, or a provider this service does
        not speak), because the embedding sweep's one-time verdict on legacy
        chunk vectors and the deployment's model record compare against the
        configured model whether or not this process can embed.
        """
        try:
            config = await self._resolve_config()
        except ValueError:
            # A provider this service does not speak: stamp the configured values.
            service = get_settings_service()
            provider = str(await service.get("embedding_provider") or settings.embedding_provider)
            model = self.model or await service.get("embedding_model") or settings.embedding_model
            dimensions = self.dimensions or await service.get("embedding_dimensions")
            return (
                document_chunk_embedding_metadata(
                    provider=provider.strip(),
                    model=str(model),
                    dimensions=int(dimensions or settings.embedding_dimensions),
                ),
                False,
            )
        try:
            await self._get_client(config)
        except ValueError:
            return config.chunk_metadata(), False
        return config.chunk_metadata(), True

    async def _embed_texts_with_config(
        self,
        texts: list[str],
        config: ResolvedEmbeddingConfig,
        *,
        kind: EmbeddingInputKind,
        titles: list[str | None] | None = None,
    ) -> list[Embedding]:
        client = await self._get_client(config)

        if config.provider == "bedrock":
            return await cast("Any", client).embed_texts(texts, input_kind=kind)

        if config.provider == "gemini":
            gemini_client = cast("Any", client)
            formatted = [
                format_gemini_embedding_text(
                    text,
                    model=config.model,
                    kind=kind,
                    title=titles[index] if titles else None,
                )
                for index, text in enumerate(texts)
            ]
            response = await gemini_client.aio.models.embed_content(
                model=config.model,
                contents=build_gemini_contents(formatted),
                config=types.EmbedContentConfig(output_dimensionality=config.dimensions),
            )
            if not response.embeddings:
                raise ValueError("No embeddings returned from Gemini API")
            embeddings = []
            for embedding in response.embeddings:
                if not embedding.values:
                    raise ValueError("Empty embedding returned from Gemini API")
                embeddings.append(embedding.values)
            return embeddings

        openai_client = cast("Any", client)
        response = await openai_client.embeddings.create(
            model=config.model,
            input=texts if len(texts) > 1 else texts[0],
            dimensions=config.dimensions,
        )
        batch_embeddings = sorted(response.data, key=lambda x: x.index)
        return [e.embedding for e in batch_embeddings]

    async def embed_texts(self, texts: list[str]) -> list[Embedding]:
        """Generate embeddings for multiple texts.

        Batches requests for efficiency while respecting API limits.

        Args:
            texts: List of texts to embed

        Returns:
            List of embedding vectors (same order as input)
        """
        if not texts:
            return []

        config = await self._resolve_config()
        embeddings: list[Embedding] = []
        batch_size = self._batch_size(config, len(texts))

        # Process in batches
        for i in range(0, len(texts), batch_size):
            batch = texts[i : i + batch_size]

            embeddings.extend(await self._embed_texts_with_config(batch, config, kind="document"))

            log.debug(
                "Embedded batch",
                batch_size=len(batch),
                total_processed=len(embeddings),
                total_remaining=len(texts) - len(embeddings),
            )

        return embeddings

    async def embed_chunks(self, chunks: list[Chunk]) -> list[Embedding]:
        """Generate embeddings for document chunks.

        Uses contextual content if available (Anthropic technique).

        Args:
            chunks: List of chunks to embed

        Returns:
            List of embedding vectors
        """
        embeddings, _metadata = await self.embed_chunks_with_metadata(chunks)
        return embeddings

    async def embed_chunks_with_metadata(
        self, chunks: list[Chunk]
    ) -> tuple[list[Embedding], dict[str, str | int]]:
        """Embed chunks and return the provenance their vectors must be stored with.

        The configuration is resolved once, so the stamp always names the
        model that produced these vectors even if settings change mid-call.
        """
        # Build text for each chunk, including context if available
        texts = []
        titles: list[str | None] = []
        for chunk in chunks:
            if chunk.context:
                # Prepend context for better retrieval
                text = f"{chunk.context}\n\n{chunk.content}"
            else:
                text = chunk.content
            texts.append(text)
            titles.append(" / ".join(chunk.heading_path) or None)

        config = await self._resolve_config()
        if not texts:
            return [], config.chunk_metadata()

        embeddings: list[Embedding] = []
        batch_size = self._batch_size(config, len(texts))
        for i in range(0, len(texts), batch_size):
            batch = texts[i : i + batch_size]
            title_batch = titles[i : i + batch_size]
            embeddings.extend(
                await self._embed_texts_with_config(
                    batch,
                    config,
                    kind="document",
                    titles=title_batch,
                )
            )

        return embeddings, config.chunk_metadata()


# Module-level service instance (lazy initialization)
_embedding_service: EmbeddingService | None = None


def get_embedding_service() -> EmbeddingService:
    """Get the global embedding service instance."""
    global _embedding_service  # noqa: PLW0603
    if _embedding_service is None:
        _embedding_service = EmbeddingService()
    return _embedding_service


async def embed_chunks(chunks: list[Chunk]) -> list[Embedding]:
    """Convenience function to embed chunks.

    Args:
        chunks: Chunks to embed

    Returns:
        List of embedding vectors
    """
    service = get_embedding_service()
    return await service.embed_chunks(chunks)


async def embed_text(text: str) -> Embedding:
    """Convenience function to embed a single text.

    Args:
        text: Text to embed

    Returns:
        Embedding vector
    """
    service = get_embedding_service()
    return await service.embed_text(text)


async def embed_query_with_metadata(text: str) -> tuple[Embedding, dict[str, str | int]]:
    """Embed a chunk search query and name the vector space it belongs to."""
    return await get_embedding_service().embed_query_with_metadata(text)
