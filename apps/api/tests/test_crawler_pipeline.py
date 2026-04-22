from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from sibyl.crawler.chunker import Chunk
from sibyl.crawler.pipeline import IngestionPipeline, IngestionStats, reingest_source
from sibyl.db import CrawledDocument, CrawlSource, SourceType


@pytest.mark.asyncio
async def test_process_document_uses_runtime_persistence_helpers() -> None:
    org_id = uuid4()
    source = SimpleNamespace(id=uuid4(), organization_id=org_id)
    document = CrawledDocument(
        source_id=source.id,
        url="https://docs.example.com/guide",
        title="Guide",
        raw_content="Guide",
        content="Guide",
        content_hash="hash",
    )
    stats = IngestionStats(source_id=source.id, source_name="Docs")
    pipeline = IngestionPipeline(str(org_id), generate_embeddings=False, integrate_with_graph=False)
    pipeline._chunker = MagicMock(
        chunk_document=MagicMock(return_value=[Chunk(content="Alpha", chunk_index=0)])
    )

    @asynccontextmanager
    async def mock_session():
        yield None

    async def passthrough_chunks(_session, *, chunks):
        return chunks

    with (
        patch("sibyl.crawler.pipeline.get_content_read_session", mock_session),
        patch(
            "sibyl.crawler.pipeline.get_crawl_source_by_id",
            AsyncMock(return_value=source),
        ) as get_source_by_id,
        patch(
            "sibyl.crawler.pipeline.get_document_by_url_for_org",
            AsyncMock(return_value=None),
        ) as get_document,
        patch(
            "sibyl.crawler.pipeline.save_crawled_document_record",
            AsyncMock(return_value=document),
        ) as save_document,
        patch(
            "sibyl.crawler.pipeline.save_document_chunks",
            AsyncMock(side_effect=passthrough_chunks),
        ) as save_chunks,
    ):
        await pipeline._process_document(document, stats)

    get_source_by_id.assert_awaited_once_with(None, source_id=source.id)
    get_document.assert_awaited_once_with(
        None,
        url=document.url,
        organization_id=org_id,
    )
    save_document.assert_awaited_once_with(None, document=document)
    save_chunks.assert_awaited_once()
    saved_chunks = save_chunks.await_args.kwargs["chunks"]
    assert len(saved_chunks) == 1
    assert saved_chunks[0].document_id == document.id
    assert stats.chunks_created == 1


@pytest.mark.asyncio
async def test_process_document_treats_runtime_duplicate_as_race() -> None:
    org_id = uuid4()
    source = SimpleNamespace(id=uuid4(), organization_id=org_id)
    session = AsyncMock()
    existing = CrawledDocument(
        source_id=source.id,
        url="https://docs.example.com/guide",
        title="Existing Guide",
        raw_content="Existing Guide",
        content="Existing Guide",
        content_hash="existing",
    )
    document = CrawledDocument(
        source_id=source.id,
        url=existing.url,
        title="Guide",
        raw_content="Guide",
        content="Guide",
        content_hash="hash",
    )
    stats = IngestionStats(source_id=source.id, source_name="Docs")
    pipeline = IngestionPipeline(str(org_id), generate_embeddings=False, integrate_with_graph=False)
    pipeline._chunker = MagicMock()

    @asynccontextmanager
    async def mock_session():
        yield session

    with (
        patch("sibyl.crawler.pipeline.get_content_read_session", mock_session),
        patch(
            "sibyl.crawler.pipeline.get_crawl_source_by_id",
            AsyncMock(return_value=source),
        ),
        patch(
            "sibyl.crawler.pipeline.get_document_by_url_for_org",
            AsyncMock(side_effect=[None, existing]),
        ) as get_document,
        patch(
            "sibyl.crawler.pipeline.save_crawled_document_record",
            AsyncMock(side_effect=RuntimeError("duplicate url")),
        ),
        patch(
            "sibyl.crawler.pipeline.save_document_chunks",
            AsyncMock(),
        ) as save_chunks,
    ):
        await pipeline._process_document(document, stats)

    session.rollback.assert_awaited_once()
    assert get_document.await_count == 2
    pipeline._chunker.chunk_document.assert_not_called()
    save_chunks.assert_not_awaited()
    assert stats.chunks_created == 0


@pytest.mark.asyncio
async def test_reingest_source_uses_runtime_lookup() -> None:
    source = CrawlSource(
        id=uuid4(),
        organization_id=uuid4(),
        name="Docs",
        url="https://docs.example.com",
        source_type=SourceType.WEBSITE,
    )
    stats = IngestionStats(source_id=source.id, source_name=source.name)
    pipeline = AsyncMock()
    pipeline.ingest_source = AsyncMock(return_value=stats)

    @asynccontextmanager
    async def mock_pipeline(*args, **kwargs):
        del args, kwargs
        yield pipeline

    @asynccontextmanager
    async def mock_session():
        yield None

    with (
        patch("sibyl.crawler.pipeline.get_content_read_session", mock_session),
        patch(
            "sibyl.crawler.pipeline.get_crawl_source_by_id",
            AsyncMock(return_value=source),
        ) as get_source_by_id,
        patch("sibyl.crawler.pipeline.IngestionPipeline", mock_pipeline),
    ):
        result = await reingest_source(source.id, str(source.organization_id))

    get_source_by_id.assert_awaited_once_with(None, source_id=source.id)
    pipeline.ingest_source.assert_awaited_once_with(source)
    assert result is stats
