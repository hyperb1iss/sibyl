"""Give recallable raw captures the vector the raw recall lane expects."""

from __future__ import annotations

import asyncio
import hashlib
import json
import random
import time
from collections.abc import AsyncIterator, Awaitable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, cast
from uuid import uuid4

import structlog

from sibyl_core.backends.surreal import SurrealContentClient
from sibyl_core.backends.surreal.content_schema import EMBEDDING_DIM
from sibyl_core.backends.surreal.schema_embedding_states import (
    CONTENT_SCHEMA_RECORD_NAME,
    RAW_EMBEDDING_REFUSAL_SCHEMA_VERSION,
    embedding_state_key,
    raw_embedding_refusal_key,
)
from sibyl_core.backends.surreal.schema_version import schema_version_record_id
from sibyl_core.embeddings.provenance import (
    PROVIDER_ERROR_INPUT,
    is_legacy_stamp,
    is_model_processing_error,
    provider_error_kind,
    provider_error_status,
    same_vector_identity,
    stamp_unchanged_predicate,
    vector_identity,
    vector_identity_differs_predicate,
    vector_space,
    vector_space_predicate,
)
from sibyl_core.embeddings.providers import EmbeddingProvider
from sibyl_core.projection.repair import LifecycleRepairResult
from sibyl_core.services import content_client
from sibyl_core.services import content_models as models
from sibyl_core.services.content_models import RawMemory
from sibyl_core.services.content_raw_persistence import (
    _RAW_MEMORY_EMBEDDING_AUTO,
    _raw_memories_with_embeddings,
    _raw_memory_without_embedding,
)
from sibyl_core.services.embedding_sweep import page_after_cursor

log = structlog.get_logger()

RAW_EMBEDDING_REPAIR_PAGE_SIZE = 256
#: Wall-clock seconds one call spends starting new work for one organization.
#: The lifecycle job visits every organization every minute, so a pass that
#: runs out saves its place and the next pass resumes there.
RAW_EMBEDDING_REPAIR_BUDGET_SECONDS = 45.0
#: The embedding state plane that holds raw repair's lease, cursor and receipt.
RAW_CAPTURE_EMBEDDING_PLANE = "raw_captures"

REPAIR_COMPLETED = "completed"
#: The budget ran out before the walk finished; ``cursor`` is where the next
#: pass resumes.
REPAIR_PARTIAL = "partial"
#: The provider throttled the pass, failed, or refused access, so the pass
#: ended and blamed no row. The next pass resumes after the page it was on.
REPAIR_PROVIDER_FAILING = "provider_failing"
#: Another pass holds this organization's raw repair lease.
REPAIR_BUSY = "busy"
#: The lease lapsed mid-pass and another pass took it; this one stopped.
REPAIR_LEASE_LOST = "lease_lost"
REPAIR_SKIPPED_NO_PROVIDER = "skipped_no_provider"
REPAIR_SKIPPED_DIMENSION_MISMATCH = "skipped_dimension_mismatch"
REPAIR_SKIPPED_SCHEMA_PENDING = "skipped_schema_pending"

# A refused capture is skipped until its revision or the configured vector
# identity changes, or this long passes.
_REFUSAL_RETRY_SECONDS = 24 * 60 * 60
# A capture the model kept failing on is deferred, never refused, for longer
# each time it fails again: an hour, then six, then a day at most.
_DEFERRAL_BACKOFF_SECONDS = (60 * 60, 6 * 60 * 60, 24 * 60 * 60)
_REFUSED = "refused"
_DEFERRED = "deferred"
# A lease outlives the budget by this much, so a pass finishing writes it
# already paid for is not mistaken for a dead one.
_LEASE_MARGIN_SECONDS = 120.0
# Failed requests since the provider last returned vectors before the pass
# checks again that it serves requests at all, so an outage that starts
# mid-pass is not mistaken for a run of refused rows.
_FAILURES_BEFORE_RECHECK = 8
_PROBE_TEXT = "raw capture embedding health check"
# Failed requests a pass spends isolating rows the model keeps failing on
# (ModelError, model timeout, internal error) before it ends as provider
# failing: one bad row in a page costs about nine, a model failing on every
# input would otherwise bisect the whole page.
_MODEL_FAILURE_LIMIT = 32
# Rows per restamp statement; the statements of one page run concurrently.
_RESTAMP_BATCH_ROWS = 64
_RESTAMP_CONCURRENCY = 4
# Longest one provider request may run, as in the embedding sweep. A request
# that started before the deadline runs to completion unless it hangs this long.
_PROVIDER_CALL_TIMEOUT_SECONDS = 60.0

_clock = time.monotonic


@dataclass(frozen=True, slots=True)
class RawEmbeddingRepairResult(LifecycleRepairResult):
    """Lifecycle counts, why a pass stopped, and where the next one resumes.

    ``refused`` counts rows the provider rejected on their own, in this pass
    or an earlier one; they are skipped until their revision or the model
    changes. ``deferred`` counts rows the model kept failing on, which wait
    out a growing expiry and are never refused. ``cursor`` is empty once a
    walk reaches every row.
    """

    status: str = REPAIR_COMPLETED
    provider_dimensions: int | None = None
    schema_dimensions: int | None = None
    refused: int = 0
    deferred: int = 0
    cursor: str = ""


# The walk reads only what the candidate decision needs. raw_content and the
# vector stay on the server until a row is actually going to be embedded; the
# scheduler runs this every minute across every organization.
_RAW_EMBEDDING_WALK_FIELDS = ", ".join(
    (
        "uuid",
        "revision",
        "organization_id",
        "source_id",
        "principal_id",
        "review_state",
        "deleted_at",
        "metadata",
    )
)
_RAW_EMBEDDING_OWED_PREDICATE = (
    "(embedding = NONE OR (metadata.embedding_metadata ?? NONE) = NONE "
    "OR (metadata.embedding_metadata.stamp_version ?? NONE) = NONE OR "
    + vector_identity_differs_predicate("metadata.embedding_metadata", "expected_metadata")
    + ")"
)
_RAW_EMBEDDING_WALK_QUERY = (
    f"SELECT {_RAW_EMBEDDING_WALK_FIELDS} FROM raw_captures "
    "WHERE organization_id = $organization_id AND uuid >= $cursor "
    f"AND deleted_at = NONE AND {_RAW_EMBEDDING_OWED_PREDICATE} "
    "ORDER BY uuid ASC LIMIT $limit;"
)
# Found by uuid alone (see the writes below): beside the organization, a 3.x
# server reads this page through an organization index, ten times slower.
_RAW_EMBEDDING_FETCH_QUERY = (
    "SELECT uuid, revision, organization_id, source_id, principal_id, review_state, "
    "deleted_at, title, raw_content, embedding, metadata "
    "FROM (SELECT VALUE id FROM raw_captures WHERE uuid IN $ids) "
    "WHERE organization_id = $organization_id;"
)
# How far a converting organization has come, for the raw lane's readiness:
# one read, only after a pass that did not reach every row.
_RAW_EMBEDDING_PROGRESS_QUERY = (
    "SELECT count(embedding != NONE AND "
    + vector_space_predicate("metadata.embedding_metadata", "expected_metadata")
    + f") AS in_model, count({_RAW_EMBEDDING_OWED_PREDICATE}) AS owed "
    "FROM raw_captures WHERE organization_id = $organization_id AND deleted_at = NONE "
    "GROUP ALL;"
)

# Only the vector and its provenance move. A full-row upsert would bump the
# revision, and revision is what sealed readers compare against their snapshots.
# A legacy stamp that already names the configured model is rewritten in the
# current format without an embedding call, a page at a time; only after the
# content upgrade has photographed the old stamps may they be rewritten at all.
#
# Both writes find their rows by uuid alone and check the organization and the
# revision on the rows found. Given the organization beside the uuid, a 3.x
# server plans the UPDATE through an organization index and walks every capture
# the organization has for each row (seconds per row at 50,000 captures); by
# uuid alone it uses the unique index.
#
# Both also require the stamp this pass read to still be the one stored, in
# every field that shapes the vector and in its format. Replacing a vector
# leaves the revision alone, so the revision cannot tell a restamp that
# another repair (a process configured for another model, during a rolling
# deploy) wrote its own vector and stamp in the meantime; without this fence
# the restamp would relabel that vector as this pass's model. The lease keeps
# two passes of this release apart; the stamp still fences every write, since
# a process of another release holds no lease (see
# ``stamp_unchanged_predicate`` for why it compares fields and how it treats
# NULLs).
_RAW_STAMP = "metadata.embedding_metadata"
_RAW_EMBEDDING_RESTAMP_QUERY = f"""
UPDATE (SELECT VALUE id FROM raw_captures WHERE uuid IN $uuids)
SET metadata.embedding_metadata = $embedding_metadata
WHERE organization_id = $organization_id AND revision = $revisions[uuid]
    AND embedding != NONE AND {stamp_unchanged_predicate(_RAW_STAMP, "$observed[uuid]")}
RETURN uuid;
"""

_RAW_EMBEDDING_UPDATE_QUERY = f"""
UPDATE (SELECT VALUE id FROM raw_captures WHERE uuid = $uuid) SET
    embedding = $embedding,
    metadata.embedding_metadata = $embedding_metadata
WHERE organization_id = $organization_id AND revision = $revision
    AND {stamp_unchanged_predicate(_RAW_STAMP, "$observed")}
RETURN uuid;
"""

# Refusals are read by record id, one digest per organization and capture, so
# the lookup never depends on how an engine plans the compound index.
_REFUSALS_QUERY = (
    "SELECT capture_id, revision, identity, kind, attempts, "
    "(expires_at > time::now()) AS active "
    "FROM array::map($keys, |$key| type::record($key)) "
    "WHERE organization_id = $organization_id;"
)
_REMEMBER_REFUSAL_QUERY = (
    "UPSERT type::record($key) SET organization_id = $organization_id, "
    "capture_id = $capture_id, revision = $revision, identity = $identity, "
    "kind = $kind, attempts = $attempts, "
    "error_type = $error_type, status_code = $status_code, refused_at = time::now(), "
    "expires_at = time::now() + <duration>$ttl RETURN NONE;"
)
_PRUNE_REFUSALS_QUERY = (
    "DELETE raw_embedding_refusals WHERE organization_id = $organization_id "
    "AND expires_at < time::now() RETURN NONE;"
)

# The lease is one conditional UPDATE on the organization's raw plane state
# row, never a read then a write: the embedded engine loses concurrent
# read-modify-write. SurrealDB's memory storage can still, under contention,
# let two conditional UPDATEs through together (RocksDB and SurrealKV have
# not), so the winner reads the holder back, and every page renews only while
# it is still the holder; two passes then overlap by one page at most, and the
# stamp fence keeps their writes apart.
_ENSURE_STATE_QUERY = (
    "UPSERT type::record($key) SET organization_id = $organization_id, plane = $plane, "
    "updated_at = time::now() RETURN NONE;"
)
_ACQUIRE_LEASE_QUERY = (
    "UPDATE type::record($key) SET lease_owner = $owner, "
    "lease_until = time::now() + <duration>$lease, active_metadata = $stamp, "
    "updated_at = time::now() "
    "WHERE lease_until = NONE OR lease_until < time::now() RETURN lease_owner;"
)
_READ_STATE_QUERY = "SELECT lease_owner, cursors, generation FROM type::record($key);"
_RENEW_LEASE_QUERY = (
    "UPDATE type::record($key) SET cursors = $cursors, "
    "lease_until = time::now() + <duration>$lease, updated_at = time::now() "
    "WHERE lease_owner = $owner RETURN lease_owner;"
)
_RELEASE_LEASE_QUERY = (
    "UPDATE type::record($key) SET cursors = $cursors, last_run = $receipt, "
    "complete_metadata = IF $complete != NONE AND (generation ?? 0) = $generation "
    "THEN $complete ELSE complete_metadata END, "
    "complete_at = IF $complete != NONE AND (generation ?? 0) = $generation "
    "THEN time::now() ELSE complete_at END, "
    "lease_owner = NONE, lease_until = NONE, updated_at = time::now() "
    "WHERE lease_owner = $owner RETURN NONE;"
)


def raw_memory_embedding_current(memory: RawMemory, provider: EmbeddingProvider) -> bool:
    """A vector counts only when its recorded provenance matches the configured provider.

    It must also be in the current stamp format; a matching legacy stamp is
    rewritten in place, without an embedding call.
    """
    if memory.embedding is None:
        return False
    stamp = memory.metadata.get("embedding_metadata")
    return not is_legacy_stamp(stamp) and same_vector_identity(
        stamp, models.raw_memory_embedding_metadata(provider.metadata)
    )


def _needs_restamp_only(memory: RawMemory, provider: EmbeddingProvider) -> bool:
    stamp = memory.metadata.get("embedding_metadata")
    return (
        memory.embedding is not None
        and is_legacy_stamp(stamp)
        and same_vector_identity(stamp, models.raw_memory_embedding_metadata(provider.metadata))
    )


def _repair_candidate(memory: RawMemory, provider: EmbeddingProvider) -> bool:
    return (
        memory.deleted_at is None
        and models.raw_memory_recallable(memory)
        and not raw_memory_embedding_current(memory, provider)
    )


def _identity_digest(stamp: Mapping[str, object]) -> str:
    identity = json.dumps(vector_identity(stamp), sort_keys=True, default=str)
    return hashlib.sha256(identity.encode()).hexdigest()[:32]


@dataclass(slots=True)
class _Pass:
    """One call's budget, lease and what it has learned about the provider."""

    organization_id: str
    provider: EmbeddingProvider
    client: SurrealContentClient
    expected_metadata: Mapping[str, object]
    identity: str
    owner: str
    lease: str
    deadline: float
    serving: bool = False
    failures_since_serving: int = 0
    model_failures: int = 0
    # Earlier deferrals of captures this pass met, for the growing expiry.
    deferral_attempts: dict[str, int] = field(default_factory=dict)
    # Each pass orders its pages and halves afresh, so a failure limit that
    # stops one pass cannot stop the next at the same rows.
    rng: random.Random = field(default_factory=random.Random)
    # Every pass finishes its first page whatever the clock says, so a walk
    # slower than the budget cannot stall the organization pass after pass.
    progressed: bool = False

    def out_of_time(self) -> bool:
        return self.progressed and _clock() >= self.deadline

    async def rows(self, query: str, **params: object) -> list[models.SurrealRecord]:
        """Run one statement to completion; the budget never cancels a query."""
        return await content_client.select_many(self.client, query, **params)

    async def provider_call[T](self, work: Awaitable[T]) -> T:
        """Await a provider request to completion, unless it hangs.

        The deadline stops new requests; one already sent is paid for and
        finishes. Only a request that runs past ``_PROVIDER_CALL_TIMEOUT_SECONDS``
        is abandoned, and its ``TimeoutError`` reads as a transient failure.
        """
        async with asyncio.timeout(_PROVIDER_CALL_TIMEOUT_SECONDS):
            return await work

    async def provider_serving(self) -> bool:
        """Whether the provider serves a request right now.

        The probe text is new every time, so a caching provider cannot answer
        it from memory during an outage.
        """
        try:
            vectors = await self.provider_call(
                self.provider.embed_texts([f"{_PROBE_TEXT} {uuid4().hex}"], input_kind="document")
            )
            models.embedding_vector_from_batch(vectors, self.provider.metadata.dimensions)
        except Exception as exc:
            log.warning(
                "raw_capture_embedding_repair_provider_failing",
                organization_id=self.organization_id,
                provider=self.provider.metadata.provider,
                model=self.provider.metadata.model,
                error_type=type(exc).__name__,
                status_code=provider_error_status(exc),
            )
            self.serving = False
            return False
        self.served()
        return True

    def served(self) -> None:
        self.serving = True
        self.failures_since_serving = 0


@dataclass(slots=True)
class _Embedded:
    """How a page's rows came out of the provider."""

    vectors: list[RawMemory] = field(default_factory=list)
    refused: list[tuple[RawMemory, BaseException]] = field(default_factory=list)
    failed: list[RawMemory] = field(default_factory=list)
    # Rows the model failed on alone: deferred with a growing expiry, never refused.
    deferred: list[tuple[RawMemory, BaseException]] = field(default_factory=list)
    unattempted: list[RawMemory] = field(default_factory=list)
    stop: str | None = None


async def repair_raw_capture_embeddings(
    organization_id: str,
    *,
    page_size: int = RAW_EMBEDDING_REPAIR_PAGE_SIZE,
    embedding_provider: EmbeddingProvider | object | None = _RAW_MEMORY_EMBEDDING_AUTO,
    client: SurrealContentClient | None = None,
    budget_seconds: float | None = None,
) -> RawEmbeddingRepairResult:
    """Embed recallable raw captures whose vector is missing or from another provider.

    Captures restored from an archive, or written while no embedding provider
    was configured, otherwise stay lexical-only forever: the raw_vector lane
    reads only rows that carry a vector from the query's model. Pages are
    keyset-ordered by uuid, and a row whose revision moved between read and
    write is reported pending rather than overwritten.

    One pass per organization runs at a time, under a lease on the
    organization's ``raw_captures`` embedding state row, which also keeps the
    walk's cursor and the last pass's receipt, so the API and the worker share
    them and a restart resumes where the last pass stopped. A call stops
    starting new work once ``budget_seconds`` have passed
    (``RAW_EMBEDDING_REPAIR_BUDGET_SECONDS`` by default), so one large
    organization cannot hold the lifecycle pass; it resumes at its cursor next
    time and wraps round to the rows before it. The budget cancels nothing:
    every query and provider request it has issued runs to completion (a
    provider request that hangs is abandoned after
    ``_PROVIDER_CALL_TIMEOUT_SECONDS``), and every vector it has paid for is
    written. A pass always finishes its first page, so a walk or a provider
    slower than the budget still moves the organization forward.

    A request the provider turns down as an unacceptable input is halved until
    the rejected rows stand alone. A lone row is refused only after a fresh
    probe shows the provider still serves requests; refusals are logged,
    counted and kept in ``raw_embedding_refusals``, and skipped until their
    revision or the model changes or a day passes. A request the model failed
    on (ModelError, model timeout, internal error) is isolated the same way
    but blames no row: a row it keeps failing on alone is stored as deferred
    and skipped for an hour, then six, then a day, and never refused. Each
    pass shuffles the rows of every page and the halves of every split, so
    the failure limit that ends one pass cannot stop the next at the same
    rows. Throttling, quota and unavailability, and credential or access
    failures, end the pass and blame no row (see ``provider_error_kind``).

    A caller that already holds the database the captures live in, such as a
    restore, passes its client so the repair cannot land on another store.

    The walk selects only the columns the candidate decision needs and lets
    the server drop rows whose vector already matches the configured provider,
    so a fully current organization returns one empty page. The server still
    reads every one of the organization's captures to find that out, because
    the stamp predicate cannot be served from an index: about 1.3 s per 20,000
    captures each pass on a native 3.2 server. Text is fetched only for the
    rows about to be embedded. A provider whose dimensions differ from
    the schema's embedding field is refused up front: every write would fail
    the typed-array check and the paid embedding call would repeat each pass.
    """
    provider = (
        models.configured_raw_memory_embedding_provider()
        if embedding_provider is _RAW_MEMORY_EMBEDDING_AUTO
        else cast("EmbeddingProvider | None", embedding_provider)
    )
    if provider is None:
        return RawEmbeddingRepairResult(status=REPAIR_SKIPPED_NO_PROVIDER)
    if provider.metadata.dimensions != EMBEDDING_DIM:
        log.warning(
            "raw_capture_embedding_repair_dimension_mismatch",
            organization_id=organization_id,
            provider=provider.metadata.provider,
            model=provider.metadata.model,
            provider_dimensions=provider.metadata.dimensions,
            schema_dimensions=EMBEDDING_DIM,
        )
        return RawEmbeddingRepairResult(
            status=REPAIR_SKIPPED_DIMENSION_MISMATCH,
            provider_dimensions=provider.metadata.dimensions,
            schema_dimensions=EMBEDDING_DIM,
        )
    budget = RAW_EMBEDDING_REPAIR_BUDGET_SECONDS if budget_seconds is None else budget_seconds
    async with _content_session(client) as session:
        # Raw capture stamps are the chunk plane's evidence of the model that
        # preceded this release; restamping them before the content upgrade
        # photographs them would erase that evidence. The refusal table
        # arrives one version later.
        if not await _schema_ready(session):
            log.info(
                "raw_capture_embedding_repair_waiting_for_schema",
                organization_id=organization_id,
            )
            return RawEmbeddingRepairResult(status=REPAIR_SKIPPED_SCHEMA_PENDING)
        expected_metadata = models.raw_memory_embedding_metadata(provider.metadata)
        run = _Pass(
            organization_id=organization_id,
            provider=provider,
            client=session,
            expected_metadata=expected_metadata,
            identity=_identity_digest(expected_metadata),
            owner=uuid4().hex,
            lease=f"{int(max(budget, 0.0) + _LEASE_MARGIN_SECONDS)}s",
            deadline=_clock() + max(budget, 0.0),
        )
        state = await _acquire_lease(run)
        if state is None:
            log.info("raw_capture_embedding_repair_busy", organization_id=organization_id)
            return RawEmbeddingRepairResult(status=REPAIR_BUSY)
        return await _run_pass(run, state, page_size=max(1, page_size))


async def _run_pass(
    run: _Pass, state: Mapping[str, Any], *, page_size: int
) -> RawEmbeddingRepairResult:
    counts = dict.fromkeys(("checked", "recovered", "pending", "failed", "refused", "deferred"), 0)
    cursors = state.get("cursors")
    saved = cursors.get(RAW_CAPTURE_EMBEDDING_PLANE) if isinstance(cursors, Mapping) else None
    start = saved if isinstance(saved, str) else ""
    generation = state.get("generation")
    status = REPAIR_COMPLETED
    resume = ""
    cursor = start
    finished = False
    try:
        # Resume after the saved cursor, then wrap round to the rows before it.
        segments: list[tuple[str, str | None]] = (
            [(start, None), ("", start)] if start else [("", None)]
        )
        for lower, upper in segments:
            cursor = lower
            while status == REPAIR_COMPLETED:
                if run.out_of_time():
                    status, resume = REPAIR_PARTIAL, cursor
                    break
                if not await _renew_lease(run, cursor):
                    status, resume = REPAIR_LEASE_LOST, cursor
                    break
                fetched = await run.rows(
                    _RAW_EMBEDDING_WALK_QUERY,
                    organization_id=run.organization_id,
                    cursor=cursor,
                    expected_metadata=run.expected_metadata,
                    limit=page_size + 1,
                )
                # Pages read from the cursor inclusively (see ``page_after_cursor``).
                rows, more = page_after_cursor(fetched, cursor, page_size)
                page = [row for row in rows if upper is None or str(row["uuid"]) <= upper]
                stop = await _repair_rows(page, run, counts) if page else None
                run.progressed = True
                if stop == REPAIR_PARTIAL:
                    # Repaired rows leave the walk and refusals are stored, so
                    # the next pass retries only what this page left.
                    status, resume = stop, cursor
                    break
                if stop is not None:
                    # A provider that is failing blames no row; the next pass
                    # moves on, so one page that keeps failing cannot hold up
                    # the organization, and comes back round when the walk wraps.
                    status, resume = stop, str(page[-1]["uuid"])
                    break
                if not page:
                    break
                cursor = str(page[-1]["uuid"])
                if not more or len(page) < len(rows):
                    break
            if status != REPAIR_COMPLETED:
                break
        finished = True
    finally:
        if not finished:
            # The walk raised; the next pass resumes where this one was.
            resume = cursor
        complete = (
            finished and status == REPAIR_COMPLETED and counts["pending"] == counts["failed"] == 0
        )
        if status != REPAIR_LEASE_LOST:
            await _release_lease(
                run,
                cursor=resume,
                counts=counts,
                status=status,
                complete=complete,
                generation=generation if isinstance(generation, int) else 0,
            )
    log.info(
        "raw_capture_embedding_repair_completed",
        organization_id=run.organization_id,
        status=status,
        resumed_from=start or None,
        cursor=resume or None,
        **counts,
    )
    return RawEmbeddingRepairResult(
        checked=counts["checked"],
        recovered=counts["recovered"],
        pending=counts["pending"],
        failed=counts["failed"],
        status=status,
        refused=counts["refused"],
        deferred=counts["deferred"],
        cursor=resume,
    )


@asynccontextmanager
async def _content_session(
    client: SurrealContentClient | None,
) -> AsyncIterator[SurrealContentClient]:
    if client is not None:
        yield client
        return
    async with content_client.surreal_content_client() as shared:
        yield shared


async def _schema_ready(client: SurrealContentClient) -> bool:
    rows = await content_client.select_many(
        client, f"SELECT version FROM [{schema_version_record_id(CONTENT_SCHEMA_RECORD_NAME)}];"
    )
    version = rows[0].get("version") if rows else None
    return isinstance(version, int | float) and version >= RAW_EMBEDDING_REFUSAL_SCHEMA_VERSION


def _state_key(run: _Pass) -> str:
    return embedding_state_key(run.organization_id, RAW_CAPTURE_EMBEDDING_PLANE)


async def _acquire_lease(run: _Pass) -> Mapping[str, Any] | None:
    """Take the organization's raw repair lease; return its state, or None when held."""
    key = _state_key(run)
    await run.rows(
        _ENSURE_STATE_QUERY,
        key=key,
        organization_id=run.organization_id,
        plane=RAW_CAPTURE_EMBEDDING_PLANE,
    )
    won = await run.rows(
        _ACQUIRE_LEASE_QUERY,
        key=key,
        owner=run.owner,
        lease=run.lease,
        stamp=dict(run.expected_metadata),
    )
    if not any(row.get("lease_owner") == run.owner for row in won):
        return None
    state = await run.rows(_READ_STATE_QUERY, key=key)
    if not state or state[0].get("lease_owner") != run.owner:
        return None
    return state[0]


async def _renew_lease(run: _Pass, cursor: str) -> bool:
    """Extend the lease and save the cursor; False once another pass holds it."""
    rows = await run.rows(
        _RENEW_LEASE_QUERY,
        key=_state_key(run),
        owner=run.owner,
        lease=run.lease,
        cursors={RAW_CAPTURE_EMBEDDING_PLANE: cursor},
    )
    return any(row.get("lease_owner") == run.owner for row in rows)


async def _release_lease(
    run: _Pass,
    *,
    cursor: str,
    counts: Mapping[str, int],
    status: str,
    complete: bool,
    generation: int,
) -> None:
    receipt: dict[str, Any] = {
        "status": status,
        "space": vector_space(run.expected_metadata),
        **{f"pass_{name}": value for name, value in counts.items()},
        "finished_at": datetime.now(UTC),
    }
    try:
        if not complete:
            progress = await run.rows(
                _RAW_EMBEDDING_PROGRESS_QUERY,
                organization_id=run.organization_id,
                expected_metadata=run.expected_metadata,
            )
            row = progress[0] if progress else {}
            receipt["in_model"] = models.coerce_int(row.get("in_model"))
            receipt["pending"] = models.coerce_int(row.get("owed"))
        else:
            await run.rows(_PRUNE_REFUSALS_QUERY, organization_id=run.organization_id)
        await run.rows(
            _RELEASE_LEASE_QUERY,
            key=_state_key(run),
            owner=run.owner,
            cursors={RAW_CAPTURE_EMBEDDING_PLANE: cursor},
            receipt=receipt,
            complete=dict(run.expected_metadata) if complete else None,
            generation=generation,
        )
    except Exception as exc:
        # The lease expires on its own; a lost release only delays the next pass.
        log.warning(
            "raw_capture_embedding_repair_release_failed",
            organization_id=run.organization_id,
            error_type=type(exc).__name__,
        )


async def _repair_rows(
    rows: Sequence[Mapping[str, object]],
    run: _Pass,
    counts: dict[str, int],
) -> str | None:
    """Repair one walked page; return why the pass must stop, if it must."""
    recallable = [
        memory
        for memory in (models.raw_memory_from_record(row) for row in rows)
        if models.raw_memory_recallable(memory)
    ]
    counts["checked"] += len(recallable)
    if not recallable:
        return None
    if run.out_of_time():
        counts["pending"] += len(recallable)
        return REPAIR_PARTIAL
    set_aside = await _set_aside(run, recallable)
    counts["refused"] += sum(1 for kind in set_aside.values() if kind == _REFUSED)
    counts["deferred"] += sum(1 for kind in set_aside.values() if kind == _DEFERRED)
    candidates = [memory for memory in recallable if memory.id not in set_aside]
    if not candidates:
        return None
    outcomes, stop = await _repair_page(candidates, run)
    for outcome in outcomes:
        counts[outcome] += 1
    return stop


async def _set_aside(run: _Pass, memories: Sequence[RawMemory]) -> dict[str, str]:
    """Captures whose stored refusal or deferral still holds, by kind.

    A row counts only while it names the capture's revision and this model.
    An expired deferral no longer sets its capture aside, but its attempt
    count sets the expiry of the next one.
    """
    records = await run.rows(
        _REFUSALS_QUERY,
        organization_id=run.organization_id,
        keys=[raw_embedding_refusal_key(run.organization_id, memory.id) for memory in memories],
    )
    revisions = {memory.id: memory.revision for memory in memories}
    set_aside: dict[str, str] = {}
    for record in records:
        capture_id = str(record.get("capture_id"))
        if record.get("identity") != run.identity or revisions.get(capture_id) != record.get(
            "revision"
        ):
            continue
        kind = _DEFERRED if record.get("kind") == _DEFERRED else _REFUSED
        if kind == _DEFERRED:
            run.deferral_attempts[capture_id] = models.coerce_int(record.get("attempts"))
        if record.get("active") is True:
            set_aside[capture_id] = kind
    return set_aside


async def _repair_page(
    candidates: Sequence[RawMemory],
    run: _Pass,
) -> tuple[list[str], str | None]:
    if run.out_of_time():
        return ["pending"] * len(candidates), REPAIR_PARTIAL
    # Fetch text and the current revision only now, for the rows being embedded.
    fetched = await run.rows(
        _RAW_EMBEDDING_FETCH_QUERY,
        organization_id=run.organization_id,
        ids=[memory.id for memory in candidates],
    )
    current = [models.raw_memory_from_record(row) for row in fetched]
    targets = [memory for memory in current if _repair_candidate(memory, run.provider)]
    # A row gone or made current between the walk and this fetch needs nothing
    # from this pass and will not reappear in the next one.
    outcomes: list[str] = ["recovered"] * (len(candidates) - len(targets))
    restamps = [memory for memory in targets if _needs_restamp_only(memory, run.provider)]
    stop: str | None = None
    if restamps:
        restamp_outcomes, stop = await _restamp(run, restamps)
        outcomes.extend(restamp_outcomes)
        targets = [memory for memory in targets if not _needs_restamp_only(memory, run.provider)]
    if not targets:
        return outcomes, stop
    if stop is not None:
        return [*outcomes, *(["pending"] * len(targets))], stop
    observed = {memory.id: memory.metadata.get("embedding_metadata") for memory in targets}
    run.rng.shuffle(targets)
    embedded = await _embed_isolating(
        [_raw_memory_without_embedding(memory) for memory in targets], run
    )
    vectors = [memory for memory in embedded.vectors if memory.embedding is not None]
    # Every vector the pass paid for is written, whatever the clock says.
    writes = await asyncio.gather(
        *(
            _write_embedding(
                run.client, memory, run.organization_id, observed=observed.get(memory.id)
            )
            for memory in vectors
        ),
        return_exceptions=True,
    )
    for memory, write in zip(vectors, writes, strict=True):
        if isinstance(write, BaseException):
            log.warning(
                "raw_capture_embedding_repair_write_failed",
                organization_id=run.organization_id,
                memory_id=memory.id,
                error_type=type(write).__name__,
            )
            outcomes.append("failed")
        else:
            outcomes.append("recovered" if write else "pending")
    for memory, exc in embedded.refused:
        await _record_refusal(run, memory, exc, kind=_REFUSED)
    for memory, exc in embedded.deferred:
        await _record_refusal(run, memory, exc, kind=_DEFERRED)
    # A target the provider left unembedded, or one the pass stopped before
    # reaching, is still work for a later pass.
    outcomes.extend(["pending"] * (len(embedded.vectors) - len(vectors)))
    outcomes.extend(["pending"] * len(embedded.unattempted))
    outcomes.extend(["failed"] * len(embedded.failed))
    outcomes.extend(["refused"] * len(embedded.refused))
    outcomes.extend(["deferred"] * len(embedded.deferred))
    return outcomes, embedded.stop


async def _restamp(run: _Pass, restamps: Sequence[RawMemory]) -> tuple[list[str], str | None]:
    """Rewrite legacy stamps that already name the configured model, in batches.

    A batch starts only while the budget lasts; one that started always runs
    to completion.
    """
    stamp = dict(run.expected_metadata)
    slots = asyncio.Semaphore(_RESTAMP_CONCURRENCY)

    async def restamp(batch: Sequence[RawMemory]) -> set[str] | None:
        async with slots:
            if run.out_of_time():
                return None
            rows = await run.rows(
                _RAW_EMBEDDING_RESTAMP_QUERY,
                uuids=[memory.id for memory in batch],
                revisions={memory.id: memory.revision for memory in batch},
                observed={memory.id: memory.metadata.get("embedding_metadata") for memory in batch},
                organization_id=run.organization_id,
                embedding_metadata=stamp,
            )
            return {str(row.get("uuid")) for row in rows}

    # A row update costs about 3.5 ms on raw_captures whatever the batch
    # size, so a page's restamps run as concurrent statements.
    batches = [
        restamps[start : start + _RESTAMP_BATCH_ROWS]
        for start in range(0, len(restamps), _RESTAMP_BATCH_ROWS)
    ]
    results = await asyncio.gather(*(restamp(batch) for batch in batches))
    outcomes: list[str] = []
    stop: str | None = None
    for batch, restamped in zip(batches, results, strict=True):
        if restamped is None:
            stop = REPAIR_PARTIAL
            outcomes.extend(["pending"] * len(batch))
        else:
            outcomes.extend(
                "recovered" if memory.id in restamped else "pending" for memory in batch
            )
    return outcomes, stop


_SPLIT = "split"
_REFUSE = "refuse"
_DEFER = "defer"


async def _embed_isolating(memories: list[RawMemory], run: _Pass) -> _Embedded:
    """Embed a page, halving a rejected request until the rejected rows stand alone.

    One text the provider will not embed fails the whole request it rides in,
    so a request whose content was rejected is split and each half retried;
    the healthy path is still one request per page. A request the model
    failed on (ModelError, model timeout, internal error) is split the same
    way so one bad row cannot hold its page, but a row it keeps failing on
    alone is deferred, never refused. Throttling, quota, unavailability
    and access failures end the pass: splitting would only multiply requests
    the provider is already turning away.
    """
    outcome = _Embedded()
    pending = [memories]
    while pending:
        batch = pending.pop()
        if run.out_of_time():
            outcome.stop = REPAIR_PARTIAL
            outcome.unattempted.extend(batch)
            break
        try:
            embedded = await run.provider_call(_raw_memories_with_embeddings(batch, run.provider))
        except Exception as exc:
            verdict = await _judge_failure(exc, batch, run)
            if verdict == _SPLIT:
                middle = len(batch) // 2
                halves = [batch[middle:], batch[:middle]]
                run.rng.shuffle(halves)
                pending.extend(halves)
                continue
            if verdict == _REFUSE:
                outcome.refused.append((batch[0], exc))
                continue
            if verdict == _DEFER:
                outcome.deferred.append((batch[0], exc))
                continue
            log.warning(
                "raw_capture_embedding_repair_page_failed",
                organization_id=run.organization_id,
                rows=len(batch),
                error_type=type(exc).__name__,
                error_kind=provider_error_kind(exc),
                status_code=provider_error_status(exc),
            )
            outcome.stop = REPAIR_PROVIDER_FAILING
            outcome.failed.extend(batch)
            break
        run.served()
        outcome.vectors.extend(embedded)
    for batch in pending:
        outcome.unattempted.extend(batch)
    return outcome


async def _judge_failure(exc: BaseException, batch: Sequence[RawMemory], run: _Pass) -> str:
    """Decide whether a failed request blames its rows, and how.

    Only a rejected input can blame rows. A lone row is blamed only after a
    fresh probe succeeds, so an outage never turns into stored refusals. A
    model processing failure is isolated the same way but never blames: its
    lone row is deferred. A larger batch is split without a probe once the
    provider has served this pass. Returns ``_SPLIT``, ``_REFUSE``,
    ``_DEFER``, or the stop status.
    """
    input_rejected = provider_error_kind(exc) == PROVIDER_ERROR_INPUT
    if not input_rejected:
        if not is_model_processing_error(exc):
            return REPAIR_PROVIDER_FAILING
        run.model_failures += 1
        if run.model_failures > _MODEL_FAILURE_LIMIT:
            return REPAIR_PROVIDER_FAILING
    run.failures_since_serving += 1
    recheck = run.failures_since_serving >= _FAILURES_BEFORE_RECHECK
    # Blame needs a fresh probe for every lone row; a deferral blames no one,
    # so it probes only when the provider has not served this pass lately.
    needs_probe = not run.serving or recheck or (input_rejected and len(batch) == 1)
    if needs_probe and not await run.provider_serving():
        return REPAIR_PROVIDER_FAILING
    if len(batch) > 1:
        return _SPLIT
    return _REFUSE if input_rejected else _DEFER


async def _record_refusal(run: _Pass, memory: RawMemory, exc: BaseException, *, kind: str) -> None:
    attempts = 1
    ttl = _REFUSAL_RETRY_SECONDS
    if kind == _DEFERRED:
        attempts = run.deferral_attempts.get(memory.id, 0) + 1
        ttl = _DEFERRAL_BACKOFF_SECONDS[min(attempts, len(_DEFERRAL_BACKOFF_SECONDS)) - 1]
    await run.rows(
        _REMEMBER_REFUSAL_QUERY,
        key=raw_embedding_refusal_key(run.organization_id, memory.id),
        organization_id=run.organization_id,
        capture_id=memory.id,
        revision=memory.revision,
        identity=run.identity,
        kind=kind,
        attempts=attempts,
        error_type=type(exc).__name__,
        status_code=provider_error_status(exc),
        ttl=f"{ttl}s",
    )
    log.warning(
        f"raw_capture_embedding_{kind}",
        attempts=attempts,
        retry_after_seconds=ttl,
        organization_id=run.organization_id,
        memory_id=memory.id,
        revision=memory.revision,
        provider=run.provider.metadata.provider,
        model=run.provider.metadata.model,
        error_type=type(exc).__name__,
        aws_error_type=getattr(exc, "error_type", None),
        status_code=provider_error_status(exc),
    )


async def _write_embedding(
    client: SurrealContentClient,
    memory: RawMemory,
    organization_id: str,
    *,
    observed: object,
) -> bool:
    rows = await content_client.select_many(
        client,
        _RAW_EMBEDDING_UPDATE_QUERY,
        uuid=memory.id,
        organization_id=organization_id,
        revision=memory.revision,
        observed=observed,
        embedding=memory.embedding,
        embedding_metadata=memory.metadata.get("embedding_metadata"),
    )
    return bool(rows)
