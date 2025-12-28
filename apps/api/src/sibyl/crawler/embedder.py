"""Embedding generation for document chunks.

Supports multiple embedding providers with batching for efficiency.
Uses OpenAI's text-embedding-3-small by default (1536 dimensions).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from sibyl.config import settings

if TYPE_CHECKING:
    from sibyl.crawler.chunker import Chunk

log = structlog.get_logger()

# Type alias for embeddings
Embedding = list[float]


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
        self.model = model or settings.embedding_model
        self.dimensions = dimensions or settings.embedding_dimensions
        self.batch_size = batch_size
        self._client: object | None = None

    async def _get_client(self) -> object:
        """Lazily initialize the OpenAI client."""
        if self._client is None:
            from openai import AsyncOpenAI

            api_key = settings.openai_api_key.get_secret_value()
            if not api_key:
                raise ValueError("OpenAI API key not configured (SIBYL_OPENAI_API_KEY)")

            self._client = AsyncOpenAI(api_key=api_key)

        return self._client

    async def embed_text(self, text: str) -> Embedding:
        """Generate embedding for a single text.

        Args:
            text: Text to embed

        Returns:
            Embedding vector
        """
        client = await self._get_client()

        # Include context in embedding if present
        response = await client.embeddings.create(  # type: ignore[union-attr]
            model=self.model,
            input=text,
            dimensions=self.dimensions,
        )

        return response.data[0].embedding

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

        client = await self._get_client()
        embeddings: list[Embedding] = []

        # Process in batches
        for i in range(0, len(texts), self.batch_size):
            batch = texts[i : i + self.batch_size]

            response = await client.embeddings.create(  # type: ignore[union-attr]
                model=self.model,
                input=batch,
                dimensions=self.dimensions,
            )

            # Ensure correct ordering
            batch_embeddings = sorted(response.data, key=lambda x: x.index)
            embeddings.extend([e.embedding for e in batch_embeddings])

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
        # Build text for each chunk, including context if available
        texts = []
        for chunk in chunks:
            if chunk.context:
                # Prepend context for better retrieval
                text = f"{chunk.context}\n\n{chunk.content}"
            else:
                text = chunk.content
            texts.append(text)

        return await self.embed_texts(texts)


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
