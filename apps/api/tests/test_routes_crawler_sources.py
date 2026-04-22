"""Tests for crawl source route delegation."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest
from fastapi import HTTPException

from sibyl.api.event_types import WSEvent
from sibyl.api.routes.crawler import create_source, list_sources
from sibyl.api.schemas import CrawlSourceCreate
from sibyl.crawler.service import SourceAlreadyExistsError
from sibyl.db import CrawlStatus, SourceType


def _make_source() -> SimpleNamespace:
    return SimpleNamespace(
        id=UUID("00000000-0000-0000-0000-000000000123"),
        name="Docs",
        url="https://docs.example.com",
        source_type=SourceType.WEBSITE,
        description="Reference docs",
        crawl_depth=3,
        crawl_status=CrawlStatus.PENDING,
        document_count=8,
        chunk_count=21,
        last_crawled_at=None,
        last_error=None,
        created_at=datetime(2026, 4, 14, 3, 30, tzinfo=UTC),
        include_patterns=["/reference/.*"],
        exclude_patterns=["/blog/.*"],
    )


class TestCrawlSourceRoutes:
    @pytest.mark.asyncio
    async def test_create_source_delegates_to_service_and_broadcasts(self) -> None:
        org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000999"))
        request = CrawlSourceCreate(
            name="Docs",
            url="https://docs.example.com/",
            source_type="website",
            description="Reference docs",
            crawl_depth=3,
            include_patterns=["/reference/.*"],
            exclude_patterns=["/blog/.*"],
        )
        source = _make_source()
        broadcast = AsyncMock()

        @asynccontextmanager
        async def mock_content_session():
            yield None

        with (
            patch(
                "sibyl.api.routes.crawler.get_content_read_session",
                mock_content_session,
            ),
            patch(
                "sibyl.api.routes.crawler.create_crawl_source_record",
                AsyncMock(return_value=source),
            ) as create_record,
            patch("sibyl.api.routes.crawler.broadcast_event", broadcast),
        ):
            response = await create_source(request=request, org=org)

        create_record.assert_awaited_once_with(
            None,
            name="Docs",
            url="https://docs.example.com/",
            organization_id=org.id,
            source_type=SourceType.WEBSITE,
            description="Reference docs",
            crawl_depth=3,
            include_patterns=["/reference/.*"],
            exclude_patterns=["/blog/.*"],
        )
        broadcast.assert_awaited_once_with(
            WSEvent.ENTITY_CREATED,
            {"type": "crawl_source", "id": str(source.id)},
            org_id=str(org.id),
        )
        assert response.model_dump() == {
            "id": str(source.id),
            "name": "Docs",
            "url": "https://docs.example.com",
            "source_type": "website",
            "description": "Reference docs",
            "crawl_depth": 3,
            "crawl_status": "pending",
            "document_count": 8,
            "chunk_count": 21,
            "last_crawled_at": None,
            "last_error": None,
            "created_at": datetime(2026, 4, 14, 3, 30, tzinfo=UTC),
            "include_patterns": ["/reference/.*"],
            "exclude_patterns": ["/blog/.*"],
        }

    @pytest.mark.asyncio
    async def test_create_source_maps_duplicate_to_conflict(self) -> None:
        org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000999"))
        request = CrawlSourceCreate(name="Docs", url="https://docs.example.com/")

        @asynccontextmanager
        async def mock_content_session():
            yield None

        with (
            patch(
                "sibyl.api.routes.crawler.get_content_read_session",
                mock_content_session,
            ),
            patch(
                "sibyl.api.routes.crawler.create_crawl_source_record",
                AsyncMock(side_effect=SourceAlreadyExistsError("https://docs.example.com")),
            ),
            pytest.raises(HTTPException) as exc_info,
        ):
            await create_source(request=request, org=org)

        assert exc_info.value.status_code == 409
        assert exc_info.value.detail == "Source with URL https://docs.example.com/ already exists"

    @pytest.mark.asyncio
    async def test_list_sources_delegates_org_filter_and_maps_response(self) -> None:
        org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000999"))

        @asynccontextmanager
        async def mock_content_session():
            yield None

        with patch(
            "sibyl.api.routes.crawler.get_content_read_session",
            mock_content_session,
        ), patch(
            "sibyl.api.routes.crawler.list_crawl_sources_for_org",
            AsyncMock(return_value=([_make_source()], 7)),
        ) as list_sources_for_org:
            response = await list_sources(status="pending", limit=25, org=org)

        list_sources_for_org.assert_awaited_once_with(
            None,
            organization_id=org.id,
            status=CrawlStatus.PENDING,
            limit=25,
        )
        assert response.total == 7
        assert len(response.sources) == 1
        assert response.sources[0].model_dump() == {
            "id": "00000000-0000-0000-0000-000000000123",
            "name": "Docs",
            "url": "https://docs.example.com",
            "source_type": "website",
            "description": "Reference docs",
            "crawl_depth": 3,
            "crawl_status": "pending",
            "document_count": 8,
            "chunk_count": 21,
            "last_crawled_at": None,
            "last_error": None,
            "created_at": datetime(2026, 4, 14, 3, 30, tzinfo=UTC),
            "include_patterns": ["/reference/.*"],
            "exclude_patterns": ["/blog/.*"],
        }
