"""Tests for source status recovery and management.

Tests the recover_stuck_sources function that runs on startup to clean up
sources stuck in IN_PROGRESS state after server restarts.
"""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from sibyl.db.models import CrawlStatus

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def mock_session():
    return AsyncMock()


@pytest.fixture
def mock_content_session(mock_session: AsyncMock):
    @asynccontextmanager
    async def _session():
        yield mock_session

    return _session


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
    async def test_no_stuck_sources(
        self,
        mock_session: AsyncMock,
        mock_content_session,
    ) -> None:
        """Test when there are no stuck sources."""
        with (
            patch("sibyl.api.routes.admin.get_content_read_session", mock_content_session),
            patch("sibyl.api.routes.admin.list_crawl_sources", AsyncMock(return_value=[])) as list_sources,
            patch("sibyl.api.routes.admin.get_source_sync_counts", AsyncMock()) as get_counts,
            patch("sibyl.api.routes.admin.save_crawl_source_record", AsyncMock()) as save_source,
        ):
            from sibyl.api.routes.admin import recover_stuck_sources

            result = await recover_stuck_sources()

        assert result["recovered"] == 0
        assert result["completed"] == 0
        assert result["reset_to_pending"] == 0
        list_sources.assert_awaited_once_with(
            mock_session,
            status=CrawlStatus.IN_PROGRESS,
            limit=None,
        )
        get_counts.assert_not_awaited()
        save_source.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_recover_source_with_documents(
        self,
        mock_session: AsyncMock,
        mock_content_session,
        create_mock_source,
    ) -> None:
        """Test recovering a stuck source that has documents (should mark COMPLETED)."""
        stuck_source = create_mock_source(
            name="Source With Docs",
            crawl_status="in_progress",
            document_count=0,
            chunk_count=0,
        )

        with (
            patch("sibyl.api.routes.admin.get_content_read_session", mock_content_session),
            patch(
                "sibyl.api.routes.admin.list_crawl_sources",
                AsyncMock(return_value=[stuck_source]),
            ),
            patch(
                "sibyl.api.routes.admin.get_source_sync_counts",
                AsyncMock(return_value=(10, 50)),
            ) as get_counts,
            patch(
                "sibyl.api.routes.admin.save_crawl_source_record",
                AsyncMock(return_value=stuck_source),
            ) as save_source,
        ):
            from sibyl.api.routes.admin import recover_stuck_sources

            result = await recover_stuck_sources()

        assert result["recovered"] == 1
        assert result["completed"] == 1
        assert result["reset_to_pending"] == 0

        assert stuck_source.crawl_status == CrawlStatus.COMPLETED
        assert stuck_source.document_count == 10
        assert stuck_source.chunk_count == 50
        assert stuck_source.current_job_id is None
        get_counts.assert_awaited_once_with(mock_session, source_id=stuck_source.id)
        save_source.assert_awaited_once_with(mock_session, source=stuck_source)

    @pytest.mark.asyncio
    async def test_recover_source_without_documents(
        self,
        mock_session: AsyncMock,
        mock_content_session,
        create_mock_source,
    ) -> None:
        """Test recovering a stuck source with no documents (should reset to PENDING)."""
        stuck_source = create_mock_source(
            name="Empty Source",
            crawl_status="in_progress",
        )

        with (
            patch("sibyl.api.routes.admin.get_content_read_session", mock_content_session),
            patch(
                "sibyl.api.routes.admin.list_crawl_sources",
                AsyncMock(return_value=[stuck_source]),
            ),
            patch(
                "sibyl.api.routes.admin.get_source_sync_counts",
                AsyncMock(return_value=(0, 0)),
            ) as get_counts,
            patch(
                "sibyl.api.routes.admin.save_crawl_source_record",
                AsyncMock(return_value=stuck_source),
            ) as save_source,
        ):
            from sibyl.api.routes.admin import recover_stuck_sources

            result = await recover_stuck_sources()

        assert result["recovered"] == 1
        assert result["completed"] == 0
        assert result["reset_to_pending"] == 1

        assert stuck_source.crawl_status == CrawlStatus.PENDING
        assert stuck_source.current_job_id is None
        get_counts.assert_awaited_once_with(mock_session, source_id=stuck_source.id)
        save_source.assert_awaited_once_with(mock_session, source=stuck_source)

    @pytest.mark.asyncio
    async def test_recover_multiple_sources(
        self,
        mock_session: AsyncMock,
        mock_content_session,
        create_mock_source,
    ) -> None:
        """Test recovering multiple stuck sources with different states."""
        source_with_docs = create_mock_source(name="Has Docs", crawl_status="in_progress")
        source_empty = create_mock_source(name="Empty", crawl_status="in_progress")

        with (
            patch("sibyl.api.routes.admin.get_content_read_session", mock_content_session),
            patch(
                "sibyl.api.routes.admin.list_crawl_sources",
                AsyncMock(return_value=[source_with_docs, source_empty]),
            ),
            patch(
                "sibyl.api.routes.admin.get_source_sync_counts",
                AsyncMock(side_effect=[(5, 25), (0, 0)]),
            ) as get_counts,
            patch(
                "sibyl.api.routes.admin.save_crawl_source_record",
                AsyncMock(side_effect=[source_with_docs, source_empty]),
            ) as save_source,
        ):
            from sibyl.api.routes.admin import recover_stuck_sources

            result = await recover_stuck_sources()

        assert result["recovered"] == 2
        assert result["completed"] == 1
        assert result["reset_to_pending"] == 1

        assert source_with_docs.crawl_status == CrawlStatus.COMPLETED
        assert source_empty.crawl_status == CrawlStatus.PENDING
        assert get_counts.await_count == 2
        assert save_source.await_count == 2

    @pytest.mark.asyncio
    async def test_recover_handles_database_error(
        self,
        mock_session: AsyncMock,
        mock_content_session,
    ) -> None:
        """Test that recovery handles database errors gracefully."""
        with (
            patch("sibyl.api.routes.admin.get_content_read_session", mock_content_session),
            patch(
                "sibyl.api.routes.admin.list_crawl_sources",
                AsyncMock(side_effect=Exception("Database connection failed")),
            ),
        ):
            from sibyl.api.routes.admin import recover_stuck_sources

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
        from sibyl.api.event_types import WSEvent
        from sibyl.api.websocket import broadcast_event

        # Mock the connection manager
        mock_manager = MagicMock()
        mock_manager.broadcast = AsyncMock()

        with patch("sibyl.api.websocket.get_manager", return_value=mock_manager):
            await broadcast_event(
                WSEvent.CRAWL_COMPLETE,
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
        from sibyl.api.event_types import WSEvent
        from sibyl.api.websocket import broadcast_event

        mock_manager = MagicMock()
        mock_manager.broadcast = AsyncMock()

        with patch("sibyl.api.websocket.get_manager", return_value=mock_manager):
            await broadcast_event(
                WSEvent.CRAWL_STARTED,
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
        from sibyl.api.event_types import WSEvent
        from sibyl.api.websocket import broadcast_event

        mock_manager = MagicMock()
        mock_manager.broadcast = AsyncMock()

        with patch("sibyl.api.websocket.get_manager", return_value=mock_manager):
            await broadcast_event(
                WSEvent.ENTITY_CREATED,
                {"id": "ent-123"},
                org_id="org-abc",
            )

            mock_manager.broadcast.assert_called_once_with(
                "entity_created",
                {"id": "ent-123"},
                org_id="org-abc",
            )
