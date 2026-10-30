"""Time-boxed captures that retire themselves through the correction path.

A capture written with an expiry is state rather than knowledge: a live
incident, a deploy freeze, a "don't touch staging" notice. When the expiry
passes, the sweep applies the same `mark_stale` correction an operator runs by
hand, so every retrieval lane, projection and graph row drops it through
machinery that already retires corrected memories. Nothing on the read path
checks expiry.

The stamp lives in capture metadata under `expires_at`, which is server-owned
(`auth/memory_policy.py`): callers state a duration or an instant, this module
validates it against the cap, and only the write path stores it. Once the sweep
has handled a capture it moves the stamp to `expired_at`, which takes the row
out of the due index, so a resolved-early capture is never corrected twice and
a later restore is never undone by the next sweep.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog

from sibyl_core.auth.memory_policy import CAPTURE_EXPIRES_AT_METADATA_KEY
from sibyl_core.errors import RevisionConflictError
from sibyl_core.models.memory_scope import MemoryScope
from sibyl_core.models.reflection import MemoryLifecycleState, memory_lifecycle_from_metadata
from sibyl_core.services import content_client
from sibyl_core.services.content_models import RawMemory

log = structlog.get_logger()

CAPTURE_EXPIRED_AT_METADATA_KEY = "expired_at"
CAPTURE_EXPIRY_REFUSED_METADATA_KEY = "expiry_refused_reason"
CAPTURE_EXPIRY_REASON = "expired"
MIN_CAPTURE_TTL = timedelta(minutes=1)
MAX_CAPTURE_TTL = timedelta(days=30)
EXPIRY_SWEEP_BATCH_SIZE = 200
EXPIRY_INDEX = "idx_raw_captures_expires_at"

_TTL_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86_400, "w": 604_800}
_TTL_PART = re.compile(r"(\d+)([smhdw])")
_TTL_SHAPE = re.compile(r"(?:\d+[smhdw])+")

# Stamps are fixed-width UTC strings, so string order is time order and the
# index range below is a time range. Seconds precision keeps them stable
# across the write and the sweep.
_STAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

# `''` is the least string, and NONE sorts below every string, so the lower
# bound keeps captures without a stamp out of the range the index serves.
DUE_CAPTURES_STATEMENT = (
    f"SELECT uuid, organization_id FROM raw_captures WITH INDEX {EXPIRY_INDEX} "
    f"WHERE metadata.{CAPTURE_EXPIRES_AT_METADATA_KEY} > '' "
    f"AND metadata.{CAPTURE_EXPIRES_AT_METADATA_KEY} <= $now LIMIT $limit;"
)


def parse_capture_ttl(text: str) -> timedelta:
    """Read a duration such as `30m`, `2h`, `1d` or `1h30m`."""
    compact = text.strip().lower().replace(" ", "")
    if not _TTL_SHAPE.fullmatch(compact):
        msg = f"expiry {text!r} is not a duration; use forms like 30m, 2h, 1d or 1h30m"
        raise ValueError(msg)
    seconds = sum(int(amount) * _TTL_UNITS[unit] for amount, unit in _TTL_PART.findall(compact))
    return timedelta(seconds=seconds)


def resolve_capture_expiry(
    *,
    expires_in: str | None = None,
    expires_at: datetime | None = None,
    now: datetime | None = None,
) -> datetime | None:
    """Turn a requested duration or instant into the instant the server stamps."""
    if expires_in is not None and expires_at is not None:
        raise ValueError("give either expires_in or expires_at, not both")
    current = now or datetime.now(UTC)
    if expires_in is not None:
        target = current + parse_capture_ttl(expires_in)
    elif expires_at is not None:
        if expires_at.tzinfo is None:
            raise ValueError("expires_at needs a timezone offset")
        target = expires_at
    else:
        return None
    remaining = target - current
    if remaining < MIN_CAPTURE_TTL:
        raise ValueError("expiry must be at least one minute in the future")
    if remaining > MAX_CAPTURE_TTL:
        raise ValueError("expiry can be at most 30 days out")
    return target.astimezone(UTC).replace(microsecond=0)


def capture_expiry_stamp(value: datetime) -> str:
    return value.astimezone(UTC).strftime(_STAMP_FORMAT)


def capture_expiry_metadata(expires_at: datetime | None) -> dict[str, str]:
    """The metadata the write path adds after its caller-supplied keys are stripped."""
    if expires_at is None:
        return {}
    return {CAPTURE_EXPIRES_AT_METADATA_KEY: capture_expiry_stamp(expires_at)}


@dataclass(frozen=True, slots=True)
class ExpiredCapture:
    memory: RawMemory
    expires_at: str
    corrected: bool
    affected_entity_ids: tuple[str, ...] = ()
    refused_reason: str | None = None


@dataclass(slots=True)
class ExpirySweepResult:
    expired: list[ExpiredCapture] = field(default_factory=list)
    already_retired: int = 0
    refused: int = 0
    conflicts: int = 0
    failed: int = 0


def _capture_authority(memory: RawMemory) -> dict[str, Any]:
    """Grants scoped to the capture's own audience, acting as its author.

    The author chose the expiry when the write was authorized, so the sweep
    retires the capture on their behalf. It holds exactly the scope the
    capture lives in and nothing else: a projection outside that audience is
    refused by the correction path the same way an operator's would be.
    """
    key = memory.scope_key
    projects: tuple[str, ...] = (key,) if memory.memory_scope is MemoryScope.PROJECT and key else ()
    teams: tuple[str, ...] = (key,) if memory.memory_scope is MemoryScope.TEAM and key else ()
    delegations: tuple[str, ...] = (
        (key,) if memory.memory_scope is MemoryScope.DELEGATED and key else ()
    )
    return {
        "accessible_projects": projects,
        "writable_projects": projects,
        "accessible_teams": teams,
        "accessible_delegations": delegations,
    }


async def due_capture_ids(*, now: datetime, limit: int) -> list[tuple[str, str]]:
    async with content_client.surreal_content_client() as client:
        rows = await content_client.select_many(
            client,
            DUE_CAPTURES_STATEMENT,
            now=capture_expiry_stamp(now),
            limit=limit,
        )
    return [
        (str(row["organization_id"]), str(row["uuid"]))
        for row in rows
        if row.get("uuid") and row.get("organization_id")
    ]


async def _retire_expiry_stamp(
    memory: RawMemory,
    *,
    refused_reason: str | None = None,
) -> RawMemory:
    from sibyl_core.services.surreal_content import save_raw_memory

    metadata = dict(memory.metadata)
    # The save merges metadata into the stored row, so dropping the key would
    # leave the stored stamp behind; an explicit null clears it.
    metadata[CAPTURE_EXPIRED_AT_METADATA_KEY] = metadata.get(CAPTURE_EXPIRES_AT_METADATA_KEY)
    metadata[CAPTURE_EXPIRES_AT_METADATA_KEY] = None
    if refused_reason:
        metadata[CAPTURE_EXPIRY_REFUSED_METADATA_KEY] = refused_reason
    return await save_raw_memory(
        replace(memory, metadata=metadata),
        expected_revision=memory.observed_revision,
    )


async def expire_capture(memory: RawMemory, *, now: datetime) -> ExpiredCapture | None:
    """Retire one due capture, or only clear its stamp when it is already retired."""
    from sibyl_core.services.memory_correction import apply_memory_correction

    stamp = memory.metadata.get(CAPTURE_EXPIRES_AT_METADATA_KEY)
    if not isinstance(stamp, str) or stamp > capture_expiry_stamp(now):
        return None
    lifecycle = memory_lifecycle_from_metadata(
        memory.metadata,
        source_id=memory.id,
        review_state=memory.review_state,
    )
    if str(lifecycle.state) != MemoryLifecycleState.ACTIVE.value:
        # Resolved early (`sibyl correct --action stale`) or otherwise retired:
        # the correction already ran, so only the stamp leaves the due range.
        await _retire_expiry_stamp(memory)
        return ExpiredCapture(memory=memory, expires_at=stamp, corrected=False)
    result = await apply_memory_correction(
        organization_id=memory.organization_id,
        source_id=memory.id,
        principal_id=memory.principal_id,
        action="mark_stale",
        reason=CAPTURE_EXPIRY_REASON,
        expected_revision=memory.observed_revision,
        **_capture_authority(memory),
    )
    if not result.applied or result.updated_memory is None:
        # A refusal is not transient, and leaving the stamp would keep the row
        # at the head of every sweep. Say so and record why on the capture.
        reason = result.preview.reason
        log.warning("capture_expiry_refused", source_id=memory.id, reason=reason)
        await _retire_expiry_stamp(memory, refused_reason=reason)
        return ExpiredCapture(
            memory=memory, expires_at=stamp, corrected=False, refused_reason=reason
        )
    saved = await _retire_expiry_stamp(result.updated_memory)
    return ExpiredCapture(
        memory=saved,
        expires_at=stamp,
        corrected=True,
        affected_entity_ids=tuple(result.affected_entity_ids),
    )


async def expire_due_captures(
    *,
    now: datetime | None = None,
    limit: int = EXPIRY_SWEEP_BATCH_SIZE,
) -> ExpirySweepResult:
    """Retire every capture whose expiry has passed, up to one batch per call."""
    from sibyl_core.services.surreal_content import get_raw_memory

    current = now or datetime.now(UTC)
    result = ExpirySweepResult()
    for organization_id, memory_id in await due_capture_ids(now=current, limit=limit):
        try:
            memory = await get_raw_memory(organization_id=organization_id, memory_id=memory_id)
            if memory is None:
                continue
            expired = await expire_capture(memory, now=current)
        except RevisionConflictError:
            # Written while the sweep held it; the next tick sees the new revision.
            result.conflicts += 1
            continue
        except Exception as exc:
            log.warning(
                "capture_expiry_failed",
                source_id=memory_id,
                error_type=type(exc).__name__,
            )
            result.failed += 1
            continue
        if expired is None:
            continue
        if expired.refused_reason:
            result.refused += 1
        elif not expired.corrected:
            result.already_retired += 1
        result.expired.append(expired)
    return result


__all__ = [
    "CAPTURE_EXPIRED_AT_METADATA_KEY",
    "CAPTURE_EXPIRES_AT_METADATA_KEY",
    "CAPTURE_EXPIRY_REASON",
    "DUE_CAPTURES_STATEMENT",
    "EXPIRY_INDEX",
    "MAX_CAPTURE_TTL",
    "MIN_CAPTURE_TTL",
    "ExpiredCapture",
    "ExpirySweepResult",
    "capture_expiry_metadata",
    "capture_expiry_stamp",
    "expire_capture",
    "expire_due_captures",
    "parse_capture_ttl",
    "resolve_capture_expiry",
]
