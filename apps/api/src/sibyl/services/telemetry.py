"""Runtime telemetry aggregation and bounded SurrealDB rollups."""

from __future__ import annotations

import asyncio
import contextlib
import time
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import NAMESPACE_URL, uuid5

import structlog

from sibyl.persistence.surreal.content import (
    _normalize_records,
    _query_error,
    get_shared_surreal_content_client,
)
from sibyl_core.observability import telemetry_registry

log = structlog.get_logger()

_DEFAULT_WINDOW_SECONDS = 15 * 60
_ROLLUP_INTERVAL_SECONDS = 60
_ROLLUP_RETENTION_HOURS = 24
_ROLLUP_FAILURE_BACKOFF_SECONDS = 60.0
_last_persisted_bucket: int | None = None
_logged_failure_buckets: dict[str, int] = {}
_next_scheduled_rollup_at = 0.0
_scheduled_rollup_task: asyncio.Task[object] | None = None
_persist_lock = asyncio.Lock()


def runtime_summary(*, window_seconds: int = _DEFAULT_WINDOW_SECONDS) -> dict[str, Any]:
    return telemetry_registry().snapshot(window_seconds=window_seconds)


def runtime_rollup_due() -> bool:
    return _last_persisted_bucket != _current_bucket()


async def maybe_persist_runtime_rollup(
    *,
    window_seconds: int = _DEFAULT_WINDOW_SECONDS,
) -> None:
    bucket = _current_bucket()
    if _last_persisted_bucket == bucket:
        return
    await persist_runtime_rollup(window_seconds=window_seconds, bucket=bucket)


async def persist_runtime_rollup(
    *,
    window_seconds: int = _DEFAULT_WINDOW_SECONDS,
    bucket: int | None = None,
) -> dict[str, Any] | None:
    global _last_persisted_bucket  # noqa: PLW0603
    bucket = bucket or _current_bucket()
    async with _persist_lock:
        if _last_persisted_bucket == bucket:
            return None

        snapshot = runtime_summary(window_seconds=window_seconds)
        bucket_start = datetime.fromtimestamp(bucket, UTC)
        bucket_key = bucket_start.strftime("%Y%m%d%H%M")
        record = {
            "uuid": str(uuid5(NAMESPACE_URL, f"sibyl.telemetry.{bucket_key}")),
            "bucket_key": bucket_key,
            "bucket_start": bucket_start,
            "window_seconds": window_seconds,
            "uptime_seconds": snapshot["uptime_seconds"],
            "summaries": snapshot["summaries"],
            "trends": snapshot["trends"],
            "metrics": snapshot["metrics"],
            "recent_events": snapshot["recent_events"],
            "updated_at": datetime.now(UTC),
        }
        try:
            client = await get_shared_surreal_content_client()
            result = await client.execute_query(
                "UPSERT telemetry_rollups CONTENT $record WHERE bucket_key = $bucket_key;",
                bucket_key=bucket_key,
                record=record,
            )
            error = _query_error(result)
            if error is not None:
                raise RuntimeError(error)
            await _prune_old_rollups(client)
        except Exception as exc:
            _report_rollup_failure(exc, bucket=bucket, bucket_key=bucket_key)
            return None

        _last_persisted_bucket = bucket
        return record


def _report_rollup_failure(error: Exception, *, bucket: int, bucket_key: str) -> None:
    """Name a rollup failure once per error class per interval.

    telemetry_rollups sat empty because every failure went to log.debug, so
    nothing said why. Counting is unconditional; the warning is throttled by
    error class so a persistent fault does not flood the log.
    """
    error_type = type(error).__name__
    telemetry_registry().increment(
        "sibyl_telemetry_rollup_failures_total",
        labels={"error_type": error_type},
    )
    if _logged_failure_buckets.get(error_type) == bucket:
        log.debug(
            "runtime_telemetry_rollup_failed",
            error=str(error),
            error_type=error_type,
            bucket_key=bucket_key,
        )
        return
    _logged_failure_buckets[error_type] = bucket
    log.warning(
        "runtime_telemetry_rollup_failed",
        error=str(error),
        error_type=error_type,
        bucket_key=bucket_key,
    )


async def list_runtime_rollups(*, limit: int = 120) -> list[dict[str, Any]]:
    try:
        client = await get_shared_surreal_content_client()
        result = await client.execute_query(
            "SELECT * FROM telemetry_rollups ORDER BY bucket_start DESC LIMIT $limit;",
            limit=limit,
        )
        error = _query_error(result)
        if error is not None:
            raise RuntimeError(error)
    except Exception as exc:
        log.debug("runtime_telemetry_rollup_list_failed", error=str(exc))
        return []

    rows = _normalize_records(result)
    return list(reversed(rows))


def schedule_runtime_rollup_persist(*, window_seconds: int = _DEFAULT_WINDOW_SECONDS) -> None:
    global _scheduled_rollup_task  # noqa: PLW0603
    if not runtime_rollup_due():
        return
    now = time.monotonic()
    if now < _next_scheduled_rollup_at:
        return
    if _scheduled_rollup_task is not None and not _scheduled_rollup_task.done():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    _scheduled_rollup_task = loop.create_task(
        _scheduled_runtime_rollup(window_seconds=window_seconds)
    )
    _scheduled_rollup_task.add_done_callback(_finish_scheduled_rollup_task)


async def _scheduled_runtime_rollup(*, window_seconds: int) -> None:
    global _next_scheduled_rollup_at  # noqa: PLW0603
    previous_bucket = _last_persisted_bucket
    try:
        await maybe_persist_runtime_rollup(window_seconds=window_seconds)
    except Exception:
        _next_scheduled_rollup_at = time.monotonic() + _ROLLUP_FAILURE_BACKOFF_SECONDS
        raise
    if runtime_rollup_due() and _last_persisted_bucket == previous_bucket:
        _next_scheduled_rollup_at = time.monotonic() + _ROLLUP_FAILURE_BACKOFF_SECONDS
    else:
        _next_scheduled_rollup_at = 0.0


def _finish_scheduled_rollup_task(task: asyncio.Task[object]) -> None:
    global _scheduled_rollup_task  # noqa: PLW0603
    if _scheduled_rollup_task is task:
        _scheduled_rollup_task = None
    _log_background_failure(task)


def _log_background_failure(task: asyncio.Task[object]) -> None:
    with contextlib.suppress(asyncio.CancelledError):
        exc = task.exception()
        if exc is not None:
            log.debug("runtime_telemetry_rollup_task_failed", error=str(exc))


async def _prune_old_rollups(client: Any) -> None:
    cutoff = datetime.now(UTC) - timedelta(hours=_ROLLUP_RETENTION_HOURS)
    result = await client.execute_query(
        "DELETE FROM telemetry_rollups WHERE bucket_start < $cutoff;",
        cutoff=cutoff,
    )
    error = _query_error(result)
    if error is not None:
        raise RuntimeError(error)


def _current_bucket() -> int:
    return int(time.time() // _ROLLUP_INTERVAL_SECONDS) * _ROLLUP_INTERVAL_SECONDS
