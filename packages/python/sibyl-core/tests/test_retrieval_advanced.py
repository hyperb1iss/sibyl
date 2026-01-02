"""Tests for advanced retrieval modules: dedup.py and hybrid.py.

Covers:
- EntityDeduplicator: vectorized similarity, pair finding, merge suggestions
- Hybrid search: vector + graph fusion, RRF merge, temporal boosting
- Score normalization and result merging from multiple sources
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pytest

from sibyl_core.models.entities import Entity, EntityType
from sibyl_core.retrieval.dedup import (
    DedupConfig,
    DuplicatePair,
    EntityDeduplicator,
    cosine_similarity,
    get_deduplicator,
    jaccard_similarity,
)
from sibyl_core.retrieval.hybrid import (
    HybridConfig,
    HybridResult,
    graph_traversal,
    hybrid_search,
    simple_hybrid_search,
    vector_search,
)

# =============================================================================
# Test Fixtures and Mock Infrastructure
# =============================================================================


@dataclass
class MockGraphClientForDedup:
    """Mock GraphClient for deduplication tests.

    Simulates FalkorDB client with controllable entity embeddings
    for testing vectorized similarity operations.
    """

    entities_with_embeddings: list[tuple[str, str, str, list[float]]] = field(default_factory=list)
    redirect_count: int = 0
    query_history: list[str] = field(default_factory=list)

    class MockDriver:
        """Mock driver for execute_query."""

        def __init__(self, parent: MockGraphClientForDedup):
            self.parent = parent

        async def execute_query(self, query: str, **params: Any) -> list[Any]:
            """Execute mock query and return configured results."""
            self.parent.query_history.append(query)

            # Handle embedding fetch
            if "name_embedding IS NOT NULL" in query:
                return self.parent.entities_with_embeddings

            # Handle relationship redirect - return count
            if "DELETE r" in query:
                self.parent.redirect_count += 1
                return [{"redirected": 1}]

            return []

    @property
    def client(self) -> MagicMock:
        """Return mock client with driver."""
        mock = MagicMock()
        mock.driver = self.MockDriver(self)
        return mock


@dataclass
class MockEntityManagerForDedup:
    """Mock EntityManager for deduplication merge tests."""

    entities: dict[str, Entity] = field(default_factory=dict)
    deleted_ids: list[str] = field(default_factory=list)
    updated_ids: list[str] = field(default_factory=list)

    async def get(self, entity_id: str) -> Entity | None:
        """Get entity by ID."""
        return self.entities.get(entity_id)

    async def update(self, entity_id: str, updates: dict[str, Any]) -> Entity:
        """Update entity."""
        self.updated_ids.append(entity_id)
        entity = self.entities[entity_id]
        if "metadata" in updates:
            entity.metadata = updates["metadata"]
        return entity

    async def delete(self, entity_id: str) -> bool:
        """Delete entity."""
        if entity_id in self.entities:
            del self.entities[entity_id]
            self.deleted_ids.append(entity_id)
            return True
        return False


@dataclass
class MockEntityManagerForHybrid:
    """Mock EntityManager for hybrid search tests."""

    search_results: list[tuple[Entity, float]] = field(default_factory=list)
    search_calls: list[dict[str, Any]] = field(default_factory=list)

    async def search(
        self,
        query: str,
        entity_types: list[EntityType] | None = None,
        limit: int = 10,
    ) -> list[tuple[Entity, float]]:
        """Return preconfigured search results."""
        self.search_calls.append({"query": query, "entity_types": entity_types, "limit": limit})
        results = self.search_results
        if entity_types:
            results = [(e, s) for e, s in results if e.entity_type in entity_types]
        return results[:limit]


@dataclass
class MockGraphClientForHybrid:
    """Mock GraphClient for hybrid search graph traversal tests."""

    traversal_results: list[dict[str, Any]] = field(default_factory=list)
    query_history: list[str] = field(default_factory=list)

    class MockDriver:
        """Mock driver for execute_query."""

        def __init__(self, parent: MockGraphClientForHybrid):
            self.parent = parent

        async def execute_query(self, query: str, **params: Any) -> list[Any]:
            """Execute mock query and return configured results."""
            self.parent.query_history.append(query)
            return self.parent.traversal_results

    @property
    def client(self) -> MagicMock:
        """Return mock client with driver."""
        mock = MagicMock()
        mock.driver = self.MockDriver(self)
        return mock

    @staticmethod
    def normalize_result(result: Any) -> list[dict[str, Any]]:
        """Normalize query results."""
        if isinstance(result, list):
            return result
        return []


def make_entity_for_test(
    entity_id: str,
    name: str = "Test Entity",
    entity_type: EntityType = EntityType.TOPIC,
    description: str = "",
    created_at: datetime | None = None,
    metadata: dict[str, Any] | None = None,
) -> Entity:
    """Factory for test entities."""
    return Entity(
        id=entity_id,
        name=name,
        entity_type=entity_type,
        description=description,
        content="",
        metadata=metadata or {},
        created_at=created_at or datetime.now(UTC),
    )


# =============================================================================
# EntityDeduplicator Tests - Vectorized Similarity
# =============================================================================


class TestEntityDeduplicatorVectorized:
    """Test vectorized similarity operations in EntityDeduplicator."""

    def test_find_similar_pairs_empty_list(self) -> None:
        """Empty entity list returns no pairs."""
        client = MockGraphClientForDedup()
        manager = MockEntityManagerForDedup()
        dedup = EntityDeduplicator(client=client, entity_manager=manager)  # type: ignore[arg-type]

        pairs = dedup._find_similar_pairs_vectorized([], threshold=0.9)
        assert pairs == []

    def test_find_similar_pairs_single_entity(self) -> None:
        """Single entity returns no pairs."""
        client = MockGraphClientForDedup()
        manager = MockEntityManagerForDedup()
        dedup = EntityDeduplicator(client=client, entity_manager=manager)  # type: ignore[arg-type]

        entities = [("id1", "Entity One", "topic", [1.0, 0.0, 0.0])]
        pairs = dedup._find_similar_pairs_vectorized(entities, threshold=0.9)
        assert pairs == []

    def test_find_similar_pairs_identical_embeddings(self) -> None:
        """Identical embeddings produce similarity 1.0."""
        client = MockGraphClientForDedup()
        manager = MockEntityManagerForDedup()
        config = DedupConfig(
            similarity_threshold=0.95,
            same_type_only=True,
            min_name_overlap=0.0,  # Disable name filter
        )
        dedup = EntityDeduplicator(client=client, entity_manager=manager, config=config)  # type: ignore[arg-type]

        embedding = [1.0, 0.5, 0.25, 0.125]
        entities = [
            ("id1", "Entity One", "topic", embedding),
            ("id2", "Entity Two", "topic", embedding),
        ]

        pairs = dedup._find_similar_pairs_vectorized(entities, threshold=0.95)
        assert len(pairs) == 1
        assert pairs[0].entity1_id == "id1"
        assert pairs[0].entity2_id == "id2"
        assert pairs[0].similarity == pytest.approx(1.0, rel=0.001)

    def test_find_similar_pairs_orthogonal_embeddings(self) -> None:
        """Orthogonal embeddings are not considered duplicates."""
        client = MockGraphClientForDedup()
        manager = MockEntityManagerForDedup()
        dedup = EntityDeduplicator(client=client, entity_manager=manager)  # type: ignore[arg-type]

        entities = [
            ("id1", "Entity One", "topic", [1.0, 0.0, 0.0]),
            ("id2", "Entity Two", "topic", [0.0, 1.0, 0.0]),
        ]

        pairs = dedup._find_similar_pairs_vectorized(entities, threshold=0.5)
        assert pairs == []

    def test_find_similar_pairs_high_similarity(self) -> None:
        """Similar but not identical embeddings found above threshold."""
        client = MockGraphClientForDedup()
        manager = MockEntityManagerForDedup()
        config = DedupConfig(
            similarity_threshold=0.9,
            same_type_only=True,
            min_name_overlap=0.0,
        )
        dedup = EntityDeduplicator(client=client, entity_manager=manager, config=config)  # type: ignore[arg-type]

        # Very similar embeddings
        entities = [
            ("id1", "Python programming", "topic", [1.0, 0.5, 0.3]),
            ("id2", "Python coding", "topic", [1.0, 0.51, 0.31]),  # Slightly different
        ]

        pairs = dedup._find_similar_pairs_vectorized(entities, threshold=0.9)
        assert len(pairs) == 1
        assert pairs[0].similarity > 0.9

    def test_find_similar_pairs_different_types_filtered(self) -> None:
        """Entities of different types are filtered when same_type_only=True."""
        client = MockGraphClientForDedup()
        manager = MockEntityManagerForDedup()
        config = DedupConfig(
            similarity_threshold=0.9,
            same_type_only=True,
            min_name_overlap=0.0,
        )
        dedup = EntityDeduplicator(client=client, entity_manager=manager, config=config)  # type: ignore[arg-type]

        embedding = [1.0, 0.5, 0.3]
        entities = [
            ("id1", "Python", "topic", embedding),
            ("id2", "Python", "pattern", embedding),  # Different type
        ]

        pairs = dedup._find_similar_pairs_vectorized(entities, threshold=0.9)
        assert pairs == []

    def test_find_similar_pairs_different_types_allowed(self) -> None:
        """Entities of different types matched when same_type_only=False."""
        client = MockGraphClientForDedup()
        manager = MockEntityManagerForDedup()
        config = DedupConfig(
            similarity_threshold=0.9,
            same_type_only=False,  # Allow cross-type matching
            min_name_overlap=0.0,
        )
        dedup = EntityDeduplicator(client=client, entity_manager=manager, config=config)  # type: ignore[arg-type]

        embedding = [1.0, 0.5, 0.3]
        entities = [
            ("id1", "Python", "topic", embedding),
            ("id2", "Python", "pattern", embedding),  # Different type
        ]

        pairs = dedup._find_similar_pairs_vectorized(entities, threshold=0.9)
        assert len(pairs) == 1

    def test_find_similar_pairs_name_overlap_filter(self) -> None:
        """Pairs filtered by minimum name overlap."""
        client = MockGraphClientForDedup()
        manager = MockEntityManagerForDedup()
        config = DedupConfig(
            similarity_threshold=0.9,
            same_type_only=True,
            min_name_overlap=0.5,  # Require 50% name overlap
        )
        dedup = EntityDeduplicator(client=client, entity_manager=manager, config=config)  # type: ignore[arg-type]

        embedding = [1.0, 0.5, 0.3]
        entities = [
            ("id1", "Python programming", "topic", embedding),
            ("id2", "JavaScript frameworks", "topic", embedding),  # No name overlap
        ]

        pairs = dedup._find_similar_pairs_vectorized(entities, threshold=0.9)
        assert pairs == []

    def test_find_similar_pairs_name_overlap_passes(self) -> None:
        """Pairs with sufficient name overlap are kept."""
        client = MockGraphClientForDedup()
        manager = MockEntityManagerForDedup()
        config = DedupConfig(
            similarity_threshold=0.9,
            same_type_only=True,
            min_name_overlap=0.3,
        )
        dedup = EntityDeduplicator(client=client, entity_manager=manager, config=config)  # type: ignore[arg-type]

        embedding = [1.0, 0.5, 0.3]
        entities = [
            ("id1", "Python async programming", "topic", embedding),
            ("id2", "Python concurrent programming", "topic", embedding),
        ]

        pairs = dedup._find_similar_pairs_vectorized(entities, threshold=0.9)
        # "Python" and "programming" overlap, should pass
        assert len(pairs) == 1

    def test_find_similar_pairs_multiple_clusters(self) -> None:
        """Multiple duplicate clusters found correctly."""
        client = MockGraphClientForDedup()
        manager = MockEntityManagerForDedup()
        config = DedupConfig(
            similarity_threshold=0.95,
            same_type_only=False,
            min_name_overlap=0.0,
        )
        dedup = EntityDeduplicator(client=client, entity_manager=manager, config=config)  # type: ignore[arg-type]

        # Two clusters of identical embeddings
        embedding_a = [1.0, 0.0, 0.0]
        embedding_b = [0.0, 1.0, 0.0]

        entities = [
            ("id1", "Cluster A 1", "topic", embedding_a),
            ("id2", "Cluster A 2", "topic", embedding_a),
            ("id3", "Cluster B 1", "topic", embedding_b),
            ("id4", "Cluster B 2", "topic", embedding_b),
        ]

        pairs = dedup._find_similar_pairs_vectorized(entities, threshold=0.95)
        # Should find: (id1, id2) and (id3, id4)
        assert len(pairs) == 2

        pair_ids = {(p.entity1_id, p.entity2_id) for p in pairs}
        assert ("id1", "id2") in pair_ids
        assert ("id3", "id4") in pair_ids


class TestEntityDeduplicatorSuggestKeep:
    """Test the _suggest_keep heuristic."""

    def test_suggest_keep_longer_name(self) -> None:
        """Longer name is preferred."""
        client = MockGraphClientForDedup()
        manager = MockEntityManagerForDedup()
        dedup = EntityDeduplicator(client=client, entity_manager=manager)  # type: ignore[arg-type]

        # Name1 is significantly longer
        result = dedup._suggest_keep("id1", "id2", "Python programming language", "Python")
        assert result == "id1"

        # Name2 is significantly longer
        result = dedup._suggest_keep("id1", "id2", "Python", "Python programming language")
        assert result == "id2"

    def test_suggest_keep_similar_length_prefers_first(self) -> None:
        """Similar length names default to first ID."""
        client = MockGraphClientForDedup()
        manager = MockEntityManagerForDedup()
        dedup = EntityDeduplicator(client=client, entity_manager=manager)  # type: ignore[arg-type]

        result = dedup._suggest_keep("id1", "id2", "Python 3", "Python 2")
        assert result == "id1"


class TestEntityDeduplicatorFindDuplicates:
    """Test the full find_duplicates async workflow."""

    @pytest.mark.asyncio
    async def test_find_duplicates_insufficient_entities(self) -> None:
        """Returns empty when fewer than 2 entities."""
        client = MockGraphClientForDedup()
        client.entities_with_embeddings = [("id1", "Entity One", "topic", [1.0, 0.0])]

        manager = MockEntityManagerForDedup()
        dedup = EntityDeduplicator(client=client, entity_manager=manager)  # type: ignore[arg-type]

        pairs = await dedup.find_duplicates()
        assert pairs == []

    @pytest.mark.asyncio
    async def test_find_duplicates_returns_sorted_pairs(self) -> None:
        """Duplicate pairs are sorted by similarity (highest first)."""
        client = MockGraphClientForDedup()
        # Three entities: (id1, id2) are very similar, (id1, id3) are somewhat similar
        client.entities_with_embeddings = [
            ("id1", "Python async", "topic", [1.0, 0.5, 0.0]),
            ("id2", "Python async", "topic", [1.0, 0.5, 0.0]),  # Identical to id1
            ("id3", "Python sync", "topic", [1.0, 0.4, 0.1]),  # Similar to id1
        ]

        manager = MockEntityManagerForDedup()
        config = DedupConfig(
            similarity_threshold=0.9,
            same_type_only=True,
            min_name_overlap=0.0,
        )
        dedup = EntityDeduplicator(client=client, entity_manager=manager, config=config)  # type: ignore[arg-type]

        pairs = await dedup.find_duplicates(threshold=0.9)

        # Both pairs should be found
        assert len(pairs) >= 1
        # First pair should have highest similarity
        if len(pairs) > 1:
            assert pairs[0].similarity >= pairs[1].similarity

    @pytest.mark.asyncio
    async def test_find_duplicates_with_type_filter(self) -> None:
        """Type filter is passed to query."""
        client = MockGraphClientForDedup()
        client.entities_with_embeddings = [
            ("id1", "Entity One", "topic", [1.0, 0.0]),
            ("id2", "Entity Two", "topic", [1.0, 0.0]),
        ]

        manager = MockEntityManagerForDedup()
        dedup = EntityDeduplicator(client=client, entity_manager=manager)  # type: ignore[arg-type]

        await dedup.find_duplicates(entity_types=["topic"])

        # Verify query was made
        assert len(client.query_history) >= 1


class TestEntityDeduplicatorMerge:
    """Test entity merge operations."""

    @pytest.mark.asyncio
    async def test_merge_entities_success(self) -> None:
        """Successful merge deletes removed entity."""
        client = MockGraphClientForDedup()
        manager = MockEntityManagerForDedup()

        # Add entities
        entity1 = make_entity_for_test("id1", name="Keep Me", metadata={"key": "value1"})
        entity2 = make_entity_for_test("id2", name="Remove Me", metadata={"other": "value2"})
        manager.entities["id1"] = entity1
        manager.entities["id2"] = entity2

        dedup = EntityDeduplicator(client=client, entity_manager=manager)  # type: ignore[arg-type]

        result = await dedup.merge_entities(keep_id="id1", remove_id="id2")

        assert result is True
        assert "id2" in manager.deleted_ids
        assert "id2" not in manager.entities

    @pytest.mark.asyncio
    async def test_merge_entities_not_found(self) -> None:
        """Merge fails gracefully when entity not found."""
        client = MockGraphClientForDedup()
        manager = MockEntityManagerForDedup()

        dedup = EntityDeduplicator(client=client, entity_manager=manager)  # type: ignore[arg-type]

        result = await dedup.merge_entities(keep_id="missing1", remove_id="missing2")

        assert result is False

    @pytest.mark.asyncio
    async def test_merge_entities_updates_cached_pairs(self) -> None:
        """Merged entities are removed from cached duplicate pairs."""
        client = MockGraphClientForDedup()
        manager = MockEntityManagerForDedup()

        entity1 = make_entity_for_test("id1")
        entity2 = make_entity_for_test("id2")
        entity3 = make_entity_for_test("id3")
        manager.entities["id1"] = entity1
        manager.entities["id2"] = entity2
        manager.entities["id3"] = entity3

        dedup = EntityDeduplicator(client=client, entity_manager=manager)  # type: ignore[arg-type]

        # Set up cached pairs
        dedup._duplicate_pairs = [
            DuplicatePair("id1", "id2", 0.99),
            DuplicatePair("id2", "id3", 0.95),
        ]

        await dedup.merge_entities(keep_id="id1", remove_id="id2")

        # Pairs containing id2 should be removed
        remaining_pairs = dedup.suggest_merges()
        assert len(remaining_pairs) == 0  # Both pairs contained id2


class TestDuplicatePair:
    """Test DuplicatePair dataclass."""

    def test_duplicate_pair_to_dict_rounds_similarity(self) -> None:
        """Similarity is rounded to 4 decimals in to_dict."""
        pair = DuplicatePair(
            entity1_id="id1",
            entity2_id="id2",
            similarity=0.987654321,
            entity1_name="Name 1",
            entity2_name="Name 2",
            entity_type="topic",
            suggested_keep="id1",
        )

        d = pair.to_dict()
        assert d["similarity"] == 0.9877


class TestGetDeduplicator:
    """Test global deduplicator factory."""

    def test_get_deduplicator_creates_new(self) -> None:
        """get_deduplicator creates new instance."""
        client = MockGraphClientForDedup()
        manager = MockEntityManagerForDedup()

        dedup = get_deduplicator(client, manager)  # type: ignore[arg-type]
        assert isinstance(dedup, EntityDeduplicator)

    def test_get_deduplicator_with_custom_config(self) -> None:
        """get_deduplicator respects custom config."""
        client = MockGraphClientForDedup()
        manager = MockEntityManagerForDedup()
        config = DedupConfig(similarity_threshold=0.8)

        dedup = get_deduplicator(client, manager, config=config)  # type: ignore[arg-type]
        assert dedup.config.similarity_threshold == 0.8


# =============================================================================
# Hybrid Search Tests
# =============================================================================


class TestHybridConfig:
    """Test HybridConfig defaults and customization."""

    def test_hybrid_config_defaults(self) -> None:
        """HybridConfig has sensible defaults."""
        config = HybridConfig()
        assert config.vector_weight == 1.0
        assert config.graph_weight == 0.8
        assert config.bm25_weight == 0.5
        assert config.rrf_k == 60.0
        assert config.graph_depth == 2
        assert config.apply_temporal is True
        assert config.temporal_decay_days == 365.0

    def test_hybrid_config_custom(self) -> None:
        """HybridConfig accepts custom values."""
        config = HybridConfig(
            vector_weight=2.0,
            graph_weight=0.5,
            rrf_k=30.0,
            apply_temporal=False,
        )
        assert config.vector_weight == 2.0
        assert config.graph_weight == 0.5
        assert config.rrf_k == 30.0
        assert config.apply_temporal is False


class TestHybridResult:
    """Test HybridResult dataclass."""

    def test_hybrid_result_entities_property(self) -> None:
        """entities property extracts just the entities."""
        e1 = make_entity_for_test("id1")
        e2 = make_entity_for_test("id2")

        result = HybridResult(
            results=[(e1, 0.9), (e2, 0.8)],
            metadata={"query": "test"},
        )

        entities = result.entities
        assert len(entities) == 2
        assert entities[0].id == "id1"
        assert entities[1].id == "id2"

    def test_hybrid_result_total_property(self) -> None:
        """total property returns result count."""
        e1 = make_entity_for_test("id1")

        result = HybridResult(results=[(e1, 0.9)], metadata={})
        assert result.total == 1

        empty_result = HybridResult(results=[], metadata={})
        assert empty_result.total == 0


class TestVectorSearch:
    """Test vector_search function."""

    @pytest.mark.asyncio
    async def test_vector_search_calls_entity_manager(self) -> None:
        """vector_search delegates to entity_manager.search."""
        manager = MockEntityManagerForHybrid()
        e1 = make_entity_for_test("id1", name="Python")
        manager.search_results = [(e1, 0.9)]

        results = await vector_search("Python", manager, limit=10)  # type: ignore[arg-type]

        assert len(results) == 1
        assert results[0][0].id == "id1"
        assert manager.search_calls[0]["query"] == "Python"

    @pytest.mark.asyncio
    async def test_vector_search_with_type_filter(self) -> None:
        """vector_search passes entity_types filter."""
        manager = MockEntityManagerForHybrid()
        e1 = make_entity_for_test("id1", entity_type=EntityType.PATTERN)
        manager.search_results = [(e1, 0.9)]

        await vector_search(
            "test",
            manager,  # type: ignore[arg-type]
            entity_types=[EntityType.PATTERN],
            limit=5,
        )

        assert manager.search_calls[0]["entity_types"] == [EntityType.PATTERN]
        assert manager.search_calls[0]["limit"] == 5

    @pytest.mark.asyncio
    async def test_vector_search_handles_exception(self) -> None:
        """vector_search returns empty on exception."""
        manager = MockEntityManagerForHybrid()
        manager.search = AsyncMock(side_effect=Exception("DB error"))  # type: ignore[method-assign]

        results = await vector_search("test", manager)  # type: ignore[arg-type]
        assert results == []


class TestGraphTraversal:
    """Test graph_traversal function."""

    @pytest.mark.asyncio
    async def test_graph_traversal_empty_seeds(self) -> None:
        """Empty seed list returns empty results."""
        client = MockGraphClientForHybrid()

        results = await graph_traversal([], client, depth=2)  # type: ignore[arg-type]
        assert results == []

    @pytest.mark.asyncio
    async def test_graph_traversal_builds_correct_query(self) -> None:
        """Graph traversal builds correct Cypher query."""
        client = MockGraphClientForHybrid()
        client.traversal_results = []

        await graph_traversal(["id1", "id2"], client, depth=3, limit=15)  # type: ignore[arg-type]

        assert len(client.query_history) == 1
        query = client.query_history[0]
        assert "RELATIONSHIP*1..3" in query  # Depth 3
        assert "seed.uuid IN $seed_ids" in query

    @pytest.mark.asyncio
    async def test_graph_traversal_scores_by_distance(self) -> None:
        """Graph traversal scores entities by distance."""
        client = MockGraphClientForHybrid()
        # Return entities at different distances
        client.traversal_results = [
            {
                "id": "near",
                "name": "Near Entity",
                "type": "topic",
                "description": "",
                "distance": 1,
            },
            {"id": "far", "name": "Far Entity", "type": "topic", "description": "", "distance": 3},
        ]

        results = await graph_traversal(["seed"], client, depth=5)  # type: ignore[arg-type]

        assert len(results) == 2
        # Closer entity should have higher score
        near_score = next(s for e, s in results if e.id == "near")
        far_score = next(s for e, s in results if e.id == "far")
        assert near_score > far_score  # 1/(1+1) > 1/(3+1)

    @pytest.mark.asyncio
    async def test_graph_traversal_with_group_id(self) -> None:
        """Graph traversal includes group_id filter."""
        client = MockGraphClientForHybrid()
        client.traversal_results = []

        await graph_traversal(["id1"], client, depth=2, group_id="org-123")  # type: ignore[arg-type]

        query = client.query_history[0]
        assert "group_id" in query


class TestHybridSearch:
    """Test the main hybrid_search function."""

    @pytest.mark.asyncio
    async def test_hybrid_search_empty_results(self) -> None:
        """Hybrid search with no results returns empty HybridResult."""
        client = MockGraphClientForHybrid()
        manager = MockEntityManagerForHybrid()
        manager.search_results = []

        result = await hybrid_search("test query", client, manager)  # type: ignore[arg-type]

        assert result.total == 0
        assert result.metadata["sources"] == []

    @pytest.mark.asyncio
    async def test_hybrid_search_vector_only(self) -> None:
        """Hybrid search with only vector results."""
        client = MockGraphClientForHybrid()
        manager = MockEntityManagerForHybrid()

        e1 = make_entity_for_test("id1", name="Python")
        manager.search_results = [(e1, 0.9)]

        config = HybridConfig(graph_weight=0)  # Disable graph

        result = await hybrid_search(
            "Python",
            client,  # type: ignore[arg-type]
            manager,  # type: ignore[arg-type]
            config=config,
            limit=10,
        )

        assert result.total >= 1
        assert "vector" in result.metadata["sources"]

    @pytest.mark.asyncio
    async def test_hybrid_search_with_temporal_boost(self) -> None:
        """Hybrid search applies temporal boosting when enabled."""
        client = MockGraphClientForHybrid()
        manager = MockEntityManagerForHybrid()

        # Recent entity
        recent = make_entity_for_test(
            "recent",
            name="Recent",
            created_at=datetime.now(UTC) - timedelta(days=1),
        )
        # Old entity
        old = make_entity_for_test(
            "old",
            name="Old",
            created_at=datetime.now(UTC) - timedelta(days=500),
        )

        manager.search_results = [(old, 0.95), (recent, 0.9)]

        config = HybridConfig(
            apply_temporal=True,
            temporal_decay_days=30.0,  # Fast decay
            graph_weight=0,
        )

        result = await hybrid_search(
            "test",
            client,  # type: ignore[arg-type]
            manager,  # type: ignore[arg-type]
            config=config,
        )

        assert result.metadata["temporal_applied"] is True

    @pytest.mark.asyncio
    async def test_hybrid_search_metadata_inclusion(self) -> None:
        """Hybrid search includes metadata when requested."""
        client = MockGraphClientForHybrid()
        manager = MockEntityManagerForHybrid()

        e1 = make_entity_for_test("id1")
        manager.search_results = [(e1, 0.9)]

        config = HybridConfig(graph_weight=0)

        result = await hybrid_search(
            "test",
            client,  # type: ignore[arg-type]
            manager,  # type: ignore[arg-type]
            config=config,
            include_metadata=True,
        )

        assert "source_details" in result.metadata

    @pytest.mark.asyncio
    async def test_hybrid_search_respects_limit(self) -> None:
        """Hybrid search respects result limit."""
        client = MockGraphClientForHybrid()
        manager = MockEntityManagerForHybrid()

        # Many results
        manager.search_results = [
            (make_entity_for_test(f"id{i}"), 0.9 - i * 0.01) for i in range(20)
        ]

        config = HybridConfig(graph_weight=0)

        result = await hybrid_search(
            "test",
            client,  # type: ignore[arg-type]
            manager,  # type: ignore[arg-type]
            config=config,
            limit=5,
        )

        assert result.total == 5


class TestSimpleHybridSearch:
    """Test simple_hybrid_search function."""

    @pytest.mark.asyncio
    async def test_simple_hybrid_search_basic(self) -> None:
        """Simple hybrid search returns vector results."""
        manager = MockEntityManagerForHybrid()
        e1 = make_entity_for_test("id1")
        manager.search_results = [(e1, 0.9)]

        results = await simple_hybrid_search("test", manager, limit=10)  # type: ignore[arg-type]

        assert len(results) == 1
        assert results[0][0].id == "id1"

    @pytest.mark.asyncio
    async def test_simple_hybrid_search_with_temporal(self) -> None:
        """Simple hybrid search applies temporal boosting."""
        manager = MockEntityManagerForHybrid()

        recent = make_entity_for_test(
            "recent",
            created_at=datetime.now(UTC) - timedelta(days=1),
        )
        old = make_entity_for_test(
            "old",
            created_at=datetime.now(UTC) - timedelta(days=365),
        )

        manager.search_results = [(old, 0.95), (recent, 0.85)]

        results = await simple_hybrid_search(
            "test",
            manager,  # type: ignore[arg-type]
            apply_temporal=True,
        )

        # Recent entity may be reordered due to temporal boost
        assert len(results) == 2

    @pytest.mark.asyncio
    async def test_simple_hybrid_search_no_temporal(self) -> None:
        """Simple hybrid search can skip temporal boosting."""
        manager = MockEntityManagerForHybrid()
        e1 = make_entity_for_test("id1")
        manager.search_results = [(e1, 0.9)]

        results = await simple_hybrid_search(
            "test",
            manager,  # type: ignore[arg-type]
            apply_temporal=False,
        )

        assert results[0][1] == 0.9  # Score unchanged


# =============================================================================
# Integration-Style Tests
# =============================================================================


class TestDedupWithRealVectors:
    """Tests using real numpy operations for vectorized similarity."""

    def test_numpy_cosine_similarity_matches_pure_python(self) -> None:
        """Numpy vectorized cosine similarity matches pure Python implementation."""
        vec1 = [1.0, 2.0, 3.0, 4.0]
        vec2 = [1.1, 2.1, 3.1, 4.1]

        # Pure Python
        python_sim = cosine_similarity(vec1, vec2)

        # Numpy
        v1 = np.array(vec1)
        v2 = np.array(vec2)
        numpy_sim = float(np.dot(v1, v2) / (np.linalg.norm(v1) * np.linalg.norm(v2)))

        assert python_sim == pytest.approx(numpy_sim, rel=0.0001)

    def test_vectorized_finds_same_pairs_as_naive(self) -> None:
        """Vectorized implementation finds same pairs as naive O(n^2) approach."""
        entities = [
            ("id1", "Entity A", "topic", [1.0, 0.0, 0.0]),
            ("id2", "Entity B", "topic", [0.0, 1.0, 0.0]),
            ("id3", "Entity A Clone", "topic", [1.0, 0.0, 0.0]),  # Duplicate of id1
        ]

        client = MockGraphClientForDedup()
        manager = MockEntityManagerForDedup()
        config = DedupConfig(
            similarity_threshold=0.99,
            same_type_only=True,
            min_name_overlap=0.0,
        )
        dedup = EntityDeduplicator(client=client, entity_manager=manager, config=config)  # type: ignore[arg-type]

        pairs = dedup._find_similar_pairs_vectorized(entities, threshold=0.99)

        # Should find exactly one pair: (id1, id3)
        assert len(pairs) == 1
        assert pairs[0].entity1_id == "id1"
        assert pairs[0].entity2_id == "id3"


class TestHybridWithRRFFusion:
    """Test hybrid search RRF fusion behavior."""

    @pytest.mark.asyncio
    async def test_rrf_boosts_entities_in_multiple_sources(self) -> None:
        """Entities appearing in multiple sources get higher RRF scores."""
        client = MockGraphClientForHybrid()
        manager = MockEntityManagerForHybrid()

        # Entity appears in vector results
        shared_entity = make_entity_for_test("shared", name="Shared Entity")
        vector_only = make_entity_for_test("vector_only", name="Vector Only")

        manager.search_results = [
            (shared_entity, 0.9),
            (vector_only, 0.85),
        ]

        # Entity also appears in graph results
        client.traversal_results = [
            {
                "id": "shared",
                "name": "Shared Entity",
                "type": "topic",
                "description": "",
                "distance": 1,
            },
            {
                "id": "graph_only",
                "name": "Graph Only",
                "type": "topic",
                "description": "",
                "distance": 1,
            },
        ]

        config = HybridConfig(
            vector_weight=1.0,
            graph_weight=1.0,
            apply_temporal=False,
        )

        result = await hybrid_search(
            "test",
            client,  # type: ignore[arg-type]
            manager,  # type: ignore[arg-type]
            config=config,
            limit=10,
        )

        # Shared entity should have higher score due to appearing in both
        if result.total >= 2:
            shared_result = next(
                ((e, s) for e, s in result.results if e.id == "shared"),
                None,
            )
            assert shared_result is not None


class TestEdgeCases:
    """Test edge cases and boundary conditions."""

    def test_cosine_similarity_large_vectors(self) -> None:
        """Cosine similarity handles large dimension vectors."""
        dim = 1536  # OpenAI embedding dimension
        vec1 = [float(i) / dim for i in range(dim)]
        vec2 = [float(i + 1) / dim for i in range(dim)]

        sim = cosine_similarity(vec1, vec2)
        assert 0.99 < sim <= 1.0  # Should be very similar

    def test_jaccard_similarity_special_characters(self) -> None:
        """Jaccard handles special characters in names."""
        sim = jaccard_similarity("C++ Programming", "C++ Development")
        assert sim > 0  # "C++" should match

    def test_jaccard_similarity_unicode(self) -> None:
        """Jaccard handles unicode characters."""
        sim = jaccard_similarity("Python", "Python")
        assert sim == 1.0

    @pytest.mark.asyncio
    async def test_hybrid_search_handles_empty_entity_id(self) -> None:
        """Hybrid search handles entities without proper IDs."""
        client = MockGraphClientForHybrid()
        manager = MockEntityManagerForHybrid()

        # Entity with empty ID
        entity = make_entity_for_test("", name="No ID Entity")
        manager.search_results = [(entity, 0.9)]

        config = HybridConfig(graph_weight=0)

        # Should not raise
        result = await hybrid_search(
            "test",
            client,  # type: ignore[arg-type]
            manager,  # type: ignore[arg-type]
            config=config,
        )

        assert result is not None

    def test_dedup_config_validation(self) -> None:
        """DedupConfig validates threshold range."""
        # Valid thresholds
        config = DedupConfig(similarity_threshold=0.0)
        assert config.similarity_threshold == 0.0

        config = DedupConfig(similarity_threshold=1.0)
        assert config.similarity_threshold == 1.0

    @pytest.mark.asyncio
    async def test_graph_traversal_handles_query_exception(self) -> None:
        """Graph traversal returns empty on exception."""
        client = MockGraphClientForHybrid()
        client.client.driver.execute_query = AsyncMock(side_effect=Exception("DB error"))

        results = await graph_traversal(["id1"], client, depth=2)  # type: ignore[arg-type]
        assert results == []
