"""Tests for shared link-graph status aggregation helpers."""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import patch

import pytest

from sibyl_core.tools.link_graph_status import (
    LinkGraphSourceStatusData,
    LinkGraphStatusData,
    get_link_graph_status_data,
)


class _SequencedContentClient:
    def __init__(self, responses: list[object]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def execute_query(self, query: str, **kwargs: object) -> object:
        self.calls.append((query, kwargs))
        return self.responses.pop(0)


class TestGetLinkGraphStatusData:
    """Tests for the shared link-graph status aggregation helper."""

    @pytest.mark.asyncio
    async def test_surreal_mode_aggregates_without_sql_session(self) -> None:
        organization_id = "00000000-0000-0000-0000-000000000111"
        source_a_id = "00000000-0000-0000-0000-000000000aaa"
        source_b_id = "00000000-0000-0000-0000-000000000bbb"
        unknown_source_id = "00000000-0000-0000-0000-000000000ccc"
        client = _SequencedContentClient(
            [
                [
                    {
                        "uuid": source_a_id,
                        "organization_id": organization_id,
                        "name": "Docs",
                        "url": "https://docs-a.example.com",
                    },
                    {
                        "uuid": source_b_id,
                        "organization_id": organization_id,
                        "name": "Docs",
                        "url": "https://docs-b.example.com",
                    },
                ],
                [{"total": 3}],
                [{"total": 1}],
                [
                    {"source_id": source_a_id, "pending": 1},
                    {"source_id": source_b_id, "pending": 1},
                    {"source_id": unknown_source_id, "pending": 4},
                ],
            ]
        )

        @asynccontextmanager
        async def client_scope():
            yield client

        with patch(
            "sibyl_core.services.content_client.surreal_content_client",
            client_scope,
        ):
            status = await get_link_graph_status_data(None, organization_id)

        assert status == LinkGraphStatusData(
            total_chunks=3,
            chunks_with_entities=1,
            sources=[
                LinkGraphSourceStatusData(source_id=source_a_id, name="Docs", pending=1),
                LinkGraphSourceStatusData(source_id=source_b_id, name="Docs", pending=1),
            ],
        )
        queries = [query for query, _ in client.calls]
        assert len(queries) == 4
        assert all("SELECT * FROM document_chunks" not in query for query in queries)
        assert "GROUP BY source_id" in queries[-1]
