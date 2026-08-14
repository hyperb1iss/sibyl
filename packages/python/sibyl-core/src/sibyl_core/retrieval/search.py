"""Native SurrealDB retrieval planning contracts."""

from __future__ import annotations

import asyncio
import os
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any, cast

import structlog

from sibyl_core.auth.memory_policy import (
    MemoryPolicyDecision,
    authorize_memory_read,
    memory_metadata_read_allowed,
    memory_scope_policy_key,
)
from sibyl_core.backends.surreal.fulltext import (
    build_fulltext_terms,
    build_match_disjunction,
)
from sibyl_core.backends.surreal.knn import knn_overfetch_pool, knn_search_effort
from sibyl_core.backends.surreal.records import SurrealQueryError, query_error
from sibyl_core.config import core_config
from sibyl_core.embeddings.providers import EmbeddingMetadata, EmbeddingProvider
from sibyl_core.memory_pipeline.lifecycle import graph_metadata_recallable
from sibyl_core.memory_pipeline.retrieval import CandidateSourceFailure, CandidateSourceResult
from sibyl_core.models.context import ContextFacet
from sibyl_core.retrieval.candidates import (
    CandidateKind,
    CandidateScope,
    RetrievalCandidate,
    VectorCandidateFetch,
)
from sibyl_core.retrieval.fusion import rrf_merge
from sibyl_core.retrieval.identifier_query import identifier_probe_tokens
from sibyl_core.retrieval.query_ranking import rank_items_by_query_coverage
from sibyl_core.retrieval.temporal import (
    get_entity_timestamp,
    resolve_temporal_reference,
    temporal_decay_multiplier,
)
from sibyl_core.services.graph import get_surreal_graph_runtime, normalize_records
from sibyl_core.services.surreal_content import (
    MemoryScope,
    RawMemory,
    RawMemoryRecallResult,
    raw_memory_recallable,
    recall_raw_memory_with_sources,
)

if TYPE_CHECKING:
    from sibyl_core.tools.responses import SearchResponse, SearchResult

type RawMemoryRecallFn = Callable[..., Awaitable[list[RawMemory] | RawMemoryRecallResult]]

DEFAULT_FILTER_SELECTIVITY_THRESHOLD = 0.1
EDGE_FULLTEXT_MATCH_HEADROOM = 8
EDGE_FULLTEXT_MIN_MATCH_LIMIT = 32
# Seed budget: how many candidates one lane may contribute before fusion. It is
# a seed-diversity budget rather than a payload budget, because several
# candidates can be cuts of one source once the retrieval unit is a passage
# rather than a whole memory, so a lane needs more rows to reach the same number
# of distinct sources. The bound is therefore the caller's own `limit`: no lane
# may propose more rows than the whole answer holds, and a caller that asks for
# a wider answer is the caller that pays for the wider fulltext and HNSW read.
# A single flat ceiling cannot express that, since it pins a 50-item request to
# the same lane depth as an 8-item one.
MIN_CANDIDATES_PER_SIGNAL = 2
MAX_RETRIEVAL_LIMIT = 50
# Lane depth for a bare `CandidateLimits()`, which is the fallback for a plan
# assembled without lane budgets rather than for a plan without a limit: the
# builder has its own default `limit` and derives every field from it.
DEFAULT_CANDIDATES_PER_SIGNAL = 8
# The raw-memory lane reads whole memories rather than passages, so it takes a
# share of the seed budget instead of the whole of it.
RAW_LEXICAL_LIMIT_DIVISOR = 4
_ACTIVE_TASK_STATUSES = {"doing", "in_progress", "review"}
_RAW_MEMORY_CONTEXT_TYPES = {"raw_memory", "session", "episode", "note"}
_EDGE_CONTEXT_TYPES = {"claim", "relationship"}
_GRAPH_EXPANSION_RELATIONSHIP_WEIGHTS = {
    "DECIDES": 1.0,
    "REQUIRES": 0.98,
    "DEPENDS_ON": 0.98,
    "BLOCKS": 0.96,
    "SUPERSEDES": 0.95,
    "SUPPORTS": 0.94,
    "VALIDATED_BY": 0.94,
    "USES_PROCEDURE": 0.92,
    "IMPLEMENTED": 0.9,
    "REFERENCES": 0.86,
    "ENCOUNTERED": 0.86,
    "TOUCHES": 0.82,
    "PRODUCES": 0.82,
    "ABOUT": 0.78,
    "BELONGS_TO": 0.72,
    "CONTAINS": 0.72,
    "SHARES_COMMUNITY": 0.74,
    "DERIVED_FROM": 0.7,
    "DOCUMENTED_IN": 0.66,
    "RELATED_TO": 0.64,
    "MENTIONS": 0.58,
}
_SUPERSEDES_PREDICATE = "SUPERSEDES"
# An entity carrying an inbound SUPERSEDES edge is the row somebody replaced.
# Resolving that over the surviving candidate set costs one indexed lookup
# (idx_relates_name_target) and is the only way a lane that never walked the
# graph -- vector, fulltext, exact key -- learns the row is stale.
_SUPERSESSION_LOOKUP_LIMIT = 512
# Candidate ids that are not entity uuids and so can never match an edge
# endpoint. Raw memories carry a "raw_memory:" prefix and episodes live in
# their own table, so feeding either to the lookup only widens the IN list.
_NON_ENTITY_CANDIDATE_TYPES = frozenset({"claim", "relationship", "raw_memory", "episode"})
_GRAPH_EXPANSION_DEPTH_DECAY = 0.72
_GRAPH_EXPANSION_FETCH_HEADROOM = 4
_GRAPH_EXPANSION_METADATA_KEYS = (
    "graph_expansion_depth",
    "graph_expansion_relationship",
    "graph_expansion_score",
    "graph_expansion_community_id",
)
log = structlog.get_logger()


async def _get_read_only_graph_runtime(organization_id: str) -> Any:
    try:
        return await get_surreal_graph_runtime(organization_id, ensure_schema=False)
    except TypeError as error:
        if "unexpected keyword argument 'ensure_schema'" not in str(error):
            raise
        return await get_surreal_graph_runtime(organization_id)


class FusionBackend(StrEnum):
    PYTHON_RRF = "python_rrf"
    SURREAL_RRF = "surreal_rrf"


type FusedCandidate = tuple[RetrievalCandidate, float, dict[str, Any]]


@dataclass(frozen=True, slots=True)
class FusionExecutionResult:
    candidates: list[FusedCandidate]
    actual_backend: FusionBackend


@dataclass(frozen=True, slots=True)
class RawCandidateFetch:
    candidates: list[RetrievalCandidate]
    failures: tuple[CandidateSourceFailure, ...] = ()
    metadata: dict[str, object] = field(default_factory=dict)


DEFAULT_FUSION_BACKEND = FusionBackend.PYTHON_RRF


class RetrievalSignal(StrEnum):
    RAW_LEXICAL = "raw_lexical"
    NODE_FULLTEXT = "node_fulltext"
    EPISODE_FULLTEXT = "episode_fulltext"
    EDGE_FULLTEXT = "edge_fulltext"
    NODE_VECTOR = "node_vector"
    EDGE_VECTOR = "edge_vector"
    GRAPH_EXPANSION = "graph_expansion"
    EXACT_KEY = "exact_key"


@dataclass(frozen=True, slots=True)
class RetrievalWeights:
    rrf_k: int = 60
    active_task_state_boost: float = 1.3
    project_match_boost: float = 1.2
    direct_raw_source_boost: float = 1.4
    # Deliberately equal to direct_raw_source_boost: both express the same thing,
    # a candidate reached through a channel somebody declared outright rather
    # than one inferred by similarity. A writer stamping an exact key is the
    # write-side twin of a caller naming a raw source, so the magnitudes match.
    exact_key_boost: float = 1.4
    graph_expansion_only_boost: float = 0.45
    graph_native_signal_boost_cap: float = 1.2
    freshness_boost_cap: float = 1.5


@dataclass(frozen=True, slots=True)
class CandidateLimits:
    raw_lexical: int = 4
    node_fulltext: int = DEFAULT_CANDIDATES_PER_SIGNAL
    episode_fulltext: int = DEFAULT_CANDIDATES_PER_SIGNAL
    edge_fulltext: int = DEFAULT_CANDIDATES_PER_SIGNAL
    node_vector: int = DEFAULT_CANDIDATES_PER_SIGNAL
    edge_vector: int = DEFAULT_CANDIDATES_PER_SIGNAL
    graph_expansion: int = DEFAULT_CANDIDATES_PER_SIGNAL
    exact_key: int = DEFAULT_CANDIDATES_PER_SIGNAL


@dataclass(frozen=True, slots=True)
class ScopeSpec:
    memory_scope: MemoryScope
    scope_key: str | None
    policy_reason: str
    principal_id: str
    project_id: str | None = None
    agent_id: str | None = None


@dataclass(frozen=True, slots=True)
class RetrievalPlan:
    query: str
    organization_id: str
    facets: tuple[ContextFacet, ...]
    facet_types: Mapping[ContextFacet, tuple[str, ...]]
    scopes: tuple[ScopeSpec, ...]
    denied_scopes: tuple[MemoryPolicyDecision, ...]
    candidate_limits: CandidateLimits = field(default_factory=CandidateLimits)
    weights: RetrievalWeights = field(default_factory=RetrievalWeights)
    knn_type_overfetch: int = 0
    signals: tuple[RetrievalSignal, ...] = (
        RetrievalSignal.RAW_LEXICAL,
        RetrievalSignal.NODE_FULLTEXT,
        RetrievalSignal.EPISODE_FULLTEXT,
        RetrievalSignal.EDGE_FULLTEXT,
        RetrievalSignal.NODE_VECTOR,
        RetrievalSignal.EDGE_VECTOR,
        RetrievalSignal.GRAPH_EXPANSION,
        RetrievalSignal.EXACT_KEY,
    )
    project: str | None = None
    accessible_projects: frozenset[str] | None = None
    graph_expansion_depth: int = 1
    vector_min_score: float = 0.0
    filter_selectivity: float | None = None
    filter_selectivity_threshold: float = DEFAULT_FILTER_SELECTIVITY_THRESHOLD


@dataclass(frozen=True, slots=True)
class SearchFilter:
    node_types: tuple[str, ...] = ()
    node_labels: tuple[str, ...] = ()
    project_ids: tuple[str, ...] = ()
    edge_uuids: tuple[str, ...] = ()
    edge_types: tuple[str, ...] = ()
    knn_type_overfetch: int = 0


@dataclass(frozen=True, slots=True)
class _GraphExpansionHop:
    uuid: str
    depth: int
    relationship: str
    score: float
    community_id: str | None = None
    direction: str = "outgoing"


def coerce_fusion_backend(
    value: str | FusionBackend | None,
) -> FusionBackend:
    if isinstance(value, FusionBackend):
        return value
    if value is None or not value.strip():
        return DEFAULT_FUSION_BACKEND
    try:
        return FusionBackend(value.strip().lower())
    except ValueError:
        return DEFAULT_FUSION_BACKEND


def fusion_backend_from_env(
    environ: Mapping[str, str] | None = None,
) -> FusionBackend:
    source = os.environ if environ is None else environ
    return coerce_fusion_backend(source.get("SIBYL_FUSION_BACKEND"))


def seed_candidates_per_signal(limit: int) -> int:
    """Candidates one lane may seed for a caller asking for `limit` results."""

    return max(MIN_CANDIDATES_PER_SIGNAL, min(int(limit), MAX_RETRIEVAL_LIMIT))


def build_context_retrieval_plan(
    *,
    query: str,
    organization_id: str,
    facets: Sequence[ContextFacet],
    facet_types: Mapping[ContextFacet, Sequence[str]],
    principal_id: str | None,
    project: str | None,
    accessible_projects: Iterable[str] | None,
    agent_id: str | None = None,
    limit: int = 24,
    allowed_memory_scope_keys: Iterable[str] | None = None,
    knn_type_overfetch: int = 0,
) -> RetrievalPlan:
    scopes: list[ScopeSpec] = []
    denied_scopes: list[MemoryPolicyDecision] = []
    normalized_accessible_projects = (
        frozenset(str(value) for value in accessible_projects)
        if accessible_projects is not None
        else None
    )
    normalized_scope_keys = (
        frozenset(str(value) for value in allowed_memory_scope_keys)
        if allowed_memory_scope_keys is not None
        else None
    )
    # accessible_projects gates project graph entities (tasks, epics) that carry
    # no memory_scope metadata. When an API key narrows memory grants, trim it to
    # the projects the key actually holds a project grant for, so a key without a
    # project grant cannot surface project context through an unscoped pack.
    scoped_accessible_projects = normalized_accessible_projects
    if normalized_scope_keys is not None and normalized_accessible_projects is not None:
        scoped_accessible_projects = frozenset(
            project_id
            for project_id in normalized_accessible_projects
            if memory_scope_policy_key(MemoryScope.PROJECT, project_id) in normalized_scope_keys
        )

    for decision, project_id, scoped_agent_id in _scope_decisions(
        principal_id=principal_id,
        project=project,
        accessible_projects=normalized_accessible_projects,
        agent_id=agent_id,
    ):
        if not decision.allowed:
            denied_scopes.append(decision)
            continue
        if principal_id is None:
            continue
        if normalized_scope_keys is not None and not _api_key_scope_allowed(
            decision, principal_id, normalized_scope_keys
        ):
            denied_scopes.append(replace(decision, allowed=False, reason="api_key_scope_excluded"))
            continue
        scopes.append(
            ScopeSpec(
                memory_scope=decision.memory_scope,
                scope_key=decision.scope_key,
                policy_reason=decision.reason,
                principal_id=principal_id,
                project_id=project_id,
                agent_id=scoped_agent_id,
            )
        )

    per_signal_limit = seed_candidates_per_signal(limit)
    facet_types_by_facet = {facet: tuple(facet_types.get(facet, ())) for facet in facets}
    return RetrievalPlan(
        query=query,
        organization_id=organization_id,
        facets=tuple(facets),
        facet_types=facet_types_by_facet,
        scopes=tuple(scopes),
        denied_scopes=tuple(denied_scopes),
        knn_type_overfetch=max(0, int(knn_type_overfetch)),
        candidate_limits=CandidateLimits(
            raw_lexical=max(1, min(per_signal_limit, limit // RAW_LEXICAL_LIMIT_DIVISOR or 1)),
            node_fulltext=per_signal_limit,
            episode_fulltext=per_signal_limit,
            edge_fulltext=per_signal_limit,
            node_vector=per_signal_limit,
            edge_vector=per_signal_limit,
            graph_expansion=per_signal_limit,
            exact_key=per_signal_limit,
        ),
        project=project,
        accessible_projects=scoped_accessible_projects,
        filter_selectivity=_project_filter_selectivity(project, scoped_accessible_projects),
    )


async def context_search(
    *,
    plan: RetrievalPlan,
    types: Sequence[str] | None = None,
    facet: ContextFacet | None = None,
    limit: int = 10,
    include_content: bool = True,
    embedding_provider: EmbeddingProvider | None = None,
    raw_memory_recall_fn: RawMemoryRecallFn = recall_raw_memory_with_sources,
) -> SearchResponse:
    """Search context-pack candidates through native SurrealDB paths."""

    from sibyl_core.tools.responses import SearchResponse

    search_started_at = time.perf_counter()
    stage_timings_ms: dict[str, float] = {}
    stage_started_at = time.perf_counter()
    limit = max(1, min(limit, MAX_RETRIEVAL_LIMIT))
    search_plan = replace(
        plan,
        candidate_limits=_candidate_limits_for_limit(plan.candidate_limits, limit),
    )
    runtime = await _get_read_only_graph_runtime(search_plan.organization_id)
    client = runtime.client
    requested_types = {value.lower() for value in types or ()}
    search_filter = _search_filter_for_plan(search_plan, requested_types=requested_types)
    stage_timings_ms["runtime_setup"] = _elapsed_ms(stage_started_at)

    stage_started_at = time.perf_counter()
    raw_task = _recall_raw_candidates(
        plan=search_plan,
        facet=facet,
        requested_types=requested_types,
        limit=search_plan.candidate_limits.raw_lexical,
        recall_fn=raw_memory_recall_fn,
    )
    node_sources_allowed = _node_sources_allowed(requested_types)
    episode_sources_allowed = _episode_sources_allowed(requested_types)
    edge_sources_allowed = _edge_sources_allowed(requested_types)
    probe_tokens = (
        identifier_probe_tokens(search_plan.query)
        if RetrievalSignal.EXACT_KEY in search_plan.signals
        else ()
    )
    graph_tasks = [
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
        (
            RetrievalSignal.EDGE_FULLTEXT,
            _edge_fulltext_candidates(
                client=client,
                plan=search_plan,
                search_filter=search_filter,
                limit=search_plan.candidate_limits.edge_fulltext,
            )
            if edge_sources_allowed
            else _empty_candidate_source(),
        ),
        (
            RetrievalSignal.EXACT_KEY,
            _exact_key_candidates(
                client=client,
                plan=search_plan,
                search_filter=search_filter,
                limit=search_plan.candidate_limits.exact_key,
                probe_tokens=probe_tokens,
            )
            if node_sources_allowed and probe_tokens
            else _empty_candidate_source(),
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
    vector_plan = _vector_scoped_plan(
        search_plan,
        include_nodes=node_sources_allowed,
        include_edges=edge_sources_allowed,
    )
    vector_fetch = await _vector_candidate_sources_detailed(
        client=client,
        plan=vector_plan,
        search_filter=search_filter,
        embedding_provider=embedding_provider,
    )
    vector_candidate_lists = [vector_fetch.node_candidates, vector_fetch.edge_candidates]
    stage_timings_ms["vector_candidates"] = _elapsed_ms(stage_started_at)

    def candidate_authorized(candidate: RetrievalCandidate) -> bool:
        return _candidate_allowed(
            candidate,
            plan=search_plan,
            requested_types=requested_types,
            facet=facet,
        )

    stage_started_at = time.perf_counter()
    graph_expansion_source = await _gather_graph_expansion_source(
        _graph_expansion_candidates(
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
                for source in [*graph_candidate_lists, *vector_candidate_lists]
                for candidate in source
                if candidate_authorized(candidate)
            ],
            limit=search_plan.candidate_limits.graph_expansion,
        )
    )
    graph_expansion_candidates = list(graph_expansion_source.candidates)
    stage_timings_ms["graph_expansion"] = _elapsed_ms(stage_started_at)

    stage_started_at = time.perf_counter()
    candidate_source_metadata = _candidate_source_metadata(
        (raw_source, *graph_sources, graph_expansion_source),
        extra_failures=raw_failures,
    )

    source_lists = [
        (RetrievalSignal.RAW_LEXICAL, raw_candidates),
        (RetrievalSignal.NODE_FULLTEXT, graph_candidate_lists[0]),
        (RetrievalSignal.EPISODE_FULLTEXT, graph_candidate_lists[1]),
        (RetrievalSignal.EDGE_FULLTEXT, graph_candidate_lists[2]),
        (RetrievalSignal.EXACT_KEY, graph_candidate_lists[3]),
        (RetrievalSignal.NODE_VECTOR, vector_candidate_lists[0]),
        (RetrievalSignal.EDGE_VECTOR, vector_candidate_lists[1]),
        (RetrievalSignal.GRAPH_EXPANSION, graph_expansion_candidates),
    ]
    filtered_lists = [
        (signal, [candidate for candidate in candidates if candidate_authorized(candidate)])
        for signal, candidates in source_lists
    ]
    filtered_lists, supersession_metadata = await _apply_supersession_gate(
        client=client,
        group_id=search_plan.organization_id,
        source_lists=filtered_lists,
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
    stage_timings_ms["candidate_filtering"] = _elapsed_ms(stage_started_at)

    stage_started_at = time.perf_counter()
    fusion = await _fuse_candidates_for_plan(
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
            _apply_query_coverage_to_fused,
            search_plan.query,
            fused,
            temporal_target=temporal_target,
        )
    stage_timings_ms["query_coverage"] = _elapsed_ms(stage_started_at)

    stage_started_at = time.perf_counter()
    results = [
        _search_result_from_candidate(
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
            "stage_timings_ms": stage_timings_ms,
        },
        graph_count=len([result for result in results if result.result_origin == "graph"]),
        document_count=0,
        limit=limit,
    )


def _elapsed_ms(started_at: float) -> float:
    return (time.perf_counter() - started_at) * 1000.0


def _api_key_scope_allowed(
    decision: MemoryPolicyDecision,
    principal_id: str,
    allowed_scope_keys: frozenset[str],
) -> bool:
    """Check a granted scope against the API key's memory-space grants.

    Private scopes key on the principal that owns them; project and other
    keyed scopes key on the scope_key, matching how API-key memory spaces
    are minted.
    """
    effective_scope_key = (
        principal_id if decision.memory_scope is MemoryScope.PRIVATE else decision.scope_key
    )
    policy_key = memory_scope_policy_key(decision.memory_scope, effective_scope_key)
    return policy_key in allowed_scope_keys


def _scope_decisions(
    *,
    principal_id: str | None,
    project: str | None,
    accessible_projects: frozenset[str] | None,
    agent_id: str | None,
) -> list[tuple[MemoryPolicyDecision, str | None, str | None]]:
    decisions = [
        (
            authorize_memory_read(
                principal_id=principal_id,
                memory_scope=MemoryScope.PRIVATE,
            ),
            None,
            None,
        )
    ]
    if project:
        decisions.append(
            (
                authorize_memory_read(
                    principal_id=principal_id,
                    memory_scope=MemoryScope.PROJECT,
                    scope_key=project,
                    accessible_projects=accessible_projects,
                ),
                project,
                None,
            )
        )
    elif accessible_projects:
        for accessible_project in sorted(accessible_projects):
            decisions.append(
                (
                    authorize_memory_read(
                        principal_id=principal_id,
                        memory_scope=MemoryScope.PROJECT,
                        scope_key=accessible_project,
                        accessible_projects=accessible_projects,
                    ),
                    accessible_project,
                    None,
                )
            )
    if agent_id:
        # Agent diaries default to the project-less private scope, so a query
        # with no specific project must include project_id=None alongside any
        # accessible projects. Scoping only to accessible projects hid
        # project-less diaries whenever the principal had any accessible
        # project (e.g. the context-pack agent-diary recall).
        diary_projects: tuple[str | None, ...] = (
            (project,) if project else (None, *sorted(accessible_projects or ()))
        )
        for diary_project in diary_projects:
            decisions.append(
                (
                    authorize_memory_read(
                        principal_id=principal_id,
                        memory_scope=MemoryScope.PRIVATE,
                        project_id=diary_project,
                        agent_id=agent_id,
                        accessible_projects=accessible_projects,
                    ),
                    diary_project,
                    agent_id,
                )
            )
    return decisions


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
    if isinstance(raw_result, RawCandidateFetch):
        raw = CandidateSourceResult.success(
            RetrievalSignal.RAW_LEXICAL.value,
            raw_result.candidates,
        )
        raw_failures = raw_result.failures
        raw_metadata = raw_result.metadata
    else:
        raw = _candidate_source_result(RetrievalSignal.RAW_LEXICAL.value, raw_result)
    graph = [
        _candidate_source_result(signal.value, result)
        for (signal, _task), result in zip(graph_tasks, gathered[1:], strict=True)
    ]
    return raw, graph, raw_failures, raw_metadata


async def _gather_graph_expansion_source(
    task: Any,
) -> CandidateSourceResult[RetrievalCandidate]:
    (result,) = await asyncio.gather(task, return_exceptions=True)
    return _candidate_source_result(RetrievalSignal.GRAPH_EXPANSION.value, result)


def _candidate_source_result(
    source: str,
    result: object,
) -> CandidateSourceResult[RetrievalCandidate]:
    if isinstance(result, BaseException):
        log.warning(
            "retrieval_candidate_source_failed",
            source=source,
            error_type=type(result).__name__,
        )
        return CandidateSourceResult.failed(source, type(result).__name__)
    if not isinstance(result, list):
        log.warning(
            "retrieval_candidate_source_invalid",
            source=source,
            result_type=type(result).__name__,
        )
        return CandidateSourceResult.failed(source, f"invalid:{type(result).__name__}")
    return CandidateSourceResult.success(source, cast("list[RetrievalCandidate]", result))


def _candidate_source_metadata(
    sources: Sequence[CandidateSourceResult[RetrievalCandidate]],
    *,
    extra_failures: Sequence[CandidateSourceFailure] = (),
) -> dict[str, object]:
    failures = [source.failure for source in sources if source.failure is not None]
    failures.extend(extra_failures)
    metadata: dict[str, object] = {
        "candidate_source_degraded": bool(failures),
        "candidate_source_failure_count": len(failures),
    }
    if failures:
        metadata["candidate_source_failures"] = [failure.as_metadata() for failure in failures]
    return metadata


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


def _candidate_list_or_empty(result: object) -> list[RetrievalCandidate]:
    if isinstance(result, BaseException) or not isinstance(result, list):
        return []
    return cast("list[RetrievalCandidate]", result)


async def _empty_candidate_source() -> list[RetrievalCandidate]:
    return []


async def _execute_query_records(
    client: Any,
    query: str,
    **params: object,
) -> list[dict[str, object]]:
    result = await client.execute_query(query, **params)
    if error := query_error(result):
        raise SurrealQueryError(query, error)
    return normalize_records(result)


async def _superseded_candidate_uuids(
    client: Any,
    *,
    group_id: str,
    uuids: Sequence[str],
) -> tuple[set[str], int]:
    """Resolve which of these candidates something else declared it replaced.

    The row count comes back alongside the set because dedup destroys the
    only evidence that the row cap bound: one retired row can carry several
    inbound edges, so 300 candidates can produce more than 512 rows and still
    dedup to a handful of uuids.
    """

    if not uuids:
        return set(), 0
    rows = await _execute_query_records(
        client,
        """
        SELECT target_id AS uuid
        FROM relates_to
        WHERE name = $predicate
          AND target_id IN $uuids
          AND group_id = $group_id
        LIMIT $limit;
        """,
        predicate=_SUPERSEDES_PREDICATE,
        uuids=list(uuids),
        group_id=group_id,
        limit=_SUPERSESSION_LOOKUP_LIMIT,
    )
    return {uuid for row in rows if (uuid := _string_value(row.get("uuid")))}, len(rows)


async def _apply_supersession_gate(
    *,
    client: Any,
    group_id: str,
    source_lists: Sequence[tuple[RetrievalSignal, list[RetrievalCandidate]]],
) -> tuple[list[tuple[RetrievalSignal, list[RetrievalCandidate]]], dict[str, Any]]:
    """Drop rows a writer has already retired, before anything is fused.

    Supersession and correction are declarations the graph lane never acted
    on: a corrected row kept its embedding, kept its rank, and kept being
    expanded into. Two independent signals retire a candidate here. Its own
    stamped lifecycle metadata, written by the correction path, covers rows
    whose replacement is not itself a graph entity. An inbound SUPERSEDES edge
    covers the reflection-promotion case, where the replacement exists and the
    edge is the only record of it. Because the successor carries neither
    signal, this is also what makes the newer row win whenever both match.
    """

    lifecycle_dropped = 0
    surviving: list[tuple[RetrievalSignal, list[RetrievalCandidate]]] = []
    for signal, candidates in source_lists:
        kept: list[RetrievalCandidate] = []
        for candidate in candidates:
            if graph_metadata_recallable(candidate.metadata):
                kept.append(candidate)
            else:
                lifecycle_dropped += 1
        surviving.append((signal, kept))

    node_uuids = _dedupe_strings(
        candidate.id
        for _signal, candidates in surviving
        for candidate in candidates
        if candidate.type not in _NON_ENTITY_CANDIDATE_TYPES
    )
    superseded: set[str] = set()
    lookup_failed: str | None = None
    # Both caps fail open: a candidate past the slice is never checked, and a
    # retired row can carry several inbound edges so the row cap can bite
    # before the candidate cap does. Neither is reachable at current pool
    # sizes, and neither may pass silently if it ever becomes reachable.
    truncated = len(node_uuids) > _SUPERSESSION_LOOKUP_LIMIT
    edge_rows = 0
    if node_uuids:
        try:
            superseded, edge_rows = await _superseded_candidate_uuids(
                client,
                group_id=group_id,
                uuids=node_uuids[:_SUPERSESSION_LOOKUP_LIMIT],
            )
        except Exception as exc:
            # A gate that cannot read its own edges must not silently pass
            # every stale row as fresh, but it also must not fail the search
            # outright: lifecycle metadata still applies and the receipt says
            # the edge half did not run.
            lookup_failed = type(exc).__name__
            log.warning("supersession_lookup_failed", error_type=lookup_failed)

    edge_dropped = 0
    if superseded:
        gated: list[tuple[RetrievalSignal, list[RetrievalCandidate]]] = []
        for signal, candidates in surviving:
            kept = [candidate for candidate in candidates if candidate.id not in superseded]
            edge_dropped += len(candidates) - len(kept)
            gated.append((signal, kept))
        surviving = gated

    receipt: dict[str, Any] = {
        "lifecycle_dropped": lifecycle_dropped,
        "superseded_dropped": edge_dropped,
        "superseded_uuids": sorted(superseded),
    }
    if truncated or edge_rows >= _SUPERSESSION_LOOKUP_LIMIT:
        receipt["truncated"] = True
        receipt["checked_candidates"] = min(len(node_uuids), _SUPERSESSION_LOOKUP_LIMIT)
        receipt["total_candidates"] = len(node_uuids)
        receipt["edge_rows_read"] = edge_rows
    if lookup_failed is not None:
        receipt["lookup_error_type"] = lookup_failed
    return surviving, {"supersession_gate": receipt}


def _candidate_limits_for_limit(
    candidate_limits: CandidateLimits,
    limit: int,
) -> CandidateLimits:
    # Narrows only. A plan is built from the caller's `limit`, so a query-time
    # limit below it must not leave a lane reading deeper than the answer, while
    # a query-time limit above it must not widen a plan the caller never asked
    # to widen.
    source_limit = max(1, min(int(limit), MAX_RETRIEVAL_LIMIT))
    return CandidateLimits(
        raw_lexical=max(1, min(candidate_limits.raw_lexical, source_limit)),
        node_fulltext=max(1, min(candidate_limits.node_fulltext, source_limit)),
        episode_fulltext=max(1, min(candidate_limits.episode_fulltext, source_limit)),
        edge_fulltext=max(1, min(candidate_limits.edge_fulltext, source_limit)),
        node_vector=max(1, min(candidate_limits.node_vector, source_limit)),
        edge_vector=max(1, min(candidate_limits.edge_vector, source_limit)),
        graph_expansion=max(1, min(candidate_limits.graph_expansion, source_limit)),
        exact_key=max(1, min(candidate_limits.exact_key, source_limit)),
    )


def _node_sources_allowed(requested_types: set[str]) -> bool:
    return not requested_types or bool(_node_types_for_requested_types(requested_types))


def _episode_sources_allowed(requested_types: set[str]) -> bool:
    return not requested_types or "episode" in requested_types


def _edge_sources_allowed(requested_types: set[str]) -> bool:
    return not requested_types or bool(requested_types & _EDGE_CONTEXT_TYPES)


def _node_types_for_requested_types(requested_types: set[str]) -> tuple[str, ...]:
    return tuple(sorted(requested_types - {"raw_memory", "relationship"}))


def _vector_scoped_plan(
    plan: RetrievalPlan,
    *,
    include_nodes: bool,
    include_edges: bool,
) -> RetrievalPlan:
    signals: list[RetrievalSignal] = []
    for signal in plan.signals:
        if signal is RetrievalSignal.NODE_VECTOR and not include_nodes:
            continue
        if signal is RetrievalSignal.EDGE_VECTOR and not include_edges:
            continue
        signals.append(signal)
    return replace(plan, signals=tuple(signals))


def _search_filter_for_plan(
    plan: RetrievalPlan,
    *,
    requested_types: set[str] | None = None,
) -> SearchFilter:
    requested_types = requested_types or set()
    return SearchFilter(
        node_types=_node_types_for_requested_types(requested_types),
        knn_type_overfetch=plan.knn_type_overfetch,
        project_ids=_authorized_project_ids(plan),
    )


def _authorized_project_ids(plan: RetrievalPlan) -> tuple[str, ...]:
    if plan.project:
        if any(
            scope.memory_scope is MemoryScope.PROJECT and scope.project_id == plan.project
            for scope in plan.scopes
        ):
            return (plan.project,)
        return ()
    if plan.accessible_projects:
        return tuple(sorted(plan.accessible_projects))
    return ()


def _project_filter_selectivity(
    project: str | None,
    accessible_projects: frozenset[str] | None,
) -> float | None:
    if not project or not accessible_projects:
        return None
    if project not in accessible_projects:
        return 0.0
    return 1.0 / len(accessible_projects)


def _explicit_project_denied(plan: RetrievalPlan) -> bool:
    return bool(plan.project and not _authorized_project_ids(plan))


async def _recall_raw_candidates(
    *,
    plan: RetrievalPlan,
    facet: ContextFacet | None,
    requested_types: set[str],
    limit: int,
    recall_fn: RawMemoryRecallFn,
) -> RawCandidateFetch:
    if facet is not None and facet is not ContextFacet.RECENT_MEMORY:
        return RawCandidateFetch([])
    if requested_types and requested_types.isdisjoint(_RAW_MEMORY_CONTEXT_TYPES):
        return RawCandidateFetch([])

    candidates: list[RetrievalCandidate] = []
    failures: list[CandidateSourceFailure] = []
    seen_ids: set[str] = set()
    raw_recall_scopes = {MemoryScope.PRIVATE, MemoryScope.PROJECT, MemoryScope.DELEGATED}
    raw_scopes = [scope for scope in plan.scopes if scope.memory_scope in raw_recall_scopes]
    recalled_by_scope = await asyncio.gather(
        *(
            recall_fn(
                organization_id=plan.organization_id,
                principal_id=scope.principal_id,
                query=plan.query,
                memory_scope=scope.memory_scope.value,
                scope_key=scope.scope_key,
                agent_id=scope.agent_id,
                project_id=scope.project_id,
                limit=limit,
            )
            for scope in raw_scopes
        ),
        return_exceptions=True,
    )
    for scope, recalled in zip(raw_scopes, recalled_by_scope, strict=True):
        if isinstance(recalled, asyncio.CancelledError):
            raise recalled
        if isinstance(recalled, BaseException):
            log.warning(
                "raw_recall_scope_failed",
                error_type=type(recalled).__name__,
                memory_scope=scope.memory_scope.value,
                project_id=scope.project_id,
                scope_key=scope.scope_key,
            )
            failures.append(CandidateSourceFailure("raw_scope_recall", type(recalled).__name__))
            continue
        if isinstance(recalled, RawMemoryRecallResult):
            memories = list(recalled.memories)
            failures.extend(recalled.failures)
        else:
            memories = recalled
        for memory in memories:
            if not raw_memory_recallable(memory):
                continue
            if memory.id in seen_ids:
                continue
            seen_ids.add(memory.id)
            candidates.append(_candidate_from_raw_memory(memory, scope))
    raw_metadata: dict[str, object] = {
        "raw_recall_degraded": bool(failures),
        "raw_recall_failure_count": len(failures),
    }
    if failures:
        raw_metadata["raw_recall_failures"] = [failure.as_metadata() for failure in failures]
    return RawCandidateFetch(
        sorted(candidates, key=lambda candidate: candidate.score, reverse=True),
        failures=tuple(failures),
        metadata=raw_metadata,
    )


async def _node_fulltext_candidates(
    *,
    client: Any,
    plan: RetrievalPlan,
    search_filter: SearchFilter,
    limit: int,
) -> list[RetrievalCandidate]:
    match = build_match_disjunction(
        ["name", "summary", "description", "content"],
        build_fulltext_terms(plan.query),
    )
    if match is None:
        return []
    filter_clauses, filter_params = _node_filter_clause(search_filter)
    rows = await _execute_query_records(
        client,
        f"""
        SELECT *,
               {match.score_expr} AS score
        FROM entity
        WHERE """
        + _where_clause(["group_id = $group_id", *filter_clauses])
        + f"""
          AND {match.where_clause}
        ORDER BY score DESC, created_at DESC, uuid DESC
        LIMIT $limit;
        """,
        group_id=plan.organization_id,
        limit=max(int(limit), 1),
        **match.params,
        **filter_params,
    )
    return [
        _candidate_from_node_record(
            row,
            signal=RetrievalSignal.NODE_FULLTEXT,
            score=_record_score(row),
        )
        for row in rows
    ]


async def _exact_key_candidates(
    *,
    client: Any,
    plan: RetrievalPlan,
    search_filter: SearchFilter,
    limit: int,
    probe_tokens: Sequence[str],
) -> list[RetrievalCandidate]:
    """Rows whose writer declared one of the query's identifier-shaped tokens.

    This is the one lane that can find a memory whose text never contains the
    query: the key is an assertion layered onto the content, not extracted from
    it. Everything is exact, so the lane cannot return a weakly relevant row and
    the precision problem that keeps BM25 out of fusion does not arise here.

    Inert by construction when the query carries no identifier: no probe tokens
    means no read at all, and the fused pool is exactly what it was before.
    """

    if not probe_tokens:
        return []
    row_limit = max(int(limit), 1)
    filter_clauses, filter_params = _node_filter_clause(search_filter)
    # CONTAINSANY against the index defined on `retrieval_keys_normalized.*`.
    # The element index is what makes this both correct and index-served: on
    # SurrealDB 3.2.3 an index on the bare array field turns this same read into
    # a full table scan, and turns a bare equality into zero rows unless the
    # WHERE clause happens to carry a second predicate. Verified live on 3.2.3
    # (UnionIndexScan across one branch per probe), so the `.*` in the index
    # definition is what this read depends on, not a stylistic choice.
    rows = await _execute_query_records(
        client,
        """
        SELECT *
        FROM entity
        WHERE """
        + _where_clause(["group_id = $group_id", *filter_clauses])
        + """
          AND retrieval_keys_normalized CONTAINSANY $probe_keys
        ORDER BY created_at DESC, uuid DESC
        LIMIT $limit;
        """,
        group_id=plan.organization_id,
        probe_keys=list(probe_tokens),
        # Read one lane's worth of rows per probe, not one in total. The database
        # truncates by recency, and overlap is only computed here, so a single
        # lane-sized read lets newer single-match rows crowd out the older row
        # that answers every identifier in the query. Probes are capped, so this
        # is the same bounded breadth a read per probe would have had.
        limit=row_limit * len(probe_tokens),
        **filter_params,
    )

    probes = set(probe_tokens)
    scored: list[tuple[int, str, list[str], Mapping[str, object]]] = []
    for row in rows:
        matched = _matched_retrieval_keys(row, probes)
        if not matched:
            continue
        scored.append((len(matched), str(row.get("uuid") or ""), matched, row))
    # A row answering more of the query's identifiers is the better answer, so
    # overlap orders the lane. Counted here rather than in SurrealQL to keep the
    # read to one index lookup and no array functions.
    scored.sort(key=lambda item: (-item[0], item[1]))
    candidates: list[RetrievalCandidate] = []
    # Truncated after the overlap sort, so the lane hands fusion its best rows
    # rather than its newest ones.
    for _count, _uuid, matched, row in scored[:row_limit]:
        candidate = _candidate_from_node_record(
            row,
            signal=RetrievalSignal.EXACT_KEY,
            score=len(matched) / len(probes),
        )
        candidate.metadata["matched_retrieval_keys"] = matched
        candidates.append(candidate)
    return candidates


def _matched_retrieval_keys(
    row: Mapping[str, object],
    probes: set[str],
) -> list[str]:
    stored = row.get("retrieval_keys_normalized")
    if not isinstance(stored, list | tuple):
        return []
    return [str(key) for key in stored if str(key) in probes]


async def _episode_fulltext_candidates(
    *,
    client: Any,
    plan: RetrievalPlan,
    search_filter: SearchFilter,
    limit: int,
) -> list[RetrievalCandidate]:
    if search_filter.project_ids:
        return []
    match = build_match_disjunction(["content"], build_fulltext_terms(plan.query))
    if match is None:
        return []
    rows = await _execute_query_records(
        client,
        f"""
        SELECT *, {match.score_expr} AS score
        FROM episode
        WHERE group_id = $group_id
          AND {match.where_clause}
        ORDER BY score DESC, created_at DESC, uuid DESC
        LIMIT $limit;
        """,
        group_id=plan.organization_id,
        limit=max(int(limit), 1),
        **match.params,
    )
    return [
        _candidate_from_episode_record(
            row,
            signal=RetrievalSignal.EPISODE_FULLTEXT,
            score=_record_score(row),
        )
        for row in rows
    ]


async def _edge_fulltext_candidates(
    *,
    client: Any,
    plan: RetrievalPlan,
    search_filter: SearchFilter,
    limit: int,
) -> list[RetrievalCandidate]:
    match = build_match_disjunction(["fact"], build_fulltext_terms(plan.query))
    if match is None:
        return []
    result_limit = max(int(limit), 1)
    match_limit = max(result_limit * EDGE_FULLTEXT_MATCH_HEADROOM, EDGE_FULLTEXT_MIN_MATCH_LIMIT)
    match_clauses, match_params = _edge_match_filter_clause(search_filter)
    match_rows = await _execute_query_records(
        client,
        f"""
        SELECT uuid, created_at, {match.score_expr} AS score
        FROM relates_to
        WHERE """
        + _where_clause(["group_id = $group_id", *match_clauses])
        + f"""
          AND {match.where_clause}
        ORDER BY score DESC, created_at DESC, uuid DESC
        LIMIT $match_limit;
        """,
        group_id=plan.organization_id,
        match_limit=match_limit,
        **match.params,
        **match_params,
    )
    match_scores: dict[str, float] = {}
    for row in match_rows:
        uuid = str(row.get("uuid") or "")
        if uuid:
            match_scores[uuid] = _record_score(row)
    match_uuids = list(match_scores)
    if search_filter.edge_uuids:
        allowed_edge_uuids = set(search_filter.edge_uuids)
        match_uuids = [uuid for uuid in match_uuids if uuid in allowed_edge_uuids]
    if not match_uuids:
        return []

    hydrate_filter = SearchFilter(
        node_labels=search_filter.node_labels,
        project_ids=search_filter.project_ids,
        edge_types=search_filter.edge_types,
    )
    filter_clauses, filter_params = _edge_filter_clause(hydrate_filter)
    rows = await _execute_query_records(
        client,
        _edge_select()
        + " WHERE "
        + _where_clause(["uuid IN $match_uuids", "group_id = $group_id", *filter_clauses])
        + " LIMIT $limit;",
        match_uuids=match_uuids,
        group_id=plan.organization_id,
        limit=len(match_uuids),
        **filter_params,
    )
    rows_by_uuid = {str(row["uuid"]): row for row in rows if row.get("uuid")}
    candidates = [
        _candidate_from_edge_record(
            rows_by_uuid[uuid],
            signal=RetrievalSignal.EDGE_FULLTEXT,
            score=match_scores[uuid],
        )
        for uuid in match_uuids
        if uuid in rows_by_uuid
    ]
    return candidates[:result_limit]


async def _vector_candidate_sources(
    *,
    client: Any,
    plan: RetrievalPlan,
    search_filter: SearchFilter,
    embedding_provider: EmbeddingProvider | None,
) -> list[list[RetrievalCandidate]]:
    result = await _vector_candidate_sources_detailed(
        client=client,
        plan=plan,
        search_filter=search_filter,
        embedding_provider=embedding_provider,
    )
    return [result.node_candidates, result.edge_candidates]


async def _vector_candidate_sources_detailed(
    *,
    client: Any,
    plan: RetrievalPlan,
    search_filter: SearchFilter,
    embedding_provider: EmbeddingProvider | None,
) -> VectorCandidateFetch:
    vector_requested = (
        RetrievalSignal.NODE_VECTOR in plan.signals or RetrievalSignal.EDGE_VECTOR in plan.signals
    )
    empty = VectorCandidateFetch(
        node_candidates=[],
        edge_candidates=[],
        requested=vector_requested,
        attempted=False,
    )
    if embedding_provider is None:
        return empty
    if not vector_requested:
        return empty
    try:
        embeddings = await embedding_provider.embed_texts([plan.query], input_kind="query")
    except Exception as exc:
        log.warning(
            "vector_embedding_failed",
            organization_id=plan.organization_id,
            query_length=len(plan.query),
            error_type=type(exc).__name__,
        )
        return VectorCandidateFetch(
            node_candidates=[],
            edge_candidates=[],
            requested=True,
            attempted=True,
            failures=(f"embedding:{type(exc).__name__}",),
            reason="embedding_failed",
        )
    try:
        query_embedding = _query_embedding_from_batch(
            embeddings,
            dimensions=embedding_provider.metadata.dimensions,
        )
    except ValueError as exc:
        log.warning(
            "vector_embedding_invalid",
            organization_id=plan.organization_id,
            error=str(exc),
        )
        return VectorCandidateFetch(
            node_candidates=[],
            edge_candidates=[],
            requested=True,
            attempted=True,
            failures=("embedding:invalid",),
            reason="invalid_embedding",
        )
    node_candidates: list[RetrievalCandidate] = []
    edge_candidates: list[RetrievalCandidate] = []
    tasks: list[Awaitable[list[RetrievalCandidate]]] = []
    task_signals: list[RetrievalSignal] = []
    if RetrievalSignal.NODE_VECTOR in plan.signals:
        tasks.append(
            _node_vector_candidates(
                client=client,
                plan=plan,
                search_filter=search_filter,
                query_embedding=query_embedding,
                embedding_metadata=embedding_provider.metadata,
                limit=plan.candidate_limits.node_vector,
            )
        )
        task_signals.append(RetrievalSignal.NODE_VECTOR)
    if RetrievalSignal.EDGE_VECTOR in plan.signals:
        tasks.append(
            _edge_vector_candidates(
                client=client,
                plan=plan,
                search_filter=search_filter,
                query_embedding=query_embedding,
                embedding_metadata=embedding_provider.metadata,
                limit=plan.candidate_limits.edge_vector,
            )
        )
        task_signals.append(RetrievalSignal.EDGE_VECTOR)
    gathered = await asyncio.gather(*tasks, return_exceptions=True)
    failures: list[str] = []
    for signal, result in zip(task_signals, gathered, strict=True):
        if isinstance(result, BaseException):
            failures.append(f"{signal.value}:{type(result).__name__}")
            log.warning(
                "vector_query_failed",
                organization_id=plan.organization_id,
                signal=signal.value,
                error_type=type(result).__name__,
            )
            continue
        if signal is RetrievalSignal.NODE_VECTOR:
            node_candidates = _candidate_list_or_empty(result)
        else:
            edge_candidates = _candidate_list_or_empty(result)
    return VectorCandidateFetch(
        node_candidates=node_candidates,
        edge_candidates=edge_candidates,
        requested=True,
        attempted=True,
        failures=tuple(failures),
    )


async def _node_vector_candidates(
    *,
    client: Any,
    plan: RetrievalPlan,
    search_filter: SearchFilter,
    query_embedding: Sequence[float],
    embedding_metadata: EmbeddingMetadata,
    limit: int,
) -> list[RetrievalCandidate]:
    if limit <= 0:
        return []
    filter_clauses, filter_params = _node_filter_clause(search_filter)
    candidate_limit = max(int(limit), 1)
    knn_effort = knn_search_effort(candidate_limit, core_config.graph_knn_ef)
    overfetch = search_filter.knn_type_overfetch
    if search_filter.node_types and overfetch > 0:
        # A selective predicate beside the HNSW bracket forces the walk 10-15x
        # deeper regardless of syntax, so the arm walks an untyped pool and
        # filters types outside the bracket. A full head is exactly the typed
        # KNN head; a shortfall falls back to the classic form below.
        pool = knn_overfetch_pool(candidate_limit, overfetch)
        pool_knn_effort = knn_search_effort(pool, core_config.graph_knn_ef)
        overfetch_clauses = [
            clause for clause in filter_clauses if clause != "entity_type IN $node_types"
        ]
        rows = await _execute_query_records(
            client,
            """
            SELECT *
            FROM (
                SELECT *,
                       (1 - vector::distance::knn()) AS score
                FROM entity
                WHERE """
            + _where_clause(["group_id = $group_id", *overfetch_clauses])
            + f"""
                  AND name_embedding <|{pool}, {pool_knn_effort}|> $query_embedding
            )
            WHERE score >= $min_score AND entity_type IN $node_types
            ORDER BY score DESC, created_at DESC, uuid DESC
            LIMIT $limit;
            """,
            group_id=plan.organization_id,
            query_embedding=list(query_embedding),
            min_score=plan.vector_min_score,
            limit=candidate_limit,
            **filter_params,
        )
        if len(rows) >= candidate_limit:
            return [
                _candidate_from_node_record(
                    row,
                    signal=RetrievalSignal.NODE_VECTOR,
                    score=_record_score(row),
                    embedding_metadata=embedding_metadata,
                )
                for row in rows
            ]
    rows = await _execute_query_records(
        client,
        """
        SELECT *
        FROM (
            SELECT *,
                   (1 - vector::distance::knn()) AS score
            FROM entity
            WHERE """
        + _where_clause(["group_id = $group_id", *filter_clauses])
        + f"""
              AND name_embedding <|{candidate_limit}, {knn_effort}|> $query_embedding
        )
        WHERE score >= $min_score
        ORDER BY score DESC, created_at DESC, uuid DESC
        LIMIT $limit;
        """,
        group_id=plan.organization_id,
        query_embedding=list(query_embedding),
        min_score=plan.vector_min_score,
        limit=candidate_limit,
        **filter_params,
    )
    return [
        _candidate_from_node_record(
            row,
            signal=RetrievalSignal.NODE_VECTOR,
            score=_record_score(row),
            embedding_metadata=embedding_metadata,
        )
        for row in rows
    ]


async def _edge_vector_candidates(
    *,
    client: Any,
    plan: RetrievalPlan,
    search_filter: SearchFilter,
    query_embedding: Sequence[float],
    embedding_metadata: EmbeddingMetadata,
    limit: int,
) -> list[RetrievalCandidate]:
    if limit <= 0:
        return []
    filter_clauses, filter_params = _edge_filter_clause(search_filter)
    candidate_limit = max(int(limit), 1)
    knn_effort = knn_search_effort(candidate_limit, core_config.graph_knn_ef)
    overfetch = search_filter.knn_type_overfetch
    if search_filter.edge_types and overfetch > 0:
        # Same HNSW planner trap as the node lane; the edge-type filter moves
        # outside the bracket and a shortfall falls back to the classic form.
        pool = knn_overfetch_pool(candidate_limit, overfetch)
        pool_knn_effort = knn_search_effort(pool, core_config.graph_knn_ef)
        overfetch_clauses = [clause for clause in filter_clauses if clause != "name IN $edge_types"]
        rows = await _execute_query_records(
            client,
            "SELECT * FROM ("
            + _edge_select(extra="(1 - vector::distance::knn()) AS score")
            + " WHERE "
            + _where_clause(["group_id = $group_id", *overfetch_clauses])
            + f"""
              AND fact_embedding <|{pool}, {pool_knn_effort}|> $query_embedding
            )
            WHERE score >= $min_score AND name IN $edge_types
            ORDER BY score DESC, created_at DESC, uuid DESC
            LIMIT $limit;
            """,
            group_id=plan.organization_id,
            query_embedding=list(query_embedding),
            min_score=plan.vector_min_score,
            limit=candidate_limit,
            **filter_params,
        )
        if len(rows) >= candidate_limit:
            return [
                _candidate_from_edge_record(
                    row,
                    signal=RetrievalSignal.EDGE_VECTOR,
                    score=_record_score(row),
                    embedding_metadata=embedding_metadata,
                )
                for row in rows
            ]
    rows = await _execute_query_records(
        client,
        "SELECT * FROM ("
        + _edge_select(extra="(1 - vector::distance::knn()) AS score")
        + " WHERE "
        + _where_clause(["group_id = $group_id", *filter_clauses])
        + f"""
          AND fact_embedding <|{candidate_limit}, {knn_effort}|> $query_embedding
        )
        WHERE score >= $min_score
        ORDER BY score DESC, created_at DESC, uuid DESC
        LIMIT $limit;
        """,
        group_id=plan.organization_id,
        query_embedding=list(query_embedding),
        min_score=plan.vector_min_score,
        limit=candidate_limit,
        **filter_params,
    )
    return [
        _candidate_from_edge_record(
            row,
            signal=RetrievalSignal.EDGE_VECTOR,
            score=_record_score(row),
            embedding_metadata=embedding_metadata,
        )
        for row in rows
    ]


def _query_embedding_from_batch(
    embeddings: Sequence[Sequence[float]],
    *,
    dimensions: int,
) -> list[float]:
    if not embeddings:
        raise ValueError("embedding provider returned no vectors")
    embedding = [float(value) for value in embeddings[0]]
    if len(embedding) != dimensions:
        raise ValueError(
            f"embedding provider returned {len(embedding)} dimensions, expected {dimensions}"
        )
    return embedding


async def _graph_expansion_candidates(
    *,
    client: Any,
    plan: RetrievalPlan,
    search_filter: SearchFilter,
    seed_candidates: Sequence[RetrievalCandidate],
    limit: int,
) -> list[RetrievalCandidate]:
    entity_seed_uuids = [
        candidate.id
        for candidate in seed_candidates
        if candidate.type not in {"claim", "relationship", "raw_memory", "episode"}
    ][:limit]
    episode_seed_uuids = [
        candidate.id for candidate in seed_candidates if candidate.type == "episode"
    ][:limit]
    if not entity_seed_uuids and not episode_seed_uuids:
        return []

    rows = await _node_bfs_records(
        client=client,
        origin_uuids=entity_seed_uuids,
        episode_origin_uuids=episode_seed_uuids,
        search_filter=search_filter,
        group_id=plan.organization_id,
        max_depth=plan.graph_expansion_depth,
        limit=limit,
    )
    return [
        _candidate_from_node_record(
            row,
            signal=RetrievalSignal.GRAPH_EXPANSION,
            score=_record_score(row),
        )
        for row in rows
    ]


def _where_clause(clauses: Sequence[str]) -> str:
    active = [clause for clause in clauses if clause]
    return " AND ".join(active) if active else "true"


def _node_filter_clause(search_filter: SearchFilter) -> tuple[list[str], dict[str, Any]]:
    clauses: list[str] = []
    params: dict[str, Any] = {}
    if search_filter.node_types:
        clauses.append("entity_type IN $node_types")
        params["node_types"] = list(search_filter.node_types)
    if search_filter.node_labels:
        clauses.append("labels CONTAINS $node_label")
        params["node_label"] = search_filter.node_labels[0]
    if search_filter.project_ids:
        clauses.append("(project_id IN $project_ids OR attributes.project_id IN $project_ids)")
        params["project_ids"] = list(search_filter.project_ids)
    return clauses, params


def _edge_filter_clause(
    search_filter: SearchFilter,
    *,
    source_node_uuid: str | None = None,
    target_node_uuid: str | None = None,
) -> tuple[list[str], dict[str, Any]]:
    clauses: list[str] = []
    params: dict[str, Any] = {}
    if search_filter.edge_uuids:
        clauses.append("uuid IN $edge_uuids")
        params["edge_uuids"] = list(search_filter.edge_uuids)
    if search_filter.edge_types:
        clauses.append("name IN $edge_types")
        params["edge_types"] = list(search_filter.edge_types)
    if search_filter.node_labels:
        clauses.append("in.labels CONTAINS $node_label AND out.labels CONTAINS $node_label")
        params["node_label"] = search_filter.node_labels[0]
    if search_filter.project_ids:
        clauses.append(
            "("
            "attributes.project_id IN $project_ids "
            "OR in.project_id IN $project_ids "
            "OR in.attributes.project_id IN $project_ids "
            "OR out.project_id IN $project_ids "
            "OR out.attributes.project_id IN $project_ids"
            ")"
        )
        params["project_ids"] = list(search_filter.project_ids)
    if source_node_uuid is not None:
        clauses.append("source_id = $source_node_uuid")
        params["source_node_uuid"] = source_node_uuid
    if target_node_uuid is not None:
        clauses.append("target_id = $target_node_uuid")
        params["target_node_uuid"] = target_node_uuid
    return clauses, params


def _edge_match_filter_clause(
    search_filter: SearchFilter,
) -> tuple[list[str], dict[str, Any]]:
    return _edge_filter_clause(search_filter)


def _edge_select(extra: str | None = None) -> str:
    extra_select = f", {extra}" if extra else ""
    return f"""
        SELECT uuid, name, fact, fact_embedding, group_id, episodes, attributes,
               created_at, expired_at, valid_at, invalid_at,
               source_id AS source_node_uuid,
               target_id AS target_node_uuid,
               in.project_id AS source_node_project_id,
               out.project_id AS target_node_project_id{extra_select}
        FROM relates_to
    """


async def _node_bfs_records(
    *,
    client: Any,
    origin_uuids: Sequence[str],
    episode_origin_uuids: Sequence[str] = (),
    search_filter: SearchFilter,
    group_id: str,
    max_depth: int,
    limit: int,
    relationship_names: Sequence[str] = (),
    include_incoming: bool = False,
    include_community_hops: bool = True,
) -> list[dict[str, object]]:
    if (not origin_uuids and not episode_origin_uuids) or max_depth < 1:
        return []

    wanted = {str(name).upper() for name in relationship_names if str(name).strip()}
    discovered: list[_GraphExpansionHop] = []
    seen_discovered: set[str] = set()
    visited_entities = set(origin_uuids)
    entity_frontier = _dedupe_strings(origin_uuids)
    episode_frontier = _dedupe_strings(episode_origin_uuids)

    for depth in range(1, max_depth + 1):
        next_entities: list[str] = []
        remaining = max(int(limit) - len(discovered), 0)
        if remaining <= 0:
            break
        fetch_limit = _graph_expansion_fetch_limit(remaining)
        next_hops: list[_GraphExpansionHop] = []
        if depth == 1 and _hop_relationship_wanted("MENTIONS", wanted):
            next_hops.extend(
                await _mentioned_entity_hops(
                    client=client,
                    episode_uuids=episode_frontier,
                    group_id=group_id,
                    depth=depth,
                    limit=fetch_limit,
                )
            )
        next_hops.extend(
            await _relation_target_hops(
                client=client,
                source_uuids=entity_frontier,
                group_id=group_id,
                depth=depth,
                limit=fetch_limit,
                relationship_names=sorted(wanted),
                exclude_relationship_names=(
                    () if _SUPERSEDES_PREDICATE in wanted else (_SUPERSEDES_PREDICATE,)
                ),
            )
        )
        if include_incoming:
            next_hops.extend(
                await _relation_source_hops(
                    client=client,
                    target_uuids=entity_frontier,
                    group_id=group_id,
                    depth=depth,
                    limit=fetch_limit,
                    relationship_names=sorted(wanted),
                )
            )
        if (
            depth == 1
            and include_community_hops
            and _hop_relationship_wanted("SHARES_COMMUNITY", wanted)
        ):
            next_hops.extend(
                await _community_member_hops(
                    client=client,
                    source_uuids=entity_frontier,
                    group_id=group_id,
                    depth=depth,
                    limit=fetch_limit,
                )
            )
        next_entities.extend(hop.uuid for hop in next_hops)

        for hop in sorted(
            _dedupe_expansion_hops(next_hops),
            key=lambda value: value.score,
            reverse=True,
        ):
            if hop.uuid in seen_discovered:
                continue
            seen_discovered.add(hop.uuid)
            discovered.append(hop)
            if len(discovered) >= limit:
                return await _hydrate_graph_expansion_records(
                    client=client,
                    hops=discovered,
                    search_filter=search_filter,
                    group_id=group_id,
                    limit=limit,
                )

        entity_frontier = [
            uuid for uuid in _dedupe_strings(next_entities) if uuid not in visited_entities
        ]
        visited_entities.update(entity_frontier)
        if not entity_frontier:
            break

    return await _hydrate_graph_expansion_records(
        client=client,
        hops=discovered,
        search_filter=search_filter,
        group_id=group_id,
        limit=limit,
    )


async def _mentioned_entity_hops(
    *,
    client: Any,
    episode_uuids: Sequence[str],
    group_id: str,
    depth: int,
    limit: int,
) -> list[_GraphExpansionHop]:
    if not episode_uuids:
        return []
    rows = await _execute_query_records(
        client,
        """
        SELECT target_id AS uuid
        FROM mentions
        WHERE source_id IN $episode_uuids
          AND group_id = $group_id
          AND out.group_id = $group_id
        LIMIT $limit;
        """,
        episode_uuids=list(episode_uuids),
        group_id=group_id,
        limit=max(int(limit), 1),
    )
    return [
        _GraphExpansionHop(
            uuid=uuid,
            depth=depth,
            relationship="MENTIONS",
            score=_graph_expansion_path_score("MENTIONS", depth=depth),
        )
        for uuid in _dedupe_strings(_record_uuids(rows))
    ]


def _hop_relationship_wanted(relationship: str, wanted: set[str]) -> bool:
    """Whether a synthesized hop label survives the caller's relationship filter.

    ``MENTIONS`` and ``SHARES_COMMUNITY`` are labels the expanders mint rather
    than edge names a query can filter on, so the filter is applied by skipping
    the expander instead of narrowing its ``WHERE``.
    """
    return not wanted or relationship in wanted


async def _relation_target_hops(
    *,
    client: Any,
    source_uuids: Sequence[str],
    group_id: str,
    depth: int,
    limit: int,
    relationship_names: Sequence[str] = (),
    exclude_relationship_names: Sequence[str] = (),
) -> list[_GraphExpansionHop]:
    """Walk edges that point away from the frontier.

    `exclude_relationship_names` exists for predicates whose written direction
    makes the outgoing endpoint the wrong answer. A SUPERSEDES edge is stored
    new-row to old-row, so walking it outwards lands on the row the writer
    declared replaced, and the weight table would then score that stale row at
    0.95 -- a supersession would raise its own victim.
    """
    if not source_uuids:
        return []
    name_clause = "AND name IN $relationship_names" if relationship_names else ""
    exclude_clause = (
        "AND name NOT IN $exclude_relationship_names" if exclude_relationship_names else ""
    )
    rows = await _execute_query_records(
        client,
        f"""
        SELECT target_id AS uuid, name AS relationship
        FROM relates_to
        WHERE source_id IN $source_uuids
          AND group_id = $group_id
          AND out.group_id = $group_id
          {name_clause}
          {exclude_clause}
        LIMIT $limit;
        """,
        source_uuids=list(source_uuids),
        group_id=group_id,
        limit=max(int(limit), 1),
        **({"relationship_names": list(relationship_names)} if relationship_names else {}),
        **(
            {"exclude_relationship_names": list(exclude_relationship_names)}
            if exclude_relationship_names
            else {}
        ),
    )
    hops: list[_GraphExpansionHop] = []
    for row in rows:
        uuid = _string_value(row.get("uuid"))
        if not uuid:
            continue
        relationship = _string_value(row.get("relationship")) or "RELATED_TO"
        hops.append(
            _GraphExpansionHop(
                uuid=uuid,
                depth=depth,
                relationship=relationship,
                score=_graph_expansion_path_score(relationship, depth=depth),
            )
        )
    return _dedupe_expansion_hops(hops)


async def _relation_source_hops(
    *,
    client: Any,
    target_uuids: Sequence[str],
    group_id: str,
    depth: int,
    limit: int,
    relationship_names: Sequence[str] = (),
) -> list[_GraphExpansionHop]:
    """Walk edges that point AT the frontier rather than away from it.

    Retrieval's own expansion lane only follows outgoing edges, which is fine
    when the seeds came from a scored search. A caller steering the walk itself
    needs the other side too: the tasks that depend on this one and the passages
    cut from this memory are all inbound, and an outgoing-only neighborhood
    reports them as absent.
    """
    if not target_uuids:
        return []
    name_clause = "AND name IN $relationship_names" if relationship_names else ""
    rows = await _execute_query_records(
        client,
        f"""
        SELECT source_id AS uuid, name AS relationship
        FROM relates_to
        WHERE target_id IN $target_uuids
          AND group_id = $group_id
          AND in.group_id = $group_id
          {name_clause}
        LIMIT $limit;
        """,
        target_uuids=list(target_uuids),
        group_id=group_id,
        limit=max(int(limit), 1),
        **({"relationship_names": list(relationship_names)} if relationship_names else {}),
    )
    hops: list[_GraphExpansionHop] = []
    for row in rows:
        uuid = _string_value(row.get("uuid"))
        if not uuid:
            continue
        relationship = _string_value(row.get("relationship")) or "RELATED_TO"
        hops.append(
            _GraphExpansionHop(
                uuid=uuid,
                depth=depth,
                relationship=relationship,
                score=_graph_expansion_path_score(relationship, depth=depth),
                direction="incoming",
            )
        )
    return _dedupe_expansion_hops(hops)


async def _community_member_hops(
    *,
    client: Any,
    source_uuids: Sequence[str],
    group_id: str,
    depth: int,
    limit: int,
) -> list[_GraphExpansionHop]:
    if not source_uuids:
        return []
    community_ids = await _community_ids_for_entities(
        client=client,
        source_uuids=source_uuids,
        group_id=group_id,
        limit=limit,
    )
    if not community_ids:
        return []
    rows = await _execute_query_records(
        client,
        """
        SELECT source_id AS uuid, target_id AS community_id
        FROM relates_to
        WHERE target_id IN $community_uuids
          AND source_id NOT IN $source_uuids
          AND name = "BELONGS_TO"
          AND group_id = $group_id
          AND in.group_id = $group_id
          AND in.entity_type != "community"
        ORDER BY target_id
        LIMIT $limit;
        """,
        community_uuids=community_ids,
        source_uuids=list(source_uuids),
        group_id=group_id,
        limit=max(int(limit), 1),
    )
    hops: list[_GraphExpansionHop] = []
    for row in rows:
        uuid = _string_value(row.get("uuid"))
        if not uuid:
            continue
        hops.append(
            _GraphExpansionHop(
                uuid=uuid,
                depth=depth,
                relationship="SHARES_COMMUNITY",
                score=_graph_expansion_path_score("SHARES_COMMUNITY", depth=depth),
                community_id=_string_value(row.get("community_id")),
            )
        )
    return _dedupe_expansion_hops(hops)


async def _community_ids_for_entities(
    *,
    client: Any,
    source_uuids: Sequence[str],
    group_id: str,
    limit: int,
) -> list[str]:
    rows = await _execute_query_records(
        client,
        """
        SELECT target_id AS uuid
        FROM relates_to
        WHERE source_id IN $source_uuids
          AND name = "BELONGS_TO"
          AND group_id = $group_id
          AND out.group_id = $group_id
          AND out.entity_type = "community"
        LIMIT $limit;
        """,
        source_uuids=list(source_uuids),
        group_id=group_id,
        limit=max(int(limit), 1),
    )
    return _dedupe_strings(_record_uuids(rows))


async def _hydrate_graph_expansion_records(
    *,
    client: Any,
    hops: Sequence[_GraphExpansionHop],
    search_filter: SearchFilter,
    group_id: str,
    limit: int,
) -> list[dict[str, object]]:
    uuids = [hop.uuid for hop in hops]
    rows = await _hydrate_entity_records(
        client=client,
        uuids=uuids,
        search_filter=search_filter,
        group_id=group_id,
        limit=limit,
    )
    hops_by_uuid = {hop.uuid: hop for hop in hops}
    records: list[dict[str, object]] = []
    for row in rows:
        uuid = _string_value(row.get("uuid"))
        hop = hops_by_uuid.get(uuid or "")
        if hop is None:
            continue
        record = dict(row)
        record["score"] = hop.score
        record["graph_expansion_depth"] = hop.depth
        record["graph_expansion_relationship"] = hop.relationship
        record["graph_expansion_score"] = hop.score
        # Not in _GRAPH_EXPANSION_METADATA_KEYS on purpose: the scored lanes copy
        # only the listed keys onto a candidate, so direction reaches a caller
        # that reads the record directly and stays out of search result metadata.
        record["graph_expansion_direction"] = hop.direction
        if hop.community_id:
            record["graph_expansion_community_id"] = hop.community_id
        records.append(record)
    records.sort(key=_record_score, reverse=True)
    return records


async def expand_neighbor_records(
    *,
    client: Any,
    origin_uuids: Sequence[str],
    group_id: str,
    max_depth: int,
    limit: int,
    relationship_names: Sequence[str] = (),
    include_incoming: bool = True,
    search_filter: SearchFilter | None = None,
    row_allowed: Callable[[Mapping[str, object]], bool] | None = None,
    row_included: Callable[[Mapping[str, object]], bool] | None = None,
) -> list[dict[str, object]]:
    """Walk one bounded neighborhood for a caller that steers its own retrieval.

    Hop-tagged entity rows, highest path score first, capped at ``limit``.

    ``row_allowed`` is the reader's authorization check, and it gates the walk
    rather than only its output. The walk advances one hop at a time and only
    authorized rows join the next frontier, so a row the reader may not see is
    not merely withheld: it is not a route. Filtering only the final result would
    still return a depth-two neighbor reachable exclusively through another
    principal's private memory, which discloses that something sits between them.

    The check runs before the cap, and each hop is widened by the same headroom
    the scored lanes read with, because a handful of unreadable rows outranking
    the readable ones would otherwise consume the whole budget and report a
    neighborhood as empty when it is not.

    ``row_included`` narrows what the caller sees without narrowing where the walk
    may go. The two are deliberately separate: authorization is about reachability,
    while a presentational filter such as an entity-type restriction must not
    silently sever a route, or asking for one type of neighbor would hide the
    two-hop rows that are only reachable through another type.
    """
    limit = max(int(limit), 0)
    depth_budget = max(int(max_depth), 1)
    effective_filter = search_filter if search_filter is not None else SearchFilter()
    if not limit:
        return []

    collected: dict[str, dict[str, object]] = {}
    # Origins are the caller's own rows. Excluding them from results as well as
    # from re-expansion is what stops a seed being returned as its own neighbor,
    # which every inbound edge makes reachable at depth two.
    seen = {str(uuid) for uuid in origin_uuids if str(uuid)}
    frontier = _dedupe_strings(origin_uuids)

    for depth in range(1, depth_budget + 1):
        if not frontier or len(collected) >= limit:
            break
        hop_rows = await _node_bfs_records(
            client=client,
            origin_uuids=frontier,
            search_filter=effective_filter,
            group_id=group_id,
            max_depth=1,
            limit=_graph_expansion_fetch_limit(limit),
            relationship_names=relationship_names,
            include_incoming=include_incoming,
            # Every round is depth 1 as far as the shared function can tell, so
            # the first-hop-only community lane would otherwise re-fire on each
            # one and widen the neighborhood in a way the scored lane never does.
            include_community_hops=depth == 1,
        )
        next_frontier: list[str] = []
        for row in hop_rows:
            uuid = _string_value(row.get("uuid"))
            if not uuid or uuid in seen:
                continue
            seen.add(uuid)
            if row_allowed is not None and not row_allowed(row):
                # Unauthorized, so not a result and not a route either.
                continue
            # Authorized, so it is a route whatever the caller wants to see.
            next_frontier.append(uuid)
            if row_included is not None and not row_included(row):
                # Filtered from the answer, but still walked through.
                continue
            # The shared function reports every round as adjacent, because each
            # round is one hop from its own frontier. True distance from the
            # caller's seeds, and the decay it earns, are this walk's to state.
            relationship = _string_value(row.get("graph_expansion_relationship")) or "RELATED_TO"
            score = _graph_expansion_path_score(relationship, depth=depth)
            row["graph_expansion_depth"] = depth
            row["graph_expansion_score"] = score
            row["score"] = score
            collected[uuid] = row
        frontier = next_frontier

    ordered = sorted(collected.values(), key=_record_score, reverse=True)
    return ordered[:limit]


async def _hydrate_entity_records(
    *,
    client: Any,
    uuids: Sequence[str],
    search_filter: SearchFilter,
    group_id: str,
    limit: int,
) -> list[dict[str, object]]:
    if not uuids:
        return []
    filter_clauses, filter_params = _node_filter_clause(search_filter)
    rows = await _execute_query_records(
        client,
        "SELECT * FROM entity WHERE "
        + _where_clause(["uuid IN $uuids", "group_id = $group_id", *filter_clauses])
        + " LIMIT $limit;",
        uuids=list(uuids),
        group_id=group_id,
        limit=max(int(limit), 1),
        **filter_params,
    )
    rows_by_uuid = {str(row["uuid"]): row for row in rows if row.get("uuid")}
    return [rows_by_uuid[uuid] for uuid in uuids if uuid in rows_by_uuid]


def _record_uuids(rows: Sequence[Mapping[str, object]]) -> list[str]:
    return [str(row["uuid"]) for row in rows if row.get("uuid")]


def _dedupe_strings(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(str(value) for value in values if str(value)))


def _dedupe_expansion_hops(
    hops: Iterable[_GraphExpansionHop],
) -> list[_GraphExpansionHop]:
    deduped: dict[str, _GraphExpansionHop] = {}
    for hop in hops:
        if hop.uuid not in deduped or hop.score > deduped[hop.uuid].score:
            deduped[hop.uuid] = hop
    return list(deduped.values())


def _graph_expansion_path_score(relationship: str, *, depth: int) -> float:
    normalized = relationship.upper()
    base = _GRAPH_EXPANSION_RELATIONSHIP_WEIGHTS.get(normalized, 0.64)
    depth_multiplier = _GRAPH_EXPANSION_DEPTH_DECAY ** max(depth - 1, 0)
    return max(min(base * depth_multiplier, 1.0), 0.1)


def _graph_expansion_fetch_limit(limit: int) -> int:
    return max(int(limit) * _GRAPH_EXPANSION_FETCH_HEADROOM, 1)


def _candidate_from_node_record(
    row: Mapping[str, object],
    *,
    signal: RetrievalSignal,
    score: float,
    embedding_metadata: EmbeddingMetadata | None = None,
) -> RetrievalCandidate:
    attributes = _record_attributes(row)
    entity_type = _entity_type_for_record(row, attributes)
    content = _content_for_record(row, attributes)
    project_id = _string_value(row.get("project_id") or attributes.get("project_id"))
    source = _string_value(
        row.get("source_id")
        or attributes.get("source_id")
        or attributes.get("source")
        or row.get("source_file")
        or attributes.get("source_file")
        or row.get("uuid")
    )
    metadata = {
        **attributes,
        **_selected_record_metadata(row),
        "entity_type": entity_type,
        "source_id": source,
        "retrieval_signals": [signal.value],
    }
    if embedding_metadata is not None:
        metadata["embedding_metadata"] = embedding_metadata.to_dict()
    visibility = "project" if project_id else "organization"
    policy_reason = "project_access_verified" if project_id else "graph_projection_allowed"
    return RetrievalCandidate(
        id=str(row.get("uuid", "")),
        type=entity_type,
        name=str(row.get("name") or entity_type),
        content=content,
        score=score,
        source=source,
        metadata=metadata,
        project_id=project_id,
        created_at=_datetime_value(row.get("created_at")),
        policy_reason=policy_reason,
        visibility=visibility,
        kind=CandidateKind.NODE,
        retrieval_signals=(signal.value,),
        scope=CandidateScope(
            organization_id=_string_value(row.get("group_id")),
            project_id=project_id,
            visibility=visibility,
            policy_reason=policy_reason,
        ),
    )


def _candidate_from_episode_record(
    row: Mapping[str, object],
    *,
    signal: RetrievalSignal,
    score: float,
) -> RetrievalCandidate:
    source = _string_value(row.get("source_description")) or _string_value(row.get("uuid"))
    policy_reason = "graph_projection_allowed"
    visibility = "organization"
    metadata = {
        **_selected_record_metadata(row),
        "entity_type": "episode",
        "source_id": source,
        "retrieval_signals": [signal.value],
    }
    return RetrievalCandidate(
        id=str(row.get("uuid", "")),
        type="episode",
        name=str(row.get("name") or "Episode"),
        content=str(row.get("content") or ""),
        score=score,
        source=source,
        metadata=metadata,
        created_at=_datetime_value(row.get("created_at")),
        policy_reason=policy_reason,
        visibility=visibility,
        kind=CandidateKind.EPISODE,
        retrieval_signals=(signal.value,),
        scope=CandidateScope(
            organization_id=_string_value(row.get("group_id")),
            visibility=visibility,
            policy_reason=policy_reason,
        ),
    )


def _candidate_from_edge_record(
    row: Mapping[str, object],
    *,
    signal: RetrievalSignal,
    score: float,
    embedding_metadata: EmbeddingMetadata | None = None,
) -> RetrievalCandidate:
    attributes = _record_attributes(row)
    source = _string_value(attributes.get("source_id") or row.get("uuid"))
    metadata = {
        **attributes,
        **_selected_edge_metadata(row),
        "entity_type": "claim",
        "relationship": _string_value(row.get("name")),
        "source_id": source,
        "source_node_uuid": _string_value(row.get("source_node_uuid")),
        "target_node_uuid": _string_value(row.get("target_node_uuid")),
        "retrieval_signals": [signal.value],
    }
    if embedding_metadata is not None:
        metadata["embedding_metadata"] = embedding_metadata.to_dict()
    policy_reason = "graph_projection_allowed"
    visibility = "organization"
    return RetrievalCandidate(
        id=str(row.get("uuid", "")),
        type="claim",
        name=str(row.get("name") or "Relationship"),
        content=str(row.get("fact") or ""),
        score=score,
        source=source,
        metadata=metadata,
        project_id=_string_value(metadata.get("project_id")),
        created_at=_datetime_value(row.get("created_at")),
        policy_reason=policy_reason,
        visibility=visibility,
        kind=CandidateKind.EDGE,
        retrieval_signals=(signal.value,),
        scope=CandidateScope(
            organization_id=_string_value(row.get("group_id")),
            project_id=_string_value(metadata.get("project_id")),
            visibility=visibility,
            policy_reason=policy_reason,
        ),
    )


def _record_attributes(row: Mapping[str, object]) -> dict[str, Any]:
    raw = row.get("attributes")
    if not isinstance(raw, Mapping):
        return {}
    return {str(key): value for key, value in raw.items()}


def _entity_type_for_record(
    row: Mapping[str, object],
    attributes: Mapping[str, Any],
) -> str:
    for value in (
        attributes.get("entity_type"),
        row.get("entity_type"),
        *_labels_without_entity_record(row),
    ):
        if text := _string_value(value):
            return text.lower()
    return "artifact"


def _labels_without_entity_record(row: Mapping[str, object]) -> list[str]:
    labels = row.get("labels")
    if not isinstance(labels, list | tuple):
        return []
    return [str(label) for label in labels if str(label).lower() != "entity"]


def _content_for_record(
    row: Mapping[str, object],
    attributes: Mapping[str, Any],
) -> str:
    # For memory-shaped rows the description is a derived blurb: often
    # content[:500], and for passages one shared summary across every span of an
    # observation. Either way it cannot stand in for content, so the full-text
    # carriers are exhausted first. Tasks invert this and are resolved by the
    # active-work path in tools/context.py rather than here.
    for value in (
        row.get("content"),
        attributes.get("content"),
        attributes.get("description"),
        row.get("description"),
        row.get("summary"),
    ):
        if text := _string_value(value):
            return text
    return ""


def _selected_record_metadata(row: Mapping[str, object]) -> dict[str, object]:
    metadata: dict[str, object] = {}
    for key in (
        "status",
        "priority",
        "complexity",
        "feature",
        "tags",
        "retrieval_keys",
        "project_id",
        "epic_id",
        "task_id",
        "source_id",
        "source_ids",
        "confidence",
        "valid_at",
        "valid_from",
        "valid_to",
        "invalid_at",
        "created_by",
        "modified_by",
        "last_recalled_at",
        "last_used_at",
        "retrieval_count",
        "citation_count",
        "misled_count",
        *_GRAPH_EXPANSION_METADATA_KEYS,
    ):
        value = row.get(key)
        if value is not None:
            metadata[key] = value
    return metadata


def _selected_edge_metadata(row: Mapping[str, object]) -> dict[str, object]:
    metadata: dict[str, object] = {}
    for key in (
        "group_id",
        "project_id",
        "source_ids",
        "confidence",
        "valid_at",
        "valid_from",
        "valid_to",
        "invalid_at",
        "expired_at",
        "created_by",
        "modified_by",
        "episodes",
    ):
        value = row.get(key)
        if value is not None:
            metadata[key] = value
    return metadata


def _record_score(row: Mapping[str, object]) -> float:
    raw = row.get("score")
    if isinstance(raw, int | float):
        return float(raw)
    return 1.0


def _candidate_from_raw_memory(
    memory: RawMemory,
    scope: ScopeSpec,
) -> RetrievalCandidate:
    source = memory.source_id or memory.capture_surface
    project_id = (
        memory.metadata.get("project_id")
        or memory.project_id
        or (memory.scope_key if memory.memory_scope is MemoryScope.PROJECT else None)
    )
    metadata = {
        "source_id": source,
        "principal_id": memory.principal_id,
        "memory_scope": memory.memory_scope.value,
        "scope_key": memory.scope_key,
        "capture_surface": memory.capture_surface,
        "tags": list(memory.tags),
        **memory.metadata,
    }
    return RetrievalCandidate(
        id=f"raw_memory:{memory.id}",
        type="raw_memory",
        name=memory.title or "Untitled raw memory",
        content=memory.raw_content,
        score=memory.score,
        source=source,
        metadata=metadata,
        result_origin="graph",
        project_id=str(project_id) if project_id is not None else None,
        created_at=memory.captured_at,
        policy_reason=scope.policy_reason,
        visibility=memory.memory_scope.value,
        kind=CandidateKind.RAW_MEMORY,
        retrieval_signals=(RetrievalSignal.RAW_LEXICAL.value,),
        scope=CandidateScope(
            project_id=str(project_id) if project_id is not None else None,
            memory_scope=memory.memory_scope.value,
            scope_key=memory.scope_key,
            principal_id=memory.principal_id,
            visibility=memory.memory_scope.value,
            policy_reason=scope.policy_reason,
        ),
    )


def _candidate_from_node(
    node: Any,
    *,
    signal: RetrievalSignal,
    score: float,
) -> RetrievalCandidate:
    attributes = _attributes(node)
    entity_type = _entity_type_for_node(node, attributes)
    content = _content_for_node(node, attributes)
    project_id = _string_value(getattr(node, "project_id", None) or attributes.get("project_id"))
    source = _string_value(
        attributes.get("source_id")
        or attributes.get("source")
        or attributes.get("source_file")
        or getattr(node, "uuid", None)
    )
    visibility = "project" if project_id else "organization"
    policy_reason = "project_access_verified" if project_id else "graph_projection_allowed"
    return RetrievalCandidate(
        id=str(getattr(node, "uuid", "")),
        type=entity_type,
        name=str(getattr(node, "name", "") or entity_type),
        content=content,
        score=score,
        source=source,
        metadata={
            **attributes,
            "entity_type": entity_type,
            "source_id": source,
            "retrieval_signals": [signal.value],
        },
        project_id=project_id,
        created_at=_datetime_value(getattr(node, "created_at", None)),
        policy_reason=policy_reason,
        visibility=visibility,
        kind=CandidateKind.NODE,
        retrieval_signals=(signal.value,),
        scope=CandidateScope(
            organization_id=_string_value(getattr(node, "group_id", None)),
            project_id=project_id,
            visibility=visibility,
            policy_reason=policy_reason,
        ),
    )


def _candidate_from_episode(
    episode: Any,
    *,
    signal: RetrievalSignal,
    score: float,
) -> RetrievalCandidate:
    source = _string_value(getattr(episode, "source_description", None)) or _string_value(
        getattr(episode, "uuid", None)
    )
    policy_reason = "graph_projection_allowed"
    visibility = "organization"
    return RetrievalCandidate(
        id=str(getattr(episode, "uuid", "")),
        type="episode",
        name=str(getattr(episode, "name", "") or "Episode"),
        content=str(getattr(episode, "content", "") or ""),
        score=score,
        source=source,
        metadata={
            "entity_type": "episode",
            "source_id": source,
            "retrieval_signals": [signal.value],
        },
        created_at=_datetime_value(getattr(episode, "created_at", None)),
        policy_reason=policy_reason,
        visibility=visibility,
        kind=CandidateKind.EPISODE,
        retrieval_signals=(signal.value,),
        scope=CandidateScope(
            organization_id=_string_value(getattr(episode, "group_id", None)),
            visibility=visibility,
            policy_reason=policy_reason,
        ),
    )


def _candidate_from_edge(
    edge: Any,
    *,
    signal: RetrievalSignal,
    score: float,
) -> RetrievalCandidate:
    attributes = _attributes(edge)
    source = _string_value(attributes.get("source_id") or getattr(edge, "uuid", None))
    source_project_id = _string_value(
        getattr(edge, "source_node_project_id", None) or attributes.get("source_node_project_id")
    )
    target_project_id = _string_value(
        getattr(edge, "target_node_project_id", None) or attributes.get("target_node_project_id")
    )
    project_id = _string_value(attributes.get("project_id"))
    policy_reason = "graph_projection_allowed"
    visibility = "organization"
    return RetrievalCandidate(
        id=str(getattr(edge, "uuid", "")),
        type="claim",
        name=str(getattr(edge, "name", "") or "Relationship"),
        content=str(getattr(edge, "fact", "") or ""),
        score=score,
        source=source,
        metadata={
            **attributes,
            "entity_type": "claim",
            "relationship": _string_value(getattr(edge, "name", None)),
            "source_id": source,
            "source_node_uuid": _string_value(getattr(edge, "source_node_uuid", None)),
            "target_node_uuid": _string_value(getattr(edge, "target_node_uuid", None)),
            "source_node_project_id": source_project_id,
            "target_node_project_id": target_project_id,
            "retrieval_signals": [signal.value],
        },
        project_id=project_id,
        created_at=_datetime_value(getattr(edge, "created_at", None)),
        policy_reason=policy_reason,
        visibility=visibility,
        kind=CandidateKind.EDGE,
        retrieval_signals=(signal.value,),
        scope=CandidateScope(
            organization_id=_string_value(getattr(edge, "group_id", None)),
            project_id=project_id,
            visibility=visibility,
            policy_reason=policy_reason,
        ),
    )


def _attributes(value: Any) -> dict[str, Any]:
    raw = getattr(value, "attributes", None)
    return dict(raw) if isinstance(raw, Mapping) else {}


def _entity_type_for_node(node: Any, attributes: Mapping[str, Any]) -> str:
    for value in (
        attributes.get("entity_type"),
        getattr(node, "entity_type", None),
        *_labels_without_entity(node),
    ):
        if text := _string_value(value):
            return text.lower()
    return "artifact"


def _labels_without_entity(node: Any) -> list[str]:
    labels = getattr(node, "labels", None)
    if not isinstance(labels, list | tuple):
        return []
    return [str(label) for label in labels if str(label).lower() != "entity"]


def _content_for_node(node: Any, attributes: Mapping[str, Any]) -> str:
    # Mirrors _content_for_record: full content outranks the truncated
    # description/summary copies regardless of which carrier holds it.
    for value in (
        getattr(node, "content", None),
        attributes.get("content"),
        attributes.get("description"),
        getattr(node, "description", None),
        getattr(node, "summary", None),
    ):
        if text := _string_value(value):
            return text
    return ""


def _node_score(value: Any) -> float:
    raw = getattr(value, "score", 1.0)
    if isinstance(raw, int | float):
        return float(raw)
    return 1.0


def _string_value(value: object) -> str | None:
    if value is None or value == "":
        return None
    return str(value)


def _datetime_value(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


def _candidate_allowed(
    candidate: RetrievalCandidate,
    *,
    plan: RetrievalPlan,
    requested_types: set[str],
    facet: ContextFacet | None,
) -> bool:
    if requested_types and not _candidate_matches_types(candidate, requested_types, facet):
        return False
    if not _candidate_scope_allowed(candidate, plan):
        return False
    if _explicit_project_denied(plan) and candidate.type != "raw_memory":
        return False
    if candidate.type == "episode" and (plan.project or plan.accessible_projects is not None):
        return False
    if plan.project and candidate.project_id and candidate.project_id != plan.project:
        return False
    if candidate.type == "claim" and plan.accessible_projects is not None:
        endpoint_project_ids = {
            _string_value(candidate.metadata.get("source_node_project_id")),
            _string_value(candidate.metadata.get("target_node_project_id")),
        }
        endpoint_project_ids.discard(None)
        if endpoint_project_ids and not endpoint_project_ids.issubset(plan.accessible_projects):
            return False
    return not (
        plan.accessible_projects is not None
        and candidate.project_id is not None
        and candidate.project_id not in plan.accessible_projects
    )


def _candidate_scope_allowed(candidate: RetrievalCandidate, plan: RetrievalPlan) -> bool:
    metadata = candidate.metadata if isinstance(candidate.metadata, Mapping) else {}
    return memory_metadata_read_allowed(
        metadata,
        principal_id=plan.scopes[0].principal_id if plan.scopes else None,
        project_id=plan.project,
        accessible_projects=plan.accessible_projects,
        agent_id=next((scope.agent_id for scope in plan.scopes if scope.agent_id), None),
        # A private candidate is only authorized if a private scope survived
        # plan filtering; otherwise an API key without a private memory grant
        # could still read the principal's own private graph rows.
        private_scope_granted=any(
            scope.memory_scope is MemoryScope.PRIVATE for scope in plan.scopes
        ),
    )


def _candidate_matches_types(
    candidate: RetrievalCandidate,
    requested_types: set[str],
    facet: ContextFacet | None,
) -> bool:
    if candidate.type in requested_types:
        return True
    if candidate.type == "claim" and "relationship" in requested_types:
        return bool(candidate.metadata.get("relationship"))
    return candidate.type == "raw_memory" and facet is ContextFacet.RECENT_MEMORY


def _fuse_candidates(
    source_lists: Sequence[tuple[RetrievalSignal, Sequence[RetrievalCandidate]]],
    *,
    plan: RetrievalPlan,
    limit: int,
    temporal_target: datetime | None = None,
) -> list[tuple[RetrievalCandidate, float, dict[str, Any]]]:
    return _rank_fused_candidates(
        source_lists,
        plan=plan,
        limit=limit,
        rrf_scores=_python_rrf_scores(source_lists, rrf_k=plan.weights.rrf_k),
        temporal_target=temporal_target,
    )


async def _fuse_candidates_for_plan(
    *,
    client: Any,
    source_lists: Sequence[tuple[RetrievalSignal, Sequence[RetrievalCandidate]]],
    plan: RetrievalPlan,
    limit: int,
    temporal_target: datetime | None = None,
    fusion_backend: FusionBackend | None = None,
    fusion_failures: list[CandidateSourceFailure] | None = None,
) -> FusionExecutionResult:
    backend = fusion_backend or DEFAULT_FUSION_BACKEND
    if backend is FusionBackend.SURREAL_RRF:
        scores: dict[str, float] = {}
        try:
            scores = await _surreal_rrf_scores(client, source_lists, plan=plan, limit=limit)
        except Exception as exc:
            log.warning(
                "surreal_rrf_failed",
                organization_id=plan.organization_id,
                error_type=type(exc).__name__,
            )
            if fusion_failures is not None:
                fusion_failures.append(
                    CandidateSourceFailure(
                        source=backend.value,
                        error_type=type(exc).__name__,
                    )
                )
        if scores:
            return FusionExecutionResult(
                candidates=_rank_fused_candidates(
                    source_lists,
                    plan=plan,
                    limit=limit,
                    rrf_scores=scores,
                    backend=backend,
                    temporal_target=temporal_target,
                ),
                actual_backend=backend,
            )
        if any(candidates for _signal, candidates in source_lists):
            return FusionExecutionResult(
                candidates=_fuse_candidates(
                    source_lists,
                    plan=plan,
                    limit=limit,
                    temporal_target=temporal_target,
                ),
                actual_backend=FusionBackend.PYTHON_RRF,
            )
        return FusionExecutionResult(candidates=[], actual_backend=backend)
    return FusionExecutionResult(
        candidates=_fuse_candidates(
            source_lists,
            plan=plan,
            limit=limit,
            temporal_target=temporal_target,
        ),
        actual_backend=FusionBackend.PYTHON_RRF,
    )


def _python_rrf_scores(
    source_lists: Sequence[tuple[RetrievalSignal, Sequence[RetrievalCandidate]]],
    *,
    rrf_k: int,
) -> dict[str, float]:
    ranked_lists = [
        [(candidate, candidate.score) for candidate in candidates]
        for _signal, candidates in source_lists
    ]
    merged = rrf_merge(ranked_lists, k=float(rrf_k), dedup_key=lambda candidate: candidate.id)
    return {candidate.id: score for candidate, score in merged}


async def _surreal_rrf_scores(
    client: Any,
    source_lists: Sequence[tuple[RetrievalSignal, Sequence[RetrievalCandidate]]],
    *,
    plan: RetrievalPlan,
    limit: int,
) -> dict[str, float]:
    rrf_inputs = [
        [
            {
                "id": candidate.id,
                "source_signal": signal.value,
                "score": candidate.score,
            }
            for candidate in candidates
        ]
        for signal, candidates in source_lists
    ]
    if not any(rrf_inputs):
        return {}
    unique_candidate_count = len(
        {candidate.id for _signal, candidates in source_lists for candidate in candidates}
    )
    rows = await _execute_query_records(
        client,
        "RETURN search::rrf($lists, $limit, $k);",
        lists=rrf_inputs,
        limit=max(int(limit), unique_candidate_count, 1),
        k=plan.weights.rrf_k,
    )

    scores: dict[str, float] = {}
    for row in rows:
        candidate_id = _string_value(row.get("id") or row.get("uuid") or row.get("record_id"))
        if not candidate_id:
            continue
        raw_score = row.get("rrf_score", row.get("rff_score", row.get("fuse_score")))
        if isinstance(raw_score, int | float):
            scores[candidate_id] = float(raw_score)
    return scores


def _rank_fused_candidates(
    source_lists: Sequence[tuple[RetrievalSignal, Sequence[RetrievalCandidate]]],
    *,
    plan: RetrievalPlan,
    limit: int,
    rrf_scores: Mapping[str, float],
    backend: FusionBackend = FusionBackend.PYTHON_RRF,
    temporal_target: datetime | None = None,
) -> list[tuple[RetrievalCandidate, float, dict[str, Any]]]:
    score_by_id: dict[str, float] = defaultdict(float)
    candidates_by_id: dict[str, RetrievalCandidate] = {}
    metadata_by_id: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"sources": [], "ranks": {}, "original_scores": {}, "fusion_backend": backend.value}
    )

    for signal, candidates in source_lists:
        for rank, candidate in enumerate(candidates, start=1):
            score_by_id[candidate.id] = float(rrf_scores.get(candidate.id, 0.0))
            candidates_by_id.setdefault(candidate.id, candidate)
            fusion_metadata = metadata_by_id[candidate.id]
            fusion_metadata["sources"].append(signal.value)
            fusion_metadata["ranks"][signal.value] = rank
            fusion_metadata["original_scores"][signal.value] = candidate.score
            if signal is RetrievalSignal.GRAPH_EXPANSION:
                _merge_graph_expansion_metadata(fusion_metadata, candidate)
            if signal is RetrievalSignal.EXACT_KEY:
                # Merged here, from the lane's own candidate instance, because
                # candidates_by_id keeps whichever lane saw the row first: a row
                # found by both full-text and the key would otherwise report an
                # empty match list off the full-text instance.
                _merge_exact_key_metadata(fusion_metadata, candidate)

    ranked: list[tuple[RetrievalCandidate, float, dict[str, Any]]] = []
    for candidate_id, score in score_by_id.items():
        candidate = candidates_by_id[candidate_id]
        fusion_metadata = metadata_by_id[candidate_id]
        demote_multiplier = _vector_only_demote_multiplier(
            plan,
            signals=fusion_metadata["sources"],
        )
        if demote_multiplier < 1.0:
            score *= demote_multiplier
            fusion_metadata["vector_only_demoted"] = True
            fusion_metadata["filter_selectivity"] = plan.filter_selectivity
            fusion_metadata["vector_only_demote_multiplier"] = demote_multiplier
        graph_multiplier = _graph_expansion_only_multiplier(
            plan,
            signals=fusion_metadata["sources"],
        )
        if graph_multiplier < 1.0:
            score *= graph_multiplier
            fusion_metadata["graph_expansion_only_demoted"] = True
            fusion_metadata["graph_expansion_only_multiplier"] = graph_multiplier
        graph_signal_multiplier = _graph_native_signal_multiplier(
            plan,
            signals=fusion_metadata["sources"],
            fusion_metadata=fusion_metadata,
        )
        if graph_signal_multiplier > 1.0:
            score *= graph_signal_multiplier
            fusion_metadata["graph_native_signal_boost"] = graph_signal_multiplier
        exact_key_multiplier = _exact_key_multiplier(plan, signals=fusion_metadata["sources"])
        if exact_key_multiplier > 1.0:
            score *= exact_key_multiplier
            fusion_metadata["exact_key_boost"] = exact_key_multiplier
        boosted, temporal_multiplier = _boost_score(
            candidate,
            score,
            plan=plan,
            temporal_target=temporal_target,
        )
        if temporal_multiplier != 1.0:
            fusion_metadata["temporal_decay_multiplier"] = temporal_multiplier
        ranked.append((candidate, boosted, fusion_metadata))
    ranked.sort(key=lambda item: item[1], reverse=True)
    return ranked[:limit]


def _merge_graph_expansion_metadata(
    fusion_metadata: dict[str, Any],
    candidate: RetrievalCandidate,
) -> None:
    metadata = candidate.metadata if isinstance(candidate.metadata, Mapping) else {}
    for key in _GRAPH_EXPANSION_METADATA_KEYS:
        value = metadata.get(key)
        if value is not None:
            fusion_metadata[key] = value


def _merge_exact_key_metadata(
    fusion_metadata: dict[str, Any],
    candidate: RetrievalCandidate,
) -> None:
    metadata = candidate.metadata if isinstance(candidate.metadata, Mapping) else {}
    matched = metadata.get("matched_retrieval_keys")
    if isinstance(matched, list | tuple):
        fusion_metadata["matched_retrieval_keys"] = [str(key) for key in matched]


def _candidate_query_text(
    candidate: RetrievalCandidate,
    *,
    matched_keys: Sequence[str] = (),
) -> str:
    # Only the keys this query actually matched, never the row's whole declared
    # list. The keys are here so the coverage re-rank cannot bury an exact-key
    # hit whose body never spells the token out, which needs the matched key and
    # nothing more. Folding in every declared key would move scores on queries
    # the arm never fired for, and would hand a writer an ungated ranking lever:
    # sixteen keys of prose keywords buying a permanent coverage lift.
    parts = [part for part in (candidate.name, candidate.content, *matched_keys) if part]
    return " ".join(parts).lower()


def _apply_query_coverage_to_fused(
    query: str,
    fused: list[tuple[RetrievalCandidate, float, dict[str, Any]]],
    *,
    temporal_target: datetime | None,
) -> list[tuple[RetrievalCandidate, float, dict[str, Any]]]:
    """Re-rank fused context candidates through the shared query-coverage core.

    The native plan already produces a strong base order from RRF plus the
    in-DB-shaped demotions and boosts; that order is the prior the shared
    ranker refines, so context packs rank by the same scorer ``/api/search``
    uses while keeping the efficient DB-native candidate fetch upstream.
    """
    metadata_by_id = {
        id(candidate): fusion_metadata for candidate, _score, fusion_metadata in fused
    }
    # Fusion holds the matched keys, not the candidate, because a row reached by
    # two lanes keeps whichever instance arrived first. The coverage text is
    # therefore resolved through the fused metadata rather than off the row.
    matched_keys_by_id = {
        id(candidate): tuple(
            str(key) for key in (fusion_metadata.get("matched_retrieval_keys") or ())
        )
        for candidate, _score, fusion_metadata in fused
    }
    reranked, _applied, _refined = rank_items_by_query_coverage(
        query,
        [(candidate, score) for candidate, score, _fusion_metadata in fused],
        text_fn=lambda candidate: _candidate_query_text(
            candidate,
            matched_keys=matched_keys_by_id.get(id(candidate), ()),
        ),
        id_fn=lambda candidate: candidate.id,
        timestamp_fn=lambda candidate: get_entity_timestamp(candidate) or candidate.created_at,
        temporal_target=temporal_target,
    )
    return [(candidate, score, metadata_by_id[id(candidate)]) for candidate, score in reranked]


def _vector_only_demote_multiplier(
    plan: RetrievalPlan,
    *,
    signals: Sequence[str],
) -> float:
    if plan.filter_selectivity is None:
        return 1.0
    if plan.filter_selectivity >= plan.filter_selectivity_threshold:
        return 1.0
    if any(
        signal
        not in {
            RetrievalSignal.NODE_VECTOR.value,
            RetrievalSignal.EDGE_VECTOR.value,
        }
        for signal in signals
    ):
        return 1.0
    if plan.filter_selectivity_threshold <= 0:
        return 1.0
    return max(plan.filter_selectivity / plan.filter_selectivity_threshold, 0.1)


def _graph_expansion_only_multiplier(
    plan: RetrievalPlan,
    *,
    signals: Sequence[str],
) -> float:
    if set(signals) != {RetrievalSignal.GRAPH_EXPANSION.value}:
        return 1.0
    return max(min(plan.weights.graph_expansion_only_boost, 1.0), 0.0)


def _exact_key_multiplier(
    plan: RetrievalPlan,
    *,
    signals: Sequence[str],
) -> float:
    """Lift a candidate whose writer declared one of the query's exact keys.

    Rank-only RRF would score this candidate like the top of any other lane,
    which is the failure that keeps a weak lexical arm out of fusion: an equal
    vote for an unequal signal. The boost is what makes the arm high-precision
    rather than merely present, and it applies to the candidate, not the lane,
    so a row found by both the key and the vector index is lifted once.
    """

    if RetrievalSignal.EXACT_KEY.value not in signals:
        return 1.0
    return max(plan.weights.exact_key_boost, 1.0)


def _graph_native_signal_multiplier(
    plan: RetrievalPlan,
    *,
    signals: Sequence[str],
    fusion_metadata: Mapping[str, Any],
) -> float:
    if RetrievalSignal.GRAPH_EXPANSION.value not in signals:
        return 1.0
    if set(signals) == {RetrievalSignal.GRAPH_EXPANSION.value}:
        return 1.0
    cap = max(plan.weights.graph_native_signal_boost_cap, 1.0)
    raw_path_score = fusion_metadata.get("graph_expansion_score")
    if not isinstance(raw_path_score, int | float):
        raw_scores = fusion_metadata.get("original_scores")
        if isinstance(raw_scores, Mapping):
            raw_path_score = raw_scores.get(RetrievalSignal.GRAPH_EXPANSION.value)
    if not isinstance(raw_path_score, int | float):
        return 1.0
    path_score = max(min(float(raw_path_score), 1.0), 0.0)
    if path_score <= 0.0:
        return 1.0
    return min(1.0 + path_score * (cap - 1.0), cap)


def _boost_score(
    candidate: RetrievalCandidate,
    score: float,
    *,
    plan: RetrievalPlan,
    temporal_target: datetime | None,
) -> tuple[float, float]:
    boosted = score
    status = _string_value(candidate.metadata.get("status"))
    if candidate.type == "task" and status in _ACTIVE_TASK_STATUSES:
        boosted *= plan.weights.active_task_state_boost
    if plan.project and candidate.project_id == plan.project:
        boosted *= plan.weights.project_match_boost
    if candidate.type == "raw_memory":
        boosted *= plan.weights.direct_raw_source_boost
    boosted *= _freshness_boost(candidate.created_at, cap=plan.weights.freshness_boost_cap)
    temporal_multiplier = (
        1.0
        if temporal_target is not None
        else temporal_decay_multiplier(
            candidate,
            decay_days=core_config.temporal_decay_days,
        )
    )
    boosted *= temporal_multiplier
    return boosted, temporal_multiplier


def _freshness_boost(created_at: datetime | None, *, cap: float) -> float:
    if created_at is None:
        return 1.0
    now = datetime.now(UTC)
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=UTC)
    age_days = max((now - created_at).total_seconds() / 86_400, 0.0)
    return min(cap, 1.0 + (0.5 / (1.0 + age_days)))


def _search_result_from_candidate(
    candidate: RetrievalCandidate,
    *,
    score: float,
    fusion_metadata: Mapping[str, Any],
    include_content: bool,
) -> SearchResult:
    from sibyl_core.tools.responses import SearchResult

    freshness = _freshness_boost(candidate.created_at, cap=1.5)
    metadata = {
        **candidate.contract_metadata(),
        "source_id": candidate.source or candidate.id,
        "visibility": candidate.visibility,
        "freshness": round(freshness, 4),
        "retrieval_signals": list(fusion_metadata.get("sources", [])),
        "retrieval_ranks": dict(fusion_metadata.get("ranks", {})),
        "retrieval_scores": dict(fusion_metadata.get("original_scores", {})),
        "policy_reason": candidate.policy_reason,
    }
    if fusion_metadata.get("vector_only_demoted"):
        metadata["vector_only_demoted"] = True
        metadata["filter_selectivity"] = fusion_metadata.get("filter_selectivity")
        metadata["vector_only_demote_multiplier"] = fusion_metadata.get(
            "vector_only_demote_multiplier"
        )
    if fusion_metadata.get("graph_expansion_only_demoted"):
        metadata["graph_expansion_only_demoted"] = True
        metadata["graph_expansion_only_multiplier"] = fusion_metadata.get(
            "graph_expansion_only_multiplier"
        )
    if fusion_metadata.get("graph_native_signal_boost"):
        metadata["graph_native_signal_boost"] = fusion_metadata.get("graph_native_signal_boost")
    if fusion_metadata.get("exact_key_boost"):
        metadata["exact_key_boost"] = fusion_metadata.get("exact_key_boost")
        metadata["matched_retrieval_keys"] = list(
            fusion_metadata.get("matched_retrieval_keys") or ()
        )
    if fusion_metadata.get("temporal_decay_multiplier") is not None:
        metadata["temporal_decay_multiplier"] = round(
            float(fusion_metadata["temporal_decay_multiplier"]),
            4,
        )
    for key in _GRAPH_EXPANSION_METADATA_KEYS:
        if key in fusion_metadata:
            metadata[key] = fusion_metadata[key]
    if candidate.project_id:
        metadata["project_id"] = candidate.project_id
    if candidate.created_at:
        metadata["created_at"] = candidate.created_at.isoformat()
    return SearchResult(
        id=candidate.id,
        type=candidate.type,
        name=candidate.name,
        content=candidate.content if include_content else "",
        score=score,
        source=candidate.source,
        result_origin="graph",
        metadata=metadata,
    )
