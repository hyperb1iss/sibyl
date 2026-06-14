from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from sibyl.services import raw_capture_live


@pytest_asyncio.fixture(autouse=True)
async def _stop_live_query_task() -> None:
    await raw_capture_live.stop_raw_capture_live_query()
    yield
    await raw_capture_live.stop_raw_capture_live_query()


@pytest.mark.asyncio
async def test_handle_raw_capture_live_notification_queues_capture_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queue_changes = AsyncMock(return_value="raw_promotion:queued")
    monkeypatch.setattr(raw_capture_live, "queue_raw_capture_changes", queue_changes)

    result = await raw_capture_live.handle_raw_capture_live_notification(
        {
            "action": "CREATE",
            "result": {"uuid": "raw-a", "organization_id": "org-1"},
        }
    )

    assert result["status"] == "queued"
    assert result["organization_id"] == "org-1"
    assert result["raw_memory_ids"] == ["raw-a"]
    queue_changes.assert_awaited_once_with(
        "org-1",
        raw_memory_ids=["raw-a"],
        rows_seen=1,
    )


@pytest.mark.asyncio
async def test_handle_raw_capture_live_notification_accepts_sdk_record_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queue_changes = AsyncMock(return_value="raw_promotion:queued")
    monkeypatch.setattr(raw_capture_live, "queue_raw_capture_changes", queue_changes)

    result = await raw_capture_live.handle_raw_capture_live_notification(
        {"uuid": "raw-a", "organization_id": "org-1"}
    )

    assert result["status"] == "queued"
    assert result["action"] == "LIVE"
    queue_changes.assert_awaited_once_with(
        "org-1",
        raw_memory_ids=["raw-a"],
        rows_seen=1,
    )


@pytest.mark.asyncio
async def test_handle_raw_capture_live_notification_ignores_delete() -> None:
    result = await raw_capture_live.handle_raw_capture_live_notification(
        {
            "action": "DELETE",
            "result": {"uuid": "raw-a", "organization_id": "org-1"},
        }
    )

    assert result == {"status": "ignored", "reason": "unsupported_action", "action": "DELETE"}


@pytest.mark.asyncio
async def test_start_raw_capture_live_query_skips_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    build_client = AsyncMock()
    monkeypatch.setattr(raw_capture_live.settings, "raw_capture_live_query_enabled", False)
    monkeypatch.setattr(raw_capture_live, "build_surreal_content_client", build_client)

    assert await raw_capture_live.start_raw_capture_live_query() is False

    build_client.assert_not_called()
    assert raw_capture_live.raw_capture_live_query_running() is False


@pytest.mark.asyncio
async def test_start_raw_capture_live_query_skips_unsupported_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = AsyncMock()
    client.supports_live_queries = False
    monkeypatch.setattr(raw_capture_live.settings, "raw_capture_live_query_enabled", True)
    monkeypatch.setattr(raw_capture_live, "build_surreal_content_client", lambda: client)

    assert await raw_capture_live.start_raw_capture_live_query() is False

    client.close.assert_awaited_once()
    assert raw_capture_live.raw_capture_live_query_running() is False
