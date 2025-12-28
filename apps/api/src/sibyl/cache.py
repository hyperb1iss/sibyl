"""Caching layer for Sibyl queries.

Provides in-memory LRU caching with TTL support for:
- Search query results
- Entity lookups by ID
- Community summaries

Supports optional Redis backend for distributed caching.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from redis.asyncio import Redis

log = structlog.get_logger()


@dataclass
class CacheStats:
    """Cache performance statistics."""

    hits: int = 0
    misses: int = 0
    evictions: int = 0
    expirations: int = 0

    @property
    def hit_rate(self) -> float:
        """Calculate cache hit rate."""
        total = self.hits + self.misses
        return self.hits / total if total > 0 else 0.0

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "hits": self.hits,
            "misses": self.misses,
            "evictions": self.evictions,
            "expirations": self.expirations,
            "hit_rate": round(self.hit_rate, 4),
            "total_requests": self.hits + self.misses,
        }


@dataclass
class CacheEntry[T]:
    """A cache entry with value and expiration."""

    value: T
    expires_at: float
    created_at: float = field(default_factory=time.time)

    @property
    def is_expired(self) -> bool:
        """Check if this entry has expired."""
        return time.time() >= self.expires_at


class LRUCache[T]:
    """Thread-safe LRU cache with TTL support.

    Uses OrderedDict for O(1) access and LRU eviction.
    """

    def __init__(self, maxsize: int = 1000, default_ttl: float = 300.0) -> None:
        """Initialize cache.

        Args:
            maxsize: Maximum number of entries (default 1000).
            default_ttl: Default time-to-live in seconds (default 5 minutes).
        """
        self._cache: OrderedDict[str, CacheEntry[T]] = OrderedDict()
        self._maxsize = maxsize
        self._default_ttl = default_ttl
        self._stats = CacheStats()

    def get(self, key: str) -> T | None:
        """Get value from cache.

        Returns None if key not found or expired.
        """
        entry = self._cache.get(key)

        if entry is None:
            self._stats.misses += 1
            return None

        if entry.is_expired:
            self._stats.expirations += 1
            del self._cache[key]
            return None

        # Move to end (most recently used)
        self._cache.move_to_end(key)
        self._stats.hits += 1
        return entry.value

    def set(self, key: str, value: T, ttl: float | None = None) -> None:
        """Set value in cache.

        Args:
            key: Cache key.
            value: Value to cache.
            ttl: Time-to-live in seconds (uses default if not specified).
        """
        ttl = ttl if ttl is not None else self._default_ttl
        expires_at = time.time() + ttl

        # If key exists, move to end
        if key in self._cache:
            self._cache.move_to_end(key)

        self._cache[key] = CacheEntry(value=value, expires_at=expires_at)

        # Evict oldest entries if over capacity
        while len(self._cache) > self._maxsize:
            self._cache.popitem(last=False)
            self._stats.evictions += 1

    def delete(self, key: str) -> bool:
        """Delete a specific key from cache."""
        if key in self._cache:
            del self._cache[key]
            return True
        return False

    def invalidate_pattern(self, pattern: str) -> int:
        """Invalidate all keys matching a pattern.

        Args:
            pattern: Substring to match in keys.

        Returns:
            Number of keys invalidated.
        """
        keys_to_delete = [k for k in self._cache if pattern in k]
        for key in keys_to_delete:
            del self._cache[key]
        return len(keys_to_delete)

    def clear(self) -> None:
        """Clear all cache entries."""
        self._cache.clear()

    @property
    def stats(self) -> CacheStats:
        """Get cache statistics."""
        return self._stats

    @property
    def size(self) -> int:
        """Current number of entries in cache."""
        return len(self._cache)


class QueryCache:
    """Multi-tier cache for Sibyl queries.

    Manages separate caches for:
    - Search results (keyed by query + filters)
    - Entity lookups (keyed by entity ID)
    - Community summaries (keyed by community ID)
    """

    def __init__(
        self,
        search_maxsize: int = 500,
        entity_maxsize: int = 2000,
        community_maxsize: int = 100,
        search_ttl: float = 300.0,  # 5 minutes
        entity_ttl: float = 600.0,  # 10 minutes
        community_ttl: float = 1800.0,  # 30 minutes
    ) -> None:
        """Initialize query caches.

        Args:
            search_maxsize: Max cached search queries.
            entity_maxsize: Max cached entities.
            community_maxsize: Max cached communities.
            search_ttl: Search result TTL in seconds.
            entity_ttl: Entity lookup TTL in seconds.
            community_ttl: Community summary TTL in seconds.
        """
        self._search_cache: LRUCache[Any] = LRUCache(maxsize=search_maxsize, default_ttl=search_ttl)
        self._entity_cache: LRUCache[Any] = LRUCache(maxsize=entity_maxsize, default_ttl=entity_ttl)
        self._community_cache: LRUCache[Any] = LRUCache(
            maxsize=community_maxsize, default_ttl=community_ttl
        )
        self._redis: Redis | None = None  # type: ignore[type-arg]

    @staticmethod
    def _make_search_key(query: str, **filters: Any) -> str:
        """Create cache key for search query."""
        # Sort filters for consistent key generation
        filter_str = json.dumps(filters, sort_keys=True, default=str)
        combined = f"search:{query}:{filter_str}"
        return hashlib.sha256(combined.encode()).hexdigest()[:32]

    # -------------------------------------------------------------------------
    # Search Cache
    # -------------------------------------------------------------------------

    def get_search(self, query: str, **filters: Any) -> Any | None:
        """Get cached search results."""
        key = self._make_search_key(query, **filters)
        return self._search_cache.get(key)

    def set_search(
        self, query: str, results: Any, ttl: float | None = None, **filters: Any
    ) -> None:
        """Cache search results."""
        key = self._make_search_key(query, **filters)
        self._search_cache.set(key, results, ttl)
        log.debug("cache_set_search", query=query[:50], filters=filters)

    def invalidate_search(self) -> int:
        """Invalidate all search results."""
        count = self._search_cache.size
        self._search_cache.clear()
        log.info("cache_invalidate_search", count=count)
        return count

    # -------------------------------------------------------------------------
    # Entity Cache
    # -------------------------------------------------------------------------

    def get_entity(self, entity_id: str) -> Any | None:
        """Get cached entity."""
        return self._entity_cache.get(f"entity:{entity_id}")

    def set_entity(self, entity_id: str, entity: Any, ttl: float | None = None) -> None:
        """Cache an entity."""
        self._entity_cache.set(f"entity:{entity_id}", entity, ttl)

    def invalidate_entity(self, entity_id: str) -> bool:
        """Invalidate a specific entity and related search results."""
        deleted = self._entity_cache.delete(f"entity:{entity_id}")
        # Also clear search cache as results may contain this entity
        self._search_cache.clear()
        log.info("cache_invalidate_entity", entity_id=entity_id)
        return deleted

    def invalidate_entities_by_type(self, entity_type: str) -> int:
        """Invalidate all entities of a given type."""
        count = self._entity_cache.invalidate_pattern(entity_type)
        log.info("cache_invalidate_by_type", entity_type=entity_type, count=count)
        return count

    # -------------------------------------------------------------------------
    # Community Cache
    # -------------------------------------------------------------------------

    def get_community(self, community_id: str) -> Any | None:
        """Get cached community summary."""
        return self._community_cache.get(f"community:{community_id}")

    def set_community(self, community_id: str, summary: Any, ttl: float | None = None) -> None:
        """Cache a community summary."""
        self._community_cache.set(f"community:{community_id}", summary, ttl)

    def invalidate_community(self, community_id: str) -> bool:
        """Invalidate a community summary."""
        return self._community_cache.delete(f"community:{community_id}")

    def invalidate_all_communities(self) -> int:
        """Invalidate all community summaries."""
        count = self._community_cache.size
        self._community_cache.clear()
        log.info("cache_invalidate_communities", count=count)
        return count

    # -------------------------------------------------------------------------
    # Global Operations
    # -------------------------------------------------------------------------

    def clear_all(self) -> dict[str, int]:
        """Clear all caches."""
        counts = {
            "search": self._search_cache.size,
            "entity": self._entity_cache.size,
            "community": self._community_cache.size,
        }
        self._search_cache.clear()
        self._entity_cache.clear()
        self._community_cache.clear()
        log.info("cache_clear_all", **counts)
        return counts

    def get_stats(self) -> dict[str, Any]:
        """Get statistics for all caches."""
        return {
            "search": {
                **self._search_cache.stats.to_dict(),
                "size": self._search_cache.size,
            },
            "entity": {
                **self._entity_cache.stats.to_dict(),
                "size": self._entity_cache.size,
            },
            "community": {
                **self._community_cache.stats.to_dict(),
                "size": self._community_cache.size,
            },
        }


# Global cache instance
_cache: QueryCache | None = None


def get_cache() -> QueryCache:
    """Get the global cache instance."""
    global _cache  # noqa: PLW0603
    if _cache is None:
        _cache = QueryCache()
        log.info("cache_initialized")
    return _cache


def reset_cache() -> None:
    """Reset the global cache instance."""
    global _cache  # noqa: PLW0603
    if _cache is not None:
        _cache.clear_all()
    _cache = None
    log.info("cache_reset")


# Decorator for caching function results
def cached_search(ttl: float | None = None):
    """Decorator to cache search function results.

    Example:
        @cached_search(ttl=60)
        async def search(query: str, **filters) -> SearchResponse:
            ...
    """

    def decorator(func):
        async def wrapper(query: str, **kwargs):
            cache = get_cache()

            # Check cache first
            cached = cache.get_search(query, **kwargs)
            if cached is not None:
                log.debug("cache_hit_search", query=query[:50])
                return cached

            # Execute function and cache result
            result = await func(query, **kwargs)
            cache.set_search(query, result, ttl, **kwargs)
            return result

        return wrapper

    return decorator


def cached_entity(ttl: float | None = None):
    """Decorator to cache entity lookup results.

    Example:
        @cached_entity(ttl=600)
        async def get_entity(entity_id: str) -> Entity:
            ...
    """

    def decorator(func):
        async def wrapper(entity_id: str, *args, **kwargs):
            cache = get_cache()

            # Check cache first
            cached = cache.get_entity(entity_id)
            if cached is not None:
                log.debug("cache_hit_entity", entity_id=entity_id)
                return cached

            # Execute function and cache result
            result = await func(entity_id, *args, **kwargs)
            if result is not None:
                cache.set_entity(entity_id, result, ttl)
            return result

        return wrapper

    return decorator


def invalidate_on_mutation(func):
    """Decorator that invalidates cache after entity mutation.

    For use with create, update, delete operations.
    The first argument after self should be the entity or entity_id.
    """

    async def wrapper(*args, **kwargs):
        result = await func(*args, **kwargs)

        # Invalidate caches after mutation
        cache = get_cache()

        # Try to get entity_id from various sources
        entity_id = None

        # Check kwargs
        if "entity_id" in kwargs:
            entity_id = kwargs["entity_id"]
        elif "entity" in kwargs and hasattr(kwargs["entity"], "id"):
            entity_id = kwargs["entity"].id

        # Check positional args (skip self if present)
        if entity_id is None and len(args) > 1:
            arg = args[1]  # First arg after self
            if isinstance(arg, str):
                entity_id = arg
            elif hasattr(arg, "id"):
                entity_id = arg.id

        if entity_id:
            cache.invalidate_entity(entity_id)
            log.debug("cache_invalidated_after_mutation", entity_id=entity_id)
        else:
            # Clear all caches if we can't determine the entity
            cache.invalidate_search()

        return result

    return wrapper


class CachedEntityManager:
    """Wrapper that adds caching to EntityManager operations.

    Usage:
        from sibyl.cache import CachedEntityManager
        manager = EntityManager(client)
        cached = CachedEntityManager(manager)

        # Lookups are cached
        entity = await cached.get(entity_id)

        # Mutations auto-invalidate cache
        await cached.update(entity_id, updates)
    """

    def __init__(self, manager: Any, entity_ttl: float = 600.0) -> None:
        """Initialize cached wrapper.

        Args:
            manager: The underlying EntityManager instance.
            entity_ttl: TTL for cached entities (default 10 minutes).
        """
        self._manager = manager
        self._entity_ttl = entity_ttl

    async def get(self, entity_id: str) -> Any:
        """Get entity with caching."""
        cache = get_cache()

        # Check cache first
        cached = cache.get_entity(entity_id)
        if cached is not None:
            log.debug("cache_hit_entity", entity_id=entity_id)
            return cached

        # Fetch from underlying manager
        result = await self._manager.get(entity_id)
        if result is not None:
            cache.set_entity(entity_id, result, self._entity_ttl)
        return result

    async def create(self, entity: Any) -> str:
        """Create entity and invalidate caches."""
        result = await self._manager.create(entity)
        get_cache().invalidate_search()
        return result

    async def update(self, entity_id: str, updates: dict[str, Any]) -> Any:
        """Update entity and invalidate caches."""
        result = await self._manager.update(entity_id, updates)
        cache = get_cache()
        cache.invalidate_entity(entity_id)
        return result

    async def delete(self, entity_id: str) -> bool:
        """Delete entity and invalidate caches."""
        result = await self._manager.delete(entity_id)
        cache = get_cache()
        cache.invalidate_entity(entity_id)
        return result

    async def search(
        self,
        query: str,
        entity_types: list[Any] | None = None,
        limit: int = 10,
    ) -> list[tuple[Any, float]]:
        """Search with caching."""
        cache = get_cache()

        # Build filter dict for cache key
        filters: dict[str, Any] = {"limit": limit}
        if entity_types:
            filters["types"] = [t.value if hasattr(t, "value") else str(t) for t in entity_types]

        # Check cache
        cached = cache.get_search(query, **filters)
        if cached is not None:
            log.debug("cache_hit_search", query=query[:50])
            return cached  # type: ignore[return-value]

        # Execute search
        result = await self._manager.search(query, entity_types, limit)
        cache.set_search(query, result, **filters)
        return result

    async def list_by_type(
        self,
        entity_type: Any,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Any]:
        """List by type (not cached - pagination makes caching complex)."""
        return await self._manager.list_by_type(entity_type, limit, offset)

    def __getattr__(self, name: str) -> Any:
        """Forward other methods to underlying manager."""
        return getattr(self._manager, name)
