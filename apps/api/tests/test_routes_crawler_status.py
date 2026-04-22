"""Tests for crawler status routes."""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest

from sibyl.api.routes.crawler import get_link_graph_status
from sibyl_core.tools.link_graph_status import (
    LinkGraphSourceStatusData,
    LinkGraphStatusData,
)


class TestLinkGraphStatusRoute:
    """Tests for /sources/link-graph/status."""

    @pytest.mark.asyncio
    async def test_keeps_same_name_sources_distinct(self) -> None:
        """Status groups by stable source ID instead of collapsing on name."""
        org_id = UUID("00000000-0000-0000-0000-000000000111")
        org = SimpleNamespace(id=org_id)

        session = AsyncMock()
        status = LinkGraphStatusData(
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

        @asynccontextmanager
        async def mock_session():
            yield session

        helper = AsyncMock(return_value=status)
        with (
            patch("sibyl.api.routes.crawler.get_content_read_session", mock_session),
            patch("sibyl.api.routes.crawler.get_link_graph_status_payload", helper),
        ):
            response = await get_link_graph_status(org=org)

        helper.assert_awaited_once_with(session, organization_id=org.id)
        assert response.total_chunks == 12
        assert response.chunks_with_entities == 5
        assert response.chunks_pending == 7
        assert [source.model_dump() for source in response.sources] == [
            {
                "source_id": "00000000-0000-0000-0000-000000000aaa",
                "name": "Docs",
                "pending": 4,
            },
            {
                "source_id": "00000000-0000-0000-0000-000000000bbb",
                "name": "Docs",
                "pending": 3,
            },
        ]
