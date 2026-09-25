"""Re-embed stored vectors whose recorded model is not the configured one.

A plane is a set of tables whose vectors share one configured model: the
graph plane holds entity and relationship vectors, the document chunk plane
holds chunk vectors. Every vector carries an ``embedding_metadata`` stamp, and
the sweep's single rule is that a row whose stamp differs from what the
configured provider writes today, or whose stamp survived its vector, gets a
new vector from the configured provider.

Rows written before Sibyl stamped vectors carry no stamp. The first pass over
a plane classifies them once and persists the verdict (see
``LegacyVectorDecision``), then stamps them either as the configured model or
as unverified, after which they follow the ordinary rule.

Passes are bounded by a wall-clock budget, hold a lease so overlapping
lifecycle ticks never double-embed, persist their walk cursor so a restart
resumes rather than rescans, and record a receipt that status surfaces read.
A plane that finished a full pass for the configured model skips its walk
until the verify interval lapses or an import or dimension rebuild reopens it.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import random
import socket
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol
from uuid import uuid4

import structlog

from sibyl_core.backends.surreal.records import normalize_records
from sibyl_core.backends.surreal.schema_embedding_states import REOPEN_EMBEDDING_STATES
from sibyl_core.config import settings
from sibyl_core.embeddings.provenance import (
    UNVERIFIED_EMBEDDING_PROVIDER,
    UNVERIFIED_ORIGIN_LEGACY,
    UNVERIFIED_ORIGIN_OPERATOR,
    is_rate_limit_error,
    unverified_embedding_metadata,
)
from sibyl_core.projection.repair import LifecycleRepairResult

log = structlog.get_logger()

SWEEP_COMPLETED = "completed"
SWEEP_PARTIAL = "partial"
SWEEP_CURRENT = "current"
SWEEP_BUSY = "busy"
SWEEP_PROVIDER_FAILING = "provider_failing"
SWEEP_SKIPPED_NO_PROVIDER = "skipped_no_provider"
SWEEP_SKIPPED_DIMENSION_MISMATCH = "skipped_dimension_mismatch"

_LEASE_MARGIN_SECONDS = 120.0
_BACKOFF_BASE_SECONDS = 1.0
_BACKOFF_CAP_SECONDS = 20.0
_CONSECUTIVE_FAILURE_LIMIT = 3

type SweepExecute = Callable[..., Awaitable[object]]
type SweepRow = dict[str, Any]
type EmbeddingStamp = dict[str, Any]


class LegacyVectorDecision(StrEnum):
    """How a plane's vectors from before stamping are classified, once."""

    ADOPT = "adopt"
    REEMBED = "reembed"
    NONE = "none"


class LegacyVectorBasis(StrEnum):
    NO_LEGACY_ROWS = "no_legacy_rows"
    PRIOR_STAMPS_MATCH = "prior_stamps_match"
    PRIOR_STAMPS_DIFFER = "prior_stamps_differ"
    NO_PRIOR_EVIDENCE = "no_prior_evidence"
    OPERATOR_ADOPT = "operator_adopt"
    OPERATOR_REEMBED = "operator_reembed"


@dataclass(frozen=True, slots=True)
class LegacyEvidence:
    """What the plane's stamped rows say about the model that preceded stamping."""

    differs: bool
    matches: bool


@dataclass(frozen=True, slots=True)
class EmbeddingSweepResult(LifecycleRepairResult):
    """One pass over one plane.

    ``recovered`` counts re-embedded rows and ``pending`` the rows still
    awaiting a vector from the configured model when the pass ended.
    """

    plane: str = ""
    status: str = SWEEP_COMPLETED
    adopted: int = 0
    skipped: int = 0
    legacy_decision: str | None = None
    provider_dimensions: int | None = None
    schema_dimensions: int | None = None
    elapsed_ms: float = 0.0

    @property
    def reembedded(self) -> int:
        return self.recovered

    def receipt(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class SweepTable:
    """One table whose vectors a plane sweeps.

    ``projection`` lists the columns the embed text and the write fence need;
    ``fence`` compares those columns on the stored row against the values the
    vector was computed from, reached through ``$rows_by_uuid[uuid]``.
    """

    name: str
    scope_field: str
    vector_field: str
    metadata_path: str
    projection: str
    fence: str
    dimensions: int

    def _candidate_predicate(self) -> str:
        return (
            f"{self.metadata_path} != NONE "
            f"AND ({self.vector_field} = NONE OR {self.metadata_path} != $stamp)"
        )

    def walk_query(self) -> str:
        return (
            f"SELECT uuid, {self.projection} FROM {self.name} "
            f"WHERE {self.scope_field} = $scope AND uuid > $cursor "
            f"AND {self._candidate_predicate()} "
            "ORDER BY uuid ASC LIMIT $limit;"
        )

    def pending_query(self) -> str:
        return (
            f"SELECT count() AS count FROM {self.name} "
            f"WHERE {self.scope_field} = $scope AND ("
            f"({self.vector_field} != NONE AND {self.metadata_path} = NONE) "
            f"OR ({self._candidate_predicate()})) GROUP ALL;"
        )

    def legacy_probe_query(self) -> str:
        return (
            f"SELECT uuid FROM {self.name} "
            f"WHERE {self.scope_field} = $scope AND {self.vector_field} != NONE "
            f"AND {self.metadata_path} = NONE LIMIT 1;"
        )

    def stamp_probe_query(self, *, matching: bool) -> str:
        comparison = "=" if matching else "!="
        return (
            f"SELECT uuid FROM {self.name} "
            f"WHERE {self.scope_field} = $scope AND {self.metadata_path} != NONE "
            f"AND {self.metadata_path}.provider != $unverified "
            f"AND {self.metadata_path} {comparison} $stamp LIMIT 1;"
        )

    def adopt_query(self) -> str:
        return (
            f"UPDATE (SELECT VALUE id FROM {self.name} "
            f"WHERE {self.scope_field} = $scope AND {self.vector_field} != NONE "
            f"AND {self.metadata_path} = NONE LIMIT $limit) "
            f"SET {self.metadata_path} = $legacy RETURN uuid;"
        )

    def unverify_query(self) -> str:
        return (
            f"UPDATE (SELECT VALUE id FROM {self.name} "
            f"WHERE {self.scope_field} = $scope AND ({self.vector_field} != NONE "
            f"OR {self.metadata_path} != NONE) AND ({self.metadata_path} = NONE "
            f"OR {self.metadata_path}.provider != $unverified) LIMIT $limit) "
            f"SET {self.metadata_path} = $marker RETURN uuid;"
        )

    def write_query(self) -> str:
        # Only the vector and its provenance move: no revision or updated_at
        # bump, because a sweep is not an edit and recency ranks on updated_at.
        return (
            f"UPDATE (SELECT VALUE id FROM {self.name} "
            f"WHERE {self.scope_field} = $scope AND uuid IN $uuids) SET "
            f"{self.vector_field} = <array<float, {self.dimensions}>>"
            "$rows_by_uuid[uuid].embedding, "
            f"{self.metadata_path} = $rows_by_uuid[uuid].embedding_metadata "
            f"WHERE {self.fence} AND ({self.vector_field} = NONE "
            f"OR {self.metadata_path} != $rows_by_uuid[uuid].embedding_metadata) "
            "RETURN uuid;"
        )


class EmbedRows(Protocol):
    """Embed one batch of walked rows with the configured provider.

    Returns one vector per row and the stamp the provider writes, resolved in
    the same call so a configuration change mid-pass cannot mislabel a vector.
    """

    async def __call__(
        self, table: SweepTable, rows: Sequence[SweepRow], /
    ) -> tuple[list[list[float]], EmbeddingStamp]: ...


@dataclass(frozen=True, slots=True)
class SweepPlane:
    name: str
    organization_id: str
    execute: SweepExecute
    tables: tuple[SweepTable, ...]
    stamp: EmbeddingStamp
    embed: EmbedRows
    provider_dimensions: int
    schema_dimensions: int
    evidence: Callable[[], Awaitable[LegacyEvidence]]

    @property
    def state_key(self) -> str:
        return embedding_state_key(self.organization_id, self.name)


def embedding_state_key(organization_id: str, plane: str) -> str:
    """Record id of one organization's state row for one plane."""
    digest = hashlib.sha256(f"{organization_id}\x1f{plane}".encode()).hexdigest()
    return f"embedding_states:s{digest[:40]}"


@dataclass(slots=True)
class _Counts:
    checked: int = 0
    recovered: int = 0
    failed: int = 0
    skipped: int = 0
    adopted: int = 0
    consecutive_failures: int = 0


class _AdaptiveLimiter:
    """Concurrency that halves on throttling and climbs back one step at a time."""

    def __init__(self, maximum: int) -> None:
        self._maximum = max(1, maximum)
        self._limit = self._maximum
        self._active = 0
        self._successes = 0
        self._condition = asyncio.Condition()

    @property
    def limit(self) -> int:
        return self._limit

    async def __aenter__(self) -> None:
        async with self._condition:
            await self._condition.wait_for(lambda: self._active < self._limit)
            self._active += 1

    async def __aexit__(self, *_exc: object) -> None:
        async with self._condition:
            self._active -= 1
            self._condition.notify_all()

    def throttled(self) -> None:
        self._limit = max(1, self._limit // 2)
        self._successes = 0

    async def succeeded(self) -> None:
        self._successes += 1
        if self._limit < self._maximum and self._successes >= self._limit:
            self._limit += 1
            self._successes = 0
            async with self._condition:
                self._condition.notify_all()


class _BudgetExhaustedError(Exception):
    """A throttled batch could not be retried inside the pass budget."""


def configured_legacy_vector_policy() -> str:
    raw = os.getenv("SIBYL_EMBEDDING_LEGACY_VECTORS", "").strip().lower()
    return raw or settings.embedding_legacy_vectors


def decide_legacy_vectors(
    *, legacy_rows: bool, evidence: LegacyEvidence | None, policy: str
) -> tuple[LegacyVectorDecision, LegacyVectorBasis]:
    """Classify a plane's unstamped vectors.

    An operator policy wins. Otherwise the plane's own stamped rows decide:
    any stamp from another model proves the deployment has used one, so the
    unstamped vectors cannot be assumed current and are replaced. When every
    stamp matches, or nothing is stamped yet, the unstamped vectors are what
    the unchanged configuration produced and are adopted in place.
    """
    if not legacy_rows:
        return LegacyVectorDecision.NONE, LegacyVectorBasis.NO_LEGACY_ROWS
    if policy == "adopt":
        return LegacyVectorDecision.ADOPT, LegacyVectorBasis.OPERATOR_ADOPT
    if policy == "reembed":
        return LegacyVectorDecision.REEMBED, LegacyVectorBasis.OPERATOR_REEMBED
    if evidence is not None and evidence.differs:
        return LegacyVectorDecision.REEMBED, LegacyVectorBasis.PRIOR_STAMPS_DIFFER
    if evidence is not None and evidence.matches:
        return LegacyVectorDecision.ADOPT, LegacyVectorBasis.PRIOR_STAMPS_MATCH
    return LegacyVectorDecision.ADOPT, LegacyVectorBasis.NO_PRIOR_EVIDENCE


async def ensure_legacy_decision(plane: SweepPlane) -> dict[str, Any]:
    """Persist the plane's legacy verdict if no pass has recorded one yet.

    Lifecycle repair calls this before any repair in the same tick can
    rewrite the stamps the verdict reads. The first writer wins, so a racing
    pass reads the same verdict back.
    """
    state = await _ensure_state(plane)
    if state.get("legacy_decision"):
        return state
    legacy_rows = False
    for table in plane.tables:
        if await _rows(plane, table.legacy_probe_query()):
            legacy_rows = True
            break
    evidence = await plane.evidence() if legacy_rows else None
    decision, basis = decide_legacy_vectors(
        legacy_rows=legacy_rows,
        evidence=evidence,
        policy=configured_legacy_vector_policy(),
    )
    legacy = (
        dict(plane.stamp)
        if decision is LegacyVectorDecision.ADOPT
        else unverified_embedding_metadata(UNVERIFIED_ORIGIN_LEGACY)
    )
    rows = normalize_records(
        await plane.execute(
            "UPDATE type::record($key) SET legacy_decision = $decision, "
            "legacy_basis = $basis, legacy_metadata = $legacy, decided_at = time::now(), "
            "updated_at = time::now() WHERE legacy_decision = NONE RETURN AFTER;",
            key=plane.state_key,
            decision=decision.value,
            basis=basis.value,
            legacy=legacy,
        )
    )
    if rows:
        log_method = log.warning if basis is LegacyVectorBasis.NO_PRIOR_EVIDENCE else log.info
        log_method(
            "embedding_legacy_vectors_decided",
            organization_id=plane.organization_id,
            plane=plane.name,
            decision=decision.value,
            basis=basis.value,
            provider=plane.stamp.get("provider"),
            model=plane.stamp.get("model"),
            dimensions=plane.stamp.get("dimensions"),
        )
        return rows[0]
    return await _read_state(plane)


async def run_embedding_sweep(
    plane: SweepPlane,
    *,
    budget_seconds: float | None = None,
    page_size: int | None = None,
    batch_size: int | None = None,
    concurrency: int | None = None,
    verify_interval_seconds: float | None = None,
) -> EmbeddingSweepResult:
    """Adopt or re-embed one plane's vectors within a wall-clock budget."""
    started = time.monotonic()
    budget = settings.embedding_sweep_budget_seconds if budget_seconds is None else budget_seconds
    deadline = started + max(budget, 0.0)
    batch = max(1, batch_size or settings.embedding_sweep_batch_size)
    slots = max(1, concurrency or settings.embedding_sweep_concurrency)
    page = max(page_size or settings.embedding_sweep_page_size, batch * slots)
    verify_interval = (
        settings.embedding_sweep_verify_interval_seconds
        if verify_interval_seconds is None
        else verify_interval_seconds
    )

    def result(**fields: Any) -> EmbeddingSweepResult:
        return EmbeddingSweepResult(
            plane=plane.name,
            provider_dimensions=plane.provider_dimensions,
            schema_dimensions=plane.schema_dimensions,
            **fields,
        )

    if plane.provider_dimensions != plane.schema_dimensions:
        log.warning(
            "embedding_sweep_dimension_mismatch",
            organization_id=plane.organization_id,
            plane=plane.name,
            provider=plane.stamp.get("provider"),
            model=plane.stamp.get("model"),
            provider_dimensions=plane.provider_dimensions,
            schema_dimensions=plane.schema_dimensions,
        )
        return result(status=SWEEP_SKIPPED_DIMENSION_MISMATCH)

    owner = f"{socket.gethostname()}:{os.getpid()}:{uuid4().hex[:12]}"
    state = await ensure_legacy_decision(plane)
    decision = state.get("legacy_decision")
    legacy_decision = str(decision) if decision else None
    if _plane_current(state, plane.stamp, verify_interval):
        return result(status=SWEEP_CURRENT, legacy_decision=legacy_decision)
    if not await _acquire_lease(plane, owner=owner, budget=budget):
        return result(status=SWEEP_BUSY, legacy_decision=legacy_decision)

    counts = _Counts()
    limiter = _AdaptiveLimiter(slots)
    status = SWEEP_PARTIAL
    cursors = _cursor_map(state.get("cursors"))
    generation = state.get("generation") or 0
    complete = False
    try:
        legacy = state.get("legacy_metadata")
        adopted_all = True
        if isinstance(legacy, Mapping):
            adopted_all = await _stamp_legacy_rows(
                plane, dict(legacy), counts=counts, deadline=deadline, page=page
            )
        finished: set[str] = set()
        clean: set[str] = set()
        walkers = {
            table.name: _TableWalk(table=table, cursor=cursors.get(table.name, ""))
            for table in plane.tables
        }
        while adopted_all and len(finished) < len(walkers) and time.monotonic() < deadline:
            for walk in walkers.values():
                if walk.table.name in finished or time.monotonic() >= deadline:
                    continue
                table_done = await _advance(
                    plane,
                    walk,
                    counts=counts,
                    limiter=limiter,
                    deadline=deadline,
                    page=page,
                    batch=batch,
                )
                cursors[walk.table.name] = walk.cursor
                if table_done is not None:
                    finished.add(walk.table.name)
                    if table_done:
                        clean.add(walk.table.name)
                if counts.consecutive_failures >= _CONSECUTIVE_FAILURE_LIMIT:
                    status = SWEEP_PROVIDER_FAILING
                    break
            if status == SWEEP_PROVIDER_FAILING:
                break
            await _save_cursors(plane, owner=owner, cursors=cursors)
        complete = adopted_all and clean == set(walkers)
        if complete:
            status = SWEEP_COMPLETED
        pending = 0 if complete else await _pending(plane)
    except BaseException:
        await _release(
            plane,
            owner=owner,
            cursors=cursors,
            receipt=None,
            complete=None,
            generation=generation,
        )
        raise
    outcome = result(
        legacy_decision=legacy_decision,
        status=status,
        checked=counts.checked,
        recovered=counts.recovered,
        pending=pending,
        failed=counts.failed,
        adopted=counts.adopted,
        skipped=counts.skipped,
        elapsed_ms=round((time.monotonic() - started) * 1000, 2),
    )
    await _release(
        plane,
        owner=owner,
        cursors=cursors,
        receipt=outcome.receipt(),
        complete=dict(plane.stamp) if complete else None,
        generation=generation,
    )
    log.info(
        "embedding_sweep_pass",
        organization_id=plane.organization_id,
        plane=plane.name,
        status=status,
        checked=counts.checked,
        reembedded=counts.recovered,
        adopted=counts.adopted,
        pending=pending,
        skipped=counts.skipped,
        failed=counts.failed,
        concurrency=limiter.limit,
        elapsed_ms=outcome.elapsed_ms,
    )
    return outcome


@dataclass(slots=True)
class _TableWalk:
    table: SweepTable
    cursor: str
    started_at_beginning: bool = field(init=False)
    wrapped: bool = False

    def __post_init__(self) -> None:
        self.started_at_beginning = self.cursor == ""


async def _advance(
    plane: SweepPlane,
    walk: _TableWalk,
    *,
    counts: _Counts,
    limiter: _AdaptiveLimiter,
    deadline: float,
    page: int,
    batch: int,
) -> bool | None:
    """Process one page. Returns None to continue, else whether the table is clean.

    A table is clean only when a probe from the start of the key space finds
    nothing left, so a pass that resumed mid-table never declares victory for
    rows behind its cursor.
    """
    rows = await _rows(
        plane,
        walk.table.walk_query(),
        cursor=walk.cursor,
        limit=page,
    )
    if rows:
        walk.cursor = str(rows[-1]["uuid"])
        await _process_page(
            plane, walk.table, rows, counts=counts, limiter=limiter, deadline=deadline, batch=batch
        )
    if len(rows) >= page:
        return None
    if not (walk.started_at_beginning or walk.wrapped):
        walk.cursor = ""
        walk.wrapped = True
        return None
    walk.cursor = ""
    leftover = await _rows(plane, walk.table.walk_query(), cursor="", limit=1)
    return not leftover


async def _process_page(
    plane: SweepPlane,
    table: SweepTable,
    rows: Sequence[SweepRow],
    *,
    counts: _Counts,
    limiter: _AdaptiveLimiter,
    deadline: float,
    batch: int,
) -> None:
    counts.checked += len(rows)
    batches = [list(rows[start : start + batch]) for start in range(0, len(rows), batch)]
    outcomes = await asyncio.gather(
        *(
            _embed_and_write(plane, table, chunk, limiter=limiter, deadline=deadline)
            for chunk in batches
        ),
        return_exceptions=True,
    )
    for chunk, outcome in zip(batches, outcomes, strict=True):
        if isinstance(outcome, _BudgetExhaustedError):
            counts.skipped += len(chunk)
            continue
        if isinstance(outcome, BaseException):
            if isinstance(outcome, asyncio.CancelledError):
                raise outcome
            counts.failed += len(chunk)
            counts.consecutive_failures += 1
            log.warning(
                "embedding_sweep_batch_failed",
                organization_id=plane.organization_id,
                plane=plane.name,
                table=table.name,
                rows=len(chunk),
                error_type=type(outcome).__name__,
            )
            continue
        counts.consecutive_failures = 0
        written = outcome
        counts.recovered += written
        counts.skipped += len(chunk) - written


async def _embed_and_write(
    plane: SweepPlane,
    table: SweepTable,
    rows: list[SweepRow],
    *,
    limiter: _AdaptiveLimiter,
    deadline: float,
) -> int:
    attempt = 0
    while True:
        async with limiter:
            try:
                vectors, stamp = await plane.embed(table, rows)
            except Exception as exc:
                if not is_rate_limit_error(exc):
                    raise
                limiter.throttled()
                log.info(
                    "embedding_sweep_throttled",
                    organization_id=plane.organization_id,
                    plane=plane.name,
                    table=table.name,
                    concurrency=limiter.limit,
                    error_type=type(exc).__name__,
                )
            else:
                await limiter.succeeded()
                break
        attempt += 1
        delay = min(_BACKOFF_CAP_SECONDS, _BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)))
        delay *= 0.5 + random.random()
        if time.monotonic() + delay >= deadline:
            raise _BudgetExhaustedError
        await asyncio.sleep(delay)
    if len(vectors) != len(rows):
        raise ValueError(f"embedding provider returned {len(vectors)} vectors for {len(rows)} rows")
    if stamp.get("dimensions") != table.dimensions or any(
        len(vector) != table.dimensions for vector in vectors
    ):
        raise ValueError(
            f"embedding provider vectors do not fit the {table.dimensions}-dimension field"
        )
    rows_by_uuid = {
        str(row["uuid"]): {
            **row,
            "embedding": [float(value) for value in vector],
            "embedding_metadata": dict(stamp),
        }
        for row, vector in zip(rows, vectors, strict=True)
    }
    written = await _rows(
        plane,
        table.write_query(),
        uuids=list(rows_by_uuid),
        rows_by_uuid=rows_by_uuid,
    )
    return len(written)


async def _stamp_legacy_rows(
    plane: SweepPlane,
    legacy: EmbeddingStamp,
    *,
    counts: _Counts,
    deadline: float,
    page: int,
) -> bool:
    """Stamp unstamped vectors with the plane's persisted legacy verdict.

    Metadata-only writes, so adoption costs no provider calls. Returns
    whether every table ran out of unstamped vectors inside the budget.
    """
    for table in plane.tables:
        while True:
            if time.monotonic() >= deadline:
                return False
            stamped = await _rows(plane, table.adopt_query(), legacy=legacy, limit=page)
            if legacy.get("provider") != UNVERIFIED_EMBEDDING_PROVIDER:
                counts.adopted += len(stamped)
            if len(stamped) < page:
                break
    return True


async def _pending(plane: SweepPlane) -> int:
    total = 0
    for table in plane.tables:
        rows = await _rows(plane, table.pending_query())
        total += int(rows[0].get("count") or 0) if rows else 0
    return total


def _plane_current(state: Mapping[str, Any], stamp: EmbeddingStamp, interval: float) -> bool:
    if interval <= 0 or state.get("complete_metadata") != stamp:
        return False
    completed = state.get("complete_age_seconds")
    return isinstance(completed, int | float) and completed < interval


def _cursor_map(value: object) -> dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    return {str(key): str(item) for key, item in value.items() if isinstance(item, str)}


async def _rows(plane: SweepPlane, query: str, **params: object) -> list[dict[str, Any]]:
    return normalize_records(
        await plane.execute(
            query,
            scope=plane.organization_id,
            stamp=plane.stamp,
            unverified=UNVERIFIED_EMBEDDING_PROVIDER,
            **params,
        )
    )


_STATE_PROJECTION = (
    "*, IF complete_at = NONE THEN NONE "
    "ELSE duration::secs(time::now() - complete_at) END AS complete_age_seconds"
)


async def _ensure_state(plane: SweepPlane) -> dict[str, Any]:
    await plane.execute(
        "UPSERT type::record($key) SET organization_id = $organization_id, "
        "plane = $plane, updated_at = time::now() RETURN NONE;",
        key=plane.state_key,
        organization_id=plane.organization_id,
        plane=plane.name,
    )
    return await _read_state(plane)


async def _read_state(plane: SweepPlane) -> dict[str, Any]:
    rows = normalize_records(
        await plane.execute(
            f"SELECT {_STATE_PROJECTION} FROM type::record($key);",
            key=plane.state_key,
        )
    )
    return rows[0] if rows else {}


async def _acquire_lease(plane: SweepPlane, *, owner: str, budget: float) -> bool:
    lease = f"{int(budget + _LEASE_MARGIN_SECONDS)}s"
    rows = normalize_records(
        await plane.execute(
            "UPDATE type::record($key) SET lease_owner = $owner, "
            "lease_until = time::now() + <duration>$lease, "
            "active_metadata = $stamp, updated_at = time::now() "
            "WHERE lease_until = NONE OR lease_until < time::now() RETURN uuid, lease_owner;",
            key=plane.state_key,
            owner=owner,
            lease=lease,
            stamp=plane.stamp,
        )
    )
    return any(row.get("lease_owner") == owner for row in rows)


async def _save_cursors(plane: SweepPlane, *, owner: str, cursors: Mapping[str, str]) -> None:
    await plane.execute(
        "UPDATE type::record($key) SET cursors = $cursors, updated_at = time::now() "
        "WHERE lease_owner = $owner RETURN NONE;",
        key=plane.state_key,
        owner=owner,
        cursors=dict(cursors),
    )


async def _release(
    plane: SweepPlane,
    *,
    owner: str,
    cursors: Mapping[str, str],
    receipt: Mapping[str, Any] | None,
    complete: EmbeddingStamp | None,
    generation: int,
) -> None:
    try:
        await plane.execute(
            "UPDATE type::record($key) SET cursors = $cursors, "
            "last_run = IF $receipt = NONE THEN last_run ELSE $receipt END, "
            "complete_metadata = IF $complete != NONE AND (generation ?? 0) = $generation "
            "THEN $complete ELSE complete_metadata END, "
            "complete_at = IF $complete != NONE AND (generation ?? 0) = $generation "
            "THEN time::now() ELSE complete_at END, "
            "lease_owner = NONE, lease_until = NONE, updated_at = time::now() "
            "WHERE lease_owner = $owner RETURN NONE;",
            key=plane.state_key,
            owner=owner,
            cursors=dict(cursors),
            receipt=(
                {**dict(receipt), "finished_at": datetime.now(UTC)} if receipt is not None else None
            ),
            complete=complete,
            generation=generation,
        )
    except Exception as exc:
        # The lease expires on its own; a lost release only delays the next pass.
        log.warning(
            "embedding_sweep_release_failed",
            organization_id=plane.organization_id,
            plane=plane.name,
            error_type=type(exc).__name__,
        )


async def mark_plane_for_reembed(
    *,
    plane: str,
    organization_id: str,
    execute: SweepExecute,
    tables: Sequence[SweepTable],
    page_size: int = 1000,
) -> int:
    """Stamp every vector in a plane as unverified so the sweep replaces it.

    The operator escape hatch for vectors the plane's verdict trusted but the
    operator knows came from another model. Metadata-only writes, paged, and
    idempotent: already-unverified rows are skipped. Returns rows marked.
    """
    marker = unverified_embedding_metadata(UNVERIFIED_ORIGIN_OPERATOR)
    marked = 0
    for table in tables:
        while True:
            rows = normalize_records(
                await execute(
                    table.unverify_query(),
                    scope=organization_id,
                    unverified=UNVERIFIED_EMBEDDING_PROVIDER,
                    marker=marker,
                    limit=page_size,
                )
            )
            marked += len(rows)
            if len(rows) < page_size:
                break
    await execute(
        REOPEN_EMBEDDING_STATES.replace(
            "WHERE $organizations CONTAINS organization_id",
            "WHERE $organizations CONTAINS organization_id AND plane = $plane",
        ),
        organizations=[organization_id],
        plane=plane,
    )
    return marked


async def read_embedding_sweep_state(
    plane: str, organization_id: str, execute: SweepExecute
) -> dict[str, Any]:
    """Return a plane's persisted state for status surfaces, or an empty dict."""
    rows = normalize_records(
        await execute(
            f"SELECT {_STATE_PROJECTION} FROM type::record($key);",
            key=embedding_state_key(organization_id, plane),
        )
    )
    return rows[0] if rows else {}


__all__ = [
    "SWEEP_BUSY",
    "SWEEP_COMPLETED",
    "SWEEP_CURRENT",
    "SWEEP_PARTIAL",
    "SWEEP_PROVIDER_FAILING",
    "SWEEP_SKIPPED_DIMENSION_MISMATCH",
    "SWEEP_SKIPPED_NO_PROVIDER",
    "EmbedRows",
    "EmbeddingSweepResult",
    "LegacyEvidence",
    "LegacyVectorBasis",
    "LegacyVectorDecision",
    "SweepPlane",
    "SweepTable",
    "configured_legacy_vector_policy",
    "decide_legacy_vectors",
    "embedding_state_key",
    "ensure_legacy_decision",
    "mark_plane_for_reembed",
    "read_embedding_sweep_state",
    "run_embedding_sweep",
]
