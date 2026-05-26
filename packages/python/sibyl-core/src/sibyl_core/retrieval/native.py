"""Native SurrealDB retrieval planning contracts."""

from __future__ import annotations

import asyncio
import os
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
    memory_scope_policy_key,
)
from sibyl_core.backends.surreal.fulltext import build_fulltext_query
from sibyl_core.config import core_config
from sibyl_core.embeddings.native import NativeEmbeddingMetadata, NativeEmbeddingProvider
from sibyl_core.models.context import ContextFacet
from sibyl_core.services.native_graph import get_native_graph_runtime, normalize_records
from sibyl_core.services.surreal_content import (
    MemoryScope,
    RawMemory,
    raw_memory_recallable,
    recall_raw_memory,
)

if TYPE_CHECKING:
    from sibyl_core.tools.responses import SearchResponse, SearchResult

type RawMemoryRecallFn = Callable[..., Awaitable[list[RawMemory]]]

DEFAULT_FILTER_SELECTIVITY_THRESHOLD = 0.1
EDGE_FULLTEXT_MATCH_HEADROOM = 8
EDGE_FULLTEXT_MIN_MATCH_LIMIT = 32
_ACTIVE_TASK_STATUSES = {"doing", "in_progress", "review"}
_RAW_MEMORY_CONTEXT_TYPES = {"raw_memory", "session", "episode", "note"}
log = structlog.get_logger()


async def _get_read_only_graph_runtime(organization_id: str) -> Any:
    try:
        return await get_native_graph_runtime(organization_id, ensure_schema=False)
    except TypeError as error:
        if "unexpected keyword argument 'ensure_schema'" not in str(error):
            raise
        return await get_native_graph_runtime(organization_id)


class NativeRetrievalMode(StrEnum):
    NATIVE = "native"
    COMPARE = "compare"


DEFAULT_NATIVE_RETRIEVAL_MODE = NativeRetrievalMode.NATIVE


class NativeFusionBackend(StrEnum):
    PYTHON_RRF = "python_rrf"
    SURREAL_RRF = "surreal_rrf"


DEFAULT_NATIVE_FUSION_BACKEND = NativeFusionBackend.PYTHON_RRF


class NativeRetrievalSignal(StrEnum):
    RAW_LEXICAL = "raw_lexical"
    NODE_FULLTEXT = "node_fulltext"
    EPISODE_FULLTEXT = "episode_fulltext"
    EDGE_FULLTEXT = "edge_fulltext"
    NODE_VECTOR = "node_vector"
    EDGE_VECTOR = "edge_vector"
    GRAPH_EXPANSION = "graph_expansion"


@dataclass(frozen=True, slots=True)
class NativeRetrievalWeights:
    rrf_k: int = 60
    active_task_state_boost: float = 1.3
    project_match_boost: float = 1.2
    direct_raw_source_boost: float = 1.4
    graph_expansion_only_boost: float = 0.45
    freshness_boost_cap: float = 1.5


@dataclass(frozen=True, slots=True)
class NativeCandidateLimits:
    raw_lexical: int = 4
    node_fulltext: int = 8
    episode_fulltext: int = 8
    edge_fulltext: int = 8
    node_vector: int = 8
    edge_vector: int = 8
    graph_expansion: int = 8


@dataclass(frozen=True, slots=True)
class NativeScopeSpec:
    memory_scope: MemoryScope
    scope_key: str | None
    policy_reason: str
    principal_id: str
    project_id: str | None = None
    agent_id: str | None = None


@dataclass(frozen=True, slots=True)
class NativeRetrievalPlan:
    query: str
    organization_id: str
    facets: tuple[ContextFacet, ...]
    facet_types: Mapping[ContextFacet, tuple[str, ...]]
    scopes: tuple[NativeScopeSpec, ...]
    denied_scopes: tuple[MemoryPolicyDecision, ...]
    candidate_limits: NativeCandidateLimits = field(default_factory=NativeCandidateLimits)
    weights: NativeRetrievalWeights = field(default_factory=NativeRetrievalWeights)
    signals: tuple[NativeRetrievalSignal, ...] = (
        NativeRetrievalSignal.RAW_LEXICAL,
        NativeRetrievalSignal.NODE_FULLTEXT,
        NativeRetrievalSignal.EPISODE_FULLTEXT,
        NativeRetrievalSignal.EDGE_FULLTEXT,
        NativeRetrievalSignal.NODE_VECTOR,
        NativeRetrievalSignal.EDGE_VECTOR,
        NativeRetrievalSignal.GRAPH_EXPANSION,
    )
    project: str | None = None
    accessible_projects: frozenset[str] | None = None
    graph_expansion_depth: int = 1
    vector_min_score: float = 0.0
    filter_selectivity: float | None = None
    filter_selectivity_threshold: float = DEFAULT_FILTER_SELECTIVITY_THRESHOLD


@dataclass(frozen=True, slots=True)
class NativeSearchFilter:
    node_labels: tuple[str, ...] = ()
    project_ids: tuple[str, ...] = ()
    edge_uuids: tuple[str, ...] = ()
    edge_types: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class NativeRetrievalCandidate:
    id: str
    type: str
    name: str
    content: str
    score: float
    source: str | None
    metadata: Mapping[str, Any]
    result_origin: str = "graph"
    project_id: str | None = None
    created_at: datetime | None = None
    policy_reason: str | None = None
    visibility: str | None = None


def coerce_native_retrieval_mode(value: str | NativeRetrievalMode | None) -> NativeRetrievalMode:
    if isinstance(value, NativeRetrievalMode):
        return value
    if value is None or not value.strip():
        return DEFAULT_NATIVE_RETRIEVAL_MODE
    try:
        return NativeRetrievalMode(value.strip().lower())
    except ValueError:
        return DEFAULT_NATIVE_RETRIEVAL_MODE


def native_retrieval_mode_from_env(
    environ: Mapping[str, str] | None = None,
) -> NativeRetrievalMode:
    source = os.environ if environ is None else environ
    return coerce_native_retrieval_mode(source.get("SIBYL_RETRIEVAL_MODE"))


def coerce_native_fusion_backend(
    value: str | NativeFusionBackend | None,
) -> NativeFusionBackend:
    if isinstance(value, NativeFusionBackend):
        return value
    if value is None or not value.strip():
        return DEFAULT_NATIVE_FUSION_BACKEND
    try:
        return NativeFusionBackend(value.strip().lower())
    except ValueError:
        return DEFAULT_NATIVE_FUSION_BACKEND


def native_fusion_backend_from_env(
    environ: Mapping[str, str] | None = None,
) -> NativeFusionBackend:
    source = os.environ if environ is None else environ
    return coerce_native_fusion_backend(source.get("SIBYL_NATIVE_FUSION_BACKEND"))


def build_native_context_retrieval_plan(
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
) -> NativeRetrievalPlan:
    scopes: list[NativeScopeSpec] = []
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
            NativeScopeSpec(
                memory_scope=decision.memory_scope,
                scope_key=decision.scope_key,
                policy_reason=decision.reason,
                principal_id=principal_id,
                project_id=project_id,
                agent_id=scoped_agent_id,
            )
        )

    per_signal_limit = max(2, min(8, limit))
    facet_types_by_facet = {facet: tuple(facet_types.get(facet, ())) for facet in facets}
    return NativeRetrievalPlan(
        query=query,
        organization_id=organization_id,
        facets=tuple(facets),
        facet_types=facet_types_by_facet,
        scopes=tuple(scopes),
        denied_scopes=tuple(denied_scopes),
        candidate_limits=NativeCandidateLimits(
            raw_lexical=max(1, min(8, limit // 4 or 1)),
            node_fulltext=per_signal_limit,
            episode_fulltext=per_signal_limit,
            edge_fulltext=per_signal_limit,
            node_vector=per_signal_limit,
            edge_vector=per_signal_limit,
            graph_expansion=per_signal_limit,
        ),
        project=project,
        accessible_projects=scoped_accessible_projects,
        filter_selectivity=_project_filter_selectivity(project, scoped_accessible_projects),
    )


async def native_context_search(
    *,
    plan: NativeRetrievalPlan,
    types: Sequence[str] | None = None,
    facet: ContextFacet | None = None,
    limit: int = 10,
    include_content: bool = True,
    embedding_provider: NativeEmbeddingProvider | None = None,
    raw_memory_recall_fn: RawMemoryRecallFn = recall_raw_memory,
) -> SearchResponse:
    """Search context-pack candidates through native SurrealDB paths."""

    from sibyl_core.tools.responses import SearchResponse

    limit = max(1, min(limit, 50))
    runtime = await _get_read_only_graph_runtime(plan.organization_id)
    client = runtime.client
    search_filter = _search_filter_for_plan(plan)
    requested_types = {value.lower() for value in types or ()}

    raw_task = _recall_raw_candidates(
        plan=plan,
        facet=facet,
        requested_types=requested_types,
        limit=plan.candidate_limits.raw_lexical,
        recall_fn=raw_memory_recall_fn,
    )
    graph_tasks = [
        _node_fulltext_candidates(
            client=client,
            plan=plan,
            search_filter=search_filter,
            limit=plan.candidate_limits.node_fulltext,
        ),
        _episode_fulltext_candidates(
            client=client,
            plan=plan,
            search_filter=search_filter,
            limit=plan.candidate_limits.episode_fulltext,
        ),
        _edge_fulltext_candidates(
            client=client,
            plan=plan,
            search_filter=search_filter,
            limit=plan.candidate_limits.edge_fulltext,
        ),
    ]
    raw_candidates, graph_candidate_lists = await _gather_candidate_sources(raw_task, graph_tasks)

    vector_candidate_lists = await _vector_candidate_sources(
        client=client,
        plan=plan,
        search_filter=search_filter,
        embedding_provider=embedding_provider,
    )
    graph_expansion_candidates = await _graph_expansion_candidates(
        client=client,
        plan=plan,
        search_filter=search_filter,
        seed_candidates=[
            candidate
            for source in [*graph_candidate_lists, *vector_candidate_lists]
            for candidate in source
        ],
        limit=plan.candidate_limits.graph_expansion,
    )

    source_lists = [
        (NativeRetrievalSignal.RAW_LEXICAL, raw_candidates),
        (NativeRetrievalSignal.NODE_FULLTEXT, graph_candidate_lists[0]),
        (NativeRetrievalSignal.EPISODE_FULLTEXT, graph_candidate_lists[1]),
        (NativeRetrievalSignal.EDGE_FULLTEXT, graph_candidate_lists[2]),
        (NativeRetrievalSignal.NODE_VECTOR, vector_candidate_lists[0]),
        (NativeRetrievalSignal.EDGE_VECTOR, vector_candidate_lists[1]),
        (NativeRetrievalSignal.GRAPH_EXPANSION, graph_expansion_candidates),
    ]
    filtered_lists = [
        (
            signal,
            [
                candidate
                for candidate in candidates
                if _candidate_allowed(
                    candidate,
                    plan=plan,
                    requested_types=requested_types,
                    facet=facet,
                )
            ],
        )
        for signal, candidates in source_lists
    ]
    fusion_backend = native_fusion_backend_from_env()
    fused = await _fuse_candidates_for_plan(
        client=client,
        source_lists=filtered_lists,
        plan=plan,
        limit=limit,
        fusion_backend=fusion_backend,
    )
    results = [
        _search_result_from_candidate(
            candidate,
            score=score,
            fusion_metadata=fusion_metadata,
            include_content=include_content,
        )
        for candidate, score, fusion_metadata in fused
    ]
    return SearchResponse(
        results=results,
        total=len(results),
        query=plan.query,
        filters={
            "types": list(types) if types else None,
            "project": plan.project,
            "retrieval_mode": NativeRetrievalMode.NATIVE.value,
            "fusion_backend": fusion_backend.value,
        },
        graph_count=len([result for result in results if result.result_origin == "graph"]),
        document_count=0,
        limit=limit,
    )


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
    if agent_id:
        diary_projects = (project,) if project else tuple(accessible_projects or ())
        if diary_projects:
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
        else:
            decisions.append(
                (
                    authorize_memory_read(
                        principal_id=principal_id,
                        memory_scope=MemoryScope.PRIVATE,
                        project_id=None,
                        agent_id=agent_id,
                        accessible_projects=accessible_projects,
                    ),
                    None,
                    agent_id,
                )
            )
    return decisions


async def _gather_candidate_sources(
    raw_task: Any,
    graph_tasks: Sequence[Any],
) -> tuple[list[NativeRetrievalCandidate], list[list[NativeRetrievalCandidate]]]:
    gathered = await asyncio.gather(raw_task, *graph_tasks, return_exceptions=True)
    raw = _candidate_list_or_empty(gathered[0])
    graph = [_candidate_list_or_empty(result) for result in gathered[1:]]
    return raw, graph


def _candidate_list_or_empty(result: object) -> list[NativeRetrievalCandidate]:
    if isinstance(result, BaseException) or not isinstance(result, list):
        return []
    return cast("list[NativeRetrievalCandidate]", result)


def _search_filter_for_plan(plan: NativeRetrievalPlan) -> NativeSearchFilter:
    return NativeSearchFilter(project_ids=_authorized_project_ids(plan))


def _authorized_project_ids(plan: NativeRetrievalPlan) -> tuple[str, ...]:
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


def _graph_knn_effort() -> int:
    return max(1, int(core_config.graph_knn_ef))


def _explicit_project_denied(plan: NativeRetrievalPlan) -> bool:
    return bool(plan.project and not _authorized_project_ids(plan))


async def _recall_raw_candidates(
    *,
    plan: NativeRetrievalPlan,
    facet: ContextFacet | None,
    requested_types: set[str],
    limit: int,
    recall_fn: RawMemoryRecallFn,
) -> list[NativeRetrievalCandidate]:
    if facet is not None and facet is not ContextFacet.RECENT_MEMORY:
        return []
    if requested_types and requested_types.isdisjoint(_RAW_MEMORY_CONTEXT_TYPES):
        return []

    candidates: list[NativeRetrievalCandidate] = []
    seen_ids: set[str] = set()
    for scope in plan.scopes:
        if scope.memory_scope not in {
            MemoryScope.PRIVATE,
            MemoryScope.PROJECT,
            MemoryScope.DELEGATED,
        }:
            continue
        recalled = await recall_fn(
            organization_id=plan.organization_id,
            principal_id=scope.principal_id,
            query=plan.query,
            memory_scope=scope.memory_scope.value,
            scope_key=scope.scope_key,
            agent_id=scope.agent_id,
            project_id=scope.project_id,
            limit=limit,
        )
        for memory in recalled:
            if not raw_memory_recallable(memory):
                continue
            if memory.id in seen_ids:
                continue
            seen_ids.add(memory.id)
            candidates.append(_candidate_from_raw_memory(memory, scope))
    return sorted(candidates, key=lambda candidate: candidate.score, reverse=True)


async def _node_fulltext_candidates(
    *,
    client: Any,
    plan: NativeRetrievalPlan,
    search_filter: NativeSearchFilter,
    limit: int,
) -> list[NativeRetrievalCandidate]:
    search_query = build_fulltext_query(plan.query)
    if not search_query:
        return []
    filter_clauses, filter_params = _node_filter_clause(search_filter)
    rows = normalize_records(
        await client.execute_query(
            """
            SELECT *,
                   math::max([
                       search::score(0),
                       search::score(1),
                       search::score(2),
                       search::score(3)
                   ]) AS score
            FROM entity
            WHERE """
            + _where_clause(["group_id = $group_id", *filter_clauses])
            + """
              AND (
                  name @0@ $search_query
                  OR summary @1@ $search_query
                  OR description @2@ $search_query
                  OR content @3@ $search_query
              )
            ORDER BY score DESC, created_at DESC, uuid DESC
            LIMIT $limit;
            """,
            group_id=plan.organization_id,
            search_query=search_query,
            limit=max(int(limit), 1),
            **filter_params,
        )
    )
    return [
        _candidate_from_node_record(
            row,
            signal=NativeRetrievalSignal.NODE_FULLTEXT,
            score=_record_score(row),
        )
        for row in rows
    ]


async def _episode_fulltext_candidates(
    *,
    client: Any,
    plan: NativeRetrievalPlan,
    search_filter: NativeSearchFilter,
    limit: int,
) -> list[NativeRetrievalCandidate]:
    if search_filter.project_ids:
        return []
    search_query = build_fulltext_query(plan.query)
    if not search_query:
        return []
    rows = normalize_records(
        await client.execute_query(
            """
            SELECT *, search::score(0) AS score
            FROM episode
            WHERE group_id = $group_id
              AND content @0@ $search_query
            ORDER BY score DESC, created_at DESC, uuid DESC
            LIMIT $limit;
            """,
            group_id=plan.organization_id,
            search_query=search_query,
            limit=max(int(limit), 1),
        )
    )
    return [
        _candidate_from_episode_record(
            row,
            signal=NativeRetrievalSignal.EPISODE_FULLTEXT,
            score=_record_score(row),
        )
        for row in rows
    ]


async def _edge_fulltext_candidates(
    *,
    client: Any,
    plan: NativeRetrievalPlan,
    search_filter: NativeSearchFilter,
    limit: int,
) -> list[NativeRetrievalCandidate]:
    search_query = build_fulltext_query(plan.query)
    if not search_query:
        return []
    result_limit = max(int(limit), 1)
    match_limit = max(result_limit * EDGE_FULLTEXT_MATCH_HEADROOM, EDGE_FULLTEXT_MIN_MATCH_LIMIT)
    match_clauses, match_params = _edge_match_filter_clause(search_filter)
    match_rows = normalize_records(
        await client.execute_query(
            """
            SELECT uuid, created_at, search::score(0) AS score
            FROM relates_to
            WHERE """
            + _where_clause(["group_id = $group_id", *match_clauses])
            + """
              AND fact @0@ $search_query
            ORDER BY score DESC, created_at DESC, uuid DESC
            LIMIT $match_limit;
            """,
            group_id=plan.organization_id,
            search_query=search_query,
            match_limit=match_limit,
            **match_params,
        )
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

    hydrate_filter = NativeSearchFilter(
        node_labels=search_filter.node_labels,
        project_ids=search_filter.project_ids,
        edge_types=search_filter.edge_types,
    )
    filter_clauses, filter_params = _edge_filter_clause(hydrate_filter)
    rows = normalize_records(
        await client.execute_query(
            _edge_select()
            + " WHERE "
            + _where_clause(["uuid IN $match_uuids", "group_id = $group_id", *filter_clauses])
            + " LIMIT $limit;",
            match_uuids=match_uuids,
            group_id=plan.organization_id,
            limit=len(match_uuids),
            **filter_params,
        )
    )
    rows_by_uuid = {str(row["uuid"]): row for row in rows if row.get("uuid")}
    candidates = [
        _candidate_from_edge_record(
            rows_by_uuid[uuid],
            signal=NativeRetrievalSignal.EDGE_FULLTEXT,
            score=match_scores[uuid],
        )
        for uuid in match_uuids
        if uuid in rows_by_uuid
    ]
    return candidates[:result_limit]


async def _vector_candidate_sources(
    *,
    client: Any,
    plan: NativeRetrievalPlan,
    search_filter: NativeSearchFilter,
    embedding_provider: NativeEmbeddingProvider | None,
) -> list[list[NativeRetrievalCandidate]]:
    if embedding_provider is None:
        return [
            [],
            [],
        ]
    if (
        NativeRetrievalSignal.NODE_VECTOR not in plan.signals
        and NativeRetrievalSignal.EDGE_VECTOR not in plan.signals
    ):
        return [
            [],
            [],
        ]
    try:
        embeddings = await embedding_provider.embed_texts([plan.query], input_kind="query")
    except Exception as exc:
        log.warning(
            "native_vector_embedding_failed",
            organization_id=plan.organization_id,
            query_length=len(plan.query),
            error_type=type(exc).__name__,
        )
        return [
            [],
            [],
        ]
    try:
        query_embedding = _query_embedding_from_batch(
            embeddings,
            dimensions=embedding_provider.metadata.dimensions,
        )
    except ValueError as exc:
        log.warning(
            "native_vector_embedding_invalid",
            organization_id=plan.organization_id,
            error=str(exc),
        )
        return [
            [],
            [],
        ]
    node_candidates: list[NativeRetrievalCandidate] = []
    edge_candidates: list[NativeRetrievalCandidate] = []
    tasks: list[Awaitable[list[NativeRetrievalCandidate]]] = []
    task_signals: list[NativeRetrievalSignal] = []
    if NativeRetrievalSignal.NODE_VECTOR in plan.signals:
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
        task_signals.append(NativeRetrievalSignal.NODE_VECTOR)
    if NativeRetrievalSignal.EDGE_VECTOR in plan.signals:
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
        task_signals.append(NativeRetrievalSignal.EDGE_VECTOR)
    gathered = await asyncio.gather(*tasks, return_exceptions=True)
    for signal, result in zip(task_signals, gathered, strict=True):
        if isinstance(result, BaseException):
            log.warning(
                "native_vector_query_failed",
                organization_id=plan.organization_id,
                signal=signal.value,
                error_type=type(result).__name__,
            )
            continue
        if signal is NativeRetrievalSignal.NODE_VECTOR:
            node_candidates = _candidate_list_or_empty(result)
        else:
            edge_candidates = _candidate_list_or_empty(result)
    return [node_candidates, edge_candidates]


async def _node_vector_candidates(
    *,
    client: Any,
    plan: NativeRetrievalPlan,
    search_filter: NativeSearchFilter,
    query_embedding: Sequence[float],
    embedding_metadata: NativeEmbeddingMetadata,
    limit: int,
) -> list[NativeRetrievalCandidate]:
    if limit <= 0:
        return []
    filter_clauses, filter_params = _node_filter_clause(search_filter)
    candidate_limit = max(int(limit), 1)
    knn_effort = _graph_knn_effort()
    rows = normalize_records(
        await client.execute_query(
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
    )
    return [
        _candidate_from_node_record(
            row,
            signal=NativeRetrievalSignal.NODE_VECTOR,
            score=_record_score(row),
            embedding_metadata=embedding_metadata,
        )
        for row in rows
    ]


async def _edge_vector_candidates(
    *,
    client: Any,
    plan: NativeRetrievalPlan,
    search_filter: NativeSearchFilter,
    query_embedding: Sequence[float],
    embedding_metadata: NativeEmbeddingMetadata,
    limit: int,
) -> list[NativeRetrievalCandidate]:
    if limit <= 0:
        return []
    filter_clauses, filter_params = _edge_filter_clause(search_filter)
    candidate_limit = max(int(limit), 1)
    knn_effort = _graph_knn_effort()
    rows = normalize_records(
        await client.execute_query(
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
    )
    return [
        _candidate_from_edge_record(
            row,
            signal=NativeRetrievalSignal.EDGE_VECTOR,
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
    plan: NativeRetrievalPlan,
    search_filter: NativeSearchFilter,
    seed_candidates: Sequence[NativeRetrievalCandidate],
    limit: int,
) -> list[NativeRetrievalCandidate]:
    seed_uuids = [
        candidate.id
        for candidate in seed_candidates
        if candidate.type not in {"claim", "relationship", "raw_memory"}
    ][:limit]
    if not seed_uuids:
        return []

    rows = await _node_bfs_records(
        client=client,
        origin_uuids=seed_uuids,
        search_filter=search_filter,
        group_id=plan.organization_id,
        max_depth=plan.graph_expansion_depth,
        limit=limit,
    )
    return [
        _candidate_from_node_record(
            row,
            signal=NativeRetrievalSignal.GRAPH_EXPANSION,
            score=1.0,
        )
        for row in rows
    ]


def _where_clause(clauses: Sequence[str]) -> str:
    active = [clause for clause in clauses if clause]
    return " AND ".join(active) if active else "true"


def _node_filter_clause(search_filter: NativeSearchFilter) -> tuple[list[str], dict[str, Any]]:
    clauses: list[str] = []
    params: dict[str, Any] = {}
    if search_filter.node_labels:
        clauses.append("labels CONTAINS $node_label")
        params["node_label"] = search_filter.node_labels[0]
    if search_filter.project_ids:
        clauses.append("(project_id IN $project_ids OR attributes.project_id IN $project_ids)")
        params["project_ids"] = list(search_filter.project_ids)
    return clauses, params


def _edge_filter_clause(
    search_filter: NativeSearchFilter,
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
        clauses.append("in.uuid = $source_node_uuid")
        params["source_node_uuid"] = source_node_uuid
    if target_node_uuid is not None:
        clauses.append("out.uuid = $target_node_uuid")
        params["target_node_uuid"] = target_node_uuid
    return clauses, params


def _edge_match_filter_clause(
    search_filter: NativeSearchFilter,
) -> tuple[list[str], dict[str, Any]]:
    return _edge_filter_clause(search_filter)


def _edge_select(extra: str | None = None) -> str:
    extra_select = f", {extra}" if extra else ""
    return f"""
        SELECT uuid, name, fact, fact_embedding, group_id, episodes, attributes,
               created_at, expired_at, valid_at, invalid_at,
               in.uuid AS source_node_uuid,
               out.uuid AS target_node_uuid,
               in.project_id AS source_node_project_id,
               out.project_id AS target_node_project_id{extra_select}
        FROM relates_to
    """


async def _node_bfs_records(
    *,
    client: Any,
    origin_uuids: Sequence[str],
    search_filter: NativeSearchFilter,
    group_id: str,
    max_depth: int,
    limit: int,
) -> list[dict[str, object]]:
    if not origin_uuids or max_depth < 1:
        return []

    discovered: list[str] = []
    seen_discovered: set[str] = set()
    visited_entities = set(origin_uuids)
    entity_frontier = _dedupe_strings(origin_uuids)
    episode_frontier = _dedupe_strings(origin_uuids)

    for depth in range(1, max_depth + 1):
        next_entities: list[str] = []
        if depth == 1:
            next_entities.extend(
                await _mentioned_entity_uuids(
                    client=client,
                    episode_uuids=episode_frontier,
                    group_id=group_id,
                )
            )
        next_entities.extend(
            await _relation_target_uuids(
                client=client,
                source_uuids=entity_frontier,
                group_id=group_id,
            )
        )

        for uuid in _dedupe_strings(next_entities):
            if uuid in seen_discovered:
                continue
            seen_discovered.add(uuid)
            discovered.append(uuid)
            if len(discovered) >= limit:
                return await _hydrate_entity_records(
                    client=client,
                    uuids=discovered,
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

    return await _hydrate_entity_records(
        client=client,
        uuids=discovered,
        search_filter=search_filter,
        group_id=group_id,
        limit=limit,
    )


async def _mentioned_entity_uuids(
    *,
    client: Any,
    episode_uuids: Sequence[str],
    group_id: str,
) -> list[str]:
    if not episode_uuids:
        return []
    rows = normalize_records(
        await client.execute_query(
            """
            SELECT out.uuid AS uuid
            FROM mentions
            WHERE in.uuid IN $episode_uuids
              AND group_id = $group_id
              AND out.group_id = $group_id;
            """,
            episode_uuids=list(episode_uuids),
            group_id=group_id,
        )
    )
    return _dedupe_strings(_record_uuids(rows))


async def _relation_target_uuids(
    *,
    client: Any,
    source_uuids: Sequence[str],
    group_id: str,
) -> list[str]:
    if not source_uuids:
        return []
    rows = normalize_records(
        await client.execute_query(
            """
            SELECT out.uuid AS uuid
            FROM relates_to
            WHERE in.uuid IN $source_uuids
              AND group_id = $group_id
              AND out.group_id = $group_id;
            """,
            source_uuids=list(source_uuids),
            group_id=group_id,
        )
    )
    return _dedupe_strings(_record_uuids(rows))


async def _hydrate_entity_records(
    *,
    client: Any,
    uuids: Sequence[str],
    search_filter: NativeSearchFilter,
    group_id: str,
    limit: int,
) -> list[dict[str, object]]:
    if not uuids:
        return []
    filter_clauses, filter_params = _node_filter_clause(search_filter)
    rows = normalize_records(
        await client.execute_query(
            "SELECT * FROM entity WHERE "
            + _where_clause(["uuid IN $uuids", "group_id = $group_id", *filter_clauses])
            + " LIMIT $limit;",
            uuids=list(uuids),
            group_id=group_id,
            limit=max(int(limit), 1),
            **filter_params,
        )
    )
    rows_by_uuid = {str(row["uuid"]): row for row in rows if row.get("uuid")}
    return [rows_by_uuid[uuid] for uuid in uuids if uuid in rows_by_uuid]


def _record_uuids(rows: Sequence[Mapping[str, object]]) -> list[str]:
    return [str(row["uuid"]) for row in rows if row.get("uuid")]


def _dedupe_strings(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(str(value) for value in values if str(value)))


def _candidate_from_node_record(
    row: Mapping[str, object],
    *,
    signal: NativeRetrievalSignal,
    score: float,
    embedding_metadata: NativeEmbeddingMetadata | None = None,
) -> NativeRetrievalCandidate:
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
    return NativeRetrievalCandidate(
        id=str(row.get("uuid", "")),
        type=entity_type,
        name=str(row.get("name") or entity_type),
        content=content,
        score=score,
        source=source,
        metadata=metadata,
        project_id=project_id,
        created_at=_datetime_value(row.get("created_at")),
        policy_reason="project_access_verified" if project_id else "graph_projection_allowed",
        visibility="project" if project_id else "organization",
    )


def _candidate_from_episode_record(
    row: Mapping[str, object],
    *,
    signal: NativeRetrievalSignal,
    score: float,
) -> NativeRetrievalCandidate:
    source = _string_value(row.get("source_description")) or _string_value(row.get("uuid"))
    return NativeRetrievalCandidate(
        id=str(row.get("uuid", "")),
        type="episode",
        name=str(row.get("name") or "Episode"),
        content=str(row.get("content") or ""),
        score=score,
        source=source,
        metadata={
            "entity_type": "episode",
            "source_id": source,
            "retrieval_signals": [signal.value],
        },
        created_at=_datetime_value(row.get("created_at")),
        policy_reason="graph_projection_allowed",
        visibility="organization",
    )


def _candidate_from_edge_record(
    row: Mapping[str, object],
    *,
    signal: NativeRetrievalSignal,
    score: float,
    embedding_metadata: NativeEmbeddingMetadata | None = None,
) -> NativeRetrievalCandidate:
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
    return NativeRetrievalCandidate(
        id=str(row.get("uuid", "")),
        type="claim",
        name=str(row.get("name") or "Relationship"),
        content=str(row.get("fact") or ""),
        score=score,
        source=source,
        metadata=metadata,
        project_id=_string_value(metadata.get("project_id")),
        created_at=_datetime_value(row.get("created_at")),
        policy_reason="graph_projection_allowed",
        visibility="organization",
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
    for value in (
        attributes.get("content"),
        attributes.get("description"),
        row.get("content"),
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
    scope: NativeScopeSpec,
) -> NativeRetrievalCandidate:
    source = memory.source_id or memory.capture_surface
    project_id = memory.metadata.get("project_id") or memory.project_id or (
        memory.scope_key if memory.memory_scope is MemoryScope.PROJECT else None
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
    return NativeRetrievalCandidate(
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
    )


def _candidate_from_node(
    node: Any,
    *,
    signal: NativeRetrievalSignal,
    score: float,
) -> NativeRetrievalCandidate:
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
    return NativeRetrievalCandidate(
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
        policy_reason="project_access_verified" if project_id else "graph_projection_allowed",
        visibility="project" if project_id else "organization",
    )


def _candidate_from_episode(
    episode: Any,
    *,
    signal: NativeRetrievalSignal,
    score: float,
) -> NativeRetrievalCandidate:
    source = _string_value(getattr(episode, "source_description", None)) or _string_value(
        getattr(episode, "uuid", None)
    )
    return NativeRetrievalCandidate(
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
        policy_reason="graph_projection_allowed",
        visibility="organization",
    )


def _candidate_from_edge(
    edge: Any,
    *,
    signal: NativeRetrievalSignal,
    score: float,
) -> NativeRetrievalCandidate:
    attributes = _attributes(edge)
    source = _string_value(attributes.get("source_id") or getattr(edge, "uuid", None))
    source_project_id = _string_value(
        getattr(edge, "source_node_project_id", None) or attributes.get("source_node_project_id")
    )
    target_project_id = _string_value(
        getattr(edge, "target_node_project_id", None) or attributes.get("target_node_project_id")
    )
    return NativeRetrievalCandidate(
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
        project_id=_string_value(attributes.get("project_id")),
        created_at=_datetime_value(getattr(edge, "created_at", None)),
        policy_reason="graph_projection_allowed",
        visibility="organization",
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
    for value in (
        attributes.get("content"),
        attributes.get("description"),
        getattr(node, "content", None),
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
    candidate: NativeRetrievalCandidate,
    *,
    plan: NativeRetrievalPlan,
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


def _candidate_scope_allowed(
    candidate: NativeRetrievalCandidate, plan: NativeRetrievalPlan
) -> bool:
    metadata = candidate.metadata if isinstance(candidate.metadata, Mapping) else {}
    raw_scope = metadata.get("memory_scope")
    if raw_scope is None:
        return True
    memory_scope = _coerce_memory_scope(raw_scope)
    if memory_scope is None:
        return False
    scope_key = _string_value(metadata.get("scope_key"))
    plan_principal = plan.scopes[0].principal_id if plan.scopes else None
    if memory_scope is MemoryScope.PRIVATE:
        owner = _string_value(metadata.get("principal_id")) or scope_key
        # A private candidate is only authorized if a private scope survived
        # plan filtering; otherwise an API key without a private memory grant
        # could still read the principal's own private graph rows.
        has_private_scope = any(scope.memory_scope is MemoryScope.PRIVATE for scope in plan.scopes)
        return has_private_scope and bool(plan_principal) and owner == plan_principal
    agent_id = next((scope.agent_id for scope in plan.scopes if scope.agent_id), None)
    decision = authorize_memory_read(
        principal_id=plan_principal,
        memory_scope=memory_scope,
        scope_key=scope_key,
        project_id=plan.project,
        agent_id=agent_id,
        accessible_projects=plan.accessible_projects,
    )
    return decision.allowed


def _coerce_memory_scope(value: object) -> MemoryScope | None:
    if isinstance(value, MemoryScope):
        return value
    try:
        return MemoryScope(str(value))
    except ValueError:
        return None


def _candidate_matches_types(
    candidate: NativeRetrievalCandidate,
    requested_types: set[str],
    facet: ContextFacet | None,
) -> bool:
    if candidate.type in requested_types:
        return True
    return candidate.type == "raw_memory" and facet is ContextFacet.RECENT_MEMORY


def _fuse_candidates(
    source_lists: Sequence[tuple[NativeRetrievalSignal, Sequence[NativeRetrievalCandidate]]],
    *,
    plan: NativeRetrievalPlan,
    limit: int,
) -> list[tuple[NativeRetrievalCandidate, float, dict[str, Any]]]:
    return _rank_fused_candidates(
        source_lists,
        plan=plan,
        limit=limit,
        rrf_scores=_python_rrf_scores(source_lists, rrf_k=plan.weights.rrf_k),
    )


async def _fuse_candidates_for_plan(
    *,
    client: Any,
    source_lists: Sequence[tuple[NativeRetrievalSignal, Sequence[NativeRetrievalCandidate]]],
    plan: NativeRetrievalPlan,
    limit: int,
    fusion_backend: NativeFusionBackend | None = None,
) -> list[tuple[NativeRetrievalCandidate, float, dict[str, Any]]]:
    backend = fusion_backend or DEFAULT_NATIVE_FUSION_BACKEND
    if backend is NativeFusionBackend.SURREAL_RRF:
        native_scores = await _surreal_rrf_scores(client, source_lists, plan=plan, limit=limit)
        if native_scores:
            return _rank_fused_candidates(
                source_lists,
                plan=plan,
                limit=limit,
                rrf_scores=native_scores,
                backend=backend,
            )
    return _fuse_candidates(source_lists, plan=plan, limit=limit)


def _python_rrf_scores(
    source_lists: Sequence[tuple[NativeRetrievalSignal, Sequence[NativeRetrievalCandidate]]],
    *,
    rrf_k: int,
) -> dict[str, float]:
    scores: dict[str, float] = defaultdict(float)
    for _signal, candidates in source_lists:
        for rank, candidate in enumerate(candidates, start=1):
            scores[candidate.id] += 1.0 / (rrf_k + rank)
    return dict(scores)


async def _surreal_rrf_scores(
    client: Any,
    source_lists: Sequence[tuple[NativeRetrievalSignal, Sequence[NativeRetrievalCandidate]]],
    *,
    plan: NativeRetrievalPlan,
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
    try:
        rows = normalize_records(
            await client.execute_query(
                "RETURN search::rrf($lists, $limit, $k);",
                lists=rrf_inputs,
                limit=max(int(limit), unique_candidate_count, 1),
                k=plan.weights.rrf_k,
            )
        )
    except Exception as exc:
        log.warning(
            "native_surreal_rrf_failed",
            organization_id=plan.organization_id,
            error_type=type(exc).__name__,
        )
        return {}

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
    source_lists: Sequence[tuple[NativeRetrievalSignal, Sequence[NativeRetrievalCandidate]]],
    *,
    plan: NativeRetrievalPlan,
    limit: int,
    rrf_scores: Mapping[str, float],
    backend: NativeFusionBackend = NativeFusionBackend.PYTHON_RRF,
) -> list[tuple[NativeRetrievalCandidate, float, dict[str, Any]]]:
    score_by_id: dict[str, float] = defaultdict(float)
    candidates_by_id: dict[str, NativeRetrievalCandidate] = {}
    metadata_by_id: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"sources": [], "ranks": {}, "original_scores": {}, "fusion_backend": backend.value}
    )

    for signal, candidates in source_lists:
        for rank, candidate in enumerate(candidates, start=1):
            score_by_id[candidate.id] = float(rrf_scores.get(candidate.id, 0.0))
            candidates_by_id.setdefault(candidate.id, candidate)
            metadata_by_id[candidate.id]["sources"].append(signal.value)
            metadata_by_id[candidate.id]["ranks"][signal.value] = rank
            metadata_by_id[candidate.id]["original_scores"][signal.value] = candidate.score

    ranked: list[tuple[NativeRetrievalCandidate, float, dict[str, Any]]] = []
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
        boosted = _boost_score(candidate, score, plan=plan)
        ranked.append((candidate, boosted, fusion_metadata))
    ranked.sort(key=lambda item: item[1], reverse=True)
    return ranked[:limit]


def _vector_only_demote_multiplier(
    plan: NativeRetrievalPlan,
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
            NativeRetrievalSignal.NODE_VECTOR.value,
            NativeRetrievalSignal.EDGE_VECTOR.value,
        }
        for signal in signals
    ):
        return 1.0
    if plan.filter_selectivity_threshold <= 0:
        return 1.0
    return max(plan.filter_selectivity / plan.filter_selectivity_threshold, 0.1)


def _graph_expansion_only_multiplier(
    plan: NativeRetrievalPlan,
    *,
    signals: Sequence[str],
) -> float:
    if set(signals) != {NativeRetrievalSignal.GRAPH_EXPANSION.value}:
        return 1.0
    return max(min(plan.weights.graph_expansion_only_boost, 1.0), 0.0)


def _boost_score(
    candidate: NativeRetrievalCandidate,
    score: float,
    *,
    plan: NativeRetrievalPlan,
) -> float:
    boosted = score
    status = _string_value(candidate.metadata.get("status"))
    if candidate.type == "task" and status in _ACTIVE_TASK_STATUSES:
        boosted *= plan.weights.active_task_state_boost
    if plan.project and candidate.project_id == plan.project:
        boosted *= plan.weights.project_match_boost
    if candidate.type == "raw_memory":
        boosted *= plan.weights.direct_raw_source_boost
    boosted *= _freshness_boost(candidate.created_at, cap=plan.weights.freshness_boost_cap)
    return boosted


def _freshness_boost(created_at: datetime | None, *, cap: float) -> float:
    if created_at is None:
        return 1.0
    now = datetime.now(UTC)
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=UTC)
    age_days = max((now - created_at).total_seconds() / 86_400, 0.0)
    return min(cap, 1.0 + (0.5 / (1.0 + age_days)))


def _search_result_from_candidate(
    candidate: NativeRetrievalCandidate,
    *,
    score: float,
    fusion_metadata: Mapping[str, Any],
    include_content: bool,
) -> SearchResult:
    from sibyl_core.tools.responses import SearchResult

    freshness = _freshness_boost(candidate.created_at, cap=1.5)
    metadata = {
        **dict(candidate.metadata),
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
