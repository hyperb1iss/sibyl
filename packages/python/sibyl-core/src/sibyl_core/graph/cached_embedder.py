"""Cached embedder wrapper to avoid redundant OpenAI API calls.

The embedding API is a major bottleneck - every search and entity creation
calls OpenAI's embedding endpoint. This wrapper adds an LRU cache to avoid
repeated embeddings for the same text.

Cache Strategy:
- In-memory LRU cache (simple, effective for single process)
- Key: normalized text (stripped, lowercased for better hit rate)
- TTL: None (embeddings are deterministic, same text = same embedding)
- Size: 1000 entries (~6MB for 1536-dim embeddings)
"""

from __future__ import annotations

import asyncio
import hashlib
from collections import OrderedDict
from collections.abc import Iterable
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from graphiti_core.embedder.client import EmbedderClient

log = structlog.get_logger()

# Cache statistics for monitoring
_stats = {"hits": 0, "misses": 0, "evictions": 0}


def get_cache_stats() -> dict[str, int]:
    """Get current cache statistics."""
    return _stats.copy()


def reset_cache_stats() -> None:
    """Reset cache statistics (useful for testing)."""
    global _stats
    _stats = {"hits": 0, "misses": 0, "evictions": 0}


class CachedEmbedder:
    """LRU-cached wrapper around any EmbedderClient.

    Caches embeddings by normalized text to avoid repeated API calls.
    Thread-safe for async operations via asyncio.Lock.

    Example:
        >>> original_embedder = OpenAIEmbedder()
        >>> cached = CachedEmbedder(original_embedder, max_size=1000)
        >>> embedding = await cached.create("hello world")  # API call
        >>> embedding = await cached.create("hello world")  # Cache hit!
    """

    def __init__(
        self,
        embedder: EmbedderClient,
        max_size: int = 1000,
    ) -> None:
        """Initialize the cached embedder.

        Args:
            embedder: The underlying embedder to wrap.
            max_size: Maximum number of cached embeddings (default 1000).
                      At 1536 dimensions, 1000 entries ≈ 6MB memory.
        """
        self._embedder = embedder
        self._max_size = max_size
        self._cache: OrderedDict[str, list[float]] = OrderedDict()
        self._lock = asyncio.Lock()
        self._pending: dict[str, asyncio.Future[list[float]]] = {}

    def _normalize_key(self, text: str) -> str:
        """Normalize text for cache key.

        Uses hash for consistent key length and to avoid memory bloat
        from storing long texts as keys.
        """
        # Normalize: strip whitespace, lowercase for better hit rate
        normalized = text.strip().lower()
        # Hash for consistent key length
        return hashlib.sha256(normalized.encode()).hexdigest()[:32]

    async def create(
        self,
        input_data: str | list[str] | Iterable[int] | Iterable[Iterable[int]],
    ) -> list[float]:
        """Create embedding with caching.

        For string inputs, checks cache first. Cache misses call the
        underlying embedder and store the result.

        Args:
            input_data: Text to embed (string) or pre-tokenized input.

        Returns:
            Embedding vector as list of floats.
        """
        # Only cache string inputs (most common case)
        if not isinstance(input_data, str):
            # For list/tokenized inputs, pass through to underlying embedder
            if isinstance(input_data, list) and input_data and isinstance(input_data[0], str):
                # List of strings - just embed the first one (Graphiti pattern)
                return await self.create(input_data[0])
            return await self._embedder.create(input_data)

        cache_key = self._normalize_key(input_data)

        # Check cache and pending requests under lock
        pending_future: asyncio.Future[list[float]] | None = None
        async with self._lock:
            # Check cache first
            if cache_key in self._cache:
                _stats["hits"] += 1
                self._cache.move_to_end(cache_key)
                return self._cache[cache_key]

            # Check if another task is already fetching this embedding
            if cache_key in self._pending:
                pending_future = self._pending[cache_key]

        # Wait for pending request outside the lock
        if pending_future is not None:
            return await pending_future

        # We need to fetch - create a future for deduplication
        future: asyncio.Future[list[float]] = asyncio.get_event_loop().create_future()

        async with self._lock:
            # Double-check no one else started while we were waiting
            if cache_key in self._cache:
                _stats["hits"] += 1
                self._cache.move_to_end(cache_key)
                return self._cache[cache_key]
            if cache_key in self._pending:
                # Someone else started, wait for them
                return await self._pending[cache_key]
            # Register our future
            self._pending[cache_key] = future

        try:
            # Cache miss - call underlying embedder
            _stats["misses"] += 1
            embedding = await self._embedder.create(input_data)

            # Store in cache
            async with self._lock:
                self._cache[cache_key] = embedding
                self._cache.move_to_end(cache_key)

                # Evict oldest if over capacity
                while len(self._cache) > self._max_size:
                    self._cache.popitem(last=False)
                    _stats["evictions"] += 1

                # Clean up pending
                self._pending.pop(cache_key, None)

            # Resolve the future for any waiting tasks
            future.set_result(embedding)
            return embedding

        except Exception as e:
            # Clean up and propagate error
            async with self._lock:
                self._pending.pop(cache_key, None)
            future.set_exception(e)
            raise

    async def create_batch(self, input_data_list: list[str]) -> list[list[float]]:
        """Create embeddings for a batch of texts.

        Checks cache for each input, only fetches uncached ones from API.
        This is more efficient than individual create() calls when some
        items might be cached.

        Args:
            input_data_list: List of texts to embed.

        Returns:
            List of embedding vectors, one per input.
        """
        results: list[list[float] | None] = [None] * len(input_data_list)
        uncached_indices: list[int] = []
        uncached_texts: list[str] = []

        # Check cache for each input
        async with self._lock:
            for i, text in enumerate(input_data_list):
                cache_key = self._normalize_key(text)
                if cache_key in self._cache:
                    _stats["hits"] += 1
                    self._cache.move_to_end(cache_key)
                    results[i] = self._cache[cache_key]
                else:
                    uncached_indices.append(i)
                    uncached_texts.append(text)

        # Fetch uncached embeddings
        if uncached_texts:
            _stats["misses"] += len(uncached_texts)

            # Check if underlying embedder supports batch
            try:
                new_embeddings = await self._embedder.create_batch(uncached_texts)
            except NotImplementedError:
                # Fall back to sequential calls
                new_embeddings = [await self._embedder.create(t) for t in uncached_texts]

            # Store in cache and results
            async with self._lock:
                for idx, text, embedding in zip(
                    uncached_indices, uncached_texts, new_embeddings, strict=True
                ):
                    cache_key = self._normalize_key(text)
                    self._cache[cache_key] = embedding
                    self._cache.move_to_end(cache_key)
                    results[idx] = embedding

                # Evict oldest if over capacity
                while len(self._cache) > self._max_size:
                    self._cache.popitem(last=False)
                    _stats["evictions"] += 1

        return results  # type: ignore[return-value]

    def cache_size(self) -> int:
        """Get current number of cached embeddings."""
        return len(self._cache)

    def clear_cache(self) -> None:
        """Clear the embedding cache."""
        self._cache.clear()
        log.info("Embedding cache cleared")


def wrap_embedder_with_cache(
    embedder: EmbedderClient,
    max_size: int = 1000,
) -> CachedEmbedder:
    """Wrap an embedder with caching.

    Convenience function to create a cached embedder.

    Args:
        embedder: The underlying embedder to wrap.
        max_size: Maximum cache size.

    Returns:
        CachedEmbedder instance wrapping the original.
    """
    log.info("Wrapping embedder with LRU cache", max_size=max_size)
    return CachedEmbedder(embedder, max_size=max_size)
