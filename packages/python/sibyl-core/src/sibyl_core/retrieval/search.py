"""Native SurrealDB retrieval planning contracts."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sibyl_core.embeddings.providers import EmbeddingProvider
from sibyl_core.memory_pipeline.retrieval import CandidateSourceFailure, CandidateSourceResult
from sibyl_core.models.context import ContextFacet
from sibyl_core.retrieval import _search_candidates as candidate_stage
from sibyl_core.retrieval import _search_database as search_database
from sibyl_core.retrieval import _search_expansion as expansion_stage
from sibyl_core.retrieval import _search_fusion as fusion_stage
from sibyl_core.retrieval import _search_lifecycle as lifecycle_stage
from sibyl_core.retrieval import _search_plan as search_planning
from sibyl_core.retrieval import _search_sources as source_stage
from sibyl_core.retrieval._search_plan import (
    DEFAULT_CANDIDATES_PER_SIGNAL,
    DEFAULT_FILTER_SELECTIVITY_THRESHOLD,
    MAX_RETRIEVAL_LIMIT,
    MIN_CANDIDATES_PER_SIGNAL,
    RAW_LEXICAL_LIMIT_DIVISOR,
    CandidateLimits,
    FusionBackend,
    RetrievalPlan,
    RetrievalSignal,
    RetrievalWeights,
    ScopeSpec,
    SearchFilter,
    build_context_retrieval_plan,
    coerce_fusion_backend,
    fusion_backend_from_env,
    seed_candidates_per_signal,
)
from sibyl_core.retrieval.candidates import RetrievalCandidate
from sibyl_core.retrieval.identifier_query import identifier_probe_tokens
from sibyl_core.retrieval.temporal import resolve_temporal_reference
from sibyl_core.services.surreal_content import recall_raw_memory_with_sources

if TYPE_CHECKING:
    from sibyl_core.tools.responses import SearchResponse

__all__ = [
    "DEFAULT_CANDIDATES_PER_SIGNAL",
    "DEFAULT_FILTER_SELECTIVITY_THRESHOLD",
    "MAX_RETRIEVAL_LIMIT",
    "MIN_CANDIDATES_PER_SIGNAL",
    "RAW_LEXICAL_LIMIT_DIVISOR",
    "CandidateLimits",
    "FusionBackend",
    "RetrievalPlan",
    "RetrievalSignal",
    "RetrievalWeights",
    "ScopeSpec",
    "SearchFilter",
    "build_context_retrieval_plan",
    "coerce_fusion_backend",
    "context_search",
    "fusion_backend_from_env",
    "seed_candidates_per_signal",
]


async def context_search(
    *,
    plan: RetrievalPlan,
    types: Sequence[str] | None = None,
    facet: ContextFacet | None = None,
    limit: int = 10,
    include_content: bool = True,
    embedding_provider: EmbeddingProvider | None = None,
    raw_memory_recall_fn: source_stage.RawMemoryRecallFn = recall_raw_memory_with_sources,
) -> SearchResponse:
    """Search context-pack candidates through native SurrealDB paths."""

    from sibyl_core.tools.responses import SearchResponse

    search_started_at = time.perf_counter()
    stage_timings_ms: dict[str, float] = {}
    stage_started_at = time.perf_counter()
    limit = max(1, min(limit, MAX_RETRIEVAL_LIMIT))
    search_plan = replace(
        plan,
        candidate_limits=search_planning._candidate_limits_for_limit(plan.candidate_limits, limit),
    )
    runtime = await search_database._get_read_only_graph_runtime(search_plan.organization_id)
    client = runtime.client
    requested_types = {value.lower() for value in types or ()}
    search_filter = search_planning._search_filter_for_plan(
        search_plan, requested_types=requested_types
    )
    stage_timings_ms["runtime_setup"] = _elapsed_ms(stage_started_at)

    stage_started_at = time.perf_counter()
    raw_task = source_stage._recall_raw_candidates(
        plan=search_plan,
        facet=facet,
        requested_types=requested_types,
        limit=search_plan.candidate_limits.raw_lexical,
        recall_fn=raw_memory_recall_fn,
    )
    node_sources_allowed = search_planning._node_sources_allowed(requested_types)
    episode_sources_allowed = search_planning._episode_sources_allowed(requested_types)
    edge_sources_allowed = search_planning._edge_sources_allowed(requested_types)
    probe_tokens = (
        identifier_probe_tokens(search_plan.query)
        if RetrievalSignal.EXACT_KEY in search_plan.signals
        else ()
    )
    graph_tasks = [
        (
            RetrievalSignal.NODE_FULLTEXT,
            source_stage._node_fulltext_candidates(
                client=client,
                plan=search_plan,
                search_filter=search_filter,
                limit=search_plan.candidate_limits.node_fulltext,
            )
            if node_sources_allowed
            else source_stage._empty_candidate_source(),
        ),
        (
            RetrievalSignal.EPISODE_FULLTEXT,
            source_stage._episode_fulltext_candidates(
                client=client,
                plan=search_plan,
                search_filter=search_filter,
                limit=search_plan.candidate_limits.episode_fulltext,
            )
            if episode_sources_allowed
            else source_stage._empty_candidate_source(),
        ),
        (
            RetrievalSignal.EDGE_FULLTEXT,
            source_stage._edge_fulltext_candidates(
                client=client,
                plan=search_plan,
                search_filter=search_filter,
                limit=search_plan.candidate_limits.edge_fulltext,
            )
            if edge_sources_allowed
            else source_stage._empty_candidate_source(),
        ),
        (
            RetrievalSignal.EXACT_KEY,
            source_stage._exact_key_candidates(
                client=client,
                plan=search_plan,
                search_filter=search_filter,
                limit=search_plan.candidate_limits.exact_key,
                probe_tokens=probe_tokens,
            )
            if node_sources_allowed and probe_tokens
            else source_stage._empty_candidate_source(),
        ),
    ]
    raw_source, graph_sources, raw_failures, raw_recall_metadata = await _gather_candidate_sources(
        raw_task,
        graph_tasks,
    )
    raw_candidates = list(raw_source.candidates)
    graph_candidate_lists = [list(source.candidates) for source in graph_sources]
    stage_timings_ms["lexical_candidates"] = _elapsed_ms(stage_started_at)

    stage_started_at = time.perf_counter()
    vector_plan = search_planning._vector_scoped_plan(
        search_plan,
        include_nodes=node_sources_allowed,
        include_edges=edge_sources_allowed,
    )
    vector_fetch = await source_stage._vector_candidate_sources_detailed(
        client=client,
        plan=vector_plan,
        search_filter=search_filter,
        embedding_provider=embedding_provider,
    )
    vector_candidate_lists = [vector_fetch.node_candidates, vector_fetch.edge_candidates]
    stage_timings_ms["vector_candidates"] = _elapsed_ms(stage_started_at)

    def candidate_authorized(candidate: RetrievalCandidate) -> bool:
        return candidate_stage._candidate_allowed(
            candidate,
            plan=search_plan,
            requested_types=requested_types,
            facet=facet,
        )

    stage_started_at = time.perf_counter()
    direct_lists = [
        (RetrievalSignal.RAW_LEXICAL, raw_candidates),
        (RetrievalSignal.NODE_FULLTEXT, graph_candidate_lists[0]),
        (RetrievalSignal.EPISODE_FULLTEXT, graph_candidate_lists[1]),
        (RetrievalSignal.EDGE_FULLTEXT, graph_candidate_lists[2]),
        (RetrievalSignal.EXACT_KEY, graph_candidate_lists[3]),
        (RetrievalSignal.NODE_VECTOR, vector_candidate_lists[0]),
        (RetrievalSignal.EDGE_VECTOR, vector_candidate_lists[1]),
    ]
    direct_lists = [
        (signal, [candidate for candidate in candidates if candidate_authorized(candidate)])
        for signal, candidates in direct_lists
    ]
    # The gate runs before the walk, not only before fusion. A retired row is
    # dropped from the answer either way, but seeding the expansion from it
    # still routes recall through its edges: its neighbours pass their own
    # checks and come back, so a corrected memory keeps steering what the
    # reader sees through rows that were never corrected. That holds for every
    # lane that can propose a seed, exact-key probes included, which is why the
    # gate moved ahead of seed selection rather than being repeated inside one
    # lane.
    direct_lists, direct_supersession_metadata = await lifecycle_stage._apply_supersession_gate(
        client=client,
        group_id=search_plan.organization_id,
        source_lists=direct_lists,
    )
    stage_timings_ms["candidate_filtering"] = _elapsed_ms(stage_started_at)

    stage_started_at = time.perf_counter()
    graph_expansion_source = await _gather_graph_expansion_source(
        expansion_stage._graph_expansion_candidates(
            client=client,
            plan=search_plan,
            search_filter=search_filter,
            # Seeds are authorized before they are walked, not only after. A
            # denied row's own content never reached the caller, but seeding the
            # walk from it exported its edges: the neighbour passes its own check
            # and comes back, so the seed's existence became observable through
            # somebody else's row. That is an oracle whenever the caller chooses
            # the seed, which an exact-key probe does by construction, and the
            # walk should not start from a row this reader cannot see regardless
            # of which lane proposed it.
            seed_candidates=[
                candidate
                for signal, candidates in direct_lists
                if signal is not RetrievalSignal.RAW_LEXICAL
                for candidate in candidates
            ],
            limit=search_plan.candidate_limits.graph_expansion,
        )
    )
    graph_expansion_candidates = list(graph_expansion_source.candidates)
    stage_timings_ms["graph_expansion"] = _elapsed_ms(stage_started_at)

    stage_started_at = time.perf_counter()
    candidate_source_metadata = source_stage._candidate_source_metadata(
        (raw_source, *graph_sources, graph_expansion_source),
        extra_failures=raw_failures,
    )

    (
        expansion_lists,
        expansion_supersession_metadata,
    ) = await lifecycle_stage._apply_supersession_gate(
        client=client,
        group_id=search_plan.organization_id,
        source_lists=[
            (
                RetrievalSignal.GRAPH_EXPANSION,
                [
                    candidate
                    for candidate in graph_expansion_candidates
                    if candidate_authorized(candidate)
                ],
            )
        ],
    )
    filtered_lists = [*direct_lists, *expansion_lists]
    supersession_metadata = lifecycle_stage._merged_supersession_metadata(
        direct_supersession_metadata,
        expansion_supersession_metadata,
    )
    # Counted after the scope filter, never before. A pre-filter count answers
    # "does any memory in this organization declare the string I just sent",
    # which is a question an unauthorized caller must not be able to ask: the
    # rows are withheld but their existence would leak, one guessed key at a
    # time.
    authorized_exact_key_candidates = next(
        (
            candidates
            for signal, candidates in filtered_lists
            if signal is RetrievalSignal.EXACT_KEY
        ),
        [],
    )
    temporal_target = resolve_temporal_reference(search_plan.query, datetime.now(UTC))
    fusion_backend = fusion_backend_from_env()
    fusion_failures: list[CandidateSourceFailure] = []
    # Filtering now happens on both sides of the walk, so the stage adds up
    # rather than overwriting: reading one half as the whole would understate
    # the stage by whatever the pre-seed gate cost.
    stage_timings_ms["candidate_filtering"] += _elapsed_ms(stage_started_at)

    stage_started_at = time.perf_counter()
    fusion = await fusion_stage._fuse_candidates_for_plan(
        client=client,
        source_lists=filtered_lists,
        plan=search_plan,
        limit=limit,
        temporal_target=temporal_target,
        fusion_backend=fusion_backend,
        fusion_failures=fusion_failures,
    )
    fused = fusion.candidates
    stage_timings_ms["fusion"] = _elapsed_ms(stage_started_at)

    stage_started_at = time.perf_counter()
    if search_plan.query.strip():
        fused = await asyncio.to_thread(
            fusion_stage._apply_query_coverage_to_fused,
            search_plan.query,
            fused,
            temporal_target=temporal_target,
        )
    stage_timings_ms["query_coverage"] = _elapsed_ms(stage_started_at)

    stage_started_at = time.perf_counter()
    results = [
        fusion_stage._search_result_from_candidate(
            candidate,
            score=score,
            fusion_metadata=fusion_metadata,
            include_content=include_content,
        )
        for candidate, score, fusion_metadata in fused
    ]
    stage_timings_ms["materialization"] = _elapsed_ms(stage_started_at)
    stage_timings_ms["total"] = _elapsed_ms(search_started_at)
    return SearchResponse(
        results=results,
        total=len(results),
        query=plan.query,
        filters={
            "types": list(types) if types else None,
            "project": search_plan.project,
            "retrieval_mode": "native",
            **_fusion_receipt_metadata(
                requested_backend=fusion_backend,
                actual_backend=fusion.actual_backend,
                failures=fusion_failures,
            ),
            **candidate_source_metadata,
            **raw_recall_metadata,
            **vector_fetch.as_metadata(),
            **_exact_key_receipt_metadata(
                probe_tokens=probe_tokens,
                candidates=authorized_exact_key_candidates,
            ),
            **supersession_metadata,
            **expansion_stage._predicate_hop_receipt(filtered_lists),
            "stage_timings_ms": stage_timings_ms,
        },
        graph_count=len([result for result in results if result.result_origin == "graph"]),
        document_count=0,
        limit=limit,
    )


def _elapsed_ms(started_at: float) -> float:
    return (time.perf_counter() - started_at) * 1000.0


async def _gather_candidate_sources(
    raw_task: Any,
    graph_tasks: Sequence[tuple[RetrievalSignal, Any]],
) -> tuple[
    CandidateSourceResult[RetrievalCandidate],
    list[CandidateSourceResult[RetrievalCandidate]],
    tuple[CandidateSourceFailure, ...],
    dict[str, object],
]:
    gathered = await asyncio.gather(
        raw_task,
        *(task for _signal, task in graph_tasks),
        return_exceptions=True,
    )
    raw_result = gathered[0]
    raw_failures: tuple[CandidateSourceFailure, ...] = ()
    raw_metadata: dict[str, object] = {}
    if isinstance(raw_result, source_stage.RawCandidateFetch):
        raw = CandidateSourceResult.success(
            RetrievalSignal.RAW_LEXICAL.value,
            raw_result.candidates,
        )
        raw_failures = raw_result.failures
        raw_metadata = raw_result.metadata
    else:
        raw = source_stage._candidate_source_result(RetrievalSignal.RAW_LEXICAL.value, raw_result)
    graph = [
        source_stage._candidate_source_result(signal.value, result)
        for (signal, _task), result in zip(graph_tasks, gathered[1:], strict=True)
    ]
    return raw, graph, raw_failures, raw_metadata


async def _gather_graph_expansion_source(
    task: Any,
) -> CandidateSourceResult[RetrievalCandidate]:
    (result,) = await asyncio.gather(task, return_exceptions=True)
    return source_stage._candidate_source_result(RetrievalSignal.GRAPH_EXPANSION.value, result)


def _fusion_receipt_metadata(
    *,
    requested_backend: FusionBackend,
    actual_backend: FusionBackend,
    failures: Sequence[CandidateSourceFailure],
) -> dict[str, object]:
    degraded = bool(failures) or actual_backend is not requested_backend
    metadata: dict[str, object] = {
        "fusion_backend": actual_backend.value,
        "fusion_backend_requested": requested_backend.value,
        "fusion_backend_actual": actual_backend.value,
        "fusion_degraded": degraded,
        "fusion_failure_count": len(failures),
    }
    if failures:
        metadata["fusion_failures"] = [failure.as_metadata() for failure in failures]
    return metadata


def _exact_key_receipt_metadata(
    *,
    probe_tokens: Sequence[str],
    candidates: Sequence[RetrievalCandidate],
) -> dict[str, object]:
    """Say whether the exact-match arm fired, so "inert" is a checkable claim."""

    metadata: dict[str, object] = {"exact_key_probe_fired": bool(probe_tokens)}
    if not probe_tokens:
        return metadata
    metadata["exact_key_probe_tokens"] = list(probe_tokens)
    metadata["exact_key_hit_count"] = len(candidates)
    return metadata
