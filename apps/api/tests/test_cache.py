"""Tests for the caching module."""

import time

import pytest

from sibyl.cache import (
    CachedEntityManager,
    CacheEntry,
    CacheStats,
    LRUCache,
    QueryCache,
    get_cache,
    reset_cache,
)


class TestCacheStats:
    """Tests for CacheStats dataclass."""

    def test_initial_state(self) -> None:
        """Stats start at zero."""
        stats = CacheStats()
        assert stats.hits == 0
        assert stats.misses == 0
        assert stats.evictions == 0
        assert stats.expirations == 0

    def test_hit_rate_empty(self) -> None:
        """Hit rate is 0 when no requests."""
        stats = CacheStats()
        assert stats.hit_rate == 0.0

    def test_hit_rate_calculation(self) -> None:
        """Hit rate calculated correctly."""
        stats = CacheStats(hits=75, misses=25)
        assert stats.hit_rate == 0.75

    def test_to_dict(self) -> None:
        """Stats serialize to dict."""
        stats = CacheStats(hits=10, misses=5, evictions=2, expirations=1)
        d = stats.to_dict()
        assert d["hits"] == 10
        assert d["misses"] == 5
        assert d["total_requests"] == 15
        assert d["hit_rate"] == pytest.approx(0.6667, rel=0.01)


class TestCacheEntry:
    """Tests for CacheEntry."""

    def test_not_expired_initially(self) -> None:
        """Entry is not expired when created."""
        entry = CacheEntry(value="test", expires_at=time.time() + 100)
        assert not entry.is_expired

    def test_expired_after_time(self) -> None:
        """Entry is expired after expires_at passes."""
        entry = CacheEntry(value="test", expires_at=time.time() - 1)
        assert entry.is_expired

    def test_stores_value(self) -> None:
        """Entry stores the value."""
        entry = CacheEntry(value={"key": "value"}, expires_at=time.time() + 100)
        assert entry.value == {"key": "value"}


class TestLRUCache:
    """Tests for LRUCache."""

    def test_get_missing_returns_none(self) -> None:
        """Get returns None for missing key."""
        cache: LRUCache[str] = LRUCache()
        assert cache.get("missing") is None

    def test_set_and_get(self) -> None:
        """Set stores and get retrieves value."""
        cache: LRUCache[str] = LRUCache()
        cache.set("key1", "value1")
        assert cache.get("key1") == "value1"

    def test_ttl_expiration(self) -> None:
        """Values expire after TTL."""
        cache: LRUCache[str] = LRUCache(default_ttl=0.01)
        cache.set("key", "value")
        assert cache.get("key") == "value"
        time.sleep(0.02)
        assert cache.get("key") is None

    def test_lru_eviction(self) -> None:
        """Oldest entries are evicted when over maxsize."""
        cache: LRUCache[int] = LRUCache(maxsize=3)
        cache.set("a", 1)
        cache.set("b", 2)
        cache.set("c", 3)
        cache.set("d", 4)  # Should evict "a"
        assert cache.get("a") is None
        assert cache.get("b") == 2
        assert cache.size == 3

    def test_access_updates_lru_order(self) -> None:
        """Accessing a key moves it to most recently used."""
        cache: LRUCache[int] = LRUCache(maxsize=3)
        cache.set("a", 1)
        cache.set("b", 2)
        cache.set("c", 3)
        cache.get("a")  # Access "a", making it most recent
        cache.set("d", 4)  # Should evict "b" (oldest)
        assert cache.get("a") == 1
        assert cache.get("b") is None
        assert cache.get("c") == 3

    def test_delete(self) -> None:
        """Delete removes a key."""
        cache: LRUCache[str] = LRUCache()
        cache.set("key", "value")
        assert cache.delete("key") is True
        assert cache.get("key") is None
        assert cache.delete("missing") is False

    def test_invalidate_pattern(self) -> None:
        """Invalidate pattern removes matching keys."""
        cache: LRUCache[int] = LRUCache()
        cache.set("user:1", 1)
        cache.set("user:2", 2)
        cache.set("entity:1", 3)

        count = cache.invalidate_pattern("user:")
        assert count == 2
        assert cache.get("user:1") is None
        assert cache.get("entity:1") == 3

    def test_clear(self) -> None:
        """Clear removes all entries."""
        cache: LRUCache[int] = LRUCache()
        cache.set("a", 1)
        cache.set("b", 2)
        cache.clear()
        assert cache.size == 0
        assert cache.get("a") is None

    def test_stats_tracking(self) -> None:
        """Stats are tracked correctly."""
        cache: LRUCache[str] = LRUCache()
        cache.set("key", "value")
        cache.get("key")  # Hit
        cache.get("key")  # Hit
        cache.get("missing")  # Miss

        assert cache.stats.hits == 2
        assert cache.stats.misses == 1


class TestQueryCache:
    """Tests for QueryCache."""

    def test_search_cache(self) -> None:
        """Search results are cached."""
        qc = QueryCache()
        results = [{"id": "1", "name": "test"}]
        qc.set_search("test query", results, types=["pattern"])
        cached = qc.get_search("test query", types=["pattern"])
        assert cached == results

    def test_search_cache_different_filters(self) -> None:
        """Different filters produce different cache keys."""
        qc = QueryCache()
        qc.set_search("query", [1], types=["pattern"])
        qc.set_search("query", [2], types=["rule"])

        assert qc.get_search("query", types=["pattern"]) == [1]
        assert qc.get_search("query", types=["rule"]) == [2]

    def test_entity_cache(self) -> None:
        """Entities are cached by ID."""
        qc = QueryCache()
        entity = {"id": "ent-123", "name": "Test"}
        qc.set_entity("ent-123", entity)
        assert qc.get_entity("ent-123") == entity

    def test_invalidate_entity_clears_search(self) -> None:
        """Invalidating an entity also clears search cache."""
        qc = QueryCache()
        qc.set_search("test", [1, 2, 3])
        qc.set_entity("ent-1", {"id": "ent-1"})

        qc.invalidate_entity("ent-1")

        assert qc.get_entity("ent-1") is None
        assert qc.get_search("test") is None  # Also cleared

    def test_community_cache(self) -> None:
        """Community summaries are cached."""
        qc = QueryCache()
        summary = {"id": "comm-1", "members": 10}
        qc.set_community("comm-1", summary)
        assert qc.get_community("comm-1") == summary

    def test_clear_all(self) -> None:
        """Clear all removes everything."""
        qc = QueryCache()
        qc.set_search("q", [1])
        qc.set_entity("e", {"x": 1})
        qc.set_community("c", {"y": 2})

        counts = qc.clear_all()
        assert counts["search"] == 1
        assert counts["entity"] == 1
        assert counts["community"] == 1
        assert qc.get_search("q") is None

    def test_get_stats(self) -> None:
        """Stats are returned for all caches."""
        qc = QueryCache()
        qc.set_search("q", [1])
        qc.get_search("q")
        qc.get_search("missing")

        stats = qc.get_stats()
        assert "search" in stats
        assert "entity" in stats
        assert "community" in stats
        assert stats["search"]["hits"] == 1
        assert stats["search"]["misses"] == 1


class TestGlobalCache:
    """Tests for global cache functions."""

    def setup_method(self) -> None:
        """Reset cache before each test."""
        reset_cache()

    def test_get_cache_creates_singleton(self) -> None:
        """get_cache returns the same instance."""
        c1 = get_cache()
        c2 = get_cache()
        assert c1 is c2

    def test_reset_cache(self) -> None:
        """reset_cache clears and recreates cache."""
        c1 = get_cache()
        c1.set_entity("test", {"id": "test"})
        reset_cache()
        c2 = get_cache()
        assert c2.get_entity("test") is None


class TestCachedEntityManager:
    """Tests for CachedEntityManager wrapper."""

    @pytest.fixture
    def mock_manager(self) -> object:
        """Create a mock entity manager."""

        class MockManager:
            def __init__(self) -> None:
                self.entities: dict[str, dict[str, object]] = {}
                self.get_calls = 0
                self.search_calls = 0

            async def get(self, entity_id: str) -> dict[str, object] | None:
                self.get_calls += 1
                return self.entities.get(entity_id)

            async def create(self, entity: object) -> str:
                eid = getattr(entity, "id", "new-id")
                self.entities[eid] = {"id": eid}
                return eid

            async def update(
                self, entity_id: str, updates: dict[str, object]
            ) -> dict[str, object] | None:
                if entity_id in self.entities:
                    self.entities[entity_id].update(updates)
                    return self.entities[entity_id]
                return None

            async def delete(self, entity_id: str) -> bool:
                if entity_id in self.entities:
                    del self.entities[entity_id]
                    return True
                return False

            async def search(
                self,
                query: str,
                entity_types: list[object] | None = None,
                limit: int = 10,
            ) -> list[tuple[dict[str, object], float]]:
                self.search_calls += 1
                return [({"id": "result-1", "name": query}, 0.9)]

            async def list_by_type(
                self, entity_type: object, limit: int = 50, offset: int = 0
            ) -> list[dict[str, object]]:
                return []

        return MockManager()

    @pytest.mark.asyncio
    async def test_get_caches_result(self, mock_manager: object) -> None:
        """Get caches results on second call."""
        reset_cache()
        mock_manager.entities["ent-1"] = {"id": "ent-1", "name": "Test"}  # type: ignore[union-attr]

        cached = CachedEntityManager(mock_manager)

        # First call - cache miss, hits underlying manager
        result1 = await cached.get("ent-1")
        assert result1 == {"id": "ent-1", "name": "Test"}
        assert mock_manager.get_calls == 1  # type: ignore[union-attr]

        # Second call - cache hit, doesn't hit underlying manager
        result2 = await cached.get("ent-1")
        assert result2 == result1
        assert mock_manager.get_calls == 1  # Still 1!  # type: ignore[union-attr]

    @pytest.mark.asyncio
    async def test_update_invalidates_cache(self, mock_manager: object) -> None:
        """Update invalidates cached entity."""
        reset_cache()
        mock_manager.entities["ent-1"] = {"id": "ent-1", "name": "Old"}  # type: ignore[union-attr]

        cached = CachedEntityManager(mock_manager)

        # Prime cache
        await cached.get("ent-1")
        assert mock_manager.get_calls == 1  # type: ignore[union-attr]

        # Update should invalidate
        await cached.update("ent-1", {"name": "New"})

        # Next get should hit manager again
        await cached.get("ent-1")
        assert mock_manager.get_calls == 2  # type: ignore[union-attr]

    @pytest.mark.asyncio
    async def test_search_caches_result(self, mock_manager: object) -> None:
        """Search results are cached."""
        reset_cache()
        cached = CachedEntityManager(mock_manager)

        # First search
        result1 = await cached.search("test query", limit=10)
        assert mock_manager.search_calls == 1  # type: ignore[union-attr]

        # Second search with same params - cache hit
        result2 = await cached.search("test query", limit=10)
        assert mock_manager.search_calls == 1  # type: ignore[union-attr]
        assert result1 == result2

    @pytest.mark.asyncio
    async def test_delete_invalidates_cache(self, mock_manager: object) -> None:
        """Delete invalidates cached entity."""
        reset_cache()
        mock_manager.entities["ent-1"] = {"id": "ent-1"}  # type: ignore[union-attr]

        cached = CachedEntityManager(mock_manager)

        # Prime cache
        await cached.get("ent-1")

        # Delete
        await cached.delete("ent-1")

        # Cache should be invalidated
        assert get_cache().get_entity("ent-1") is None
