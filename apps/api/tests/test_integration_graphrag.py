"""Integration tests for Graph-RAG retrieval pipeline.

Tests the complete retrieval pipeline including:
- Hybrid search (vector + graph traversal)
- Temporal boosting
- RRF fusion
- BM25 keyword search
- Community-aware search

These tests use mocks but simulate realistic data patterns
to verify the pipeline behaves correctly end-to-end.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from sibyl.retrieval import (
    BM25Index,
    rrf_merge,
    temporal_boost,
)
from sibyl.retrieval.dedup import EntityDeduplicator, cosine_similarity
from sibyl.retrieval.fusion import rrf_merge_with_metadata


class TestHybridSearchPipeline:
    """Integration tests for hybrid search pipeline."""

    @pytest.fixture
    def sample_entities(self) -> list[dict]:
        """Create sample entities with realistic structure."""
        now = datetime.now(UTC)
        return [
            {
                "id": "pattern_1",
                "name": "Error Handling Best Practices",
                "entity_type": "pattern",
                "description": "Comprehensive guide to error handling in Python",
                "created_at": now - timedelta(days=30),
            },
            {
                "id": "pattern_2",
                "name": "Async Error Recovery",
                "entity_type": "pattern",
                "description": "Handling errors in async code with retry logic",
                "created_at": now - timedelta(days=5),  # More recent
            },
            {
                "id": "pattern_3",
                "name": "Database Connection Pooling",
                "entity_type": "pattern",
                "description": "Managing database connections efficiently",
                "created_at": now - timedelta(days=60),
            },
            {
                "id": "rule_1",
                "name": "Always Log Errors",
                "entity_type": "rule",
                "description": "Log all errors with context for debugging",
                "created_at": now - timedelta(days=10),
            },
            {
                "id": "episode_1",
                "name": "Fixed Auth Bug",
                "entity_type": "episode",
                "description": "Debugging session where we fixed authentication timeout",
                "created_at": now - timedelta(hours=2),  # Very recent
            },
        ]

    def test_temporal_boost_ranks_recent_higher(self, sample_entities: list[dict]) -> None:
        """Verify temporal boosting ranks recent entities higher."""
        # All entities have same base score
        results = [(e, 0.8) for e in sample_entities]

        boosted = temporal_boost(results, decay_days=30.0)

        # Extract IDs in order
        ranked_ids = [e["id"] for e, _ in boosted]

        # Most recent (episode_1, 2 hours old) should be first
        assert ranked_ids[0] == "episode_1"

        # Second most recent (pattern_2, 5 days old) should be near top
        assert ranked_ids.index("pattern_2") < ranked_ids.index("pattern_3")

        # Oldest (pattern_3, 60 days old) should be last
        assert ranked_ids[-1] == "pattern_3"

    def test_temporal_boost_balances_score_and_recency(self, sample_entities: list[dict]) -> None:
        """Temporal boost balances original score with recency."""
        now = datetime.now(UTC)

        # Old but highly relevant
        old_relevant = {
            "id": "old_gold",
            "name": "Foundational Pattern",
            "created_at": now - timedelta(days=30),  # 30 days old
        }

        # New but less relevant
        new_weak = {
            "id": "new_weak",
            "name": "Minor Update",
            "created_at": now - timedelta(days=1),
        }

        results = [
            (old_relevant, 0.95),  # High score, moderately old
            (new_weak, 0.3),  # Low score, new
        ]

        boosted = temporal_boost(results, decay_days=60.0)

        # With decay_days=60, 30-day old entity keeps ~61% of score
        # 0.95 * 0.61 ≈ 0.58 vs 0.3 * 0.98 ≈ 0.29
        # Old but highly relevant should still rank first
        ranked_ids = [e["id"] for e, _ in boosted]
        assert ranked_ids[0] == "old_gold"

    def test_rrf_merge_combines_result_lists(self) -> None:
        """RRF merge properly combines results from different sources."""
        # Vector search results
        vector_results = [
            ({"id": "e1", "name": "Entity 1"}, 0.95),
            ({"id": "e2", "name": "Entity 2"}, 0.85),
            ({"id": "e3", "name": "Entity 3"}, 0.75),
        ]

        # Graph traversal results (different ordering)
        graph_results = [
            ({"id": "e2", "name": "Entity 2"}, 0.9),  # Also in vector
            ({"id": "e4", "name": "Entity 4"}, 0.8),
            ({"id": "e1", "name": "Entity 1"}, 0.7),  # Also in vector
        ]

        merged = rrf_merge([vector_results, graph_results], k=60)

        merged_ids = [e["id"] for e, _ in merged]

        # e1 and e2 appear in both lists, should rank higher
        # e2 is rank 1 in graph + rank 2 in vector = best combined
        assert "e2" in merged_ids[:2]
        assert "e1" in merged_ids[:2]

        # e3 and e4 only appear in one list
        assert "e3" in merged_ids
        assert "e4" in merged_ids

    def test_rrf_merge_with_weights(self) -> None:
        """Weighted RRF respects source importance."""
        vector_results = [({"id": "vector_winner"}, 0.9)]
        graph_results = [({"id": "graph_winner"}, 0.9)]

        # Heavy weight on graph
        merged = rrf_merge(
            [vector_results, graph_results],
            weights=[0.2, 1.0],
        )

        ranked_ids = [e["id"] for e, _ in merged]

        # Graph winner should rank first due to higher weight
        assert ranked_ids[0] == "graph_winner"

    def test_rrf_merge_tracks_sources(self) -> None:
        """RRF with metadata tracks where results came from."""
        vector_results = [
            ({"id": "e1"}, 0.9),
            ({"id": "e2"}, 0.8),
        ]
        graph_results = [
            ({"id": "e2"}, 0.85),
            ({"id": "e3"}, 0.75),
        ]

        merged = rrf_merge_with_metadata(
            [vector_results, graph_results],
            list_names=["vector", "graph"],
        )

        # Find e2's metadata (appears in both)
        e2_entry = next((e, s, m) for e, s, m in merged if e["id"] == "e2")
        _, _, metadata = e2_entry

        assert "vector" in metadata["sources"]
        assert "graph" in metadata["sources"]
        assert metadata["ranks"]["vector"] == 2
        assert metadata["ranks"]["graph"] == 1


class TestBM25Integration:
    """Integration tests for BM25 keyword search."""

    @pytest.fixture
    def bm25_index(self) -> BM25Index:
        """Create and populate a BM25 index."""
        index = BM25Index()

        # Add programming patterns
        index.add(
            {
                "id": "1",
                "name": "Python Error Handling",
                "description": "Best practices for handling errors in Python applications",
            }
        )
        index.add(
            {
                "id": "2",
                "name": "JavaScript Async Patterns",
                "description": "Using async/await and promises effectively",
            }
        )
        index.add(
            {
                "id": "3",
                "name": "Python Async Programming",
                "description": "Asyncio patterns for concurrent Python code",
            }
        )
        index.add(
            {
                "id": "4",
                "name": "Database Error Recovery",
                "description": "Handling database connection errors gracefully",
            }
        )

        return index

    def test_exact_term_matching(self, bm25_index: BM25Index) -> None:
        """BM25 finds entities with exact term matches."""
        results = bm25_index.search("python")

        result_ids = [e["id"] for e, _ in results]

        # Should find both Python entities
        assert "1" in result_ids  # Python Error Handling
        assert "3" in result_ids  # Python Async Programming
        # Should not find JavaScript
        assert "2" not in result_ids

    def test_multi_term_scoring(self, bm25_index: BM25Index) -> None:
        """Multiple matching terms increase score."""
        results = bm25_index.search("python async")

        # Python Async Programming has both terms
        result_ids = [e["id"] for e, _ in results]
        {e["id"]: s for e, s in results}

        # Entity 3 (Python Async) should score highest
        assert result_ids[0] == "3"

        # Both Python entities should appear
        assert "1" in result_ids
        assert "3" in result_ids

    def test_bm25_plus_rrf(self, bm25_index: BM25Index) -> None:
        """BM25 results can be merged with other sources via RRF."""
        bm25_results = bm25_index.search("error handling")

        # Simulated vector results (different ordering)
        vector_results = [
            ({"id": "4"}, 0.9),  # Database Error Recovery
            ({"id": "1"}, 0.85),  # Python Error Handling
        ]

        merged = rrf_merge([bm25_results, vector_results])

        merged_ids = [e["id"] for e, _ in merged]

        # Entity 1 appears in both, should rank high
        assert "1" in merged_ids[:2]
        # Entity 4 appears in both
        assert "4" in merged_ids


class TestDeduplicationIntegration:
    """Integration tests for entity deduplication."""

    @pytest.fixture
    def mock_client(self) -> MagicMock:
        """Create mock graph client."""
        client = MagicMock()
        client.client.driver.execute_query = AsyncMock(return_value=[])
        return client

    @pytest.fixture
    def mock_entity_manager(self) -> MagicMock:
        """Create mock entity manager."""
        manager = MagicMock()
        manager.get = AsyncMock(return_value=None)
        manager.update = AsyncMock(return_value=None)
        manager.delete = AsyncMock(return_value=True)
        return manager

    def test_cosine_similarity_detects_duplicates(self) -> None:
        """Cosine similarity correctly identifies similar embeddings."""
        # Two nearly identical embeddings
        emb1 = [0.1, 0.8, 0.3, 0.5]
        emb2 = [0.11, 0.79, 0.31, 0.49]  # Very similar

        sim = cosine_similarity(emb1, emb2)
        assert sim > 0.99

        # Two different embeddings
        emb3 = [0.9, 0.1, 0.0, 0.0]
        sim_diff = cosine_similarity(emb1, emb3)
        assert sim_diff < 0.5

    @pytest.mark.asyncio
    async def test_dedup_finds_similar_entities(
        self, mock_client: MagicMock, mock_entity_manager: MagicMock
    ) -> None:
        """Deduplicator finds entities with similar embeddings."""
        # Mock entities with embeddings
        mock_client.client.driver.execute_query = AsyncMock(
            return_value=[
                ("e1", "Error Handling Pattern", "pattern", [1.0, 0.0, 0.0]),
                ("e2", "Error Handling Best Practices", "pattern", [0.99, 0.01, 0.0]),  # Similar
                ("e3", "Database Pooling", "pattern", [0.0, 1.0, 0.0]),  # Different
            ]
        )

        dedup = EntityDeduplicator(
            client=mock_client,
            entity_manager=mock_entity_manager,
        )
        dedup.config.min_name_overlap = 0.0  # Disable name check for test

        pairs = await dedup.find_duplicates(threshold=0.95)

        assert len(pairs) == 1
        pair = pairs[0]
        assert pair.entity1_id == "e1"
        assert pair.entity2_id == "e2"
        assert pair.similarity > 0.95


class TestEndToEndRetrieval:
    """End-to-end tests for the complete retrieval pipeline."""

    def test_full_pipeline_ranking(self) -> None:
        """Test complete pipeline: BM25 + vector + temporal + RRF."""
        now = datetime.now(UTC)

        # Entities with various characteristics
        entities = [
            {
                "id": "recent_exact",
                "name": "Python Error Handling",
                "created_at": now - timedelta(hours=1),
            },
            {
                "id": "old_exact",
                "name": "Python Error Patterns",
                "created_at": now - timedelta(days=180),
            },
            {
                "id": "recent_partial",
                "name": "JavaScript Async",
                "created_at": now - timedelta(days=1),
            },
        ]

        # Simulate BM25 results (exact keyword match)
        bm25_results = [
            (entities[0], 0.9),  # recent_exact: Python match
            (entities[1], 0.85),  # old_exact: Python match
        ]

        # Simulate vector results (semantic similarity)
        vector_results = [
            (entities[0], 0.95),  # recent_exact: high similarity
            (entities[2], 0.7),  # recent_partial: moderate similarity
            (entities[1], 0.6),  # old_exact: lower similarity
        ]

        # Step 1: Merge with RRF
        merged = rrf_merge([bm25_results, vector_results])

        # Step 2: Apply temporal boost
        boosted = temporal_boost(merged, decay_days=30.0)

        # Get final ranking
        ranked_ids = [e["id"] for e, _ in boosted]

        # recent_exact should be first:
        # - Appears in both lists (RRF boost)
        # - Very recent (temporal boost)
        assert ranked_ids[0] == "recent_exact"

        # old_exact should rank lower despite keyword match (temporal penalty)
        assert ranked_ids.index("old_exact") > ranked_ids.index("recent_exact")

    def test_community_context_search(self) -> None:
        """Test searching with community context."""
        # Simulated community with member entities
        community = {
            "id": "comm_1",
            "name": "Error Handling Patterns",
            "summary": "Best practices for handling errors in distributed systems",
            "key_concepts": ["error handling", "retry logic", "circuit breaker"],
            "member_ids": ["p1", "p2", "p3"],
        }

        # Query matches community concepts
        query_concepts = ["error", "retry"]

        # Check if community matches query
        matched_concepts = [
            c for c in community["key_concepts"] if any(q in c.lower() for q in query_concepts)
        ]

        assert len(matched_concepts) >= 2
        assert "error handling" in matched_concepts
        assert "retry logic" in matched_concepts

    def test_multi_hop_discovery(self) -> None:
        """Test that graph traversal finds multi-hop related entities."""
        # Simulate graph structure:
        # Task A -> depends on -> Task B -> depends on -> Pattern C
        # Direct vector search for "Pattern C" wouldn't find Task A

        # Vector results (direct matches)
        vector_results = [
            ({"id": "pattern_c", "name": "Pattern C"}, 0.9),
        ]

        # Graph traversal from pattern_c seeds
        graph_results = [
            ({"id": "task_b", "name": "Task B"}, 0.5),  # 1 hop
            ({"id": "task_a", "name": "Task A"}, 0.25),  # 2 hops
        ]

        # Merge with RRF
        merged = rrf_merge([vector_results, graph_results])

        merged_ids = [e["id"] for e, _ in merged]

        # All three should be discoverable
        assert "pattern_c" in merged_ids
        assert "task_b" in merged_ids
        assert "task_a" in merged_ids

        # Pattern C (direct match) should rank highest
        assert merged_ids[0] == "pattern_c"


class TestRetrievalMetrics:
    """Tests for retrieval quality metrics."""

    def test_dcg_calculation(self) -> None:
        """Test Discounted Cumulative Gain calculation."""
        # Relevance scores (graded relevance)
        relevances = [3, 2, 3, 0, 1]  # Position-based

        # DCG = rel_1 + sum(rel_i / log2(i+1))
        import math

        dcg = relevances[0]
        for i in range(1, len(relevances)):
            dcg += relevances[i] / math.log2(i + 2)

        # Ideal DCG (sorted by relevance)
        ideal = sorted(relevances, reverse=True)
        idcg = ideal[0]
        for i in range(1, len(ideal)):
            idcg += ideal[i] / math.log2(i + 2)

        # NDCG = DCG / IDCG
        ndcg = dcg / idcg if idcg > 0 else 0

        # NDCG should be between 0 and 1
        assert 0 <= ndcg <= 1

    def test_precision_at_k(self) -> None:
        """Test Precision@K calculation."""
        # Retrieved results with relevance labels
        retrieved = [
            ({"id": "1"}, True),  # Relevant
            ({"id": "2"}, True),  # Relevant
            ({"id": "3"}, False),  # Not relevant
            ({"id": "4"}, True),  # Relevant
            ({"id": "5"}, False),  # Not relevant
        ]

        # Precision@3 = relevant in top 3 / 3
        k = 3
        relevant_at_k = sum(1 for _, rel in retrieved[:k] if rel)
        precision_at_k = relevant_at_k / k

        assert precision_at_k == 2 / 3

    def test_recall_at_k(self) -> None:
        """Test Recall@K calculation."""
        total_relevant = 4  # Total relevant in corpus

        retrieved = [
            ({"id": "1"}, True),
            ({"id": "2"}, True),
            ({"id": "3"}, False),
            ({"id": "4"}, True),
            ({"id": "5"}, False),
        ]

        # Recall@5 = relevant found / total relevant
        k = 5
        relevant_found = sum(1 for _, rel in retrieved[:k] if rel)
        recall_at_k = relevant_found / total_relevant

        assert recall_at_k == 3 / 4
