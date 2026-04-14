"""Tests for job route visibility helpers."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest

from sibyl.api.routes.jobs import _job_visible_to_org


class TestJobVisibility:
    @pytest.mark.asyncio
    async def test_source_jobs_use_embedded_org_metadata_without_db_lookup(self) -> None:
        org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
        session = AsyncMock()
        job = SimpleNamespace(
            function="crawl_source",
            args=("00000000-0000-0000-0000-000000000222",),
            kwargs={"organization_id": str(org.id)},
        )

        assert await _job_visible_to_org(job, org=org, session=session) is True
        session.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_source_jobs_hide_other_org_metadata_without_db_lookup(self) -> None:
        org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
        session = AsyncMock()
        job = SimpleNamespace(
            function="sync_source",
            args=("00000000-0000-0000-0000-000000000222",),
            kwargs={"organization_id": "00000000-0000-0000-0000-000000000999"},
        )

        assert await _job_visible_to_org(job, org=org, session=session) is False
        session.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_legacy_source_jobs_fall_back_to_db_lookup(self) -> None:
        org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
        result = MagicMock()
        result.scalar_one_or_none.return_value = object()
        session = AsyncMock()
        session.execute.return_value = result
        job = SimpleNamespace(
            function="crawl_source",
            args=("00000000-0000-0000-0000-000000000222",),
            kwargs=None,
        )

        assert await _job_visible_to_org(job, org=org, session=session) is True
        session.execute.assert_awaited_once()
