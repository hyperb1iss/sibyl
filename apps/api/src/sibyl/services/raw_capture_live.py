"""Opt-in SurrealDB live-query bridge for raw capture updates."""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Mapping
from typing import Any, cast

import structlog

from sibyl.config import settings
from sibyl.jobs.raw_changefeed import (
    RAW_CAPTURE_CHANGEFEED_TABLE,
    queue_raw_capture_changes,
    raw_capture_ref_from_payload,
)
from sibyl.persistence.surreal.content import build_surreal_content_client
from sibyl_core.observability import elapsed_ms

log = structlog.get_logger()

_live_query_task: asyncio.Task[None] | None = None


def raw_capture_live_query_running() -> bool:
    return _live_query_task is not None and not _live_query_task.done()


async def start_raw_capture_live_query() -> bool:
    global _live_query_task  # noqa: PLW0603

    if not settings.raw_capture_live_query_enabled:
        return False
    if raw_capture_live_query_running():
        return True

    client = build_surreal_content_client()
    if not client.supports_live_queries:
        await client.close()
        log.warning("raw_capture_live_query_unsupported", url_scheme="non_websocket")
        return False

    _live_query_task = asyncio.create_task(
        _run_raw_capture_live_query(client),
        name="sibyl-raw-capture-live-query",
    )
    return True


async def stop_raw_capture_live_query() -> None:
    global _live_query_task  # noqa: PLW0603

    task = _live_query_task
    _live_query_task = None
    if task is None:
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def handle_raw_capture_live_notification(notification: object) -> dict[str, object]:
    if not isinstance(notification, Mapping):
        return {"status": "ignored", "reason": "invalid_notification"}

    notification_map = cast("Mapping[str, object]", notification)
    payload: Mapping[str, object]
    if "action" in notification_map:
        action = str(notification_map.get("action") or "").upper()
        if action not in {"CREATE", "UPDATE"}:
            return {"status": "ignored", "reason": "unsupported_action", "action": action}

        payload_value = notification_map.get("result")
        if not isinstance(payload_value, Mapping):
            return {"status": "ignored", "reason": "missing_result", "action": action}
        payload = cast("Mapping[str, object]", payload_value)
    else:
        action = "LIVE"
        payload = notification_map

    ref = raw_capture_ref_from_payload(payload)
    if ref is None:
        return {"status": "ignored", "reason": "missing_raw_capture_ref", "action": action}

    started_at = time.perf_counter()
    promotion_job_id = await queue_raw_capture_changes(
        ref.organization_id,
        raw_memory_ids=[ref.raw_memory_id],
        rows_seen=1,
    )
    result: dict[str, object] = {
        "status": "queued" if promotion_job_id else "idle",
        "action": action,
        "organization_id": ref.organization_id,
        "raw_memory_ids": [ref.raw_memory_id],
        "promotion_job_id": promotion_job_id,
        "duration_ms": elapsed_ms(started_at),
    }
    log.info("raw_capture_live_notification_processed", **result)
    return result


async def _run_raw_capture_live_query(client: Any) -> None:
    try:
        while True:
            try:
                async with client.live_table(RAW_CAPTURE_CHANGEFEED_TABLE) as notifications:
                    log.info("raw_capture_live_query_started")
                    async for notification in notifications:
                        await handle_raw_capture_live_notification(notification)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("raw_capture_live_query_failed", error=str(exc))
                await asyncio.sleep(settings.raw_capture_live_query_retry_seconds)
    finally:
        await client.close()


__all__ = [
    "handle_raw_capture_live_notification",
    "raw_capture_live_query_running",
    "start_raw_capture_live_query",
    "stop_raw_capture_live_query",
]
