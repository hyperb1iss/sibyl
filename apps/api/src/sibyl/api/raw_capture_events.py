"""Raw-capture realtime event helpers."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import structlog

from sibyl.api.event_types import WSEvent

log = structlog.get_logger()


async def publish_raw_capture_changed(
    *,
    organization_id: str,
    raw_memory_ids: Iterable[object],
    promotion_job_id: object | None = None,
    rows_seen: object | None = None,
    previous_versionstamp: object | None = None,
    next_versionstamp: object | None = None,
) -> None:
    raw_memory_id_values = [str(raw_memory_id) for raw_memory_id in raw_memory_ids if raw_memory_id]
    if not organization_id or not raw_memory_id_values:
        return

    payload: dict[str, Any] = {
        "organization_id": organization_id,
        "raw_memory_ids": raw_memory_id_values,
    }
    optional_fields = {
        "promotion_job_id": promotion_job_id,
        "rows_seen": rows_seen,
        "previous_versionstamp": previous_versionstamp,
        "next_versionstamp": next_versionstamp,
    }
    payload.update({key: value for key, value in optional_fields.items() if value is not None})

    try:
        from sibyl.api.pubsub import publish_event

        await publish_event(WSEvent.RAW_CAPTURE_CHANGED, payload, org_id=organization_id)
    except Exception:
        log.debug("raw_capture_changed_publish_failed", organization_id=organization_id)
