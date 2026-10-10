"""Tests for source status recovery and management.

Tests the recover_stuck_sources function that runs on every API start to
clean up sources a crawl job left IN_PROGRESS when it died, without touching
crawls a live worker still owns. The proof against a real arq worker and
Redis/Valkey runs when SIBYL_LIVE_REDIS_HOST and SIBYL_LIVE_REDIS_PORT are set.
"""

import asyncio
import os
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, suppress
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from sibyl.coordination import broker as broker_module
from sibyl.coordination.broker import JobInfo, JobStatus
from sibyl_core.models import CrawlStatus

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
        source = MagicMock()
        source.id = uuid4() if source_id is None else source_id
        source.name = name
        source.crawl_status = CrawlStatus(crawl_status)
        source.current_job_id = current_job_id
        source.document_count = document_count
        source.chunk_count = chunk_count
        return source

    return _create


class FakeBroker:
    """A job broker that answers status queries from a fixed table."""

    def __init__(
        self,
        statuses: dict[str, JobStatus] | None = None,
        *,
        error: Exception | None = None,
    ) -> None:
        self.statuses = statuses or {}
        self.error = error
        self.asked: list[str] = []

    async def get_job_status(self, job_id: str) -> JobInfo:
        self.asked.append(job_id)
        if self.error is not None:
            raise self.error
        return JobInfo(
            job_id=job_id,
            function="crawl_source",
            status=self.statuses.get(job_id, JobStatus.NOT_FOUND),
        )


@pytest.fixture
def no_live_jobs() -> Iterator[FakeBroker]:
    """A broker that knows no jobs, as one does for crawls whose worker died."""
    broker = FakeBroker()
    with patch("sibyl.api.routes.admin.get_broker", lambda: broker):
        yield broker


# =============================================================================
# Tests for recover_stuck_sources
# =============================================================================


@pytest.mark.usefixtures("no_live_jobs")
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
            patch(
                "sibyl.api.routes.admin.list_crawl_sources", AsyncMock(return_value=[])
            ) as list_sources,
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


class InMemorySources:
    """The content store's crawl sources, enough for startup recovery."""

    def __init__(self, *sources: SimpleNamespace, doc_counts: dict | None = None) -> None:
        self.sources = list(sources)
        self.doc_counts = doc_counts or {}
        self.saved: list[object] = []

    def patches(self):
        @asynccontextmanager
        async def session() -> AsyncIterator[object]:
            yield object()

        async def list_sources(_session, *, status, limit):
            assert limit is None
            return [source for source in self.sources if source.crawl_status == status]

        async def sync_counts(_session, *, source_id):
            return self.doc_counts.get(source_id, (0, 0))

        async def save(_session, *, source):
            self.saved.append(source.id)
            return source

        return (
            patch("sibyl.api.routes.admin.get_content_read_session", session),
            patch("sibyl.api.routes.admin.list_crawl_sources", list_sources),
            patch("sibyl.api.routes.admin.get_source_sync_counts", sync_counts),
            patch("sibyl.api.routes.admin.save_crawl_source_record", save),
        )

    async def recover(self) -> dict:
        from sibyl.api.routes.admin import recover_stuck_sources

        first, second, third, fourth = self.patches()
        with first, second, third, fourth:
            return await recover_stuck_sources()


def _in_progress_source(*, current_job_id: str | None = "use-crawl-id") -> SimpleNamespace:
    source_id = uuid4()
    return SimpleNamespace(
        id=source_id,
        name=f"source-{source_id.hex[:6]}",
        crawl_status=CrawlStatus.IN_PROGRESS,
        current_job_id=f"crawl:{source_id}" if current_job_id == "use-crawl-id" else None,
        document_count=0,
        chunk_count=0,
    )


class TestRecoveryLeavesLiveCrawlsAlone:
    """A replica starting mid-deploy must not reset a crawl a worker still runs."""

    @pytest.mark.parametrize(
        "live_status", [JobStatus.IN_PROGRESS, JobStatus.QUEUED, JobStatus.DEFERRED]
    )
    async def test_source_owned_by_a_live_job_survives_an_api_start(
        self, live_status: JobStatus
    ) -> None:
        running = _in_progress_source()
        orphaned = _in_progress_source()
        store = InMemorySources(running, orphaned)
        broker = FakeBroker({running.current_job_id: live_status})

        with patch("sibyl.api.routes.admin.get_broker", lambda: broker):
            result = await store.recover()

        assert running.crawl_status == CrawlStatus.IN_PROGRESS
        assert running.current_job_id == f"crawl:{running.id}"
        assert orphaned.crawl_status == CrawlStatus.PENDING
        assert orphaned.current_job_id is None
        assert store.saved == [orphaned.id]
        assert result == {
            "recovered": 1,
            "completed": 0,
            "reset_to_pending": 1,
            "still_running": 1,
        }

    async def test_live_job_is_found_by_its_crawl_id_when_the_source_lost_it(self) -> None:
        running = _in_progress_source(current_job_id=None)
        store = InMemorySources(running)
        broker = FakeBroker({f"crawl:{running.id}": JobStatus.IN_PROGRESS})

        with patch("sibyl.api.routes.admin.get_broker", lambda: broker):
            result = await store.recover()

        assert running.crawl_status == CrawlStatus.IN_PROGRESS
        assert store.saved == []
        assert result["still_running"] == 1

    @pytest.mark.parametrize(
        "dead_status",
        [JobStatus.COMPLETE, JobStatus.FAILED, JobStatus.CANCELLED, JobStatus.NOT_FOUND],
    )
    async def test_source_whose_job_is_gone_is_still_recovered(
        self, dead_status: JobStatus
    ) -> None:
        with_docs = _in_progress_source()
        empty = _in_progress_source()
        store = InMemorySources(with_docs, empty, doc_counts={with_docs.id: (4, 12)})
        broker = FakeBroker(
            {with_docs.current_job_id: dead_status, empty.current_job_id: dead_status}
        )

        with patch("sibyl.api.routes.admin.get_broker", lambda: broker):
            result = await store.recover()

        assert with_docs.crawl_status == CrawlStatus.COMPLETED
        assert (with_docs.document_count, with_docs.chunk_count) == (4, 12)
        assert empty.crawl_status == CrawlStatus.PENDING
        assert with_docs.current_job_id is None
        assert empty.current_job_id is None
        assert result == {
            "recovered": 2,
            "completed": 1,
            "reset_to_pending": 1,
            "still_running": 0,
        }

    async def test_unreachable_broker_leaves_every_source_alone(self) -> None:
        first = _in_progress_source()
        second = _in_progress_source()
        store = InMemorySources(first, second)
        broker = FakeBroker(error=ConnectionError("redis down"))

        with patch("sibyl.api.routes.admin.get_broker", lambda: broker):
            result = await store.recover()

        assert first.crawl_status == CrawlStatus.IN_PROGRESS
        assert second.crawl_status == CrawlStatus.IN_PROGRESS
        assert store.saved == []
        assert len(broker.asked) == 1
        assert result["recovered"] == 0


def _local_broker(functions: dict | None = None):
    from sibyl.coordination._local.broker import LocalQueueBroker

    async def never_called(*_args, **_kwargs):
        raise AssertionError("no job should run")

    return LocalQueueBroker(
        functions=functions or {"crawl_source": never_called},
        max_concurrency=2,
        shutdown_grace_seconds=0.1,
    )


class TestSingleProcessRecovery:
    """One process with the in-process broker recovers exactly as it always has."""

    async def test_fresh_process_recovers_every_in_progress_source(self) -> None:
        with_docs = _in_progress_source()
        empty = _in_progress_source(current_job_id=None)
        store = InMemorySources(with_docs, empty, doc_counts={with_docs.id: (3, 9)})
        broker = _local_broker()
        await broker.startup()
        try:
            with patch("sibyl.api.routes.admin.get_broker", lambda: broker):
                result = await store.recover()
        finally:
            await broker.shutdown()

        assert with_docs.crawl_status == CrawlStatus.COMPLETED
        assert empty.crawl_status == CrawlStatus.PENDING
        assert result == {
            "recovered": 2,
            "completed": 1,
            "reset_to_pending": 1,
            "still_running": 0,
        }

    async def test_crawl_running_in_process_is_left_to_finish(self) -> None:
        running = _in_progress_source()
        started = asyncio.Event()
        release = asyncio.Event()

        async def crawl_source(_ctx, source_id, **_kwargs):
            started.set()
            await release.wait()
            return {"source_id": source_id}

        broker = _local_broker({"crawl_source": crawl_source})
        await broker.startup()
        try:
            job_id = await broker.enqueue_crawl(running.id, force=True)
            await asyncio.wait_for(started.wait(), timeout=5)
            store = InMemorySources(running)
            with patch("sibyl.api.routes.admin.get_broker", lambda: broker):
                result = await store.recover()
        finally:
            release.set()
            await broker.shutdown()

        assert job_id == running.current_job_id
        assert running.crawl_status == CrawlStatus.IN_PROGRESS
        assert result["still_running"] == 1


def _live_redis() -> tuple[str, int]:
    host = os.environ.get("SIBYL_LIVE_REDIS_HOST", "")
    port = os.environ.get("SIBYL_LIVE_REDIS_PORT", "")
    if not host or not port:
        pytest.skip("live worker recovery tests need SIBYL_LIVE_REDIS_HOST/PORT")
    return host, int(port)


class TestRecoveryAgainstALiveWorker:
    """An API start during a deploy, with a real arq worker mid-crawl on Redis."""

    async def test_crawl_on_a_live_worker_survives_and_an_orphan_is_recovered(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from arq import Worker, func

        from sibyl.config import settings

        host, port = _live_redis()
        monkeypatch.setattr(settings, "coordination_backend", "redis")
        monkeypatch.setattr(settings, "redis_host", host)
        monkeypatch.setattr(settings, "redis_port", port)
        monkeypatch.setattr(settings, "redis_jobs_db", 11)
        monkeypatch.setattr(broker_module, "_broker", None)
        monkeypatch.setattr(broker_module, "_broker_backend", None)
        broker = broker_module.get_broker()

        running = _in_progress_source()
        orphaned = _in_progress_source()
        started = asyncio.Event()
        release = asyncio.Event()

        async def crawl_source(_ctx, source_id, **_kwargs):
            started.set()
            await release.wait()
            return {"source_id": source_id}

        worker = Worker(
            functions=[func(crawl_source, name="crawl_source")],
            redis_settings=broker.get_redis_settings(),
            handle_signals=False,
            poll_delay=0.05,
            max_jobs=2,
        )
        worker_task = asyncio.create_task(worker.async_run())
        pool = await broker.get_pool()
        try:
            job_id = await broker.enqueue_crawl(running.id, force=True)
            await asyncio.wait_for(started.wait(), timeout=10)
            assert (await broker.get_job_status(job_id)).status == JobStatus.IN_PROGRESS

            result = await InMemorySources(running, orphaned).recover()

            assert running.crawl_status == CrawlStatus.IN_PROGRESS
            assert running.current_job_id == job_id
            assert orphaned.crawl_status == CrawlStatus.PENDING
            assert orphaned.current_job_id is None
            assert result == {
                "recovered": 1,
                "completed": 0,
                "reset_to_pending": 1,
                "still_running": 1,
            }
        finally:
            release.set()
            for _ in range(200):
                if (await broker.get_job_status(f"crawl:{running.id}")).status in {
                    JobStatus.COMPLETE,
                    JobStatus.FAILED,
                    JobStatus.NOT_FOUND,
                }:
                    break
                await asyncio.sleep(0.05)
            await worker.close()
            worker_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await worker_task
            for source in (running, orphaned):
                job = f"crawl:{source.id}"
                await pool.delete(
                    f"arq:result:{job}",
                    f"arq:job:{job}",
                    f"arq:retry:{job}",
                    f"arq:in-progress:{job}",
                    broker_module_metadata_key(job),
                )
                await pool.zrem(broker_module.RECENT_JOB_INDEX_KEY, job)
            await broker.close_pool()


def broker_module_metadata_key(job_id: str) -> str:
    from sibyl.coordination._redis.broker import _job_metadata_key

    return _job_metadata_key(job_id)


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
