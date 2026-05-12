"""Native SurrealDB retrieval planning contracts."""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from sibyl_core.auth.memory_policy import MemoryPolicyDecision, authorize_memory_read
from sibyl_core.models.context import ContextFacet
from sibyl_core.services.surreal_content import MemoryScope

DEFAULT_FILTER_SELECTIVITY_THRESHOLD = 0.1


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
