"""Tests for community summarization module."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sibyl.graph.summarize import (
    CommunitySummary,
    SummaryConfig,
    format_entity_for_prompt,
    generate_community_summaries,
    generate_community_summary,
    get_community_content,
    store_community_summary,
    update_stale_summaries,
)


class TestSummaryConfig:
    """Tests for SummaryConfig dataclass."""

    def test_default_values(self) -> None:
        """Test default configuration values."""
        config = SummaryConfig()

        assert config.model == "gpt-4o-mini"
        assert config.max_members_per_summary == 20
        assert config.max_content_tokens == 4000
        assert config.extract_concepts is True
        assert config.max_concepts == 5

    def test_custom_values(self) -> None:
        """Test custom configuration values."""
        config = SummaryConfig(
            model="gpt-4",
            max_members_per_summary=10,
            max_content_tokens=2000,
            extract_concepts=False,
            max_concepts=3,
        )

        assert config.model == "gpt-4"
        assert config.max_members_per_summary == 10
        assert config.extract_concepts is False


class TestCommunitySummary:
    """Tests for CommunitySummary dataclass."""

    def test_default_values(self) -> None:
        """Test default field values."""
        summary = CommunitySummary(
            community_id="c1",
            summary="Test summary",
        )

        assert summary.key_concepts == []
        assert summary.representative_entities == []

    def test_full_values(self) -> None:
        """Test with all fields populated."""
        summary = CommunitySummary(
            community_id="c1",
            summary="Test summary",
            key_concepts=["error handling", "logging"],
            representative_entities=["e1", "e2"],
        )

        assert len(summary.key_concepts) == 2
        assert summary.representative_entities[0] == "e1"


class TestFormatEntityForPrompt:
    """Tests for format_entity_for_prompt function."""

    def test_basic_formatting(self) -> None:
        """Format entity with all fields."""
        entity = {
            "name": "Error Handling",
            "type": "pattern",
            "description": "Best practices for handling errors",
        }

        result = format_entity_for_prompt(entity)

        assert "[pattern]" in result
        assert "Error Handling" in result
        assert "Best practices" in result

    def test_missing_fields(self) -> None:
        """Handle missing fields gracefully."""
        entity = {"name": "Test"}

        result = format_entity_for_prompt(entity)

        assert "[entity]" in result  # Default type
        assert "Test" in result

    def test_truncation(self) -> None:
        """Long descriptions are truncated."""
        long_desc = "x" * 1000
        entity = {
            "name": "Test",
            "type": "pattern",
            "description": long_desc,
        }

        result = format_entity_for_prompt(entity, max_chars=100)

        assert len(result) < 200  # Reasonable length
        assert "..." in result


class TestGetCommunityContent:
    """Tests for get_community_content function."""

    @pytest.fixture
    def mock_client(self) -> MagicMock:
        """Create mock graph client."""
        client = MagicMock()
        client.client.driver.execute_query = AsyncMock(return_value=[])
        return client

    @pytest.mark.asyncio
    async def test_empty_community(self, mock_client: MagicMock) -> None:
        """Empty community returns empty list."""
        members = await get_community_content(mock_client, "c1")
        assert members == []

    @pytest.mark.asyncio
    async def test_returns_members(self, mock_client: MagicMock) -> None:
        """Returns member entities."""
        mock_client.client.driver.execute_query = AsyncMock(
            return_value=[
                ("e1", "Error Handling", "pattern", "Description 1", "Content 1"),
                ("e2", "Logging", "pattern", "Description 2", "Content 2"),
            ]
        )

        members = await get_community_content(mock_client, "c1")

        assert len(members) == 2
        assert members[0]["id"] == "e1"
        assert members[0]["name"] == "Error Handling"
        assert members[1]["content"] == "Content 2"

    @pytest.mark.asyncio
    async def test_respects_limit(self, mock_client: MagicMock) -> None:
        """Query includes limit parameter."""
        await get_community_content(mock_client, "c1", max_members=5)

        call_args = mock_client.client.driver.execute_query.call_args
        assert call_args.kwargs.get("limit") == 5


class TestGenerateCommunitySummary:
    """Tests for generate_community_summary function."""

    @pytest.fixture
    def mock_client(self) -> MagicMock:
        """Create mock graph client."""
        client = MagicMock()
        client.client.driver.execute_query = AsyncMock(return_value=[])
        return client

    @pytest.mark.asyncio
    async def test_no_members(self, mock_client: MagicMock) -> None:
        """Returns None for community with no members."""
        summary = await generate_community_summary(mock_client, "c1")
        assert summary is None

    @pytest.mark.asyncio
    async def test_with_members_and_mock_openai(self, mock_client: MagicMock) -> None:
        """Generates summary with mocked OpenAI."""
        # Mock community members
        mock_client.client.driver.execute_query = AsyncMock(
            return_value=[
                ("e1", "Error Handling", "pattern", "Handle errors gracefully", ""),
                ("e2", "Logging", "pattern", "Log important events", ""),
            ]
        )

        # Mock OpenAI response
        mock_openai_response = MagicMock()
        mock_openai_response.choices = [
            MagicMock(
                message=MagicMock(
                    content='{"summary": "Test summary", "key_concepts": ["errors", "logging"]}'
                )
            )
        ]

        with patch("openai.AsyncOpenAI") as mock_openai_class:
            mock_openai_client = MagicMock()
            mock_openai_client.chat.completions.create = AsyncMock(
                return_value=mock_openai_response
            )
            mock_openai_class.return_value = mock_openai_client

            summary = await generate_community_summary(mock_client, "c1")

            assert summary is not None
            assert summary.summary == "Test summary"
            assert "errors" in summary.key_concepts
            assert summary.community_id == "c1"


class TestStoreCommunitySum:
    """Tests for store_community_summary function."""

    @pytest.fixture
    def mock_client(self) -> MagicMock:
        """Create mock graph client."""
        client = MagicMock()
        client.client.driver.execute_query = AsyncMock(return_value=[("c1",)])
        return client

    @pytest.mark.asyncio
    async def test_stores_summary(self, mock_client: MagicMock) -> None:
        """Summary is stored in graph."""
        summary = CommunitySummary(
            community_id="c1",
            summary="Test summary",
            key_concepts=["error handling", "logging"],
            representative_entities=["e1", "e2"],
        )

        result = await store_community_summary(mock_client, summary)

        assert result is True
        mock_client.client.driver.execute_query.assert_called_once()

        call_args = mock_client.client.driver.execute_query.call_args
        assert call_args.kwargs["summary"] == "Test summary"
        assert "error handling" in call_args.kwargs["key_concepts"]

    @pytest.mark.asyncio
    async def test_generates_name_from_concepts(self, mock_client: MagicMock) -> None:
        """Name is generated from key concepts."""
        summary = CommunitySummary(
            community_id="c1",
            summary="Test summary",
            key_concepts=["error handling", "logging", "monitoring"],
        )

        await store_community_summary(mock_client, summary)

        call_args = mock_client.client.driver.execute_query.call_args
        name = call_args.kwargs["name"]
        assert "error handling" in name
        assert "logging" in name


class TestGenerateCommunitySummaries:
    """Tests for generate_community_summaries function."""

    @pytest.fixture
    def mock_client(self) -> MagicMock:
        """Create mock graph client."""
        client = MagicMock()
        client.client.driver.execute_query = AsyncMock(return_value=[])
        return client

    @pytest.mark.asyncio
    async def test_fetches_community_ids(self, mock_client: MagicMock) -> None:
        """Fetches community IDs when not provided."""
        mock_client.client.driver.execute_query = AsyncMock(return_value=[("c1",), ("c2",)])

        await generate_community_summaries(mock_client, store=False)

        # First call should fetch community IDs
        first_call = mock_client.client.driver.execute_query.call_args_list[0]
        assert "entity_type: 'community'" in first_call[0][0]

    @pytest.mark.asyncio
    async def test_uses_provided_ids(self, mock_client: MagicMock) -> None:
        """Uses provided community IDs directly."""
        await generate_community_summaries(
            mock_client,
            community_ids=["c1", "c2"],
            store=False,
        )

        # Should not query for community IDs
        if mock_client.client.driver.execute_query.call_count > 0:
            first_call = mock_client.client.driver.execute_query.call_args_list[0]
            # First call should be for member content, not community listing
            assert "BELONGS_TO" in first_call[0][0]

    @pytest.mark.asyncio
    async def test_empty_communities(self, mock_client: MagicMock) -> None:
        """Returns empty list for no communities."""
        summaries = await generate_community_summaries(mock_client, community_ids=[])
        assert summaries == []


class TestUpdateStaleSummaries:
    """Tests for update_stale_summaries function."""

    @pytest.fixture
    def mock_client(self) -> MagicMock:
        """Create mock graph client."""
        client = MagicMock()
        client.client.driver.execute_query = AsyncMock(return_value=[])
        return client

    @pytest.mark.asyncio
    async def test_no_stale_summaries(self, mock_client: MagicMock) -> None:
        """Returns 0 when no stale summaries."""
        count = await update_stale_summaries(mock_client)
        assert count == 0

    @pytest.mark.asyncio
    async def test_finds_stale_communities(self, mock_client: MagicMock) -> None:
        """Identifies communities with stale summaries."""
        mock_client.client.driver.execute_query = AsyncMock(
            side_effect=[
                [("c1",), ("c2",)],  # Stale community IDs
                [],  # No members for c1
                [],  # No members for c2
            ]
        )

        await update_stale_summaries(mock_client)

        # First call should query for stale communities
        first_call = mock_client.client.driver.execute_query.call_args_list[0]
        assert "updated_at" in first_call[0][0]
