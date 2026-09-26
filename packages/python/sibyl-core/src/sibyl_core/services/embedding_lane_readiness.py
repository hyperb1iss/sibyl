"""Whether a vector lane can find anything in the query's model right now.

Right after a provider switch almost no stored vector is in the new model's
space. The lanes filter by model inside the HNSW walk, so they stay correct,
but a walk that must skip nearly every vector to collect k matches costs a
second or more per lane and returns little or nothing: measured on a native
server at 20,000 entities, lanes took 1 to 2 seconds for zero rows at a
fresh switch, 0.3 to 1 second with 1% of rows converted, 0.1 to 0.5 seconds
from 5%, and 13 to 40 milliseconds fully swept. The embedding sweep already
knows how far it has come, so a lane consults the plane's sweep state and
stands aside while fewer than ``LANE_MIN_IN_MODEL_FRACTION`` of the plane's
vectors are in the query's model, leaving the query to the lexical lanes.
The state is re-read every ``_CACHE_SECONDS``, so lanes resume on their own as the sweep
passes the threshold and are fully back when the plane completes.

When the state is missing or unreadable, the lane runs: skipping is an
optimization, never a guess.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import structlog

from sibyl_core.embeddings.provenance import same_vector_space, vector_space
from sibyl_core.services.embedding_sweep import SweepExecute, read_embedding_sweep_state

log = structlog.get_logger()

# Below this share of a plane's vectors in the query's model a lane costs more
# than it finds; at or above it the measured lanes answered within half a second.
LANE_MIN_IN_MODEL_FRACTION = 0.05
_CACHE_SECONDS = 30.0

LANE_READY_COMPLETE = "complete"
LANE_READY_UNKNOWN = "unknown"
LANE_READY_CONVERTING = "converting"
LANE_SKIPPED_SWITCHED = "model_switched"
LANE_SKIPPED_SPARSE = "model_sparse"


@dataclass(frozen=True, slots=True)
class LaneReadiness:
    run: bool
    reason: str
    in_model_fraction: float | None = None


_cache: dict[tuple[str, str, str], tuple[float, LaneReadiness]] = {}


def reset_lane_readiness_cache() -> None:
    _cache.clear()


def judge_lane_readiness(state: Mapping[str, Any], query_stamp: Mapping[str, Any]) -> LaneReadiness:
    """Decide from a plane's persisted sweep state whether its vector lane can pay off."""
    if not state:
        return LaneReadiness(run=True, reason=LANE_READY_UNKNOWN)
    complete = state.get("complete_metadata")
    if same_vector_space(complete, query_stamp):
        return LaneReadiness(run=True, reason=LANE_READY_COMPLETE, in_model_fraction=1.0)
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
    if isinstance(complete, Mapping):
        # Every vector was last completed for another model and no pass has
        # counted progress toward this one: the provider just switched.
        return LaneReadiness(run=False, reason=LANE_SKIPPED_SWITCHED, in_model_fraction=0.0)
    return LaneReadiness(run=True, reason=LANE_READY_UNKNOWN)


async def vector_lane_readiness(
    *,
    plane: str,
    organization_id: str,
    execute: SweepExecute,
    query_stamp: Mapping[str, Any],
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
    except Exception as exc:
        log.debug("vector_lane_readiness_unavailable", plane=plane, error_type=type(exc).__name__)
        readiness = LaneReadiness(run=True, reason=LANE_READY_UNKNOWN)
    else:
        readiness = judge_lane_readiness(state, query_stamp)
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


async def chunk_vector_lane_ready(
    client: Any, organization_id: str, query_stamp: Mapping[str, Any] | None
) -> bool:
    """Whether an organization's chunk vector lane can pay off for a query in this model."""
    if query_stamp is None:
        return True
    from sibyl_core.services import content_client
    from sibyl_core.services.document_embedding_sweep import DOCUMENT_CHUNK_EMBEDDING_PLANE

    async def execute(query: str, **params: object) -> object:
        return await content_client.select_many(client, query, **params)

    readiness = await vector_lane_readiness(
        plane=DOCUMENT_CHUNK_EMBEDDING_PLANE,
        organization_id=organization_id,
        execute=execute,
        query_stamp=query_stamp,
    )
    return readiness.run


__all__ = [
    "LANE_MIN_IN_MODEL_FRACTION",
    "LANE_READY_COMPLETE",
    "LANE_READY_CONVERTING",
    "LANE_READY_UNKNOWN",
    "LANE_SKIPPED_SPARSE",
    "LANE_SKIPPED_SWITCHED",
    "LaneReadiness",
    "chunk_vector_lane_ready",
    "judge_lane_readiness",
    "reset_lane_readiness_cache",
    "vector_lane_readiness",
]
