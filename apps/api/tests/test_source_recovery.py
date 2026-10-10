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
from copy import copy
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

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
    with patch("sibyl.coordination.broker.get_broker", lambda: broker):
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
            patch("sibyl.api.routes.admin.reset_stuck_crawl_source", AsyncMock()) as reset_source,
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
        reset_source.assert_not_awaited()

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
                "sibyl.api.routes.admin.reset_stuck_crawl_source",
                AsyncMock(return_value=stuck_source),
            ) as reset_source,
        ):
            from sibyl.api.routes.admin import recover_stuck_sources

            result = await recover_stuck_sources()

        assert result["recovered"] == 1
        assert result["completed"] == 1
        assert result["reset_to_pending"] == 0

        get_counts.assert_awaited_once_with(mock_session, source_id=stuck_source.id)
        reset_source.assert_awaited_once_with(
            mock_session,
            source_id=stuck_source.id,
            expected_job_id="job-123",
            crawl_status=CrawlStatus.COMPLETED,
            document_count=10,
            chunk_count=50,
        )

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
                "sibyl.api.routes.admin.reset_stuck_crawl_source",
                AsyncMock(return_value=stuck_source),
            ) as reset_source,
        ):
            from sibyl.api.routes.admin import recover_stuck_sources

            result = await recover_stuck_sources()

        assert result["recovered"] == 1
        assert result["completed"] == 0
        assert result["reset_to_pending"] == 1

        get_counts.assert_awaited_once_with(mock_session, source_id=stuck_source.id)
        reset_source.assert_awaited_once_with(
            mock_session,
            source_id=stuck_source.id,
            expected_job_id="job-123",
            crawl_status=CrawlStatus.PENDING,
            document_count=0,
            chunk_count=0,
        )

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
                "sibyl.api.routes.admin.reset_stuck_crawl_source",
                AsyncMock(side_effect=[source_with_docs, source_empty]),
            ) as reset_source,
        ):
            from sibyl.api.routes.admin import recover_stuck_sources

            result = await recover_stuck_sources()

        assert result["recovered"] == 2
        assert result["completed"] == 1
        assert result["reset_to_pending"] == 1

        statuses = [call.kwargs["crawl_status"] for call in reset_source.await_args_list]
        assert statuses == [CrawlStatus.COMPLETED, CrawlStatus.PENDING]
        assert get_counts.await_count == 2

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
    """The content store's crawl sources, with the conditional writes it offers.

    The sources passed in are the stored rows. Readers get copies, as a real
    read returns a snapshot, so a test can change a row between a caller's
    read and its write the way a crawl on another process would.
    """

    def __init__(self, *sources: SimpleNamespace, doc_counts: dict | None = None) -> None:
        self.rows = {source.id: source for source in sources}
        self.doc_counts = doc_counts or {}
        self.saved: list[object] = []
        self.on_read: list[object] = []

    def read(self, source_id: object) -> SimpleNamespace:
        return copy(self.rows[source_id])

    async def reset_stuck(
        self,
        _session,
        *,
        source_id,
        expected_job_id,
        crawl_status,
        document_count,
        chunk_count,
        crawled_at=None,
    ):
        row = self.rows.get(source_id)
        if (
            row is None
            or row.crawl_status != CrawlStatus.IN_PROGRESS
            or (row.current_job_id or "") != (expected_job_id or "")
        ):
            return None
        row.crawl_status = crawl_status
        row.current_job_id = None
        row.document_count = document_count
        row.chunk_count = chunk_count
        if getattr(row, "last_crawled_at", None) is None and crawled_at is not None:
            row.last_crawled_at = crawled_at
        self.saved.append(source_id)
        return copy(row)

    async def update_counts(self, _session, *, source_id, document_count, chunk_count):
        row = self.rows.get(source_id)
        if row is None:
            return None
        row.document_count = document_count
        row.chunk_count = chunk_count
        self.saved.append(source_id)
        return copy(row)

    def patches(self):
        @asynccontextmanager
        async def session() -> AsyncIterator[object]:
            yield object()

        async def list_sources(_session, *, status, limit):
            assert limit is None
            return [copy(row) for row in self.rows.values() if row.crawl_status == status]

        async def sync_counts(_session, *, source_id):
            return self.doc_counts.get(source_id, (0, 0))

        return (
            patch("sibyl.api.routes.admin.get_content_read_session", session),
            patch("sibyl.api.routes.admin.list_crawl_sources", list_sources),
            patch("sibyl.api.routes.admin.get_source_sync_counts", sync_counts),
            patch("sibyl.api.routes.admin.reset_stuck_crawl_source", self.reset_stuck),
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

        with patch("sibyl.coordination.broker.get_broker", lambda: broker):
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

        with patch("sibyl.coordination.broker.get_broker", lambda: broker):
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

        with patch("sibyl.coordination.broker.get_broker", lambda: broker):
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

        with patch("sibyl.coordination.broker.get_broker", lambda: broker):
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
            with patch("sibyl.coordination.broker.get_broker", lambda: broker):
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
            with patch("sibyl.coordination.broker.get_broker", lambda: broker):
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


@dataclass
class LiveCrawlWorker:
    """A real arq worker on Redis/Valkey that holds each crawl until released."""

    broker: object
    started: asyncio.Event
    release: asyncio.Event

    async def start_crawl(self, source: SimpleNamespace) -> str:
        job_id = await self.broker.enqueue_crawl(source.id, force=True)
        await asyncio.wait_for(self.started.wait(), timeout=10)
        assert (await self.broker.get_job_status(job_id)).status == JobStatus.IN_PROGRESS
        return job_id


@pytest.fixture
async def live_crawl_worker(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[LiveCrawlWorker]:
    """Coordination on a live Redis, with a separate arq worker mid-crawl."""
    from arq import Worker, func

    from sibyl.config import settings
    from sibyl.coordination._redis.broker import _job_metadata_key

    host, port = _live_redis()
    # Patch the field values directly: assigning through the model would also
    # mark the Redis fields as set, and ``auto`` coordination would resolve
    # to redis for every test after this one.
    for field, value in {
        "coordination_backend": "redis",
        "redis_host": host,
        "redis_port": port,
        "redis_jobs_db": 11,
    }.items():
        monkeypatch.setitem(settings.__dict__, field, value)
    monkeypatch.setattr(broker_module, "_broker", None)
    monkeypatch.setattr(broker_module, "_broker_backend", None)
    broker = broker_module.get_broker()
    started = asyncio.Event()
    release = asyncio.Event()
    crawled: list[str] = []

    async def crawl_source(_ctx, source_id, **_kwargs):
        crawled.append(source_id)
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
        yield LiveCrawlWorker(broker=broker, started=started, release=release)
    finally:
        release.set()
        for source_id in crawled:
            for _ in range(200):
                status = (await broker.get_job_status(f"crawl:{source_id}")).status
                if status in {JobStatus.COMPLETE, JobStatus.FAILED, JobStatus.NOT_FOUND}:
                    break
                await asyncio.sleep(0.05)
        await worker.close()
        worker_task.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await worker_task
        for source_id in crawled:
            job = f"crawl:{source_id}"
            await pool.delete(
                f"arq:result:{job}",
                f"arq:job:{job}",
                f"arq:retry:{job}",
                f"arq:in-progress:{job}",
                _job_metadata_key(job),
            )
            await pool.zrem(broker_module.RECENT_JOB_INDEX_KEY, job)
        await broker.close_pool()


class TestRecoveryAgainstALiveWorker:
    """An API start during a deploy, with a real arq worker mid-crawl on Redis."""

    async def test_crawl_on_a_live_worker_survives_and_an_orphan_is_recovered(
        self, live_crawl_worker: LiveCrawlWorker
    ) -> None:
        running = _in_progress_source()
        orphaned = _in_progress_source()
        job_id = await live_crawl_worker.start_crawl(running)

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


# =============================================================================
# Manual sync of a source a crawl still owns
# =============================================================================


def _org_source(source: SimpleNamespace) -> SimpleNamespace:
    source.organization_id = uuid4()
    source.last_crawled_at = None
    return source


async def _sync_via_route(
    source: SimpleNamespace,
    *,
    doc_counts: tuple[int, int] = (0, 0),
    store: InMemorySources | None = None,
) -> tuple[dict | None, int | None, list]:
    """POST /sources/{id}/sync against an in-memory source; returns (body, error, saves)."""
    from fastapi import HTTPException

    from sibyl.api.routes.crawler import sync_source

    store = store or InMemorySources(source)

    @asynccontextmanager
    async def session() -> AsyncIterator[object]:
        yield object()

    async def read_source(_session, source_id, _org):
        return store.read(UUID(source_id))

    with (
        patch("sibyl.api.routes.crawler.get_content_read_session", session),
        patch("sibyl.api.routes.crawler._get_org_source", read_source),
        patch(
            "sibyl.api.routes.crawler.get_source_sync_counts", AsyncMock(return_value=doc_counts)
        ),
        patch("sibyl.api.routes.crawler.reset_stuck_crawl_source", store.reset_stuck),
        patch("sibyl.api.routes.crawler.update_crawl_source_counts", store.update_counts),
        patch("sibyl.api.routes.crawler.broadcast_event", AsyncMock()),
    ):
        try:
            body = await sync_source(str(source.id), org=SimpleNamespace(id=source.organization_id))
        except HTTPException as exc:
            return None, exc.status_code, store.saved
    return body, None, store.saved


async def _sync_via_job(
    source: SimpleNamespace,
    *,
    doc_counts: tuple[int, int] = (0, 0),
    store: InMemorySources | None = None,
) -> tuple[dict, list]:
    """The sync job an MCP sync or refresh enqueues, against an in-memory source."""
    import sibyl.jobs.crawl as crawl_jobs

    store = store or InMemorySources(source)

    @asynccontextmanager
    async def session() -> AsyncIterator[object]:
        yield object()

    async def read_source(_session, *, source_id):
        return store.read(source_id)

    with (
        patch("sibyl.jobs.crawl.get_content_read_session", session),
        patch("sibyl.jobs.crawl.get_crawl_source_by_id", read_source),
        patch("sibyl.jobs.crawl.get_source_sync_counts", AsyncMock(return_value=doc_counts)),
        patch("sibyl.jobs.crawl.reset_stuck_crawl_source", store.reset_stuck),
        patch("sibyl.jobs.crawl.update_crawl_source_counts", store.update_counts),
        patch("sibyl.jobs.crawl._safe_broadcast", AsyncMock()),
    ):
        result = await crawl_jobs.sync_source({}, str(source.id))
    return result, store.saved


class CrawlStartsMidway(FakeBroker):
    """A broker that reports no live crawl, and a crawl that starts just after.

    Between the caller's read of the source and its write, another process
    claims the source for a new crawl (or a crawl finishes), which is the
    window a stale whole-record save used to overwrite.
    """

    def __init__(self, store: InMemorySources, change) -> None:
        super().__init__()
        self.store = store
        self.change = change

    async def get_job_status(self, job_id: str) -> JobInfo:
        info = await super().get_job_status(job_id)
        for row in self.store.rows.values():
            self.change(row)
        return info


def _new_crawl_claims(row: SimpleNamespace) -> None:
    row.crawl_status = CrawlStatus.IN_PROGRESS
    row.current_job_id = f"crawl:{row.id}:next"


def _crawl_finishes(row: SimpleNamespace) -> None:
    row.crawl_status = CrawlStatus.FAILED
    row.current_job_id = None
    row.last_error = "fetch failed"


class TestWritesLeaveAConcurrentCrawlsState:
    """Recovery and sync change a source only if no crawl claimed it after they read it."""

    @pytest.mark.parametrize("change", [_new_crawl_claims, _crawl_finishes])
    async def test_recovery_keeps_what_a_crawl_wrote_meanwhile(self, change) -> None:
        source = _in_progress_source()
        store = InMemorySources(source)
        broker = CrawlStartsMidway(store, change)
        after_change = copy(source)
        change(after_change)

        with patch("sibyl.coordination.broker.get_broker", lambda: broker):
            result = await store.recover()

        assert vars(source) == vars(after_change)
        assert store.saved == []
        assert result["recovered"] == 0

    @pytest.mark.parametrize("change", [_new_crawl_claims, _crawl_finishes])
    async def test_route_sync_keeps_what_a_crawl_wrote_meanwhile(self, change) -> None:
        source = _org_source(_in_progress_source())
        store = InMemorySources(source)
        broker = CrawlStartsMidway(store, change)
        after_change = copy(source)
        change(after_change)

        with patch("sibyl.coordination.broker.get_broker", lambda: broker):
            body, status_code, saved = await _sync_via_route(source, store=store)

        assert (body, status_code, saved) == (None, 409, [])
        assert vars(source) == vars(after_change)

    @pytest.mark.parametrize("change", [_new_crawl_claims, _crawl_finishes])
    async def test_sync_job_keeps_what_a_crawl_wrote_meanwhile(self, change) -> None:
        source = _org_source(_in_progress_source())
        store = InMemorySources(source)
        broker = CrawlStartsMidway(store, change)
        after_change = copy(source)
        change(after_change)

        with patch("sibyl.coordination.broker.get_broker", lambda: broker):
            result, saved = await _sync_via_job(source, store=store)

        assert result["skipped"] == "crawl_running"
        assert saved == []
        assert vars(source) == vars(after_change)

    async def test_route_sync_of_a_settled_source_changes_only_its_counts(self) -> None:
        source = _org_source(_in_progress_source())
        source.crawl_status = CrawlStatus.COMPLETED
        source.current_job_id = None
        store = InMemorySources(source)
        broker = CrawlStartsMidway(store, _new_crawl_claims)

        with patch("sibyl.coordination.broker.get_broker", lambda: broker):
            body, status_code, _saved = await _sync_via_route(
                source, doc_counts=(3, 9), store=store
            )

        assert status_code is None
        assert body is not None
        # The crawl that started meanwhile keeps its status and job id.
        assert source.crawl_status == CrawlStatus.IN_PROGRESS
        assert source.current_job_id == f"crawl:{source.id}:next"
        assert (source.document_count, source.chunk_count) == (3, 9)


class TestManualSyncLeavesLiveCrawlsAlone:
    """A manual sync is refused, or skipped, while a job still owns the crawl."""

    @pytest.mark.parametrize(
        "live_status", [JobStatus.IN_PROGRESS, JobStatus.QUEUED, JobStatus.DEFERRED]
    )
    async def test_route_answers_409_and_leaves_the_source(self, live_status: JobStatus) -> None:
        source = _org_source(_in_progress_source())
        broker = FakeBroker({source.current_job_id: live_status})

        with patch("sibyl.coordination.broker.get_broker", lambda: broker):
            body, status_code, saved = await _sync_via_route(source, doc_counts=(2, 6))

        assert status_code == 409
        assert body is None
        assert saved == []
        assert source.crawl_status == CrawlStatus.IN_PROGRESS
        assert source.current_job_id == f"crawl:{source.id}"

    async def test_route_answers_503_when_the_broker_cannot_say(self) -> None:
        source = _org_source(_in_progress_source())
        broker = FakeBroker(error=ConnectionError("redis down"))

        with patch("sibyl.coordination.broker.get_broker", lambda: broker):
            _body, status_code, saved = await _sync_via_route(source)

        assert status_code == 503
        assert saved == []
        assert source.crawl_status == CrawlStatus.IN_PROGRESS

    async def test_route_still_fixes_a_source_whose_crawl_is_gone(self) -> None:
        source = _org_source(_in_progress_source())

        with patch("sibyl.coordination.broker.get_broker", FakeBroker):
            body, status_code, saved = await _sync_via_route(source, doc_counts=(2, 6))

        assert status_code is None
        assert body is not None
        assert body["status"] == "completed"
        assert saved == [source.id]
        assert (source.document_count, source.chunk_count) == (2, 6)

    async def test_sync_job_skips_a_crawl_that_is_still_owned(self) -> None:
        source = _org_source(_in_progress_source())
        broker = FakeBroker({source.current_job_id: JobStatus.IN_PROGRESS})

        with patch("sibyl.coordination.broker.get_broker", lambda: broker):
            result, saved = await _sync_via_job(source, doc_counts=(2, 6))

        assert result["skipped"] == "crawl_running"
        assert result["job_id"] == source.current_job_id
        assert saved == []
        assert source.crawl_status == CrawlStatus.IN_PROGRESS

    async def test_sync_job_still_fixes_a_source_whose_crawl_is_gone(self) -> None:
        source = _org_source(_in_progress_source())

        with patch("sibyl.coordination.broker.get_broker", FakeBroker):
            result, saved = await _sync_via_job(source)

        assert "skipped" not in result
        assert result["status"] == "pending"
        assert saved == [source.id]
        assert source.current_job_id is None


class TestManualSyncAgainstALiveWorker:
    """Manual syncs while a real arq worker on Redis holds the crawl."""

    async def test_route_refuses_while_a_live_worker_crawls(
        self, live_crawl_worker: LiveCrawlWorker
    ) -> None:
        source = _org_source(_in_progress_source())
        job_id = await live_crawl_worker.start_crawl(source)

        body, status_code, saved = await _sync_via_route(source)

        assert (body, status_code, saved) == (None, 409, [])
        assert source.crawl_status == CrawlStatus.IN_PROGRESS
        assert source.current_job_id == job_id

    async def test_sync_job_leaves_a_live_workers_crawl_alone(
        self, live_crawl_worker: LiveCrawlWorker
    ) -> None:
        source = _org_source(_in_progress_source())
        job_id = await live_crawl_worker.start_crawl(source)

        result, saved = await _sync_via_job(source)

        assert result["skipped"] == "crawl_running"
        assert result["job_id"] == job_id
        assert saved == []
        assert source.crawl_status == CrawlStatus.IN_PROGRESS


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
