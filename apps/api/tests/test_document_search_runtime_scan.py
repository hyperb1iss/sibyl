"""Runtime-scan path tests for sibyl_core document search.

These live under apps/api rather than packages/python/sibyl-core because
``_search_documents_runtime_scan`` lazily imports ``sibyl.persistence``,
which is only importable where the api package is installed; the core
package's isolated test environment has no ``sibyl`` module.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from sibyl_core.services import document_search as document_search_service
from sibyl_core.services.document_search import search_documents


@pytest.mark.asyncio
async def test_search_documents_runtime_scan_stops_at_chunk_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(document_search_service.settings, "store", "legacy")
    monkeypatch.setattr(document_search_service, "RUNTIME_SCAN_CHUNK_LIMIT_MULTIPLIER", 1)
    monkeypatch.setattr(document_search_service, "RUNTIME_SCAN_CHUNK_LIMIT_MAX", 3)

    source_ids = [uuid4(), uuid4()]
    sources = [
        SimpleNamespace(id=source_ids[0], name="Source 1"),
        SimpleNamespace(id=source_ids[1], name="Source 2"),
    ]
    documents = {
        source_ids[0]: [
            SimpleNamespace(
                id=uuid4(),
                source_id=source_ids[0],
                title="Doc 1",
                content="alpha",
                url="https://example.com/1",
                has_code=False,
            )
        ],
        source_ids[1]: [
            SimpleNamespace(
                id=uuid4(),
                source_id=source_ids[1],
                title="Doc 2",
                content="alpha",
                url="https://example.com/2",
                has_code=False,
            )
        ],
    }
    chunks = {
        source_ids[0]: [
            SimpleNamespace(
                id=uuid4(),
                document_id=documents[source_ids[0]][0].id,
                content="alpha one",
                context="",
                heading_path=[],
                chunk_type="text",
                chunk_index=0,
                language=None,
                has_entities=False,
                embedding=[1.0, 0.0],
            ),
            SimpleNamespace(
                id=uuid4(),
                document_id=documents[source_ids[0]][0].id,
                content="alpha two",
                context="",
                heading_path=[],
                chunk_type="text",
                chunk_index=1,
                language=None,
                has_entities=False,
                embedding=[1.0, 0.0],
            ),
        ],
        source_ids[1]: [
            SimpleNamespace(
                id=uuid4(),
                document_id=documents[source_ids[1]][0].id,
                content="alpha three",
                context="",
                heading_path=[],
                chunk_type="text",
                chunk_index=0,
                language=None,
                has_entities=False,
                embedding=[1.0, 0.0],
            ),
            SimpleNamespace(
                id=uuid4(),
                document_id=documents[source_ids[1]][0].id,
                content="alpha four",
                context="",
                heading_path=[],
                chunk_type="text",
                chunk_index=1,
                language=None,
                has_entities=False,
                embedding=[1.0, 0.0],
            ),
        ],
    }

    chunk_calls: list = []

    @asynccontextmanager
    async def fake_session():
        yield object()

    async def fake_list_sources_for_graph_linking(*args, **kwargs):
        return sources

    async def fake_list_source_documents(*args, source_id, **kwargs):
        return documents[source_id]

    async def fake_list_source_chunks(*args, source_id, **kwargs):
        chunk_calls.append(source_id)
        return chunks[source_id]

    with (
        patch(
            "sibyl_core.services.document_search._embed_text",
            AsyncMock(return_value=[1.0, 0.0]),
        ),
        patch("sibyl.persistence.content_runtime.get_content_read_session", fake_session),
        patch(
            "sibyl.persistence.content_runtime.list_sources_for_graph_linking",
            fake_list_sources_for_graph_linking,
        ),
        patch(
            "sibyl.persistence.content_runtime.list_source_documents",
            fake_list_source_documents,
        ),
        patch("sibyl.persistence.content_runtime.list_source_chunks", fake_list_source_chunks),
    ):
        results = await search_documents("alpha", organization_id=str(uuid4()), limit=3)

    assert results
    assert len(chunk_calls) == 2
