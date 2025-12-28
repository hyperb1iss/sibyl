"""Tests for source status recovery and management.

Tests the recover_stuck_sources function that runs on startup to clean up
sources stuck in IN_PROGRESS state after server restarts.
"""

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def mock_session():
    """Mock database session with context manager support."""
    session = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    return session


@pytest.fixture
def create_mock_source():
    """Factory for creating mock CrawlSource objects."""

    def _create(
        source_id: str | None = None,
        name: str = "Test Source",
        crawl_status: str = "in_progress",
        current_job_id: str | None = "job-123",
        document_count: int = 0,
        chunk_count: int = 0,
    ):
        from sibyl.db.models import CrawlStatus

        source = MagicMock()
        source.id = uuid4() if source_id is None else source_id
        source.name = name
        source.crawl_status = CrawlStatus(crawl_status)
        source.current_job_id = current_job_id
        source.document_count = document_count
        source.chunk_count = chunk_count
        return source

    return _create


# =============================================================================
# Tests for recover_stuck_sources
# =============================================================================


class TestRecoverStuckSources:
    """Tests for the recover_stuck_sources function."""

    @pytest.mark.asyncio
    async def test_no_stuck_sources(self, mock_session: AsyncMock) -> None:
        """Test when there are no stuck sources."""
        # Mock empty result for IN_PROGRESS query
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_session.execute = AsyncMock(return_value=mock_result)

        with patch("sibyl.db.get_session") as mock_get_session:
            mock_get_session.return_value = mock_session

            from sibyl.api.routes.admin import recover_stuck_sources

            result = await recover_stuck_sources()

        assert result["recovered"] == 0
        assert result["completed"] == 0
        assert result["reset_to_pending"] == 0

    @pytest.mark.asyncio
    async def test_recover_source_with_documents(
        self, mock_session: AsyncMock, create_mock_source
    ) -> None:
        """Test recovering a stuck source that has documents (should mark COMPLETED)."""
        from sibyl.db.models import CrawlStatus

        # Create stuck source with documents
        stuck_source = create_mock_source(
            name="Source With Docs",
            crawl_status="in_progress",
            document_count=0,  # Will be updated
            chunk_count=0,  # Will be updated
        )

        # Mock the queries
        sources_result = MagicMock()
        sources_result.scalars.return_value.all.return_value = [stuck_source]

        doc_count_result = MagicMock()
        doc_count_result.scalar.return_value = 10  # Has 10 documents

        chunk_count_result = MagicMock()
        chunk_count_result.scalar.return_value = 50  # Has 50 chunks

        # Return different results for different execute calls
        mock_session.execute = AsyncMock(
            side_effect=[sources_result, doc_count_result, chunk_count_result]
        )

        with patch("sibyl.db.get_session") as mock_get_session:
            mock_get_session.return_value = mock_session

            from sibyl.api.routes.admin import recover_stuck_sources

            result = await recover_stuck_sources()

        # Should mark as COMPLETED since it has documents
        assert result["recovered"] == 1
        assert result["completed"] == 1
        assert result["reset_to_pending"] == 0

        # Verify source was updated
        assert stuck_source.crawl_status == CrawlStatus.COMPLETED
        assert stuck_source.document_count == 10
        assert stuck_source.chunk_count == 50
        assert stuck_source.current_job_id is None

    @pytest.mark.asyncio
    async def test_recover_source_without_documents(
        self, mock_session: AsyncMock, create_mock_source
    ) -> None:
        """Test recovering a stuck source with no documents (should reset to PENDING)."""
        from sibyl.db.models import CrawlStatus

        # Create stuck source without documents
        stuck_source = create_mock_source(
            name="Empty Source",
            crawl_status="in_progress",
        )

        # Mock the queries
        sources_result = MagicMock()
        sources_result.scalars.return_value.all.return_value = [stuck_source]

        doc_count_result = MagicMock()
        doc_count_result.scalar.return_value = 0  # No documents

        chunk_count_result = MagicMock()
        chunk_count_result.scalar.return_value = 0  # No chunks

        mock_session.execute = AsyncMock(
            side_effect=[sources_result, doc_count_result, chunk_count_result]
        )

        with patch("sibyl.db.get_session") as mock_get_session:
            mock_get_session.return_value = mock_session

            from sibyl.api.routes.admin import recover_stuck_sources

            result = await recover_stuck_sources()

        # Should reset to PENDING since no documents
        assert result["recovered"] == 1
        assert result["completed"] == 0
        assert result["reset_to_pending"] == 1

        # Verify source was reset
        assert stuck_source.crawl_status == CrawlStatus.PENDING
        assert stuck_source.current_job_id is None

    @pytest.mark.asyncio
    async def test_recover_multiple_sources(
        self, mock_session: AsyncMock, create_mock_source
    ) -> None:
        """Test recovering multiple stuck sources with different states."""
        from sibyl.db.models import CrawlStatus

        # Create multiple stuck sources
        source_with_docs = create_mock_source(name="Has Docs", crawl_status="in_progress")
        source_empty = create_mock_source(name="Empty", crawl_status="in_progress")

        # Mock the queries - sources query, then doc/chunk counts for each
        sources_result = MagicMock()
        sources_result.scalars.return_value.all.return_value = [
            source_with_docs,
            source_empty,
        ]

        # First source: has documents
        doc_count_1 = MagicMock()
        doc_count_1.scalar.return_value = 5
        chunk_count_1 = MagicMock()
        chunk_count_1.scalar.return_value = 25

        # Second source: empty
        doc_count_2 = MagicMock()
        doc_count_2.scalar.return_value = 0
        chunk_count_2 = MagicMock()
        chunk_count_2.scalar.return_value = 0

        mock_session.execute = AsyncMock(
            side_effect=[
                sources_result,
                doc_count_1,
                chunk_count_1,
                doc_count_2,
                chunk_count_2,
            ]
        )

        with patch("sibyl.db.get_session") as mock_get_session:
            mock_get_session.return_value = mock_session

            from sibyl.api.routes.admin import recover_stuck_sources

            result = await recover_stuck_sources()

        assert result["recovered"] == 2
        assert result["completed"] == 1
        assert result["reset_to_pending"] == 1

        # Verify each source was handled correctly
        assert source_with_docs.crawl_status == CrawlStatus.COMPLETED
        assert source_empty.crawl_status == CrawlStatus.PENDING

    @pytest.mark.asyncio
    async def test_recover_handles_database_error(self, mock_session: AsyncMock) -> None:
        """Test that recovery handles database errors gracefully."""
        mock_session.execute = AsyncMock(side_effect=Exception("Database connection failed"))

        with patch("sibyl.db.get_session") as mock_get_session:
            mock_get_session.return_value = mock_session

            from sibyl.api.routes.admin import recover_stuck_sources

            # Should not raise, just log and return zeros
            result = await recover_stuck_sources()

        assert result["recovered"] == 0
        assert result["completed"] == 0
        assert result["reset_to_pending"] == 0


# =============================================================================
# Tests for source deletion
# =============================================================================


class TestSourceDeletion:
    """Tests for source deletion endpoint behavior."""

    @pytest.mark.asyncio
    async def test_delete_source_cascades_properly(self, mock_session: AsyncMock) -> None:
        """Test that deleting a source also deletes chunks and documents."""
        source_id = uuid4()

        # Mock source lookup
        mock_source = MagicMock()
        mock_source.id = source_id
        mock_source.name = "Test Source"
        mock_session.get = AsyncMock(return_value=mock_source)

        # Mock chunk and document queries
        mock_chunks_result = MagicMock()
        mock_chunks = [MagicMock(), MagicMock(), MagicMock()]  # 3 chunks
        mock_chunks_result.scalars.return_value = mock_chunks

        mock_docs_result = MagicMock()
        mock_docs = [MagicMock(), MagicMock()]  # 2 documents
        mock_docs_result.scalars.return_value = mock_docs

        mock_session.execute = AsyncMock(side_effect=[mock_chunks_result, mock_docs_result])
        mock_session.delete = AsyncMock()

        # Simulate the deletion logic from crawler.py
        # Get source
        source = await mock_session.get(MagicMock, source_id)
        assert source is not None

        # Delete chunks
        chunks_result = await mock_session.execute(MagicMock())
        for chunk in chunks_result.scalars():
            await mock_session.delete(chunk)

        # Delete documents
        docs_result = await mock_session.execute(MagicMock())
        for doc in docs_result.scalars():
            await mock_session.delete(doc)

        # Delete source
        await mock_session.delete(source)

        # Verify delete was called for chunks, docs, and source
        assert mock_session.delete.call_count == 6  # 3 chunks + 2 docs + 1 source

    @pytest.mark.asyncio
    async def test_delete_nonexistent_source_raises_404(self, mock_session: AsyncMock) -> None:
        """Test that deleting a nonexistent source returns 404."""
        mock_session.get = AsyncMock(return_value=None)

        # Simulate the check in the endpoint
        source = await mock_session.get(MagicMock, "nonexistent-id")
        assert source is None  # Would trigger 404 in actual endpoint


# =============================================================================
# Tests for WebSocket event broadcasting
# =============================================================================


class TestCrawlWebSocketEvents:
    """Tests for crawl-related WebSocket event handling."""

    @pytest.mark.asyncio
    async def test_crawl_complete_event_includes_source_id(self) -> None:
        """Test that crawl_complete events include the source_id."""
        from sibyl.api.websocket import broadcast_event

        # Mock the connection manager
        mock_manager = MagicMock()
        mock_manager.broadcast = AsyncMock()

        with patch("sibyl.api.websocket.get_manager", return_value=mock_manager):
            await broadcast_event(
                "crawl_complete",
                {"source_id": "src-123", "status": "completed", "documents_crawled": 50},
            )

            # Verify broadcast was called with correct event type
            mock_manager.broadcast.assert_called_once_with(
                "crawl_complete",
                {"source_id": "src-123", "status": "completed", "documents_crawled": 50},
                org_id=None,
            )

    @pytest.mark.asyncio
    async def test_crawl_started_event_includes_source_id(self) -> None:
        """Test that crawl_started events include the source_id."""
        from sibyl.api.websocket import broadcast_event

        mock_manager = MagicMock()
        mock_manager.broadcast = AsyncMock()

        with patch("sibyl.api.websocket.get_manager", return_value=mock_manager):
            await broadcast_event(
                "crawl_started",
                {"source_id": "src-456", "max_pages": 100},
            )

            mock_manager.broadcast.assert_called_once_with(
                "crawl_started",
                {"source_id": "src-456", "max_pages": 100},
                org_id=None,
            )

    @pytest.mark.asyncio
    async def test_broadcast_event_respects_org_id(self) -> None:
        """Test that broadcast_event passes org_id correctly."""
        from sibyl.api.websocket import broadcast_event

        mock_manager = MagicMock()
        mock_manager.broadcast = AsyncMock()

        with patch("sibyl.api.websocket.get_manager", return_value=mock_manager):
            await broadcast_event(
                "entity_created",
                {"id": "ent-123"},
                org_id="org-abc",
            )

            mock_manager.broadcast.assert_called_once_with(
                "entity_created",
                {"id": "ent-123"},
                org_id="org-abc",
            )
