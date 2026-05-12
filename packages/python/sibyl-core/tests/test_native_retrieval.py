from __future__ import annotations

from sibyl_core.models.context import ContextFacet
from sibyl_core.retrieval.native import (
    DEFAULT_FILTER_SELECTIVITY_THRESHOLD,
    NativeRetrievalMode,
    NativeRetrievalSignal,
    build_native_context_retrieval_plan,
    coerce_native_retrieval_mode,
    native_retrieval_mode_from_env,
)
from sibyl_core.services.surreal_content import MemoryScope


def test_native_retrieval_mode_defaults_to_graphiti() -> None:
    assert coerce_native_retrieval_mode(None) is NativeRetrievalMode.GRAPHITI
    assert coerce_native_retrieval_mode("") is NativeRetrievalMode.GRAPHITI
    assert coerce_native_retrieval_mode("surreal") is NativeRetrievalMode.GRAPHITI
    assert native_retrieval_mode_from_env({}) is NativeRetrievalMode.GRAPHITI


def test_native_retrieval_mode_accepts_native_and_compare() -> None:
    assert coerce_native_retrieval_mode("native") is NativeRetrievalMode.NATIVE
    assert coerce_native_retrieval_mode("COMPARE") is NativeRetrievalMode.COMPARE
    assert native_retrieval_mode_from_env({"SIBYL_RETRIEVAL_MODE": "native"}) is (
        NativeRetrievalMode.NATIVE
    )


def test_build_native_context_retrieval_plan_records_scopes_and_weights() -> None:
    plan = build_native_context_retrieval_plan(
        query="ship native retrieval",
        organization_id="org-123",
        facets=[ContextFacet.ACTIVE_WORK, ContextFacet.RECENT_MEMORY],
        facet_types={
            ContextFacet.ACTIVE_WORK: ["task", "epic", "project"],
            ContextFacet.RECENT_MEMORY: ["session", "episode", "note"],
        },
        principal_id="user-123",
        project="project_123",
        accessible_projects={"project_123"},
        agent_id="nova",
        limit=24,
    )

    assert plan.organization_id == "org-123"
    assert plan.facets == (ContextFacet.ACTIVE_WORK, ContextFacet.RECENT_MEMORY)
    assert plan.facet_types[ContextFacet.ACTIVE_WORK] == ("task", "epic", "project")
    assert [scope.memory_scope for scope in plan.scopes] == [
        MemoryScope.PRIVATE,
        MemoryScope.PROJECT,
        MemoryScope.PRIVATE,
    ]
    assert [scope.policy_reason for scope in plan.scopes] == [
        "private_principal_bound",
        "project_access_verified",
        "agent_diary_private_read_allowed",
    ]
    assert plan.denied_scopes == ()
    assert plan.weights.rrf_k == 60
    assert plan.weights.active_task_state_boost == 1.3
    assert plan.weights.project_match_boost == 1.2
    assert plan.weights.direct_raw_source_boost == 1.4
    assert plan.weights.freshness_boost_cap == 1.5
    assert plan.filter_selectivity_threshold == DEFAULT_FILTER_SELECTIVITY_THRESHOLD
    assert NativeRetrievalSignal.RAW_LEXICAL in plan.signals
    assert NativeRetrievalSignal.GRAPH_EXPANSION in plan.signals


def test_build_native_context_retrieval_plan_denies_unverified_project_scope() -> None:
    plan = build_native_context_retrieval_plan(
        query="private only",
        organization_id="org-123",
        facets=[ContextFacet.RECENT_MEMORY],
        facet_types={ContextFacet.RECENT_MEMORY: ["session", "episode", "note"]},
        principal_id="user-123",
        project="project_123",
        accessible_projects={"project_other"},
        limit=6,
    )

    assert [scope.memory_scope for scope in plan.scopes] == [MemoryScope.PRIVATE]
    assert len(plan.denied_scopes) == 1
    assert plan.denied_scopes[0].memory_scope is MemoryScope.PROJECT
    assert plan.denied_scopes[0].scope_key == "project_123"
    assert plan.denied_scopes[0].reason == "unverified_membership"


def test_build_native_context_retrieval_plan_requires_principal() -> None:
    plan = build_native_context_retrieval_plan(
        query="no principal",
        organization_id="org-123",
        facets=[ContextFacet.RECENT_MEMORY],
        facet_types={ContextFacet.RECENT_MEMORY: ["session", "episode", "note"]},
        principal_id=None,
        project="project_123",
        accessible_projects={"project_123"},
        agent_id="nova",
    )

    assert plan.scopes == ()
    assert [decision.reason for decision in plan.denied_scopes] == [
        "principal_mismatch",
        "principal_mismatch",
        "principal_mismatch",
    ]
