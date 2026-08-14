"""Reconciling a written row with the verdict that governs it.

A pre-write read cannot close this race on its own. Whatever is read before the
write, the write itself is an await, and a correction can land inside it: the
correction's cascade runs while the insert is still in flight, finds nothing,
and the stale row commits afterwards. Narrowing that window makes the race
rarer without ever making it impossible.

So the write is bracketed instead. The pre-write read stays, because it is
cheap and it is what lets most rows be born correct. After the rows commit,
the verdict is read once more: if it moved, the just-written rows are stamped
by id. A correction that ran before the write is seen by the pre-write read, a
correction that ran after it is seen by the cascade, and a correction that ran
during it is seen by this pass. There is no fourth interval.

The verdict has two authorities depending on what was written. A row projected
straight from a capture answers to `raw_captures`, the row a correction
mutates. A row derived from another graph row answers to that parent, which
the correction stamps.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import structlog

from sibyl_core.projection.inheritance import inherited_lifecycle_metadata

log = structlog.get_logger()

# Follows the embedding-backfill retry already in the worker
# (`jobs/entities.py`): a few attempts, short linear backoff, no new
# machinery. The failures worth retrying here are the same shape, a
# momentarily unreachable store rather than a bad request.
RECONCILE_MAX_ATTEMPTS = 4
RECONCILE_RETRY_BASE_SECONDS = 0.25

# Written onto a row whose verdict could not be read after the retries ran
# out. The row is excluded on the way in rather than left servable, because
# "we could not check" and "it is fine" are not the same statement, and a
# reader cannot tell them apart from the row.
RECONCILE_PENDING_KEY = "lifecycle_reconciliation_pending"

_UNVERIFIED_STAMP: dict[str, Any] = {
    "excluded_from_recall": True,
    RECONCILE_PENDING_KEY: True,
}


@dataclass(frozen=True, slots=True)
class ReconcileOutcome:
    """What the post-write pass did, so a caller can log or assert on it."""

    checked: int = 0
    restamped: int = 0
    unverified: int = 0
    changed: bool = False


async def _with_retries(operation: str, callback: Any) -> tuple[Any, bool]:
    """Run a reconciliation read, retrying transient failures a bounded number of times.

    Returns the value and whether it was actually obtained, rather than
    raising. A raised failure here is worse than it looks: the local broker
    records a failed job as COMPLETE with an error and then suppresses the same
    deterministic job id for the result TTL, so an exception is a poison pill
    rather than a retry. The caller decides what an unread verdict means, and
    for a written row it means the row does not serve until someone can check.
    """

    for attempt in range(1, RECONCILE_MAX_ATTEMPTS + 1):
        try:
            return await callback(), True
        except Exception as exc:
            if attempt >= RECONCILE_MAX_ATTEMPTS:
                log.warning(
                    "lifecycle_reconciliation_unavailable",
                    operation=operation,
                    attempts=attempt,
                    error_type=type(exc).__name__,
                )
                return None, False
            delay_seconds = RECONCILE_RETRY_BASE_SECONDS * attempt
            log.warning(
                "lifecycle_reconciliation_retry",
                operation=operation,
                attempt=attempt,
                delay_seconds=delay_seconds,
                error_type=type(exc).__name__,
            )
            await asyncio.sleep(delay_seconds)
    return None, False


async def _apply_stamp(
    entity_manager: Any,
    row_ids: Sequence[str],
    stamp: Mapping[str, Any],
) -> int:
    """Write one verdict onto rows that already exist, by id.

    Idempotent by construction: the same stamp applied twice is the same row.
    """

    update = getattr(entity_manager, "update", None)
    if not callable(update) or not stamp:
        return 0
    applied = 0
    for row_id in dict.fromkeys(row_ids):
        if not row_id:
            continue
        try:
            result = await update(str(row_id), {"metadata": dict(stamp)})
        except Exception as exc:
            log.warning(
                "lifecycle_reconciliation_stamp_failed",
                entity_id=row_id,
                error_type=type(exc).__name__,
            )
            continue
        if result is not None:
            applied += 1
    return applied


async def _reconcile(
    entity_manager: Any,
    *,
    operation: str,
    read_verdict: Any,
    applied_stamp: Mapping[str, Any] | None,
    row_ids: Sequence[str],
) -> ReconcileOutcome:
    ids = [str(row_id) for row_id in dict.fromkeys(row_ids) if row_id]
    if not ids:
        return ReconcileOutcome()

    verdict, verified = await _with_retries(operation, read_verdict)
    if not verified:
        marked = await _apply_stamp(entity_manager, ids, _UNVERIFIED_STAMP)
        return ReconcileOutcome(checked=len(ids), unverified=marked)

    current = dict(verdict or {})
    previous = dict(applied_stamp or {})
    if current == previous:
        return ReconcileOutcome(checked=len(ids))

    # The verdict moved between the pre-write read and now, which is the
    # interval the write itself occupies. The rows exist, so they are stamped
    # rather than rebuilt.
    restamped = await _apply_stamp(entity_manager, ids, current or {"excluded_from_recall": False})
    log.info(
        "lifecycle_reconciliation_restamped",
        operation=operation,
        rows=restamped,
        lifecycle_state=current.get("lifecycle_state"),
    )
    return ReconcileOutcome(checked=len(ids), restamped=restamped, changed=True)


async def prewrite_capture_stamp(
    *,
    organization_id: str,
    metadata: Mapping[str, Any] | None,
) -> tuple[dict[str, Any], bool]:
    """The verdict to write a row with, and whether it was actually read.

    An unread verdict is not an error here. The row still gets written, and the
    post-write pass is what refuses to let an unverified row serve, so a
    momentary outage costs a retired-until-checked row rather than a poisoned
    job that never runs again.
    """

    from sibyl_core.services.memory import projected_row_lifecycle_stamp

    async def read() -> dict[str, Any]:
        return await projected_row_lifecycle_stamp(
            organization_id=organization_id,
            metadata=metadata,
        )

    stamp, verified = await _with_retries("capture_prewrite", read)
    return dict(stamp or {}), verified


async def reconcile_with_capture(
    entity_manager: Any,
    *,
    organization_id: str,
    metadata: Mapping[str, Any] | None,
    row_ids: Sequence[str],
    applied_stamp: Mapping[str, Any] | None,
) -> ReconcileOutcome:
    """Re-check a written row against the capture it was projected from."""

    from sibyl_core.services.memory import projected_row_lifecycle_stamp

    async def read() -> dict[str, Any]:
        return await projected_row_lifecycle_stamp(
            organization_id=organization_id,
            metadata=metadata,
        )

    return await _reconcile(
        entity_manager,
        operation="capture",
        read_verdict=read,
        applied_stamp=applied_stamp,
        row_ids=row_ids,
    )


async def reconcile_with_parent(
    entity_manager: Any,
    *,
    source_id: str,
    row_ids: Sequence[str],
    applied_stamp: Mapping[str, Any] | None,
) -> ReconcileOutcome:
    """Re-check written derived rows against the parent they were derived from."""

    async def read() -> dict[str, Any]:
        get = getattr(entity_manager, "get", None)
        if not callable(get):
            return dict(applied_stamp or {})
        stored = await get(str(source_id))
        return inherited_lifecycle_metadata(getattr(stored, "metadata", None))

    return await _reconcile(
        entity_manager,
        operation="parent",
        read_verdict=read,
        applied_stamp=applied_stamp,
        row_ids=row_ids,
    )


__all__ = [
    "RECONCILE_MAX_ATTEMPTS",
    "RECONCILE_PENDING_KEY",
    "ReconcileOutcome",
    "prewrite_capture_stamp",
    "reconcile_with_capture",
    "reconcile_with_parent",
]
