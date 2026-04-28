"""Tests for Surreal-backed document search."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from sibyl_core.services import document_search as document_search_service
from sibyl_core.services.document_search import search_documents
from sibyl_core.services.surreal_content import ContentChunk, ContentDocument, ContentSource


class TestDocumentSearch:
    @pytest.mark.asyncio
    async def test_search_documents_uses_direct_surreal_chunk_search(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(document_search_service.settings, "store", "surreal")

        source = ContentSource(
            id="src-1",
            organization_id="org-1",
            name="Docs",
            url="https://docs.example.com",
        )
        document = ContentDocument(
            id="doc-1",
            source_id="src-1",
            url="https://docs.example.com/guide",
            title="Guide",
            content="alpha beta guide",
            has_code=False,
        )
        chunks = [
            ContentChunk(
                id="chunk-1",
                document_id="doc-1",
                content="alpha beta",
                context="intro",
                heading_path=["Intro"],
                embedding=[1.0, 0.0],
            ),
            ContentChunk(
                id="chunk-2",
                document_id="doc-1",
                content="alpha beta deeper",
                context="details",
                heading_path=["Intro", "Details"],
                embedding=[0.7, 0.0],
            ),
        ]

        direct_search = AsyncMock(
            return_value=(
                [(chunks[0], document, source.name, source.id, 0.91)],
                [(chunks[1], document, source.name, source.id, 0.42)],
            )
        )
        scope_loader = AsyncMock()
        with (
            patch(
                "sibyl_core.services.document_search.search_document_chunks",
                direct_search,
            ),
            patch(
                "sibyl_core.services.document_search.load_search_scope",
                scope_loader,
            ),
            patch("sibyl.crawler.embedder.embed_text", AsyncMock(return_value=[1.0, 0.0])),
        ):
            results = await search_documents("alpha", organization_id="org-1", limit=5)

        assert len(results) == 1
        assert results[0].metadata["document_id"] == "doc-1"
        assert results[0].source == "Docs"
        assert results[0].content.startswith("[Intro]")
        direct_search.assert_awaited_once()
        scope_loader.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_search_documents_falls_back_to_surreal_scope(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(document_search_service.settings, "store", "surreal")

        source = ContentSource(
            id="src-1",
            organization_id="org-1",
            name="Docs",
            url="https://docs.example.com",
        )
        document = ContentDocument(
            id="doc-1",
            source_id="src-1",
            url="https://docs.example.com/guide",
            title="Guide",
            content="alpha beta guide",
            has_code=False,
        )
        chunks = [
            ContentChunk(
                id="chunk-1",
                document_id="doc-1",
                content="alpha beta",
                context="intro",
                heading_path=["Intro"],
                embedding=[1.0, 0.0],
            ),
            ContentChunk(
                id="chunk-2",
                document_id="doc-1",
                content="alpha beta deeper",
                context="details",
                heading_path=["Intro", "Details"],
                embedding=[0.7, 0.0],
            ),
        ]

        scope_loader = AsyncMock(
            return_value=(
                [source],
                {source.id: source},
                {document.id: document},
                chunks,
            )
        )
        with (
            patch(
                "sibyl_core.services.document_search.search_document_chunks",
                AsyncMock(side_effect=RuntimeError("index unavailable")),
            ),
            patch(
                "sibyl_core.services.document_search.load_search_scope",
                scope_loader,
            ),
            patch("sibyl.crawler.embedder.embed_text", AsyncMock(return_value=[1.0, 0.0])),
        ):
            results = await search_documents("alpha", organization_id="org-1", limit=5)

        assert len(results) == 1
        assert results[0].metadata["document_id"] == "doc-1"
        assert results[0].source == "Docs"
        assert results[0].content.startswith("[Intro]")
        scope_loader.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_search_documents_tokenizes_document_content_once(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(document_search_service.settings, "store", "surreal")

        source = ContentSource(
            id="src-1",
            organization_id="org-1",
            name="Docs",
            url="https://docs.example.com",
        )
        document = ContentDocument(
            id="doc-1",
            source_id="src-1",
            url="https://docs.example.com/guide",
            title="Guide",
            content="rareterm " * 2000,
            has_code=False,
        )
        chunks = [
            ContentChunk(
                id=f"chunk-{index}",
                document_id="doc-1",
                content=f"chunk {index}",
                context="details",
                heading_path=["Guide"],
            )
            for index in range(6)
        ]

        document_content_calls = 0
        original_tokenize_fields = document_search_service.tokenize_fields

        def tracked_tokenize_fields(*fields: str | None) -> set[str]:
            nonlocal document_content_calls
            if document.content in fields:
                document_content_calls += 1
            return original_tokenize_fields(*fields)

        monkeypatch.setattr(document_search_service, "tokenize_fields", tracked_tokenize_fields)

        with (
            patch(
                "sibyl_core.services.document_search.search_document_chunks",
                AsyncMock(side_effect=RuntimeError("index unavailable")),
            ),
            patch(
                "sibyl_core.services.document_search.load_search_scope",
                AsyncMock(
                    return_value=(
                        [source],
                        {source.id: source},
                        {document.id: document},
                        chunks,
                    )
                ),
            ),
            patch("sibyl.crawler.embedder.embed_text", AsyncMock(return_value=[1.0, 0.0])),
        ):
            results = await search_documents("rareterm", organization_id="org-1", limit=5)

        assert len(results) == 1
        assert document_content_calls == 1
