"""Tests for source and document models."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from sibyl.models.entities import EntityType
from sibyl.models.sources import (
    Community,
    CrawlStatus,
    Document,
    Source,
    SourceType,
)


class TestSourceTypeEnum:
    """Tests for SourceType enum."""

    def test_all_source_type_values(self) -> None:
        """Verify all expected source type values exist."""
        expected = {"website", "github", "local", "api_docs"}
        actual = {s.value for s in SourceType}
        assert actual == expected


class TestCrawlStatusEnum:
    """Tests for CrawlStatus enum."""

    def test_all_crawl_status_values(self) -> None:
        """Verify all expected crawl status values exist."""
        expected = {"pending", "in_progress", "completed", "failed", "partial"}
        actual = {s.value for s in CrawlStatus}
        assert actual == expected


class TestSource:
    """Tests for Source entity model."""

    def test_minimal_source_creation(self) -> None:
        """Test creating a source with minimal fields."""
        source = Source(
            id="src-001",
            url="https://docs.example.com",
        )
        assert source.id == "src-001"
        assert source.url == "https://docs.example.com"
        assert source.entity_type == EntityType.SOURCE
        assert source.source_type == SourceType.WEBSITE
        assert source.crawl_depth == 2
        assert source.respect_robots is True
        assert source.crawl_status == CrawlStatus.PENDING

    def test_full_source_creation(self) -> None:
        """Test creating a source with all fields."""
        now = datetime.now(UTC)
        source = Source(
            id="src-002",
            url="https://docs.python.org",
            description="Python official documentation",
            source_type=SourceType.WEBSITE,
            crawl_depth=3,
            crawl_patterns=[r"/library/.*", r"/reference/.*"],
            exclude_patterns=[r"/download/.*"],
            schedule="0 0 * * 0",  # Weekly on Sunday
            respect_robots=True,
            last_crawled=now,
            crawl_status=CrawlStatus.COMPLETED,
            document_count=150,
            total_tokens=500000,
            total_entities=450,
        )
        assert source.source_type == SourceType.WEBSITE
        assert source.crawl_depth == 3
        assert len(source.crawl_patterns) == 2
        assert source.document_count == 150

    def test_source_name_property(self) -> None:
        """Test name property extracts domain from URL."""
        source = Source(id="s1", url="https://docs.example.com/api/v1")
        assert source.name == "docs.example.com/api/v1"

    def test_source_name_with_trailing_slash(self) -> None:
        """Test name property handles trailing slashes."""
        source = Source(id="s1", url="https://example.com/")
        assert source.name == "example.com"

    def test_source_content_property(self) -> None:
        """Test content property returns description or URL."""
        source1 = Source(id="s1", url="https://example.com", description="Test docs")
        assert source1.content == "Test docs"

        source2 = Source(id="s2", url="https://example.com")
        assert source2.content == "https://example.com"

    def test_crawl_depth_bounds(self) -> None:
        """Test crawl_depth validation bounds."""
        # Valid bounds
        Source(id="s1", url="https://a.com", crawl_depth=0)
        Source(id="s2", url="https://a.com", crawl_depth=10)

        # Invalid bounds
        with pytest.raises(ValidationError):
            Source(id="s3", url="https://a.com", crawl_depth=11)
        with pytest.raises(ValidationError):
            Source(id="s4", url="https://a.com", crawl_depth=-1)

    def test_github_source_type(self) -> None:
        """Test GitHub source type."""
        source = Source(
            id="src-gh",
            url="https://github.com/org/repo",
            source_type=SourceType.GITHUB,
        )
        assert source.source_type == SourceType.GITHUB

    def test_local_source_type(self) -> None:
        """Test local source type."""
        source = Source(
            id="src-local",
            url="/Users/bliss/dev/docs",
            source_type=SourceType.LOCAL,
        )
        assert source.source_type == SourceType.LOCAL


class TestDocument:
    """Tests for Document entity model."""

    def test_minimal_document_creation(self) -> None:
        """Test creating a document with minimal fields."""
        doc = Document(
            id="doc-001",
            source_id="src-001",
            url="https://docs.example.com/api",
        )
        assert doc.id == "doc-001"
        assert doc.source_id == "src-001"
        assert doc.entity_type == EntityType.DOCUMENT
        assert doc.depth == 0
        assert doc.is_index is False

    def test_full_document_creation(self) -> None:
        """Test creating a document with all fields."""
        now = datetime.now(UTC)
        doc = Document(
            id="doc-002",
            source_id="src-001",
            url="https://docs.example.com/api/auth",
            title="Authentication API",
            content="# Authentication\n\nThis section covers...",
            parent_url="https://docs.example.com/api",
            section_path=["Docs", "API", "Auth"],
            depth=2,
            extracted_entities=["ent-001", "ent-002"],
            headings=["Authentication", "OAuth 2.0", "JWT"],
            links=["https://docs.example.com/api/tokens"],
            crawled_at=now,
            content_hash="abc123def456",
            word_count=500,
            token_count=750,
            is_index=False,
            has_code=True,
            language="python",
        )
        assert doc.title == "Authentication API"
        assert len(doc.section_path) == 3
        assert doc.has_code is True
        assert doc.language == "python"

    def test_document_name_property_with_title(self) -> None:
        """Test name property returns title when available."""
        doc = Document(
            id="d1",
            source_id="s1",
            url="https://example.com/page",
            title="Page Title",
        )
        assert doc.name == "Page Title"

    def test_document_name_property_without_title(self) -> None:
        """Test name property falls back to URL segment."""
        doc = Document(
            id="d1",
            source_id="s1",
            url="https://example.com/api/auth",
        )
        assert doc.name == "auth"

    def test_document_name_property_root_url(self) -> None:
        """Test name property handles root URLs."""
        doc = Document(
            id="d1",
            source_id="s1",
            url="https://example.com/",
        )
        # Root URL extracts domain as name
        assert doc.name == "example.com"

    def test_document_name_property_truly_empty(self) -> None:
        """Test name property handles empty title and URL segment."""
        doc = Document(
            id="d1",
            source_id="s1",
            url="",
            title="",
        )
        assert doc.name == "Untitled"


class TestCommunity:
    """Tests for Community entity model."""

    def test_minimal_community_creation(self) -> None:
        """Test creating a community with minimal fields."""
        community = Community(id="com-001")
        assert community.id == "com-001"
        assert community.entity_type == EntityType.COMMUNITY
        assert community.level == 0
        assert community.member_count == 0

    def test_full_community_creation(self) -> None:
        """Test creating a community with all fields."""
        community = Community(
            id="com-002",
            member_ids=["ent-001", "ent-002", "ent-003"],
            member_count=3,
            level=1,
            parent_community_id="com-parent",
            child_community_ids=["com-child-1", "com-child-2"],
            summary="This community covers authentication patterns and security best practices.",
            key_concepts=["authentication", "security", "OAuth"],
            representative_entities=["ent-001"],
            modularity=0.75,
            density=0.85,
        )
        assert len(community.member_ids) == 3
        assert community.level == 1
        assert len(community.key_concepts) == 3
        assert community.modularity == 0.75

    def test_community_name_with_concepts(self) -> None:
        """Test name property uses key concepts."""
        community = Community(
            id="c1",
            key_concepts=["auth", "security", "oauth", "jwt"],
            member_count=10,
        )
        # Should use first 3 concepts
        assert community.name == "auth, security, oauth"

    def test_community_name_without_concepts(self) -> None:
        """Test name property fallback without concepts."""
        community = Community(
            id="c1",
            level=2,
            member_count=15,
        )
        assert community.name == "Community L2 (15 members)"

    def test_community_content_property(self) -> None:
        """Test content property returns summary."""
        community = Community(
            id="c1",
            summary="Summary of community content",
        )
        assert community.content == "Summary of community content"

    def test_community_hierarchy(self) -> None:
        """Test community hierarchy fields."""
        parent = Community(
            id="com-parent",
            level=2,
            child_community_ids=["com-child-1", "com-child-2"],
        )
        child = Community(
            id="com-child-1",
            level=1,
            parent_community_id="com-parent",
        )
        assert parent.level > child.level
        assert child.parent_community_id == parent.id
        assert child.id in parent.child_community_ids
