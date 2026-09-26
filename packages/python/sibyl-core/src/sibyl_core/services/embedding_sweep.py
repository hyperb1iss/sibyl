"""Re-embed stored vectors whose recorded model is not the configured one.

A plane is a set of tables whose vectors share one configured model: the
graph plane holds entity and relationship vectors, the document chunk plane
holds chunk vectors. Every vector carries an ``embedding_metadata`` stamp, and
the sweep's single rule is that a row whose stamp differs from what the
configured provider writes today in a field that shapes the vector (see
``VECTOR_IDENTITY_FIELDS``), or whose stamp survived its vector, gets a new
vector from the configured provider.

Rows written before Sibyl stamped vectors carry no stamp. The first pass over
a plane classifies them once and persists the verdict (see
``LegacyVectorDecision``), then stamps them either as the configured model or
as unverified, after which they follow the ordinary rule. The verdict weighs
only evidence recorded before this release first touched the store (see
``LegacyEvidence``).

Passes are bounded by a wall-clock budget, hold a lease so overlapping
lifecycle ticks never double-embed, persist their walk cursor so a restart
resumes rather than rescans, and record a receipt that status surfaces read.
A plane that finished a full pass for the configured model skips its walk
until the verify interval lapses or an import or dimension rebuild reopens it.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
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
from sibyl_core.backends.surreal.schema_embedding_states import (
    REOPEN_EMBEDDING_STATES,
    embedding_state_key,
)
from sibyl_core.config import settings
from sibyl_core.embeddings.provenance import (
    UNVERIFIED_EMBEDDING_PROVIDER,
    UNVERIFIED_ORIGIN_LEGACY,
    UNVERIFIED_ORIGIN_OPERATOR,
    VECTOR_IDENTITY_FIELDS,
    is_transient_provider_error,
    same_vector_identity,
    unverified_embedding_metadata,
    vector_identity_differs_predicate,
)
from sibyl_core.projection.repair import LifecycleRepairResult

log = structlog.get_logger()

SWEEP_COMPLETED = "completed"
SWEEP_PARTIAL = "partial"
SWEEP_CURRENT = "current"
SWEEP_BUSY = "busy"
SWEEP_PROVIDER_FAILING = "provider_failing"
SWEEP_STORE_FAILING = "store_failing"
SWEEP_SKIPPED_NO_PROVIDER = "skipped_no_provider"
SWEEP_SKIPPED_DIMENSION_MISMATCH = "skipped_dimension_mismatch"
SWEEP_SKIPPED_SCHEMA_PENDING = "skipped_schema_pending"
# Another process took the plane's lease while this pass still held work.
SWEEP_LEASE_LOST = "lease_lost"

_LEASE_MARGIN_SECONDS = 120.0
_BACKOFF_BASE_SECONDS = 1.0
_BACKOFF_CAP_SECONDS = 20.0
# How a pass decides the provider itself is down rather than rejecting some
# rows. Bisecting a batch that holds bad rows fails at every level above the
# clean halves, so failures alone prove nothing about the provider. Before any
# call succeeds, a pass tolerates this many failures (a healthy provider with
# a tenth of its rows rejected succeeds long before); after a success, it is
# single-row calls failing in a row that mark an outage, because bad rows
# never make good single rows fail.
_FAILURES_BEFORE_ANY_SUCCESS_LIMIT = 128
_SINGLE_ROW_FAILURES_IN_ROW_LIMIT = 8
# When the store, not the data, is refusing writes. A refused batch write is
# halved like a refused provider batch, so failures above a bad row prove
# nothing: before any write lands a pass tolerates this many, and after one
# lands it is single-row writes failing in a row that mark a store fault.
_WRITE_FAILURES_BEFORE_ANY_SUCCESS_LIMIT = 16
_SINGLE_ROW_WRITE_FAILURES_IN_ROW_LIMIT = 3
# "Not now" answers a pass must collect, with nothing succeeding, before it
# reports the provider down rather than merely busy.
_TRANSIENT_FAILURES_FOR_OUTAGE = 4
# Rows the provider refused, remembered per plane until their text or the
# configured model changes, so they are not re-sent every pass.
_REJECTION_MEMORY_LIMIT = 2000
# Longest a single provider request may run. A batch that times out is split,
# a single row that times out backs off like throttling, and both stay well
# inside the lease margin.
_EMBED_CALL_TIMEOUT_SECONDS = 60.0

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
    DEPLOYMENT_STAMPS_DIFFER = "deployment_stamps_differ"
    DEPLOYMENT_STAMPS_MATCH = "deployment_stamps_match"
    DEPLOYMENT_MODEL_CHANGED = "deployment_model_changed"
    OTHER_PLANE_SWITCHED = "other_plane_switched"
    NO_PRIOR_EVIDENCE = "no_prior_evidence"
    OPERATOR_ADOPT = "operator_adopt"
    OPERATOR_REEMBED = "operator_reembed"


# A plane that adopted unstamped vectors with nothing to vouch for their model
# carries this warning until an operator re-embeds it.
LEGACY_WARNING_ADOPTED_WITHOUT_EVIDENCE = "adopted_without_evidence"
# A plane with no evidence of its own that adopted because other
# organizations' graph stamps name the configured model. Informational: that
# evidence is not the plane's own, and a tenant on the previous release could
# have forged the stamps behind it.
LEGACY_NOTICE_ADOPTED_ON_DEPLOYMENT_EVIDENCE = "adopted_on_deployment_evidence"


class EmbeddingSchemaPendingError(RuntimeError):
    """A namespace has not taken the sweep's evidence snapshot yet.

    Whoever runs the migration, nothing may read or rewrite embedding
    evidence before it: the snapshot must see the stamps the previous
    release left.
    """


@dataclass(frozen=True, slots=True)
class LegacyEvidence:
    """What predates this release says about the model behind a plane's unstamped vectors.

    ``differs`` and ``matches`` come from the stamps the plane's own rows
    carried when its schema upgraded. ``deployment_differs`` and
    ``deployment_matches`` say the same of the plane in the other
    organizations: one configuration embeds every organization's graph.
    ``model_changed`` means
    the deployment record saw this plane's configured model change since this
    release first ran. ``other_plane_switched`` means the other plane shows a
    switch: graph and content are configured separately, but a deployment
    that moved one provider has usually moved both.
    """

    differs: bool = False
    matches: bool = False
    deployment_differs: bool = False
    deployment_matches: bool = False
    model_changed: bool = False
    other_plane_switched: bool = False

    @property
    def switched(self) -> bool:
        """Whether this plane itself shows a model switch."""
        return self.differs or self.deployment_differs or self.model_changed


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
    rejected: int = 0
    legacy_decision: str | None = None
    warning: str | None = None
    notice: str | None = None
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
        differs = vector_identity_differs_predicate(self.metadata_path, "stamp")
        return f"{self.metadata_path} != NONE AND ({self.vector_field} = NONE OR {differs})"

    def walk_query(self) -> str:
        return (
            f"SELECT uuid, {self.projection} FROM {self.name} "
            f"WHERE {self.scope_field} = $scope AND uuid > $cursor "
            f"AND {self._candidate_predicate()} "
            "ORDER BY uuid ASC LIMIT $limit;"
        )

    def probe_query(self) -> str:
        return (
            f"SELECT uuid FROM {self.name} "
            f"WHERE {self.scope_field} = $scope AND {self._candidate_predicate()} "
            "ORDER BY uuid ASC LIMIT $limit;"
        )

    def pending_query(self) -> str:
        return (
            f"SELECT count() AS count FROM {self.name} "
            f"WHERE {self.scope_field} = $scope AND $remembered CONTAINSNOT uuid AND ("
            f"({self.vector_field} != NONE AND {self.metadata_path} = NONE) "
            f"OR ({self._candidate_predicate()})) GROUP ALL;"
        )

    def remembered_query(self) -> str:
        return (
            f"SELECT uuid FROM {self.name} "
            f"WHERE {self.scope_field} = $scope AND $remembered CONTAINS uuid "
            f"AND {self._candidate_predicate()};"
        )

    def legacy_probe_query(self) -> str:
        return (
            f"SELECT uuid FROM {self.name} "
            f"WHERE {self.scope_field} = $scope AND {self.vector_field} != NONE "
            f"AND {self.metadata_path} = NONE LIMIT 1;"
        )

    def adopt_query(self) -> str:
        return (
            f"UPDATE (SELECT VALUE id FROM {self.name} "
            f"WHERE {self.scope_field} = $scope AND {self.vector_field} != NONE "
            f"AND {self.metadata_path} = NONE LIMIT $limit) "
            f"SET {self.metadata_path} = $legacy RETURN uuid;"
        )

    def reembed_count_query(self) -> str:
        """Rows an operator re-embed would hand the sweep: every vector or stamp."""
        return (
            f"SELECT count() AS count FROM {self.name} "
            f"WHERE {self.scope_field} = $scope AND ({self.vector_field} != NONE "
            f"OR {self.metadata_path} != NONE) GROUP ALL;"
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
        # Every row write re-reads the plane's lease in the same statement, so
        # a pass that lost its lease to another process writes nothing.
        return (
            f"UPDATE (SELECT VALUE id FROM {self.name} "
            f"WHERE {self.scope_field} = $scope AND uuid IN $uuids) SET "
            f"{self.vector_field} = <array<float, {self.dimensions}>>"
            "$rows_by_uuid[uuid].embedding, "
            f"{self.metadata_path} = $rows_by_uuid[uuid].embedding_metadata "
            "WHERE (SELECT VALUE lease_owner FROM type::record($lease_key) "
            "WHERE lease_until > time::now())[0] = $owner "
            f"AND {self.fence} AND ({self.vector_field} = NONE "
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


@dataclass(slots=True)
class _Counts:
    checked: int = 0
    recovered: int = 0
    failed: int = 0
    skipped: int = 0
    adopted: int = 0
    rejected: int = 0
    successes: int = 0
    failures_before_success: int = 0
    single_row_failures_in_row: int = 0
    write_successes: int = 0
    write_failures_before_success: int = 0
    single_row_write_failures_in_row: int = 0
    transient_failures: int = 0
    backoff_exhausted: int = 0
    seen: set[str] = field(default_factory=set)
    rejections: dict[str, str] = field(default_factory=dict)
    owner: str = ""
    lease_lost: bool = False

    @property
    def provider_failing(self) -> bool:
        if self.successes == 0:
            return self.failures_before_success >= _FAILURES_BEFORE_ANY_SUCCESS_LIMIT
        return self.single_row_failures_in_row >= _SINGLE_ROW_FAILURES_IN_ROW_LIMIT

    @property
    def store_failing(self) -> bool:
        if self.write_successes == 0:
            return self.write_failures_before_success >= _WRITE_FAILURES_BEFORE_ANY_SUCCESS_LIMIT
        return self.single_row_write_failures_in_row >= _SINGLE_ROW_WRITE_FAILURES_IN_ROW_LIMIT

    @property
    def provider_unavailable(self) -> bool:
        return (
            self.successes == 0
            and self.backoff_exhausted > 0
            and self.transient_failures >= _TRANSIENT_FAILURES_FOR_OUTAGE
        )

    def write_failed(self, *, rows: int) -> None:
        if self.write_successes == 0:
            self.write_failures_before_success += 1
        if rows == 1:
            self.single_row_write_failures_in_row += 1

    def write_succeeded(self) -> None:
        self.write_successes += 1
        self.single_row_write_failures_in_row = 0

    def remembered_uuids(self, table: SweepTable) -> list[str]:
        prefix = f"{table.name}:"
        return [key.removeprefix(prefix) for key in self.rejections if key.startswith(prefix)]

    def keep_rejections(self, table: SweepTable, uuids: set[str]) -> None:
        prefix = f"{table.name}:"
        self.rejections = {
            key: digest
            for key, digest in self.rejections.items()
            if not key.startswith(prefix) or key.removeprefix(prefix) in uuids
        }

    def call_failed(self, *, rows: int) -> None:
        if self.successes == 0:
            self.failures_before_success += 1
        if rows == 1:
            self.single_row_failures_in_row += 1

    def call_succeeded(self) -> None:
        self.successes += 1
        self.single_row_failures_in_row = 0

    def remember_rejection(self, table: SweepTable, row: SweepRow) -> None:
        # Only a provider that has accepted other rows this pass is trusted to
        # be judging the text; a refusal before any success may be the
        # provider's own fault (credentials, model id) and is retried later.
        if self.successes and len(self.rejections) < _REJECTION_MEMORY_LIMIT:
            self.rejections[_row_key(table, row)] = _row_digest(row)

    def remembered(self, table: SweepTable, uuid: object) -> bool:
        return f"{table.name}:{uuid}" in self.rejections

    def known_rejection(self, table: SweepTable, row: SweepRow) -> bool:
        return self.rejections.get(_row_key(table, row)) == _row_digest(row)

    def rejections_for(self, table: SweepTable) -> int:
        prefix = f"{table.name}:"
        return sum(1 for key in self.rejections if key.startswith(prefix))


def _row_key(table: SweepTable, row: SweepRow) -> str:
    return f"{table.name}:{row['uuid']}"


def _row_digest(row: SweepRow) -> str:
    """What the provider was shown: every walked column except the identity."""
    material = {key: value for key, value in row.items() if key != "uuid"}
    return hashlib.sha256(json.dumps(material, sort_keys=True, default=str).encode()).hexdigest()[
        :32
    ]


def _stamp_digest(stamp: EmbeddingStamp) -> str:
    identity = {field: stamp.get(field) for field in VECTOR_IDENTITY_FIELDS}
    return hashlib.sha256(json.dumps(identity, sort_keys=True, default=str).encode()).hexdigest()[
        :32
    ]


def _load_rejections(state: Mapping[str, Any], stamp: EmbeddingStamp) -> dict[str, str]:
    memory = state.get("rejections")
    if not isinstance(memory, Mapping) or memory.get("stamp") != _stamp_digest(stamp):
        return {}
    rows = memory.get("rows")
    if not isinstance(rows, Mapping):
        return {}
    return {str(key): str(value) for key, value in rows.items()}


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


class _DeadlineReachedError(Exception):
    """The pass budget is spent (or the lease lost); no further provider call may start."""


class _SplitRequiredError(Exception):
    """A multi-row request ran past the call timeout; smaller requests may fit."""


def configured_legacy_vector_policy() -> str:
    raw = os.getenv("SIBYL_EMBEDDING_LEGACY_VECTORS", "").strip().lower()
    return raw or settings.embedding_legacy_vectors


def decide_legacy_vectors(
    *, legacy_rows: bool, evidence: LegacyEvidence | None, policy: str
) -> tuple[LegacyVectorDecision, LegacyVectorBasis]:
    """Classify a plane's unstamped vectors.

    An operator policy wins. Otherwise any evidence of a switch wins over any
    evidence of continuity, because re-embedding a plane that did not change
    costs provider calls while adopting one that did corrupts search: the
    plane's own pre-upgrade stamps naming another model, then another
    organization's, then the deployment record, then the other plane. With
    no switch in sight, stamps that name the configured model (the plane's
    own, then another organization's) adopt the vectors as what the unchanged
    configuration produced. Only with no evidence anywhere are they adopted
    with a warning.
    """
    if not legacy_rows:
        return LegacyVectorDecision.NONE, LegacyVectorBasis.NO_LEGACY_ROWS
    if policy == "adopt":
        return LegacyVectorDecision.ADOPT, LegacyVectorBasis.OPERATOR_ADOPT
    if policy == "reembed":
        return LegacyVectorDecision.REEMBED, LegacyVectorBasis.OPERATOR_REEMBED
    evidence = evidence or LegacyEvidence()
    if evidence.differs:
        return LegacyVectorDecision.REEMBED, LegacyVectorBasis.PRIOR_STAMPS_DIFFER
    if evidence.deployment_differs:
        return LegacyVectorDecision.REEMBED, LegacyVectorBasis.DEPLOYMENT_STAMPS_DIFFER
    if evidence.model_changed:
        return LegacyVectorDecision.REEMBED, LegacyVectorBasis.DEPLOYMENT_MODEL_CHANGED
    if evidence.other_plane_switched:
        return LegacyVectorDecision.REEMBED, LegacyVectorBasis.OTHER_PLANE_SWITCHED
    if evidence.matches:
        return LegacyVectorDecision.ADOPT, LegacyVectorBasis.PRIOR_STAMPS_MATCH
    if evidence.deployment_matches:
        return LegacyVectorDecision.ADOPT, LegacyVectorBasis.DEPLOYMENT_STAMPS_MATCH
    return LegacyVectorDecision.ADOPT, LegacyVectorBasis.NO_PRIOR_EVIDENCE


async def ensure_legacy_decision(
    plane: SweepPlane,
    *,
    defer_unproven: bool = False,
    defer_limit_seconds: float | None = None,
) -> dict[str, Any]:
    """Persist the plane's legacy verdict if no pass has recorded one yet.

    The first writer wins, so a racing pass reads the same verdict back.
    With ``defer_unproven``, a verdict that would adopt with no evidence at
    all is not recorded; the returned state carries ``legacy_deferred`` so
    the caller can settle it once more of the deployment has been read. The
    first deferral is persisted, and once ``defer_limit_seconds`` have
    passed since it the verdict is recorded on the evidence at hand.
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
    unproven = basis is LegacyVectorBasis.NO_PRIOR_EVIDENCE
    if unproven and defer_unproven:
        deferred = normalize_records(
            await plane.execute(
                "UPDATE type::record($key) SET legacy_deferred_at = "
                "legacy_deferred_at ?? time::now(), updated_at = time::now() "
                "WHERE legacy_decision = NONE RETURN legacy_deferred_at, "
                "duration::secs(time::now() - legacy_deferred_at) AS deferred_age_seconds;",
                key=plane.state_key,
            )
        )
        age = deferred[0].get("deferred_age_seconds") if deferred else None
        waited = isinstance(age, int | float) and (
            defer_limit_seconds is not None and age >= defer_limit_seconds
        )
        if not deferred:
            return await _read_state(plane)
        if not waited:
            return {**state, **deferred[0], "legacy_deferred": True}
    rows = normalize_records(
        await plane.execute(
            "UPDATE type::record($key) SET legacy_decision = $decision, "
            "legacy_basis = $basis, legacy_metadata = $legacy, legacy_warning = $warning, "
            "legacy_notice = $notice, legacy_deferred_at = NONE, decided_at = time::now(), "
            "updated_at = time::now() WHERE legacy_decision = NONE RETURN AFTER;",
            key=plane.state_key,
            decision=decision.value,
            basis=basis.value,
            legacy=legacy,
            warning=LEGACY_WARNING_ADOPTED_WITHOUT_EVIDENCE if unproven else None,
            notice=(
                LEGACY_NOTICE_ADOPTED_ON_DEPLOYMENT_EVIDENCE
                if basis is LegacyVectorBasis.DEPLOYMENT_STAMPS_MATCH
                else None
            ),
        )
    )
    if rows:
        fields = {
            "organization_id": plane.organization_id,
            "plane": plane.name,
            "decision": decision.value,
            "basis": basis.value,
            "provider": plane.stamp.get("provider"),
            "model": plane.stamp.get("model"),
            "dimensions": plane.stamp.get("dimensions"),
        }
        if unproven:
            log.warning(
                "embedding_legacy_vectors_adopted_without_evidence",
                **fields,
                remediation=(
                    "if the embedding model changed in this deploy, run "
                    "`sibyld db reembed --org-id <org> --plane "
                    f"{'graph' if plane.name == 'graph' else 'documents'} --yes`"
                ),
            )
        else:
            log.info("embedding_legacy_vectors_decided", **fields)
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
        skipped = result(status=SWEEP_SKIPPED_DIMENSION_MISMATCH)
        await _record_skip(plane, skipped)
        return skipped

    owner = f"{socket.gethostname()}:{os.getpid()}:{uuid4().hex[:12]}"
    state = await ensure_legacy_decision(plane)
    decision = state.get("legacy_decision")
    legacy_decision = str(decision) if decision else None
    warning = state.get("legacy_warning") or None
    notice = state.get("legacy_notice") or None
    if _plane_current(state, plane.stamp, verify_interval):
        return result(
            status=SWEEP_CURRENT, legacy_decision=legacy_decision, warning=warning, notice=notice
        )
    if not await _acquire_lease(plane, owner=owner, budget=budget):
        return result(
            status=SWEEP_BUSY, legacy_decision=legacy_decision, warning=warning, notice=notice
        )

    counts = _Counts(rejections=_load_rejections(state, plane.stamp), owner=owner)
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
                if counts.provider_failing:
                    status = SWEEP_PROVIDER_FAILING
                    break
                if counts.store_failing:
                    status = SWEEP_STORE_FAILING
                    break
                if counts.lease_lost:
                    status = SWEEP_LEASE_LOST
                    break
            if status in {SWEEP_PROVIDER_FAILING, SWEEP_STORE_FAILING, SWEEP_LEASE_LOST}:
                break
            await _save_cursors(plane, owner=owner, cursors=cursors, budget=budget)
        complete = adopted_all and clean == set(walkers) and not counts.lease_lost
        if complete:
            status = SWEEP_COMPLETED
        elif status == SWEEP_PARTIAL and counts.provider_unavailable:
            # Every attempt this pass was told "not now" until the budget ran out.
            status = SWEEP_PROVIDER_FAILING
        await _forget_settled_rejections(plane, counts)
        pending = 0 if complete else await _pending(plane, counts)
    except BaseException:
        await _release(
            plane,
            owner=owner,
            cursors=cursors,
            receipt=None,
            complete=None,
            generation=generation,
            rejections=counts.rejections,
        )
        raise
    outcome = result(
        legacy_decision=legacy_decision,
        warning=warning,
        notice=notice,
        status=status,
        checked=counts.checked,
        recovered=counts.recovered,
        pending=pending,
        failed=counts.failed,
        adopted=counts.adopted,
        skipped=counts.skipped,
        rejected=counts.rejected,
        elapsed_ms=round((time.monotonic() - started) * 1000, 2),
    )
    await _release(
        plane,
        owner=owner,
        cursors=cursors,
        receipt=outcome.receipt(),
        complete=dict(plane.stamp) if complete else None,
        generation=generation,
        rejections=counts.rejections,
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
        rejected=counts.rejected,
        failed=counts.failed,
        concurrency=limiter.limit,
        warning=warning,
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
    # Remembered rejections stay candidates; one more row than there are of
    # them is enough to prove whether anything else is left.
    remembered = counts.rejections_for(walk.table)
    leftover = await _rows(plane, walk.table.probe_query(), limit=remembered + 1)
    return all(counts.remembered(walk.table, row.get("uuid")) for row in leftover)


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
    fresh: list[SweepRow] = []
    for row in rows:
        key = _row_key(table, row)
        if key not in counts.seen:
            counts.seen.add(key)
            counts.checked += 1
        if counts.known_rejection(table, row):
            counts.rejected += 1
        else:
            fresh.append(row)
    batches = [list(fresh[start : start + batch]) for start in range(0, len(fresh), batch)]
    outcomes = await asyncio.gather(
        *(
            _embed_and_write(plane, table, chunk, counts=counts, limiter=limiter, deadline=deadline)
            for chunk in batches
        ),
        return_exceptions=True,
    )
    for chunk, outcome in zip(batches, outcomes, strict=True):
        if isinstance(outcome, asyncio.CancelledError):
            raise outcome
        if isinstance(outcome, BaseException):
            # A store write failed: the vectors were computed but not kept.
            counts.failed += len(chunk)
            log.warning(
                "embedding_sweep_write_failed",
                organization_id=plane.organization_id,
                plane=plane.name,
                table=table.name,
                rows=len(chunk),
                error_type=type(outcome).__name__,
            )
            continue
        counts.recovered += outcome.written
        counts.failed += outcome.failed
        counts.skipped += outcome.skipped


@dataclass(frozen=True, slots=True)
class _BatchOutcome:
    written: int = 0
    failed: int = 0
    skipped: int = 0

    def __add__(self, other: _BatchOutcome) -> _BatchOutcome:
        return _BatchOutcome(
            self.written + other.written,
            self.failed + other.failed,
            self.skipped + other.skipped,
        )


async def _embed_and_write(
    plane: SweepPlane,
    table: SweepTable,
    rows: list[SweepRow],
    *,
    counts: _Counts,
    limiter: _AdaptiveLimiter,
    deadline: float,
) -> _BatchOutcome:
    """Embed and store one batch, isolating rows the provider rejects.

    A rejected batch is split in half and each half retried, down to single
    rows, so one text the provider refuses cannot keep the other rows of its
    batch stale forever; a batch that times out is split the same way. Only a
    single row the provider refuses counts as failed. Rows left unattempted
    (budget spent, provider judged down) are skipped and stay pending.
    """
    if counts.provider_failing or counts.store_failing or counts.lease_lost:
        return _BatchOutcome(skipped=len(rows))
    try:
        vectors, stamp = await _embed_with_backoff(
            plane, table, rows, counts=counts, limiter=limiter, deadline=deadline
        )
    except _DeadlineReachedError:
        return _BatchOutcome(skipped=len(rows))
    except _BudgetExhaustedError:
        counts.backoff_exhausted += 1
        return _BatchOutcome(skipped=len(rows))
    except _SplitRequiredError:
        return await _bisect(plane, table, rows, counts=counts, limiter=limiter, deadline=deadline)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        counts.call_failed(rows=len(rows))
        if len(rows) == 1:
            counts.remember_rejection(table, rows[0])
            log.warning(
                "embedding_sweep_row_rejected",
                organization_id=plane.organization_id,
                plane=plane.name,
                table=table.name,
                row=str(rows[0].get("uuid")),
                error_type=type(exc).__name__,
            )
            return _BatchOutcome(failed=1)
        if counts.provider_failing:
            return _BatchOutcome(skipped=len(rows))
        return await _bisect(plane, table, rows, counts=counts, limiter=limiter, deadline=deadline)
    counts.call_succeeded()
    rows_by_uuid = {
        str(row["uuid"]): {
            **row,
            "embedding": [float(value) for value in vector],
            "embedding_metadata": dict(stamp),
        }
        for row, vector in zip(rows, vectors, strict=True)
    }
    return await _write_vectors(plane, table, rows_by_uuid, counts=counts)


async def _write_vectors(
    plane: SweepPlane,
    table: SweepTable,
    rows_by_uuid: dict[str, dict[str, Any]],
    *,
    counts: _Counts,
) -> _BatchOutcome:
    """Store computed vectors, halving a refused write to find the row it refuses.

    The vectors are already paid for, so isolating a row the store rejects
    (an older row the current schema no longer accepts) costs writes only.
    """
    if counts.store_failing or counts.lease_lost:
        return _BatchOutcome(skipped=len(rows_by_uuid))
    try:
        written = await _rows(
            plane,
            table.write_query(),
            uuids=list(rows_by_uuid),
            rows_by_uuid=rows_by_uuid,
            lease_key=plane.state_key,
            owner=counts.owner,
        )
    except Exception as exc:
        counts.write_failed(rows=len(rows_by_uuid))
        if len(rows_by_uuid) == 1:
            log.warning(
                "embedding_sweep_write_failed",
                organization_id=plane.organization_id,
                plane=plane.name,
                table=table.name,
                row=next(iter(rows_by_uuid)),
                error_type=type(exc).__name__,
            )
            return _BatchOutcome(failed=1)
        if counts.store_failing:
            return _BatchOutcome(skipped=len(rows_by_uuid))
        keys = list(rows_by_uuid)
        middle = len(keys) // 2
        left = await _write_vectors(
            plane, table, {key: rows_by_uuid[key] for key in keys[:middle]}, counts=counts
        )
        right = await _write_vectors(
            plane, table, {key: rows_by_uuid[key] for key in keys[middle:]}, counts=counts
        )
        return left + right
    if len(written) < len(rows_by_uuid) and not await _lease_held(plane, counts.owner):
        # Rows also drop out when their text moved under the vector; only a
        # lost lease stops the pass.
        counts.lease_lost = True
        log.warning(
            "embedding_sweep_lease_lost",
            organization_id=plane.organization_id,
            plane=plane.name,
            table=table.name,
            unwritten=len(rows_by_uuid) - len(written),
        )
    counts.write_succeeded()
    return _BatchOutcome(written=len(written), skipped=len(rows_by_uuid) - len(written))


async def _lease_held(plane: SweepPlane, owner: str) -> bool:
    rows = await _rows(
        plane,
        "SELECT lease_owner FROM type::record($key) WHERE lease_until > time::now();",
        key=plane.state_key,
    )
    return bool(rows) and rows[0].get("lease_owner") == owner


async def _bisect(
    plane: SweepPlane,
    table: SweepTable,
    rows: list[SweepRow],
    *,
    counts: _Counts,
    limiter: _AdaptiveLimiter,
    deadline: float,
) -> _BatchOutcome:
    middle = len(rows) // 2
    left, right = await asyncio.gather(
        _embed_and_write(
            plane, table, rows[:middle], counts=counts, limiter=limiter, deadline=deadline
        ),
        _embed_and_write(
            plane, table, rows[middle:], counts=counts, limiter=limiter, deadline=deadline
        ),
    )
    return left + right


async def _embed_with_backoff(
    plane: SweepPlane,
    table: SweepTable,
    rows: list[SweepRow],
    *,
    counts: _Counts,
    limiter: _AdaptiveLimiter,
    deadline: float,
) -> tuple[list[list[float]], EmbeddingStamp]:
    """One provider call, retried with backoff while the provider signals capacity.

    No call starts once the pass budget is spent, including a call that waited
    for a concurrency slot and the halves of a split batch, so a pass always
    ends well inside its lease.
    """
    attempt = 0
    while True:
        if time.monotonic() >= deadline or counts.lease_lost:
            raise _DeadlineReachedError
        async with limiter:
            if time.monotonic() >= deadline or counts.lease_lost:
                raise _DeadlineReachedError
            try:
                vectors, stamp = await asyncio.wait_for(
                    plane.embed(table, rows), timeout=_EMBED_CALL_TIMEOUT_SECONDS
                )
            except TimeoutError as exc:
                limiter.throttled()
                counts.transient_failures += 1
                if len(rows) > 1:
                    raise _SplitRequiredError from exc
                log.info(
                    "embedding_sweep_throttled",
                    organization_id=plane.organization_id,
                    plane=plane.name,
                    table=table.name,
                    concurrency=limiter.limit,
                    error_type=type(exc).__name__,
                )
            except Exception as exc:
                if not is_transient_provider_error(exc):
                    raise
                limiter.throttled()
                counts.transient_failures += 1
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
    return vectors, stamp


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


async def _pending(plane: SweepPlane, counts: _Counts) -> int:
    """Rows still owed a vector, not counting rows the provider refused."""
    total = 0
    for table in plane.tables:
        rows = await _rows(plane, table.pending_query(), remembered=counts.remembered_uuids(table))
        total += int(rows[0].get("count") or 0) if rows else 0
    return total


async def _forget_settled_rejections(plane: SweepPlane, counts: _Counts) -> None:
    """Drop remembered refusals whose rows were deleted, edited away or embedded."""
    for table in plane.tables:
        remembered = counts.remembered_uuids(table)
        if not remembered:
            continue
        rows = await _rows(plane, table.remembered_query(), remembered=remembered)
        counts.keep_rejections(table, {str(row.get("uuid")) for row in rows})


def _plane_current(state: Mapping[str, Any], stamp: EmbeddingStamp, interval: float) -> bool:
    if interval <= 0 or not same_vector_identity(state.get("complete_metadata"), stamp):
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
    "ELSE duration::secs(time::now() - complete_at) END AS complete_age_seconds, "
    "IF legacy_deferred_at = NONE THEN NONE "
    "ELSE duration::secs(time::now() - legacy_deferred_at) END AS deferred_age_seconds"
)


async def _ensure_state(plane: SweepPlane) -> dict[str, Any]:
    state = await _read_state(plane)
    if state:
        return state
    await plane.execute(
        "UPSERT type::record($key) SET organization_id = $organization_id, "
        "plane = $plane, updated_at = time::now() RETURN NONE;",
        key=plane.state_key,
        organization_id=plane.organization_id,
        plane=plane.name,
    )
    return await _read_state(plane)


async def _record_skip(plane: SweepPlane, skipped: EmbeddingSweepResult) -> None:
    """Leave a receipt for a pass refused before it could start, for status surfaces."""
    await _ensure_state(plane)
    await plane.execute(
        "UPDATE type::record($key) SET active_metadata = $stamp, last_run = $receipt, "
        "updated_at = time::now() RETURN NONE;",
        key=plane.state_key,
        stamp=plane.stamp,
        receipt={**skipped.receipt(), "finished_at": datetime.now(UTC)},
    )


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


async def _save_cursors(
    plane: SweepPlane, *, owner: str, cursors: Mapping[str, str], budget: float
) -> None:
    # Saving progress also renews the lease, so a pass still working is never
    # mistaken for a dead one by the next tick.
    await plane.execute(
        "UPDATE type::record($key) SET cursors = $cursors, "
        "lease_until = time::now() + <duration>$lease, updated_at = time::now() "
        "WHERE lease_owner = $owner RETURN NONE;",
        key=plane.state_key,
        owner=owner,
        cursors=dict(cursors),
        lease=f"{int(budget + _LEASE_MARGIN_SECONDS)}s",
    )


async def _release(
    plane: SweepPlane,
    *,
    owner: str,
    cursors: Mapping[str, str],
    receipt: Mapping[str, Any] | None,
    complete: EmbeddingStamp | None,
    generation: int,
    rejections: Mapping[str, str],
) -> None:
    try:
        await plane.execute(
            # A reopen during this pass forgot the plane's refusals on purpose;
            # the generation it moved keeps this pass from writing them back.
            "UPDATE type::record($key) SET cursors = $cursors, "
            "rejections = IF (generation ?? 0) = $generation THEN $rejections "
            "ELSE rejections END, "
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
            rejections={"stamp": _stamp_digest(plane.stamp), "rows": dict(rejections)},
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
    # The operator has now vouched for nothing in this plane being trusted
    # blindly, so an adoption warning no longer applies.
    await execute(
        REOPEN_EMBEDDING_STATES.replace(
            "updated_at = time::now()",
            "legacy_warning = NONE, legacy_notice = NONE, updated_at = time::now()",
        ).replace(
            "WHERE $organizations CONTAINS organization_id",
            "WHERE $organizations CONTAINS organization_id AND plane = $plane",
        ),
        organizations=[organization_id],
        plane=plane,
    )
    return marked


async def count_plane_reembed(
    *,
    organization_id: str,
    execute: SweepExecute,
    tables: Sequence[SweepTable],
) -> int:
    """How many rows ``mark_plane_for_reembed`` would send through the sweep."""
    total = 0
    for table in tables:
        rows = normalize_records(await execute(table.reembed_count_query(), scope=organization_id))
        count = rows[0].get("count") if rows else None
        total += count if isinstance(count, int) else 0
    return total


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
    "LEGACY_NOTICE_ADOPTED_ON_DEPLOYMENT_EVIDENCE",
    "LEGACY_WARNING_ADOPTED_WITHOUT_EVIDENCE",
    "SWEEP_BUSY",
    "SWEEP_COMPLETED",
    "SWEEP_CURRENT",
    "SWEEP_LEASE_LOST",
    "SWEEP_PARTIAL",
    "SWEEP_PROVIDER_FAILING",
    "SWEEP_SKIPPED_DIMENSION_MISMATCH",
    "SWEEP_SKIPPED_NO_PROVIDER",
    "SWEEP_SKIPPED_SCHEMA_PENDING",
    "SWEEP_STORE_FAILING",
    "EmbedRows",
    "EmbeddingSchemaPendingError",
    "EmbeddingSweepResult",
    "LegacyEvidence",
    "LegacyVectorBasis",
    "LegacyVectorDecision",
    "SweepPlane",
    "SweepTable",
    "configured_legacy_vector_policy",
    "count_plane_reembed",
    "decide_legacy_vectors",
    "embedding_state_key",
    "ensure_legacy_decision",
    "mark_plane_for_reembed",
    "read_embedding_sweep_state",
    "run_embedding_sweep",
]
