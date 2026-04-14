"""Tests for shared link-graph status aggregation helpers."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest

from sibyl_core.tools.link_graph_status import (
    LinkGraphSourceStatusData,
    LinkGraphStatusData,
    get_link_graph_status_data,
)


class TestGetLinkGraphStatusData:
    """Tests for the shared link-graph status aggregation helper."""

    @pytest.mark.asyncio
    async def test_org_scoped_and_keeps_sources_distinct(self) -> None:
        """The helper should scope by org and keep source IDs distinct."""
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
