"""Tests for sibyl-core cross-encoder reranking.

Covers reranking functionality including:
- Config validation
- Content extraction
- Cross-encoder scoring
- Result reranking
- Fallback behavior when dependencies missing
"""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import MagicMock, patch

import pytest

from sibyl_core.retrieval.reranking import (
    CrossEncoderConfig,
    RerankResult,
    _extract_content,
    cross_encoder_rerank,
    rerank_results,
)

# =============================================================================
# Config Tests
# =============================================================================


class TestCrossEncoderConfig:
    """Test CrossEncoderConfig dataclass."""

    def test_default_config(self) -> None:
        """Default config has reranking disabled."""
        config = CrossEncoderConfig()
        assert config.enabled is False
        assert config.model_name == "cross-encoder/ms-marco-MiniLM-L-6-v2"
        assert config.top_k == 20
        assert config.batch_size == 32
        assert config.fallback_on_error is True

    def test_custom_config(self) -> None:
        """Config can be customized."""
        config = CrossEncoderConfig(
            enabled=True,
            model_name="cross-encoder/ms-marco-MiniLM-L-12-v2",
            top_k=10,
            min_score=0.5,
            use_gpu=True,
        )
        assert config.enabled is True
        assert config.model_name == "cross-encoder/ms-marco-MiniLM-L-12-v2"
        assert config.top_k == 10
        assert config.min_score == 0.5
        assert config.use_gpu is True


class TestRerankResult:
    """Test RerankResult dataclass."""

    def test_basic_result(self) -> None:
        """RerankResult stores results and metadata."""
        result = RerankResult(
            results=[("entity1", 0.9), ("entity2", 0.8)],
            reranked_count=2,
            model_name="test-model",
        )
        assert len(result.results) == 2
        assert result.reranked_count == 2
        assert result.model_name == "test-model"

    def test_result_with_metadata(self) -> None:
        """RerankResult can include arbitrary metadata."""
        result = RerankResult(
            results=[],
            reranked_count=0,
            metadata={"reranking_skipped": "disabled"},
        )
        assert result.metadata["reranking_skipped"] == "disabled"


# =============================================================================
# Content Extraction Tests
# =============================================================================


class TestExtractContent:
    """Test _extract_content helper."""

    def test_extract_from_content_attribute(self) -> None:
        """Extracts from 'content' attribute."""

        @dataclass
        class Entity:
            content: str

        entity = Entity(content="This is the content")
        assert _extract_content(entity) == "This is the content"

    def test_extract_from_description_attribute(self) -> None:
        """Extracts from 'description' if no content."""

        @dataclass
        class Entity:
            description: str

        entity = Entity(description="This is the description")
        assert _extract_content(entity) == "This is the description"

    def test_extract_from_dict_content(self) -> None:
        """Extracts from dict 'content' key."""
        entity = {"content": "Dict content"}
        assert _extract_content(entity) == "Dict content"

    def test_extract_from_dict_description(self) -> None:
        """Extracts from dict 'description' if no content."""
        entity = {"description": "Dict description"}
        assert _extract_content(entity) == "Dict description"

    def test_extract_fallback_to_name(self) -> None:
        """Falls back to 'name' if no content fields."""

        @dataclass
        class Entity:
            name: str

        entity = Entity(name="Entity Name")
        assert _extract_content(entity) == "Entity Name"

    def test_extract_from_dict_name(self) -> None:
        """Falls back to dict 'name' key."""
        entity = {"name": "Dict Name"}
        assert _extract_content(entity) == "Dict Name"

    def test_extract_fallback_to_str(self) -> None:
        """Falls back to str() representation."""
        entity = 12345
        assert _extract_content(entity) == "12345"


# =============================================================================
# Cross-Encoder Rerank Tests
# =============================================================================


class TestCrossEncoderRerank:
    """Test cross_encoder_rerank function."""

    def test_empty_results_returns_empty(self) -> None:
        """Empty input returns empty output."""
        mock_model = MagicMock()
        result = cross_encoder_rerank("query", [], mock_model)
        assert result == []

    def test_reranks_with_model_scores(self) -> None:
        """Reranks results based on model scores."""

        @dataclass
        class Entity:
            id: str
            content: str

        entities = [
            (Entity("e1", "Low relevance content"), 0.9),
            (Entity("e2", "High relevance content"), 0.8),
            (Entity("e3", "Medium relevance content"), 0.7),
        ]

        mock_model = MagicMock()
        # Model scores: e1=0.3, e2=0.9, e3=0.5
        mock_model.predict.return_value = [0.3, 0.9, 0.5]

        result = cross_encoder_rerank("test query", entities, mock_model, top_k=10)

        # Should be reordered by model scores: e2, e3, e1
        assert len(result) == 3
        assert result[0][0].id == "e2"  # Highest model score
        assert result[1][0].id == "e3"  # Second highest
        assert result[2][0].id == "e1"  # Lowest model score

    def test_respects_top_k(self) -> None:
        """Only reranks top_k candidates."""
        entities = [({"id": f"e{i}", "content": f"Content {i}"}, 1.0 - i * 0.1) for i in range(10)]

        mock_model = MagicMock()
        # Only 3 candidates will be reranked (top_k=3)
        mock_model.predict.return_value = [0.9, 0.8, 0.7]

        result = cross_encoder_rerank("query", entities, mock_model, top_k=3)

        # Model was only called with 3 pairs
        mock_model.predict.assert_called_once()
        call_args = mock_model.predict.call_args
        assert len(call_args[0][0]) == 3  # 3 query-doc pairs

        # Result should have all 10 (3 reranked + 7 remainder)
        assert len(result) == 10

    def test_min_score_filters_results(self) -> None:
        """min_score filters out low-scoring results."""

        @dataclass
        class Entity:
            id: str
            content: str

        entities = [
            (Entity("e1", "Content 1"), 0.9),
            (Entity("e2", "Content 2"), 0.8),
            (Entity("e3", "Content 3"), 0.7),
        ]

        mock_model = MagicMock()
        mock_model.predict.return_value = [0.8, 0.4, 0.6]  # e2 below threshold

        result = cross_encoder_rerank("query", entities, mock_model, top_k=10, min_score=0.5)

        # e2 (score 0.4) should be filtered
        ids = [e.id for e, _ in result]
        assert "e2" not in ids
        assert "e1" in ids
        assert "e3" in ids

    def test_handles_model_error(self) -> None:
        """Returns original results if model prediction fails."""
        entities = [({"id": "e1", "content": "Test"}, 0.9)]

        mock_model = MagicMock()
        mock_model.predict.side_effect = Exception("Model error")

        result = cross_encoder_rerank("query", entities, mock_model)

        # Should return original results unchanged
        assert result == entities


# =============================================================================
# Async Rerank Tests
# =============================================================================


class TestRerankResults:
    """Test rerank_results async function."""

    @pytest.mark.asyncio
    async def test_disabled_config_skips_reranking(self) -> None:
        """When disabled, returns original results."""
        entities = [({"id": "e1"}, 0.9), ({"id": "e2"}, 0.8)]
        config = CrossEncoderConfig(enabled=False)

        result = await rerank_results("query", entities, config)

        assert result.results == entities
        assert result.reranked_count == 0
        assert result.metadata.get("reranking_skipped") == "disabled"

    @pytest.mark.asyncio
    async def test_empty_results_skips_reranking(self) -> None:
        """Empty input skips reranking."""
        config = CrossEncoderConfig(enabled=True)

        result = await rerank_results("query", [], config)

        assert result.results == []
        assert result.reranked_count == 0
        assert result.metadata.get("reranking_skipped") == "no_results"

    @pytest.mark.asyncio
    async def test_fallback_on_missing_dependency(self) -> None:
        """Falls back gracefully when sentence-transformers missing."""
        entities = [({"id": "e1"}, 0.9)]
        config = CrossEncoderConfig(enabled=True, fallback_on_error=True)

        with patch(
            "sibyl_core.retrieval.reranking.get_cross_encoder",
            side_effect=ImportError("No module"),
        ):
            result = await rerank_results("query", entities, config)

        assert result.results == entities
        assert result.reranked_count == 0
        assert "not_installed" in result.metadata.get("reranking_skipped", "")

    @pytest.mark.asyncio
    async def test_fallback_on_error(self) -> None:
        """Falls back gracefully on reranking errors."""
        entities = [({"id": "e1"}, 0.9)]
        config = CrossEncoderConfig(enabled=True, fallback_on_error=True)

        mock_model = MagicMock()
        mock_model.predict.side_effect = Exception("Inference failed")

        with patch(
            "sibyl_core.retrieval.reranking.get_cross_encoder",
            return_value=mock_model,
        ):
            result = await rerank_results("query", entities, config)

        # Should return original results
        assert len(result.results) == 1
        assert result.results[0][0]["id"] == "e1"

    @pytest.mark.asyncio
    async def test_successful_reranking(self) -> None:
        """Successfully reranks results."""

        @dataclass
        class Entity:
            id: str
            content: str

        entities = [
            (Entity("e1", "First"), 0.9),
            (Entity("e2", "Second"), 0.8),
        ]
        config = CrossEncoderConfig(enabled=True, top_k=10)

        mock_model = MagicMock()
        mock_model.predict.return_value = [0.7, 0.95]  # e2 scores higher

        with patch(
            "sibyl_core.retrieval.reranking.get_cross_encoder",
            return_value=mock_model,
        ):
            result = await rerank_results("query", entities, config)

        assert result.reranked_count == 2
        assert result.model_name == config.model_name
        # e2 should be first after reranking
        assert result.results[0][0].id == "e2"


# =============================================================================
# Integration Tests
# =============================================================================


class TestRerankingIntegration:
    """Integration tests for reranking module."""

    def test_config_values_propagate(self) -> None:
        """Config values are used in reranking."""
        config = CrossEncoderConfig(
            enabled=True,
            top_k=5,
            batch_size=16,
            min_score=0.3,
        )

        assert config.top_k == 5
        assert config.batch_size == 16
        assert config.min_score == 0.3

    @pytest.mark.asyncio
    async def test_rerank_preserves_entity_type(self) -> None:
        """Reranking preserves entity object types."""

        @dataclass
        class CustomEntity:
            id: str
            name: str
            content: str
            custom_field: int

        entities = [
            (CustomEntity("e1", "Entity 1", "Content 1", 42), 0.9),
            (CustomEntity("e2", "Entity 2", "Content 2", 99), 0.8),
        ]

        mock_model = MagicMock()
        mock_model.predict.return_value = [0.6, 0.9]

        with patch(
            "sibyl_core.retrieval.reranking.get_cross_encoder",
            return_value=mock_model,
        ):
            result = await rerank_results(
                "query",
                entities,
                CrossEncoderConfig(enabled=True),
            )

        # Entities should still be CustomEntity instances
        for entity, _score in result.results:
            assert isinstance(entity, CustomEntity)
            assert hasattr(entity, "custom_field")
