"""Reconciling a written row with the verdict that governs it.

A pre-write read cannot close this race. Whatever is read before the write, the
write itself is an await, and a correction can land inside it: the correction's
cascade runs while the insert is in flight, finds nothing, and the stale row
commits afterwards. Narrowing that window makes the race rarer without ever
making it impossible.

So the write is bracketed instead. The pre-write read stays, because it is
cheap and it is what lets most rows be born correct. After the rows commit, the
verdict is read once more and the rows are stamped by id if it moved. A
correction that ran before the write is seen by the pre-write read, one that
ran after it is seen by the cascade, and one that ran during it is seen by this
pass. There is no fourth interval.

The pass is fenced on the row's revision, because "read the verdict, then write
it" is itself a read-modify-write and the last writer wins by default. A
correction landing between this pass's read and its write would be overwritten
by a verdict that was already stale, which is how a restored row gets retired
again by a job that started before the restore. The fence turns that into a
refused write, a re-read, and agreement with whoever wrote last.

The verdict has two authorities depending on what was written. A row projected
straight from a capture answers to `raw_captures`, the row a correction
mutates. A row derived from another graph row answers to that parent, which the
correction stamps.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import structlog

from sibyl_core.projection.inheritance import inherited_lifecycle_metadata

log = structlog.get_logger()

# Follows the embedding-backfill retry already in the worker
# (`jobs/entities.py`): a few attempts, short linear backoff, no new machinery.
# The failures worth retrying here have the same shape, a momentarily
# unreachable store rather than a bad request.
RECONCILE_MAX_ATTEMPTS = 4
RECONCILE_RETRY_BASE_SECONDS = 0.25

# How many times a fenced write may lose to a concurrent writer before this
# pass gives up on agreeing with it. Each refusal means somebody else wrote a
# verdict, so this is bounded by real contention on one row rather than by
# anything this pass controls.
RECONCILE_FENCE_ATTEMPTS = 5

# Written onto a row whose verdict could not be read after the retries ran out.
# The row is excluded on the way in rather than left servable, because "we could
# not check" and "it is fine" are not the same statement and a reader cannot
# tell them apart from the row. Cleared by the next pass that manages to read a
# verdict, and by a correction or restore that writes one.
RECONCILE_PENDING_KEY = "lifecycle_reconciliation_pending"

_UNVERIFIED_STAMP: dict[str, Any] = {
    "excluded_from_recall": True,
    RECONCILE_PENDING_KEY: True,
}

# What a row carries once somebody could read its verdict and it said nothing
# is wrong. `None` removes a key on the graph's merge patch, which is how the
# marker actually leaves the row rather than merely reading falsy.
_CLEARED_MARKER: dict[str, Any] = {RECONCILE_PENDING_KEY: None}

_VerdictReader = Callable[[], Coroutine[Any, Any, dict[str, Any]]]


@dataclass(frozen=True, slots=True)
class ReconcileOutcome:
    """What the post-write pass did, so a caller can log or assert on it."""

    checked: int = 0
    restamped: int = 0
    unverified: int = 0
    cleared: int = 0

    @property
    def changed(self) -> bool:
        return bool(self.restamped or self.unverified or self.cleared)


class ReconcileExclusionError(RuntimeError):
    """The row could not be verified and could not be excluded either.

    The one state this module refuses to leave behind quietly. Everything else
    degrades to a retired row that somebody can restore; this is a row that may
    be serving corrected text with nothing on it to say so.
    """


async def _with_retries(operation: str, callback: Any) -> tuple[Any, bool]:
    """Run a reconciliation read, retrying transient failures a bounded number of times.

    Returns the value and whether it was obtained, rather than raising. A raised
    failure here is worse than it looks: the local broker records a failed job
    as COMPLETE with an error and then suppresses the same deterministic job id
    for the result TTL, so an exception is a poison pill rather than a retry.
    The caller decides what an unread verdict means, and for a written row it
    means the row does not serve until somebody can check.
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


def _row_metadata(row: Any) -> dict[str, Any]:
    """The row's metadata as a plain mapping, or nothing.

    Guarded rather than assumed: a row whose metadata is not a mapping must not
    take reconciliation down, because the fallback for a row this pass cannot
    reason about is to exclude it, not to fail the write that produced it.
    """

    metadata = getattr(row, "metadata", None)
    return dict(metadata) if isinstance(metadata, Mapping) else {}


def _row_revision(row: Any) -> int | None:
    revision = getattr(row, "revision", None)
    return revision if isinstance(revision, int) else None


def _revision_conflict(exc: BaseException) -> bool:
    return type(exc).__name__ == "RevisionConflictError"


async def _write_stamp(
    entity_manager: Any,
    row_id: str,
    patch: Mapping[str, Any],
    *,
    expected_revision: int,
) -> tuple[bool, bool]:
    """Apply one patch under the row's revision fence, retrying transient failures.

    Returns (applied, fenced_out). A fenced-out write is not a failure: it says
    somebody else wrote this row first, and the caller re-reads so the later
    writer wins rather than this one.

    `expected_revision` is required rather than optional. An unfenced write is
    not a degraded mode of this function, it is the bug this module exists to
    prevent, so there is no argument shape that produces one.

    Transient failures are retried here rather than raised, for the same reason
    the reads are: a raised exception from a projection job is recorded by the
    local broker as a completed-with-error result and then suppresses the same
    deterministic job id, which is a poison pill rather than a retry. Exhaustion
    returns `(False, False)` and the caller decides.
    """

    update = getattr(entity_manager, "update", None)
    if not callable(update):
        return False, False
    for attempt in range(1, RECONCILE_MAX_ATTEMPTS + 1):
        try:
            result = await update(
                row_id,
                {"metadata": dict(patch)},
                expected_revision=expected_revision,
            )
        except Exception as exc:
            if _revision_conflict(exc):
                return False, True
            if attempt >= RECONCILE_MAX_ATTEMPTS:
                log.warning(
                    "lifecycle_reconciliation_write_unavailable",
                    entity_id=row_id,
                    attempts=attempt,
                    error_type=type(exc).__name__,
                )
                return False, False
            log.warning(
                "lifecycle_reconciliation_write_retry",
                entity_id=row_id,
                attempt=attempt,
                error_type=type(exc).__name__,
            )
            await asyncio.sleep(RECONCILE_RETRY_BASE_SECONDS * attempt)
            continue
        return result is not None, False
    return False, False


async def _read_row(entity_manager: Any, row_id: str) -> tuple[Any, bool]:
    get = getattr(entity_manager, "get", None)
    if not callable(get):
        return None, True

    async def read() -> Any:
        try:
            return await get(row_id)
        except KeyError:
            return None

    return await _with_retries("row", read)


def _desired_patch(verdict: Mapping[str, Any], current: Mapping[str, Any]) -> dict[str, Any]:
    """What has to change on a row whose verdict is now known.

    An empty verdict on a row that carries an exclusion is a real change, not a
    no-op: the capture was corrected and then restored while this row was being
    written, so the exclusion has to come back off.
    """

    if verdict:
        return {key: value for key, value in verdict.items() if current.get(key) != value}
    if current.get("excluded_from_recall"):
        return {"excluded_from_recall": False}
    return {}


async def _reconcile_row(
    entity_manager: Any,
    row_id: str,
    *,
    operation: str,
    organization_id: str | None,
    read_verdict: _VerdictReader,
) -> str:
    """Bring one written row into agreement with its verdict, under the fence."""

    for _attempt in range(RECONCILE_FENCE_ATTEMPTS):
        row, row_read = await _read_row(entity_manager, row_id)
        if not row_read:
            # The row could not be read, so there is no revision to fence on and
            # no safe write from here. An unfenced write at this point is how a
            # run of failed reads ends up overwriting a correction that landed
            # while they were failing.
            return await _force_exclusion(entity_manager, row_id, operation, organization_id)
        if row is None:
            # The row is gone. Nothing to reconcile and nothing to fail over.
            return "missing"
        metadata = _row_metadata(row)
        revision = _row_revision(row)

        verdict, verified = await _with_retries(operation, read_verdict)
        if not verified:
            if metadata and inherited_lifecycle_metadata(metadata):
                # Somebody authoritative already wrote a verdict onto this row,
                # so an unread capture does not make it unverified.
                return "unchanged"
            if revision is None:
                # Every write is fenced, so a row that cannot supply a revision
                # cannot be written from here.
                return await _force_exclusion(entity_manager, row_id, operation, organization_id)
            applied, fenced_out = await _write_stamp(
                entity_manager,
                row_id,
                _UNVERIFIED_STAMP,
                expected_revision=revision,
            )
            if applied:
                log.warning(
                    "lifecycle_reconciliation_row_unverified",
                    operation=operation,
                    organization_id=organization_id,
                    entity_id=row_id,
                )
                return "unverified"
            if fenced_out:
                continue
            continue

        current = dict(verdict or {})
        patch = _desired_patch(current, metadata)
        if metadata.get(RECONCILE_PENDING_KEY):
            # A verdict was readable this time, so the marker that said nobody
            # could read one has to come off with the same write.
            patch.update(_CLEARED_MARKER)
        if not patch:
            return "unchanged"
        if revision is None:
            # The row needs a write and cannot be fenced, which is the one
            # combination this pass refuses to resolve by writing anyway.
            return await _force_exclusion(entity_manager, row_id, operation, organization_id)

        applied, fenced_out = await _write_stamp(
            entity_manager,
            row_id,
            patch,
            expected_revision=revision,
        )
        if applied:
            log.info(
                "lifecycle_reconciliation_restamped",
                operation=operation,
                organization_id=organization_id,
                entity_id=row_id,
                lifecycle_state=current.get("lifecycle_state"),
                cleared_marker=RECONCILE_PENDING_KEY in patch,
            )
            return "cleared" if set(patch) == {RECONCILE_PENDING_KEY} else "restamped"
        if fenced_out:
            # Somebody wrote between this pass's read and its write. Re-read and
            # agree with them rather than overwriting a newer verdict.
            continue
        break

    # Every attempt lost the fence or failed to apply. The row's verdict is
    # unknown to this pass, so it is excluded rather than left servable.
    return await _force_exclusion(entity_manager, row_id, operation, organization_id)


async def _force_exclusion(
    entity_manager: Any,
    row_id: str,
    operation: str,
    organization_id: str | None,
) -> str:
    """Exclude a row this pass could not settle, or say plainly that it could not.

    This is the one write that is not allowed to fail quietly. A row left
    servable after reconciliation gave up is a row that may be serving
    corrected text with nothing on it to say so, which is the state this whole
    change exists to prevent.
    """

    for attempt in range(1, RECONCILE_MAX_ATTEMPTS + 1):
        row, row_read = await _read_row(entity_manager, row_id)
        if row_read and row is None:
            return "missing"
        metadata = _row_metadata(row)
        if inherited_lifecycle_metadata(metadata):
            # A verdict landed while this pass was losing its fences.
            return "unchanged"
        revision = _row_revision(row)
        applied = False
        if revision is not None:
            # Fenced here too. This write is a safety floor rather than a
            # verdict, so losing to somebody who actually knows the verdict is
            # the outcome to want, and the next pass around re-reads and sees
            # their write.
            applied, _fenced_out = await _write_stamp(
                entity_manager,
                row_id,
                _UNVERIFIED_STAMP,
                expected_revision=revision,
            )
        if applied:
            log.warning(
                "lifecycle_reconciliation_row_unverified",
                operation=operation,
                organization_id=organization_id,
                entity_id=row_id,
                forced=True,
            )
            return "unverified"
        if attempt < RECONCILE_MAX_ATTEMPTS:
            await asyncio.sleep(RECONCILE_RETRY_BASE_SECONDS * attempt)

    log.error(
        "lifecycle_reconciliation_exclusion_failed",
        operation=operation,
        organization_id=organization_id,
        entity_id=row_id,
    )
    msg = f"could not exclude unverified row {row_id}"
    raise ReconcileExclusionError(msg)


async def _reconcile(
    entity_manager: Any,
    *,
    operation: str,
    organization_id: str | None,
    read_verdict: _VerdictReader,
    row_ids: Sequence[str],
) -> ReconcileOutcome:
    ids = [str(row_id) for row_id in dict.fromkeys(row_ids) if row_id]
    if not ids:
        return ReconcileOutcome()

    restamped = 0
    unverified = 0
    cleared = 0
    for row_id in ids:
        result = await _reconcile_row(
            entity_manager,
            row_id,
            operation=operation,
            organization_id=organization_id,
            read_verdict=read_verdict,
        )
        if result == "restamped":
            restamped += 1
        elif result == "unverified":
            unverified += 1
        elif result == "cleared":
            cleared += 1
    return ReconcileOutcome(
        checked=len(ids),
        restamped=restamped,
        unverified=unverified,
        cleared=cleared,
    )


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
) -> ReconcileOutcome:
    """Re-check written rows against the capture they were projected from."""

    from sibyl_core.services.memory import projected_row_lifecycle_stamp

    async def read() -> dict[str, Any]:
        return await projected_row_lifecycle_stamp(
            organization_id=organization_id,
            metadata=metadata,
        )

    return await _reconcile(
        entity_manager,
        operation="capture",
        organization_id=organization_id,
        read_verdict=read,
        row_ids=row_ids,
    )


async def reconcile_with_parent(
    entity_manager: Any,
    *,
    source_id: str,
    row_ids: Sequence[str],
    organization_id: str | None = None,
) -> ReconcileOutcome:
    """Re-check written derived rows against the parent they were derived from."""

    async def read() -> dict[str, Any]:
        get = getattr(entity_manager, "get", None)
        if not callable(get):
            return {}
        try:
            stored = await get(str(source_id))
        except KeyError:
            return {}
        return inherited_lifecycle_metadata(getattr(stored, "metadata", None))

    return await _reconcile(
        entity_manager,
        operation="parent",
        organization_id=organization_id,
        read_verdict=read,
        row_ids=row_ids,
    )


__all__ = [
    "RECONCILE_FENCE_ATTEMPTS",
    "RECONCILE_MAX_ATTEMPTS",
    "RECONCILE_PENDING_KEY",
    "ReconcileExclusionError",
    "ReconcileOutcome",
    "prewrite_capture_stamp",
    "reconcile_with_capture",
    "reconcile_with_parent",
]
