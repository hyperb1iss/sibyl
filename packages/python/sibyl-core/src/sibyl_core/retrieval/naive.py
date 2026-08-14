"""Naive-strong retrieval arm: the control the 8-lane machine is raced against.

The arm exists to measure what the machine's extra lanes and its ranking layer
are worth. It is deliberately the smallest pipeline that can still be strong:
BM25 fulltext over the verbatim-span corpus, dense KNN over the same rows,
plain reciprocal-rank fusion, and a tight character-budgeted pack. Nothing
else. No graph expansion, no exact-key probe, no edge lanes, no query planning,
no coverage re-rank, no fact frames, no temporal or freshness or project or
active-task boosts, no synthesis.

Every ranking knob the machine tunes is a knob this arm refuses. Its fusion
constant stays at the RRF paper's default and no caller can override it, and
the only other constant sets how deep a lane reads, which changes what fusion
is allowed to see rather than how fusion scores it. If the arm wins, the
deleted surface was not carrying the accuracy; if it loses, the loss localizes
what the machine's remaining lanes actually buy. Adding a weight here would
destroy that reading, because a tuned control measures nothing.

The lane readers are imported from ``retrieval.search`` rather than reimplemented
so the race compares fusion and pack shape, not two different spellings of the
same SurrealDB read: whatever the analyzers and indexes do for the machine's
BM25 lane, they do identically here.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping, Sequence
from dataclasses import replace
from enum import StrEnum
from typing import TYPE_CHECKING, Any

import structlog

from sibyl_core.retrieval.candidates import RetrievalCandidate, VectorCandidateFetch
from sibyl_core.retrieval.fusion import rrf_merge_with_metadata
from sibyl_core.retrieval.search import (
    MAX_RETRIEVAL_LIMIT,
    RetrievalPlan,
    RetrievalSignal,
    _candidate_allowed,
    _candidate_source_metadata,
    _candidate_source_result,
    _elapsed_ms,
    _empty_candidate_source,
    _episode_fulltext_candidates,
    _episode_sources_allowed,
    _get_read_only_graph_runtime,
    _node_fulltext_candidates,
    _node_sources_allowed,
    _search_filter_for_plan,
    _search_result_from_candidate,
    _vector_candidate_sources_detailed,
)

if TYPE_CHECKING:
    from sibyl_core.embeddings.providers import EmbeddingProvider
    from sibyl_core.models.context import ContextFacet
    from sibyl_core.tools.responses import SearchResponse, SearchResult

log = structlog.get_logger()

# The RRF paper's default, and the arm's only tunable. It is stated once, as a
# constant rather than a parameter, because a per-request k would reintroduce
# exactly the hand-weighting the arm is built to do without.
NAIVE_RRF_K = 60.0

NAIVE_RETRIEVAL_MODE = "naive"


class NaiveSignal(StrEnum):
    """The two lanes the arm runs, named to match the machine's own signals.

    Sharing the machine's vocabulary keeps the receipts comparable: a fused
    result from either pipeline reports ``retrieval_signals`` drawn from the
    same namespace, so a screen can diff lane membership across arms without a
    translation table.
    """

    LEXICAL = RetrievalSignal.NODE_FULLTEXT.value
    EPISODE_LEXICAL = RetrievalSignal.EPISODE_FULLTEXT.value
    DENSE = RetrievalSignal.NODE_VECTOR.value


_NAIVE_PLAN_SIGNALS: tuple[RetrievalSignal, ...] = (
    RetrievalSignal.NODE_FULLTEXT,
    RetrievalSignal.EPISODE_FULLTEXT,
    RetrievalSignal.NODE_VECTOR,
)


def naive_retrieval_plan(plan: RetrievalPlan) -> RetrievalPlan:
    """Strip a context plan down to the lanes the arm runs.

    The scope, project authorization, and facet fields are carried through
    untouched: the arm deletes ranking surface, never access control.
    """

    return replace(plan, signals=_NAIVE_PLAN_SIGNALS)


async def naive_search(
    *,
    plan: RetrievalPlan,
    types: Sequence[str] | None = None,
    facet: ContextFacet | None = None,
    limit: int = 10,
    include_content: bool = True,
    embedding_provider: EmbeddingProvider | None = None,
    char_budget: int | None = None,
    content_max_chars: int | None = None,
) -> SearchResponse:
    """Retrieve with BM25 + dense KNN + plain RRF + a tight pack, and nothing else."""

    from sibyl_core.tools.responses import SearchResponse
    from sibyl_core.tools.search import MAX_SEARCH_CONTENT_MAX_CHARS

    search_started_at = time.perf_counter()
    stage_timings_ms: dict[str, float] = {}
    stage_started_at = time.perf_counter()

    limit = max(1, min(limit, MAX_RETRIEVAL_LIMIT))
    # Clamped exactly as the enhanced path clamps it, so an item costs the arm
    # the same characters it would cost the machine. Without this the arm packs
    # untruncated spans against a budget the machine spends truncated ones on,
    # and the race compares payload sizes instead of retrieval.
    if content_max_chars is not None:
        content_max_chars = max(0, min(int(content_max_chars), MAX_SEARCH_CONTENT_MAX_CHARS))
    # Every lane reads to the ceiling, whatever the caller asked for. A depth
    # scaled off the limit leaves a boundary class alive: at the default limit
    # of 12 a four-times pool stops at 48, so a row ranked 49 in both lanes
    # still outscores a single lane's rank one and is still clipped before
    # fusion sees it. Fifty rows per lane is cheap, kills that class outright,
    # and removes a number somebody would otherwise be tempted to tune. The
    # depth is set on the three lanes directly rather than through the
    # narrowing helper, which can only shrink a plan's budget.
    lane_depth = MAX_RETRIEVAL_LIMIT
    search_plan = naive_retrieval_plan(
        replace(
            plan,
            candidate_limits=replace(
                plan.candidate_limits,
                node_fulltext=lane_depth,
                episode_fulltext=lane_depth,
                node_vector=lane_depth,
            ),
        )
    )
    runtime = await _get_read_only_graph_runtime(search_plan.organization_id)
    client = runtime.client
    requested_types = {value.lower() for value in types or ()}
    search_filter = _search_filter_for_plan(search_plan, requested_types=requested_types)
    node_sources_allowed = _node_sources_allowed(requested_types)
    episode_sources_allowed = _episode_sources_allowed(requested_types)
    stage_timings_ms["runtime_setup"] = _elapsed_ms(stage_started_at)

    stage_started_at = time.perf_counter()
    lexical_tasks = (
        (
            RetrievalSignal.NODE_FULLTEXT,
            _node_fulltext_candidates(
                client=client,
                plan=search_plan,
                search_filter=search_filter,
                limit=search_plan.candidate_limits.node_fulltext,
            )
            if node_sources_allowed
            else _empty_candidate_source(),
        ),
        (
            RetrievalSignal.EPISODE_FULLTEXT,
            _episode_fulltext_candidates(
                client=client,
                plan=search_plan,
                search_filter=search_filter,
                limit=search_plan.candidate_limits.episode_fulltext,
            )
            if episode_sources_allowed
            else _empty_candidate_source(),
        ),
    )
    vector_plan = replace(
        search_plan,
        signals=(RetrievalSignal.NODE_VECTOR,) if node_sources_allowed else (),
    )
    # No return_exceptions here, deliberately. The machine degrades to its
    # surviving lanes because a partial answer beats none for a user. The arm is
    # not answering a user, it is producing a measurement, and a run whose
    # lexical lane died returns a thin pack that the benchmark scores as a
    # recall miss rather than an error. That silently attributes an outage to
    # the arm's ranking. A failed data point can be excluded and rerun; a
    # degraded one that looks healthy cannot be found later.
    lexical_gathered, vector_fetch = await asyncio.gather(
        asyncio.gather(*(task for _signal, task in lexical_tasks)),
        _vector_candidate_sources_detailed(
            client=client,
            plan=vector_plan,
            search_filter=search_filter,
            embedding_provider=embedding_provider,
        ),
    )
    lexical_sources = [
        _candidate_source_result(signal.value, result)
        for (signal, _task), result in zip(lexical_tasks, lexical_gathered, strict=True)
    ]
    # A lane that came back degraded rather than raising is still a lane that
    # did not run, so it fails the request for the same reason.
    if vector_fetch.failures or vector_fetch.reason:
        msg = (
            "naive retrieval vector lane degraded "
            f"(reason={vector_fetch.reason!r}, failures={list(vector_fetch.failures)})"
        )
        raise RuntimeError(msg)
    degraded_lexical = [source.source for source in lexical_sources if source.failure is not None]
    if degraded_lexical:
        msg = f"naive retrieval lexical lanes degraded: {', '.join(sorted(degraded_lexical))}"
        raise RuntimeError(msg)
    vector_fetch_state: VectorCandidateFetch | None = vector_fetch
    vector_candidates = list(vector_fetch.node_candidates)
    vector_source = _candidate_source_result(
        RetrievalSignal.NODE_VECTOR.value,
        vector_candidates,
    )
    stage_timings_ms["candidates"] = _elapsed_ms(stage_started_at)

    stage_started_at = time.perf_counter()
    source_lists: list[tuple[RetrievalSignal, list[RetrievalCandidate]]] = [
        (RetrievalSignal.NODE_FULLTEXT, list(lexical_sources[0].candidates)),
        (RetrievalSignal.EPISODE_FULLTEXT, list(lexical_sources[1].candidates)),
        (RetrievalSignal.NODE_VECTOR, vector_candidates),
    ]
    filtered_lists = [
        (
            signal,
            [
                candidate
                for candidate in candidates
                if _candidate_allowed(
                    candidate,
                    plan=search_plan,
                    requested_types=requested_types,
                    facet=facet,
                )
            ],
        )
        for signal, candidates in source_lists
    ]
    # Recomputed from the authorized rows, never snapshotted from the raw fetch.
    # A count taken before the scope filter answers "does a row matching this
    # query exist in this organization" for a caller who may read none of them,
    # which is an existence oracle over private memories: empty results beside a
    # non-zero candidate count is the leak. The response contract promises
    # authorized diagnostics, so the diagnostics have to be built from the
    # authorized set.
    authorized_vector_count = len(
        next(
            (
                candidates
                for signal, candidates in filtered_lists
                if signal is RetrievalSignal.NODE_VECTOR
            ),
            [],
        )
    )
    vector_metadata = _authorized_vector_metadata(vector_fetch_state, authorized_vector_count)
    stage_timings_ms["candidate_filtering"] = _elapsed_ms(stage_started_at)

    stage_started_at = time.perf_counter()
    fused = fuse_naive_candidates(filtered_lists, limit=limit)
    stage_timings_ms["fusion"] = _elapsed_ms(stage_started_at)

    stage_started_at = time.perf_counter()
    results, pack_receipt = pack_naive_results(
        fused,
        include_content=include_content,
        char_budget=char_budget,
        content_max_chars=content_max_chars,
    )
    stage_timings_ms["pack"] = _elapsed_ms(stage_started_at)
    stage_timings_ms["total"] = _elapsed_ms(search_started_at)
    # Logged, never returned. Per-stage durations are measured across the
    # candidate fetch, which runs before the scope filter, so a caller who may
    # read none of the matches can still tell "nothing matched" from "matches
    # exist but are not yours" by timing repeated queries. The counts were
    # rebuilt from the authorized set for exactly that reason and the clock
    # would have put the same signal back. Constant-time stages are not
    # achievable here, so the honest fix is not to publish them.
    log.info(
        "naive_retrieval_complete",
        organization_id=search_plan.organization_id,
        result_count=len(results),
        stage_timings_ms=stage_timings_ms,
    )

    return SearchResponse(
        results=results,
        total=len(results),
        query=plan.query,
        filters={
            "types": list(types) if types else None,
            "project": search_plan.project,
            "retrieval_mode": NAIVE_RETRIEVAL_MODE,
            "retrieval_arm": NAIVE_RETRIEVAL_MODE,
            "fusion_backend": "python_rrf",
            "naive_rrf_k": NAIVE_RRF_K,
            "naive_lanes": [signal.value for signal, _candidates in filtered_lists],
            "naive_lane_counts": {
                signal.value: len(candidates) for signal, candidates in filtered_lists
            },
            **_candidate_source_metadata([*lexical_sources, vector_source]),
            **vector_metadata,
            **pack_receipt,
        },
        graph_count=len([result for result in results if result.result_origin == "graph"]),
        document_count=0,
        limit=limit,
    )


def _authorized_vector_metadata(
    fetch: VectorCandidateFetch | None,
    authorized_count: int,
) -> dict[str, object]:
    """Vector-lane diagnostics describing only what this caller may read.

    Health fields (requested, attempted, degraded, failures) describe the arm's
    own execution and carry no row information, so they pass through. The count
    and the status are rebuilt, because both are derived from the raw candidate
    list and would otherwise report rows the caller was denied.
    """

    if fetch is None:
        return {
            "vector_status": "query_failed",
            "vector_requested": True,
            "vector_attempted": True,
            "vector_degraded": True,
            "vector_candidate_count": 0,
        }
    if not fetch.requested:
        status = "not_requested"
    elif fetch.reason is not None:
        status = fetch.reason
    elif fetch.failures and authorized_count:
        status = "partial"
    elif fetch.failures:
        status = "query_failed"
    elif not fetch.attempted:
        status = "unavailable"
    elif authorized_count == 0:
        # "empty" whether the lane found nothing or found only rows this caller
        # cannot read. Those two must be indistinguishable from outside.
        status = "empty"
    else:
        status = "ok"
    metadata: dict[str, object] = {
        "vector_status": status,
        "vector_requested": fetch.requested,
        "vector_attempted": fetch.attempted,
        "vector_degraded": fetch.degraded,
        "vector_candidate_count": authorized_count,
    }
    if fetch.failures:
        metadata["vector_failures"] = list(fetch.failures)
    return metadata


def fuse_naive_candidates(
    source_lists: Sequence[tuple[RetrievalSignal, Sequence[RetrievalCandidate]]],
    *,
    limit: int,
    k: float = NAIVE_RRF_K,
) -> list[tuple[RetrievalCandidate, float, dict[str, Any]]]:
    """Plain reciprocal-rank fusion: no weights, no boosts, no re-rank.

    A candidate's fused score is the sum of ``1 / (k + rank)`` over the lanes
    that returned it, and the lane's own score never enters. That is the whole
    ranking function.

    Ties break on candidate id, ascending. RRF produces exact ties routinely
    (two rows each returned at rank one by a different lane score identically),
    and the shared merge helper sorts on score alone, so a tie would otherwise
    resolve by the order the lanes happen to be passed in. That is a silent
    lexical-lane priority: a real ranking preference, never declared, applied
    only at a binding cutoff. Ordering by id instead is arbitrary but stated,
    reproducible across runs, and independent of lane order.
    """

    ranked_lists = [
        [(candidate, candidate.score) for candidate in candidates]
        for _signal, candidates in source_lists
    ]
    # Fused unlimited, then ordered, then truncated: truncating inside the
    # helper would apply its lane-order tie-break before this one could run.
    fused = rrf_merge_with_metadata(
        ranked_lists,
        list_names=[signal.value for signal, _candidates in source_lists],
        k=k,
        dedup_key=lambda candidate: candidate.id,
    )
    fused.sort(key=lambda item: (-item[1], item[0].id))
    for _candidate, _score, metadata in fused:
        metadata["fusion_backend"] = "python_rrf"
    return fused[:limit] if limit else fused


def pack_naive_results(
    fused: Sequence[tuple[RetrievalCandidate, float, Mapping[str, Any]]],
    *,
    include_content: bool,
    char_budget: int | None,
    content_max_chars: int | None = None,
) -> tuple[list[SearchResult], dict[str, object]]:
    """Take fused results in rank order until the character budget is spent.

    Tight means tight: the first item that would cross the budget ends the pack
    rather than being truncated, so every item the reader sees is a whole span.
    A single item wider than the whole budget is still admitted, because an
    empty pack answers nothing.

    `content_max_chars` caps a single item the way the enhanced path caps it,
    and it is applied before the budget is charged so an item costs the arm what
    the same item costs the machine.
    """

    results: list[SearchResult] = []
    spent = 0
    stopped_on_budget = False
    for candidate, score, metadata in fused:
        result = _search_result_from_candidate(
            candidate,
            score=score,
            fusion_metadata=metadata,
            include_content=include_content,
        )
        if content_max_chars is not None and result.content:
            result.content = result.content[:content_max_chars]
        cost = len(result.content or "")
        if char_budget is not None and results and spent + cost > char_budget:
            stopped_on_budget = True
            break
        results.append(result)
        spent += cost
    receipt: dict[str, object] = {
        "naive_pack_char_budget": char_budget,
        "naive_pack_chars_used": spent,
        "naive_pack_item_count": len(results),
        "naive_pack_candidate_count": len(fused),
        "naive_pack_budget_exhausted": stopped_on_budget,
        "content_max_chars": content_max_chars,
    }
    return results, receipt
