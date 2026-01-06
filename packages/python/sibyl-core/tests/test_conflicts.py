"""Tests for sibyl-core conflict detection tools.

Covers contradiction detection during knowledge ingest including:
- Finding semantically similar entities
- Classifying conflicts (duplicate, overlap, contradiction)
- Integration with add() function
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from sibyl_core.models.entities import Entity, EntityType
from sibyl_core.tools.conflicts import (
    CONFLICT_THRESHOLD,
    DUPLICATE_THRESHOLD,
    HIGH_OVERLAP_THRESHOLD,
    _check_contradiction_signals,
    _simple_title_similarity,
    classify_conflict,
    detect_conflicts,
    find_similar_entities,
)
from sibyl_core.tools.responses import ConflictWarning

# =============================================================================
# Response Model Tests
# =============================================================================


class TestConflictWarning:
    """Test ConflictWarning dataclass."""

    def test_conflict_warning_creation(self) -> None:
        """ConflictWarning can be created with required fields."""
        warning = ConflictWarning(
            existing_id="entity_123",
            existing_name="Existing Pattern",
            existing_content="This is existing content...",
            similarity_score=0.85,
            conflict_type="semantic_overlap",
            explanation="High semantic overlap detected.",
        )
        assert warning.existing_id == "entity_123"
        assert warning.similarity_score == 0.85
        assert warning.conflict_type == "semantic_overlap"

    def test_conflict_warning_types(self) -> None:
        """ConflictWarning supports all conflict types."""
        for conflict_type in ["semantic_overlap", "potential_contradiction", "duplicate"]:
            warning = ConflictWarning(
                existing_id="e1",
                existing_name="Name",
                existing_content="Content",
                similarity_score=0.8,
                conflict_type=conflict_type,  # type: ignore
            )
            assert warning.conflict_type == conflict_type


# =============================================================================
# Helper Function Tests
# =============================================================================


class TestSimpleTitleSimilarity:
    """Test _simple_title_similarity helper."""

    def test_identical_titles(self) -> None:
        """Identical titles have similarity 1.0."""
        assert _simple_title_similarity("Redis Connection Pool", "Redis Connection Pool") == 1.0

    def test_completely_different_titles(self) -> None:
        """Completely different titles have similarity 0.0."""
        # Use titles with no word overlap
        sim = _simple_title_similarity("Apple Orange Banana", "Car Truck Plane")
        assert sim == 0.0

    def test_partial_overlap(self) -> None:
        """Partial word overlap gives intermediate similarity."""
        # "Redis" and "Pool" overlap, "Connection" and "Settings" don't
        sim = _simple_title_similarity("Redis Connection Pool", "Redis Pool Settings")
        assert 0.3 < sim < 0.8  # Partial overlap

    def test_case_insensitive(self) -> None:
        """Title similarity is case-insensitive."""
        assert _simple_title_similarity("Redis POOL", "redis pool") == 1.0

    def test_empty_title(self) -> None:
        """Empty titles return 0.0."""
        assert _simple_title_similarity("", "Some Title") == 0.0
        assert _simple_title_similarity("Some Title", "") == 0.0


class TestCheckContradictionSignals:
    """Test _check_contradiction_signals helper."""

    def test_no_contradiction(self) -> None:
        """No contradiction signals in unrelated content."""
        result = _check_contradiction_signals(
            "Redis is fast and efficient.",
            "PostgreSQL handles complex queries well.",
        )
        assert result is None

    def test_negation_contradiction(self) -> None:
        """Detects negation patterns (should vs should not)."""
        result = _check_contradiction_signals(
            "You should use connection pooling.",
            "You should not use connection pooling for short-lived connections.",
        )
        assert result is not None
        assert "should" in result.lower()

    def test_always_never_contradiction(self) -> None:
        """Detects always/never contradiction."""
        result = _check_contradiction_signals(
            "Always use async connections.",
            "Never use async for simple queries.",
        )
        assert result is not None
        assert "always" in result.lower() or "never" in result.lower()

    def test_version_conflict(self) -> None:
        """Detects version conflicts."""
        result = _check_contradiction_signals(
            "This works in version 2.0.",
            "This feature requires version 3.5 or higher.",
        )
        assert result is not None
        assert "version" in result.lower()

    def test_works_doesnt_work_contradiction(self) -> None:
        """Detects works/doesn't work contradiction."""
        result = _check_contradiction_signals(
            "This approach works for large datasets.",
            "This approach doesn't work well at scale.",
        )
        assert result is not None


class TestClassifyConflict:
    """Test classify_conflict function."""

    def test_duplicate_classification(self) -> None:
        """High similarity (>95%) classifies as duplicate."""
        conflict_type, explanation = classify_conflict(
            new_title="Redis Connection Pooling",
            new_content="Connection pooling is essential for Redis performance.",
            existing_name="Redis Connection Pooling",
            existing_content="Connection pooling is essential for Redis performance.",
            similarity_score=0.98,
        )
        assert conflict_type == "duplicate"
        assert "98%" in explanation or "duplicate" in explanation.lower()

    def test_high_overlap_similar_titles(self) -> None:
        """High overlap with similar titles classifies as duplicate."""
        # Use identical titles to ensure >0.8 title similarity
        conflict_type, _explanation = classify_conflict(
            new_title="Redis Pool Best Practices",
            new_content="Best practices for Redis connection pooling.",
            existing_name="Redis Pool Best Practices",  # Same title
            existing_content="Guide to Redis connection pooling best practices.",
            similarity_score=0.90,
        )
        assert conflict_type == "duplicate"

    def test_high_overlap_different_titles(self) -> None:
        """High overlap with different titles classifies as semantic_overlap."""
        conflict_type, _explanation = classify_conflict(
            new_title="Database Performance Tips",
            new_content="Use connection pooling for better performance.",
            existing_name="Redis Optimization Guide",
            existing_content="Guide to optimizing Redis with connection pools.",
            similarity_score=0.88,
        )
        assert conflict_type == "semantic_overlap"

    def test_potential_contradiction(self) -> None:
        """Moderate similarity with contradiction signals classifies as potential_contradiction."""
        conflict_type, explanation = classify_conflict(
            new_title="Redis async usage",
            new_content="You should use async connections for Redis.",
            existing_name="Redis sync patterns",
            existing_content="You should not use async for simple Redis operations.",
            similarity_score=0.75,
        )
        assert conflict_type == "potential_contradiction"
        assert "contradiction" in explanation.lower()

    def test_moderate_overlap_no_contradiction(self) -> None:
        """Moderate similarity without contradiction signals classifies as semantic_overlap."""
        conflict_type, _explanation = classify_conflict(
            new_title="Redis caching strategy",
            new_content="Cache frequently accessed data in Redis.",
            existing_name="Redis usage patterns",
            existing_content="Common patterns for using Redis in applications.",
            similarity_score=0.72,
        )
        assert conflict_type == "semantic_overlap"


# =============================================================================
# Async Function Tests
# =============================================================================


class TestFindSimilarEntities:
    """Test find_similar_entities function."""

    @pytest.mark.asyncio
    async def test_returns_empty_for_no_matches(self) -> None:
        """Returns empty list when no similar entities found."""
        mock_entity_manager = AsyncMock()
        mock_entity_manager.search = AsyncMock(return_value=[])

        mock_client = AsyncMock()

        with (
            patch(
                "sibyl_core.tools.conflicts.get_graph_client",
                return_value=mock_client,
            ),
            patch(
                "sibyl_core.tools.conflicts.EntityManager",
                return_value=mock_entity_manager,
            ),
        ):
            results = await find_similar_entities(
                title="New Entity",
                content="New content",
                organization_id="org_123",
            )
            assert results == []

    @pytest.mark.asyncio
    async def test_filters_below_threshold(self) -> None:
        """Filters out results below minimum score threshold."""
        mock_entity = Entity(
            id="entity_1",
            name="Low Score Entity",
            entity_type=EntityType.EPISODE,
            description="Some content",
            content="Some content",
        )

        mock_entity_manager = AsyncMock()
        mock_entity_manager.search = AsyncMock(
            return_value=[
                (mock_entity, 0.5),  # Below default threshold of 0.70
            ]
        )

        mock_client = AsyncMock()

        with (
            patch(
                "sibyl_core.tools.conflicts.get_graph_client",
                return_value=mock_client,
            ),
            patch(
                "sibyl_core.tools.conflicts.EntityManager",
                return_value=mock_entity_manager,
            ),
        ):
            results = await find_similar_entities(
                title="New Entity",
                content="New content",
                organization_id="org_123",
                min_score=CONFLICT_THRESHOLD,
            )
            assert results == []

    @pytest.mark.asyncio
    async def test_returns_above_threshold(self) -> None:
        """Returns results above minimum score threshold."""
        mock_entity = Entity(
            id="entity_1",
            name="Similar Entity",
            entity_type=EntityType.EPISODE,
            description="Similar content here",
            content="Similar content here",
        )

        mock_entity_manager = AsyncMock()
        mock_entity_manager.search = AsyncMock(
            return_value=[
                (mock_entity, 0.85),  # Above threshold
            ]
        )

        mock_client = AsyncMock()

        with (
            patch(
                "sibyl_core.tools.conflicts.get_graph_client",
                return_value=mock_client,
            ),
            patch(
                "sibyl_core.tools.conflicts.EntityManager",
                return_value=mock_entity_manager,
            ),
        ):
            results = await find_similar_entities(
                title="New Entity",
                content="New content",
                organization_id="org_123",
            )
            assert len(results) == 1
            assert results[0][0] == "entity_1"
            assert results[0][1] == "Similar Entity"
            assert results[0][3] == 0.85


class TestDetectConflicts:
    """Test detect_conflicts function."""

    @pytest.mark.asyncio
    async def test_no_conflicts_found(self) -> None:
        """Returns empty list when no similar entities exist."""
        with patch(
            "sibyl_core.tools.conflicts.find_similar_entities",
            return_value=[],
        ):
            warnings = await detect_conflicts(
                title="Unique Knowledge",
                content="Completely unique content.",
                organization_id="org_123",
            )
            assert warnings == []

    @pytest.mark.asyncio
    async def test_duplicate_conflict(self) -> None:
        """Detects duplicate content."""
        similar_entities = [
            ("entity_1", "Same Title", "Same content", 0.98),
        ]

        with patch(
            "sibyl_core.tools.conflicts.find_similar_entities",
            return_value=similar_entities,
        ):
            warnings = await detect_conflicts(
                title="Same Title",
                content="Same content",
                organization_id="org_123",
            )
            assert len(warnings) == 1
            assert warnings[0].conflict_type == "duplicate"
            assert warnings[0].existing_id == "entity_1"

    @pytest.mark.asyncio
    async def test_excludes_self(self) -> None:
        """Excludes entity with matching exclude_id."""
        similar_entities = [
            ("entity_self", "My Title", "My content", 0.99),
            ("entity_other", "Other Title", "Other content", 0.80),
        ]

        with patch(
            "sibyl_core.tools.conflicts.find_similar_entities",
            return_value=similar_entities,
        ):
            warnings = await detect_conflicts(
                title="My Title",
                content="My content",
                organization_id="org_123",
                exclude_id="entity_self",
            )
            # Should only have the other entity
            assert len(warnings) == 1
            assert warnings[0].existing_id == "entity_other"

    @pytest.mark.asyncio
    async def test_sorts_by_severity(self) -> None:
        """Sorts warnings by severity (duplicate > contradiction > overlap)."""
        similar_entities = [
            ("entity_overlap", "Overlap", "Some overlap", 0.75),
            ("entity_dup", "Duplicate", "Same content", 0.98),
            ("entity_contra", "Contra", "You should not do this", 0.78),
        ]

        # Mock classify_conflict to return specific types
        with (
            patch(
                "sibyl_core.tools.conflicts.find_similar_entities",
                return_value=similar_entities,
            ),
            patch(
                "sibyl_core.tools.conflicts.classify_conflict",
                side_effect=[
                    ("semantic_overlap", "Overlap detected"),
                    ("duplicate", "Very similar"),
                    ("potential_contradiction", "Contradicts"),
                ],
            ),
        ):
            warnings = await detect_conflicts(
                title="Test",
                content="Test content you should do this",
                organization_id="org_123",
            )

            assert len(warnings) == 3
            # Duplicate should be first
            assert warnings[0].conflict_type == "duplicate"
            # Contradiction second
            assert warnings[1].conflict_type == "potential_contradiction"
            # Overlap last
            assert warnings[2].conflict_type == "semantic_overlap"

    @pytest.mark.asyncio
    async def test_respects_max_conflicts(self) -> None:
        """Returns at most max_conflicts warnings."""
        similar_entities = [
            ("e1", "Entity 1", "Content 1", 0.90),
            ("e2", "Entity 2", "Content 2", 0.88),
            ("e3", "Entity 3", "Content 3", 0.86),
            ("e4", "Entity 4", "Content 4", 0.84),
            ("e5", "Entity 5", "Content 5", 0.82),
        ]

        with patch(
            "sibyl_core.tools.conflicts.find_similar_entities",
            return_value=similar_entities,
        ):
            warnings = await detect_conflicts(
                title="Test",
                content="Test content",
                organization_id="org_123",
                max_conflicts=2,
            )
            assert len(warnings) == 2


# =============================================================================
# Threshold Tests
# =============================================================================


class TestThresholds:
    """Test threshold constants are correctly defined."""

    def test_threshold_ordering(self) -> None:
        """Thresholds are in correct order: duplicate > high_overlap > conflict."""
        assert DUPLICATE_THRESHOLD > HIGH_OVERLAP_THRESHOLD
        assert HIGH_OVERLAP_THRESHOLD > CONFLICT_THRESHOLD

    def test_threshold_ranges(self) -> None:
        """Thresholds are in valid range (0.0-1.0)."""
        for threshold in [DUPLICATE_THRESHOLD, HIGH_OVERLAP_THRESHOLD, CONFLICT_THRESHOLD]:
            assert 0.0 <= threshold <= 1.0


# =============================================================================
# Integration Tests
# =============================================================================


class TestConflictIntegration:
    """Integration tests for conflict detection with add()."""

    @pytest.mark.asyncio
    async def test_conflict_response_includes_warnings(self) -> None:
        """ConflictWarning is serializable and included in AddResponse."""
        from datetime import UTC, datetime

        from sibyl_core.tools.responses import AddResponse

        warning = ConflictWarning(
            existing_id="entity_123",
            existing_name="Existing Pattern",
            existing_content="Content preview...",
            similarity_score=0.85,
            conflict_type="semantic_overlap",
            explanation="Review for overlap",
        )

        response = AddResponse(
            success=True,
            id="new_entity_456",
            message="Added: New Entity (⚠️ 1 potential conflict(s) detected)",
            timestamp=datetime.now(UTC),
            conflicts=[warning],
        )

        assert len(response.conflicts) == 1
        assert response.conflicts[0].existing_id == "entity_123"
        assert response.conflicts[0].similarity_score == 0.85
