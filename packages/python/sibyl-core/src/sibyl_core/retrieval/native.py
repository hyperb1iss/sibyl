"""Native SurrealDB retrieval planning contracts."""

from __future__ import annotations

import asyncio
import os
from collections import defaultdict
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any, cast

from sibyl_core.auth.memory_policy import MemoryPolicyDecision, authorize_memory_read
from sibyl_core.graph.search_interface import SurrealSearchInterface
from sibyl_core.models.context import ContextFacet
from sibyl_core.services import get_graph_runtime
from sibyl_core.services.surreal_content import MemoryScope, RawMemory, recall_raw_memory

if TYPE_CHECKING:
    from sibyl_core.tools.responses import SearchResponse, SearchResult

type RawMemoryRecallFn = Callable[..., Awaitable[list[RawMemory]]]

DEFAULT_FILTER_SELECTIVITY_THRESHOLD = 0.1
_ACTIVE_TASK_STATUSES = {"doing", "in_progress", "review"}
_RAW_MEMORY_CONTEXT_TYPES = {"raw_memory", "session", "episode", "note"}


class NativeRetrievalMode(StrEnum):
    GRAPHITI = "graphiti"
    NATIVE = "native"
    COMPARE = "compare"


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
        return NativeRetrievalMode.GRAPHITI
    try:
        return NativeRetrievalMode(value.strip().lower())
    except ValueError:
        return NativeRetrievalMode.GRAPHITI


def native_retrieval_mode_from_env(
    environ: Mapping[str, str] | None = None,
) -> NativeRetrievalMode:
    source = os.environ if environ is None else environ
    return coerce_native_retrieval_mode(source.get("SIBYL_RETRIEVAL_MODE"))


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
) -> NativeRetrievalPlan:
    scopes: list[NativeScopeSpec] = []
    denied_scopes: list[MemoryPolicyDecision] = []
    normalized_accessible_projects = (
        frozenset(str(value) for value in accessible_projects)
        if accessible_projects is not None
        else None
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
        accessible_projects=normalized_accessible_projects,
    )


async def native_context_search(
    *,
    plan: NativeRetrievalPlan,
    types: Sequence[str] | None = None,
    facet: ContextFacet | None = None,
    limit: int = 10,
    include_content: bool = True,
    raw_memory_recall_fn: RawMemoryRecallFn = recall_raw_memory,
) -> SearchResponse:
    """Search context-pack candidates through native SurrealDB paths."""

    from sibyl_core.tools.responses import SearchResponse

    limit = max(1, min(limit, 50))
    runtime = await get_graph_runtime(plan.organization_id)
    driver = runtime.client.get_org_driver(plan.organization_id)
    interface = SurrealSearchInterface()
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
            interface=interface,
            driver=driver,
            plan=plan,
            search_filter=search_filter,
            limit=plan.candidate_limits.node_fulltext,
        ),
        _episode_fulltext_candidates(
            interface=interface,
            driver=driver,
            plan=plan,
            search_filter=search_filter,
            limit=plan.candidate_limits.episode_fulltext,
        ),
        _edge_fulltext_candidates(
            interface=interface,
            driver=driver,
            plan=plan,
            search_filter=search_filter,
            limit=plan.candidate_limits.edge_fulltext,
        ),
    ]
    raw_candidates, graph_candidate_lists = await _gather_candidate_sources(raw_task, graph_tasks)

    vector_candidate_lists = await _vector_candidate_sources(
        interface=interface,
        driver=driver,
        plan=plan,
        search_filter=search_filter,
    )
    graph_expansion_candidates = await _graph_expansion_candidates(
        interface=interface,
        driver=driver,
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
    fused = _fuse_candidates(filtered_lists, plan=plan, limit=limit)
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
        },
        graph_count=len([result for result in results if result.result_origin == "graph"]),
        document_count=0,
        limit=limit,
    )


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
        decisions.append(
            (
                authorize_memory_read(
                    principal_id=principal_id,
                    memory_scope=MemoryScope.PRIVATE,
                    project_id=project,
                    agent_id=agent_id,
                    accessible_projects=accessible_projects,
                ),
                project,
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
    project_ids: tuple[str, ...] = ()
    if plan.project:
        project_ids = (plan.project,)
    elif plan.accessible_projects:
        project_ids = tuple(sorted(plan.accessible_projects))
    return NativeSearchFilter(project_ids=project_ids)


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
            if memory.id in seen_ids:
                continue
            seen_ids.add(memory.id)
            candidates.append(_candidate_from_raw_memory(memory, scope))
    return candidates


async def _node_fulltext_candidates(
    *,
    interface: SurrealSearchInterface,
    driver: Any,
    plan: NativeRetrievalPlan,
    search_filter: NativeSearchFilter,
    limit: int,
) -> list[NativeRetrievalCandidate]:
    nodes = await interface.node_fulltext_search(
        driver,
        plan.query,
        search_filter,
        [plan.organization_id],
        limit,
    )
    return [
        _candidate_from_node(
            node,
            signal=NativeRetrievalSignal.NODE_FULLTEXT,
            score=_node_score(node),
        )
        for node in nodes
    ]


async def _episode_fulltext_candidates(
    *,
    interface: SurrealSearchInterface,
    driver: Any,
    plan: NativeRetrievalPlan,
    search_filter: NativeSearchFilter,
    limit: int,
) -> list[NativeRetrievalCandidate]:
    episodes = await interface.episode_fulltext_search(
        driver,
        plan.query,
        search_filter,
        [plan.organization_id],
        limit,
    )
    return [
        _candidate_from_episode(
            episode,
            signal=NativeRetrievalSignal.EPISODE_FULLTEXT,
            score=_node_score(episode),
        )
        for episode in episodes
    ]


async def _edge_fulltext_candidates(
    *,
    interface: SurrealSearchInterface,
    driver: Any,
    plan: NativeRetrievalPlan,
    search_filter: NativeSearchFilter,
    limit: int,
) -> list[NativeRetrievalCandidate]:
    edges = await interface.edge_fulltext_search(
        driver,
        plan.query,
        search_filter,
        [plan.organization_id],
        limit,
    )
    return [
        _candidate_from_edge(
            edge,
            signal=NativeRetrievalSignal.EDGE_FULLTEXT,
            score=_node_score(edge),
        )
        for edge in edges
    ]


async def _vector_candidate_sources(
    *,
    interface: SurrealSearchInterface,
    driver: Any,
    plan: NativeRetrievalPlan,
    search_filter: NativeSearchFilter,
) -> list[list[NativeRetrievalCandidate]]:
    try:
        runtime = await get_graph_runtime(plan.organization_id)
        vector = await runtime.client.client.embedder.create(plan.query)
    except Exception:
        return [[], []]

    node_results, edge_results = await asyncio.gather(
        interface.node_similarity_search(
            driver,
            vector,
            search_filter,
            [plan.organization_id],
            plan.candidate_limits.node_vector,
            plan.vector_min_score,
        ),
        interface.edge_similarity_search(
            driver,
            vector,
            None,
            None,
            search_filter,
            [plan.organization_id],
            plan.candidate_limits.edge_vector,
            plan.vector_min_score,
        ),
        return_exceptions=True,
    )
    return [
        [
            _candidate_from_node(
                node,
                signal=NativeRetrievalSignal.NODE_VECTOR,
                score=_node_score(node),
            )
            for node in _object_list_or_empty(node_results)
        ],
        [
            _candidate_from_edge(
                edge,
                signal=NativeRetrievalSignal.EDGE_VECTOR,
                score=_node_score(edge),
            )
            for edge in _object_list_or_empty(edge_results)
        ],
    ]


async def _graph_expansion_candidates(
    *,
    interface: SurrealSearchInterface,
    driver: Any,
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

    nodes = await interface.node_bfs_search(
        driver,
        seed_uuids,
        search_filter,
        plan.graph_expansion_depth,
        [plan.organization_id],
        limit,
    )
    return [
        _candidate_from_node(
            node,
            signal=NativeRetrievalSignal.GRAPH_EXPANSION,
            score=1.0,
        )
        for node in nodes
    ]


def _object_list_or_empty(result: object) -> list[Any]:
    if isinstance(result, BaseException) or not isinstance(result, list):
        return []
    return result


def _candidate_from_raw_memory(
    memory: RawMemory,
    scope: NativeScopeSpec,
) -> NativeRetrievalCandidate:
    source = memory.source_id or memory.capture_surface
    project_id = memory.metadata.get("project_id") or (
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
    if candidate.type == "episode" and (plan.project or plan.accessible_projects is not None):
        return False
    if plan.project and candidate.project_id and candidate.project_id != plan.project:
        return False
    return not (
        plan.accessible_projects is not None
        and candidate.project_id is not None
        and candidate.project_id not in plan.accessible_projects
    )


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
    score_by_id: dict[str, float] = defaultdict(float)
    candidates_by_id: dict[str, NativeRetrievalCandidate] = {}
    metadata_by_id: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"sources": [], "ranks": {}, "original_scores": {}}
    )

    for signal, candidates in source_lists:
        for rank, candidate in enumerate(candidates, start=1):
            score_by_id[candidate.id] += 1.0 / (plan.weights.rrf_k + rank)
            candidates_by_id.setdefault(candidate.id, candidate)
            metadata_by_id[candidate.id]["sources"].append(signal.value)
            metadata_by_id[candidate.id]["ranks"][signal.value] = rank
            metadata_by_id[candidate.id]["original_scores"][signal.value] = candidate.score

    ranked: list[tuple[NativeRetrievalCandidate, float, dict[str, Any]]] = []
    for candidate_id, score in score_by_id.items():
        candidate = candidates_by_id[candidate_id]
        boosted = _boost_score(candidate, score, plan=plan)
        ranked.append((candidate, boosted, metadata_by_id[candidate_id]))
    ranked.sort(key=lambda item: item[1], reverse=True)
    return ranked[:limit]


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
