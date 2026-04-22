from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

import sibyl.jobs.crawl as crawl_jobs
from sibyl.api.event_types import WSEvent
from sibyl.db import CrawlStatus


def _make_source() -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(),
        name="Docs",
        organization_id=uuid4(),
        crawl_status=CrawlStatus.PENDING,
        current_job_id="job-123",
        last_error=None,
        last_crawled_at=None,
        document_count=0,
        chunk_count=0,
    )


@pytest.mark.asyncio
async def test_crawl_source_uses_content_runtime_helpers() -> None:
    initial = _make_source()
    progress_source = _make_source()
    progress_source.id = initial.id
    progress_source.organization_id = initial.organization_id
    complete_source = _make_source()
    complete_source.id = initial.id
    complete_source.organization_id = initial.organization_id

    @asynccontextmanager
    async def mock_session():
        yield None

    class FakePipeline:
        def __init__(self, organization_id: str, *, generate_embeddings: bool = True) -> None:
            self.organization_id = organization_id
            self.generate_embeddings = generate_embeddings

        async def __aenter__(self) -> FakePipeline:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def ingest_source(self, source, *, max_pages, max_depth, on_progress):
            assert source.id == initial.id
            assert max_pages == 25
            assert max_depth == 4
            await on_progress(
                SimpleNamespace(
                    documents_crawled=1,
                    documents_stored=1,
                    chunks_created=2,
                    errors=0,
                ),
                2,
            )
            return SimpleNamespace(
                documents_crawled=2,
                documents_stored=2,
                chunks_created=4,
                embeddings_generated=4,
                errors=0,
                duration_seconds=1.5,
            )

    with (
        patch("sibyl.jobs.crawl.get_content_read_session", mock_session),
        patch(
            "sibyl.jobs.crawl.get_crawl_source_by_id",
            AsyncMock(side_effect=[initial, progress_source, complete_source]),
        ) as get_source_by_id,
        patch(
            "sibyl.jobs.crawl.save_crawl_source_record",
            AsyncMock(side_effect=lambda _session, *, source: source),
        ) as save_source,
        patch("sibyl.jobs.crawl._safe_broadcast", AsyncMock()) as broadcast,
        patch("sibyl.crawler.IngestionPipeline", FakePipeline),
    ):
        result = await crawl_jobs.crawl_source(
            {},
            str(initial.id),
            max_pages=25,
            max_depth=4,
        )

    assert result == {
        "source_id": str(initial.id),
        "source_name": "Docs",
        "documents_crawled": 2,
        "documents_stored": 2,
        "chunks_created": 4,
        "embeddings_generated": 4,
        "errors": 0,
        "duration_seconds": 1.5,
    }
    assert get_source_by_id.await_count == 3
    saved_states = [call.kwargs["source"] for call in save_source.await_args_list]
    assert saved_states[0].crawl_status == CrawlStatus.IN_PROGRESS
    assert saved_states[1].document_count == 1
    assert saved_states[1].chunk_count == 2
    assert saved_states[2].crawl_status == CrawlStatus.COMPLETED
    assert saved_states[2].document_count == 2
    assert saved_states[2].chunk_count == 4
    assert [call.args[0] for call in broadcast.await_args_list] == [
        WSEvent.CRAWL_STARTED,
        WSEvent.CRAWL_PROGRESS,
        WSEvent.CRAWL_COMPLETE,
    ]


@pytest.mark.asyncio
async def test_sync_source_uses_runtime_counts() -> None:
    source = _make_source()
    source.crawl_status = CrawlStatus.IN_PROGRESS
    source.document_count = 0
    source.chunk_count = 0
    now = datetime(2026, 4, 21, 18, 5, tzinfo=UTC)

    @asynccontextmanager
    async def mock_session():
        yield None

    with (
        patch("sibyl.jobs.crawl.get_content_read_session", mock_session),
        patch(
            "sibyl.jobs.crawl.get_crawl_source_by_id",
            AsyncMock(return_value=source),
        ) as get_source_by_id,
        patch(
            "sibyl.jobs.crawl.get_source_sync_counts",
            AsyncMock(return_value=(3, 7)),
        ) as get_counts,
        patch(
            "sibyl.jobs.crawl.save_crawl_source_record",
            AsyncMock(side_effect=lambda _session, *, source: source),
        ) as save_source,
        patch("sibyl.jobs.crawl._safe_broadcast", AsyncMock()) as broadcast,
        patch("sibyl.jobs.crawl.utcnow_naive", return_value=now),
    ):
        result = await crawl_jobs.sync_source({}, str(source.id))

    get_source_by_id.assert_awaited_once_with(None, source_id=source.id)
    get_counts.assert_awaited_once_with(None, source_id=source.id)
    save_source.assert_awaited_once()
    saved = save_source.await_args.kwargs["source"]
    assert saved.crawl_status == CrawlStatus.COMPLETED
    assert saved.current_job_id is None
    assert saved.last_crawled_at == now
    assert saved.document_count == 3
    assert saved.chunk_count == 7
    broadcast.assert_awaited_once_with(
        WSEvent.CRAWL_SYNC_COMPLETE,
        result,
        org_id=str(source.organization_id),
    )
    assert result["status"] == "completed"
    assert result["changes"] == {
        "status": "in_progress -> completed",
        "document_count": "0 -> 3",
        "chunk_count": "0 -> 7",
    }
