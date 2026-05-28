"""Tests for sibyl-core tools layer.

Covers helpers, search, explore, and add tools with comprehensive mocking
of EntityManager dependencies.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib import import_module
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sibyl_core.auth.memory_policy import memory_scope_policy_key
from sibyl_core.models.context import (
    ContextFacet,
    ContextIntent,
    ContextItem,
    ContextItemQualityMetadata,
    ContextPack,
    ContextSection,
)
from sibyl_core.models.entities import EntityType, RelationshipType
from sibyl_core.tools.helpers import (
    MAX_CONTENT_LENGTH,
    MAX_TITLE_LENGTH,
    VALID_ENTITY_TYPES,
    _auto_discover_links,
    _build_entity_metadata,
    _generate_id,
    _get_field,
    _serialize_enum,
    auto_tag_task,
    get_project_tags,
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


def make_graph_runtime(
    *,
    client: Any | None = None,
    entity_manager: Any | None = None,
    relationship_manager: Any | None = None,
) -> SimpleNamespace:
    """Build a lightweight graph runtime for tool tests."""

    return SimpleNamespace(
        client=client if client is not None else MagicMock(),
        entity_manager=entity_manager if entity_manager is not None else MagicMock(),
        relationship_manager=(
            relationship_manager if relationship_manager is not None else MagicMock()
        ),
    )


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
        assert "decision" in VALID_ENTITY_TYPES
        assert "idea" in VALID_ENTITY_TYPES
        assert "artifact" in VALID_ENTITY_TYPES

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


class TestGetProjectTags:
    """Test project tag lookup helpers."""

    @pytest.mark.asyncio
    async def test_prefers_entity_manager_runtime(self) -> None:
        """Project tag lookup should use the entity manager seam when available."""
        entity_manager = MagicMock()
        entity_manager.list_by_type = AsyncMock(
            return_value=[
                MockEntity(
                    id="task_1",
                    entity_type=EntityType.TASK,
                    name="Task 1",
                    tags=["Backend", "API"],
                ),
                MockEntity(
                    id="task_2",
                    entity_type=EntityType.TASK,
                    name="Task 2",
                    tags=["api", "Urgent"],
                ),
            ]
        )
        runtime = make_graph_runtime(entity_manager=entity_manager)

        tags = await get_project_tags(runtime, "project-123")

        assert tags == ["api", "backend", "urgent"]
        entity_manager.list_by_type.assert_awaited_once_with(
            EntityType.TASK,
            project_id="project-123",
            limit=1000,
            include_archived=True,
        )

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

        response = await search(
            query="test",
            offset=-10,  # Negative
            organization_id="org_123",
            include_documents=False,
            include_graph=False,
        )
        assert response.offset == 0

    @pytest.mark.asyncio
    async def test_search_graph_runtime_skips_schema_preparation(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Search should not run schema bootstrap on the read path."""
        from sibyl_core.services import native_graph as native_graph_module
        from sibyl_core.tools.search import get_graph_runtime

        seen: dict[str, object] = {}

        async def fake_runtime(group_id: str, **kwargs: object) -> object:
            seen["group_id"] = group_id
            seen.update(kwargs)
            return object()

        monkeypatch.setattr(native_graph_module, "get_native_graph_runtime", fake_runtime)

        await get_graph_runtime("org_123")

        assert seen["group_id"] == "org_123"
        assert seen["ensure_schema"] is False

    @pytest.mark.asyncio
    async def test_search_graph_runtime_supports_legacy_runtime_factory(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from sibyl_core.services import native_graph as native_graph_module
        from sibyl_core.tools.search import get_graph_runtime

        seen: dict[str, object] = {}
        runtime = object()

        async def fake_runtime(
            group_id: str,
            *,
            embedding_provider: object | None = None,
        ) -> object:
            seen["group_id"] = group_id
            seen["embedding_provider"] = embedding_provider
            return runtime

        monkeypatch.setattr(native_graph_module, "get_native_graph_runtime", fake_runtime)

        result = await get_graph_runtime("org_123")

        assert result is runtime
        assert seen["group_id"] == "org_123"
        assert "embedding_provider" in seen

    @pytest.mark.asyncio
    async def test_search_builds_filters_dict(self) -> None:
        """Search builds filters dict from parameters."""
        from sibyl_core.tools.search import search

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

    def test_search_graph_filters_reject_private_scope_mismatch(self) -> None:
        """Projected private memories require the owning principal."""
        from sibyl_core.tools.search import _matches_graph_filters

        entity = MockEntity(
            id="private_projection",
            entity_type=EntityType.TOPIC,
            name="Private projection",
            metadata={
                "memory_scope": "private",
                "scope_key": "alice",
                "principal_id": "alice",
            },
        )

        assert not _matches_graph_filters(
            entity,
            language=None,
            category=None,
            status=None,
            project=None,
            principal_id="bob",
            allowed_memory_scope_keys=None,
            source=None,
            assignee=None,
            since_date=None,
            accessible_projects=None,
        )
        assert _matches_graph_filters(
            entity,
            language=None,
            category=None,
            status=None,
            project=None,
            principal_id="alice",
            allowed_memory_scope_keys=None,
            source=None,
            assignee=None,
            since_date=None,
            accessible_projects=None,
        )

    def test_search_graph_filters_require_api_key_memory_scope_grant(self) -> None:
        """API-key memory grants narrow projected project memories."""
        from sibyl_core.tools.search import _matches_graph_filters

        entity = MockEntity(
            id="project_projection",
            entity_type=EntityType.TOPIC,
            name="Project projection",
            metadata={
                "memory_scope": "project",
                "scope_key": "project_hidden",
                "project_id": "project_hidden",
            },
        )

        assert not _matches_graph_filters(
            entity,
            language=None,
            category=None,
            status=None,
            project=None,
            principal_id="bob",
            allowed_memory_scope_keys={memory_scope_policy_key("project", "project_visible")},
            source=None,
            assignee=None,
            since_date=None,
            accessible_projects={"project_hidden", "project_visible"},
        )

    @pytest.mark.asyncio
    async def test_search_passes_reference_time_to_hybrid_config(self) -> None:
        """Search forwards as-of time into enhanced graph ranking."""
        from sibyl_core.retrieval.hybrid import HybridResult

        search_module = import_module("sibyl_core.tools.search")
        hybrid_search = AsyncMock(
            return_value=HybridResult(
                results=[],
                metadata={"entity_manager_search_completed": True},
            )
        )

        with (
            patch(
                "sibyl_core.tools.search.get_graph_runtime",
                AsyncMock(return_value=make_graph_runtime(entity_manager=AsyncMock())),
            ),
            patch("sibyl_core.tools.search.hybrid_search", hybrid_search),
        ):
            response = await search_module.search(
                query="What happened 10 days ago?",
                organization_id="org_123",
                include_documents=False,
                reference_time="2026/01/20 00:00",
            )

        config = hybrid_search.await_args.kwargs["config"]
        assert response.filters["reference_time"] == "2026/01/20 00:00"
        assert config.reference_time == datetime(2026, 1, 20, tzinfo=UTC)

    @pytest.mark.asyncio
    async def test_search_filters_projected_memory_scope_from_enhanced_results(self) -> None:
        """Enhanced graph results respect projected-memory scope metadata."""
        from sibyl_core.retrieval.hybrid import HybridResult

        search_module = import_module("sibyl_core.tools.search")
        hidden = MockEntity(
            id="hidden_session",
            entity_type=EntityType.SESSION,
            name="Hidden session",
            content="Private session",
            metadata={
                "memory_scope": "private",
                "scope_key": "alice",
                "principal_id": "alice",
            },
        )
        owned = MockEntity(
            id="owned_session",
            entity_type=EntityType.SESSION,
            name="Owned session",
            content="Owned session",
            metadata={
                "memory_scope": "private",
                "scope_key": "bob",
                "principal_id": "bob",
            },
        )
        hybrid_search = AsyncMock(
            return_value=HybridResult(
                results=[(hidden, 0.99), (owned, 0.9)],
                metadata={"entity_manager_search_completed": True},
            )
        )

        with (
            patch(
                "sibyl_core.tools.search.get_graph_runtime",
                AsyncMock(return_value=make_graph_runtime(entity_manager=AsyncMock())),
            ),
            patch("sibyl_core.tools.search.hybrid_search", hybrid_search),
        ):
            response = await search_module.search(
                query="private",
                types=["session"],
                organization_id="org_123",
                include_documents=False,
                principal_id="bob",
            )

        assert [result.id for result in response.results] == ["owned_session"]
        result_filter = hybrid_search.await_args.kwargs["result_filter"]
        assert not result_filter(hidden)
        assert result_filter(owned)

    @pytest.mark.asyncio
    async def test_search_document_only_mode(self) -> None:
        """Search with types=['document'] skips graph search."""
        from sibyl_core.tools.search import search

        response = await search(
            query="test",
            types=["document"],
            organization_id="org_123",
            include_documents=False,  # Still skip actual doc search
        )
        assert response.graph_count == 0
        assert response.document_count == 0

    @pytest.mark.asyncio
    async def test_search_document_type_respects_include_documents_false(self) -> None:
        """Explicit store toggles win over document type expansion."""
        search_module = import_module("sibyl_core.tools.search")
        document_search = AsyncMock(return_value=[])

        with patch("sibyl_core.tools.search._search_documents", document_search):
            response = await search_module.search(
                query="test",
                types=["document"],
                organization_id="org_123",
                include_documents=False,
            )

        assert response.total == 0
        document_search.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_search_source_filters_respect_include_documents_false(self) -> None:
        """Graph-only searches stay graph-only even with document source filters."""
        search_module = import_module("sibyl_core.tools.search")
        document_search = AsyncMock(return_value=[])

        with patch("sibyl_core.tools.search._search_documents", document_search):
            response = await search_module.search(
                query="test",
                types=["pattern"],
                source_name="Next.js",
                organization_id="org_123",
                include_documents=False,
                include_graph=False,
            )

        assert response.total == 0
        document_search.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_search_project_filter_skips_implicit_document_search(self) -> None:
        """Project-scoped searches should not surface unscoped document results by default."""
        from sibyl_core.retrieval.hybrid import HybridResult

        search_module = import_module("sibyl_core.tools.search")
        document_search = AsyncMock(return_value=[])
        mock_entity_manager = AsyncMock()

        with (
            patch(
                "sibyl_core.tools.search.get_graph_runtime",
                AsyncMock(return_value=make_graph_runtime(entity_manager=mock_entity_manager)),
            ),
            patch("sibyl_core.tools.search._search_documents", document_search),
            patch(
                "sibyl_core.tools.search.hybrid_search",
                AsyncMock(
                    return_value=HybridResult(
                        results=[],
                        metadata={"entity_manager_search_completed": True},
                    )
                ),
            ),
        ):
            response = await search_module.search(
                query="graph",
                project="project_123",
                organization_id="org_123",
                include_documents=True,
                include_graph=True,
            )

        assert response.document_count == 0
        document_search.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_search_explicit_source_name_keeps_document_search_with_project_filter(
        self,
    ) -> None:
        """Explicit document filters keep document search enabled."""
        from sibyl_core.retrieval.hybrid import HybridResult

        search_module = import_module("sibyl_core.tools.search")
        document_search = AsyncMock(return_value=[])
        mock_entity_manager = AsyncMock()

        with (
            patch(
                "sibyl_core.tools.search.get_graph_runtime",
                AsyncMock(return_value=make_graph_runtime(entity_manager=mock_entity_manager)),
            ),
            patch("sibyl_core.tools.search._search_documents", document_search),
            patch(
                "sibyl_core.tools.search.hybrid_search",
                AsyncMock(
                    return_value=HybridResult(
                        results=[],
                        metadata={"entity_manager_search_completed": True},
                    )
                ),
            ),
        ):
            await search_module.search(
                query="graph",
                project="project_123",
                source_name="docs",
                organization_id="org_123",
                include_documents=True,
                include_graph=True,
            )

        document_search.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_search_empty_query_skips_graph(self) -> None:
        """Empty query skips graph search."""
        from sibyl_core.tools.search import search

        response = await search(
            query="",
            organization_id="org_123",
            include_documents=False,
        )
        assert response.total == 0
        assert response.query == ""

    @pytest.mark.asyncio
    async def test_search_empty_query_with_filters_lists_graph_entities(self) -> None:
        """Empty filtered graph search lists matching entities."""
        from sibyl_core.tools.search import search

        task = MockEntity(
            id="task_graph",
            entity_type=EntityType.TASK,
            name="Graph task",
            description="Filtered task",
            metadata={"project_id": "project_123", "status": "todo"},
        )
        mock_entity_manager = AsyncMock()
        mock_entity_manager.list_by_type = AsyncMock(return_value=[task])

        with patch(
            "sibyl_core.tools.search.get_graph_runtime",
            AsyncMock(return_value=make_graph_runtime(entity_manager=mock_entity_manager)),
        ):
            response = await search(
                query="",
                types=["task"],
                project="project_123",
                status="todo",
                organization_id="org_123",
                include_documents=False,
            )

        assert response.graph_count == 1
        assert response.results[0].id == "task_graph"
        mock_entity_manager.list_by_type.assert_awaited_once_with(
            EntityType.TASK,
            limit=10,
            offset=0,
            project_id="project_123",
            status="todo",
        )

    @pytest.mark.asyncio
    async def test_search_filters_project_entities_by_own_id(self) -> None:
        from sibyl_core.tools.search import search

        visible = MockEntity(
            id="project_visible",
            entity_type=EntityType.PROJECT,
            name="Visible project",
        )
        hidden = MockEntity(
            id="project_hidden",
            entity_type=EntityType.PROJECT,
            name="Hidden project",
        )
        mock_entity_manager = AsyncMock()
        mock_entity_manager.list_by_type = AsyncMock(return_value=[visible, hidden])

        with patch(
            "sibyl_core.tools.search.get_graph_runtime",
            AsyncMock(return_value=make_graph_runtime(entity_manager=mock_entity_manager)),
        ):
            response = await search(
                query="",
                types=["project"],
                accessible_projects={"project_visible"},
                organization_id="org_123",
                include_documents=False,
            )

        assert response.graph_count == 1
        assert [result.id for result in response.results] == ["project_visible"]
        mock_entity_manager.list_by_type.assert_awaited_once_with(
            EntityType.PROJECT,
            limit=10,
            offset=0,
            project_id=None,
            status=None,
        )

    @pytest.mark.asyncio
    async def test_search_empty_query_with_sparse_filters_pages_until_match(self) -> None:
        """Empty filtered graph search does not drop later matches after a short first page."""
        from sibyl_core.tools.search import search

        javascript_pattern = MockEntity(
            id="pattern_js",
            entity_type=EntityType.PATTERN,
            name="JavaScript pattern",
            languages=["javascript"],
        )
        python_pattern = MockEntity(
            id="pattern_python",
            entity_type=EntityType.PATTERN,
            name="Python pattern",
            languages=["python"],
        )

        async def list_by_type(
            entity_type: EntityType,
            *,
            limit: int,
            offset: int,
            project_id: str | None,
            status: str | None,
        ) -> list[MockEntity]:
            assert entity_type is EntityType.PATTERN
            assert limit == 1
            assert project_id is None
            assert status is None
            if offset == 0:
                return [javascript_pattern]
            if offset == 1:
                return [python_pattern]
            return []

        mock_entity_manager = AsyncMock()
        mock_entity_manager.list_by_type = AsyncMock(side_effect=list_by_type)

        with patch(
            "sibyl_core.tools.search.get_graph_runtime",
            AsyncMock(return_value=make_graph_runtime(entity_manager=mock_entity_manager)),
        ):
            response = await search(
                query="",
                types=["pattern"],
                language="python",
                organization_id="org_123",
                include_documents=False,
                limit=1,
            )

        assert response.graph_count == 1
        assert response.results[0].id == "pattern_python"
        assert mock_entity_manager.list_by_type.await_count == 2

    @pytest.mark.asyncio
    async def test_search_returns_response_structure(self) -> None:
        """Search returns properly structured SearchResponse."""
        from sibyl_core.tools.search import search

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

    @pytest.mark.asyncio
    async def test_search_document_timeout_returns_without_results(self) -> None:
        search_module = import_module("sibyl_core.tools.search")

        async def slow_document_search(**_: object) -> list[SearchResult]:
            await asyncio.sleep(0.05)
            return [
                SearchResult(
                    id="doc-timeout",
                    type="document",
                    name="Too slow",
                    content="",
                    score=1.0,
                    result_origin="document",
                )
            ]

        with (
            patch("sibyl_core.tools.search._search_documents", slow_document_search),
            patch.object(search_module, "DOCUMENT_SEARCH_TIMEOUT_SECONDS", 0.001),
        ):
            response = await search_module.search(
                query="test",
                organization_id="org_123",
                include_documents=True,
                include_graph=False,
            )

        assert response.total == 0
        assert response.document_count == 0

    @pytest.mark.asyncio
    async def test_search_with_graph_cancels_slow_document_join(self) -> None:
        from sibyl_core.retrieval.hybrid import HybridResult

        search_module = import_module("sibyl_core.tools.search")
        cancelled = asyncio.Event()

        async def slow_document_search(**_: object) -> list[SearchResult]:
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                cancelled.set()
                raise
            return []

        async def graph_search(**_: object) -> HybridResult:
            return HybridResult(
                results=[],
                metadata={"entity_manager_search_completed": True},
            )

        with (
            patch(
                "sibyl_core.tools.search.get_graph_runtime",
                AsyncMock(return_value=make_graph_runtime(entity_manager=AsyncMock())),
            ),
            patch("sibyl_core.tools.search._search_documents", slow_document_search),
            patch("sibyl_core.tools.search.hybrid_search", graph_search),
            patch.object(search_module, "DOCUMENT_SEARCH_TIMEOUT_SECONDS", 1.0),
            patch.object(search_module, "DOCUMENT_SEARCH_GRAPH_JOIN_TIMEOUT_SECONDS", 0.001),
        ):
            response = await search_module.search(
                query="fast graph slow docs",
                organization_id="org_123",
                include_documents=True,
                include_graph=True,
            )

        assert cancelled.is_set()
        assert response.total == 0
        assert response.document_count == 0

    @pytest.mark.asyncio
    async def test_search_with_graph_uses_short_document_budget(self) -> None:
        from sibyl_core.retrieval.hybrid import HybridResult

        search_module = import_module("sibyl_core.tools.search")
        cancelled = asyncio.Event()

        async def slow_document_search(**_: object) -> list[SearchResult]:
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                cancelled.set()
                raise
            return []

        async def graph_search(**_: object) -> HybridResult:
            await asyncio.sleep(0.02)
            return HybridResult(
                results=[],
                metadata={"entity_manager_search_completed": True},
            )

        with (
            patch(
                "sibyl_core.tools.search.get_graph_runtime",
                AsyncMock(return_value=make_graph_runtime(entity_manager=AsyncMock())),
            ),
            patch("sibyl_core.tools.search._search_documents", slow_document_search),
            patch("sibyl_core.tools.search.hybrid_search", graph_search),
            patch.object(search_module, "DOCUMENT_SEARCH_TIMEOUT_SECONDS", 10.0),
            patch.object(search_module, "DOCUMENT_SEARCH_GRAPH_JOIN_TIMEOUT_SECONDS", 0.001),
        ):
            response = await search_module.search(
                query="fast graph slow docs",
                organization_id="org_123",
                include_documents=True,
                include_graph=True,
            )

        assert cancelled.is_set()
        assert response.document_count == 0

    @pytest.mark.asyncio
    async def test_search_promotes_exact_title_match_over_noisy_hybrid_results(self) -> None:
        """Enhanced search should overlay exact graph title matches ahead of noisy seeds."""
        from sibyl_core.retrieval.hybrid import HybridResult
        from sibyl_core.tools.search import search

        noisy = MockEntity(
            id="pattern_noisy",
            entity_type=EntityType.PATTERN,
            name="Searchable E2E stale partial",
            description="Noisy hybrid result",
        )
        exact = MockEntity(
            id="pattern_exact",
            entity_type=EntityType.PATTERN,
            name="Searchable E2E e2e-1234",
            description="Fresh exact match",
            content="Unique searchable content e2e-1234 for verification",
        )

        mock_client = AsyncMock()
        mock_entity_manager = AsyncMock()
        mock_entity_manager.search_exact_name = AsyncMock(return_value=[(exact, 2.0)])

        with (
            patch(
                "sibyl_core.tools.search.get_graph_runtime",
                AsyncMock(
                    return_value=make_graph_runtime(
                        client=mock_client,
                        entity_manager=mock_entity_manager,
                    )
                ),
            ),
            patch(
                "sibyl_core.tools.search.hybrid_search",
                new=AsyncMock(return_value=HybridResult(results=[(noisy, 0.95)])),
            ),
        ):
            response = await search(
                query="Searchable E2E e2e-1234",
                types=["pattern"],
                organization_id="org_123",
                include_documents=False,
            )

        assert response.graph_count == 2
        assert [result.id for result in response.results[:2]] == ["pattern_exact", "pattern_noisy"]
        mock_entity_manager.search_exact_name.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_search_accepts_guide_type_alias(self) -> None:
        """Guide is the public name for existing guide storage rows."""
        from sibyl_core.retrieval.hybrid import HybridResult
        from sibyl_core.tools.search import search

        guide = MockEntity(
            id="guide_error_handling",
            entity_type=EntityType.GUIDE,
            name="Error handling guide",
            description="Boundary validation and Result types.",
        )

        mock_client = AsyncMock()
        mock_entity_manager = AsyncMock()
        mock_entity_manager.search_exact_name = AsyncMock(return_value=[])
        hybrid_search = AsyncMock(return_value=HybridResult(results=[(guide, 0.95)]))

        with (
            patch(
                "sibyl_core.tools.search.get_graph_runtime",
                AsyncMock(
                    return_value=make_graph_runtime(
                        client=mock_client,
                        entity_manager=mock_entity_manager,
                    )
                ),
            ),
            patch("sibyl_core.tools.search.hybrid_search", new=hybrid_search),
        ):
            response = await search(
                query="error handling",
                types=["guide"],
                organization_id="org_123",
                include_documents=False,
            )

        assert [result.id for result in response.results] == ["guide_error_handling"]
        assert hybrid_search.await_args.kwargs["entity_types"] == [EntityType.GUIDE]

    @pytest.mark.asyncio
    async def test_search_uses_exact_title_lookup_when_fallback_search_is_empty(self) -> None:
        """Exact title lookup should still run when hybrid and fallback search miss."""
        from sibyl_core.retrieval.hybrid import HybridResult
        from sibyl_core.tools.search import search

        exact = MockEntity(
            id="pattern_exact",
            entity_type=EntityType.PATTERN,
            name="Searchable E2E e2e-1234",
            description="Fresh exact match",
            content="Unique searchable content e2e-1234 for verification",
        )

        mock_client = AsyncMock()
        mock_entity_manager = AsyncMock()
        mock_entity_manager.search = AsyncMock(return_value=[])
        mock_entity_manager.search_exact_name = AsyncMock(return_value=[(exact, 2.0)])

        with (
            patch(
                "sibyl_core.tools.search.get_graph_runtime",
                AsyncMock(
                    return_value=make_graph_runtime(
                        client=mock_client,
                        entity_manager=mock_entity_manager,
                    )
                ),
            ),
            patch(
                "sibyl_core.tools.search.hybrid_search",
                new=AsyncMock(return_value=HybridResult(results=[])),
            ),
        ):
            response = await search(
                query="Searchable E2E e2e-1234",
                types=["pattern"],
                organization_id="org_123",
                include_documents=False,
            )

        assert response.graph_count == 1
        assert [result.id for result in response.results] == ["pattern_exact"]
        mock_entity_manager.search.assert_awaited_once()
        mock_entity_manager.search_exact_name.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_search_skips_redundant_fallback_after_exhaustive_hybrid_miss(self) -> None:
        from sibyl_core.retrieval.hybrid import HybridResult
        from sibyl_core.tools.search import search

        mock_client = AsyncMock()
        mock_entity_manager = AsyncMock()
        mock_entity_manager.search = AsyncMock(return_value=[])
        mock_entity_manager.search_exact_name = AsyncMock(return_value=[])

        with (
            patch(
                "sibyl_core.tools.search.get_graph_runtime",
                AsyncMock(
                    return_value=make_graph_runtime(
                        client=mock_client,
                        entity_manager=mock_entity_manager,
                    )
                ),
            ),
            patch(
                "sibyl_core.tools.search.hybrid_search",
                new=AsyncMock(
                    return_value=HybridResult(
                        results=[],
                        metadata={"entity_manager_search_completed": True},
                    )
                ),
            ),
        ):
            response = await search(
                query="task notification bzzmrxv82 tool toolu_01s1pyuhrut1ljdyhxcbuxzk",
                organization_id="org_123",
                include_documents=False,
            )

        assert response.graph_count == 0
        mock_entity_manager.search.assert_not_awaited()
        mock_entity_manager.search_exact_name.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_search_overlaps_document_search_with_graph_search(self) -> None:
        from sibyl_core.retrieval.hybrid import HybridResult
        from sibyl_core.tools.search import search

        events: list[str] = []

        async def document_search(**_: object) -> list[SearchResult]:
            events.append("document_start")
            await asyncio.sleep(0)
            events.append("document_done")
            return [
                SearchResult(
                    id="doc-1",
                    type="document",
                    name="Document",
                    content="Document result",
                    score=0.8,
                    result_origin="document",
                )
            ]

        async def graph_search(**_: object) -> HybridResult:
            events.append("graph_start")
            await asyncio.sleep(0.02)
            events.append("graph_done")
            return HybridResult(
                results=[],
                metadata={"entity_manager_search_completed": True},
            )

        with (
            patch(
                "sibyl_core.tools.search.get_graph_runtime",
                AsyncMock(return_value=make_graph_runtime(entity_manager=AsyncMock())),
            ),
            patch("sibyl_core.tools.search._search_documents", document_search),
            patch("sibyl_core.tools.search.hybrid_search", graph_search),
        ):
            response = await search(
                query="slow graph fast docs",
                organization_id="org_123",
                include_documents=True,
            )

        assert response.document_count == 1
        assert events.index("document_start") < events.index("graph_done")

    @pytest.mark.asyncio
    async def test_search_fuses_graph_and_document_results_by_source_rank(self) -> None:
        from sibyl_core.retrieval.hybrid import HybridResult
        from sibyl_core.tools.search import search

        task = MockEntity(
            id="task_graph",
            entity_type=EntityType.TASK,
            name="Graph task",
            description="Graph result with a low backend score.",
            content="Graph result with a low backend score.",
        )
        pattern = MockEntity(
            id="pattern_graph",
            entity_type=EntityType.PATTERN,
            name="Graph pattern",
            description="Second graph result.",
            content="Second graph result.",
        )
        doc_results = [
            SearchResult(
                id=f"doc_{index}",
                type="document",
                name=f"Doc {index}",
                content="Documentation result with a higher backend score.",
                score=0.3 - (index * 0.001),
                result_origin="document",
            )
            for index in range(5)
        ]

        mock_entity_manager = AsyncMock()
        mock_entity_manager.search_exact_name = AsyncMock(return_value=[])

        with (
            patch(
                "sibyl_core.tools.search.get_graph_runtime",
                AsyncMock(return_value=make_graph_runtime(entity_manager=mock_entity_manager)),
            ),
            patch("sibyl_core.tools.search._search_documents", AsyncMock(return_value=doc_results)),
            patch(
                "sibyl_core.tools.search.hybrid_search",
                AsyncMock(
                    return_value=HybridResult(
                        results=[(task, 0.02), (pattern, 0.01)],
                        metadata={"entity_manager_search_completed": True},
                    )
                ),
            ),
        ):
            response = await search(
                query="graph",
                organization_id="org_123",
                include_documents=True,
                limit=5,
            )

        assert response.graph_count == 2
        assert response.document_count == 3
        assert [result.result_origin for result in response.results] == [
            "graph",
            "document",
            "graph",
            "document",
            "document",
        ]

    @pytest.mark.asyncio
    async def test_search_skips_redundant_probes_when_fallback_search_errors(self) -> None:
        """Graph search failures should not trigger more graph probes for the same query."""
        from sibyl_core.retrieval.hybrid import HybridResult
        from sibyl_core.tools.search import search

        mock_client = AsyncMock()
        mock_entity_manager = AsyncMock()
        mock_entity_manager.search = AsyncMock(side_effect=RuntimeError("graph search blew up"))
        mock_entity_manager.search_exact_name = AsyncMock(return_value=[])

        with (
            patch(
                "sibyl_core.tools.search.get_graph_runtime",
                AsyncMock(
                    return_value=make_graph_runtime(
                        client=mock_client,
                        entity_manager=mock_entity_manager,
                    )
                ),
            ),
            patch(
                "sibyl_core.tools.search.hybrid_search",
                new=AsyncMock(return_value=HybridResult(results=[])),
            ),
        ):
            response = await search(
                query="Searchable E2E e2e-1234",
                types=["pattern"],
                organization_id="org_123",
                include_documents=False,
            )

        assert response.graph_count == 0
        mock_entity_manager.search.assert_awaited_once()
        mock_entity_manager.search_exact_name.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_search_skips_untyped_retry_when_typed_fallback_errors(self) -> None:
        """Typed search failures should not immediately retry the same backend untyped."""
        from sibyl_core.retrieval.hybrid import HybridResult
        from sibyl_core.tools.search import search

        mock_client = AsyncMock()
        mock_entity_manager = AsyncMock()
        mock_entity_manager.search = AsyncMock(side_effect=RuntimeError("graph search blew up"))
        mock_entity_manager.search_exact_name = AsyncMock(return_value=[])

        with (
            patch(
                "sibyl_core.tools.search.get_graph_runtime",
                AsyncMock(
                    return_value=make_graph_runtime(
                        client=mock_client,
                        entity_manager=mock_entity_manager,
                    )
                ),
            ),
            patch(
                "sibyl_core.tools.search.hybrid_search",
                new=AsyncMock(return_value=HybridResult(results=[])),
            ),
        ):
            response = await search(
                query="remember project scoped",
                types=["decision"],
                project="project_123",
                organization_id="org_123",
                include_documents=False,
            )

        assert response.graph_count == 0
        mock_entity_manager.search.assert_awaited_once()
        mock_entity_manager.search_exact_name.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_search_retries_untyped_when_typed_backend_misses(self) -> None:
        """Typed search keeps facet recall working when backend type filtering misses."""
        from sibyl_core.retrieval.hybrid import HybridResult
        from sibyl_core.tools.search import search

        decision = MockEntity(
            id="decision_project_scope",
            entity_type=EntityType.DECISION,
            name="Scoped remember captures linked project context",
            description="Project-scoped remember should be recalled.",
            content="Project-scoped remember should be recalled.",
            metadata={"project_id": "project_123"},
        )
        procedure = MockEntity(
            id="procedure_other",
            entity_type=EntityType.PROCEDURE,
            name="Unrelated procedure",
            description="Typed backend returned the wrong type.",
            content="Typed backend returned the wrong type.",
            metadata={"project_id": "project_123"},
        )

        mock_client = AsyncMock()
        mock_entity_manager = AsyncMock()
        mock_entity_manager.search = AsyncMock(side_effect=[[(procedure, 0.9)], [(decision, 1.0)]])
        mock_entity_manager.search_exact_name = AsyncMock(return_value=[])

        with (
            patch(
                "sibyl_core.tools.search.get_graph_runtime",
                AsyncMock(
                    return_value=make_graph_runtime(
                        client=mock_client,
                        entity_manager=mock_entity_manager,
                    )
                ),
            ),
            patch(
                "sibyl_core.tools.search.hybrid_search",
                new=AsyncMock(return_value=HybridResult(results=[])),
            ),
        ):
            response = await search(
                query="remember project scoped",
                types=["decision"],
                project="project_123",
                organization_id="org_123",
                include_documents=False,
            )

        assert response.graph_count == 1
        assert [result.id for result in response.results] == ["decision_project_scope"]
        assert mock_entity_manager.search.await_count == 2


class TestDocumentSearchFusion:
    """Test document-side search fusion helpers."""

    def test_dedupe_document_rows_keeps_best_chunk_per_document(self) -> None:
        """Each document keeps only its best-scoring chunk."""
        from sibyl_core.tools.search import _dedupe_document_rows

        doc1 = MagicMock()
        doc1.id = "doc_1"
        doc2 = MagicMock()
        doc2.id = "doc_2"

        rows = [
            (MagicMock(id="chunk_a"), doc1, "Docs", "src_1", 0.61),
            (MagicMock(id="chunk_b"), doc1, "Docs", "src_1", 0.84),
            (MagicMock(id="chunk_c"), doc2, "Docs", "src_1", 0.73),
        ]

        deduped = _dedupe_document_rows(rows)

        assert [(row[1].id, row[0].id) for row in deduped] == [
            ("doc_1", "chunk_b"),
            ("doc_2", "chunk_c"),
        ]

    def test_merge_document_results_boosts_shared_documents(self) -> None:
        """Documents returned by both retrievers rank above single-source hits."""
        from sibyl_core.tools.search import _merge_document_results

        vector_results = [
            SearchResult(
                id="chunk_1",
                type="document",
                name="Doc One",
                content="Vector hit one",
                score=0.9,
                result_origin="document",
                metadata={"document_id": "doc_1"},
            ),
            SearchResult(
                id="chunk_2_vector",
                type="document",
                name="Doc Two",
                content="Vector hit two",
                score=0.8,
                result_origin="document",
                metadata={"document_id": "doc_2"},
            ),
        ]
        lexical_results = [
            SearchResult(
                id="chunk_2_lexical",
                type="document",
                name="Doc Two",
                content="Lexical hit two",
                score=0.5,
                result_origin="document",
                metadata={"document_id": "doc_2"},
            ),
            SearchResult(
                id="chunk_3",
                type="document",
                name="Doc Three",
                content="Lexical hit three",
                score=0.4,
                result_origin="document",
                metadata={"document_id": "doc_3"},
            ),
        ]

        merged = _merge_document_results(vector_results, lexical_results, limit=10)

        assert [result.metadata["document_id"] for result in merged] == [
            "doc_2",
            "doc_1",
            "doc_3",
        ]
        assert merged[0].id == "chunk_2_vector"
        assert merged[0].score > merged[1].score > merged[2].score

    def test_merge_document_results_keeps_single_branch_scores_positive(self) -> None:
        """Single-branch lexical matches still return usable scores."""
        from sibyl_core.tools.search import _merge_document_results

        lexical_results = [
            SearchResult(
                id="chunk_3",
                type="document",
                name="Doc Three",
                content="Lexical hit three",
                score=0.4,
                result_origin="document",
                metadata={"document_id": "doc_3"},
            )
        ]

        merged = _merge_document_results([], lexical_results, limit=10)

        assert len(merged) == 1
        assert merged[0].metadata["document_id"] == "doc_3"
        assert merged[0].score == pytest.approx(0.3)


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

        with patch(
            "sibyl_core.tools.explore.get_graph_runtime",
            AsyncMock(
                return_value=make_graph_runtime(
                    client=mock_client,
                    entity_manager=mock_entity_manager,
                )
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

        with patch(
            "sibyl_core.tools.explore.get_graph_runtime",
            AsyncMock(
                return_value=make_graph_runtime(
                    client=mock_client,
                    relationship_manager=mock_rel_manager,
                )
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

        with patch(
            "sibyl_core.tools.explore.get_graph_runtime",
            AsyncMock(
                return_value=make_graph_runtime(
                    client=mock_client,
                    entity_manager=mock_entity_manager,
                )
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
    async def test_explore_list_mode_keeps_unassigned_episodes_with_access_filter(self) -> None:
        """Explore lists org-level episodes even when project RBAC is scoped."""
        from sibyl_core.tools.explore import explore

        episode = MockEntity(
            id="episode_123",
            entity_type=EntityType.EPISODE,
            name="Baseline Corpus Episode",
            description="Runtime baseline seed.",
            metadata={"tags": ["baseline-corpus"]},
        )
        mock_client = AsyncMock()
        mock_entity_manager = AsyncMock()
        mock_entity_manager.list_by_type = AsyncMock(return_value=[episode])

        with patch(
            "sibyl_core.tools.explore.get_graph_runtime",
            AsyncMock(
                return_value=make_graph_runtime(
                    client=mock_client,
                    entity_manager=mock_entity_manager,
                )
            ),
        ):
            response = await explore(
                mode="list",
                types=["episode"],
                accessible_projects=set(),
                organization_id="org_123",
            )

        assert response.total == 1
        assert response.entities[0].name == "Baseline Corpus Episode"
        mock_entity_manager.list_by_type.assert_awaited_once()
        assert mock_entity_manager.list_by_type.await_args.args == (EntityType.EPISODE,)

    @pytest.mark.asyncio
    async def test_explore_list_mode_builds_multi_project_filters(self) -> None:
        """Explore list mode preserves multi-project filters."""
        from sibyl_core.tools.explore import explore

        mock_client = AsyncMock()
        mock_entity_manager = AsyncMock()
        mock_entity_manager.list_by_type = AsyncMock(return_value=[])

        with patch(
            "sibyl_core.tools.explore.get_graph_runtime",
            AsyncMock(
                return_value=make_graph_runtime(
                    client=mock_client,
                    entity_manager=mock_entity_manager,
                )
            ),
        ):
            response = await explore(
                mode="list",
                types=["task"],
                project_ids=["proj_123", "proj_456"],
                status="todo",
                organization_id="org_123",
            )
            assert response.filters["types"] == ["task"]
            assert response.filters["project_ids"] == ["proj_123", "proj_456"]
            assert response.filters["status"] == "todo"

    @pytest.mark.asyncio
    async def test_explore_filters_project_entities_by_own_id(self) -> None:
        from sibyl_core.tools.explore import explore

        visible = MockEntity(
            id="project_visible",
            entity_type=EntityType.PROJECT,
            name="Visible project",
        )
        hidden = MockEntity(
            id="project_hidden",
            entity_type=EntityType.PROJECT,
            name="Hidden project",
        )
        mock_entity_manager = AsyncMock()
        mock_entity_manager.list_by_type = AsyncMock(return_value=[visible, hidden])

        with patch(
            "sibyl_core.tools.explore.get_graph_runtime",
            AsyncMock(
                return_value=make_graph_runtime(
                    entity_manager=mock_entity_manager,
                )
            ),
        ):
            response = await explore(
                mode="list",
                types=["project"],
                project_ids=["project_visible"],
                organization_id="org_123",
            )

        assert response.total == 1
        assert [entity.id for entity in response.entities] == ["project_visible"]
        mock_entity_manager.list_by_type.assert_awaited_once()
        assert mock_entity_manager.list_by_type.await_args.kwargs["project_id"] is None

    @pytest.mark.asyncio
    async def test_explore_related_filters_project_entities_by_own_id(self) -> None:
        from sibyl_core.tools.explore import explore

        def relationship(target_id: str) -> SimpleNamespace:
            return SimpleNamespace(
                source_id="task_visible",
                target_id=target_id,
                relationship_type=MockEnum("RELATED_TO"),
            )

        visible = MockEntity(
            id="project_visible",
            entity_type=EntityType.PROJECT,
            name="Visible project",
        )
        hidden = MockEntity(
            id="project_hidden",
            entity_type=EntityType.PROJECT,
            name="Hidden project",
        )
        unassigned = MockEntity(
            id="pattern_unassigned",
            entity_type=EntityType.PATTERN,
            name="Unassigned pattern",
        )
        relationship_manager = SimpleNamespace(
            get_related_entities=AsyncMock(
                return_value=[
                    (hidden, relationship("project_hidden")),
                    (visible, relationship("project_visible")),
                    (unassigned, relationship("pattern_unassigned")),
                ]
            )
        )

        with patch(
            "sibyl_core.tools.explore.get_graph_runtime",
            AsyncMock(
                return_value=make_graph_runtime(
                    relationship_manager=relationship_manager,
                )
            ),
        ):
            response = await explore(
                mode="related",
                entity_id="task_visible",
                accessible_projects={"project_visible"},
                organization_id="org_123",
            )

        assert [entity.id for entity in response.entities] == [
            "project_visible",
            "pattern_unassigned",
        ]

    @pytest.mark.asyncio
    async def test_explore_related_requires_entity_id(self) -> None:
        """Explore related mode returns error without entity_id."""
        from sibyl_core.tools.explore import explore

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

        response = await explore(
            mode="dependencies",
            entity_id=None,  # Missing
            organization_id="org_123",
        )
        assert response.total == 0
        assert "error" in response.filters

    @pytest.mark.asyncio
    async def test_explore_dependencies_filters_inaccessible_projects(self) -> None:
        from sibyl_core.tools.explore import explore

        def relationship(target_id: str) -> SimpleNamespace:
            return SimpleNamespace(
                source_id="task_root",
                target_id=target_id,
                relationship_type=RelationshipType.DEPENDS_ON,
            )

        root = MockEntity(
            id="task_root",
            entity_type=EntityType.TASK,
            name="Root",
            project_id="project_visible",
        )
        visible_dep = MockEntity(
            id="task_visible_dep",
            entity_type=EntityType.TASK,
            name="Visible dependency",
            project_id="project_visible",
        )
        hidden_dep = MockEntity(
            id="task_hidden_dep",
            entity_type=EntityType.TASK,
            name="Hidden dependency",
            project_id="project_hidden",
        )
        entity_manager = SimpleNamespace(
            get=AsyncMock(
                side_effect=lambda entity_id: {
                    "task_root": root,
                    "task_visible_dep": visible_dep,
                    "task_hidden_dep": hidden_dep,
                }.get(entity_id)
            )
        )
        relationship_manager = SimpleNamespace(
            get_related_entities=AsyncMock(
                side_effect=[
                    [
                        (hidden_dep, relationship("task_hidden_dep")),
                        (visible_dep, relationship("task_visible_dep")),
                    ],
                    [],
                ]
            )
        )

        with patch(
            "sibyl_core.tools.explore.get_graph_runtime",
            AsyncMock(
                return_value=make_graph_runtime(
                    entity_manager=entity_manager,
                    relationship_manager=relationship_manager,
                )
            ),
        ):
            response = await explore(
                mode="dependencies",
                entity_id="task_root",
                accessible_projects={"project_visible"},
                organization_id="org_123",
            )

        assert {entity.id for entity in response.entities} == {
            "task_root",
            "task_visible_dep",
        }

    @pytest.mark.asyncio
    async def test_explore_traverse_requires_entity_id(self) -> None:
        """Explore traverse mode returns error without entity_id."""
        from sibyl_core.tools.explore import explore

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

        with patch(
            "sibyl_core.tools.explore.get_graph_runtime",
            AsyncMock(
                return_value=make_graph_runtime(
                    client=mock_client,
                    entity_manager=mock_entity_manager,
                )
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

    def test_passes_entity_filters_project_ids(self) -> None:
        """Filter by multiple project IDs works."""
        from sibyl_core.tools.explore import _passes_entity_filters

        entity = MockEntity(
            id="1",
            entity_type=EntityType.TASK,
            name="Task",
            project_id="proj_b",
        )
        assert _passes_entity_filters(
            entity,
            language=None,
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
            project_ids={"proj_a", "proj_b"},
        )
        assert not _passes_entity_filters(
            entity,
            language=None,
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
            project_ids={"proj_a"},
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

        with patch(
            "sibyl_core.tools.add.get_graph_runtime",
            AsyncMock(return_value=make_graph_runtime()),
        ):
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

        with patch(
            "sibyl_core.tools.add.get_graph_runtime",
            AsyncMock(return_value=make_graph_runtime()),
        ):
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

        with patch(
            "sibyl_core.tools.add.get_graph_runtime",
            AsyncMock(
                return_value=make_graph_runtime(
                    client=mock_client,
                    entity_manager=mock_entity_manager,
                )
            ),
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

        with patch(
            "sibyl_core.tools.add.get_graph_runtime",
            AsyncMock(
                return_value=make_graph_runtime(
                    client=mock_client,
                    entity_manager=mock_entity_manager,
                )
            ),
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

    @pytest.mark.asyncio
    async def test_add_uses_higher_default_conflict_threshold(self) -> None:
        from sibyl_core.tools.add import add

        mock_entity_manager = MagicMock()
        mock_entity_manager.create_direct = AsyncMock(return_value="pattern_123")
        detect_conflicts = AsyncMock(return_value=[])

        with (
            patch(
                "sibyl_core.tools.add.get_graph_runtime",
                AsyncMock(return_value=make_graph_runtime(entity_manager=mock_entity_manager)),
            ),
            patch("sibyl_core.tools.conflicts.detect_conflicts", detect_conflicts),
            patch("sibyl_core.tools.add._auto_discover_links", return_value=[]),
        ):
            response = await add(
                title="Cache embeddings",
                content="Reuse title embeddings inside one add call.",
                entity_type="pattern",
                metadata={"organization_id": "org_123"},
                sync=True,
            )

        assert response.success is True
        assert detect_conflicts.await_args.kwargs["min_similarity"] == 0.85

    @pytest.mark.asyncio
    async def test_add_skip_conflicts_bypasses_conflict_detection(self) -> None:
        from sibyl_core.tools.add import add

        mock_entity_manager = MagicMock()
        mock_entity_manager.create_direct = AsyncMock(return_value="pattern_123")
        detect_conflicts = AsyncMock(return_value=[])

        with (
            patch(
                "sibyl_core.tools.add.get_graph_runtime",
                AsyncMock(return_value=make_graph_runtime(entity_manager=mock_entity_manager)),
            ),
            patch("sibyl_core.tools.conflicts.detect_conflicts", detect_conflicts),
            patch("sibyl_core.tools.add._auto_discover_links", return_value=[]),
        ):
            response = await add(
                title="Fast capture",
                content="Skip duplicate checks when latency matters.",
                entity_type="pattern",
                metadata={"organization_id": "org_123"},
                skip_conflicts=True,
                sync=True,
            )

        assert response.success is True
        detect_conflicts.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_auto_discover_links_skips_empty_candidate_types(self) -> None:
        entity_manager = SimpleNamespace(
            count_by_type=AsyncMock(return_value={"session": 50}),
            search=AsyncMock(return_value=[]),
        )

        links = await _auto_discover_links(
            entity_manager=entity_manager,
            title="LongMemEval session",
            content="A source session with no linkable knowledge candidates.",
            technologies=[],
            category=None,
            exclude_id="session_123",
        )

        assert links == []
        entity_manager.search.assert_not_awaited()


class TestAddEntityTypes:
    """Test add tool with different entity types."""

    @pytest.mark.asyncio
    async def test_add_pattern(self) -> None:
        """Add creates Pattern entity."""
        from sibyl_core.tools.add import add

        mock_client = AsyncMock()
        mock_entity_manager = MagicMock()
        created_entity = None

        async def capture_create(entity, **_: object):
            nonlocal created_entity
            created_entity = entity
            return entity.id

        mock_entity_manager.create_direct = capture_create

        with patch(
            "sibyl_core.tools.add.get_graph_runtime",
            AsyncMock(
                return_value=make_graph_runtime(
                    client=mock_client,
                    entity_manager=mock_entity_manager,
                )
            ),
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

        async def capture_create(entity, **_: object):
            nonlocal created_entity
            created_entity = entity
            return entity.id

        mock_entity_manager.create_direct = capture_create

        with patch(
            "sibyl_core.tools.add.get_graph_runtime",
            AsyncMock(
                return_value=make_graph_runtime(
                    client=mock_client,
                    entity_manager=mock_entity_manager,
                )
            ),
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
    async def test_add_domain_general_decision(self) -> None:
        """Add creates generic domain memory entities directly."""
        from sibyl_core.tools.add import add

        mock_client = AsyncMock()
        mock_entity_manager = MagicMock()
        created_entity = None

        async def capture_create(entity, **_: object):
            nonlocal created_entity
            created_entity = entity
            return entity.id

        mock_entity_manager.create_direct = capture_create

        with patch(
            "sibyl_core.tools.add.get_graph_runtime",
            AsyncMock(
                return_value=make_graph_runtime(
                    client=mock_client,
                    entity_manager=mock_entity_manager,
                )
            ),
        ):
            response = await add(
                title="Choose venue layout",
                content="Use a runway layout because it improves audience sight lines.",
                entity_type="decision",
                category="performance",
                metadata={"organization_id": "org_123"},
                sync=True,
            )
            assert response.success is True
            assert created_entity is not None
            assert created_entity.entity_type == EntityType.DECISION
            assert created_entity.metadata["category"] == "performance"

    @pytest.mark.asyncio
    async def test_add_project_scopes_generic_memory(self) -> None:
        """Add scopes non-task memory to projects with metadata and BELONGS_TO."""
        from sibyl_core.tools.add import add

        mock_client = AsyncMock()
        mock_entity_manager = MagicMock()
        mock_rel_manager = MagicMock()
        created_entity = None
        created_relationships = []

        async def capture_create(entity, **_: object):
            nonlocal created_entity
            created_entity = entity
            return entity.id

        async def capture_rel_create_bulk(rels):
            created_relationships.extend(rels)
            return len(rels), 0

        mock_entity_manager.create_direct = capture_create
        mock_rel_manager.create_bulk = AsyncMock(side_effect=capture_rel_create_bulk)

        with (
            patch(
                "sibyl_core.tools.add.get_graph_runtime",
                AsyncMock(
                    return_value=make_graph_runtime(
                        client=mock_client,
                        entity_manager=mock_entity_manager,
                        relationship_manager=mock_rel_manager,
                    )
                ),
            ),
            patch("sibyl_core.tools.add._auto_discover_links", return_value=[]),
        ):
            response = await add(
                title="Remember venue layout",
                content="The gala runway layout improves audience sight lines.",
                entity_type="decision",
                category="performance",
                project="project_venue",
                metadata={"organization_id": "org_123"},
                sync=True,
            )

        assert response.success is True
        assert created_entity is not None
        assert created_entity.metadata["project_id"] == "project_venue"
        assert len(created_relationships) == 1
        relationship = created_relationships[0]
        assert relationship.source_id == created_entity.id
        assert relationship.target_id == "project_venue"
        assert relationship.relationship_type == RelationshipType.BELONGS_TO

    @pytest.mark.asyncio
    async def test_add_task_with_relationships(self) -> None:
        """Add task creates BELONGS_TO relationships."""
        from sibyl_core.tools.add import add

        mock_client = AsyncMock()
        mock_entity_manager = MagicMock()
        mock_rel_manager = MagicMock()
        created_relationships = []

        async def capture_create(entity, **_: object):
            return entity.id

        async def capture_rel_create_bulk(rels):
            created_relationships.extend(rels)
            return len(rels), 0

        mock_entity_manager.create_direct = capture_create
        mock_rel_manager.create_bulk = AsyncMock(side_effect=capture_rel_create_bulk)

        with (
            patch(
                "sibyl_core.tools.add.get_graph_runtime",
                AsyncMock(
                    return_value=make_graph_runtime(
                        client=mock_client,
                        entity_manager=mock_entity_manager,
                        relationship_manager=mock_rel_manager,
                    )
                ),
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
            mock_rel_manager.create_bulk.assert_called_once()


async def _fake_synthesis_related(**kwargs: Any) -> list[Any]:
    return []


async def _fake_synthesis_search(**kwargs: Any) -> SearchResponse:
    return SearchResponse(
        results=[
            SearchResult(
                id="task:synthesis",
                type="task",
                name="Build synthesis",
                content="Synthesis should plan, draft, verify, and remember artifacts.",
                score=0.91,
            )
        ],
        total=1,
        query=kwargs["query"],
        filters={"types": kwargs["types"]},
    )


async def _fake_synthesis_context(**kwargs: Any) -> ContextPack:
    return ContextPack(
        goal=kwargs["goal"],
        intent=ContextIntent.RESEARCH,
        query=kwargs["goal"],
        domain=kwargs.get("domain"),
        project=kwargs.get("project"),
        sections=[
            ContextSection(
                facet=ContextFacet.ACTIVE_WORK,
                title="Tasks",
                items=[
                    ContextItem(
                        id="task:synthesis",
                        type="task",
                        name="Build synthesis",
                        content="D4 exposes synthesis through CLI and MCP.",
                        score=0.91,
                        facet=ContextFacet.ACTIVE_WORK,
                        reason="task explains the next synthesis surface",
                        source="source:synthesis-task",
                        quality=ContextItemQualityMetadata(
                            project_id=kwargs.get("project"),
                            updated_at="2026-05-14T12:00:00Z",
                        ),
                        metadata={"source_id": "source:synthesis-task"},
                    )
                ],
            )
        ],
        total_items=1,
    )


@pytest.mark.asyncio
async def test_synthesis_plan_tool_materializes_section_sources() -> None:
    from sibyl_core.tools.synthesis import synthesis_plan

    with (
        patch("sibyl_core.services.synthesis.default_search", _fake_synthesis_search),
        patch("sibyl_core.services.synthesis.default_related_sources", _fake_synthesis_related),
        patch("sibyl_core.services.synthesis.default_context_pack", _fake_synthesis_context),
    ):
        result = await synthesis_plan(
            goal="Write the roadmap",
            output_type="roadmap",
            project="project-sibyl",
            organization_id="org-123",
            principal_id="user-123",
            accessible_projects={"project-sibyl"},
        )

    assert result["outline"]["sections"][0]["title"] == "Current State"
    assert result["source_packs"][0]["source_ids"] == ["source:synthesis-task"]
    assert result["source_packs"][0]["freshness"] == {
        "source:synthesis-task": "2026-05-14T12:00:00Z"
    }


@pytest.mark.asyncio
async def test_synthesis_draft_tool_can_remember_artifact() -> None:
    from sibyl_core.tools.synthesis import synthesis_draft

    remember_calls: list[dict[str, Any]] = []

    async def fake_remember(**kwargs: Any) -> SimpleNamespace:
        remember_calls.append(kwargs)
        return SimpleNamespace(id="memory:artifact", source_id=kwargs["source_id"])

    with (
        patch("sibyl_core.services.synthesis.default_search", _fake_synthesis_search),
        patch("sibyl_core.services.synthesis.default_related_sources", _fake_synthesis_related),
        patch("sibyl_core.services.synthesis.default_context_pack", _fake_synthesis_context),
        patch("sibyl_core.services.synthesis.default_remember_artifact", fake_remember),
    ):
        result = await synthesis_draft(
            goal="Write the roadmap",
            output_type="roadmap",
            output_format="json",
            remember=True,
            tags=["roadmap"],
            project="project-sibyl",
            memory_scope="project",
            scope_key="project-sibyl",
            organization_id="org-123",
            principal_id="user-123",
            accessible_projects={"project-sibyl"},
        )

    artifact = result["artifact"]
    assert result["status"] == "verified"
    assert artifact["remembered_memory_id"] == "memory:artifact"
    assert artifact["remembered_source_id"] == remember_calls[0]["source_id"]
    assert remember_calls[0]["memory_scope"] == "project"
    assert remember_calls[0]["metadata"]["source_ids"] == ["source:synthesis-task"]


@pytest.mark.asyncio
async def test_synthesis_verify_tool_reports_gaps_without_artifact() -> None:
    from sibyl_core.tools.synthesis import synthesis_verify

    async def empty_search(**kwargs: Any) -> SearchResponse:
        return SearchResponse(results=[], total=0, query=kwargs["query"], filters={})

    async def empty_context(**kwargs: Any) -> ContextPack:
        return ContextPack(
            goal=kwargs["goal"],
            intent=ContextIntent.RESEARCH,
            query=kwargs["goal"],
            domain=kwargs.get("domain"),
            project=kwargs.get("project"),
            sections=[],
            total_items=0,
        )

    with (
        patch("sibyl_core.services.synthesis.default_search", empty_search),
        patch("sibyl_core.services.synthesis.default_related_sources", _fake_synthesis_related),
        patch("sibyl_core.services.synthesis.default_context_pack", empty_context),
    ):
        result = await synthesis_verify(
            goal="Explain an unsupported mobile launch",
            required_sections=["Mobile Launch::Describe the supported mobile release"],
            organization_id="org-123",
            principal_id="user-123",
            accessible_projects=set(),
        )

    assert "artifact" not in result
    assert result["verification"]["status"] == "gaps"
    assert result["verification"]["gaps"][0]["reason"] == "no_source_supports_requested_section"


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
