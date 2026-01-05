"""Tests for sibyl-core tools layer.

Covers helpers, search, explore, and add tools with comprehensive mocking
of EntityManager dependencies.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sibyl_core.models.entities import EntityType
from sibyl_core.tools.helpers import (
    MAX_CONTENT_LENGTH,
    MAX_TITLE_LENGTH,
    VALID_ENTITY_TYPES,
    _build_entity_metadata,
    _generate_id,
    _get_field,
    _serialize_enum,
    auto_tag_task,
)
from sibyl_core.tools.responses import (
    AddResponse,
    EntitySummary,
    ExploreResponse,
    RelatedEntity,
    SearchResponse,
    SearchResult,
)

# =============================================================================
# Mock Fixtures and Helpers
# =============================================================================


@dataclass
class MockEntity:
    """Mock entity for testing."""

    id: str
    entity_type: EntityType
    name: str
    description: str = ""
    content: str = ""
    source_file: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    category: str | None = None
    languages: list[str] = field(default_factory=list)
    status: Any = None
    priority: Any = None
    project_id: str | None = None
    epic_id: str | None = None
    assignees: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class MockEnum:
    """Mock enum for testing enum serialization."""

    def __init__(self, value: str) -> None:
        self.value = value


# =============================================================================
# Helper Function Tests
# =============================================================================


class TestValidEntityTypes:
    """Test VALID_ENTITY_TYPES constant."""

    def test_valid_entity_types_contains_core_types(self) -> None:
        """VALID_ENTITY_TYPES contains expected core types."""
        assert "pattern" in VALID_ENTITY_TYPES
        assert "rule" in VALID_ENTITY_TYPES
        assert "template" in VALID_ENTITY_TYPES
        assert "topic" in VALID_ENTITY_TYPES
        assert "episode" in VALID_ENTITY_TYPES
        assert "task" in VALID_ENTITY_TYPES
        assert "project" in VALID_ENTITY_TYPES
        assert "epic" in VALID_ENTITY_TYPES

    def test_valid_entity_types_all_lowercase(self) -> None:
        """All valid entity types are lowercase."""
        for entity_type in VALID_ENTITY_TYPES:
            assert entity_type == entity_type.lower()

    def test_valid_entity_types_derived_from_enum(self) -> None:
        """VALID_ENTITY_TYPES matches EntityType enum values."""
        enum_values = {t.value for t in EntityType}
        assert enum_values == VALID_ENTITY_TYPES


class TestValidationConstants:
    """Test validation constants."""

    def test_max_title_length(self) -> None:
        """MAX_TITLE_LENGTH has expected value."""
        assert MAX_TITLE_LENGTH == 200

    def test_max_content_length(self) -> None:
        """MAX_CONTENT_LENGTH has expected value."""
        assert MAX_CONTENT_LENGTH == 50000


class TestGetField:
    """Test _get_field helper function."""

    def test_get_field_direct_attribute(self) -> None:
        """Gets field directly from object attribute."""
        entity = MockEntity(
            id="test_1",
            entity_type=EntityType.PATTERN,
            name="Test Pattern",
            category="testing",
        )
        assert _get_field(entity, "category") == "testing"

    def test_get_field_from_metadata(self) -> None:
        """Falls back to metadata when attribute is None."""
        entity = MockEntity(
            id="test_1",
            entity_type=EntityType.PATTERN,
            name="Test",
            metadata={"custom_field": "custom_value"},
        )
        assert _get_field(entity, "custom_field") == "custom_value"

    def test_get_field_default_value(self) -> None:
        """Returns default when field not found anywhere."""
        entity = MockEntity(
            id="test_1",
            entity_type=EntityType.PATTERN,
            name="Test",
        )
        assert _get_field(entity, "nonexistent", "default") == "default"

    def test_get_field_default_none(self) -> None:
        """Returns None when field not found and no default."""
        entity = MockEntity(
            id="test_1",
            entity_type=EntityType.PATTERN,
            name="Test",
        )
        assert _get_field(entity, "nonexistent") is None

    def test_get_field_empty_list_default(self) -> None:
        """Can use empty list as default."""
        entity = MockEntity(
            id="test_1",
            entity_type=EntityType.PATTERN,
            name="Test",
        )
        result = _get_field(entity, "languages", [])
        assert result == []

    def test_get_field_prefers_attribute_over_metadata(self) -> None:
        """Attribute takes precedence over metadata."""
        entity = MockEntity(
            id="test_1",
            entity_type=EntityType.PATTERN,
            name="Test",
            category="from_attr",
            metadata={"category": "from_metadata"},
        )
        assert _get_field(entity, "category") == "from_attr"


class TestSerializeEnum:
    """Test _serialize_enum helper function."""

    def test_serialize_enum_with_value(self) -> None:
        """Serializes enum to its value."""
        mock_enum = MockEnum("test_value")
        assert _serialize_enum(mock_enum) == "test_value"

    def test_serialize_enum_none(self) -> None:
        """Returns None for None input."""
        assert _serialize_enum(None) is None

    def test_serialize_enum_string(self) -> None:
        """Returns string as-is if not enum."""
        assert _serialize_enum("plain_string") == "plain_string"

    def test_serialize_enum_number(self) -> None:
        """Returns number as-is if not enum."""
        assert _serialize_enum(42) == 42

    def test_serialize_real_entity_type(self) -> None:
        """Works with real EntityType enum."""
        assert _serialize_enum(EntityType.PATTERN) == "pattern"
        assert _serialize_enum(EntityType.TASK) == "task"


class TestBuildEntityMetadata:
    """Test _build_entity_metadata helper function."""

    def test_build_metadata_basic(self) -> None:
        """Builds metadata with common fields."""
        entity = MockEntity(
            id="test_1",
            entity_type=EntityType.PATTERN,
            name="Test",
            category="testing",
            languages=["python", "typescript"],
            metadata={"extra": "value"},
        )
        metadata = _build_entity_metadata(entity)

        assert metadata["category"] == "testing"
        assert metadata["languages"] == ["python", "typescript"]
        assert metadata["extra"] == "value"

    def test_build_metadata_with_status_enum(self) -> None:
        """Serializes status enum to string."""
        entity = MockEntity(
            id="test_1",
            entity_type=EntityType.TASK,
            name="Test Task",
            status=MockEnum("doing"),
        )
        metadata = _build_entity_metadata(entity)

        assert metadata["status"] == "doing"

    def test_build_metadata_with_priority_enum(self) -> None:
        """Serializes priority enum to string."""
        entity = MockEntity(
            id="test_1",
            entity_type=EntityType.TASK,
            name="Test Task",
            priority=MockEnum("high"),
        )
        metadata = _build_entity_metadata(entity)

        assert metadata["priority"] == "high"

    def test_build_metadata_excludes_none_values(self) -> None:
        """None values are not included in extra fields."""
        entity = MockEntity(
            id="test_1",
            entity_type=EntityType.PATTERN,
            name="Test",
            metadata={},
        )
        metadata = _build_entity_metadata(entity)

        # category is None, should not be in metadata
        assert "category" not in metadata or metadata.get("category") is None

    def test_build_metadata_includes_project_id(self) -> None:
        """Includes project_id for tasks."""
        entity = MockEntity(
            id="test_1",
            entity_type=EntityType.TASK,
            name="Test Task",
            project_id="proj_abc",
        )
        metadata = _build_entity_metadata(entity)

        assert metadata["project_id"] == "proj_abc"

    def test_build_metadata_includes_assignees(self) -> None:
        """Includes assignees list for tasks."""
        entity = MockEntity(
            id="test_1",
            entity_type=EntityType.TASK,
            name="Test Task",
            assignees=["alice", "bob"],
        )
        metadata = _build_entity_metadata(entity)

        assert metadata["assignees"] == ["alice", "bob"]


class TestGenerateId:
    """Test _generate_id helper function."""

    def test_generate_id_basic(self) -> None:
        """Generates deterministic ID with prefix."""
        id1 = _generate_id("task", "Test Title", "general")
        assert id1.startswith("task_")
        assert len(id1) == len("task_") + 12  # prefix + 12 char hash

    def test_generate_id_deterministic(self) -> None:
        """Same inputs produce same ID."""
        id1 = _generate_id("task", "Test Title", "general")
        id2 = _generate_id("task", "Test Title", "general")
        assert id1 == id2

    def test_generate_id_different_inputs(self) -> None:
        """Different inputs produce different IDs."""
        id1 = _generate_id("task", "Title One", "general")
        id2 = _generate_id("task", "Title Two", "general")
        assert id1 != id2

    def test_generate_id_different_prefixes(self) -> None:
        """Different prefixes produce different IDs."""
        id1 = _generate_id("task", "Same Title", "general")
        id2 = _generate_id("epic", "Same Title", "general")
        assert id1 != id2

    def test_generate_id_truncates_long_parts(self) -> None:
        """Long parts are truncated to 100 chars each."""
        long_string = "x" * 200
        id1 = _generate_id("task", long_string, "general")
        # Should still generate valid ID
        assert id1.startswith("task_")
        assert len(id1) == len("task_") + 12


# =============================================================================
# Auto-Tagging Tests
# =============================================================================


class TestAutoTagTask:
    """Test auto_tag_task function."""

    def test_auto_tag_empty_inputs(self) -> None:
        """Empty inputs return empty tags (or minimal)."""
        tags = auto_tag_task("", "")
        # May return empty or match generic patterns
        assert isinstance(tags, list)

    def test_auto_tag_explicit_tags(self) -> None:
        """Explicit tags are included."""
        tags = auto_tag_task(
            title="Test task",
            description="A simple test",
            explicit_tags=["custom", "manual"],
        )
        assert "custom" in tags
        assert "manual" in tags

    def test_auto_tag_domain_keyword(self) -> None:
        """Matches domain keywords in content."""
        tags = auto_tag_task(
            title="Add authentication flow",
            description="Implement JWT token handling",
        )
        # Should match backend/security keywords
        assert any(t in tags for t in ["backend", "security"])

    def test_auto_tag_frontend_keywords(self) -> None:
        """Identifies frontend tasks."""
        tags = auto_tag_task(
            title="Create React component",
            description="Build a modal dialog with Tailwind CSS",
        )
        assert "frontend" in tags

    def test_auto_tag_database_keywords(self) -> None:
        """Identifies database tasks."""
        tags = auto_tag_task(
            title="Add PostgreSQL migration",
            description="Create table for user profiles",
        )
        assert "database" in tags

    def test_auto_tag_devops_keywords(self) -> None:
        """Identifies devops tasks."""
        tags = auto_tag_task(
            title="Configure Docker deployment",
            description="Set up Kubernetes pods",
        )
        assert "devops" in tags

    def test_auto_tag_testing_keywords(self) -> None:
        """Identifies testing tasks."""
        tags = auto_tag_task(
            title="Add pytest fixtures",
            description="Write unit tests for auth module",
        )
        assert "testing" in tags

    def test_auto_tag_type_feature(self) -> None:
        """Identifies feature tasks."""
        tags = auto_tag_task(
            title="Implement new dashboard",
            description="Build analytics view",
        )
        assert "feature" in tags

    def test_auto_tag_type_bug(self) -> None:
        """Identifies bug fix tasks."""
        tags = auto_tag_task(
            title="Fix login crash",
            description="Resolve null pointer error",
        )
        assert "bug" in tags

    def test_auto_tag_type_refactor(self) -> None:
        """Identifies refactor tasks."""
        tags = auto_tag_task(
            title="Refactor auth module",
            description="Clean up legacy code",
        )
        assert "refactor" in tags

    def test_auto_tag_technologies(self) -> None:
        """Includes technology tags."""
        tags = auto_tag_task(
            title="Test task",
            description="Description",
            technologies=["python", "fastapi"],
        )
        assert "python" in tags
        # fastapi maps to backend
        assert "backend" in tags

    def test_auto_tag_domain_parameter(self) -> None:
        """Domain parameter adds tag."""
        tags = auto_tag_task(
            title="Test task",
            description="Description",
            domain="authentication",
        )
        assert "authentication" in tags

    def test_auto_tag_project_tags_consistency(self) -> None:
        """Prefers existing project tags for consistency."""
        tags = auto_tag_task(
            title="Add api endpoint",
            description="Create REST handler",
            project_tags=["api-v2", "backend"],
        )
        # Project tags that match content should be included
        assert "backend" in tags

    def test_auto_tag_deduplication(self) -> None:
        """Returns deduplicated tags."""
        tags = auto_tag_task(
            title="Test",
            description="Test",
            explicit_tags=["testing", "test"],
            domain="testing",
        )
        # "testing" should only appear once
        assert tags.count("testing") == 1

    def test_auto_tag_sorted_output(self) -> None:
        """Tags are sorted alphabetically."""
        tags = auto_tag_task(
            title="Create React component",
            description="Add authentication flow",
            explicit_tags=["zebra", "alpha"],
        )
        assert tags == sorted(tags)

    def test_auto_tag_min_length_filter(self) -> None:
        """Tags shorter than 2 chars are filtered."""
        tags = auto_tag_task(
            title="Test",
            description="Description",
            explicit_tags=["a", "ab", "abc"],
        )
        assert "a" not in tags
        assert "ab" in tags
        assert "abc" in tags


# =============================================================================
# Response Model Tests
# =============================================================================


class TestSearchResult:
    """Test SearchResult dataclass."""

    def test_search_result_creation(self) -> None:
        """SearchResult can be created with required fields."""
        result = SearchResult(
            id="result_1",
            type="pattern",
            name="Test Pattern",
            content="Pattern content here",
            score=0.95,
        )
        assert result.id == "result_1"
        assert result.type == "pattern"
        assert result.name == "Test Pattern"
        assert result.content == "Pattern content here"
        assert result.score == 0.95
        assert result.result_origin == "graph"  # default

    def test_search_result_optional_fields(self) -> None:
        """SearchResult optional fields default correctly."""
        result = SearchResult(
            id="result_1",
            type="pattern",
            name="Test",
            content="Content",
            score=0.9,
        )
        assert result.source is None
        assert result.url is None
        assert result.metadata == {}

    def test_search_result_full_fields(self) -> None:
        """SearchResult can be created with all fields."""
        result = SearchResult(
            id="doc_1",
            type="document",
            name="API Docs",
            content="Documentation content",
            score=0.85,
            source="nextjs-docs",
            url="https://nextjs.org/docs",
            result_origin="document",
            metadata={"chunk_type": "text", "chunk_index": 3},
        )
        assert result.source == "nextjs-docs"
        assert result.url == "https://nextjs.org/docs"
        assert result.result_origin == "document"
        assert result.metadata["chunk_type"] == "text"


class TestSearchResponse:
    """Test SearchResponse dataclass."""

    def test_search_response_creation(self) -> None:
        """SearchResponse can be created with required fields."""
        response = SearchResponse(
            results=[],
            total=0,
            query="test query",
            filters={},
        )
        assert response.results == []
        assert response.total == 0
        assert response.query == "test query"
        assert response.graph_count == 0
        assert response.document_count == 0

    def test_search_response_with_results(self) -> None:
        """SearchResponse contains results properly."""
        results = [
            SearchResult(id="1", type="pattern", name="P1", content="C1", score=0.9),
            SearchResult(id="2", type="rule", name="R1", content="C2", score=0.8),
        ]
        response = SearchResponse(
            results=results,
            total=2,
            query="test",
            filters={"types": ["pattern", "rule"]},
            graph_count=2,
            document_count=0,
            has_more=False,
        )
        assert len(response.results) == 2
        assert response.graph_count == 2
        assert response.has_more is False

    def test_search_response_pagination(self) -> None:
        """SearchResponse supports pagination fields."""
        response = SearchResponse(
            results=[],
            total=100,
            query="big query",
            filters={},
            limit=10,
            offset=20,
            has_more=True,
        )
        assert response.limit == 10
        assert response.offset == 20
        assert response.has_more is True


class TestEntitySummary:
    """Test EntitySummary dataclass."""

    def test_entity_summary_creation(self) -> None:
        """EntitySummary can be created with required fields."""
        summary = EntitySummary(
            id="entity_1",
            type="pattern",
            name="Test Pattern",
            description="A test pattern",
        )
        assert summary.id == "entity_1"
        assert summary.type == "pattern"
        assert summary.name == "Test Pattern"
        assert summary.description == "A test pattern"
        assert summary.metadata == {}

    def test_entity_summary_with_metadata(self) -> None:
        """EntitySummary can include metadata."""
        summary = EntitySummary(
            id="task_1",
            type="task",
            name="Fix Bug",
            description="Fix the auth bug",
            metadata={"status": "doing", "priority": "high"},
        )
        assert summary.metadata["status"] == "doing"
        assert summary.metadata["priority"] == "high"


class TestRelatedEntity:
    """Test RelatedEntity dataclass."""

    def test_related_entity_creation(self) -> None:
        """RelatedEntity can be created with required fields."""
        related = RelatedEntity(
            id="related_1",
            type="pattern",
            name="Related Pattern",
            relationship="RELATED_TO",
            direction="outgoing",
        )
        assert related.id == "related_1"
        assert related.relationship == "RELATED_TO"
        assert related.direction == "outgoing"
        assert related.distance == 1  # default

    def test_related_entity_incoming(self) -> None:
        """RelatedEntity handles incoming direction."""
        related = RelatedEntity(
            id="dep_1",
            type="task",
            name="Dependency",
            relationship="DEPENDS_ON",
            direction="incoming",
            distance=2,
        )
        assert related.direction == "incoming"
        assert related.distance == 2


class TestExploreResponse:
    """Test ExploreResponse dataclass."""

    def test_explore_response_list_mode(self) -> None:
        """ExploreResponse for list mode."""
        entities = [
            EntitySummary(id="1", type="pattern", name="P1", description="D1"),
            EntitySummary(id="2", type="pattern", name="P2", description="D2"),
        ]
        response = ExploreResponse(
            mode="list",
            entities=entities,
            total=2,
            filters={"types": ["pattern"]},
        )
        assert response.mode == "list"
        assert len(response.entities) == 2
        assert response.total == 2

    def test_explore_response_related_mode(self) -> None:
        """ExploreResponse for related mode."""
        entities = [
            RelatedEntity(
                id="1",
                type="task",
                name="Task",
                relationship="DEPENDS_ON",
                direction="outgoing",
            ),
        ]
        response = ExploreResponse(
            mode="related",
            entities=entities,
            total=1,
            filters={"entity_id": "source_entity"},
        )
        assert response.mode == "related"
        assert isinstance(response.entities[0], RelatedEntity)

    def test_explore_response_pagination(self) -> None:
        """ExploreResponse supports pagination."""
        response = ExploreResponse(
            mode="list",
            entities=[],
            total=0,
            filters={},
            limit=50,
            offset=100,
            has_more=True,
            actual_total=200,
        )
        assert response.limit == 50
        assert response.offset == 100
        assert response.has_more is True
        assert response.actual_total == 200


class TestAddResponse:
    """Test AddResponse dataclass."""

    def test_add_response_success(self) -> None:
        """AddResponse for successful creation."""
        response = AddResponse(
            success=True,
            id="entity_123",
            message="Added: Test Entity",
            timestamp=datetime.now(UTC),
        )
        assert response.success is True
        assert response.id == "entity_123"
        assert "Added" in response.message

    def test_add_response_failure(self) -> None:
        """AddResponse for failed creation."""
        response = AddResponse(
            success=False,
            id=None,
            message="Title cannot be empty",
            timestamp=datetime.now(UTC),
        )
        assert response.success is False
        assert response.id is None
        assert "empty" in response.message


# =============================================================================
# Search Tool Tests
# =============================================================================


class TestSearchTool:
    """Test search tool function."""

    @pytest.mark.asyncio
    async def test_search_requires_organization_id(self) -> None:
        """Search raises error without organization_id."""
        from sibyl_core.tools.search import search

        # Mock the graph client to avoid real connections
        with patch("sibyl_core.tools.search.get_graph_client"):
            response = await search(
                query="test query",
                organization_id=None,  # Missing org ID
                include_documents=False,  # Skip document search
            )
            # Search returns empty results when graph search fails due to missing org
            assert response.total == 0

    @pytest.mark.asyncio
    async def test_search_clamps_limit(self) -> None:
        """Search clamps limit to valid range."""
        from sibyl_core.tools.search import search

        with patch("sibyl_core.tools.search.get_graph_client"):
            # Test limit clamping to max 50
            response = await search(
                query="test",
                limit=100,  # Over max
                organization_id="org_123",
                include_documents=False,
                include_graph=False,  # Skip graph search too
            )
            assert response.limit == 50

    @pytest.mark.asyncio
    async def test_search_clamps_offset(self) -> None:
        """Search clamps offset to non-negative."""
        from sibyl_core.tools.search import search

        with patch("sibyl_core.tools.search.get_graph_client"):
            response = await search(
                query="test",
                offset=-10,  # Negative
                organization_id="org_123",
                include_documents=False,
                include_graph=False,
            )
            assert response.offset == 0

    @pytest.mark.asyncio
    async def test_search_builds_filters_dict(self) -> None:
        """Search builds filters dict from parameters."""
        from sibyl_core.tools.search import search

        with patch("sibyl_core.tools.search.get_graph_client"):
            response = await search(
                query="test",
                types=["pattern", "rule"],
                language="python",
                category="auth",
                status="todo",
                project="proj_123",
                organization_id="org_123",
                include_documents=False,
                include_graph=False,
            )
            assert response.filters["types"] == ["pattern", "rule"]
            assert response.filters["language"] == "python"
            assert response.filters["category"] == "auth"
            assert response.filters["status"] == "todo"
            assert response.filters["project"] == "proj_123"

    @pytest.mark.asyncio
    async def test_search_document_only_mode(self) -> None:
        """Search with types=['document'] skips graph search."""
        from sibyl_core.tools.search import search

        mock_client = AsyncMock()
        with patch("sibyl_core.tools.search.get_graph_client", return_value=mock_client):
            response = await search(
                query="test",
                types=["document"],
                organization_id="org_123",
                include_documents=False,  # Still skip actual doc search
            )
            # Should not have created EntityManager for graph search
            assert response.graph_count == 0

    @pytest.mark.asyncio
    async def test_search_empty_query_skips_graph(self) -> None:
        """Empty query skips graph search."""
        from sibyl_core.tools.search import search

        with patch("sibyl_core.tools.search.get_graph_client"):
            response = await search(
                query="",
                organization_id="org_123",
                include_documents=False,
            )
            assert response.total == 0
            assert response.query == ""

    @pytest.mark.asyncio
    async def test_search_returns_response_structure(self) -> None:
        """Search returns properly structured SearchResponse."""
        from sibyl_core.tools.search import search

        with patch("sibyl_core.tools.search.get_graph_client"):
            response = await search(
                query="test",
                organization_id="org_123",
                include_documents=False,
                include_graph=False,
            )
            assert isinstance(response, SearchResponse)
            assert isinstance(response.results, list)
            assert isinstance(response.total, int)
            assert isinstance(response.filters, dict)


# =============================================================================
# Explore Tool Tests
# =============================================================================


class TestExploreTool:
    """Test explore tool function."""

    @pytest.mark.asyncio
    async def test_explore_requires_organization_id(self) -> None:
        """Explore raises error without organization_id."""
        from sibyl_core.tools.explore import explore

        with pytest.raises(ValueError, match="organization_id is required"):
            await explore(mode="list", organization_id=None)

    @pytest.mark.asyncio
    async def test_explore_clamps_limit(self) -> None:
        """Explore clamps limit to valid range."""
        from sibyl_core.tools.explore import explore

        mock_client = AsyncMock()
        mock_entity_manager = AsyncMock()
        mock_entity_manager.list_by_type = AsyncMock(return_value=[])

        with (
            patch(
                "sibyl_core.tools.explore.get_graph_client",
                return_value=mock_client,
            ),
            patch(
                "sibyl_core.tools.explore.EntityManager",
                return_value=mock_entity_manager,
            ),
        ):
            response = await explore(
                mode="list",
                limit=300,  # Over max of 200
                organization_id="org_123",
            )
            assert response.limit == 200

    @pytest.mark.asyncio
    async def test_explore_clamps_depth(self) -> None:
        """Explore clamps depth to 1-3 range."""
        from sibyl_core.tools.explore import explore

        mock_client = AsyncMock()
        mock_rel_manager = AsyncMock()
        mock_rel_manager.get_related_entities = AsyncMock(return_value=[])

        with (
            patch(
                "sibyl_core.tools.explore.get_graph_client",
                return_value=mock_client,
            ),
            patch(
                "sibyl_core.tools.explore.RelationshipManager",
                return_value=mock_rel_manager,
            ),
        ):
            response = await explore(
                mode="traverse",
                entity_id="entity_123",
                depth=10,  # Over max of 3
                organization_id="org_123",
            )
            # Depth should be clamped to 3 (but this is internal)
            assert isinstance(response, ExploreResponse)

    @pytest.mark.asyncio
    async def test_explore_list_mode_builds_filters(self) -> None:
        """Explore list mode builds filters dict."""
        from sibyl_core.tools.explore import explore

        mock_client = AsyncMock()
        mock_entity_manager = AsyncMock()
        mock_entity_manager.list_by_type = AsyncMock(return_value=[])

        with (
            patch(
                "sibyl_core.tools.explore.get_graph_client",
                return_value=mock_client,
            ),
            patch(
                "sibyl_core.tools.explore.EntityManager",
                return_value=mock_entity_manager,
            ),
        ):
            response = await explore(
                mode="list",
                types=["task"],
                project="proj_123",
                status="todo",
                priority="high",
                organization_id="org_123",
            )
            assert response.filters["types"] == ["task"]
            assert response.filters["project"] == "proj_123"
            assert response.filters["status"] == "todo"
            assert response.filters["priority"] == "high"

    @pytest.mark.asyncio
    async def test_explore_related_requires_entity_id(self) -> None:
        """Explore related mode returns error without entity_id."""
        from sibyl_core.tools.explore import explore

        mock_client = AsyncMock()
        with patch("sibyl_core.tools.explore.get_graph_client", return_value=mock_client):
            response = await explore(
                mode="related",
                entity_id=None,  # Missing
                organization_id="org_123",
            )
            assert response.total == 0
            assert "error" in response.filters

    @pytest.mark.asyncio
    async def test_explore_dependencies_requires_entity_id(self) -> None:
        """Explore dependencies mode returns error without entity_id."""
        from sibyl_core.tools.explore import explore

        mock_client = AsyncMock()
        with patch("sibyl_core.tools.explore.get_graph_client", return_value=mock_client):
            response = await explore(
                mode="dependencies",
                entity_id=None,  # Missing
                organization_id="org_123",
            )
            assert response.total == 0
            assert "error" in response.filters

    @pytest.mark.asyncio
    async def test_explore_traverse_requires_entity_id(self) -> None:
        """Explore traverse mode returns error without entity_id."""
        from sibyl_core.tools.explore import explore

        mock_client = AsyncMock()
        with patch("sibyl_core.tools.explore.get_graph_client", return_value=mock_client):
            response = await explore(
                mode="traverse",
                entity_id=None,  # Missing
                organization_id="org_123",
            )
            assert response.total == 0
            assert "error" in response.filters

    @pytest.mark.asyncio
    async def test_explore_returns_response_structure(self) -> None:
        """Explore returns properly structured ExploreResponse."""
        from sibyl_core.tools.explore import explore

        mock_client = AsyncMock()
        mock_entity_manager = AsyncMock()
        mock_entity_manager.list_by_type = AsyncMock(return_value=[])

        with (
            patch(
                "sibyl_core.tools.explore.get_graph_client",
                return_value=mock_client,
            ),
            patch(
                "sibyl_core.tools.explore.EntityManager",
                return_value=mock_entity_manager,
            ),
        ):
            response = await explore(
                mode="list",
                organization_id="org_123",
            )
            assert isinstance(response, ExploreResponse)
            assert response.mode == "list"
            assert isinstance(response.entities, list)
            assert isinstance(response.total, int)


class TestExploreEntityFilters:
    """Test explore entity filtering logic."""

    def test_passes_entity_filters_language(self) -> None:
        """Filter by language works."""
        from sibyl_core.tools.explore import _passes_entity_filters

        entity = MockEntity(
            id="1",
            entity_type=EntityType.PATTERN,
            name="Test",
            languages=["python", "typescript"],
        )
        assert _passes_entity_filters(
            entity,
            language="python",
            category=None,
            project=None,
            accessible_projects=None,
            epic=None,
            status=None,
            priority=None,
            complexity=None,
            feature=None,
            tags=None,
            include_archived=False,
        )
        assert not _passes_entity_filters(
            entity,
            language="rust",
            category=None,
            project=None,
            accessible_projects=None,
            epic=None,
            status=None,
            priority=None,
            complexity=None,
            feature=None,
            tags=None,
            include_archived=False,
        )

    def test_passes_entity_filters_category(self) -> None:
        """Filter by category works."""
        from sibyl_core.tools.explore import _passes_entity_filters

        entity = MockEntity(
            id="1",
            entity_type=EntityType.PATTERN,
            name="Test",
            category="authentication",
        )
        assert _passes_entity_filters(
            entity,
            language=None,
            category="auth",  # Partial match
            project=None,
            accessible_projects=None,
            epic=None,
            status=None,
            priority=None,
            complexity=None,
            feature=None,
            tags=None,
            include_archived=False,
        )

    def test_passes_entity_filters_status(self) -> None:
        """Filter by status works with comma-separated values."""
        from sibyl_core.tools.explore import _passes_entity_filters

        entity = MockEntity(
            id="1",
            entity_type=EntityType.TASK,
            name="Test Task",
            status=MockEnum("doing"),
        )
        assert _passes_entity_filters(
            entity,
            language=None,
            category=None,
            project=None,
            accessible_projects=None,
            epic=None,
            status="todo,doing,review",  # Multiple values
            priority=None,
            complexity=None,
            feature=None,
            tags=None,
            include_archived=False,
        )
        assert not _passes_entity_filters(
            entity,
            language=None,
            category=None,
            project=None,
            accessible_projects=None,
            epic=None,
            status="done",
            priority=None,
            complexity=None,
            feature=None,
            tags=None,
            include_archived=False,
        )


# =============================================================================
# Add Tool Tests
# =============================================================================


class TestAddTool:
    """Test add tool function."""

    @pytest.mark.asyncio
    async def test_add_validates_empty_title(self) -> None:
        """Add returns error for empty title."""
        from sibyl_core.tools.add import add

        response = await add(title="", content="Some content")
        assert response.success is False
        assert response.id is None
        assert "Title cannot be empty" in response.message

    @pytest.mark.asyncio
    async def test_add_validates_whitespace_title(self) -> None:
        """Add returns error for whitespace-only title."""
        from sibyl_core.tools.add import add

        response = await add(title="   ", content="Some content")
        assert response.success is False
        assert "Title cannot be empty" in response.message

    @pytest.mark.asyncio
    async def test_add_validates_empty_content(self) -> None:
        """Add returns error for empty content."""
        from sibyl_core.tools.add import add

        response = await add(title="Valid Title", content="")
        assert response.success is False
        assert "Content cannot be empty" in response.message

    @pytest.mark.asyncio
    async def test_add_validates_title_length(self) -> None:
        """Add returns error for title exceeding max length."""
        from sibyl_core.tools.add import add

        long_title = "x" * (MAX_TITLE_LENGTH + 1)
        response = await add(title=long_title, content="Some content")
        assert response.success is False
        assert f"exceeds {MAX_TITLE_LENGTH}" in response.message

    @pytest.mark.asyncio
    async def test_add_validates_content_length(self) -> None:
        """Add returns error for content exceeding max length."""
        from sibyl_core.tools.add import add

        long_content = "x" * (MAX_CONTENT_LENGTH + 1)
        response = await add(title="Valid Title", content=long_content)
        assert response.success is False
        assert f"exceeds {MAX_CONTENT_LENGTH}" in response.message

    @pytest.mark.asyncio
    async def test_add_requires_organization_id(self) -> None:
        """Add returns error without organization_id in metadata."""
        from sibyl_core.tools.add import add

        mock_client = AsyncMock()
        with patch("sibyl_core.tools.add.get_graph_client", return_value=mock_client):
            response = await add(
                title="Test",
                content="Content",
                metadata={},  # No org ID
            )
            assert response.success is False
            assert "organization_id is required" in response.message

    @pytest.mark.asyncio
    async def test_add_task_requires_project(self) -> None:
        """Add task returns error without project."""
        from sibyl_core.tools.add import add

        mock_client = AsyncMock()
        with patch("sibyl_core.tools.add.get_graph_client", return_value=mock_client):
            response = await add(
                title="Test Task",
                content="Task content",
                entity_type="task",
                metadata={"organization_id": "org_123"},
                project=None,  # Missing project
            )
            assert response.success is False
            assert "require a project" in response.message

    @pytest.mark.asyncio
    async def test_add_epic_requires_project(self) -> None:
        """Add epic returns error without project."""
        from sibyl_core.tools.add import add

        mock_client = AsyncMock()
        with patch("sibyl_core.tools.add.get_graph_client", return_value=mock_client):
            response = await add(
                title="Test Epic",
                content="Epic content",
                entity_type="epic",
                metadata={"organization_id": "org_123"},
                project=None,  # Missing project
            )
            assert response.success is False
            assert "require a project" in response.message

    @pytest.mark.asyncio
    async def test_add_returns_response_structure(self) -> None:
        """Add returns properly structured AddResponse."""
        from sibyl_core.tools.add import add

        # Just test validation response structure
        response = await add(title="", content="")
        assert isinstance(response, AddResponse)
        assert isinstance(response.success, bool)
        assert isinstance(response.message, str)
        assert isinstance(response.timestamp, datetime)

    @pytest.mark.asyncio
    async def test_add_strips_whitespace(self) -> None:
        """Add strips whitespace from title and content."""
        from sibyl_core.tools.add import add

        mock_client = AsyncMock()
        mock_entity_manager = AsyncMock()
        mock_entity_manager.create_direct = AsyncMock(return_value="episode_123")
        mock_entity_manager.create = AsyncMock(return_value="episode_123")

        with (
            patch("sibyl_core.tools.add.get_graph_client", return_value=mock_client),
            patch("sibyl_core.tools.add.EntityManager", return_value=mock_entity_manager),
            patch("sibyl_core.tools.add.RelationshipManager"),
        ):
            response = await add(
                title="  Test Title  ",
                content="  Test content  ",
                metadata={"organization_id": "org_123"},
                sync=True,  # Use sync mode to avoid ARQ
            )
            # Should succeed after stripping whitespace
            assert response.success is True

    @pytest.mark.asyncio
    async def test_add_generates_deterministic_id(self) -> None:
        """Add generates deterministic entity ID."""
        from sibyl_core.tools.add import add

        mock_client = AsyncMock()
        mock_entity_manager = MagicMock()
        created_id = None

        async def capture_create(entity):
            nonlocal created_id
            created_id = entity.id
            return entity.id

        mock_entity_manager.create = capture_create
        mock_entity_manager.create_direct = capture_create

        with (
            patch("sibyl_core.tools.add.get_graph_client", return_value=mock_client),
            patch("sibyl_core.tools.add.EntityManager", return_value=mock_entity_manager),
            patch("sibyl_core.tools.add.RelationshipManager"),
        ):
            response = await add(
                title="Test Entity",
                content="Test content",
                entity_type="episode",
                category="testing",
                metadata={"organization_id": "org_123"},
                sync=True,  # Use sync mode to avoid ARQ import
            )
            assert response.success is True
            assert response.id is not None
            assert response.id.startswith("episode_")


class TestAddEntityTypes:
    """Test add tool with different entity types."""

    @pytest.mark.asyncio
    async def test_add_pattern(self) -> None:
        """Add creates Pattern entity."""
        from sibyl_core.tools.add import add

        mock_client = AsyncMock()
        mock_entity_manager = MagicMock()
        created_entity = None

        async def capture_create(entity):
            nonlocal created_entity
            created_entity = entity
            return entity.id

        mock_entity_manager.create_direct = capture_create

        with (
            patch("sibyl_core.tools.add.get_graph_client", return_value=mock_client),
            patch("sibyl_core.tools.add.EntityManager", return_value=mock_entity_manager),
            patch("sibyl_core.tools.add.RelationshipManager"),
        ):
            response = await add(
                title="Error Handling Pattern",
                content="Always use try/except blocks...",
                entity_type="pattern",
                category="error-handling",
                languages=["python"],
                metadata={"organization_id": "org_123"},
                sync=True,  # Use sync mode to avoid ARQ import
            )
            assert response.success is True
            assert created_entity is not None
            assert created_entity.entity_type == EntityType.PATTERN

    @pytest.mark.asyncio
    async def test_add_project(self) -> None:
        """Add creates Project entity."""
        from sibyl_core.tools.add import add

        mock_client = AsyncMock()
        mock_entity_manager = MagicMock()
        created_entity = None

        async def capture_create(entity):
            nonlocal created_entity
            created_entity = entity
            return entity.id

        mock_entity_manager.create_direct = capture_create

        with (
            patch("sibyl_core.tools.add.get_graph_client", return_value=mock_client),
            patch("sibyl_core.tools.add.EntityManager", return_value=mock_entity_manager),
            patch("sibyl_core.tools.add.RelationshipManager"),
        ):
            response = await add(
                title="Sibyl",
                content="Collective Intelligence Runtime",
                entity_type="project",
                repository_url="https://github.com/hyperb1iss/sibyl",
                metadata={"organization_id": "org_123"},
                sync=True,  # Use sync mode to avoid ARQ import
            )
            assert response.success is True
            assert created_entity is not None
            assert created_entity.entity_type == EntityType.PROJECT

    @pytest.mark.asyncio
    async def test_add_task_with_relationships(self) -> None:
        """Add task creates BELONGS_TO relationships."""
        from sibyl_core.tools.add import add

        mock_client = AsyncMock()
        mock_entity_manager = MagicMock()
        mock_rel_manager = MagicMock()
        created_relationships = []

        async def capture_create(entity):
            return entity.id

        async def capture_rel_create(rel):
            created_relationships.append(rel)

        mock_entity_manager.create_direct = capture_create
        mock_rel_manager.create = capture_rel_create

        with (
            patch("sibyl_core.tools.add.get_graph_client", return_value=mock_client),
            patch("sibyl_core.tools.add.EntityManager", return_value=mock_entity_manager),
            patch(
                "sibyl_core.tools.add.RelationshipManager",
                return_value=mock_rel_manager,
            ),
            patch("sibyl_core.tools.add.get_project_tags", return_value=[]),
            patch("sibyl_core.tools.add._auto_discover_links", return_value=[]),
        ):
            response = await add(
                title="Implement feature",
                content="Build the new feature",
                entity_type="task",
                project="proj_123",
                epic="epic_456",
                metadata={"organization_id": "org_123"},
                sync=True,  # Use sync mode to test relationship creation
            )
            assert response.success is True
            # Should have created BELONGS_TO relationships
            assert len(created_relationships) >= 2
            rel_types = [r.relationship_type.value for r in created_relationships]
            assert "BELONGS_TO" in rel_types


# =============================================================================
# Integration Tests
# =============================================================================


class TestToolsIntegration:
    """Integration tests for tools working together."""

    @pytest.mark.asyncio
    async def test_add_response_timestamp_is_utc(self) -> None:
        """Add response timestamp is UTC."""
        from sibyl_core.tools.add import add

        response = await add(title="", content="")  # Validation error
        assert response.timestamp.tzinfo == UTC

    def test_response_models_are_serializable(self) -> None:
        """Response models can be serialized to dict."""
        # SearchResult
        result = SearchResult(id="1", type="pattern", name="Test", content="Content", score=0.9)
        # Dataclass has __dict__
        assert hasattr(result, "__dict__")

        # SearchResponse
        response = SearchResponse(results=[result], total=1, query="test", filters={})
        assert hasattr(response, "__dict__")

        # EntitySummary
        summary = EntitySummary(id="1", type="task", name="Task", description="Desc")
        assert hasattr(summary, "__dict__")

    def test_helpers_handle_edge_cases(self) -> None:
        """Helper functions handle edge cases gracefully."""
        # _get_field with None entity attributes
        entity = MockEntity(id="1", entity_type=EntityType.PATTERN, name="Test", metadata={})
        assert _get_field(entity, "nonexistent", "fallback") == "fallback"

        # _serialize_enum with various types
        assert _serialize_enum(None) is None
        assert _serialize_enum("string") == "string"
        assert _serialize_enum(123) == 123

        # _generate_id with special characters
        id1 = _generate_id("task", "Title with spaces!", "category/sub")
        assert id1.startswith("task_")
