"""Tests for sibyl-core retrieval module.

Covers BM25 keyword search, deduplication, RRF fusion, and temporal decay.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from sibyl_core.retrieval.bm25 import (
    BM25Config,
    BM25Index,
    extract_text,
    get_bm25_index,
    reset_bm25_index,
    tokenize,
)
from sibyl_core.retrieval.dedup import (
    DedupConfig,
    DuplicatePair,
    cosine_similarity,
    jaccard_similarity,
)
from sibyl_core.retrieval.fusion import (
    FusionConfig,
    default_dedup_key,
    rrf_merge,
    rrf_merge_with_metadata,
    rrf_score,
    weighted_score_merge,
)
from sibyl_core.retrieval.temporal import (
    TemporalConfig,
    calculate_age_days,
    calculate_boost,
    get_entity_timestamp,
    parse_temporal_datetime,
    resolve_temporal_reference,
    temporal_boost,
    temporal_boost_single,
    temporal_proximity_boost,
)

# =============================================================================
# BM25 Tests
# =============================================================================


class TestTokenize:
    """Test BM25 tokenization."""

    def test_tokenize_basic(self) -> None:
        """Basic text is split into lowercase tokens."""
        tokens = tokenize("Hello World Python Code")
        assert tokens == ["hello", "world", "python", "code"]

    def test_tokenize_stop_words(self) -> None:
        """Stop words are filtered out."""
        stop_words = {"the", "a", "is"}
        tokens = tokenize("The quick fox is a runner", stop_words=stop_words)
        assert "the" not in tokens
        assert "is" not in tokens
        # "a" is filtered by stop words, but "quick", "fox", "runner" remain
        assert "quick" in tokens
        assert "fox" in tokens
        assert "runner" in tokens

    def test_tokenize_min_length(self) -> None:
        """Short tokens below min_length are filtered."""
        tokens = tokenize("I am a big dog", min_length=3)
        # "I", "am", "a" filtered (length < 3)
        assert "big" in tokens
        assert "dog" in tokens
        assert "am" not in tokens
        assert "i" not in tokens

    def test_tokenize_empty_input(self) -> None:
        """Empty input returns empty list."""
        assert tokenize("") == []
        assert tokenize("   ") == []

    def test_tokenize_special_characters(self) -> None:
        """Special characters are stripped, alphanumeric preserved."""
        tokens = tokenize("hello-world! test123 @symbol")
        assert "hello" in tokens
        assert "world" in tokens
        assert "test123" in tokens
        assert "symbol" in tokens

    def test_tokenize_numbers(self) -> None:
        """Numbers are preserved as tokens."""
        tokens = tokenize("Python 3 is version 3", min_length=1)
        assert "python" in tokens
        assert "3" in tokens


class TestBM25Config:
    """Test BM25Config defaults and configuration."""

    def test_bm25_config_defaults(self) -> None:
        """Config has sensible defaults."""
        config = BM25Config()
        assert config.k1 == 1.5
        assert config.b == 0.75
        assert config.min_token_length == 2
        assert isinstance(config.stop_words, set)
        assert "the" in config.stop_words
        assert "is" in config.stop_words

    def test_bm25_config_custom(self) -> None:
        """Config accepts custom values."""
        config = BM25Config(
            k1=2.0,
            b=0.5,
            min_token_length=3,
            stop_words={"custom", "words"},
        )
        assert config.k1 == 2.0
        assert config.b == 0.5
        assert config.min_token_length == 3
        assert config.stop_words == {"custom", "words"}


class TestExtractText:
    """Test text extraction from entities."""

    def test_extract_text_from_dict(self) -> None:
        """Extract text from dictionary entity."""
        entity = {"name": "Python", "description": "A programming language"}
        text = extract_text(entity)
        assert "Python" in text
        assert "programming language" in text

    def test_extract_text_from_object(self) -> None:
        """Extract text from object with attributes."""

        @dataclass
        class Entity:
            name: str
            description: str

        entity = Entity(name="FastAPI", description="Modern web framework")
        text = extract_text(entity)
        assert "FastAPI" in text
        assert "Modern web framework" in text

    def test_extract_text_missing_fields(self) -> None:
        """Handle missing fields gracefully."""
        entity = {"name": "Only Name"}
        text = extract_text(entity)
        assert text == "Only Name"

    def test_extract_text_custom_fields(self) -> None:
        """Extract from custom field list."""
        entity = {"summary": "Short", "body": "Long content here"}
        text = extract_text(entity, fields=["summary", "body"])
        assert "Short" in text
        assert "Long content here" in text


class TestBM25Index:
    """Test BM25Index operations."""

    def test_bm25_index_add_document(self) -> None:
        """Adding documents updates the index correctly."""
        index = BM25Index()
        entity = {"id": "doc1", "name": "Python Programming", "description": "Learn Python"}

        doc_id = index.add(entity)
        assert doc_id == "doc1"
        assert index.size == 1

    def test_bm25_index_add_multiple(self) -> None:
        """Multiple documents can be indexed."""
        index = BM25Index()
        index.add({"id": "1", "name": "Python async programming"})
        index.add({"id": "2", "name": "JavaScript promises"})
        index.add({"id": "3", "name": "Python web frameworks"})

        assert index.size == 3

    def test_bm25_index_search(self) -> None:
        """Search finds relevant documents."""
        index = BM25Index()
        index.add({"id": "1", "name": "Python async programming", "description": "Learn async"})
        index.add({"id": "2", "name": "JavaScript promises", "description": "Async JS"})
        index.add({"id": "3", "name": "Python web frameworks", "description": "Django Flask"})

        results = index.search("Python async")

        assert len(results) > 0
        # Python async doc should rank highest (matches both terms)
        top_result = results[0][0]
        assert top_result["id"] == "1"

    def test_bm25_index_empty_query(self) -> None:
        """Empty query returns empty results."""
        index = BM25Index()
        index.add({"id": "1", "name": "Python programming"})

        results = index.search("")
        assert results == []

    def test_bm25_index_stopwords_only_query(self) -> None:
        """Query with only stop words returns empty."""
        index = BM25Index()
        index.add({"id": "1", "name": "Python programming"})

        results = index.search("the is a")
        assert results == []

    def test_bm25_index_no_matches(self) -> None:
        """Query with no matches returns empty."""
        index = BM25Index()
        index.add({"id": "1", "name": "Python programming"})

        results = index.search("javascript react")
        assert results == []

    def test_bm25_index_remove_document(self) -> None:
        """Removing documents updates the index."""
        index = BM25Index()
        index.add({"id": "1", "name": "Python"})
        index.add({"id": "2", "name": "JavaScript"})

        assert index.size == 2
        removed = index.remove("1")
        assert removed is True
        assert index.size == 1

    def test_bm25_index_remove_nonexistent(self) -> None:
        """Removing nonexistent document returns False."""
        index = BM25Index()
        index.add({"id": "1", "name": "Python"})

        removed = index.remove("nonexistent")
        assert removed is False
        assert index.size == 1

    def test_bm25_index_clear(self) -> None:
        """Clear empties the entire index."""
        index = BM25Index()
        index.add({"id": "1", "name": "Python"})
        index.add({"id": "2", "name": "JavaScript"})

        index.clear()
        assert index.size == 0

    def test_bm25_index_update_document(self) -> None:
        """Re-adding a document updates it."""
        index = BM25Index()
        index.add({"id": "1", "name": "Python"})
        index.add({"id": "1", "name": "Python Programming Language"})

        assert index.size == 1
        results = index.search("programming language")
        assert len(results) == 1

    def test_bm25_index_min_score(self) -> None:
        """min_score filters low-scoring results."""
        index = BM25Index()
        index.add({"id": "1", "name": "Python programming async"})
        index.add({"id": "2", "name": "Other topic here"})

        # With high min_score, only strong matches pass
        results = index.search("Python programming", min_score=0.5)
        assert all(score >= 0.5 for _, score in results)

    def test_bm25_index_limit(self) -> None:
        """Limit restricts number of results."""
        index = BM25Index()
        for i in range(10):
            index.add({"id": str(i), "name": f"Python document number {i}"})

        results = index.search("Python", limit=3)
        assert len(results) == 3


class TestBM25ScoreCalculation:
    """Test BM25 score formula correctness."""

    def test_bm25_score_calculation(self) -> None:
        """Scores match expected BM25 formula."""
        index = BM25Index()

        # Add docs with known content
        index.add({"id": "1", "name": "python python python"})  # High TF
        index.add({"id": "2", "name": "python"})  # Low TF
        index.add({"id": "3", "name": "other content here"})  # No match

        results = index.search("python")

        # Doc with higher term frequency should score higher
        assert len(results) == 2
        scores = {r[0]["id"]: r[1] for r in results}
        assert scores["1"] > scores["2"]  # Higher TF = higher score

    def test_bm25_idf_effect(self) -> None:
        """Rare terms contribute more to score (IDF)."""
        index = BM25Index()

        # Add docs - 'python' appears in all, 'asyncio' is rare
        index.add({"id": "1", "name": "python asyncio concurrent"})
        index.add({"id": "2", "name": "python web framework"})
        index.add({"id": "3", "name": "python data science"})

        # Search for rare term should highlight doc1
        results = index.search("asyncio")
        assert len(results) == 1
        assert results[0][0]["id"] == "1"

    def test_bm25_length_normalization(self) -> None:
        """Longer documents are normalized appropriately."""
        config = BM25Config(b=0.75)  # Default length normalization
        index = BM25Index(config=config)

        # Short doc with term
        index.add({"id": "short", "name": "python"})
        # Long doc with same term
        index.add({"id": "long", "name": "python " + " ".join(["extra"] * 50)})

        results = index.search("python")
        scores = {r[0]["id"]: r[1] for r in results}

        # With length normalization, shorter doc scores relatively higher
        assert scores["short"] > scores["long"]


class TestGlobalBM25Index:
    """Test global BM25 index management."""

    def test_get_bm25_index_singleton(self) -> None:
        """get_bm25_index returns the same instance."""
        reset_bm25_index()
        idx1 = get_bm25_index()
        idx2 = get_bm25_index()
        assert idx1 is idx2

    def test_reset_bm25_index(self) -> None:
        """reset_bm25_index clears the global index."""
        reset_bm25_index()
        idx = get_bm25_index()
        idx.add({"id": "1", "name": "test"})
        assert idx.size == 1

        reset_bm25_index()
        new_idx = get_bm25_index()
        assert new_idx.size == 0
        assert new_idx is not idx


# =============================================================================
# Deduplication Tests
# =============================================================================


class TestCosineSimilarity:
    """Test cosine similarity calculation."""

    def test_cosine_identical_vectors(self) -> None:
        """Identical vectors have similarity 1.0."""
        vec = [1.0, 2.0, 3.0]
        assert cosine_similarity(vec, vec) == pytest.approx(1.0)

    def test_cosine_orthogonal_vectors(self) -> None:
        """Orthogonal vectors have similarity 0.0."""
        vec1 = [1.0, 0.0, 0.0]
        vec2 = [0.0, 1.0, 0.0]
        assert cosine_similarity(vec1, vec2) == pytest.approx(0.0)

    def test_cosine_opposite_vectors(self) -> None:
        """Opposite vectors have similarity -1.0."""
        vec1 = [1.0, 0.0]
        vec2 = [-1.0, 0.0]
        assert cosine_similarity(vec1, vec2) == pytest.approx(-1.0)

    def test_cosine_empty_vectors(self) -> None:
        """Empty vectors return 0.0."""
        assert cosine_similarity([], []) == 0.0

    def test_cosine_mismatched_length(self) -> None:
        """Mismatched vector lengths return 0.0."""
        vec1 = [1.0, 2.0]
        vec2 = [1.0, 2.0, 3.0]
        assert cosine_similarity(vec1, vec2) == 0.0

    def test_cosine_zero_vector(self) -> None:
        """Zero vector returns 0.0."""
        vec1 = [0.0, 0.0, 0.0]
        vec2 = [1.0, 2.0, 3.0]
        assert cosine_similarity(vec1, vec2) == 0.0

    def test_cosine_similar_vectors(self) -> None:
        """Similar vectors have high similarity."""
        vec1 = [1.0, 2.0, 3.0]
        vec2 = [1.1, 2.1, 3.1]
        sim = cosine_similarity(vec1, vec2)
        assert sim > 0.99  # Very similar


class TestJaccardSimilarity:
    """Test Jaccard similarity for strings."""

    def test_jaccard_identical_strings(self) -> None:
        """Identical strings have similarity 1.0."""
        assert jaccard_similarity("hello world", "hello world") == 1.0

    def test_jaccard_disjoint_strings(self) -> None:
        """Completely different strings have similarity 0.0."""
        assert jaccard_similarity("hello world", "foo bar") == 0.0

    def test_jaccard_partial_overlap(self) -> None:
        """Partial overlap gives expected similarity."""
        # "hello world" vs "hello there" - intersection: {hello}, union: {hello, world, there}
        sim = jaccard_similarity("hello world", "hello there")
        assert sim == pytest.approx(1 / 3)

    def test_jaccard_empty_strings(self) -> None:
        """Both empty returns 1.0."""
        assert jaccard_similarity("", "") == 1.0

    def test_jaccard_one_empty(self) -> None:
        """One empty string returns 0.0."""
        assert jaccard_similarity("hello", "") == 0.0
        assert jaccard_similarity("", "world") == 0.0

    def test_jaccard_case_insensitive(self) -> None:
        """Comparison is case-insensitive."""
        assert jaccard_similarity("HELLO World", "hello WORLD") == 1.0


class TestDedupConfig:
    """Test DedupConfig defaults."""

    def test_dedup_config_defaults(self) -> None:
        """DedupConfig has sensible defaults."""
        config = DedupConfig()
        assert config.similarity_threshold == 0.95
        assert config.batch_size == 100
        assert config.same_type_only is True
        assert config.min_name_overlap == 0.3

    def test_dedup_config_custom(self) -> None:
        """DedupConfig accepts custom values."""
        config = DedupConfig(
            similarity_threshold=0.9,
            batch_size=50,
            same_type_only=False,
            min_name_overlap=0.5,
        )
        assert config.similarity_threshold == 0.9
        assert config.batch_size == 50
        assert config.same_type_only is False
        assert config.min_name_overlap == 0.5


class TestDuplicatePair:
    """Test DuplicatePair dataclass."""

    def test_duplicate_pair_creation(self) -> None:
        """DuplicatePair can be created with all fields."""
        pair = DuplicatePair(
            entity1_id="id1",
            entity2_id="id2",
            similarity=0.98,
            entity1_name="Entity One",
            entity2_name="Entity Two",
            entity_type="concept",
            suggested_keep="id1",
        )
        assert pair.entity1_id == "id1"
        assert pair.similarity == 0.98

    def test_duplicate_pair_to_dict(self) -> None:
        """to_dict serializes correctly."""
        pair = DuplicatePair(
            entity1_id="id1",
            entity2_id="id2",
            similarity=0.987654,
            entity1_name="Name 1",
            entity2_name="Name 2",
            entity_type="pattern",
            suggested_keep="id2",
        )
        d = pair.to_dict()
        assert d["entity1_id"] == "id1"
        assert d["similarity"] == 0.9877  # Rounded to 4 decimals
        assert d["suggested_keep"] == "id2"


# =============================================================================
# Fusion Tests
# =============================================================================


class TestRRFScore:
    """Test RRF score calculation."""

    def test_rrf_score_rank_one(self) -> None:
        """Rank 1 produces expected score."""
        # score = 1 / (60 + 1) = 1/61
        score = rrf_score(1, k=60.0)
        assert score == pytest.approx(1 / 61)

    def test_rrf_score_higher_rank(self) -> None:
        """Higher ranks produce lower scores."""
        score_1 = rrf_score(1, k=60.0)
        score_10 = rrf_score(10, k=60.0)
        score_100 = rrf_score(100, k=60.0)

        assert score_1 > score_10 > score_100

    def test_rrf_k_parameter(self) -> None:
        """k parameter affects score distribution."""
        # Lower k = more weight on top ranks
        score_low_k = rrf_score(1, k=10.0)
        score_high_k = rrf_score(1, k=100.0)

        assert score_low_k > score_high_k


class TestDefaultDedupKey:
    """Test default_dedup_key extraction."""

    def test_dedup_key_dict_id(self) -> None:
        """Extracts 'id' from dict."""
        entity = {"id": "abc123", "name": "Test"}
        assert default_dedup_key(entity) == "abc123"

    def test_dedup_key_dict_uuid(self) -> None:
        """Falls back to 'uuid' if no 'id'."""
        entity = {"uuid": "uuid-456", "name": "Test"}
        assert default_dedup_key(entity) == "uuid-456"

    def test_dedup_key_object_id(self) -> None:
        """Extracts 'id' from object."""

        @dataclass
        class Entity:
            id: str
            name: str

        entity = Entity(id="obj123", name="Test")
        assert default_dedup_key(entity) == "obj123"


class TestRRFMerge:
    """Test RRF merge functionality."""

    def test_rrf_basic(self) -> None:
        """Basic merge of two result sets."""
        list1 = [({"id": "a"}, 0.9), ({"id": "b"}, 0.8)]
        list2 = [({"id": "b"}, 0.95), ({"id": "c"}, 0.85)]

        merged = rrf_merge([list1, list2])

        # "b" appears in both lists, should rank highly
        ids = [r[0]["id"] for r in merged]
        assert "b" in ids
        assert "a" in ids
        assert "c" in ids

        # "b" should be first (appears in both at good ranks)
        assert merged[0][0]["id"] == "b"

    def test_rrf_weights(self) -> None:
        """Weights affect final ranking."""
        # Same entity at same rank in both lists
        list1 = [({"id": "a"}, 0.9)]
        list2 = [({"id": "b"}, 0.9)]

        # Equal weights - both should have same RRF score
        merged_equal = rrf_merge([list1, list2], weights=[1.0, 1.0])
        scores_equal = {r[0]["id"]: r[1] for r in merged_equal}
        assert scores_equal["a"] == pytest.approx(scores_equal["b"])

        # Heavily weight first list
        merged_weighted = rrf_merge([list1, list2], weights=[10.0, 1.0])
        scores_weighted = {r[0]["id"]: r[1] for r in merged_weighted}
        assert scores_weighted["a"] > scores_weighted["b"]

    def test_rrf_empty_lists(self) -> None:
        """Empty input returns empty output."""
        assert rrf_merge([]) == []
        assert rrf_merge([[]]) == []
        assert rrf_merge([[], []]) == []

    def test_rrf_single_list(self) -> None:
        """Single list is returned with RRF scores."""
        list1 = [({"id": "a"}, 0.9), ({"id": "b"}, 0.8)]
        merged = rrf_merge([list1])

        assert len(merged) == 2
        assert merged[0][0]["id"] == "a"
        assert merged[1][0]["id"] == "b"

    def test_rrf_disjoint_sets(self) -> None:
        """Merges non-overlapping result sets."""
        list1 = [({"id": "a"}, 0.9), ({"id": "b"}, 0.8)]
        list2 = [({"id": "c"}, 0.95), ({"id": "d"}, 0.85)]

        merged = rrf_merge([list1, list2])

        # All entities should be present
        ids = {r[0]["id"] for r in merged}
        assert ids == {"a", "b", "c", "d"}

    def test_rrf_k_parameter_effect(self) -> None:
        """k constant affects score magnitude."""
        list1 = [({"id": "a"}, 0.9)]

        merged_low_k = rrf_merge([list1], k=10.0)
        merged_high_k = rrf_merge([list1], k=100.0)

        # Lower k gives higher absolute scores
        assert merged_low_k[0][1] > merged_high_k[0][1]

    def test_rrf_limit(self) -> None:
        """Limit restricts output size."""
        list1 = [({"id": str(i)}, 0.9 - i * 0.1) for i in range(10)]

        merged = rrf_merge([list1], limit=3)
        assert len(merged) == 3

    def test_rrf_preserves_first_entity(self) -> None:
        """When same entity in multiple lists, first occurrence is kept."""
        entity_v1 = {"id": "a", "version": 1}
        entity_v2 = {"id": "a", "version": 2}

        list1 = [(entity_v1, 0.9)]
        list2 = [(entity_v2, 0.8)]

        merged = rrf_merge([list1, list2])
        assert merged[0][0]["version"] == 1  # First occurrence kept


class TestRRFMergeWithMetadata:
    """Test RRF merge with source metadata."""

    def test_rrf_with_metadata_sources(self) -> None:
        """Metadata includes source list names."""
        list1 = [({"id": "a"}, 0.9)]
        list2 = [({"id": "a"}, 0.85)]

        merged = rrf_merge_with_metadata(
            [list1, list2],
            list_names=["vector", "graph"],
        )

        entity, _score, meta = merged[0]
        assert entity["id"] == "a"
        assert "vector" in meta["sources"]
        assert "graph" in meta["sources"]
        assert meta["ranks"]["vector"] == 1
        assert meta["ranks"]["graph"] == 1
        assert meta["original_scores"]["vector"] == 0.9
        assert meta["original_scores"]["graph"] == 0.85


class TestWeightedScoreMerge:
    """Test weighted score merge (alternative to RRF)."""

    def test_weighted_merge_basic(self) -> None:
        """Basic weighted merge combines scores."""
        list1 = [({"id": "a"}, 0.9)]
        list2 = [({"id": "b"}, 0.8)]

        merged = weighted_score_merge([list1, list2])
        assert len(merged) == 2

    def test_weighted_merge_same_entity(self) -> None:
        """Same entity scores are averaged across lists."""
        # Use multiple items per list so normalization is meaningful
        list1 = [({"id": "a"}, 1.0), ({"id": "b"}, 0.5)]
        list2 = [({"id": "a"}, 0.8), ({"id": "c"}, 0.4)]

        merged = weighted_score_merge([list1, list2], normalize=True)
        # Entity "a" appears in both lists and should have combined score
        ids = {r[0]["id"] for r in merged}
        assert "a" in ids
        assert "b" in ids
        assert "c" in ids

    def test_weighted_merge_no_normalize(self) -> None:
        """Can skip normalization for raw scores."""
        list1 = [({"id": "a"}, 0.9)]
        list2 = [({"id": "a"}, 0.5)]

        merged = weighted_score_merge([list1, list2], normalize=False)
        # Average of 0.9 and 0.5 = 0.7
        assert merged[0][1] == pytest.approx(0.7)

    def test_weighted_merge_empty(self) -> None:
        """Empty lists handled gracefully."""
        assert weighted_score_merge([]) == []
        assert weighted_score_merge([[], []]) == []


class TestFusionConfig:
    """Test FusionConfig defaults."""

    def test_fusion_config_defaults(self) -> None:
        """FusionConfig has expected defaults."""
        config = FusionConfig()
        assert config.k == 60.0
        assert config.weights is None
        assert config.dedup_key is None


# =============================================================================
# Temporal Decay Tests
# =============================================================================


class TestTemporalConfig:
    """Test TemporalConfig defaults."""

    def test_temporal_config_defaults(self) -> None:
        """TemporalConfig has sensible defaults."""
        config = TemporalConfig()
        assert config.decay_days == 365.0
        assert config.min_boost == 0.1
        assert config.max_age_days == 1825.0  # 5 years
        assert config.timestamp_field == "auto"

    def test_temporal_config_custom(self) -> None:
        """TemporalConfig accepts custom values."""
        config = TemporalConfig(
            decay_days=30.0,
            min_boost=0.05,
            max_age_days=365.0,
            timestamp_field="created_at",
        )
        assert config.decay_days == 30.0
        assert config.min_boost == 0.05


class TestGetEntityTimestamp:
    """Test timestamp extraction from entities."""

    def test_timestamp_from_dict_created_at(self) -> None:
        """Extract created_at from dict."""
        now = datetime.now(UTC)
        entity = {"id": "1", "created_at": now}
        ts = get_entity_timestamp(entity, field="created_at")
        assert ts == now

    def test_timestamp_from_dict_valid_from(self) -> None:
        """Extract valid_from from dict."""
        now = datetime.now(UTC)
        entity = {"id": "1", "valid_from": now}
        ts = get_entity_timestamp(entity, field="valid_from")
        assert ts == now

    def test_timestamp_auto_prefers_valid_from(self) -> None:
        """Auto mode prefers valid_from over created_at."""
        valid = datetime(2024, 1, 1, tzinfo=UTC)
        created = datetime(2023, 1, 1, tzinfo=UTC)
        entity = {"valid_from": valid, "created_at": created}
        ts = get_entity_timestamp(entity, field="auto")
        assert ts == valid

    def test_timestamp_auto_prefers_valid_at_metadata(self) -> None:
        """Auto mode prefers valid_at metadata over created_at."""
        created = datetime(2023, 1, 1, tzinfo=UTC)
        valid_at = datetime(2024, 2, 5, 12, 0, tzinfo=UTC)
        entity = {"created_at": created, "metadata": {"valid_at": "2024/02/05 12:00"}}
        ts = get_entity_timestamp(entity, field="auto")
        assert ts == valid_at

    def test_timestamp_from_metadata(self) -> None:
        """Extract timestamp from metadata dict."""
        now = datetime.now(UTC)
        entity = {"id": "1", "metadata": {"created_at": now}}
        ts = get_entity_timestamp(entity, field="created_at")
        assert ts == now

    def test_timestamp_from_string(self) -> None:
        """Parse ISO string timestamp."""
        entity = {"created_at": "2024-06-15T12:00:00+00:00"}
        ts = get_entity_timestamp(entity, field="created_at")
        assert ts is not None
        assert ts.year == 2024
        assert ts.month == 6

    def test_parse_temporal_datetime_accepts_longmemeval_format(self) -> None:
        """Parse LongMemEval timestamps with weekday markers."""
        parsed = parse_temporal_datetime("2025/05/17 (Sat) 09:30")
        assert parsed == datetime(2025, 5, 17, 9, 30, tzinfo=UTC)

    def test_parse_temporal_datetime_rejects_timezone_parenthetical(self) -> None:
        """Do not mistake timezone annotations for weekday markers."""
        parsed = parse_temporal_datetime("2025/05/17 (PST) 09:30")
        assert parsed is None

    def test_timestamp_missing(self) -> None:
        """Missing timestamp returns None."""
        entity = {"id": "1", "name": "test"}
        ts = get_entity_timestamp(entity, field="created_at")
        assert ts is None

    def test_resolve_temporal_reference_with_words_and_weekday(self) -> None:
        """Resolve relative query time against the question as-of time."""
        reference = datetime(2026, 3, 28, 12, 0, tzinfo=UTC)
        target = resolve_temporal_reference(
            "What did I do with Rachel on the Wednesday two months ago?",
            reference,
        )

        assert target == datetime(2026, 1, 28, 12, 0, tzinfo=UTC)


class TestCalculateAgeDays:
    """Test age calculation in days."""

    def test_age_days_recent(self) -> None:
        """Recent timestamp gives small age."""
        now = datetime.now(UTC)
        yesterday = now - timedelta(days=1)
        age = calculate_age_days(yesterday, reference=now)
        assert age == pytest.approx(1.0, rel=0.01)

    def test_age_days_old(self) -> None:
        """Old timestamp gives large age."""
        now = datetime.now(UTC)
        year_ago = now - timedelta(days=365)
        age = calculate_age_days(year_ago, reference=now)
        assert age == pytest.approx(365.0, rel=0.01)

    def test_age_days_future(self) -> None:
        """Future timestamp gives 0 age (clamped)."""
        now = datetime.now(UTC)
        future = now + timedelta(days=10)
        age = calculate_age_days(future, reference=now)
        assert age == 0.0

    def test_age_days_timezone_handling(self) -> None:
        """Handles timezone-naive timestamps."""
        now = datetime.now(UTC)
        naive = datetime(2024, 1, 1, 12, 0, 0)  # No timezone
        # Should not raise, adds UTC
        age = calculate_age_days(naive, reference=now)
        assert age >= 0


class TestCalculateBoost:
    """Test boost factor calculation."""

    def test_boost_age_zero(self) -> None:
        """Zero age gives boost of 1.0."""
        boost = calculate_boost(0.0, decay_days=365.0)
        assert boost == pytest.approx(1.0)

    def test_boost_decay_days(self) -> None:
        """At decay_days age, boost is ~0.368 (1/e)."""
        boost = calculate_boost(365.0, decay_days=365.0)
        expected = math.exp(-1)  # ~0.368
        assert boost == pytest.approx(expected, rel=0.01)

    def test_boost_very_old(self) -> None:
        """Very old items get min_boost."""
        boost = calculate_boost(
            age_days=2000.0,
            decay_days=365.0,
            min_boost=0.1,
            max_age_days=1825.0,
        )
        assert boost == 0.1

    def test_boost_min_clamp(self) -> None:
        """Boost is clamped to min_boost."""
        # Even with very high age before max, min_boost is enforced
        boost = calculate_boost(
            age_days=1000.0,
            decay_days=100.0,  # Very fast decay
            min_boost=0.2,
        )
        assert boost >= 0.2


class TestTemporalBoost:
    """Test temporal_boost function on result lists."""

    def test_temporal_decay_recent(self) -> None:
        """Recent items maintain high scores."""
        now = datetime.now(UTC)
        recent = now - timedelta(days=1)

        results = [
            ({"id": "1", "created_at": recent}, 1.0),
        ]

        boosted = temporal_boost(
            results,
            decay_days=365.0,
            reference_time=now,
        )

        # Very recent - boost should be near 1.0
        assert boosted[0][1] > 0.99

    def test_temporal_decay_old(self) -> None:
        """Old items get penalized."""
        now = datetime.now(UTC)
        old = now - timedelta(days=365 * 3)  # 3 years old

        results = [
            ({"id": "1", "created_at": old}, 1.0),
        ]

        boosted = temporal_boost(
            results,
            decay_days=365.0,
            min_boost=0.1,
            reference_time=now,
        )

        # 3 years old with 1-year decay = boost of e^(-3) ~= 0.05, clamped to 0.1
        assert boosted[0][1] < 0.2

    def test_temporal_decay_now(self) -> None:
        """Current time items get full score (boost = 1.0)."""
        now = datetime.now(UTC)

        results = [
            ({"id": "1", "created_at": now}, 0.8),
        ]

        boosted = temporal_boost(
            results,
            decay_days=365.0,
            reference_time=now,
        )

        assert boosted[0][1] == pytest.approx(0.8, rel=0.01)

    def test_temporal_decay_config(self) -> None:
        """Respects decay parameters."""
        now = datetime.now(UTC)
        week_ago = now - timedelta(days=7)

        results = [({"id": "1", "created_at": week_ago}, 1.0)]

        # Fast decay (7-day half-life)
        fast_decay = temporal_boost(
            results,
            decay_days=7.0,
            reference_time=now,
        )

        # Slow decay (365-day half-life)
        slow_decay = temporal_boost(
            results,
            decay_days=365.0,
            reference_time=now,
        )

        # Fast decay should reduce score more
        assert fast_decay[0][1] < slow_decay[0][1]

    def test_temporal_boost_empty(self) -> None:
        """Empty input returns empty output."""
        assert temporal_boost([]) == []

    def test_temporal_boost_no_timestamp(self) -> None:
        """Items without timestamps keep original score."""
        results = [({"id": "1", "name": "no timestamp"}, 0.9)]

        boosted = temporal_boost(results, decay_days=30.0)
        assert boosted[0][1] == 0.9

    def test_temporal_boost_reorders(self) -> None:
        """Results are re-sorted by boosted score."""
        now = datetime.now(UTC)
        recent = now - timedelta(days=1)
        old = now - timedelta(days=365)

        # Old item has higher original score
        results = [
            ({"id": "old", "created_at": old}, 1.0),
            ({"id": "recent", "created_at": recent}, 0.7),
        ]

        boosted = temporal_boost(
            results,
            decay_days=30.0,  # Fast decay
            reference_time=now,
        )

        # Recent item should now rank first despite lower original score
        assert boosted[0][0]["id"] == "recent"

    def test_temporal_proximity_boost_reorders_by_query_time(self) -> None:
        """Explicit temporal intent boosts records near the target time."""
        target = datetime(2026, 1, 10, tzinfo=UTC)
        results = [
            ({"id": "far", "metadata": {"valid_at": "2026/01/01 00:00"}}, 1.0),
            ({"id": "near", "metadata": {"valid_at": "2026/01/11 00:00"}}, 0.8),
        ]

        boosted = temporal_proximity_boost(results, target_time=target)

        assert boosted[0][0]["id"] == "near"


class TestTemporalBoostSingle:
    """Test single-entity temporal boosting."""

    def test_temporal_boost_single_recent(self) -> None:
        """Recent entity gets near-full score."""
        now = datetime.now(UTC)
        entity = {"created_at": now - timedelta(hours=1)}

        boosted = temporal_boost_single(
            entity,
            score=1.0,
            reference_time=now,
        )

        assert boosted > 0.99

    def test_temporal_boost_single_no_timestamp(self) -> None:
        """Entity without timestamp returns original score."""
        entity = {"id": "1", "name": "test"}

        boosted = temporal_boost_single(entity, score=0.8)
        assert boosted == 0.8

    def test_temporal_boost_single_config(self) -> None:
        """Custom config is respected."""
        now = datetime.now(UTC)
        entity = {"created_at": now - timedelta(days=30)}

        config = TemporalConfig(decay_days=30.0, min_boost=0.5)

        boosted = temporal_boost_single(
            entity,
            score=1.0,
            config=config,
            reference_time=now,
        )

        # At decay_days age, boost = 1/e ~= 0.368
        # Since 1/e < min_boost (0.5), result is clamped to min_boost
        assert boosted == pytest.approx(0.5, rel=0.01)


# =============================================================================
# Integration / Edge Case Tests
# =============================================================================


class TestLargeDatasets:
    """Test behavior with larger datasets."""

    def test_bm25_large_corpus(self) -> None:
        """BM25 handles large document sets."""
        index = BM25Index()

        # Add 1000 documents
        for i in range(1000):
            index.add({"id": str(i), "name": f"Document {i} about topic {i % 10}"})

        assert index.size == 1000

        # Search should still work
        results = index.search("topic 5", limit=20)
        assert len(results) <= 20
        assert len(results) > 0

    def test_rrf_many_lists(self) -> None:
        """RRF handles many result lists."""
        lists = []
        for _ in range(10):
            list_i = [({"id": f"doc{j}"}, 1.0 - j * 0.1) for j in range(5)]
            lists.append(list_i)

        merged = rrf_merge(lists)
        assert len(merged) == 5  # 5 unique docs

    def test_temporal_boost_large_list(self) -> None:
        """Temporal boost handles large result sets."""
        now = datetime.now(UTC)
        results = []
        for i in range(500):
            age = timedelta(days=i)
            results.append(({"id": str(i), "created_at": now - age}, 1.0))

        boosted = temporal_boost(results, decay_days=365.0, reference_time=now)
        assert len(boosted) == 500

        # Most recent should be first
        assert boosted[0][0]["id"] == "0"


class TestEdgeCases:
    """Test edge cases and boundary conditions."""

    def test_bm25_single_character_token(self) -> None:
        """Single character tokens filtered by default min_length=2."""
        index = BM25Index()
        index.add({"id": "1", "name": "I am here"})

        # "I" is filtered, "am" passes (length 2)
        results = index.search("I")
        assert results == []

    def test_rrf_negative_scores(self) -> None:
        """RRF ignores original scores (uses rank only)."""
        list1 = [({"id": "a"}, -0.5), ({"id": "b"}, -0.9)]

        merged = rrf_merge([list1])
        # Order preserved by rank, not score
        assert merged[0][0]["id"] == "a"
        assert merged[1][0]["id"] == "b"

    def test_cosine_very_small_vectors(self) -> None:
        """Very small magnitude vectors handled."""
        vec1 = [1e-10, 1e-10]
        vec2 = [1e-10, 1e-10]
        sim = cosine_similarity(vec1, vec2)
        assert sim == pytest.approx(1.0, rel=0.01)

    def test_temporal_boost_exact_max_age(self) -> None:
        """Entity at exactly max_age_days gets min_boost."""
        now = datetime.now(UTC)
        entity = {"created_at": now - timedelta(days=1825)}  # Exactly 5 years

        boosted = temporal_boost_single(
            entity,
            score=1.0,
            config=TemporalConfig(max_age_days=1825.0, min_boost=0.1),
            reference_time=now,
        )

        assert boosted == pytest.approx(0.1)
