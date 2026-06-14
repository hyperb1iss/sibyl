from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from sibyl.api.event_types import WSEvent
from sibyl.api.raw_capture_events import publish_raw_capture_changed


@pytest.mark.asyncio
async def test_publish_raw_capture_changed_scopes_payload_to_org(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publish_event = AsyncMock()
    monkeypatch.setattr("sibyl.api.pubsub.publish_event", publish_event)

    await publish_raw_capture_changed(
        organization_id="org-1",
        raw_memory_ids=["raw-a", "raw-b"],
        promotion_job_id="raw_promotion:queued",
        rows_seen=2,
        previous_versionstamp=3,
        next_versionstamp=9,
    )

    publish_event.assert_awaited_once_with(
        WSEvent.RAW_CAPTURE_CHANGED,
        {
            "organization_id": "org-1",
            "raw_memory_ids": ["raw-a", "raw-b"],
            "promotion_job_id": "raw_promotion:queued",
            "rows_seen": 2,
            "previous_versionstamp": 3,
            "next_versionstamp": 9,
        },
        org_id="org-1",
    )


@pytest.mark.asyncio
async def test_publish_raw_capture_changed_skips_empty_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publish_event = AsyncMock()
    monkeypatch.setattr("sibyl.api.pubsub.publish_event", publish_event)

    await publish_raw_capture_changed(organization_id="org-1", raw_memory_ids=[])

    publish_event.assert_not_awaited()
