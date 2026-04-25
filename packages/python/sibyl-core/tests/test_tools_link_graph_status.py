"""Tests for shared link-graph status aggregation helpers."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest

from sibyl_core.services import link_graph_status as status_service
from sibyl_core.services.surreal_content import ContentChunk, ContentDocument, ContentSource
from sibyl_core.tools.link_graph_status import (
    LinkGraphSourceStatusData,
    LinkGraphStatusData,
    get_link_graph_status_data,
)


class TestGetLinkGraphStatusData:
    """Tests for the shared link-graph status aggregation helper."""

    @pytest.mark.asyncio
    async def test_org_scoped_and_keeps_sources_distinct(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The helper should scope by org and keep source IDs distinct."""
        monkeypatch.setattr(status_service.settings, "store", "legacy")
        org_id = UUID("00000000-0000-0000-0000-000000000111")

        session = AsyncMock()
        session.execute = AsyncMock(
            side_effect=[
                MagicMock(scalar=MagicMock(return_value=12)),
                MagicMock(scalar=MagicMock(return_value=5)),
                MagicMock(
                    all=MagicMock(
                        return_value=[
                            SimpleNamespace(
                                source_id=UUID("00000000-0000-0000-0000-000000000aaa"),
                                name="Docs",
                                pending=4,
                            ),
                            SimpleNamespace(
                                source_id=UUID("00000000-0000-0000-0000-000000000bbb"),
                                name="Docs",
                                pending=3,
                            ),
                        ]
                    )
                ),
            ]
        )

        status = await get_link_graph_status_data(session, org_id)

        rendered_queries = [str(call.args[0]) for call in session.execute.await_args_list]

        assert status == LinkGraphStatusData(
            total_chunks=12,
            chunks_with_entities=5,
            sources=[
                LinkGraphSourceStatusData(
                    source_id="00000000-0000-0000-0000-000000000aaa",
                    name="Docs",
                    pending=4,
                ),
                LinkGraphSourceStatusData(
                    source_id="00000000-0000-0000-0000-000000000bbb",
                    name="Docs",
                    pending=3,
                ),
            ],
        )
        assert status.chunks_pending == 7
        assert all("organization_id" in query for query in rendered_queries)
        assert any("has_entities = true" in query.lower() for query in rendered_queries)
        assert any("has_entities = false" in query.lower() for query in rendered_queries)

    @pytest.mark.asyncio
    async def test_surreal_mode_aggregates_without_sql_session(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(status_service.settings, "store", "surreal")

        source_a = ContentSource(
            id="00000000-0000-0000-0000-000000000aaa",
            organization_id="00000000-0000-0000-0000-000000000111",
            name="Docs",
            url="https://docs-a.example.com",
        )
        source_b = ContentSource(
            id="00000000-0000-0000-0000-000000000bbb",
            organization_id="00000000-0000-0000-0000-000000000111",
            name="Docs",
            url="https://docs-b.example.com",
        )
        documents = {
            "doc-a": ContentDocument(
                id="doc-a",
                source_id=source_a.id,
                url="https://docs-a.example.com/guide",
            ),
            "doc-b": ContentDocument(
                id="doc-b",
                source_id=source_b.id,
                url="https://docs-b.example.com/guide",
            ),
        }
        chunks = [
            ContentChunk(id="chunk-1", document_id="doc-a", has_entities=True),
            ContentChunk(id="chunk-2", document_id="doc-a", has_entities=False),
            ContentChunk(id="chunk-3", document_id="doc-b", has_entities=False),
        ]

        with patch(
            "sibyl_core.services.link_graph_status.load_search_scope",
            AsyncMock(
                return_value=(
                    [source_a, source_b],
                    {source_a.id: source_a, source_b.id: source_b},
                    documents,
                    chunks,
                )
            ),
        ):
            status = await get_link_graph_status_data(None, source_a.organization_id)

        assert status == LinkGraphStatusData(
            total_chunks=3,
            chunks_with_entities=1,
            sources=[
                LinkGraphSourceStatusData(source_id=source_a.id, name="Docs", pending=1),
                LinkGraphSourceStatusData(source_id=source_b.id, name="Docs", pending=1),
            ],
        )
