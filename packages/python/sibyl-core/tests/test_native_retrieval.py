from __future__ import annotations

import pytest

import sibyl_core.retrieval.native as native_module
from sibyl_core.auth.memory_policy import memory_scope_policy_key
from sibyl_core.embeddings.native import (
    DeterministicNativeEmbeddingProvider,
    NativeEmbeddingMetadata,
)
from sibyl_core.models.context import ContextFacet
from sibyl_core.retrieval.native import (
    DEFAULT_FILTER_SELECTIVITY_THRESHOLD,
    NativeFusionBackend,
    NativeRetrievalCandidate,
    NativeRetrievalMode,
    NativeRetrievalSignal,
    build_native_context_retrieval_plan,
    coerce_native_fusion_backend,
    coerce_native_retrieval_mode,
    native_fusion_backend_from_env,
    native_retrieval_mode_from_env,
)
from sibyl_core.services.surreal_content import MemoryScope, RawMemory


def test_native_retrieval_mode_defaults_to_native() -> None:
    assert coerce_native_retrieval_mode(None) is NativeRetrievalMode.NATIVE
    assert coerce_native_retrieval_mode("") is NativeRetrievalMode.NATIVE
    assert coerce_native_retrieval_mode("surreal") is NativeRetrievalMode.NATIVE
    assert native_retrieval_mode_from_env({}) is NativeRetrievalMode.NATIVE


def test_native_retrieval_mode_accepts_native_and_compare() -> None:
    assert coerce_native_retrieval_mode("graphiti") is NativeRetrievalMode.NATIVE
    assert coerce_native_retrieval_mode("native") is NativeRetrievalMode.NATIVE
    assert coerce_native_retrieval_mode("COMPARE") is NativeRetrievalMode.COMPARE
    assert native_retrieval_mode_from_env({"SIBYL_RETRIEVAL_MODE": "native"}) is (
        NativeRetrievalMode.NATIVE
    )


def test_native_fusion_backend_defaults_to_python_rrf() -> None:
    assert coerce_native_fusion_backend(None) is NativeFusionBackend.PYTHON_RRF
    assert coerce_native_fusion_backend("") is NativeFusionBackend.PYTHON_RRF
    assert coerce_native_fusion_backend("invalid") is NativeFusionBackend.PYTHON_RRF
    assert native_fusion_backend_from_env({}) is NativeFusionBackend.PYTHON_RRF


def test_native_fusion_backend_accepts_surreal_rrf() -> None:
    assert coerce_native_fusion_backend("surreal_rrf") is NativeFusionBackend.SURREAL_RRF
    assert coerce_native_fusion_backend("SURREAL_RRF") is NativeFusionBackend.SURREAL_RRF
    assert native_fusion_backend_from_env({"SIBYL_NATIVE_FUSION_BACKEND": "surreal_rrf"}) is (
        NativeFusionBackend.SURREAL_RRF
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
    assert plan.weights.graph_expansion_only_boost == 0.45
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


def test_memory_scope_policy_key_uses_unit_separator_format() -> None:
    assert memory_scope_policy_key(MemoryScope.PRIVATE, "user-123") == "private\x1fuser-123"
    assert memory_scope_policy_key("project", " project_123 ") == "project\x1fproject_123"
    assert memory_scope_policy_key(MemoryScope.PRIVATE, None) == "private\x1f"


def _scoped_plan(allowed_memory_scope_keys: set[str] | None) -> native_module.NativeRetrievalPlan:
    return build_native_context_retrieval_plan(
        query="api key scoped retrieval",
        organization_id="org-123",
        facets=[ContextFacet.RECENT_MEMORY],
        facet_types={ContextFacet.RECENT_MEMORY: ["session", "episode", "note"]},
        principal_id="user-123",
        project="project_123",
        accessible_projects={"project_123"},
        agent_id="nova",
        limit=12,
        allowed_memory_scope_keys=allowed_memory_scope_keys,
    )


def test_build_native_context_retrieval_plan_skips_scope_filter_when_unset() -> None:
    plan = _scoped_plan(None)

    assert [scope.memory_scope for scope in plan.scopes] == [
        MemoryScope.PRIVATE,
        MemoryScope.PROJECT,
        MemoryScope.PRIVATE,
    ]
    assert not [d for d in plan.denied_scopes if d.reason == "api_key_scope_excluded"]


def test_build_native_context_retrieval_plan_keeps_scopes_within_api_key_grants() -> None:
    plan = _scoped_plan(
        {
            memory_scope_policy_key(MemoryScope.PRIVATE, "user-123"),
            memory_scope_policy_key(MemoryScope.PROJECT, "project_123"),
        }
    )

    assert [scope.memory_scope for scope in plan.scopes] == [
        MemoryScope.PRIVATE,
        MemoryScope.PROJECT,
        MemoryScope.PRIVATE,
    ]
    assert not [d for d in plan.denied_scopes if d.reason == "api_key_scope_excluded"]


def test_build_native_context_retrieval_plan_excludes_scopes_outside_api_key_grants() -> None:
    plan = _scoped_plan({memory_scope_policy_key(MemoryScope.PRIVATE, "user-123")})

    assert [scope.memory_scope for scope in plan.scopes] == [
        MemoryScope.PRIVATE,
        MemoryScope.PRIVATE,
    ]
    excluded = [d for d in plan.denied_scopes if d.reason == "api_key_scope_excluded"]
    assert [d.memory_scope for d in excluded] == [MemoryScope.PROJECT]
    assert excluded[0].scope_key == "project_123"
    assert native_module._search_filter_for_plan(plan).project_ids == ()


def test_build_native_context_retrieval_plan_excludes_all_scopes_when_no_grant_matches() -> None:
    plan = _scoped_plan({memory_scope_policy_key(MemoryScope.PROJECT, "project_other")})

    assert plan.scopes == ()
    assert {d.reason for d in plan.denied_scopes} == {"api_key_scope_excluded"}


def test_build_native_context_retrieval_plan_trims_accessible_projects_to_api_key_grants() -> None:
    plan = build_native_context_retrieval_plan(
        query="unscoped pack",
        organization_id="org-123",
        facets=[ContextFacet.ACTIVE_WORK],
        facet_types={ContextFacet.ACTIVE_WORK: ["task", "epic", "project"]},
        principal_id="user-123",
        project=None,
        accessible_projects={"project_123", "project_456"},
        allowed_memory_scope_keys={memory_scope_policy_key(MemoryScope.PRIVATE, "user-123")},
    )

    assert plan.accessible_projects == frozenset()
    assert native_module._authorized_project_ids(plan) == ()


def test_build_native_context_retrieval_plan_keeps_granted_accessible_projects() -> None:
    plan = build_native_context_retrieval_plan(
        query="unscoped pack",
        organization_id="org-123",
        facets=[ContextFacet.ACTIVE_WORK],
        facet_types={ContextFacet.ACTIVE_WORK: ["task", "epic", "project"]},
        principal_id="user-123",
        project=None,
        accessible_projects={"project_123", "project_456"},
        allowed_memory_scope_keys={
            memory_scope_policy_key(MemoryScope.PRIVATE, "user-123"),
            memory_scope_policy_key(MemoryScope.PROJECT, "project_123"),
        },
    )

    assert plan.accessible_projects == frozenset({"project_123"})
    assert native_module._authorized_project_ids(plan) == ("project_123",)


def test_build_native_context_retrieval_plan_keeps_all_accessible_projects_when_unscoped() -> None:
    plan = build_native_context_retrieval_plan(
        query="unscoped pack",
        organization_id="org-123",
        facets=[ContextFacet.ACTIVE_WORK],
        facet_types={ContextFacet.ACTIVE_WORK: ["task", "epic", "project"]},
        principal_id="user-123",
        project=None,
        accessible_projects={"project_123", "project_456"},
        allowed_memory_scope_keys=None,
    )

    assert plan.accessible_projects == frozenset({"project_123", "project_456"})


def test_candidate_allowed_denies_private_candidate_without_private_grant() -> None:
    plan = build_native_context_retrieval_plan(
        query="private memory",
        organization_id="org-123",
        facets=[ContextFacet.RECENT_MEMORY],
        facet_types={ContextFacet.RECENT_MEMORY: ["episode", "note"]},
        principal_id="bob",
        project="project_123",
        accessible_projects={"project_123"},
        allowed_memory_scope_keys={memory_scope_policy_key(MemoryScope.PROJECT, "project_123")},
    )

    assert MemoryScope.PRIVATE not in [scope.memory_scope for scope in plan.scopes]
    assert not native_module._candidate_allowed(
        NativeRetrievalCandidate(
            id="entity-1",
            type="note",
            name="Bob private reflection promoted into project",
            content="secret",
            score=1.0,
            source=None,
            metadata={"memory_scope": "private", "scope_key": "bob", "principal_id": "bob"},
            project_id="project_123",
        ),
        plan=plan,
        requested_types=set(),
        facet=None,
    )


def test_candidate_allowed_allows_private_candidate_with_private_grant() -> None:
    plan = build_native_context_retrieval_plan(
        query="private memory",
        organization_id="org-123",
        facets=[ContextFacet.RECENT_MEMORY],
        facet_types={ContextFacet.RECENT_MEMORY: ["episode", "note"]},
        principal_id="bob",
        project="project_123",
        accessible_projects={"project_123"},
        allowed_memory_scope_keys={
            memory_scope_policy_key(MemoryScope.PRIVATE, "bob"),
            memory_scope_policy_key(MemoryScope.PROJECT, "project_123"),
        },
    )

    assert native_module._candidate_allowed(
        NativeRetrievalCandidate(
            id="entity-2",
            type="note",
            name="Bob private reflection promoted into project",
            content="secret",
            score=1.0,
            source=None,
            metadata={"memory_scope": "private", "scope_key": "bob", "principal_id": "bob"},
            project_id="project_123",
        ),
        plan=plan,
        requested_types=set(),
        facet=None,
    )


def test_candidate_allowed_denies_private_scope_key_mismatch() -> None:
    plan = build_native_context_retrieval_plan(
        query="private memory",
        organization_id="org-123",
        facets=[ContextFacet.RECENT_MEMORY],
        facet_types={ContextFacet.RECENT_MEMORY: ["episode", "note"]},
        principal_id="bob",
        project=None,
        accessible_projects=None,
        agent_id=None,
    )

    assert not native_module._candidate_allowed(
        NativeRetrievalCandidate(
            id="entity-1",
            type="note",
            name="Alice private reflection",
            content="secret",
            score=1.0,
            source=None,
            metadata={"memory_scope": "private", "scope_key": "alice"},
            project_id=None,
        ),
        plan=plan,
        requested_types=set(),
        facet=None,
    )


def test_candidate_allowed_allows_private_scope_key_match() -> None:
    plan = build_native_context_retrieval_plan(
        query="private memory",
        organization_id="org-123",
        facets=[ContextFacet.RECENT_MEMORY],
        facet_types={ContextFacet.RECENT_MEMORY: ["episode", "note"]},
        principal_id="bob",
        project=None,
        accessible_projects=None,
        agent_id=None,
    )

    assert native_module._candidate_allowed(
        NativeRetrievalCandidate(
            id="entity-2",
            type="note",
            name="Bob private reflection",
            content="mine",
            score=1.0,
            source=None,
            metadata={"memory_scope": "private", "scope_key": "bob"},
            project_id=None,
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


def test_candidate_allowed_rejects_cross_project_claim_edge() -> None:
    plan = build_native_context_retrieval_plan(
        query="edge permissions",
        organization_id="org-123",
        facets=[ContextFacet.ACTIVE_WORK],
        facet_types={ContextFacet.ACTIVE_WORK: ["claim"]},
        principal_id="user-123",
        project="project_A",
        accessible_projects={"project_A"},
    )
    candidate = NativeRetrievalCandidate(
        id="edge-1",
        type="claim",
        name="Cross-project relationship",
        content="Sensitive relationship",
        score=1.0,
        source=None,
        metadata={
            "source_node_project_id": "project_A",
            "target_node_project_id": "project_B",
        },
        project_id=None,
    )

    assert not native_module._candidate_allowed(
        candidate,
        plan=plan,
        requested_types=set(),
        facet=None,
    )


def test_candidate_allowed_accepts_claim_edge_when_both_endpoints_accessible() -> None:
    plan = build_native_context_retrieval_plan(
        query="edge permissions",
        organization_id="org-123",
        facets=[ContextFacet.ACTIVE_WORK],
        facet_types={ContextFacet.ACTIVE_WORK: ["claim"]},
        principal_id="user-123",
        project="project_A",
        accessible_projects={"project_A"},
    )
    candidate = NativeRetrievalCandidate(
        id="edge-1",
        type="claim",
        name="In-project relationship",
        content="Safe relationship",
        score=1.0,
        source=None,
        metadata={
            "source_node_project_id": "project_A",
            "target_node_project_id": "project_A",
        },
        project_id=None,
    )

    assert native_module._candidate_allowed(
        candidate,
        plan=plan,
        requested_types=set(),
        facet=None,
    )


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


def test_graph_expansion_only_sessions_demote_below_direct_hits() -> None:
    plan = build_native_context_retrieval_plan(
        query="coffee limit",
        organization_id="org-123",
        facets=[ContextFacet.RECENT_MEMORY],
        facet_types={ContextFacet.RECENT_MEMORY: ["session"]},
        principal_id="user-123",
        project=None,
        accessible_projects=None,
    )
    graph_candidate = NativeRetrievalCandidate(
        id="graph-only",
        type="session",
        name="Graph only",
        content="A session found only through projected memory graph expansion.",
        score=1.0,
        source=None,
        metadata={},
    )
    direct_candidate = NativeRetrievalCandidate(
        id="direct",
        type="session",
        name="Direct",
        content="A direct lexical session hit.",
        score=1.0,
        source=None,
        metadata={},
    )

    ranked = native_module._fuse_candidates(
        [
            (NativeRetrievalSignal.GRAPH_EXPANSION, [graph_candidate]),
            (NativeRetrievalSignal.NODE_FULLTEXT, [direct_candidate]),
        ],
        plan=plan,
        limit=2,
    )

    assert [candidate.id for candidate, _, _ in ranked] == ["direct", "graph-only"]
    assert ranked[1][2]["graph_expansion_only_demoted"] is True
    assert ranked[1][2]["graph_expansion_only_multiplier"] == 0.45


class _RrfClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def execute_query(self, query: str, **params: object) -> list[dict[str, object]]:
        self.calls.append((query, params))
        return [
            {"id": "shared", "rrf_score": 0.05},
            {"id": "lexical", "rff_score": 0.01},
        ]


class _FailingRrfClient:
    async def execute_query(self, _query: str, **_params: object) -> list[dict[str, object]]:
        raise RuntimeError("search::rrf unavailable")


@pytest.mark.asyncio
async def test_surreal_rrf_backend_uses_database_fusion_scores() -> None:
    plan = build_native_context_retrieval_plan(
        query="surreal rrf",
        organization_id="org-123",
        facets=[ContextFacet.ACTIVE_WORK],
        facet_types={ContextFacet.ACTIVE_WORK: ["task"]},
        principal_id="user-123",
        project="project_123",
        accessible_projects={"project_123"},
    )
    lexical = NativeRetrievalCandidate(
        id="lexical",
        type="task",
        name="Lexical",
        content="A lexical-only result.",
        score=0.9,
        source=None,
        metadata={},
        project_id="project_123",
    )
    shared = NativeRetrievalCandidate(
        id="shared",
        type="task",
        name="Shared",
        content="A result in both lists.",
        score=0.8,
        source=None,
        metadata={},
        project_id="project_123",
    )
    client = _RrfClient()

    ranked = await native_module._fuse_candidates_for_plan(
        client=client,
        source_lists=[
            (NativeRetrievalSignal.NODE_FULLTEXT, [lexical, shared]),
            (NativeRetrievalSignal.NODE_VECTOR, [shared]),
        ],
        plan=plan,
        limit=2,
        fusion_backend=NativeFusionBackend.SURREAL_RRF,
    )

    assert [candidate.id for candidate, _, _ in ranked] == ["shared", "lexical"]
    query, params = client.calls[0]
    assert "search::rrf($lists, $limit, $k)" in query
    assert params["limit"] == 2
    assert params["k"] == 60
    assert ranked[0][2]["fusion_backend"] == "surreal_rrf"
    assert ranked[0][2]["ranks"] == {"node_fulltext": 2, "node_vector": 1}


@pytest.mark.asyncio
async def test_surreal_rrf_backend_falls_back_to_python_rrf_on_error() -> None:
    plan = build_native_context_retrieval_plan(
        query="surreal rrf fallback",
        organization_id="org-123",
        facets=[ContextFacet.ACTIVE_WORK],
        facet_types={ContextFacet.ACTIVE_WORK: ["task"]},
        principal_id="user-123",
        project=None,
        accessible_projects=None,
    )
    candidate = NativeRetrievalCandidate(
        id="candidate",
        type="task",
        name="Candidate",
        content="Fallback result.",
        score=0.9,
        source=None,
        metadata={},
    )

    ranked = await native_module._fuse_candidates_for_plan(
        client=_FailingRrfClient(),
        source_lists=[(NativeRetrievalSignal.NODE_FULLTEXT, [candidate])],
        plan=plan,
        limit=1,
        fusion_backend=NativeFusionBackend.SURREAL_RRF,
    )

    assert ranked[0][0].id == "candidate"
    assert ranked[0][2]["fusion_backend"] == "python_rrf"


@pytest.mark.asyncio
async def test_deterministic_native_embedding_provider_batches_stably() -> None:
    metadata = NativeEmbeddingMetadata(
        provider="deterministic",
        model="unit-test",
        dimensions=4,
        cache_namespace="retrieval-test",
        tokenizer_estimate_method="utf8-byte-length",
    )
    provider = DeterministicNativeEmbeddingProvider(metadata)

    first, second = await provider.embed_texts(["alpha", "alpha"], input_kind="query")

    assert first == second
    assert len(first) == 4
    assert provider.metadata.to_dict() == {
        "provider": "deterministic",
        "model": "unit-test",
        "dimensions": 4,
        "cache_namespace": "retrieval-test",
        "tokenizer_estimate_method": "utf8-byte-length",
        "text_version": "native-graph-v1",
        "normalize": True,
        "input_kind_sensitive": True,
    }


class _VectorClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def execute_query(self, query: str, **params: object) -> list[dict[str, object]]:
        self.calls.append((query, params))
        if "FROM entity" in query and "name_embedding" in query:
            return [
                {
                    "uuid": "task-vector",
                    "name": "Vector Task",
                    "entity_type": "task",
                    "content": "native vector task",
                    "project_id": "project_123",
                    "attributes": {},
                    "created_at": None,
                    "score": 0.81,
                }
            ]
        if "FROM relates_to" in query and "fact_embedding" in query:
            return [
                {
                    "uuid": "edge-vector",
                    "name": "RELATED_TO",
                    "fact": "native vector relationship",
                    "group_id": "org-123",
                    "episodes": [],
                    "attributes": {"project_id": "project_123"},
                    "created_at": None,
                    "expired_at": None,
                    "valid_at": None,
                    "invalid_at": None,
                    "source_node_uuid": "task-vector",
                    "target_node_uuid": "pattern-vector",
                    "score": 0.72,
                }
            ]
        return []


@pytest.mark.asyncio
async def test_vector_candidate_sources_use_native_embedding_contract() -> None:
    plan = build_native_context_retrieval_plan(
        query="native vectors",
        organization_id="org-123",
        facets=[ContextFacet.ACTIVE_WORK],
        facet_types={ContextFacet.ACTIVE_WORK: ["task"]},
        principal_id="user-123",
        project="project_123",
        accessible_projects={"project_123"},
    )
    provider = DeterministicNativeEmbeddingProvider(
        NativeEmbeddingMetadata(
            provider="deterministic",
            model="unit-test",
            dimensions=4,
            cache_namespace="retrieval-test",
            tokenizer_estimate_method="utf8-byte-length",
        )
    )
    client = _VectorClient()

    node_candidates, edge_candidates = await native_module._vector_candidate_sources(
        client=client,
        plan=plan,
        search_filter=native_module.NativeSearchFilter(project_ids=("project_123",)),
        embedding_provider=provider,
    )

    assert [candidate.id for candidate in node_candidates] == ["task-vector"]
    assert [candidate.id for candidate in edge_candidates] == ["edge-vector"]
    assert node_candidates[0].score == 0.81
    assert edge_candidates[0].score == 0.72
    assert node_candidates[0].metadata["embedding_metadata"] == provider.metadata.to_dict()
    assert edge_candidates[0].metadata["embedding_metadata"] == provider.metadata.to_dict()
    node_query, node_params = next(call for call in client.calls if "name_embedding" in call[0])
    edge_query, edge_params = next(call for call in client.calls if "fact_embedding" in call[0])
    assert "name_embedding <|8, 40|> $query_embedding" in node_query
    assert "fact_embedding <|8, 40|> $query_embedding" in edge_query
    assert len(node_params["query_embedding"]) == 4
    assert len(edge_params["query_embedding"]) == 4
    assert node_params["project_ids"] == ["project_123"]
    assert edge_params["project_ids"] == ["project_123"]


@pytest.mark.asyncio
async def test_vector_candidate_sources_use_configured_knn_effort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(native_module.core_config, "graph_knn_ef", 96)
    plan = build_native_context_retrieval_plan(
        query="native vectors",
        organization_id="org-123",
        facets=[ContextFacet.ACTIVE_WORK],
        facet_types={ContextFacet.ACTIVE_WORK: ["task"]},
        principal_id="user-123",
        project="project_123",
        accessible_projects={"project_123"},
    )
    provider = DeterministicNativeEmbeddingProvider(
        NativeEmbeddingMetadata(
            provider="deterministic",
            model="unit-test",
            dimensions=4,
            cache_namespace="retrieval-test",
            tokenizer_estimate_method="utf8-byte-length",
        )
    )
    client = _VectorClient()

    await native_module._vector_candidate_sources(
        client=client,
        plan=plan,
        search_filter=native_module.NativeSearchFilter(project_ids=("project_123",)),
        embedding_provider=provider,
    )

    node_query = next(call for call in client.calls if "name_embedding" in call[0])[0]
    edge_query = next(call for call in client.calls if "fact_embedding" in call[0])[0]
    assert "name_embedding <|8, 96|> $query_embedding" in node_query
    assert "fact_embedding <|8, 96|> $query_embedding" in edge_query


def test_node_record_candidates_keep_top_level_provenance_metadata() -> None:
    candidate = native_module._candidate_from_node_record(
        {
            "uuid": "procedure-1",
            "name": "Procedure",
            "entity_type": "procedure",
            "content": "Use native Surreal rows.",
            "project_id": "project_123",
            "source_id": "raw_1",
            "source_ids": ["raw_1", "raw_2"],
            "confidence": 0.91,
            "valid_at": "2026-05-13T12:00:00+00:00",
            "valid_from": "2026-05-13T12:00:00+00:00",
            "valid_to": "2026-05-14T12:00:00+00:00",
            "invalid_at": None,
            "created_by": "stef",
            "modified_by": "nova",
            "attributes": {},
        },
        signal=NativeRetrievalSignal.NODE_FULLTEXT,
        score=0.8,
    )

    assert candidate.source == "raw_1"
    assert candidate.project_id == "project_123"
    assert candidate.metadata["source_id"] == "raw_1"
    assert candidate.metadata["source_ids"] == ["raw_1", "raw_2"]
    assert candidate.metadata["confidence"] == 0.91
    assert candidate.metadata["valid_at"] == "2026-05-13T12:00:00+00:00"
    assert candidate.metadata["valid_from"] == "2026-05-13T12:00:00+00:00"
    assert candidate.metadata["valid_to"] == "2026-05-14T12:00:00+00:00"
    assert candidate.metadata["created_by"] == "stef"
    assert candidate.metadata["modified_by"] == "nova"


def test_edge_record_candidates_keep_top_level_temporal_metadata() -> None:
    candidate = native_module._candidate_from_edge_record(
        {
            "uuid": "rel-1",
            "name": "SUPPORTS",
            "fact": "Task supports the plan.",
            "group_id": "org-123",
            "project_id": "project_123",
            "source_ids": ["raw_1"],
            "confidence": 0.88,
            "valid_at": "2026-05-13T12:00:00+00:00",
            "valid_to": "2026-05-14T12:00:00+00:00",
            "invalid_at": "2026-05-15T12:00:00+00:00",
            "expired_at": "2026-05-16T12:00:00+00:00",
            "created_by": "stef",
            "modified_by": "nova",
            "episodes": ["episode_1"],
            "source_node_uuid": "task-1",
            "target_node_uuid": "project-1",
            "attributes": {"source_id": "raw_1"},
        },
        signal=NativeRetrievalSignal.EDGE_FULLTEXT,
        score=0.9,
    )

    assert candidate.project_id == "project_123"
    assert candidate.metadata["source_id"] == "raw_1"
    assert candidate.metadata["source_ids"] == ["raw_1"]
    assert candidate.metadata["confidence"] == 0.88
    assert candidate.metadata["valid_at"] == "2026-05-13T12:00:00+00:00"
    assert candidate.metadata["valid_to"] == "2026-05-14T12:00:00+00:00"
    assert candidate.metadata["invalid_at"] == "2026-05-15T12:00:00+00:00"
    assert candidate.metadata["expired_at"] == "2026-05-16T12:00:00+00:00"
    assert candidate.metadata["created_by"] == "stef"
    assert candidate.metadata["modified_by"] == "nova"
    assert candidate.metadata["episodes"] == ["episode_1"]
    assert candidate.metadata["source_node_uuid"] == "task-1"
    assert candidate.metadata["target_node_uuid"] == "project-1"


@pytest.mark.asyncio
async def test_raw_candidates_sort_by_relevance_across_scopes() -> None:
    plan = build_native_context_retrieval_plan(
        query="What should Nova recall from the diary for delegated handoff? sibyl",
        organization_id="org-123",
        facets=[ContextFacet.RECENT_MEMORY],
        facet_types={ContextFacet.RECENT_MEMORY: ["session", "episode", "note"]},
        principal_id="user-123",
        project=None,
        accessible_projects=None,
        agent_id="nova",
        limit=8,
    )

    async def fake_recall(**kwargs: object) -> list[RawMemory]:
        if kwargs.get("agent_id") == "nova":
            return [
                RawMemory(
                    id="diary-1",
                    organization_id="org-123",
                    source_id="baseline:agent-diary",
                    principal_id="user-123",
                    agent_id="nova",
                    title="Nova Baseline Diary",
                    raw_content="Nova diary says checkpoint Neon Thread for delegated handoff.",
                    capture_surface="agent_diary",
                    metadata={"agent_id": "nova", "memory_kind": "agent_diary"},
                    score=0.7,
                )
            ]
        return [
            RawMemory(
                id="private-1",
                organization_id="org-123",
                source_id="baseline:delegated-recall",
                principal_id="user-123",
                title="Delegated Recall Baseline",
                raw_content="Delegated recall baseline says Obsidian Spire covers storage.",
                score=0.3,
            ),
            RawMemory(
                id="private-2",
                organization_id="org-123",
                source_id="baseline:personal-memory",
                principal_id="user-123",
                title="Personal Baseline Memory",
                raw_content="Personal baseline memory says remember Amethyst Loom.",
                score=0.2,
            ),
        ]

    candidates = await native_module._recall_raw_candidates(
        plan=plan,
        facet=ContextFacet.RECENT_MEMORY,
        requested_types={"session", "episode", "note"},
        limit=2,
        recall_fn=fake_recall,
    )

    assert [candidate.id for candidate in candidates] == [
        "raw_memory:diary-1",
        "raw_memory:private-1",
        "raw_memory:private-2",
    ]


@pytest.mark.asyncio
async def test_raw_candidates_filter_lifecycle_hidden_memory() -> None:
    plan = build_native_context_retrieval_plan(
        query="privacy",
        organization_id="org-123",
        facets=[ContextFacet.RECENT_MEMORY],
        facet_types={ContextFacet.RECENT_MEMORY: ["session", "episode", "note"]},
        principal_id="user-123",
        project=None,
        accessible_projects=None,
        limit=8,
    )

    async def fake_recall(**_: object) -> list[RawMemory]:
        return [
            RawMemory(
                id="hidden-1",
                organization_id="org-123",
                source_id="hidden",
                principal_id="user-123",
                title="Hidden memory",
                raw_content="privacy hidden",
                review_state="hidden",
                score=0.9,
            ),
            RawMemory(
                id="visible-1",
                organization_id="org-123",
                source_id="visible",
                principal_id="user-123",
                title="Visible memory",
                raw_content="privacy visible",
                score=0.5,
            ),
            RawMemory(
                id="superseded-1",
                organization_id="org-123",
                source_id="superseded",
                principal_id="user-123",
                title="Superseded memory",
                raw_content="privacy old",
                metadata={"lifecycle_state": "superseded"},
                score=0.4,
            ),
        ]

    candidates = await native_module._recall_raw_candidates(
        plan=plan,
        facet=ContextFacet.RECENT_MEMORY,
        requested_types={"session", "episode", "note"},
        limit=5,
        recall_fn=fake_recall,
    )

    assert [candidate.id for candidate in candidates] == ["raw_memory:visible-1"]


class _EdgeFulltextClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def execute_query(self, query: str, **params: object) -> list[dict[str, object]]:
        self.calls.append((query, params))
        if "fact @0@" in query:
            return [
                {"uuid": "edge-1", "score": 0.9},
                {"uuid": "edge-drop", "score": 0.8},
                {"uuid": "edge-2", "score": 0.7},
                {"uuid": "edge-3", "score": 0.6},
                {"uuid": "edge-4", "score": 0.5},
            ]
        return [
            {
                "uuid": "edge-4",
                "name": "RELATES_TO",
                "fact": "Later match",
                "group_id": "org-123",
                "episodes": [],
                "attributes": {"project_id": "project_123"},
                "created_at": None,
                "expired_at": None,
                "valid_at": None,
                "invalid_at": None,
                "source_node_uuid": "task-4",
                "target_node_uuid": "pattern-4",
            },
            {
                "uuid": "edge-1",
                "name": "RELATES_TO",
                "fact": "Surreal planner warning",
                "group_id": "org-123",
                "episodes": [],
                "attributes": {"project_id": "project_123"},
                "created_at": None,
                "expired_at": None,
                "valid_at": None,
                "invalid_at": None,
                "source_node_uuid": "task-1",
                "target_node_uuid": "pattern-1",
            },
            {
                "uuid": "edge-3",
                "name": "RELATES_TO",
                "fact": "Third match",
                "group_id": "org-123",
                "episodes": [],
                "attributes": {"project_id": "project_123"},
                "created_at": None,
                "expired_at": None,
                "valid_at": None,
                "invalid_at": None,
                "source_node_uuid": "task-3",
                "target_node_uuid": "pattern-3",
            },
            {
                "uuid": "edge-2",
                "name": "RELATES_TO",
                "fact": "Second match",
                "group_id": "org-123",
                "episodes": [],
                "attributes": {"project_id": "project_123"},
                "created_at": None,
                "expired_at": None,
                "valid_at": None,
                "invalid_at": None,
                "source_node_uuid": "task-2",
                "target_node_uuid": "pattern-2",
            },
        ]


@pytest.mark.asyncio
async def test_edge_fulltext_splits_matches_from_relation_hydration() -> None:
    plan = build_native_context_retrieval_plan(
        query="surreal planner warning",
        organization_id="org-123",
        facets=[ContextFacet.ACTIVE_WORK],
        facet_types={ContextFacet.ACTIVE_WORK: ["task"]},
        principal_id="user-123",
        project="project_123",
        accessible_projects={"project_123"},
    )
    client = _EdgeFulltextClient()

    candidates = await native_module._edge_fulltext_candidates(
        client=client,
        plan=plan,
        search_filter=native_module.NativeSearchFilter(
            project_ids=("project_123",),
            edge_uuids=("edge-1", "edge-2", "edge-3", "edge-4"),
            edge_types=("RELATES_TO",),
        ),
        limit=3,
    )

    assert [candidate.id for candidate in candidates] == ["edge-1", "edge-2", "edge-3"]
    assert [candidate.score for candidate in candidates] == [0.9, 0.7, 0.6]
    match_query = client.calls[0][0]
    hydrate_query = client.calls[1][0]
    assert "fact @0@" in match_query
    assert "attributes.project_id IN $project_ids" in match_query
    assert "in.project_id IN $project_ids" in match_query
    assert "out.project_id IN $project_ids" in match_query
    assert "uuid IN $edge_uuids" in match_query
    assert "name IN $edge_types" in match_query
    assert client.calls[0][1]["match_limit"] == 32
    assert client.calls[0][1]["project_ids"] == ["project_123"]
    assert "fact @0@" not in hydrate_query
    assert "uuid IN $match_uuids" in hydrate_query
