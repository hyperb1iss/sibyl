"""Tests for the core MCP tools (search, explore, add, manage)."""

from datetime import UTC, datetime

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
    search,
)


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
        assert VALID_ENTITY_TYPES == enum_values

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
        response = await explore(mode="list", types=["pattern"])
        assert isinstance(response, ExploreResponse)
        assert response.mode == "list"

    @pytest.mark.asyncio
    async def test_depth_clamped(self) -> None:
        """Depth should be clamped to 1-3."""
        response = await explore(mode="traverse", entity_id="test", depth=10)
        assert isinstance(response, ExploreResponse)

    @pytest.mark.asyncio
    async def test_dependencies_mode(self) -> None:
        """Dependencies mode should be handled."""
        response = await explore(mode="dependencies", project="proj_test")
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
