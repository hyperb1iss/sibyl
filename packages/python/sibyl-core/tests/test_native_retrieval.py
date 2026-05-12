from __future__ import annotations

import sibyl_core.retrieval.native as native_module
from sibyl_core.models.context import ContextFacet
from sibyl_core.retrieval.native import (
    DEFAULT_FILTER_SELECTIVITY_THRESHOLD,
    NativeRetrievalCandidate,
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
    assert plan.filter_selectivity == 1.0


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
    assert native_module._search_filter_for_plan(plan).project_ids == ()
    assert not native_module._candidate_allowed(
        NativeRetrievalCandidate(
            id="task-1",
            type="task",
            name="Denied project task",
            content="Project task should not render.",
            score=1.0,
            source=None,
            metadata={},
            project_id="project_123",
        ),
        plan=plan,
        requested_types=set(),
        facet=None,
    )


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


def test_native_plan_estimates_project_filter_selectivity() -> None:
    accessible_projects = {f"project_{index}" for index in range(20)}
    plan = build_native_context_retrieval_plan(
        query="selective vector filter",
        organization_id="org-123",
        facets=[ContextFacet.ACTIVE_WORK],
        facet_types={ContextFacet.ACTIVE_WORK: ["task"]},
        principal_id="user-123",
        project="project_0",
        accessible_projects=accessible_projects,
    )

    assert plan.filter_selectivity == 1 / len(accessible_projects)


def test_vector_only_candidates_demote_under_selective_project_filter() -> None:
    accessible_projects = {f"project_{index}" for index in range(20)}
    plan = build_native_context_retrieval_plan(
        query="selective vector filter",
        organization_id="org-123",
        facets=[ContextFacet.ACTIVE_WORK],
        facet_types={ContextFacet.ACTIVE_WORK: ["task"]},
        principal_id="user-123",
        project="project_0",
        accessible_projects=accessible_projects,
    )
    vector_candidate = NativeRetrievalCandidate(
        id="vector-only",
        type="task",
        name="Vector only",
        content="A weak vector-only match.",
        score=1.0,
        source=None,
        metadata={},
        project_id="project_0",
    )
    lexical_candidate = NativeRetrievalCandidate(
        id="lexical",
        type="task",
        name="Lexical",
        content="A grounded lexical match.",
        score=1.0,
        source=None,
        metadata={},
        project_id="project_0",
    )

    ranked = native_module._fuse_candidates(
        [
            (NativeRetrievalSignal.NODE_VECTOR, [vector_candidate]),
            (NativeRetrievalSignal.NODE_FULLTEXT, [lexical_candidate]),
        ],
        plan=plan,
        limit=2,
    )

    assert [candidate.id for candidate, _, _ in ranked] == ["lexical", "vector-only"]
    assert ranked[1][2]["vector_only_demoted"] is True
    assert ranked[1][2]["filter_selectivity"] == plan.filter_selectivity


def test_vector_matches_with_lexical_signal_do_not_demote() -> None:
    accessible_projects = {f"project_{index}" for index in range(20)}
    plan = build_native_context_retrieval_plan(
        query="selective vector filter",
        organization_id="org-123",
        facets=[ContextFacet.ACTIVE_WORK],
        facet_types={ContextFacet.ACTIVE_WORK: ["task"]},
        principal_id="user-123",
        project="project_0",
        accessible_projects=accessible_projects,
    )
    candidate = NativeRetrievalCandidate(
        id="shared",
        type="task",
        name="Shared",
        content="A vector hit with lexical corroboration.",
        score=1.0,
        source=None,
        metadata={},
        project_id="project_0",
    )

    ranked = native_module._fuse_candidates(
        [
            (NativeRetrievalSignal.NODE_VECTOR, [candidate]),
            (NativeRetrievalSignal.NODE_FULLTEXT, [candidate]),
        ],
        plan=plan,
        limit=1,
    )

    assert "vector_only_demoted" not in ranked[0][2]
