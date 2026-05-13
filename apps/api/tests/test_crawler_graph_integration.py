"""Tests for graph-document integration.

Tests entity extraction, linking, and bidirectional relationships
between crawled documents and the knowledge graph.
"""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from sibyl.crawler.graph_integration import (
    EntityExtractor,
    EntityLink,
    EntityLinker,
    ExtractedEntity,
    GraphIntegrationService,
    IntegrationStats,
    integrate_document_with_graph,
    normalize_extracted_entity_type,
)
from sibyl_core.models.entities import Entity, EntityType, RelationshipType

# Test organization ID for multi-tenancy
TEST_ORG_ID = "test-org-crawler-graph"

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def sample_chunk_content() -> str:
    """Sample chunk content for entity extraction."""
    return """
    FastAPI is a modern web framework for building APIs with Python.
    It uses Pydantic for data validation and automatic API documentation.
    Common patterns include dependency injection and async request handlers.
    """


@pytest.fixture
def sample_code_chunk() -> str:
    """Sample code chunk for entity extraction."""
    return """
    Here's how to create a FastAPI app:

    ```python
    from fastapi import FastAPI

    app = FastAPI()

    @app.get("/")
    async def root():
        return {"message": "Hello World"}
    ```
    """


@pytest.fixture
def mock_graph_client():
    """Create a mock GraphClient."""
    client = MagicMock()
    client.client = MagicMock()
    client.client.driver = MagicMock()
    client.execute_read_org = AsyncMock(return_value=[])
    client.execute_write_org = AsyncMock(return_value=[])
    return client


@pytest.fixture
def mock_openai_client():
    """Create a mock OpenAI client."""
    return MagicMock()


# =============================================================================
# EntityExtractor Tests
# =============================================================================


class TestEntityExtractor:
    """Tests for LLM-based entity extraction."""

    @pytest.mark.asyncio
    async def test_extract_from_chunk_success(self, sample_chunk_content):
        """Test successful entity extraction from chunk."""
        # Mock Anthropic API response format (response.content[0].text)
        mock_content_block = MagicMock()
        mock_content_block.text = '{"entities": [{"name": "FastAPI", "type": "tool", "description": "Web framework", "confidence": 0.9}]}'

        mock_response = MagicMock()
        mock_response.content = [mock_content_block]

        mock_client = AsyncMock()
        mock_client.messages.create = AsyncMock(return_value=mock_response)

        extractor = EntityExtractor()

        with patch.object(extractor, "_get_client", return_value=mock_client):
            entities = await extractor.extract_from_chunk(
                content=sample_chunk_content,
                context="API Documentation",
                url="https://docs.example.com/fastapi",
            )

            assert len(entities) == 1
            assert entities[0].name == "FastAPI"
            assert entities[0].entity_type == "tool"
            assert entities[0].confidence == 0.9

    @pytest.mark.asyncio
    async def test_extract_from_chunk_empty_content(self):
        """Test extraction from empty content."""
        # Mock Anthropic API response format
        mock_content_block = MagicMock()
        mock_content_block.text = '{"entities": []}'

        mock_response = MagicMock()
        mock_response.content = [mock_content_block]

        mock_client = AsyncMock()
        mock_client.messages.create = AsyncMock(return_value=mock_response)

        extractor = EntityExtractor()

        with patch.object(extractor, "_get_client", return_value=mock_client):
            entities = await extractor.extract_from_chunk(content="", url=None)
            assert entities == []

    @pytest.mark.asyncio
    async def test_extract_from_chunk_error_handling(self, sample_chunk_content):
        """Test error handling during extraction."""
        mock_client = AsyncMock()
        mock_client.messages.create = AsyncMock(side_effect=Exception("API Error"))

        extractor = EntityExtractor()

        with patch.object(extractor, "_get_client", return_value=mock_client):
            entities = await extractor.extract_from_chunk(
                content=sample_chunk_content,
                url="https://example.com",
            )

            # Should return empty list on error, not crash
            assert entities == []

    @pytest.mark.asyncio
    async def test_extract_batch(self, sample_chunk_content, sample_code_chunk):
        """Test batch entity extraction."""
        # Mock Anthropic API response format
        mock_content_block = MagicMock()
        mock_content_block.text = '{"entities": [{"name": "Entity1", "type": "tool", "description": "Test", "confidence": 0.8}]}'

        mock_response = MagicMock()
        mock_response.content = [mock_content_block]

        mock_client = AsyncMock()
        mock_client.messages.create = AsyncMock(return_value=mock_response)

        extractor = EntityExtractor()

        chunks = [
            (sample_chunk_content, "Context 1", "url1"),
            (sample_code_chunk, "Context 2", "url2"),
        ]

        with patch.object(extractor, "_get_client", return_value=mock_client):
            entities = await extractor.extract_batch(chunks, max_concurrent=2)

            # Should have entities from both chunks
            assert len(entities) == 2

    @pytest.mark.asyncio
    async def test_extract_with_different_entity_types(self):
        """Test extraction of various entity types."""
        # Mock Anthropic API response format
        mock_content_block = MagicMock()
        mock_content_block.text = """{
            "entities": [
                {"name": "FastAPI", "type": "tool", "description": "Framework", "confidence": 0.9},
                {"name": "async/await", "type": "pattern", "description": "Async pattern", "confidence": 0.85},
                {"name": "Python", "type": "language", "description": "Programming language", "confidence": 0.95}
            ]
        }"""

        mock_response = MagicMock()
        mock_response.content = [mock_content_block]

        mock_client = AsyncMock()
        mock_client.messages.create = AsyncMock(return_value=mock_response)

        extractor = EntityExtractor()

        with patch.object(extractor, "_get_client", return_value=mock_client):
            entities = await extractor.extract_from_chunk(
                content="Test content",
                url="https://example.com",
            )

            assert len(entities) == 3
            types = {e.entity_type for e in entities}
            assert "tool" in types
            assert "pattern" in types
            assert "language" in types


# =============================================================================
# EntityLinker Tests
# =============================================================================


class TestEntityLinker:
    """Tests for entity linking to graph."""

    @staticmethod
    def _graph_entity(entity_id: str, name: str, entity_type: EntityType) -> Entity:
        return Entity(
            id=entity_id,
            entity_type=entity_type,
            name=name,
            description=f"{name} entity",
        )

    @pytest.mark.asyncio
    async def test_link_entity_exact_match(self, mock_graph_client):
        """Test linking with exact name match."""
        linker = EntityLinker(mock_graph_client, TEST_ORG_ID)
        linker._entity_manager = MagicMock()
        linker._entity_manager.list_by_type = AsyncMock(
            return_value=[self._graph_entity("entity-123", "FastAPI", EntityType.TOOL)]
        )

        extracted = ExtractedEntity(
            name="FastAPI",
            entity_type="tool",
            description="Web framework",
            confidence=0.9,
        )

        link = await linker.link_entity(extracted)

        assert link is not None
        assert link.entity_uuid == "entity-123"
        assert link.confidence == 1.0

    @pytest.mark.asyncio
    async def test_link_entity_partial_match(self, mock_graph_client):
        """Test linking with partial name match."""
        linker = EntityLinker(mock_graph_client, TEST_ORG_ID, similarity_threshold=0.5)
        linker._entity_manager = MagicMock()
        linker._entity_manager.list_by_type = AsyncMock(
            return_value=[self._graph_entity("entity-456", "FastAPI v3", EntityType.TOOL)]
        )

        extracted = ExtractedEntity(
            name="FastAPI",
            entity_type="tool",
            description="Web framework",
            confidence=0.9,
        )

        link = await linker.link_entity(extracted)

        assert link is not None
        assert link.entity_uuid == "entity-456"
        # Partial match should have lower confidence
        assert link.confidence < 1.0
        assert link.confidence >= 0.5

    @pytest.mark.asyncio
    async def test_link_entity_no_match(self, mock_graph_client):
        """Test when no matching entity exists."""
        linker = EntityLinker(mock_graph_client, TEST_ORG_ID)
        linker._entity_manager = MagicMock()
        linker._entity_manager.list_by_type = AsyncMock(
            return_value=[self._graph_entity("other-123", "Django", EntityType.TOOL)]
        )

        extracted = ExtractedEntity(
            name="FastAPI",
            entity_type="tool",
            description="Web framework",
            confidence=0.9,
        )

        link = await linker.link_entity(extracted)

        assert link is None

    @pytest.mark.asyncio
    async def test_link_entity_normalizes_extracted_type_for_lookup(self, mock_graph_client):
        """Extractor-only types should map onto runtime graph entity types."""
        linker = EntityLinker(mock_graph_client, TEST_ORG_ID)
        linker._entity_manager = MagicMock()
        linker._entity_manager.list_by_type = AsyncMock(
            return_value=[self._graph_entity("topic-123", "Authentication", EntityType.TOPIC)]
        )

        extracted = ExtractedEntity(
            name="Authentication",
            entity_type="concept",
            description="General auth concept",
            confidence=0.8,
        )

        link = await linker.link_entity(extracted)

        assert link is not None
        assert linker._entity_manager.list_by_type.await_args.args[0] == EntityType.TOPIC
        assert linker._entity_manager.list_by_type.await_args.kwargs["include_archived"] is True
        assert link.entity_uuid == "topic-123"

    @pytest.mark.asyncio
    async def test_link_batch(self, mock_graph_client):
        """Test batch entity linking."""
        linker = EntityLinker(mock_graph_client, TEST_ORG_ID)
        linker._entity_manager = MagicMock()
        linker._entity_manager.list_by_type = AsyncMock(
            side_effect=[
                [self._graph_entity("entity-1", "FastAPI", EntityType.TOOL)],
                [self._graph_entity("entity-2", "Python", EntityType.LANGUAGE)],
                [self._graph_entity("topic-1", "Auth", EntityType.TOPIC)],
            ]
        )

        entities = [
            ExtractedEntity(name="FastAPI", entity_type="tool", description="", confidence=0.9),
            ExtractedEntity(name="Python", entity_type="language", description="", confidence=0.95),
            ExtractedEntity(name="Unknown", entity_type="concept", description="", confidence=0.5),
        ]

        linked, unlinked = await linker.link_batch(entities)

        assert len(linked) == 2
        assert len(unlinked) == 1
        assert unlinked[0].name == "Unknown"

    @pytest.mark.asyncio
    async def test_entity_cache(self, mock_graph_client):
        """Test that graph entities are cached."""
        linker = EntityLinker(mock_graph_client, TEST_ORG_ID)
        linker._entity_manager = MagicMock()
        linker._entity_manager.list_by_type = AsyncMock(
            return_value=[self._graph_entity("entity-1", "FastAPI", EntityType.TOOL)]
        )

        await linker._get_graph_entities("tool")
        assert linker._entity_manager.list_by_type.call_count == 1
        await linker._get_graph_entities("tool")
        assert linker._entity_manager.list_by_type.call_count == 1

    @pytest.mark.asyncio
    async def test_get_graph_entities_paginates_manager_results(self, mock_graph_client):
        """Entity cache loading should page through manager results."""
        linker = EntityLinker(mock_graph_client, TEST_ORG_ID)
        linker._entity_manager = MagicMock()
        linker._entity_manager.list_by_type = AsyncMock(
            side_effect=[
                [
                    self._graph_entity("entity-1", "FastAPI", EntityType.TOOL),
                    self._graph_entity("entity-2", "Pydantic", EntityType.TOOL),
                ],
                [self._graph_entity("entity-3", "Starlette", EntityType.TOOL)],
            ]
        )

        with patch("sibyl.crawler.graph_integration._ENTITY_LINK_PAGE_SIZE", 2):
            entities = await linker._get_graph_entities("tool")

        assert [entity["uuid"] for entity in entities] == ["entity-1", "entity-2", "entity-3"]
        assert linker._entity_manager.list_by_type.await_args_list[0].kwargs["offset"] == 0
        assert linker._entity_manager.list_by_type.await_args_list[1].kwargs["offset"] == 2


# =============================================================================
# GraphIntegrationService Tests
# =============================================================================


class TestGraphIntegrationService:
    """Tests for the integration orchestrator."""

    @pytest.fixture
    def mock_document_chunks(self):
        """Create mock DocumentChunk instances."""
        chunks = []
        for i in range(3):
            chunk = MagicMock()
            chunk.id = uuid4()
            chunk.document_id = uuid4()
            chunk.content = f"Content about topic {i}"
            chunk.context = f"Context {i}"
            chunk.entity_ids = []
            chunk.has_entities = False
            chunks.append(chunk)
        return chunks

    @pytest.mark.asyncio
    async def test_process_chunks_basic(self, mock_graph_client, mock_document_chunks):
        """Test basic chunk processing."""
        # Mock extractor
        mock_extractor = AsyncMock()
        mock_extractor.extract_batch = AsyncMock(
            return_value=[
                ExtractedEntity(name="Entity1", entity_type="tool", description="", confidence=0.9)
            ]
        )

        # Mock linker
        mock_linker = AsyncMock()
        mock_linker.link_batch = AsyncMock(return_value=([], []))

        service = GraphIntegrationService(mock_graph_client, TEST_ORG_ID, extract_entities=True)
        service.extractor = mock_extractor
        service.linker = mock_linker

        stats = await service.process_chunks(mock_document_chunks, "Test Source")

        assert stats.chunks_processed == 3
        assert stats.entities_extracted == 1

    @pytest.mark.asyncio
    async def test_process_chunks_creates_new_entities_for_unlinked(
        self, mock_graph_client, mock_document_chunks
    ):
        """Unlinked extracted entities can be materialized into the graph once per unique key."""
        extracted = [
            ExtractedEntity(
                name="FastAPI",
                entity_type="tool",
                description="Web framework",
                confidence=0.9,
                source_chunk_id=str(mock_document_chunks[0].id),
            ),
            ExtractedEntity(
                name="Retry timeout",
                entity_type="warning",
                description="Timeouts should be retried",
                confidence=0.8,
                source_chunk_id=str(mock_document_chunks[0].id),
            ),
            ExtractedEntity(
                name="Retry timeout",
                entity_type="warning",
                description="Timeouts should be retried quickly",
                confidence=0.7,
                source_chunk_id=str(mock_document_chunks[1].id),
            ),
        ]

        linker = MagicMock()
        linker.link_batch = AsyncMock(return_value=([], extracted))
        linker.invalidate_cache = MagicMock()

        session = AsyncMock()

        @asynccontextmanager
        async def mock_session():
            yield session

        service = GraphIntegrationService(
            mock_graph_client,
            TEST_ORG_ID,
            extract_entities=True,
            create_new_entities=True,
        )
        service.extractor = AsyncMock()
        service.extractor.extract_batch = AsyncMock(return_value=extracted)
        service.linker = linker
        service.entity_manager = MagicMock()
        service.entity_manager.create_direct = AsyncMock(
            side_effect=["tool:fastapi", "error_pattern:retry-timeout"]
        )

        with (
            patch("sibyl.crawler.graph_integration.get_content_read_session", mock_session),
            patch(
                "sibyl.crawler.graph_integration.save_document_chunks",
                AsyncMock(return_value=mock_document_chunks),
            ) as save_chunks,
        ):
            stats = await service.process_chunks(mock_document_chunks, "Test Source")

        created_types = [
            call.args[0].entity_type.value
            for call in service.entity_manager.create_direct.await_args_list
        ]
        assert created_types == ["tool", "error_pattern"]
        assert stats.entities_linked == 3
        assert stats.new_entities_created == 2
        assert stats.errors == 0
        assert mock_document_chunks[0].has_entities is True
        assert mock_document_chunks[1].has_entities is True
        assert mock_document_chunks[2].has_entities is False
        assert mock_document_chunks[0].entity_ids == ["tool:fastapi", "error_pattern:retry-timeout"]
        assert mock_document_chunks[1].entity_ids == ["error_pattern:retry-timeout"]
        linker.invalidate_cache.assert_called_once()
        assert set(linker.invalidate_cache.call_args.args) == {"tool", "error_pattern"}
        save_chunks.assert_awaited_once_with(
            session,
            chunks=[mock_document_chunks[0], mock_document_chunks[1]],
        )

    @pytest.mark.asyncio
    async def test_process_chunks_disabled_extraction(
        self, mock_graph_client, mock_document_chunks
    ):
        """Test processing with extraction disabled."""
        service = GraphIntegrationService(mock_graph_client, TEST_ORG_ID, extract_entities=False)

        stats = await service.process_chunks(mock_document_chunks, "Test Source")

        assert stats.chunks_processed == 0
        assert stats.entities_extracted == 0

    @pytest.mark.asyncio
    async def test_create_doc_relationships(self, mock_graph_client):
        """Test creating document relationships via manager seams."""
        service = GraphIntegrationService(mock_graph_client, TEST_ORG_ID)
        service.entity_manager = MagicMock()
        service.entity_manager.create_direct = AsyncMock(return_value="doc-entity")
        service.relationship_manager = MagicMock()
        service.relationship_manager.create = AsyncMock(side_effect=["rel-1", "rel-2", "rel-3"])

        doc_id = uuid4()
        entity_uuids = ["entity-1", "entity-2", "entity-3"]

        count = await service.create_doc_relationships(
            doc_id,
            entity_uuids,
            document_title="FastAPI Docs",
            document_url="https://docs.example.com/fastapi",
        )

        assert count == 3
        service.entity_manager.create_direct.assert_awaited_once()
        created_doc = service.entity_manager.create_direct.await_args.args[0]
        assert created_doc.id == str(doc_id)
        assert created_doc.entity_type == EntityType.DOCUMENT
        assert created_doc.metadata["title"] == "FastAPI Docs"
        assert created_doc.metadata["url"] == "https://docs.example.com/fastapi"
        assert service.relationship_manager.create.await_count == 3
        created_relationship = service.relationship_manager.create.await_args_list[0].args[0]
        assert created_relationship.relationship_type == RelationshipType.DOCUMENTED_IN
        assert created_relationship.source_id == "entity-1"
        assert created_relationship.target_id == str(doc_id)
        assert mock_graph_client.execute_write_org.call_count == 0

    @pytest.mark.asyncio
    async def test_create_doc_relationships_empty(self, mock_graph_client):
        """Test with no entities to link."""
        service = GraphIntegrationService(mock_graph_client, TEST_ORG_ID)

        count = await service.create_doc_relationships(uuid4(), [])

        assert count == 0
        assert mock_graph_client.execute_write_org.call_count == 0


# =============================================================================
# Integration Stats Tests
# =============================================================================


class TestIntegrationStats:
    """Tests for IntegrationStats dataclass."""

    def test_default_values(self):
        """Test default stat values."""
        stats = IntegrationStats()

        assert stats.chunks_processed == 0
        assert stats.entities_extracted == 0
        assert stats.entities_linked == 0
        assert stats.new_entities_created == 0
        assert stats.errors == 0

    def test_custom_values(self):
        """Test custom stat values."""
        stats = IntegrationStats(
            chunks_processed=10,
            entities_extracted=25,
            entities_linked=20,
            new_entities_created=5,
            errors=2,
        )

        assert stats.chunks_processed == 10
        assert stats.entities_extracted == 25
        assert stats.entities_linked == 20


# =============================================================================
# Convenience Function Tests
# =============================================================================


class TestConvenienceFunctions:
    """Tests for module-level convenience functions."""

    @pytest.mark.asyncio
    async def test_integrate_document_with_graph_success(self):
        """Test successful document integration."""
        mock_client = MagicMock()
        mock_client.client = MagicMock()
        mock_client.execute_read_org = AsyncMock(return_value=[])
        mock_client.execute_write_org = AsyncMock(return_value=[])

        chunks = [MagicMock() for _ in range(3)]
        for chunk in chunks:
            chunk.content = "Test content"
            chunk.context = None
            chunk.document_id = uuid4()

        # Patch at the import location inside the function
        with (
            patch(
                "sibyl.crawler.graph_integration.get_graph_runtime",
                new_callable=AsyncMock,
                return_value=MagicMock(client=mock_client),
            ),
            patch.object(
                GraphIntegrationService, "process_chunks", new_callable=AsyncMock
            ) as mock_process,
        ):
            mock_process.return_value = IntegrationStats(chunks_processed=3)

            stats = await integrate_document_with_graph(
                uuid4(),  # document_id (positional)
                chunks=chunks,
                source_name="Test Source",
                organization_id=TEST_ORG_ID,
            )

            assert stats.chunks_processed == 3

    @pytest.mark.asyncio
    async def test_integrate_document_graph_unavailable(self):
        """Test integration when graph is unavailable."""
        chunks = [MagicMock() for _ in range(3)]

        # Patch at the import location inside the function
        with patch(
            "sibyl.crawler.graph_integration.get_graph_runtime",
            new_callable=AsyncMock,
            side_effect=Exception("Graph unavailable"),
        ):
            stats = await integrate_document_with_graph(
                uuid4(),  # document_id (positional)
                chunks=chunks,
                source_name="Test Source",
                organization_id=TEST_ORG_ID,
            )

            # Should return empty stats, not crash
            assert stats.chunks_processed == 0


# =============================================================================
# ExtractedEntity Tests
# =============================================================================


class TestExtractedEntity:
    """Tests for ExtractedEntity dataclass."""

    def test_creation(self):
        """Test entity creation."""
        entity = ExtractedEntity(
            name="FastAPI",
            entity_type="tool",
            description="Modern web framework",
            confidence=0.95,
            source_chunk_id="chunk-123",
            source_url="https://example.com",
        )

        assert entity.name == "FastAPI"
        assert entity.entity_type == "tool"
        assert entity.confidence == 0.95

    def test_defaults(self):
        """Test default values."""
        entity = ExtractedEntity(
            name="Test",
            entity_type="concept",
            description="Test entity",
            confidence=0.5,
        )

        assert entity.source_chunk_id is None
        assert entity.source_url is None

    @pytest.mark.parametrize(
        ("raw_type", "expected"),
        [
            ("concept", EntityType.TOPIC),
            ("warning", EntityType.ERROR_PATTERN),
            ("example", EntityType.PATTERN),
            ("tool", EntityType.TOOL),
            ("weird-new-type", EntityType.TOPIC),
        ],
    )
    def test_normalize_extracted_entity_type(self, raw_type: str, expected: EntityType):
        """Extractor labels should collapse onto supported graph entity types."""
        assert normalize_extracted_entity_type(raw_type) == expected


# =============================================================================
# EntityLink Tests
# =============================================================================


class TestEntityLink:
    """Tests for EntityLink dataclass."""

    def test_creation(self):
        """Test link creation."""
        link = EntityLink(
            chunk_id="chunk-123",
            entity_uuid="entity-456",
            entity_name="FastAPI",
            entity_type="tool",
            confidence=0.9,
        )

        assert link.chunk_id == "chunk-123"
        assert link.entity_uuid == "entity-456"
        assert link.relationship_type == "DOCUMENTED_IN"

    def test_custom_relationship(self):
        """Test custom relationship type."""
        link = EntityLink(
            chunk_id="chunk-123",
            entity_uuid="entity-456",
            entity_name="Pattern",
            entity_type="pattern",
            confidence=0.8,
            relationship_type="DEMONSTRATES",
        )

        assert link.relationship_type == "DEMONSTRATES"
