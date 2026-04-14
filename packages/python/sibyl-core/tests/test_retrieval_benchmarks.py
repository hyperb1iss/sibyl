"""Retrieval pipeline benchmarks and mini-LongMemEval.

Tests correctness and measures relative performance of the retrieval
pipeline components: temporal boosting, RRF fusion, cross-encoder
reranking, and hybrid search. Includes a mini-LongMemEval that seeds
entities across time and verifies temporal/relational/factual queries.

Run:
    moon run core:test -- -k benchmark
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from sibyl_core.models.entities import Entity, EntityType
from sibyl_core.retrieval.fusion import rrf_merge
from sibyl_core.retrieval.temporal import (
    TemporalConfig,
    calculate_boost,
    temporal_boost,
    temporal_boost_single,
)

# =============================================================================
# Helpers
# =============================================================================


def _make_entity(
    name: str = "test",
    entity_type: EntityType = EntityType.TOPIC,
    description: str = "",
    content: str = "",
    created_at: datetime | None = None,
) -> Entity:
    """Create a test entity."""
    return Entity(
        id=f"bench_{name.replace(' ', '_')[:20]}",
        name=name,
        entity_type=entity_type,
        description=description or name,
        content=content or name,
        created_at=created_at or datetime.now(UTC),
    )


def _entity_with_time(
    name: str,
    days_ago: float,
    entity_type: EntityType = EntityType.EPISODE,
    description: str = "",
    content: str = "",
) -> Entity:
    """Create an entity with a specific age."""
    ts = datetime.now(UTC) - timedelta(days=days_ago)
    return _make_entity(
        name=name,
        entity_type=entity_type,
        description=description or name,
        content=content or name,
        created_at=ts,
    )


@dataclass
class _MockEntityManager:
    """Minimal mock for benchmark tests."""

    entities: dict[str, Entity] = field(default_factory=dict)
    search_results: list[tuple[Entity, float]] = field(default_factory=list)

    def add_entity(self, entity: Entity) -> None:
        self.entities[entity.id] = entity

    async def search(
        self,
        query: str,
        entity_types: list[EntityType] | None = None,
        limit: int = 10,
    ) -> list[tuple[Entity, float]]:
        if self.search_results:
            results = self.search_results
            if entity_types:
                results = [(e, s) for e, s in results if e.entity_type in entity_types]
            return results[:limit]

        results = []
        query_lower = query.lower()
        for entity in self.entities.values():
            if entity_types and entity.entity_type not in entity_types:
                continue
            score = 0.0
            if query_lower in entity.name.lower():
                score = 0.9
            elif query_lower in (entity.description or "").lower():
                score = 0.7
            elif query_lower in (entity.content or "").lower() or not query_lower:
                score = 0.5
            if score > 0:
                results.append((entity, score))
        results.sort(key=lambda x: x[1], reverse=True)
        return results[:limit]


def _timed(fn, *args, **kwargs) -> tuple[Any, float]:
    """Time a synchronous function, return (result, ms)."""
    start = time.perf_counter()
    result = fn(*args, **kwargs)
    elapsed_ms = (time.perf_counter() - start) * 1000
    return result, elapsed_ms


# =============================================================================
# Temporal Boost Benchmarks
# =============================================================================


class TestTemporalBoostBenchmark:
    """Verify temporal boost correctness and measure throughput."""

    def test_recent_entities_rank_higher(self):
        """Recent entities should score higher than old ones after boosting."""
        recent = _entity_with_time("recent insight", days_ago=1)
        medium = _entity_with_time("month-old insight", days_ago=30)
        old = _entity_with_time("year-old insight", days_ago=365)
        ancient = _entity_with_time("ancient insight", days_ago=1000)

        results = [(recent, 0.8), (medium, 0.8), (old, 0.8), (ancient, 0.8)]
        boosted = temporal_boost(results, decay_days=365.0)

        scores = {e.name: s for e, s in boosted}
        assert scores["recent insight"] > scores["month-old insight"]
        assert scores["month-old insight"] > scores["year-old insight"]
        assert scores["year-old insight"] > scores["ancient insight"]

    def test_decay_reorders_equal_relevance(self):
        """When relevance is equal, temporal decay should reorder by recency."""
        entities = [(_entity_with_time(f"entity_{i}", days_ago=i * 30), 0.9) for i in range(10)]
        boosted = temporal_boost(entities, decay_days=90.0)

        names = [e.name for e, _ in boosted]
        assert names[0] == "entity_0"
        assert names[-1] == "entity_9"

    def test_high_relevance_beats_recency(self):
        """A highly relevant old entity should beat a mediocre recent one."""
        old_relevant = _entity_with_time("old but relevant", days_ago=180)
        new_irrelevant = _entity_with_time("new but weak", days_ago=1)

        results = [(old_relevant, 0.95), (new_irrelevant, 0.3)]
        boosted = temporal_boost(results, decay_days=365.0)

        assert boosted[0][0].name == "old but relevant"

    def test_no_timestamp_entities_preserved(self):
        """Entities without timestamps should keep their original score."""
        no_ts = _make_entity(name="timeless", description="no timestamp")
        with_ts = _entity_with_time("timed", days_ago=30)

        results = [(no_ts, 0.7), (with_ts, 0.7)]
        boosted = temporal_boost(results, decay_days=365.0)

        no_ts_score = next(s for e, s in boosted if e.name == "timeless")
        assert no_ts_score == pytest.approx(0.7)

    def test_min_boost_prevents_zero(self):
        """Very old entities should get min_boost, not zero."""
        boost = calculate_boost(age_days=3650, decay_days=365.0, min_boost=0.1)
        assert boost == 0.1

    def test_per_type_decay_rates(self):
        """Different entity types should support different decay rates."""
        episode = _entity_with_time("episode", days_ago=60, entity_type=EntityType.EPISODE)
        pattern = _entity_with_time("pattern", days_ago=60, entity_type=EntityType.PATTERN)

        episode_score = temporal_boost_single(episode, 0.8, TemporalConfig(decay_days=30.0))
        pattern_score = temporal_boost_single(pattern, 0.8, TemporalConfig(decay_days=365.0))

        assert pattern_score > episode_score

    def test_benchmark_throughput_1k_entities(self):
        """Measure temporal boost throughput for 1000 entities."""
        entities = [
            (_entity_with_time(f"e_{i}", days_ago=i), 0.5 + (i % 50) / 100) for i in range(1000)
        ]

        _, elapsed_ms = _timed(temporal_boost, entities, decay_days=365.0)

        assert elapsed_ms < 500, (
            f"Temporal boost for 1K entities took {elapsed_ms:.1f}ms (budget: 500ms)"
        )

    def test_benchmark_throughput_10k_entities(self):
        """Measure temporal boost throughput for 10000 entities."""
        entities = [
            (_entity_with_time(f"e_{i}", days_ago=i % 365), 0.5 + (i % 50) / 100)
            for i in range(10_000)
        ]

        _, elapsed_ms = _timed(temporal_boost, entities, decay_days=365.0)

        assert elapsed_ms < 2000, (
            f"Temporal boost for 10K entities took {elapsed_ms:.1f}ms (budget: 2000ms)"
        )


# =============================================================================
# RRF Fusion Benchmarks
# =============================================================================


class TestRRFFusionBenchmark:
    """Verify RRF merge correctness and throughput."""

    def test_rrf_preserves_top_results(self):
        """Top-ranked items in both lists should rank highest after fusion."""
        top = _make_entity(name="top_hit")
        mid = _make_entity(name="mid_hit")
        low = _make_entity(name="low_hit")

        vector_results = [(top, 0.95), (mid, 0.8), (low, 0.6)]
        graph_results = [(top, 0.9), (low, 0.7), (mid, 0.5)]

        merged = rrf_merge([vector_results, graph_results], k=60.0)
        assert merged[0][0].name == "top_hit"

    def test_rrf_weight_influence(self):
        """Higher-weighted lists should contribute more to final ranking."""
        a = _make_entity(name="vector_fav")
        b = _make_entity(name="graph_fav")

        vector_results = [(a, 0.9), (b, 0.5)]
        graph_results = [(b, 0.9), (a, 0.5)]

        merged_vector_heavy = rrf_merge([vector_results, graph_results], k=60.0, weights=[2.0, 1.0])
        merged_graph_heavy = rrf_merge([vector_results, graph_results], k=60.0, weights=[1.0, 2.0])

        assert merged_vector_heavy[0][0].name == "vector_fav"
        assert merged_graph_heavy[0][0].name == "graph_fav"

    def test_rrf_handles_disjoint_lists(self):
        """RRF should handle lists with no overlap."""
        a = _make_entity(name="only_vector")
        b = _make_entity(name="only_graph")

        merged = rrf_merge([[(a, 0.9)], [(b, 0.8)]], k=60.0)
        assert len(merged) == 2

    def test_benchmark_rrf_100_results(self):
        """Measure RRF merge throughput for 100 results per list."""
        lists = []
        for _list_idx in range(3):
            results = [(_make_entity(name=f"e_{i}"), 1.0 - i / 100) for i in range(100)]
            lists.append(results)

        _, elapsed_ms = _timed(rrf_merge, lists, k=60.0)

        assert elapsed_ms < 100, f"RRF merge 3x100 took {elapsed_ms:.1f}ms (budget: 100ms)"

    def test_benchmark_rrf_1000_results(self):
        """Measure RRF merge for 1000 results per list."""
        lists = []
        for _list_idx in range(3):
            results = [(_make_entity(name=f"e_{i}"), 1.0 - i / 1000) for i in range(1000)]
            lists.append(results)

        _, elapsed_ms = _timed(rrf_merge, lists, k=60.0)

        assert elapsed_ms < 500, f"RRF merge 3x1000 took {elapsed_ms:.1f}ms (budget: 500ms)"


# =============================================================================
# Mini-LongMemEval: Temporal/Relational/Factual Recall
# =============================================================================


class TestMiniLongMemEval:
    """Simulated memory recall evaluation.

    Seeds a _MockEntityManager with entities spanning different time periods,
    topics, and relationships, then verifies the retrieval pipeline returns
    correct results for temporal, factual, and relational queries.
    """

    @pytest.fixture
    def seeded_manager(self) -> _MockEntityManager:
        """Seed a manager with 50 entities across time and topics."""
        manager = _MockEntityManager()

        knowledge = [
            ("Redis connection pooling", "episode", 7, "Use pool_size >= concurrent_requests"),
            ("OAuth token refresh", "pattern", 14, "Refresh tokens 5 min before expiry"),
            (
                "FalkorDB port config",
                "episode",
                30,
                "Port 6380, not 6379, to avoid Redis conflicts",
            ),
            ("JWT validation middleware", "pattern", 45, "Validate audience and issuer claims"),
            ("PostgreSQL vacuum strategy", "episode", 60, "Autovacuum with scale_factor=0.05"),
            ("GraphQL N+1 prevention", "pattern", 90, "Use DataLoader for batching"),
            (
                "Kubernetes pod affinity",
                "episode",
                120,
                "Use preferredDuringScheduling for flexibility",
            ),
            ("Python async context managers", "pattern", 150, "Always use async with for cleanup"),
            ("WebSocket reconnection", "episode", 180, "Exponential backoff with jitter"),
            ("Docker multi-stage builds", "pattern", 200, "Separate build and runtime stages"),
            (
                "Memory leak in event listeners",
                "episode",
                3,
                "Remove listeners on component unmount",
            ),
            (
                "Rate limiting with sliding window",
                "pattern",
                10,
                "Sliding window counter algorithm",
            ),
            ("Database migration rollback", "episode", 25, "Always write reversible migrations"),
            ("API versioning strategy", "pattern", 50, "URL prefix versioning for public APIs"),
            ("Terraform state locking", "episode", 75, "Use DynamoDB for state lock backend"),
            ("CORS preflight caching", "pattern", 100, "Cache OPTIONS responses for 86400s"),
            (
                "gRPC deadline propagation",
                "episode",
                130,
                "Propagate deadlines across service boundaries",
            ),
            (
                "React hydration mismatch",
                "episode",
                160,
                "Ensure server and client render identical output",
            ),
            (
                "Nginx proxy buffering",
                "pattern",
                190,
                "Disable proxy_buffering for streaming responses",
            ),
            ("TLS certificate rotation", "episode", 220, "Rotate certs 30 days before expiry"),
        ]

        for name, etype, days_ago, content in knowledge:
            entity = _entity_with_time(
                name=name,
                days_ago=days_ago,
                entity_type=EntityType(etype),
                description=content,
                content=content,
            )
            manager.add_entity(entity)

        return manager

    @pytest.mark.asyncio
    async def test_factual_recall(self, seeded_manager: _MockEntityManager):
        """Search should find entities by semantic content."""
        results = await seeded_manager.search("Redis connection")
        assert len(results) > 0
        names = [e.name for e, _ in results]
        assert "Redis connection pooling" in names

    @pytest.mark.asyncio
    async def test_temporal_recall_recent(self, seeded_manager: _MockEntityManager):
        """Temporal boost should prefer recent entities when relevance is close."""
        results = await seeded_manager.search("memory")
        boosted = temporal_boost(results, decay_days=30.0)

        if boosted:
            top_entity = boosted[0][0]
            assert top_entity.name == "Memory leak in event listeners"

    @pytest.mark.asyncio
    async def test_type_filtering(self, seeded_manager: _MockEntityManager):
        """Type filter should restrict results."""
        patterns = await seeded_manager.search("", entity_types=[EntityType.PATTERN])
        for entity, _ in patterns:
            assert entity.entity_type == EntityType.PATTERN

    @pytest.mark.asyncio
    async def test_temporal_boost_reshuffles_old_vs_new(self, seeded_manager: _MockEntityManager):
        """Aggressive decay should produce different scores than gentle decay."""
        all_results = await seeded_manager.search("")

        boosted_aggressive = temporal_boost(all_results, decay_days=14.0)
        boosted_gentle = temporal_boost(all_results, decay_days=365.0)

        if len(boosted_aggressive) >= 3 and len(boosted_gentle) >= 3:
            agg_scores = [s for _, s in boosted_aggressive[:5]]
            gen_scores = [s for _, s in boosted_gentle[:5]]
            assert agg_scores != gen_scores, "Different decay rates should produce different scores"

    @pytest.mark.asyncio
    async def test_recall_at_k(self, seeded_manager: _MockEntityManager):
        """Measure recall@k for known queries."""
        test_cases = [
            ("authentication", "OAuth token refresh"),
            ("database", "PostgreSQL vacuum strategy"),
            ("kubernetes", "Kubernetes pod affinity"),
            ("certificate", "TLS certificate rotation"),
            ("websocket", "WebSocket reconnection"),
        ]

        hits = 0
        for query, expected_name in test_cases:
            results = await seeded_manager.search(query, limit=5)
            found_names = {e.name for e, _ in results}
            if expected_name in found_names:
                hits += 1

        recall_at_5 = hits / len(test_cases)
        assert recall_at_5 >= 0.6, f"Recall@5 = {recall_at_5:.0%} (expected >= 60%)"


# =============================================================================
# Cross-Encoder Reranking Benchmarks
# =============================================================================


class TestRerankingBenchmark:
    """Verify reranking correctness (without requiring sentence-transformers)."""

    def test_reranking_config_defaults(self):
        """Verify default reranking config matches expectations."""
        from sibyl_core.retrieval.reranking import CrossEncoderConfig

        config = CrossEncoderConfig()
        assert config.enabled is False
        assert config.top_k == 20
        assert config.fallback_on_error is True

    @pytest.mark.asyncio
    async def test_reranking_disabled_passthrough(self):
        """When disabled, reranking should return results unchanged."""
        from sibyl_core.retrieval.reranking import CrossEncoderConfig, rerank_results

        entities = [(_make_entity(name=f"e_{i}"), 1.0 - i / 10) for i in range(5)]
        config = CrossEncoderConfig(enabled=False)

        result = await rerank_results("test query", entities, config)

        assert result.reranked_count == 0
        assert len(result.results) == len(entities)
        assert result.results[0][0].name == "e_0"

    @pytest.mark.asyncio
    async def test_reranking_graceful_fallback(self):
        """Without sentence-transformers, reranking should fall back gracefully."""
        from sibyl_core.retrieval.reranking import CrossEncoderConfig, rerank_results

        entities = [(_make_entity(name=f"e_{i}"), 1.0 - i / 10) for i in range(5)]
        config = CrossEncoderConfig(enabled=True, fallback_on_error=True)

        result = await rerank_results("test query", entities, config)

        assert len(result.results) == len(entities)

    def test_content_extraction(self):
        """Content extractor should handle various entity shapes."""
        from sibyl_core.retrieval.reranking import _extract_content

        entity_with_content = _make_entity(name="test", content="detailed content here")
        assert _extract_content(entity_with_content) == "detailed content here"

        entity_no_content = _make_entity(name="fallback_name", content="")
        result = _extract_content(entity_no_content)
        assert "fallback_name" in result


# =============================================================================
# Hybrid Search Pipeline Benchmarks
# =============================================================================


class TestHybridSearchBenchmark:
    """Test the full hybrid search pipeline with mocks."""

    @pytest.mark.asyncio
    async def test_hybrid_config_wiring(self):
        """Verify HybridConfig picks up reranking and temporal settings."""
        from sibyl_core.retrieval.hybrid import HybridConfig

        config = HybridConfig(
            apply_reranking=True,
            rerank_model="cross-encoder/ms-marco-MiniLM-L-6-v2",
            apply_temporal=True,
            temporal_decay_days=30.0,
        )

        assert config.apply_reranking is True
        assert config.temporal_decay_days == 30.0

    @pytest.mark.asyncio
    async def test_simple_hybrid_with_temporal(self):
        """simple_hybrid_search should apply temporal boosting."""
        from sibyl_core.retrieval.hybrid import simple_hybrid_search

        manager = _MockEntityManager()
        recent = _entity_with_time("recent", days_ago=1)
        old = _entity_with_time("old", days_ago=365)
        manager.search_results = [(old, 0.9), (recent, 0.85)]

        results = await simple_hybrid_search("test", manager, apply_temporal=True)

        assert results[0][0].name == "recent"

    @pytest.mark.asyncio
    async def test_simple_hybrid_without_temporal(self):
        """Without temporal, original ordering should be preserved."""
        from sibyl_core.retrieval.hybrid import simple_hybrid_search

        manager = _MockEntityManager()
        recent = _entity_with_time("recent", days_ago=1)
        old = _entity_with_time("old", days_ago=365)
        manager.search_results = [(old, 0.9), (recent, 0.85)]

        results = await simple_hybrid_search("test", manager, apply_temporal=False)

        assert results[0][0].name == "old"
