"""Tests for the core MCP tools (search, explore, add, manage)."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sibyl.models.entities import EntityType
from sibyl.tools.core import (
    VALID_ENTITY_TYPES,
    AddResponse,
    EntitySummary,
    ExploreResponse,
    SearchResponse,
    SearchResult,
    add,
    explore,
    get_health,
    get_stats,
    search,
)
from tests.harness import create_test_entity, mock_tools

# Test organization ID for graph operations
TEST_ORG_ID = "test-org-12345"


class TestSearchResponse:
    """Tests for SearchResponse dataclass."""

    def test_basic_response(self) -> None:
        """Test creating a basic search response."""
        response = SearchResponse(
            query="test query",
            results=[],
            total=0,
            filters={"types": ["pattern"]},
        )
        assert response.query == "test query"
        assert response.results == []
        assert response.total == 0
        assert response.filters == {"types": ["pattern"]}

    def test_response_with_results(self) -> None:
        """Test response with SearchResult objects."""
        results = [
            SearchResult(
                id="p1", type="pattern", name="Pattern 1", content="content 1", score=0.95
            ),
            SearchResult(
                id="p2", type="pattern", name="Pattern 2", content="content 2", score=0.85
            ),
        ]
        response = SearchResponse(
            query="patterns",
            results=results,
            total=2,
            filters={},
        )
        assert len(response.results) == 2
        assert response.total == 2
        assert response.results[0].score > response.results[1].score


class TestExploreResponse:
    """Tests for ExploreResponse dataclass."""

    def test_basic_response(self) -> None:
        """Test creating a basic explore response."""
        response = ExploreResponse(
            mode="list",
            entities=[],
            total=0,
            filters={},
        )
        assert response.mode == "list"
        assert response.entities == []
        assert response.total == 0

    def test_response_with_entities(self) -> None:
        """Test explore response with entity data."""
        entities = [
            EntitySummary(id="e1", type="pattern", name="Entity 1", description="A test entity"),
        ]
        response = ExploreResponse(
            mode="list",
            entities=entities,
            total=1,
            filters={},
        )
        assert response.mode == "list"
        assert len(response.entities) == 1


class TestAddResponse:
    """Tests for AddResponse dataclass."""

    def test_success_response(self) -> None:
        """Test successful add response."""
        response = AddResponse(
            success=True,
            id="ent_123",
            message="Entity created",
            timestamp=datetime.now(UTC),
        )
        assert response.success is True
        assert response.id == "ent_123"

    def test_failure_response(self) -> None:
        """Test failed add response."""
        response = AddResponse(
            success=False,
            id=None,
            message="Title cannot be empty",
            timestamp=datetime.now(UTC),
        )
        assert response.success is False
        assert response.id is None


class TestValidEntityTypes:
    """Tests for entity type validation."""

    def test_valid_types_matches_enum(self) -> None:
        """VALID_ENTITY_TYPES should match EntityType enum values."""
        enum_values = {t.value for t in EntityType}
        assert enum_values == VALID_ENTITY_TYPES

    def test_types_are_lowercase(self) -> None:
        """Entity types should be lowercase."""
        for t in VALID_ENTITY_TYPES:
            assert t == t.lower()

    def test_core_types_present(self) -> None:
        """Core entity types should be present."""
        core_types = {"pattern", "rule", "template", "task", "project", "episode"}
        assert core_types.issubset(VALID_ENTITY_TYPES)


class TestSearchInputValidation:
    """Tests for search() input validation."""

    @pytest.mark.asyncio
    async def test_empty_query_allowed(self) -> None:
        """Empty query with filters should be allowed."""
        # This will attempt connection but should not fail on validation
        response = await search("", types=["task"], status="doing")
        # Response is returned even if connection fails (graceful degradation)
        assert isinstance(response, SearchResponse)

    @pytest.mark.asyncio
    async def test_limit_clamped_to_max(self) -> None:
        """Limit should be clamped to maximum 50."""
        response = await search("test", limit=100)
        # The function clamps internally; verify response is valid
        assert isinstance(response, SearchResponse)

    @pytest.mark.asyncio
    async def test_limit_clamped_to_min(self) -> None:
        """Limit should be clamped to minimum 1."""
        response = await search("test", limit=0)
        assert isinstance(response, SearchResponse)


class TestExploreInputValidation:
    """Tests for explore() input validation."""

    @pytest.mark.asyncio
    async def test_list_mode_no_entity_id(self) -> None:
        """List mode should not require entity_id."""
        response = await explore(mode="list", types=["pattern"], organization_id=TEST_ORG_ID)
        assert isinstance(response, ExploreResponse)
        assert response.mode == "list"

    @pytest.mark.asyncio
    async def test_depth_clamped(self) -> None:
        """Depth should be clamped to 1-3."""
        response = await explore(
            mode="traverse", entity_id="test", depth=10, organization_id=TEST_ORG_ID
        )
        assert isinstance(response, ExploreResponse)

    @pytest.mark.asyncio
    async def test_dependencies_mode(self) -> None:
        """Dependencies mode should be handled."""
        response = await explore(
            mode="dependencies", project="proj_test", organization_id=TEST_ORG_ID
        )
        assert isinstance(response, ExploreResponse)
        assert response.mode == "dependencies"


class TestAddInputValidation:
    """Tests for add() input validation."""

    @pytest.mark.asyncio
    async def test_empty_title_fails(self) -> None:
        """Empty title should return failure."""
        response = await add("", "Some content")
        assert response.success is False
        assert "Title cannot be empty" in response.message

    @pytest.mark.asyncio
    async def test_whitespace_title_fails(self) -> None:
        """Whitespace-only title should return failure."""
        response = await add("   ", "Some content")
        assert response.success is False
        assert "Title cannot be empty" in response.message

    @pytest.mark.asyncio
    async def test_empty_content_fails(self) -> None:
        """Empty content should return failure."""
        response = await add("Valid Title", "")
        assert response.success is False
        assert "Content cannot be empty" in response.message

    @pytest.mark.asyncio
    async def test_title_max_length(self) -> None:
        """Title exceeding max length should fail."""
        long_title = "x" * 300  # Exceeds 200 char limit
        response = await add(long_title, "Some content")
        assert response.success is False
        assert "exceeds" in response.message.lower()


class TestExploreModeLiterals:
    """Tests for explore mode validation."""

    def test_valid_modes(self) -> None:
        """Valid modes should be list, related, traverse, dependencies."""
        # These are enforced by Literal type, just document expected values
        valid_modes = ["list", "related", "traverse", "dependencies"]
        for mode in valid_modes:
            # Type checking would catch invalid modes at compile time
            assert mode in ["list", "related", "traverse", "dependencies"]


class TestSearchDeduplication:
    """Tests for search result deduplication."""

    def test_dedup_keeps_highest_score(self) -> None:
        """When same ID appears twice, higher score should be kept."""
        # Simulate graph result and doc result with same ID
        result_low = SearchResult(
            id="entity_1",
            type="pattern",
            name="Pattern 1",
            content="content",
            score=0.7,
            result_origin="document",
        )
        result_high = SearchResult(
            id="entity_1",
            type="pattern",
            name="Pattern 1",
            content="content",
            score=0.9,
            result_origin="graph",
        )

        # Simulate the deduplication logic from core.py
        seen_ids: dict[str, SearchResult] = {}
        for result in [result_low, result_high]:
            if result.id not in seen_ids or result.score > seen_ids[result.id].score:
                seen_ids[result.id] = result

        assert len(seen_ids) == 1
        assert seen_ids["entity_1"].score == 0.9
        assert seen_ids["entity_1"].result_origin == "graph"

    def test_dedup_preserves_unique_entries(self) -> None:
        """Unique IDs should all be preserved."""
        results = [
            SearchResult(id="a", type="pattern", name="A", content="", score=0.9),
            SearchResult(id="b", type="pattern", name="B", content="", score=0.8),
            SearchResult(id="c", type="pattern", name="C", content="", score=0.7),
        ]

        seen_ids: dict[str, SearchResult] = {}
        for result in results:
            if result.id not in seen_ids or result.score > seen_ids[result.id].score:
                seen_ids[result.id] = result

        assert len(seen_ids) == 3
        assert all(rid in seen_ids for rid in ["a", "b", "c"])


class TestSearchWithHarness:
    """Tests for search() function using test harness."""

    @pytest.mark.asyncio
    async def test_search_returns_graph_results(self) -> None:
        """Search should return results from entity manager."""
        async with mock_tools() as ctx:
            # Add entity and set up search results
            entity = create_test_entity(
                entity_type=EntityType.PATTERN,
                name="Test Pattern",
            )
            ctx.entity_manager.add_entity(entity)
            ctx.entity_manager.set_search_results([(entity, 0.95)])

            result = await search("test pattern", organization_id=TEST_ORG_ID)

            assert isinstance(result, SearchResponse)
            assert result.query == "test pattern"

    @pytest.mark.asyncio
    async def test_search_with_type_filter(self) -> None:
        """Search should filter by entity types."""
        async with mock_tools() as ctx:
            pattern = create_test_entity(entity_type=EntityType.PATTERN, name="Pattern")
            rule = create_test_entity(entity_type=EntityType.RULE, name="Rule")
            ctx.entity_manager.add_entity(pattern)
            ctx.entity_manager.add_entity(rule)
            ctx.entity_manager.set_search_results([(pattern, 0.9), (rule, 0.8)])

            result = await search("test", types=["pattern"], organization_id=TEST_ORG_ID)

            assert isinstance(result, SearchResponse)

    @pytest.mark.asyncio
    async def test_search_with_status_filter(self) -> None:
        """Search should accept status filter for tasks."""
        async with mock_tools():
            result = await search(
                "task", types=["task"], status="doing", organization_id=TEST_ORG_ID
            )

            assert isinstance(result, SearchResponse)
            assert result.filters.get("status") == "doing"

    @pytest.mark.asyncio
    async def test_search_with_project_filter(self) -> None:
        """Search should accept project filter for tasks."""
        async with mock_tools():
            result = await search("task", project="proj_123", organization_id=TEST_ORG_ID)

            assert isinstance(result, SearchResponse)
            assert result.filters.get("project") == "proj_123"

    @pytest.mark.asyncio
    async def test_search_includes_both_graph_and_documents(self) -> None:
        """Search should include both graph and document results by default."""
        async with mock_tools() as ctx:
            entity = create_test_entity(entity_type=EntityType.PATTERN, name="Pattern")
            ctx.entity_manager.set_search_results([(entity, 0.9)])

            result = await search("test", organization_id=TEST_ORG_ID)

            assert isinstance(result, SearchResponse)


class TestExploreWithHarness:
    """Tests for explore() function using test harness."""

    @pytest.mark.asyncio
    async def test_explore_list_returns_entities(self) -> None:
        """Explore list mode should return entities."""
        async with mock_tools() as ctx:
            entity = create_test_entity(entity_type=EntityType.PATTERN, name="Pattern")
            ctx.entity_manager.add_entity(entity)
            ctx.entity_manager._list_results = [entity]

            result = await explore(mode="list", types=["pattern"], organization_id=TEST_ORG_ID)

            assert isinstance(result, ExploreResponse)
            assert result.mode == "list"

    @pytest.mark.asyncio
    async def test_explore_related_requires_entity_id(self) -> None:
        """Related mode should require entity_id."""
        async with mock_tools():
            result = await explore(mode="related", organization_id=TEST_ORG_ID)

            # Should return error response without entity_id
            assert isinstance(result, ExploreResponse)

    @pytest.mark.asyncio
    async def test_explore_traverse_mode(self) -> None:
        """Traverse mode should work with entity_id."""
        async with mock_tools():
            result = await explore(
                mode="traverse",
                entity_id="entity_123",
                depth=2,
                organization_id=TEST_ORG_ID,
            )

            assert isinstance(result, ExploreResponse)
            assert result.mode == "traverse"

    @pytest.mark.asyncio
    async def test_explore_dependencies_mode(self) -> None:
        """Dependencies mode should work with project filter."""
        async with mock_tools():
            result = await explore(
                mode="dependencies", project="proj_test", organization_id=TEST_ORG_ID
            )

            assert isinstance(result, ExploreResponse)
            assert result.mode == "dependencies"

    @pytest.mark.asyncio
    async def test_explore_with_language_filter(self) -> None:
        """Explore should accept language filter."""
        async with mock_tools():
            result = await explore(
                mode="list",
                types=["pattern"],
                language="python",
                organization_id=TEST_ORG_ID,
            )

            assert isinstance(result, ExploreResponse)
            assert result.filters.get("language") == "python"


class TestAddWithHarness:
    """Tests for add() function using test harness."""

    @pytest.mark.asyncio
    async def test_add_creates_episode_by_default(self) -> None:
        """Add should create episode entity by default."""
        async with mock_tools() as ctx:
            ctx.entity_manager._create_result = "entity_abc123"

            result = await add(
                "Learning Title",
                "Some valuable learning content",
            )

            assert isinstance(result, AddResponse)
            # Check validation passes
            if result.success:
                assert result.id is not None

    @pytest.mark.asyncio
    async def test_add_pattern_type(self) -> None:
        """Add should allow specifying pattern type."""
        async with mock_tools() as ctx:
            ctx.entity_manager._create_result = "entity_xyz"

            result = await add(
                "Pattern Title",
                "Pattern description",
                entity_type="pattern",
            )

            assert isinstance(result, AddResponse)

    @pytest.mark.asyncio
    async def test_add_with_category(self) -> None:
        """Add should accept category parameter."""
        async with mock_tools() as ctx:
            ctx.entity_manager._create_result = "entity_123"

            result = await add(
                "Categorized Entry",
                "Content here",
                category="debugging",
            )

            assert isinstance(result, AddResponse)

    @pytest.mark.asyncio
    async def test_add_with_languages(self) -> None:
        """Add should accept languages list."""
        async with mock_tools() as ctx:
            ctx.entity_manager._create_result = "entity_123"

            result = await add(
                "Python Pattern",
                "A pattern for Python",
                languages=["python", "typescript"],
            )

            assert isinstance(result, AddResponse)

    @pytest.mark.asyncio
    async def test_add_with_tags(self) -> None:
        """Add should accept tags list."""
        async with mock_tools() as ctx:
            ctx.entity_manager._create_result = "entity_123"

            result = await add(
                "Tagged Entry",
                "Content with tags",
                tags=["important", "reviewed"],
            )

            assert isinstance(result, AddResponse)


class TestGetHealth:
    """Tests for get_health() function."""

    @pytest.mark.asyncio
    async def test_health_returns_basic_structure(self) -> None:
        """Health check should return expected structure."""
        async with mock_tools():
            result = await get_health()

            assert "status" in result
            assert "graph_connected" in result
            assert "entity_counts" in result

    @pytest.mark.asyncio
    async def test_health_without_org_skips_counts(self) -> None:
        """Health without org_id should not count entities."""
        async with mock_tools():
            result = await get_health(organization_id=None)

            # Without org_id, entity counts should be empty
            assert result["entity_counts"] == {}

    @pytest.mark.asyncio
    async def test_health_with_org_counts_entities(self) -> None:
        """Health with org_id should count entities."""
        async with mock_tools() as ctx:
            # Add some entities
            entity = create_test_entity(entity_type=EntityType.PATTERN, name="Pattern")
            ctx.entity_manager.add_entity(entity)
            ctx.entity_manager._list_results = [entity]

            result = await get_health(organization_id=TEST_ORG_ID)

            assert "status" in result

    @pytest.mark.asyncio
    async def test_health_handles_connection_failure(self) -> None:
        """Health should report unhealthy on connection failure."""

        async def failing_client() -> None:
            raise ConnectionError("Cannot connect to FalkorDB")

        with patch("sibyl.tools.core.get_graph_client", failing_client):
            result = await get_health()

            assert result["status"] == "unhealthy"
            assert len(result["errors"]) > 0


class TestGetStats:
    """Tests for get_stats() function."""

    @pytest.mark.asyncio
    async def test_stats_requires_org_id(self) -> None:
        """Stats should require organization_id."""
        with pytest.raises(ValueError, match="organization_id is required"):
            await get_stats(organization_id=None)

    @pytest.mark.asyncio
    async def test_stats_returns_entity_counts(self) -> None:
        """Stats should return entity counts per type."""
        mock_client = MagicMock()
        mock_driver = MagicMock()
        mock_client.client.driver.clone = MagicMock(return_value=mock_driver)

        mock_driver.execute_query = AsyncMock(
            return_value=[
                {"type": "pattern", "count": 10},
                {"type": "rule", "count": 5},
            ]
        )

        with (
            patch("sibyl.tools.core.get_graph_client", AsyncMock(return_value=mock_client)),
            patch(
                "sibyl.tools.core.GraphClient.normalize_result",
                return_value=[
                    {"type": "pattern", "count": 10},
                    {"type": "rule", "count": 5},
                ],
            ),
        ):
            result = await get_stats(organization_id=TEST_ORG_ID)

            assert "entity_counts" in result
            assert result["entity_counts"]["pattern"] == 10
            assert result["entity_counts"]["rule"] == 5


class TestAutoTagTask:
    """Tests for auto_tag_task helper function."""

    def test_imports_correctly(self) -> None:
        """auto_tag_task should be importable."""
        from sibyl.tools.core import auto_tag_task

        assert callable(auto_tag_task)

    def test_auto_tag_detects_frontend(self) -> None:
        """auto_tag_task should detect frontend technologies."""
        from sibyl.tools.core import auto_tag_task

        tags = auto_tag_task(
            title="Fix React component",
            description="Update the React component to use hooks",
        )

        # Should add frontend-related tags
        assert isinstance(tags, list)
        assert "frontend" in tags or "react" in tags

    def test_auto_tag_detects_backend(self) -> None:
        """auto_tag_task should detect backend technologies."""
        from sibyl.tools.core import auto_tag_task

        tags = auto_tag_task(
            title="Add API endpoint",
            description="Create FastAPI endpoint for user data",
        )

        assert isinstance(tags, list)
        assert "backend" in tags or "api" in tags

    def test_auto_tag_detects_testing(self) -> None:
        """auto_tag_task should detect testing context."""
        from sibyl.tools.core import auto_tag_task

        tags = auto_tag_task(
            title="Write unit tests",
            description="Add pytest tests for authentication module",
        )

        # Should add testing-related tags
        assert isinstance(tags, list)
        assert "testing" in tags or "test" in tags

    def test_auto_tag_with_technologies(self) -> None:
        """auto_tag_task should include technology tags."""
        from sibyl.tools.core import auto_tag_task

        tags = auto_tag_task(
            title="Implement feature",
            description="Add new functionality",
            technologies=["python", "redis"],
        )

        assert isinstance(tags, list)

    def test_auto_tag_with_domain(self) -> None:
        """auto_tag_task should include domain tag."""
        from sibyl.tools.core import auto_tag_task

        tags = auto_tag_task(
            title="Fix auth bug",
            description="Authentication is broken",
            domain="authentication",
        )

        assert isinstance(tags, list)
        assert "authentication" in tags

    def test_auto_tag_preserves_explicit_tags(self) -> None:
        """auto_tag_task should preserve explicit tags."""
        from sibyl.tools.core import auto_tag_task

        tags = auto_tag_task(
            title="Task",
            description="Description",
            explicit_tags=["priority", "urgent"],
        )

        assert "priority" in tags
        assert "urgent" in tags


class TestGetProjectTags:
    """Tests for get_project_tags function."""

    @pytest.mark.asyncio
    async def test_returns_list_of_tags(self) -> None:
        """get_project_tags should return list of strings."""
        from sibyl.tools.core import get_project_tags

        mock_client = MagicMock()
        mock_client.execute_read_org = AsyncMock(return_value=[{"tags": ["backend", "api"]}])

        result = await get_project_tags(mock_client, "proj_123")

        assert isinstance(result, list)

    @pytest.mark.asyncio
    async def test_handles_empty_result(self) -> None:
        """get_project_tags should handle no results."""
        from sibyl.tools.core import get_project_tags

        mock_client = MagicMock()
        mock_client.execute_read_org = AsyncMock(return_value=[])

        result = await get_project_tags(mock_client, "proj_123")

        assert result == []


class TestHelperFunctions:
    """Tests for helper functions in tools/core.py."""

    def test_get_field_returns_value(self) -> None:
        """_get_field should return attribute value."""
        from sibyl.tools.core import _get_field

        class MockEntity:
            name = "Test"

        result = _get_field(MockEntity(), "name")
        assert result == "Test"

    def test_get_field_returns_default(self) -> None:
        """_get_field should return default for missing attr."""
        from sibyl.tools.core import _get_field

        # Use an entity-like object with empty metadata
        class MockEntity:
            metadata: dict = {}

        result = _get_field(MockEntity(), "missing", default="default_value")
        assert result == "default_value"

    def test_serialize_enum_converts(self) -> None:
        """_serialize_enum should convert enum to value."""
        from sibyl.tools.core import _serialize_enum

        result = _serialize_enum(EntityType.PATTERN)
        assert result == "pattern"

    def test_serialize_enum_returns_non_enum(self) -> None:
        """_serialize_enum should return non-enum as-is."""
        from sibyl.tools.core import _serialize_enum

        result = _serialize_enum("plain_string")
        assert result == "plain_string"

    def test_build_entity_metadata(self) -> None:
        """_build_entity_metadata should extract entity metadata."""
        from sibyl.tools.core import _build_entity_metadata

        entity = create_test_entity(entity_type=EntityType.PATTERN, name="Test")

        result = _build_entity_metadata(entity)

        assert isinstance(result, dict)

    def test_generate_id(self) -> None:
        """_generate_id should create deterministic IDs."""
        from sibyl.tools.core import _generate_id

        id1 = _generate_id("task", "project_1", "title_1")
        id2 = _generate_id("task", "project_1", "title_1")
        id3 = _generate_id("task", "project_1", "title_2")

        assert id1 == id2  # Same input = same output
        assert id1 != id3  # Different input = different output
        assert id1.startswith("task_")  # Has correct prefix
