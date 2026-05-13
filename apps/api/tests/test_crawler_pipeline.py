from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from sibyl.crawler.chunker import Chunk
from sibyl.crawler.pipeline import IngestionPipeline, IngestionStats, reingest_source
from sibyl.persistence.content_common import (
    ContentConflictError,
    CrawledDocumentRecord,
    CrawlSourceRecord,
)
from sibyl_core.models import SourceType


@pytest.mark.asyncio
async def test_process_document_uses_runtime_persistence_helpers() -> None:
    org_id = uuid4()
    source = SimpleNamespace(id=uuid4(), organization_id=org_id)
    document = CrawledDocumentRecord(
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
    existing = CrawledDocumentRecord(
        source_id=source.id,
        url="https://docs.example.com/guide",
        title="Existing Guide",
        raw_content="Existing Guide",
        content="Existing Guide",
        content_hash="existing",
    )
    document = CrawledDocumentRecord(
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
            AsyncMock(side_effect=ContentConflictError("duplicate url")),
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
async def test_start_uses_native_graph_integration_service() -> None:
    service = MagicMock()
    entity_manager = MagicMock()
    service.get_entity_manager.return_value = entity_manager
    crawler = MagicMock()
    crawler.start = AsyncMock()
    crawler.stop = AsyncMock()
    pipeline = IngestionPipeline(
        "org-native-graph",
        generate_embeddings=False,
        integrate_with_graph=True,
    )

    with (
        patch("sibyl.crawler.pipeline.CrawlerService", return_value=crawler),
        patch(
            "sibyl.crawler.pipeline.create_graph_integration_service",
            AsyncMock(return_value=service),
        ) as create_service,
    ):
        await pipeline.start()
        await pipeline.stop()

    create_service.assert_awaited_once_with(
        "org-native-graph",
        extract_entities=True,
        create_new_entities=False,
    )
    service.get_entity_manager.assert_called_once_with()
    assert pipeline._graph_integration is service
    assert pipeline._entity_manager is entity_manager
    crawler.start.assert_awaited_once()
    crawler.stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_reingest_source_uses_runtime_lookup() -> None:
    source = CrawlSourceRecord(
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
