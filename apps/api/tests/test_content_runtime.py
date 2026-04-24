from __future__ import annotations

from contextlib import asynccontextmanager

import pytest

from sibyl.persistence import content_common, content_runtime
from sibyl.persistence.legacy.crawler import (
    get_crawl_stats_payload as legacy_get_crawl_stats_payload,
)
from sibyl.persistence.legacy.entities import (
    get_legacy_raw_capture as legacy_get_legacy_raw_capture,
    get_raw_capture as legacy_get_raw_capture,
)
from sibyl.persistence.surreal.content import (
    get_crawl_stats_payload as surreal_get_crawl_stats_payload,
    get_legacy_raw_capture as surreal_get_legacy_raw_capture,
    get_raw_capture as surreal_get_raw_capture,
    list_rag_source_documents_page as surreal_list_rag_source_documents_page,
)


def test_content_runtime_uses_legacy_exports_in_legacy_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(content_runtime.settings, "store", "legacy")

    assert content_runtime._resolve_backend_export("get_crawl_stats_payload") is legacy_get_crawl_stats_payload
    assert content_runtime._resolve_backend_export("get_raw_capture") is legacy_get_raw_capture


def test_content_runtime_maps_surreal_exports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(content_runtime.settings, "store", "surreal")

    assert content_runtime._resolve_backend_export("get_crawl_stats_payload") is surreal_get_crawl_stats_payload
    assert (
        content_runtime._resolve_backend_export("list_rag_source_documents_page")
        is surreal_list_rag_source_documents_page
    )
    assert content_runtime._resolve_backend_export("get_raw_capture") is surreal_get_raw_capture


def test_content_runtime_keeps_legacy_aliases_pointed_at_neutral_exports() -> None:
    assert content_runtime.get_legacy_raw_capture is content_runtime.get_raw_capture
    assert content_runtime.list_legacy_raw_captures is content_runtime.list_raw_captures
    assert content_runtime.resolve_legacy_document_entity is content_runtime.resolve_document_entity
    assert legacy_get_legacy_raw_capture is legacy_get_raw_capture
    assert surreal_get_legacy_raw_capture is surreal_get_raw_capture
    assert content_common.CrawlStats is content_common.LegacyCrawlStats
    assert content_common.DocumentEntityRecord is content_common.LegacyDocumentEntityRecord


@pytest.mark.asyncio
async def test_content_runtime_skips_relational_session_in_surreal_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(content_runtime.settings, "store", "surreal")

    async with content_runtime.get_content_read_session() as session:
        assert session is None


@pytest.mark.asyncio
async def test_content_runtime_delegates_to_postgres_session_in_legacy_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = object()

    @asynccontextmanager
    async def mock_get_session():
        yield session

    monkeypatch.setattr(content_runtime.settings, "store", "legacy")
    monkeypatch.setattr(content_runtime, "get_session", mock_get_session)

    async with content_runtime.get_content_read_session() as yielded:
        assert yielded is session


@pytest.mark.asyncio
async def test_content_runtime_dependency_uses_active_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(content_runtime.settings, "store", "surreal")

    dependency = content_runtime.get_content_read_session_dependency()
    yielded = await anext(dependency)
    assert yielded is None

    with pytest.raises(StopAsyncIteration):
        await anext(dependency)
