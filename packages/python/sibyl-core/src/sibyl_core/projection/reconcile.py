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
from functools import partial
from typing import Any

import structlog

from sibyl_core.memory_pipeline.lifecycle import (
    RECONCILE_PENDING_KEY,
    graph_metadata_recallable,
)
from sibyl_core.memory_pipeline.source_lifecycle import (
    CORRECTION_BLOCKERS_KEY,
    SOURCE_BINDINGS_KEY,
    SOURCE_VALIDATION_PENDING_KEY,
    SourceCorrection,
    merge_source_correction,
)
from sibyl_core.projection.inheritance import inherited_lifecycle_metadata
from sibyl_core.projection.pending import PENDING_KEYS, inherited_pending, pending_patch

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
_UNVERIFIED_STAMP: dict[str, Any] = {RECONCILE_PENDING_KEY: True}

_VerdictReader = Callable[[Mapping[str, Any]], Coroutine[Any, Any, dict[str, Any]]]


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
    revision = getattr(row, "observed_revision", None)
    return revision if type(revision) is int and revision > 0 else None


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
                replace_metadata_keys=tuple(key for key in PENDING_KEYS if key in patch),
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
    """Merge observed source clocks without changing authored exclusions or bindings.

    A post-write read observes source state, not which source text the row used.
    Existing bindings therefore remain immutable. Root clocks merge by revision,
    including when the row already carries a later correction than this read.
    """

    merged = dict(current)
    for key, value in verdict.items():
        if key == SOURCE_BINDINGS_KEY:
            continue
        if key == CORRECTION_BLOCKERS_KEY:
            if not isinstance(value, Mapping):
                raise ValueError("correction clocks must be a mapping")
            for root_id, entry in value.items():
                if not isinstance(entry, Mapping):
                    raise ValueError("source correction clock must be a mapping")
                revision = entry.get("revision")
                blocking = entry.get("blocking")
                if (
                    not isinstance(root_id, str)
                    or not isinstance(revision, int)
                    or isinstance(revision, bool)
                    or not isinstance(blocking, bool)
                ):
                    raise ValueError("source correction clock has invalid fields")
                merged = merge_source_correction(
                    merged,
                    SourceCorrection(root_id=root_id, revision=revision, blocking=blocking),
                )
            continue
        if (
            key not in {SOURCE_VALIDATION_PENDING_KEY, RECONCILE_PENDING_KEY}
            and key in current
            and not graph_metadata_recallable({key: current[key]})
        ):
            # Flat legacy/parent verdicts cannot establish ownership of an
            # existing exclusion. A clear verdict must not erase an own hide.
            continue
        merged[key] = value
    return {key: value for key, value in merged.items() if current.get(key) != value}


async def _reconcile_row(
    entity_manager: Any,
    row_id: str,
    *,
    operation: str,
    organization_id: str | None,
    read_verdict: _VerdictReader,
    authority: str,
) -> str:
    """Bring one written row into agreement with its verdict, under the fence."""

    for _attempt in range(RECONCILE_FENCE_ATTEMPTS):
        row, row_read = await _read_row(entity_manager, row_id)
        if not row_read:
            # The row could not be read, so there is no revision to fence on and
            # no safe write from here. An unfenced write at this point is how a
            # run of failed reads ends up overwriting a correction that landed
            # while they were failing.
            return await _force_exclusion(
                entity_manager, row_id, operation, organization_id, authority
            )
        if row is None:
            # The row is gone. Nothing to reconcile and nothing to fail over.
            return "missing"
        metadata = _row_metadata(row)
        revision = _row_revision(row)

        verdict, verified = await _with_retries(operation, partial(read_verdict, metadata))
        if not verified:
            if revision is None:
                # Every write is fenced, so a row that cannot supply a revision
                # cannot be written from here.
                return await _force_exclusion(
                    entity_manager, row_id, operation, organization_id, authority
                )
            applied, fenced_out = await _write_stamp(
                entity_manager,
                row_id,
                pending_patch(metadata, _UNVERIFIED_STAMP, authority=authority),
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
        try:
            patch = _desired_patch(
                {key: value for key, value in current.items() if key not in PENDING_KEYS},
                metadata,
            )
            patch.update(pending_patch(metadata, current, authority=authority))
        except (TypeError, ValueError) as exc:
            log.warning(
                "lifecycle_reconciliation_invalid_verdict",
                operation=operation,
                entity_id=row_id,
                error_type=type(exc).__name__,
            )
            return await _force_exclusion(
                entity_manager, row_id, operation, organization_id, authority
            )
        if not patch:
            return "unchanged"
        if revision is None:
            # The row needs a write and cannot be fenced, which is the one
            # combination this pass refuses to resolve by writing anyway.
            return await _force_exclusion(
                entity_manager, row_id, operation, organization_id, authority
            )

        applied, fenced_out = await _write_stamp(
            entity_manager,
            row_id,
            patch,
            expected_revision=revision,
        )
        if applied:
            remaining = {**metadata, **patch}
            unverified = any(remaining.get(key) for key in PENDING_KEYS)
            cleared_marker = not unverified and any(
                metadata.get(key) and key in patch for key in PENDING_KEYS
            )
            log.info(
                "lifecycle_reconciliation_restamped",
                operation=operation,
                organization_id=organization_id,
                entity_id=row_id,
                lifecycle_state=current.get("lifecycle_state"),
                cleared_marker=cleared_marker,
                unverified=unverified,
            )
            if unverified:
                return "unverified"
            return "cleared" if cleared_marker else "restamped"
        if fenced_out:
            # Somebody wrote between this pass's read and its write. Re-read and
            # agree with them rather than overwriting a newer verdict.
            continue
        break

    # Every attempt lost the fence or failed to apply. The row's verdict is
    # unknown to this pass, so it is excluded rather than left servable.
    return await _force_exclusion(entity_manager, row_id, operation, organization_id, authority)


async def _force_exclusion(
    entity_manager: Any,
    row_id: str,
    operation: str,
    organization_id: str | None,
    authority: str,
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
                pending_patch(metadata, _UNVERIFIED_STAMP, authority=authority),
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
    authority: str,
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
            authority=authority,
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


def _capture_authority(metadata: Mapping[str, Any] | None) -> str:
    fields = metadata or {}
    memory_id = fields.get("raw_memory_id")
    if isinstance(memory_id, str) and memory_id.strip():
        return f"capture:{memory_id.strip()}"
    source_id = fields.get("raw_source_id")
    if isinstance(source_id, str) and source_id.strip():
        return f"capture-source:{source_id.strip()}"
    return "capture:unbound"


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
    verdict = dict(stamp or {})
    if not verified or (metadata or {}).get("raw_memory_id"):
        # The insert is not source-validated until its post-write check lands.
        # A row read failure must leave durable exclusion, not a servable row.
        verdict[RECONCILE_PENDING_KEY] = True
    authority = _capture_authority(metadata)
    return {
        **{key: value for key, value in verdict.items() if key not in PENDING_KEYS},
        **inherited_pending(verdict, authority),
    }, verified


async def reconcile_with_capture(
    entity_manager: Any,
    *,
    organization_id: str,
    metadata: Mapping[str, Any] | None,
    row_ids: Sequence[str],
    expected_signature: str | None = None,
) -> ReconcileOutcome:
    """Re-check written rows against the capture they were projected from."""

    from sibyl_core.services.memory import projected_row_lifecycle_stamp

    authority = _capture_authority(metadata)
    if expected_signature is not None:
        if (
            not authority.startswith("capture:")
            or authority == "capture:unbound"
            or len(expected_signature) != 64
            or any(char not in "0123456789abcdef" for char in expected_signature)
        ):
            raise ValueError("signature reconciliation requires a capture ID and SHA256 digest")
        authority = (
            f"capture-signature-v1:{authority.removeprefix('capture:')}:{expected_signature}"
        )

    async def read(row_metadata: Mapping[str, Any]) -> dict[str, Any]:
        return await projected_row_lifecycle_stamp(
            organization_id=organization_id,
            metadata={
                **(metadata or {}),
                SOURCE_BINDINGS_KEY: row_metadata.get(SOURCE_BINDINGS_KEY, {}),
            },
            **(
                {"expected_signature": expected_signature} if expected_signature is not None else {}
            ),
        )

    return await _reconcile(
        entity_manager,
        operation="capture",
        authority=authority,
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

    async def read(_row_metadata: Mapping[str, Any]) -> dict[str, Any]:
        get = getattr(entity_manager, "get", None)
        if not callable(get):
            return dict(_UNVERIFIED_STAMP)
        try:
            stored = await get(str(source_id))
        except KeyError:
            stored = None
        if stored is None:
            # Absence supplies no lifecycle verdict. Keep this parent's check
            # pending so a later read can recover it without retiring the child.
            return dict(_UNVERIFIED_STAMP)
        return inherited_lifecycle_metadata(getattr(stored, "metadata", None))

    return await _reconcile(
        entity_manager,
        operation="parent",
        authority=f"parent:{source_id}",
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
