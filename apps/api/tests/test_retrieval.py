"""Tests for retrieval components."""

import math
from datetime import UTC, datetime, timedelta

import pytest

from sibyl.retrieval.bm25 import BM25Index, tokenize
from sibyl.retrieval.fusion import (
    rrf_merge,
    rrf_merge_with_metadata,
    rrf_score,
    weighted_score_merge,
)
from sibyl.retrieval.temporal import (
    calculate_age_days,
    calculate_boost,
    get_entity_timestamp,
    temporal_boost,
)


class TestTemporalBoosting:
    """Tests for temporal boosting."""

    def test_calculate_boost_new_entity(self) -> None:
        """New entities get full boost."""
        boost = calculate_boost(age_days=0.0, decay_days=365.0)
        assert boost == pytest.approx(1.0)

    def test_calculate_boost_one_year(self) -> None:
        """One year old entity gets ~0.37 boost (1/e)."""
        boost = calculate_boost(age_days=365.0, decay_days=365.0)
        assert boost == pytest.approx(1.0 / math.e, rel=0.01)

    def test_calculate_boost_respects_min(self) -> None:
        """Very old entities get minimum boost."""
        boost = calculate_boost(age_days=10000, decay_days=365.0, min_boost=0.1)
        assert boost == 0.1

    def test_calculate_boost_max_age(self) -> None:
        """Entities at max age get min boost."""
        boost = calculate_boost(age_days=2000, decay_days=365.0, min_boost=0.1, max_age_days=1825)
        assert boost == 0.1

    def test_get_entity_timestamp_dict(self) -> None:
        """Extract timestamp from dict."""
        now = datetime.now(UTC)
        entity = {"id": "1", "created_at": now}
        ts = get_entity_timestamp(entity, "created_at")
        assert ts == now

    def test_get_entity_timestamp_object(self) -> None:
        """Extract timestamp from object."""

        class Entity:
            def __init__(self) -> None:
                self.created_at = datetime.now(UTC)
                self.valid_from = None

        entity = Entity()
        ts = get_entity_timestamp(entity, "created_at")
        assert ts == entity.created_at

    def test_get_entity_timestamp_auto(self) -> None:
        """Auto mode tries valid_from first, then created_at."""
        now = datetime.now(UTC)
        entity = {"created_at": now, "valid_from": None}
        ts = get_entity_timestamp(entity, "auto")
        assert ts == now

    def test_get_entity_timestamp_string(self) -> None:
        """Parse ISO string timestamps."""
        entity = {"created_at": "2024-01-15T10:30:00+00:00"}
        ts = get_entity_timestamp(entity, "created_at")
        assert ts is not None
        assert ts.year == 2024

    def test_calculate_age_days(self) -> None:
        """Calculate age correctly."""
        past = datetime.now(UTC) - timedelta(days=10)
        age = calculate_age_days(past)
        assert age == pytest.approx(10.0, rel=0.01)

    def test_temporal_boost_empty(self) -> None:
        """Empty results return empty."""
        result = temporal_boost([])
        assert result == []

    def test_temporal_boost_sorts_by_score(self) -> None:
        """Results are re-sorted by boosted score."""
        now = datetime.now(UTC)
        old = now - timedelta(days=365)

        results = [
            ({"id": "old", "created_at": old}, 0.9),
            ({"id": "new", "created_at": now}, 0.8),
        ]

        boosted = temporal_boost(results, decay_days=365.0)

        # New entity should now be first (its score less reduced)
        assert boosted[0][0]["id"] == "new"

    def test_temporal_boost_no_timestamp(self) -> None:
        """Entities without timestamps keep original score."""
        results = [
            ({"id": "1"}, 0.9),
            ({"id": "2"}, 0.8),
        ]
        boosted = temporal_boost(results)
        assert boosted[0][1] == 0.9
        assert boosted[1][1] == 0.8


class TestRRFMerge:
    """Tests for Reciprocal Rank Fusion."""

    def test_rrf_score(self) -> None:
        """RRF score calculation."""
        assert rrf_score(rank=1, k=60) == 1 / 61
        assert rrf_score(rank=2, k=60) == 1 / 62

    def test_rrf_merge_empty(self) -> None:
        """Empty lists return empty."""
        result = rrf_merge([])
        assert result == []

    def test_rrf_merge_single_list(self) -> None:
        """Single list preserves order."""
        results = [
            ({"id": "a"}, 0.9),
            ({"id": "b"}, 0.8),
        ]
        merged = rrf_merge([results])
        assert merged[0][0]["id"] == "a"
        assert merged[1][0]["id"] == "b"

    def test_rrf_merge_deduplicates(self) -> None:
        """Duplicate entities are merged."""
        list1 = [({"id": "a"}, 0.9)]
        list2 = [({"id": "a"}, 0.95)]

        merged = rrf_merge([list1, list2])

        # Should have only one result
        assert len(merged) == 1
        # Score should be sum of RRF contributions
        expected_score = rrf_score(1, 60) + rrf_score(1, 60)
        assert merged[0][1] == pytest.approx(expected_score)

    def test_rrf_merge_ranking(self) -> None:
        """Entity appearing in both lists ranks higher."""
        list1 = [({"id": "a"}, 0.9), ({"id": "b"}, 0.8)]
        list2 = [({"id": "b"}, 0.95), ({"id": "c"}, 0.85)]

        merged = rrf_merge([list1, list2])

        # 'b' appears in both lists, should rank highest
        assert merged[0][0]["id"] == "b"

    def test_rrf_merge_with_weights(self) -> None:
        """Weights affect RRF scores."""
        list1 = [({"id": "a"}, 0.9)]
        list2 = [({"id": "b"}, 0.8)]

        # Heavy weight on list2
        merged = rrf_merge([list1, list2], weights=[0.1, 1.0])

        # 'b' should rank higher due to weight
        assert merged[0][0]["id"] == "b"

    def test_rrf_merge_limit(self) -> None:
        """Limit caps results."""
        results = [({"id": str(i)}, 0.9 - i * 0.1) for i in range(10)]
        merged = rrf_merge([results], limit=3)
        assert len(merged) == 3

    def test_rrf_merge_with_metadata(self) -> None:
        """Metadata tracks sources."""
        list1 = [({"id": "a"}, 0.9)]
        list2 = [({"id": "a"}, 0.8), ({"id": "b"}, 0.7)]

        merged = rrf_merge_with_metadata(
            [list1, list2],
            list_names=["vector", "graph"],
        )

        # Check metadata for 'a'
        entity_a = next(m for e, s, m in merged if e["id"] == "a")
        assert "vector" in entity_a["sources"]
        assert "graph" in entity_a["sources"]
        assert entity_a["ranks"]["vector"] == 1


class TestWeightedScoreMerge:
    """Tests for weighted score merging."""

    def test_weighted_merge_empty(self) -> None:
        """Empty lists return empty."""
        result = weighted_score_merge([])
        assert result == []

    def test_weighted_merge_normalizes(self) -> None:
        """Scores are normalized within lists."""
        list1 = [({"id": "a"}, 100.0)]  # Different scale
        list2 = [({"id": "b"}, 0.5)]

        merged = weighted_score_merge([list1, list2], normalize=True)

        # Both should have normalized scores
        assert len(merged) == 2

    def test_weighted_merge_deduplicates(self) -> None:
        """Duplicates are averaged."""
        list1 = [({"id": "a"}, 1.0)]
        list2 = [({"id": "a"}, 0.5)]

        merged = weighted_score_merge([list1, list2], normalize=False)

        assert len(merged) == 1
        # Average of 1.0 and 0.5
        assert merged[0][1] == pytest.approx(0.75)


class TestBM25:
    """Tests for BM25 search."""

    def test_tokenize_basic(self) -> None:
        """Basic tokenization."""
        tokens = tokenize("Hello World", min_length=2)
        assert "hello" in tokens
        assert "world" in tokens

    def test_tokenize_stops(self) -> None:
        """Stop words are removed."""
        tokens = tokenize("the quick brown fox", min_length=2, stop_words={"the"})
        assert "the" not in tokens
        assert "quick" in tokens

    def test_tokenize_min_length(self) -> None:
        """Short tokens are removed."""
        tokens = tokenize("I am a test", min_length=3)
        assert "i" not in tokens
        assert "am" not in tokens
        assert "test" in tokens

    def test_bm25_add_and_search(self) -> None:
        """Add entities and search."""
        index = BM25Index()
        index.add({"id": "1", "name": "Python programming guide"})
        index.add({"id": "2", "name": "JavaScript tutorial"})
        index.add({"id": "3", "name": "Python async patterns"})

        results = index.search("python")

        assert len(results) == 2
        # Python docs should come first
        ids = [e["id"] for e, _ in results]
        assert "1" in ids
        assert "3" in ids
        assert "2" not in ids

    def test_bm25_tf_ranking(self) -> None:
        """Higher term frequency ranks higher."""
        index = BM25Index()
        index.add({"id": "1", "name": "python", "content": "python is great"})
        index.add({"id": "2", "name": "python python python", "content": "python rocks"})

        results = index.search("python")

        # Entity with more 'python' mentions should rank higher
        assert results[0][0]["id"] == "2"

    def test_bm25_remove(self) -> None:
        """Remove entity from index."""
        index = BM25Index()
        index.add({"id": "1", "name": "test"})
        assert index.size == 1

        index.remove("1")
        assert index.size == 0

        results = index.search("test")
        assert len(results) == 0

    def test_bm25_update(self) -> None:
        """Re-adding updates the entity."""
        index = BM25Index()
        index.add({"id": "1", "name": "old name"})

        results = index.search("old")
        assert len(results) == 1

        # Update
        index.add({"id": "1", "name": "new name"})

        results = index.search("old")
        assert len(results) == 0

        results = index.search("new")
        assert len(results) == 1

    def test_bm25_clear(self) -> None:
        """Clear empties the index."""
        index = BM25Index()
        index.add({"id": "1", "name": "test"})
        index.add({"id": "2", "name": "test2"})

        index.clear()

        assert index.size == 0

    def test_bm25_empty_query(self) -> None:
        """Empty query returns empty."""
        index = BM25Index()
        index.add({"id": "1", "name": "test"})

        results = index.search("")
        assert results == []

    def test_bm25_min_score(self) -> None:
        """Min score filters weak matches."""
        index = BM25Index()
        index.add({"id": "1", "name": "python programming"})
        index.add({"id": "2", "name": "cooking recipes"})

        # Search for 'python' with high min_score
        results = index.search("python", min_score=0.5)

        # Only strong match
        assert all(e["id"] == "1" for e, _ in results)
