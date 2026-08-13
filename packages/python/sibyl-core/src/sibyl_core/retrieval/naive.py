"""Naive-strong retrieval arm: the control the 8-lane machine is raced against.

The arm exists to measure what the machine's extra lanes and its ranking layer
are worth. It is deliberately the smallest pipeline that can still be strong:
BM25 fulltext over the verbatim-span corpus, dense KNN over the same rows,
plain reciprocal-rank fusion, and a tight character-budgeted pack. Nothing
else. No graph expansion, no exact-key probe, no edge lanes, no query planning,
no coverage re-rank, no fact frames, no temporal or freshness or project or
active-task boosts, no synthesis.

Every knob the machine tunes is a knob this arm refuses, so the constant below
is the arm's only parameter and it stays at the RRF paper's default. If the arm
wins, the deleted surface was not carrying the accuracy; if it loses, the loss
localizes what the machine's remaining lanes actually buy. Adding a weight here
would destroy that reading, because a tuned control measures nothing.

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
    _candidate_limits_for_limit,
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
    search_plan = naive_retrieval_plan(
        replace(
            plan,
            candidate_limits=_candidate_limits_for_limit(plan.candidate_limits, limit),
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
    lexical_gathered, vector_fetch = await asyncio.gather(
        asyncio.gather(*(task for _signal, task in lexical_tasks), return_exceptions=True),
        _vector_candidate_sources_detailed(
            client=client,
            plan=vector_plan,
            search_filter=search_filter,
            embedding_provider=embedding_provider,
            # A lane that raises degrades the arm to its other lane rather than
            # failing the request, matching how the machine treats a dead lane.
        ),
        return_exceptions=True,
    )
    lexical_sources = [
        _candidate_source_result(signal.value, result)
        for (signal, _task), result in zip(
            lexical_tasks,
            lexical_gathered if isinstance(lexical_gathered, list) else [None, None],
            strict=True,
        )
    ]
    vector_metadata: dict[str, object] = {}
    vector_candidates: list[RetrievalCandidate] = []
    if isinstance(vector_fetch, VectorCandidateFetch):
        vector_candidates = list(vector_fetch.node_candidates)
        vector_metadata = dict(vector_fetch.as_metadata())
        vector_source = _candidate_source_result(
            RetrievalSignal.NODE_VECTOR.value,
            vector_candidates,
        )
    else:
        log.warning(
            "naive_retrieval_vector_source_failed",
            error_type=type(vector_fetch).__name__,
        )
        vector_source = _candidate_source_result(RetrievalSignal.NODE_VECTOR.value, vector_fetch)
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
            "stage_timings_ms": stage_timings_ms,
        },
        graph_count=len([result for result in results if result.result_origin == "graph"]),
        document_count=0,
        limit=limit,
    )


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
    """

    ranked_lists = [
        [(candidate, candidate.score) for candidate in candidates]
        for _signal, candidates in source_lists
    ]
    fused = rrf_merge_with_metadata(
        ranked_lists,
        list_names=[signal.value for signal, _candidates in source_lists],
        k=k,
        dedup_key=lambda candidate: candidate.id,
        limit=limit,
    )
    for _candidate, _score, metadata in fused:
        metadata["fusion_backend"] = "python_rrf"
    return fused


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
