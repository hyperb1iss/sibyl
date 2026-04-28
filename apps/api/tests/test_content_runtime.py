from __future__ import annotations

from contextlib import asynccontextmanager
from uuid import uuid4

import pytest

from sibyl.persistence import content_common, content_runtime
from sibyl.persistence.legacy.crawler import (
    get_crawl_stats_payload as legacy_get_crawl_stats_payload,
)
from sibyl.persistence.legacy.entities import (
    get_raw_capture as legacy_get_raw_capture,
)
from sibyl.persistence.surreal import content as surreal_content
from sibyl.persistence.surreal.content import (
    get_crawl_stats_payload as surreal_get_crawl_stats_payload,
    get_raw_capture as surreal_get_raw_capture,
    list_rag_source_documents_page as surreal_list_rag_source_documents_page,
)


def test_content_runtime_uses_legacy_exports_in_legacy_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(content_runtime.settings, "store", "legacy")

    assert (
        content_runtime._resolve_backend_export("get_crawl_stats_payload")
        is legacy_get_crawl_stats_payload
    )
    assert content_runtime._resolve_backend_export("get_raw_capture") is legacy_get_raw_capture


def test_content_runtime_maps_surreal_exports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(content_runtime.settings, "store", "surreal")

    assert (
        content_runtime._resolve_backend_export("get_crawl_stats_payload")
        is surreal_get_crawl_stats_payload
    )
    assert (
        content_runtime._resolve_backend_export("list_rag_source_documents_page")
        is surreal_list_rag_source_documents_page
    )
    assert content_runtime._resolve_backend_export("get_raw_capture") is surreal_get_raw_capture


def test_content_runtime_only_exports_neutral_runtime_surface() -> None:
    assert not hasattr(content_runtime, "get_legacy_raw_capture")
    assert not hasattr(content_runtime, "list_legacy_raw_captures")
    assert not hasattr(content_runtime, "resolve_legacy_document_entity")
    assert not hasattr(surreal_content, "get_legacy_raw_capture")
    assert not hasattr(surreal_content, "list_legacy_raw_captures")
    assert not hasattr(surreal_content, "resolve_legacy_document_entity")
    assert content_common.__all__ == ["CrawlStats", "DocumentEntityRecord"]
    assert "LegacyCrawlStats" not in content_common.__all__
    assert "LegacyDocumentEntityRecord" not in content_common.__all__
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


@pytest.mark.asyncio
async def test_surreal_search_scope_uses_source_name_fulltext_filter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, object]]] = []

        async def execute_query(self, query: str, **params: object) -> list[object]:
            self.calls.append((query, params))
            return []

    fake_client = FakeClient()

    @asynccontextmanager
    async def fake_session():
        yield fake_client

    monkeypatch.setattr(surreal_content, "surreal_content_client", fake_session)

    sources, sources_by_id, documents_by_id, chunks = await surreal_content._load_search_scope(
        organization_id=uuid4(),
        source_id=None,
        source_name='DOCS "Portal"\x00',
    )

    assert sources == []
    assert sources_by_id == {}
    assert documents_by_id == {}
    assert chunks == []
    source_query, source_params = fake_client.calls[0]
    assert "name @0@ $source_name" in source_query
    assert "string::contains" not in source_query
    assert source_params["source_name"] == "docs portal"
    assert len(fake_client.calls) == 1


@pytest.mark.asyncio
async def test_surreal_search_scope_empty_source_name_does_not_broaden_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, object]]] = []

        async def execute_query(self, query: str, **params: object) -> list[object]:
            self.calls.append((query, params))
            return []

    fake_client = FakeClient()

    @asynccontextmanager
    async def fake_session():
        yield fake_client

    monkeypatch.setattr(surreal_content, "surreal_content_client", fake_session)

    await surreal_content._load_search_scope(
        organization_id=uuid4(),
        source_id=None,
        source_name="",
    )

    source_query, source_params = fake_client.calls[0]
    assert "uuid = $source_name_empty_sentinel" in source_query
    assert source_params["source_name_empty_sentinel"] == "__sibyl_empty_source_name__"
    assert len(fake_client.calls) == 1


@pytest.mark.asyncio
async def test_surreal_search_scope_source_id_takes_precedence_over_source_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, object]]] = []

        async def execute_query(self, query: str, **params: object) -> list[object]:
            self.calls.append((query, params))
            return []

    fake_client = FakeClient()
    source_id = uuid4()

    @asynccontextmanager
    async def fake_session():
        yield fake_client

    monkeypatch.setattr(surreal_content, "surreal_content_client", fake_session)

    await surreal_content._load_search_scope(
        organization_id=uuid4(),
        source_id=source_id,
        source_name="docs",
    )

    source_query, source_params = fake_client.calls[0]
    assert "uuid = $source_id" in source_query
    assert "name @0@ $source_name" not in source_query
    assert source_params["source_id"] == str(source_id)
    assert "source_name" not in source_params
