"""Tests for entity deduplication module."""

import math
from unittest.mock import AsyncMock, MagicMock

import pytest

from sibyl.retrieval.dedup import (
    DedupConfig,
    DuplicatePair,
    EntityDeduplicator,
    cosine_similarity,
    jaccard_similarity,
)


class TestCosineSimilarity:
    """Tests for cosine similarity calculation."""

    def test_identical_vectors(self) -> None:
        """Identical vectors have similarity 1.0."""
        vec = [1.0, 2.0, 3.0]
        assert cosine_similarity(vec, vec) == pytest.approx(1.0)

    def test_orthogonal_vectors(self) -> None:
        """Orthogonal vectors have similarity 0.0."""
        vec1 = [1.0, 0.0, 0.0]
        vec2 = [0.0, 1.0, 0.0]
        assert cosine_similarity(vec1, vec2) == pytest.approx(0.0)

    def test_opposite_vectors(self) -> None:
        """Opposite vectors have similarity -1.0."""
        vec1 = [1.0, 0.0, 0.0]
        vec2 = [-1.0, 0.0, 0.0]
        assert cosine_similarity(vec1, vec2) == pytest.approx(-1.0)

    def test_similar_vectors(self) -> None:
        """Similar vectors have high similarity."""
        vec1 = [1.0, 2.0, 3.0]
        vec2 = [1.1, 2.1, 3.1]  # Slightly different
        sim = cosine_similarity(vec1, vec2)
        assert sim > 0.99

    def test_different_lengths(self) -> None:
        """Different length vectors return 0.0."""
        vec1 = [1.0, 2.0]
        vec2 = [1.0, 2.0, 3.0]
        assert cosine_similarity(vec1, vec2) == 0.0

    def test_empty_vectors(self) -> None:
        """Empty vectors return 0.0."""
        assert cosine_similarity([], []) == 0.0

    def test_zero_vector(self) -> None:
        """Zero vector returns 0.0."""
        vec1 = [0.0, 0.0, 0.0]
        vec2 = [1.0, 2.0, 3.0]
        assert cosine_similarity(vec1, vec2) == 0.0

    def test_normalized_vectors(self) -> None:
        """Test with normalized vectors."""
        # Create unit vectors
        vec1 = [1.0 / math.sqrt(2), 1.0 / math.sqrt(2), 0.0]
        vec2 = [1.0 / math.sqrt(2), 0.0, 1.0 / math.sqrt(2)]
        # Dot product of these should be 0.5
        assert cosine_similarity(vec1, vec2) == pytest.approx(0.5)


class TestJaccardSimilarity:
    """Tests for Jaccard similarity calculation."""

    def test_identical_strings(self) -> None:
        """Identical strings have similarity 1.0."""
        assert jaccard_similarity("hello world", "hello world") == 1.0

    def test_completely_different(self) -> None:
        """Completely different strings have similarity 0.0."""
        assert jaccard_similarity("hello world", "foo bar") == 0.0

    def test_partial_overlap(self) -> None:
        """Partial overlap gives expected similarity."""
        # "hello" overlaps, "world" and "there" differ
        # Jaccard = 1 / 3 = 0.333...
        sim = jaccard_similarity("hello world", "hello there")
        assert sim == pytest.approx(1.0 / 3.0)

    def test_case_insensitive(self) -> None:
        """Comparison is case-insensitive."""
        assert jaccard_similarity("Hello World", "hello world") == 1.0

    def test_empty_strings(self) -> None:
        """Empty strings have similarity 1.0."""
        assert jaccard_similarity("", "") == 1.0

    def test_one_empty(self) -> None:
        """One empty string has similarity 0.0."""
        assert jaccard_similarity("hello", "") == 0.0
        assert jaccard_similarity("", "world") == 0.0


class TestDuplicatePair:
    """Tests for DuplicatePair dataclass."""

    def test_to_dict(self) -> None:
        """Test dictionary conversion."""
        pair = DuplicatePair(
            entity1_id="id1",
            entity2_id="id2",
            similarity=0.987654321,
            entity1_name="Entity One",
            entity2_name="Entity Two",
            entity_type="pattern",
            suggested_keep="id1",
        )

        d = pair.to_dict()

        assert d["entity1_id"] == "id1"
        assert d["entity2_id"] == "id2"
        assert d["similarity"] == 0.9877  # Rounded to 4 decimal places
        assert d["entity1_name"] == "Entity One"
        assert d["entity2_name"] == "Entity Two"
        assert d["entity_type"] == "pattern"
        assert d["suggested_keep"] == "id1"

    def test_default_values(self) -> None:
        """Test default field values."""
        pair = DuplicatePair(
            entity1_id="id1",
            entity2_id="id2",
            similarity=0.95,
        )

        assert pair.entity1_name == ""
        assert pair.entity2_name == ""
        assert pair.entity_type == ""
        assert pair.suggested_keep == ""


class TestDedupConfig:
    """Tests for DedupConfig dataclass."""

    def test_default_values(self) -> None:
        """Test default configuration values."""
        config = DedupConfig()

        assert config.similarity_threshold == 0.95
        assert config.batch_size == 100
        assert config.same_type_only is True
        assert config.min_name_overlap == 0.3

    def test_custom_values(self) -> None:
        """Test custom configuration values."""
        config = DedupConfig(
            similarity_threshold=0.98,
            batch_size=50,
            same_type_only=False,
            min_name_overlap=0.0,
        )

        assert config.similarity_threshold == 0.98
        assert config.batch_size == 50
        assert config.same_type_only is False
        assert config.min_name_overlap == 0.0


class TestEntityDeduplicator:
    """Tests for EntityDeduplicator class."""

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

    @pytest.fixture
    def dedup(self, mock_client: MagicMock, mock_entity_manager: MagicMock) -> EntityDeduplicator:
        """Create EntityDeduplicator with mocks."""
        return EntityDeduplicator(
            client=mock_client,
            entity_manager=mock_entity_manager,
            config=DedupConfig(min_name_overlap=0.0),  # Disable name overlap check
        )

    @pytest.mark.asyncio
    async def test_find_duplicates_empty(self, dedup: EntityDeduplicator) -> None:
        """Empty entity list returns empty duplicates."""
        pairs = await dedup.find_duplicates()
        assert pairs == []

    @pytest.mark.asyncio
    async def test_find_duplicates_single_entity(
        self, dedup: EntityDeduplicator, mock_client: MagicMock
    ) -> None:
        """Single entity returns empty duplicates."""
        # Return single entity with embedding
        mock_client.client.driver.execute_query = AsyncMock(
            return_value=[
                ("id1", "Entity One", "pattern", [1.0, 0.0, 0.0])
            ]
        )

        pairs = await dedup.find_duplicates()
        assert pairs == []

    @pytest.mark.asyncio
    async def test_find_duplicates_no_matches(
        self, dedup: EntityDeduplicator, mock_client: MagicMock
    ) -> None:
        """Different entities return empty duplicates."""
        # Two orthogonal embeddings = 0 similarity
        mock_client.client.driver.execute_query = AsyncMock(
            return_value=[
                ("id1", "Entity One", "pattern", [1.0, 0.0, 0.0]),
                ("id2", "Entity Two", "pattern", [0.0, 1.0, 0.0]),
            ]
        )

        pairs = await dedup.find_duplicates()
        assert pairs == []

    @pytest.mark.asyncio
    async def test_find_duplicates_matching_pair(
        self, dedup: EntityDeduplicator, mock_client: MagicMock
    ) -> None:
        """Similar entities are detected as duplicates."""
        # Nearly identical embeddings
        mock_client.client.driver.execute_query = AsyncMock(
            return_value=[
                ("id1", "Error handling", "pattern", [1.0, 0.0, 0.0]),
                ("id2", "Error handling pattern", "pattern", [0.99, 0.01, 0.0]),
            ]
        )

        pairs = await dedup.find_duplicates(threshold=0.9)

        assert len(pairs) == 1
        assert pairs[0].entity1_id == "id1"
        assert pairs[0].entity2_id == "id2"
        assert pairs[0].similarity > 0.9

    @pytest.mark.asyncio
    async def test_find_duplicates_same_type_only(
        self, dedup: EntityDeduplicator, mock_client: MagicMock
    ) -> None:
        """Different entity types are not compared when same_type_only=True."""
        dedup.config.same_type_only = True

        mock_client.client.driver.execute_query = AsyncMock(
            return_value=[
                ("id1", "Error handling", "pattern", [1.0, 0.0, 0.0]),
                ("id2", "Error handling", "rule", [1.0, 0.0, 0.0]),  # Same embedding, different type
            ]
        )

        pairs = await dedup.find_duplicates(threshold=0.9)
        assert len(pairs) == 0

    @pytest.mark.asyncio
    async def test_find_duplicates_cross_type(
        self, mock_client: MagicMock, mock_entity_manager: MagicMock
    ) -> None:
        """Different entity types are compared when same_type_only=False."""
        dedup = EntityDeduplicator(
            client=mock_client,
            entity_manager=mock_entity_manager,
            config=DedupConfig(same_type_only=False, min_name_overlap=0.0),
        )

        mock_client.client.driver.execute_query = AsyncMock(
            return_value=[
                ("id1", "Error handling", "pattern", [1.0, 0.0, 0.0]),
                ("id2", "Error handling", "rule", [1.0, 0.0, 0.0]),  # Same embedding
            ]
        )

        pairs = await dedup.find_duplicates(threshold=0.9)
        assert len(pairs) == 1

    @pytest.mark.asyncio
    async def test_find_duplicates_sorted_by_similarity(
        self, dedup: EntityDeduplicator, mock_client: MagicMock
    ) -> None:
        """Results are sorted by similarity (highest first)."""
        mock_client.client.driver.execute_query = AsyncMock(
            return_value=[
                ("id1", "Entity A", "pattern", [1.0, 0.0, 0.0]),
                ("id2", "Entity B", "pattern", [0.95, 0.05, 0.0]),  # Lower similarity to id1
                ("id3", "Entity C", "pattern", [0.99, 0.01, 0.0]),  # Higher similarity to id1
            ]
        )

        pairs = await dedup.find_duplicates(threshold=0.9)

        assert len(pairs) >= 1
        # First pair should have highest similarity
        if len(pairs) > 1:
            assert pairs[0].similarity >= pairs[1].similarity

    def test_suggest_merges_returns_cached(self, dedup: EntityDeduplicator) -> None:
        """suggest_merges returns cached duplicate pairs."""
        # Manually set cached pairs
        dedup._duplicate_pairs = [
            DuplicatePair("id1", "id2", 0.98),
            DuplicatePair("id3", "id4", 0.95),
        ]

        merges = dedup.suggest_merges()
        assert len(merges) == 2
        assert merges[0].similarity == 0.98

    @pytest.mark.asyncio
    async def test_merge_entities_success(
        self, dedup: EntityDeduplicator, mock_entity_manager: MagicMock, mock_client: MagicMock
    ) -> None:
        """Successful merge removes entity and redirects relationships."""
        # Setup entity returns
        keep_entity = MagicMock()
        keep_entity.metadata = {"existing": "data"}
        remove_entity = MagicMock()
        remove_entity.metadata = {"remove": "data"}

        mock_entity_manager.get = AsyncMock(
            side_effect=lambda eid: keep_entity if eid == "keep" else remove_entity
        )

        # Add pair to cache
        dedup._duplicate_pairs = [DuplicatePair("keep", "remove", 0.98)]

        result = await dedup.merge_entities(keep_id="keep", remove_id="remove")

        assert result is True
        # Entity manager delete should be called
        mock_entity_manager.delete.assert_called_once_with("remove")
        # Pair should be removed from cache
        assert len(dedup._duplicate_pairs) == 0

    @pytest.mark.asyncio
    async def test_merge_entities_not_found(
        self, dedup: EntityDeduplicator, mock_entity_manager: MagicMock
    ) -> None:
        """Merge fails if entity not found."""
        mock_entity_manager.get = AsyncMock(return_value=None)

        result = await dedup.merge_entities(keep_id="keep", remove_id="remove")

        assert result is False
        mock_entity_manager.delete.assert_not_called()

    @pytest.mark.asyncio
    async def test_merge_entities_preserves_keep_metadata(
        self, dedup: EntityDeduplicator, mock_entity_manager: MagicMock, mock_client: MagicMock
    ) -> None:
        """Keep entity's metadata takes precedence during merge."""
        keep_entity = MagicMock()
        keep_entity.metadata = {"shared_key": "keep_value", "keep_only": "data"}
        remove_entity = MagicMock()
        remove_entity.metadata = {"shared_key": "remove_value", "remove_only": "data"}

        mock_entity_manager.get = AsyncMock(
            side_effect=lambda eid: keep_entity if eid == "keep" else remove_entity
        )

        await dedup.merge_entities(keep_id="keep", remove_id="remove", merge_metadata=True)

        # Check update call
        update_call = mock_entity_manager.update.call_args
        merged_metadata = update_call[0][1]["metadata"]

        # Keep entity's value should win on shared key
        assert merged_metadata["shared_key"] == "keep_value"
        # Both unique keys should be present
        assert merged_metadata["keep_only"] == "data"
        assert merged_metadata["remove_only"] == "data"

    @pytest.mark.asyncio
    async def test_merge_entities_no_metadata_merge(
        self, dedup: EntityDeduplicator, mock_entity_manager: MagicMock, mock_client: MagicMock
    ) -> None:
        """Metadata is not merged when merge_metadata=False."""
        keep_entity = MagicMock()
        keep_entity.metadata = {"keep": "data"}
        remove_entity = MagicMock()
        remove_entity.metadata = {"remove": "data"}

        mock_entity_manager.get = AsyncMock(
            side_effect=lambda eid: keep_entity if eid == "keep" else remove_entity
        )

        await dedup.merge_entities(keep_id="keep", remove_id="remove", merge_metadata=False)

        # Update should not be called for metadata merge
        mock_entity_manager.update.assert_not_called()

    def test_suggest_keep_longer_name(self, dedup: EntityDeduplicator) -> None:
        """Longer name is suggested for keep."""
        suggested = dedup._suggest_keep(
            "id1", "id2",
            "Short name",
            "Much longer and more descriptive name",
        )
        assert suggested == "id2"

    def test_suggest_keep_similar_names(self, dedup: EntityDeduplicator) -> None:
        """Similar length names default to first ID."""
        suggested = dedup._suggest_keep(
            "id1", "id2",
            "Error handling",
            "Error handlers",
        )
        assert suggested == "id1"


class TestDedupWithNameOverlap:
    """Tests for deduplication with name overlap filtering."""

    @pytest.fixture
    def mock_client(self) -> MagicMock:
        """Create mock graph client."""
        client = MagicMock()
        client.client.driver.execute_query = AsyncMock(return_value=[])
        return client

    @pytest.fixture
    def mock_entity_manager(self) -> MagicMock:
        """Create mock entity manager."""
        return MagicMock()

    @pytest.mark.asyncio
    async def test_name_overlap_filter(
        self, mock_client: MagicMock, mock_entity_manager: MagicMock
    ) -> None:
        """Low name overlap filters out potential duplicates."""
        dedup = EntityDeduplicator(
            client=mock_client,
            entity_manager=mock_entity_manager,
            config=DedupConfig(min_name_overlap=0.5),  # Require 50% name overlap
        )

        # Same embedding but different names
        mock_client.client.driver.execute_query = AsyncMock(
            return_value=[
                ("id1", "Error handling patterns", "pattern", [1.0, 0.0, 0.0]),
                ("id2", "Completely different topic", "pattern", [1.0, 0.0, 0.0]),
            ]
        )

        pairs = await dedup.find_duplicates(threshold=0.9)
        # Should be filtered out due to low name overlap
        assert len(pairs) == 0

    @pytest.mark.asyncio
    async def test_name_overlap_passes(
        self, mock_client: MagicMock, mock_entity_manager: MagicMock
    ) -> None:
        """High name overlap passes filter."""
        dedup = EntityDeduplicator(
            client=mock_client,
            entity_manager=mock_entity_manager,
            config=DedupConfig(min_name_overlap=0.3),
        )

        # Same embedding and similar names
        mock_client.client.driver.execute_query = AsyncMock(
            return_value=[
                ("id1", "Error handling patterns", "pattern", [1.0, 0.0, 0.0]),
                ("id2", "Error handling best patterns", "pattern", [1.0, 0.0, 0.0]),
            ]
        )

        pairs = await dedup.find_duplicates(threshold=0.9)
        # Should pass name overlap filter
        assert len(pairs) == 1
