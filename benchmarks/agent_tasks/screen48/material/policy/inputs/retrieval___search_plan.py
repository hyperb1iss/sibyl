"""Retrieval planning contracts and authorization-aware filter construction."""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum

from sibyl_core.auth.memory_policy import (
    MemoryPolicyDecision,
    authorize_memory_read,
    memory_scope_policy_key,
)
from sibyl_core.models.context import ContextFacet
from sibyl_core.services.surreal_content import MemoryScope

DEFAULT_FILTER_SELECTIVITY_THRESHOLD = 0.1
MIN_CANDIDATES_PER_SIGNAL = 2
MAX_RETRIEVAL_LIMIT = 50
DEFAULT_CANDIDATES_PER_SIGNAL = 8
RAW_LEXICAL_LIMIT_DIVISOR = 4
_EDGE_CONTEXT_TYPES = {"claim", "relationship"}


class FusionBackend(StrEnum):
    PYTHON_RRF = "python_rrf"
    SURREAL_RRF = "surreal_rrf"


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
