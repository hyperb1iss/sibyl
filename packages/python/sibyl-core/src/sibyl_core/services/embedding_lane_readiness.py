"""How a vector lane should read a plane right now.

Two questions, answered from the plane's persisted sweep state.

Which stored vectors count as the query's model. Lanes filter by model
inside the HNSW walk, so a vector without a stamp is invisible to them. Once
a plane's verdict adopted its unstamped vectors as the configured model,
they are the query's model, so they count until adoption stamps them (one
pass, in bulk). Before the verdict is recorded they never count: the verdict
waits until every organization has published its evidence, and one that has
not may hold the stamps that show a switch, so counting them early could
score one model's vectors against another's queries. The price is a
lexical-only window for those vectors until the first lifecycle pass records
the verdict. An answer given before the verdict is never cached, so the
verdict takes effect on the next query.

Whether the lane can pay off at all. Only once the model has changed (the
plane last finished a pass for another model, its unstamped vectors were
judged another model's, or its own pre-upgrade records name another model)
can almost nothing be in the query's model. A walk that must skip nearly
every vector to collect k matches costs a second or more per lane and
returns little or nothing: measured on a native server at 20,000 entities,
lanes took 1 to 2 seconds for zero rows at a fresh switch, 0.3 to 1 second
with 1% of rows converted, 0.1 to 0.5 seconds from 5%, and 13 to 40
milliseconds fully swept. So after a switch a lane stands aside while fewer
than ``LANE_MIN_IN_MODEL_FRACTION`` of the plane's vectors are in the
query's model, leaving the query to the lexical lanes. A plane that never
switched never skips. The state is re-read every ``_CACHE_SECONDS``, so
lanes resume on their own as the sweep passes the threshold.

When the state is missing or unreadable, the lane runs: skipping is an
optimization, never a guess.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

import structlog

from sibyl_core.config import settings
from sibyl_core.embeddings.provenance import same_vector_space, vector_space
from sibyl_core.services.embedding_sweep import (
    LegacyEvidence,
    LegacyVectorBasis,
    LegacyVectorDecision,
    SweepExecute,
    decide_legacy_vectors,
    read_embedding_sweep_state,
)

log = structlog.get_logger()

# Below this share of a plane's vectors in the query's model a lane costs more
# than it finds; at or above it the measured lanes answered within half a second.
LANE_MIN_IN_MODEL_FRACTION = 0.05
_CACHE_SECONDS = 30.0

LANE_READY_COMPLETE = "complete"
LANE_READY_UNKNOWN = "unknown"
LANE_READY_ADOPTING = "adopting"
LANE_READY_CONVERTING = "converting"
LANE_SKIPPED_SWITCHED = "model_switched"
LANE_SKIPPED_SPARSE = "model_sparse"

type Provisional = tuple[LegacyVectorDecision, LegacyVectorBasis]
type ProvisionalVerdict = Callable[[], Awaitable[Provisional]]


@dataclass(frozen=True, slots=True)
class LaneReadiness:
    run: bool
    reason: str
    in_model_fraction: float | None = None
    # Unstamped vectors count as the query's model: a recorded verdict
    # adopted them as it (or found none).
    admit_unstamped: bool = False


_cache: dict[tuple[str, str, str], tuple[float, LaneReadiness]] = {}


def reset_lane_readiness_cache() -> None:
    _cache.clear()


def _own_snapshot_verdict(
    state: Mapping[str, Any], query_stamp: Mapping[str, Any], legacy_policy: str
) -> Provisional:
    """The verdict the plane's own pre-upgrade stamps point to, on their own."""
    snapshot = state.get("legacy_evidence")
    raw = snapshot.get("stamps") if isinstance(snapshot, Mapping) else None
    stamps = [stamp for stamp in raw if isinstance(stamp, Mapping)] if isinstance(raw, list) else []
    differs = any(not same_vector_space(stamp, query_stamp) for stamp in stamps)
    return decide_legacy_vectors(
        legacy_rows=True,
        evidence=LegacyEvidence(differs=differs, matches=bool(stamps) and not differs),
        policy=legacy_policy,
    )


def judge_lane_readiness(
    state: Mapping[str, Any],
    query_stamp: Mapping[str, Any],
    *,
    provisional: Provisional | None = None,
    legacy_policy: str = "auto",
) -> LaneReadiness:
    """Decide from a plane's persisted sweep state how its vector lane should read.

    ``provisional`` is the verdict the plane's own pre-upgrade evidence points
    to, consulted only while none is recorded and only to skip a lane that
    evidently switched; without it the graph state row's snapshot stands in.
    """
    complete = state.get("complete_metadata")
    if same_vector_space(complete, query_stamp):
        return LaneReadiness(run=True, reason=LANE_READY_COMPLETE, in_model_fraction=1.0)
    recorded = state.get("legacy_decision") or None
    adopted = state.get("legacy_metadata")
    if recorded is not None:
        decision = LegacyVectorDecision(recorded)
    else:
        decision, _basis = provisional or _own_snapshot_verdict(state, query_stamp, legacy_policy)
    switched = (
        decision == LegacyVectorDecision.REEMBED
        or isinstance(complete, Mapping)
        or (isinstance(adopted, Mapping) and not same_vector_space(adopted, query_stamp))
    )
    if not switched:
        # Only a recorded verdict lets unstamped vectors in; one still pending
        # may yet go the other way on evidence not published so far.
        if recorded == LegacyVectorDecision.ADOPT:
            return LaneReadiness(run=True, reason=LANE_READY_ADOPTING, admit_unstamped=True)
        return LaneReadiness(
            run=True,
            reason=LANE_READY_UNKNOWN,
            admit_unstamped=recorded == LegacyVectorDecision.NONE,
        )
    last_run = state.get("last_run")
    if isinstance(last_run, Mapping) and same_vector_space(last_run.get("space"), query_stamp):
        in_model = last_run.get("in_model")
        pending = last_run.get("pending")
        if isinstance(in_model, int) and isinstance(pending, int) and in_model + pending > 0:
            fraction = in_model / (in_model + pending)
            if fraction < LANE_MIN_IN_MODEL_FRACTION:
                return LaneReadiness(
                    run=False, reason=LANE_SKIPPED_SPARSE, in_model_fraction=fraction
                )
            return LaneReadiness(run=True, reason=LANE_READY_CONVERTING, in_model_fraction=fraction)
    # The model changed and no pass has counted progress toward this one.
    return LaneReadiness(run=False, reason=LANE_SKIPPED_SWITCHED, in_model_fraction=0.0)


async def vector_lane_readiness(
    *,
    plane: str,
    organization_id: str,
    execute: SweepExecute,
    query_stamp: Mapping[str, Any],
    provisional: ProvisionalVerdict | None = None,
) -> LaneReadiness:
    """The cached readiness of one organization's plane for queries in one model."""
    space = str(sorted((vector_space(query_stamp) or {}).items()))
    key = (plane, organization_id, space)
    now = time.monotonic()
    cached = _cache.get(key)
    if cached is not None and cached[0] > now:
        return cached[1]
    try:
        state = await read_embedding_sweep_state(plane, organization_id, execute)
        verdict = (
            await provisional()
            if provisional is not None and not state.get("legacy_decision")
            else None
        )
    except Exception as exc:
        log.debug("vector_lane_readiness_unavailable", plane=plane, error_type=type(exc).__name__)
        # Unreadable state says nothing about a switch; the strict filter
        # stays, and so does the lane.
        readiness = LaneReadiness(run=True, reason=LANE_READY_UNKNOWN)
    else:
        readiness = judge_lane_readiness(
            state,
            query_stamp,
            provisional=verdict,
            legacy_policy=settings.embedding_legacy_vectors,
        )
        # A verdict can be recorded at any moment before this; an answer given
        # without one is not kept, so the next query sees the verdict.
        if state.get("legacy_decision"):
            _cache[key] = (now + _CACHE_SECONDS, readiness)
    if not readiness.run:
        log.info(
            "vector_lane_skipped",
            plane=plane,
            organization_id=organization_id,
            reason=readiness.reason,
            in_model_fraction=readiness.in_model_fraction,
        )
    return readiness


async def chunk_vector_lane_readiness(
    client: Any, organization_id: str, query_stamp: Mapping[str, Any] | None
) -> LaneReadiness:
    """How one organization's chunk vector lane should read for a query in this model."""
    if query_stamp is None:
        return LaneReadiness(run=True, reason=LANE_READY_UNKNOWN)
    from sibyl_core.services import content_client
    from sibyl_core.services.document_embedding_sweep import DOCUMENT_CHUNK_EMBEDDING_PLANE
    from sibyl_core.services.embedding_evidence import (
        classify_partial_stamps,
        read_chunk_evidence,
    )

    async def execute(query: str, **params: object) -> object:
        return await content_client.select_many(client, query, **params)

    async def provisional() -> Provisional:
        # The chunk plane's own pre-upgrade evidence, to skip a lane that
        # evidently switched; it never lets unstamped chunks in.
        differs, matches = classify_partial_stamps(
            await read_chunk_evidence(execute), dict(query_stamp)
        )
        return decide_legacy_vectors(
            legacy_rows=True,
            evidence=LegacyEvidence(differs=differs, matches=matches),
            policy=settings.embedding_legacy_vectors,
        )

    return await vector_lane_readiness(
        plane=DOCUMENT_CHUNK_EMBEDDING_PLANE,
        organization_id=organization_id,
        execute=execute,
        query_stamp=query_stamp,
        provisional=provisional,
    )


__all__ = [
    "LANE_MIN_IN_MODEL_FRACTION",
    "LANE_READY_ADOPTING",
    "LANE_READY_COMPLETE",
    "LANE_READY_CONVERTING",
    "LANE_READY_UNKNOWN",
    "LANE_SKIPPED_SPARSE",
    "LANE_SKIPPED_SWITCHED",
    "LaneReadiness",
    "chunk_vector_lane_readiness",
    "judge_lane_readiness",
    "reset_lane_readiness_cache",
    "vector_lane_readiness",
]
