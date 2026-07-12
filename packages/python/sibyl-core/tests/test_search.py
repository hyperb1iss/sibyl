from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

import sibyl_core.embeddings.providers as embedding_providers
import sibyl_core.retrieval.hybrid as hybrid_module
import sibyl_core.retrieval.query_ranking as query_ranking_module
import sibyl_core.retrieval.search as search_module
from sibyl_core.auth.memory_policy import memory_scope_policy_key
from sibyl_core.embeddings.providers import (
    CachedEmbeddingProvider,
    DeterministicEmbeddingProvider,
    EmbeddingMetadata,
    OpenAIEmbeddingProvider,
    capture_embedding_usage,
    configured_embedding_provider,
    create_embedding_provider,
    embedding_usage_delta,
    embedding_usage_snapshot,
)
from sibyl_core.memory_pipeline.retrieval import CandidateSourceResult
from sibyl_core.models.context import ContextFacet
from sibyl_core.retrieval.candidates import CandidateKind, CandidateScope
from sibyl_core.retrieval.search import (
    DEFAULT_FILTER_SELECTIVITY_THRESHOLD,
    FusionBackend,
    RetrievalCandidate,
    RetrievalSignal,
    VectorCandidateFetch,
    build_context_retrieval_plan,
    coerce_fusion_backend,
    fusion_backend_from_env,
)
from sibyl_core.services.surreal_content import MemoryScope, RawMemory, RawMemoryRecallResult


class _FakeSentenceTransformer:
    def __init__(self, *, dimensions: int = 3) -> None:
        self.dimensions = dimensions
        self.requests: list[tuple[list[str], dict[str, object]]] = []

    def get_sentence_embedding_dimension(self) -> int:
        return self.dimensions

    def encode(self, texts: list[str], **kwargs: object) -> list[list[float]]:
        self.requests.append((list(texts), dict(kwargs)))
        return [
            [float(index + 1), 0.0, 0.0][: self.dimensions] for index, _text in enumerate(texts)
        ]


@pytest.mark.asyncio
async def test_openai_embedding_provider_records_response_usage() -> None:
    class Embeddings:
        async def create(self, **kwargs: object) -> SimpleNamespace:
            inputs = kwargs["input"]
            assert isinstance(inputs, list)
            return SimpleNamespace(
                data=[SimpleNamespace(embedding=[0.1, 0.2]) for _ in inputs],
                usage=SimpleNamespace(prompt_tokens=7, total_tokens=7, cost=0.0001),
            )

    metadata = EmbeddingMetadata(
        provider="openai",
        model="text-embedding-test",
        dimensions=2,
        cache_namespace="usage-test",
        tokenizer_estimate_method="test",
    )
    provider = CachedEmbeddingProvider(
        OpenAIEmbeddingProvider(
            metadata=metadata,
            client=SimpleNamespace(embeddings=Embeddings()),
        )
    )
    before = embedding_usage_snapshot(provider)
    with capture_embedding_usage(provider) as captured:
        assert await provider.embed_texts(["alpha", "beta"]) == [[0.1, 0.2], [0.1, 0.2]]

    expected_usage = {
        "provider": "openai",
        "model": "text-embedding-test",
        "requests": 1,
        "inputs": 2,
        "prompt_tokens": 7,
        "total_tokens": 7,
        "cost_reported_requests": 1,
        "cost_usd": 0.0001,
    }
    assert captured == expected_usage
    assert embedding_usage_delta(before, embedding_usage_snapshot(provider)) == expected_usage


@pytest.mark.asyncio
async def test_local_sentence_transformer_provider_embeds_with_metadata() -> None:
    client = _FakeSentenceTransformer(dimensions=3)
    provider = create_embedding_provider(
        provider="local",
        model="sentence-transformers/all-MiniLM-L6-v2",
        dimensions=3,
        cache_namespace="graph-local-test",
        client=client,
    )

    embeddings = await provider.embed_texts(["  alpha  ", ""], input_kind="query")

    assert provider.metadata.provider == "local"
    assert provider.metadata.cache_namespace == "graph-local-test"
    assert provider.metadata.tokenizer_estimate_method == "sentence-transformers"
    assert provider.metadata.input_kind_sensitive is False
    assert embeddings == [[1.0, 0.0, 0.0], [2.0, 0.0, 0.0]]
    assert client.requests == [
        (
            ["alpha", "[empty]"],
            {
                "normalize_embeddings": True,
                "convert_to_numpy": True,
                "show_progress_bar": False,
            },
        )
    ]


def test_local_sentence_transformer_provider_rejects_dimension_mismatch() -> None:
    with pytest.raises(ValueError, match="produces 2 dimensions"):
        create_embedding_provider(
            provider="local",
            model="sentence-transformers/all-MiniLM-L6-v2",
            dimensions=3,
            cache_namespace="graph-local-test",
            client=_FakeSentenceTransformer(dimensions=2),
        )


def test_local_sentence_transformer_provider_requires_optional_dependency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(embedding_providers.importlib.util, "find_spec", lambda _name: None)

    with pytest.raises(RuntimeError, match="requires the optional"):
        create_embedding_provider(
            provider="local",
            model="sentence-transformers/all-MiniLM-L6-v2",
            dimensions=384,
            cache_namespace="graph-local-test",
        )


def test_configured_local_embedding_provider_uses_minilm_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SIBYL_MOCK_LLM", raising=False)
    monkeypatch.setenv("SIBYL_GRAPH_EMBEDDING_PROVIDER", "local")
    monkeypatch.delenv("SIBYL_GRAPH_EMBEDDING_MODEL", raising=False)
    monkeypatch.delenv("SIBYL_GRAPH_EMBEDDING_DIMENSIONS", raising=False)
    monkeypatch.setattr(embedding_providers.importlib.util, "find_spec", lambda _name: object())

    provider = configured_embedding_provider()

    assert provider is not None
    assert provider.metadata.provider == "local"
    assert provider.metadata.model == "sentence-transformers/all-MiniLM-L6-v2"
    assert provider.metadata.dimensions == 384
    assert provider.metadata.cache_namespace == "graph"


def test_configured_local_embedding_provider_uses_model_dimensions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SIBYL_MOCK_LLM", raising=False)
    monkeypatch.setenv("SIBYL_GRAPH_EMBEDDING_PROVIDER", "local")
    monkeypatch.setenv("SIBYL_GRAPH_EMBEDDING_MODEL", "BAAI/bge-m3")
    monkeypatch.delenv("SIBYL_GRAPH_EMBEDDING_DIMENSIONS", raising=False)
    monkeypatch.setattr(embedding_providers.importlib.util, "find_spec", lambda _name: object())

    provider = configured_embedding_provider()

    assert provider is not None
    assert provider.metadata.model == "BAAI/bge-m3"
    assert provider.metadata.dimensions == 1024


def test_configured_local_embedding_provider_disables_without_dependency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SIBYL_MOCK_LLM", raising=False)
    monkeypatch.setenv("SIBYL_GRAPH_EMBEDDING_PROVIDER", "local")
    monkeypatch.delenv("SIBYL_GRAPH_EMBEDDING_MODEL", raising=False)
    monkeypatch.delenv("SIBYL_GRAPH_EMBEDDING_DIMENSIONS", raising=False)
    monkeypatch.setattr(embedding_providers.importlib.util, "find_spec", lambda _name: None)

    assert configured_embedding_provider() is None


def test_fusion_backend_defaults_to_python_rrf() -> None:
    assert coerce_fusion_backend(None) is FusionBackend.PYTHON_RRF
    assert coerce_fusion_backend("") is FusionBackend.PYTHON_RRF
    assert coerce_fusion_backend("invalid") is FusionBackend.PYTHON_RRF
    assert fusion_backend_from_env({}) is FusionBackend.PYTHON_RRF


def test_fusion_backend_accepts_surreal_rrf() -> None:
    assert coerce_fusion_backend("surreal_rrf") is FusionBackend.SURREAL_RRF
    assert coerce_fusion_backend("SURREAL_RRF") is FusionBackend.SURREAL_RRF
    assert fusion_backend_from_env({"SIBYL_FUSION_BACKEND": "surreal_rrf"}) is (
        FusionBackend.SURREAL_RRF
    )


@pytest.mark.asyncio
async def test_read_only_graph_runtime_supports_legacy_runtime_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Runtime:
        client = object()

    calls: list[str] = []

    async def fake_runtime(organization_id: str) -> Runtime:
        calls.append(organization_id)
        return Runtime()

    monkeypatch.setattr(search_module, "get_surreal_graph_runtime", fake_runtime)

    runtime = await search_module._get_read_only_graph_runtime("org-123")

    assert isinstance(runtime, Runtime)
    assert calls == ["org-123"]


def test_build_context_retrieval_plan_records_scopes_and_weights() -> None:
    plan = build_context_retrieval_plan(
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
    assert plan.weights.graph_native_signal_boost_cap == 1.2
    assert plan.weights.freshness_boost_cap == 1.5
    assert plan.filter_selectivity_threshold == DEFAULT_FILTER_SELECTIVITY_THRESHOLD
    assert RetrievalSignal.RAW_LEXICAL in plan.signals
    assert RetrievalSignal.GRAPH_EXPANSION in plan.signals
    assert plan.filter_selectivity == 1.0


def test_search_filter_for_plan_carries_requested_entity_types() -> None:
    plan = build_context_retrieval_plan(
        query="task context",
        organization_id="org-123",
        facets=[ContextFacet.ACTIVE_WORK],
        facet_types={ContextFacet.ACTIVE_WORK: ["task"]},
        principal_id="user-123",
        project="project_123",
        accessible_projects={"project_123"},
        limit=6,
    )

    search_filter = search_module._search_filter_for_plan(
        plan,
        requested_types={"task", "epic"},
    )

    assert search_filter.node_types == ("epic", "task")
    assert search_filter.project_ids == ("project_123",)


def test_build_context_retrieval_plan_denies_unverified_project_scope() -> None:
    plan = build_context_retrieval_plan(
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
    assert search_module._search_filter_for_plan(plan).project_ids == ()
    assert not search_module._candidate_allowed(
        RetrievalCandidate(
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


def _scoped_plan(allowed_memory_scope_keys: set[str] | None) -> search_module.RetrievalPlan:
    return build_context_retrieval_plan(
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


def test_build_context_retrieval_plan_skips_scope_filter_when_unset() -> None:
    plan = _scoped_plan(None)

    assert [scope.memory_scope for scope in plan.scopes] == [
        MemoryScope.PRIVATE,
        MemoryScope.PROJECT,
        MemoryScope.PRIVATE,
    ]
    assert not [d for d in plan.denied_scopes if d.reason == "api_key_scope_excluded"]


def test_build_context_retrieval_plan_keeps_scopes_within_api_key_grants() -> None:
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


def test_build_context_retrieval_plan_excludes_scopes_outside_api_key_grants() -> None:
    plan = _scoped_plan({memory_scope_policy_key(MemoryScope.PRIVATE, "user-123")})

    assert [scope.memory_scope for scope in plan.scopes] == [
        MemoryScope.PRIVATE,
        MemoryScope.PRIVATE,
    ]
    excluded = [d for d in plan.denied_scopes if d.reason == "api_key_scope_excluded"]
    assert [d.memory_scope for d in excluded] == [MemoryScope.PROJECT]
    assert excluded[0].scope_key == "project_123"
    assert search_module._search_filter_for_plan(plan).project_ids == ()


def test_build_context_retrieval_plan_excludes_all_scopes_when_no_grant_matches() -> None:
    plan = _scoped_plan({memory_scope_policy_key(MemoryScope.PROJECT, "project_other")})

    assert plan.scopes == ()
    assert {d.reason for d in plan.denied_scopes} == {"api_key_scope_excluded"}


def test_build_context_retrieval_plan_trims_accessible_projects_to_api_key_grants() -> None:
    plan = build_context_retrieval_plan(
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
    assert search_module._authorized_project_ids(plan) == ()


def test_build_context_retrieval_plan_keeps_granted_accessible_projects() -> None:
    plan = build_context_retrieval_plan(
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
    assert search_module._authorized_project_ids(plan) == ("project_123",)
    assert [
        (scope.memory_scope, scope.scope_key)
        for scope in plan.scopes
        if scope.memory_scope is MemoryScope.PROJECT
    ] == [(MemoryScope.PROJECT, "project_123")]


def test_build_context_retrieval_plan_adds_granted_unscoped_project_scope() -> None:
    plan = build_context_retrieval_plan(
        query="unscoped pack",
        organization_id="org-123",
        facets=[ContextFacet.RECENT_MEMORY],
        facet_types={ContextFacet.RECENT_MEMORY: ["raw_memory", "episode", "note"]},
        principal_id="user-123",
        project=None,
        accessible_projects={"project_123", "project_456"},
        allowed_memory_scope_keys={memory_scope_policy_key(MemoryScope.PROJECT, "project_123")},
    )

    assert [
        (scope.memory_scope, scope.scope_key)
        for scope in plan.scopes
        if scope.memory_scope is MemoryScope.PROJECT
    ] == [(MemoryScope.PROJECT, "project_123")]
    assert MemoryScope.PRIVATE not in [scope.memory_scope for scope in plan.scopes]
    excluded = [d for d in plan.denied_scopes if d.reason == "api_key_scope_excluded"]
    assert {(d.memory_scope, d.scope_key) for d in excluded} == {
        (MemoryScope.PRIVATE, None),
        (MemoryScope.PROJECT, "project_456"),
    }


def test_build_context_retrieval_plan_keeps_all_accessible_projects_when_unscoped() -> None:
    plan = build_context_retrieval_plan(
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


def test_retrieval_candidate_contract_metadata_preserves_scope_and_signals() -> None:
    candidate = RetrievalCandidate(
        id="task-123",
        type="task",
        name="Scoped task",
        content="Task content",
        score=1.0,
        source="task-123",
        metadata={"entity_type": "task"},
        kind=CandidateKind.NODE,
        retrieval_signals=(RetrievalSignal.NODE_VECTOR.value,),
        scope=CandidateScope(
            organization_id="org-123",
            project_id="project-123",
            memory_scope="project",
            scope_key="project-123",
            principal_id="user-123",
            visibility="project",
            policy_reason="project_access_verified",
        ),
    )

    metadata = candidate.contract_metadata()

    assert metadata["candidate_kind"] == "node"
    assert metadata["retrieval_signals"] == ["node_vector"]
    assert metadata["candidate_organization_id"] == "org-123"
    assert metadata["candidate_project_id"] == "project-123"
    assert metadata["candidate_memory_scope"] == "project"
    assert metadata["candidate_policy_reason"] == "project_access_verified"


def test_build_context_retrieval_plan_includes_project_less_agent_diary_scope() -> None:
    """An unscoped agent pack must query the project-less diary scope too.

    Agent diaries default to the project-less private scope (that is how
    /memory/raw stores them and how /memory/raw/recall finds them). Scoping
    the diary read to accessible projects only hid project-less diaries from
    context packs whenever the principal had any accessible project.
    """

    plan = build_context_retrieval_plan(
        query="unscoped agent diary",
        organization_id="org-123",
        facets=[ContextFacet.RECENT_MEMORY],
        facet_types={ContextFacet.RECENT_MEMORY: ["session", "episode", "note"]},
        principal_id="user-123",
        project=None,
        accessible_projects={"project_123", "project_456"},
        agent_id="nova",
    )

    diary_scopes = [
        scope
        for scope in plan.scopes
        if scope.memory_scope is MemoryScope.PRIVATE and scope.agent_id == "nova"
    ]
    assert {scope.project_id for scope in diary_scopes} == {None, "project_123", "project_456"}


def test_candidate_from_raw_memory_uses_top_level_project_id_when_metadata_missing() -> None:
    candidate = search_module._candidate_from_raw_memory(
        RawMemory(
            id="raw-1",
            organization_id="org-123",
            source_id="agent-diary",
            principal_id="user-123",
            project_id="project_999",
            title="Diary memory",
            raw_content="secret",
            memory_scope=MemoryScope.PRIVATE,
            metadata={"agent_id": "nova"},
        ),
        scope=search_module.ScopeSpec(
            memory_scope=MemoryScope.PRIVATE,
            principal_id="user-123",
            scope_key=None,
            policy_reason="agent_diary_private_read_allowed",
        ),
    )

    assert candidate.project_id == "project_999"


def test_candidate_allowed_denies_private_candidate_without_private_grant() -> None:
    plan = build_context_retrieval_plan(
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
    assert not search_module._candidate_allowed(
        RetrievalCandidate(
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
    plan = build_context_retrieval_plan(
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

    assert search_module._candidate_allowed(
        RetrievalCandidate(
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
    plan = build_context_retrieval_plan(
        query="private memory",
        organization_id="org-123",
        facets=[ContextFacet.RECENT_MEMORY],
        facet_types={ContextFacet.RECENT_MEMORY: ["episode", "note"]},
        principal_id="bob",
        project=None,
        accessible_projects=None,
        agent_id=None,
    )

    assert not search_module._candidate_allowed(
        RetrievalCandidate(
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
    plan = build_context_retrieval_plan(
        query="private memory",
        organization_id="org-123",
        facets=[ContextFacet.RECENT_MEMORY],
        facet_types={ContextFacet.RECENT_MEMORY: ["episode", "note"]},
        principal_id="bob",
        project=None,
        accessible_projects=None,
        agent_id=None,
    )

    assert search_module._candidate_allowed(
        RetrievalCandidate(
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


def test_build_context_retrieval_plan_requires_principal() -> None:
    plan = build_context_retrieval_plan(
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
    plan = build_context_retrieval_plan(
        query="edge permissions",
        organization_id="org-123",
        facets=[ContextFacet.ACTIVE_WORK],
        facet_types={ContextFacet.ACTIVE_WORK: ["claim"]},
        principal_id="user-123",
        project="project_A",
        accessible_projects={"project_A"},
    )
    candidate = RetrievalCandidate(
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

    assert not search_module._candidate_allowed(
        candidate,
        plan=plan,
        requested_types=set(),
        facet=None,
    )


def test_candidate_allowed_accepts_claim_edge_when_both_endpoints_accessible() -> None:
    plan = build_context_retrieval_plan(
        query="edge permissions",
        organization_id="org-123",
        facets=[ContextFacet.ACTIVE_WORK],
        facet_types={ContextFacet.ACTIVE_WORK: ["claim"]},
        principal_id="user-123",
        project="project_A",
        accessible_projects={"project_A"},
    )
    candidate = RetrievalCandidate(
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

    assert search_module._candidate_allowed(
        candidate,
        plan=plan,
        requested_types=set(),
        facet=None,
    )


def test_candidate_allowed_treats_relationship_as_edge_claim_alias() -> None:
    plan = build_context_retrieval_plan(
        query="edge permissions",
        organization_id="org-123",
        facets=[ContextFacet.DOMAIN],
        facet_types={ContextFacet.DOMAIN: ["relationship"]},
        principal_id="user-123",
        project="project_A",
        accessible_projects={"project_A"},
    )
    candidate = RetrievalCandidate(
        id="edge-1",
        type="claim",
        name="In-project relationship",
        content="Safe relationship",
        score=1.0,
        source=None,
        metadata={
            "relationship": "RELATED_TO",
            "source_node_project_id": "project_A",
            "target_node_project_id": "project_A",
        },
        project_id=None,
    )

    assert search_module._candidate_allowed(
        candidate,
        plan=plan,
        requested_types={"relationship"},
        facet=ContextFacet.DOMAIN,
    )


def test_plan_estimates_project_filter_selectivity() -> None:
    accessible_projects = {f"project_{index}" for index in range(20)}
    plan = build_context_retrieval_plan(
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
    plan = build_context_retrieval_plan(
        query="selective vector filter",
        organization_id="org-123",
        facets=[ContextFacet.ACTIVE_WORK],
        facet_types={ContextFacet.ACTIVE_WORK: ["task"]},
        principal_id="user-123",
        project="project_0",
        accessible_projects=accessible_projects,
    )
    vector_candidate = RetrievalCandidate(
        id="vector-only",
        type="task",
        name="Vector only",
        content="A weak vector-only match.",
        score=1.0,
        source=None,
        metadata={},
        project_id="project_0",
    )
    lexical_candidate = RetrievalCandidate(
        id="lexical",
        type="task",
        name="Lexical",
        content="A grounded lexical match.",
        score=1.0,
        source=None,
        metadata={},
        project_id="project_0",
    )

    ranked = search_module._fuse_candidates(
        [
            (RetrievalSignal.NODE_VECTOR, [vector_candidate]),
            (RetrievalSignal.NODE_FULLTEXT, [lexical_candidate]),
        ],
        plan=plan,
        limit=2,
    )

    assert [candidate.id for candidate, _, _ in ranked] == ["lexical", "vector-only"]
    assert ranked[1][2]["vector_only_demoted"] is True
    assert ranked[1][2]["filter_selectivity"] == plan.filter_selectivity


def test_vector_matches_with_lexical_signal_do_not_demote() -> None:
    accessible_projects = {f"project_{index}" for index in range(20)}
    plan = build_context_retrieval_plan(
        query="selective vector filter",
        organization_id="org-123",
        facets=[ContextFacet.ACTIVE_WORK],
        facet_types={ContextFacet.ACTIVE_WORK: ["task"]},
        principal_id="user-123",
        project="project_0",
        accessible_projects=accessible_projects,
    )
    candidate = RetrievalCandidate(
        id="shared",
        type="task",
        name="Shared",
        content="A vector hit with lexical corroboration.",
        score=1.0,
        source=None,
        metadata={},
        project_id="project_0",
    )

    ranked = search_module._fuse_candidates(
        [
            (RetrievalSignal.NODE_VECTOR, [candidate]),
            (RetrievalSignal.NODE_FULLTEXT, [candidate]),
        ],
        plan=plan,
        limit=1,
    )

    assert "vector_only_demoted" not in ranked[0][2]


def test_graph_expansion_only_sessions_demote_below_direct_hits() -> None:
    plan = build_context_retrieval_plan(
        query="coffee limit",
        organization_id="org-123",
        facets=[ContextFacet.RECENT_MEMORY],
        facet_types={ContextFacet.RECENT_MEMORY: ["session"]},
        principal_id="user-123",
        project=None,
        accessible_projects=None,
    )
    graph_candidate = RetrievalCandidate(
        id="graph-only",
        type="session",
        name="Graph only",
        content="A session found only through projected memory graph expansion.",
        score=1.0,
        source=None,
        metadata={},
    )
    direct_candidate = RetrievalCandidate(
        id="direct",
        type="session",
        name="Direct",
        content="A direct lexical session hit.",
        score=1.0,
        source=None,
        metadata={},
    )

    ranked = search_module._fuse_candidates(
        [
            (RetrievalSignal.GRAPH_EXPANSION, [graph_candidate]),
            (RetrievalSignal.NODE_FULLTEXT, [direct_candidate]),
        ],
        plan=plan,
        limit=2,
    )

    assert [candidate.id for candidate, _, _ in ranked] == ["direct", "graph-only"]
    assert ranked[1][2]["graph_expansion_only_demoted"] is True
    assert ranked[1][2]["graph_expansion_only_multiplier"] == 0.45


def test_graph_path_metadata_survives_fusion_with_direct_hit() -> None:
    plan = build_context_retrieval_plan(
        query="surreal live query decision",
        organization_id="org-123",
        facets=[ContextFacet.ACTIVE_WORK],
        facet_types={ContextFacet.ACTIVE_WORK: ["task"]},
        principal_id="user-123",
        project="project_123",
        accessible_projects={"project_123"},
    )
    direct_candidate = RetrievalCandidate(
        id="shared",
        type="task",
        name="Shared Task",
        content="Direct lexical task hit.",
        score=1.0,
        source=None,
        metadata={},
        project_id="project_123",
    )
    graph_candidate = RetrievalCandidate(
        id="shared",
        type="task",
        name="Shared Task via path",
        content="Same task reached through the decision graph.",
        score=1.0,
        source=None,
        metadata={
            "graph_expansion_depth": 1,
            "graph_expansion_relationship": "DECIDES",
            "graph_expansion_score": 1.0,
        },
        project_id="project_123",
    )

    [(candidate, score, fusion_metadata)] = search_module._fuse_candidates(
        [
            (RetrievalSignal.NODE_FULLTEXT, [direct_candidate]),
            (RetrievalSignal.GRAPH_EXPANSION, [graph_candidate]),
        ],
        plan=plan,
        limit=1,
    )
    result = search_module._search_result_from_candidate(
        candidate,
        score=score,
        fusion_metadata=fusion_metadata,
        include_content=True,
    )

    assert candidate is direct_candidate
    assert fusion_metadata["graph_expansion_relationship"] == "DECIDES"
    assert fusion_metadata["graph_native_signal_boost"] == pytest.approx(1.2)
    assert result.metadata["graph_expansion_depth"] == 1
    assert result.metadata["graph_expansion_relationship"] == "DECIDES"
    assert result.metadata["graph_expansion_score"] == 1.0
    assert result.metadata["graph_native_signal_boost"] == pytest.approx(1.2)


def test_graph_native_signal_boost_skips_graph_only_hits() -> None:
    plan = build_context_retrieval_plan(
        query="surreal live query decision",
        organization_id="org-123",
        facets=[ContextFacet.ACTIVE_WORK],
        facet_types={ContextFacet.ACTIVE_WORK: ["task"]},
        principal_id="user-123",
        project="project_123",
        accessible_projects={"project_123"},
    )
    graph_candidate = RetrievalCandidate(
        id="graph-only",
        type="task",
        name="Graph-only Task",
        content="Only reached through graph expansion.",
        score=1.0,
        source=None,
        metadata={
            "graph_expansion_depth": 1,
            "graph_expansion_relationship": "DECIDES",
            "graph_expansion_score": 1.0,
        },
        project_id="project_123",
    )

    [(candidate, score, fusion_metadata)] = search_module._fuse_candidates(
        [(RetrievalSignal.GRAPH_EXPANSION, [graph_candidate])],
        plan=plan,
        limit=1,
    )
    result = search_module._search_result_from_candidate(
        candidate,
        score=score,
        fusion_metadata=fusion_metadata,
        include_content=True,
    )

    assert "graph_native_signal_boost" not in fusion_metadata
    assert result.metadata["graph_expansion_only_demoted"] is True
    assert result.metadata["graph_expansion_only_multiplier"] == 0.45
    assert "graph_native_signal_boost" not in result.metadata


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


class _RrfOnlyFailingClient:
    async def execute_query(self, query: str, **_params: object) -> list[dict[str, object]]:
        if "search::rrf" in query:
            raise RuntimeError("search::rrf unavailable")
        return []


class _SurrealErrorEnvelopeClient:
    async def execute_query(self, _query: str, **_params: object) -> list[dict[str, object]]:
        return [{"status": "ERR", "result": "fulltext index unavailable", "time": "1ms"}]


@pytest.mark.asyncio
async def test_surreal_rrf_backend_uses_database_fusion_scores() -> None:
    plan = build_context_retrieval_plan(
        query="surreal rrf",
        organization_id="org-123",
        facets=[ContextFacet.ACTIVE_WORK],
        facet_types={ContextFacet.ACTIVE_WORK: ["task"]},
        principal_id="user-123",
        project="project_123",
        accessible_projects={"project_123"},
    )
    lexical = RetrievalCandidate(
        id="lexical",
        type="task",
        name="Lexical",
        content="A lexical-only result.",
        score=0.9,
        source=None,
        metadata={},
        project_id="project_123",
    )
    shared = RetrievalCandidate(
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

    fusion = await search_module._fuse_candidates_for_plan(
        client=client,
        source_lists=[
            (RetrievalSignal.NODE_FULLTEXT, [lexical, shared]),
            (RetrievalSignal.NODE_VECTOR, [shared]),
        ],
        plan=plan,
        limit=2,
        fusion_backend=FusionBackend.SURREAL_RRF,
    )
    ranked = fusion.candidates

    assert fusion.actual_backend is FusionBackend.SURREAL_RRF
    assert [candidate.id for candidate, _, _ in ranked] == ["shared", "lexical"]
    query, params = client.calls[0]
    assert "search::rrf($lists, $limit, $k)" in query
    assert params["limit"] == 2
    assert params["k"] == 60
    assert ranked[0][2]["fusion_backend"] == "surreal_rrf"
    assert ranked[0][2]["ranks"] == {"node_fulltext": 2, "node_vector": 1}


@pytest.mark.asyncio
async def test_surreal_rrf_backend_falls_back_to_python_rrf_on_error() -> None:
    plan = build_context_retrieval_plan(
        query="surreal rrf fallback",
        organization_id="org-123",
        facets=[ContextFacet.ACTIVE_WORK],
        facet_types={ContextFacet.ACTIVE_WORK: ["task"]},
        principal_id="user-123",
        project=None,
        accessible_projects=None,
    )
    candidate = RetrievalCandidate(
        id="candidate",
        type="task",
        name="Candidate",
        content="Fallback result.",
        score=0.9,
        source=None,
        metadata={},
    )
    failures: list[search_module.CandidateSourceFailure] = []

    fusion = await search_module._fuse_candidates_for_plan(
        client=_FailingRrfClient(),
        source_lists=[(RetrievalSignal.NODE_FULLTEXT, [candidate])],
        plan=plan,
        limit=1,
        fusion_backend=FusionBackend.SURREAL_RRF,
        fusion_failures=failures,
    )
    ranked = fusion.candidates

    assert fusion.actual_backend is FusionBackend.PYTHON_RRF
    assert ranked[0][0].id == "candidate"
    assert ranked[0][2]["fusion_backend"] == "python_rrf"
    assert [failure.as_metadata() for failure in failures] == [
        {"source": "surreal_rrf", "error_type": "RuntimeError"}
    ]


@pytest.mark.asyncio
async def test_surreal_rrf_empty_fallback_reports_actual_backend() -> None:
    plan = build_context_retrieval_plan(
        query="surreal rrf empty fallback",
        organization_id="org-123",
        facets=[ContextFacet.ACTIVE_WORK],
        facet_types={ContextFacet.ACTIVE_WORK: ["task"]},
        principal_id="user-123",
        project=None,
        accessible_projects=None,
    )
    candidate = RetrievalCandidate(
        id="candidate",
        type="task",
        name="Candidate",
        content="Fallback result.",
        score=0.9,
        source=None,
        metadata={},
    )
    failures: list[search_module.CandidateSourceFailure] = []

    fusion = await search_module._fuse_candidates_for_plan(
        client=_FailingRrfClient(),
        source_lists=[(RetrievalSignal.NODE_FULLTEXT, [candidate])],
        plan=plan,
        limit=0,
        fusion_backend=FusionBackend.SURREAL_RRF,
        fusion_failures=failures,
    )
    metadata = search_module._fusion_receipt_metadata(
        requested_backend=FusionBackend.SURREAL_RRF,
        actual_backend=fusion.actual_backend,
        failures=failures,
    )

    assert fusion.candidates == []
    assert metadata["fusion_backend"] == "python_rrf"
    assert metadata["fusion_backend_requested"] == "surreal_rrf"
    assert metadata["fusion_backend_actual"] == "python_rrf"
    assert metadata["fusion_degraded"] is True
    assert metadata["fusion_failures"] == [{"source": "surreal_rrf", "error_type": "RuntimeError"}]


@pytest.mark.asyncio
async def test_candidate_source_reports_surreal_error_envelope() -> None:
    plan = build_context_retrieval_plan(
        query="active task followup",
        organization_id="org-123",
        facets=[ContextFacet.ACTIVE_WORK],
        facet_types={ContextFacet.ACTIVE_WORK: ["task"]},
        principal_id="user-123",
        project=None,
        accessible_projects=None,
    )

    gathered = await search_module._gather_candidate_sources(
        search_module._empty_candidate_source(),
        [
            (
                RetrievalSignal.NODE_FULLTEXT,
                search_module._node_fulltext_candidates(
                    client=_SurrealErrorEnvelopeClient(),
                    plan=plan,
                    search_filter=search_module.SearchFilter(),
                    limit=3,
                ),
            )
        ],
    )
    raw_source, graph_sources, _raw_failures, _raw_metadata = gathered

    assert raw_source.failure is None
    assert graph_sources[0].failure is not None
    assert graph_sources[0].failure.as_metadata() == {
        "source": "node_fulltext",
        "error_type": "SurrealQueryError",
    }


@pytest.mark.asyncio
async def test_deterministic_embedding_provider_batches_stably() -> None:
    metadata = EmbeddingMetadata(
        provider="deterministic",
        model="unit-test",
        dimensions=4,
        cache_namespace="retrieval-test",
        tokenizer_estimate_method="utf8-byte-length",
    )
    provider = DeterministicEmbeddingProvider(metadata)

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


class _FacetSearchClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def execute_query(self, query: str, **params: object) -> list[dict[str, object]]:
        self.calls.append((query, params))
        return []


class _GraphExpansionClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def execute_query(self, query: str, **params: object) -> list[dict[str, object]]:
        self.calls.append((query, params))
        if "FROM mentions" in query:
            return [{"uuid": "mentioned-node"}]
        if 'out.entity_type = "community"' in query or "target_id IN $community_uuids" in query:
            return []
        if "FROM relates_to" in query:
            return [{"uuid": "related-node"}]
        if "FROM entity" in query:
            return [
                {
                    "uuid": "related-node",
                    "name": "Related Task",
                    "entity_type": "task",
                    "content": "nearby task context",
                    "project_id": "project_123",
                    "attributes": {},
                    "created_at": None,
                },
                {
                    "uuid": "mentioned-node",
                    "name": "Mentioned Task",
                    "entity_type": "task",
                    "content": "episode-mentioned task context",
                    "project_id": "project_123",
                    "attributes": {},
                    "created_at": None,
                },
            ]
        return []


class _WeightedGraphExpansionClient:
    async def execute_query(self, query: str, **params: object) -> list[dict[str, object]]:
        if "FROM mentions" in query:
            return []
        if 'out.entity_type = "community"' in query or "target_id IN $community_uuids" in query:
            return []
        if "FROM relates_to" in query:
            rows = [
                {"uuid": "generic-node", "relationship": "RELATED_TO"},
                {"uuid": "decision-node", "relationship": "DECIDES"},
            ]
            return rows[: int(params.get("limit", len(rows)))]
        if "FROM entity" in query:
            return [
                {
                    "uuid": "generic-node",
                    "name": "Generic Context",
                    "entity_type": "task",
                    "content": "generic nearby context",
                    "project_id": "project_123",
                    "attributes": {},
                    "created_at": None,
                },
                {
                    "uuid": "decision-node",
                    "name": "Decision Context",
                    "entity_type": "decision",
                    "content": "high-signal decision context",
                    "project_id": "project_123",
                    "attributes": {},
                    "created_at": None,
                },
            ]
        return []


class _OverlappingGraphExpansionClient:
    async def execute_query(self, query: str, **_params: object) -> list[dict[str, object]]:
        if "FROM mentions" in query:
            return [{"uuid": "shared-node"}]
        if 'out.entity_type = "community"' in query or "target_id IN $community_uuids" in query:
            return []
        if "FROM relates_to" in query:
            return [{"uuid": "shared-node", "relationship": "DECIDES"}]
        if "FROM entity" in query:
            return [
                {
                    "uuid": "shared-node",
                    "name": "Shared Context",
                    "entity_type": "decision",
                    "content": "same node reached through two graph paths",
                    "project_id": "project_123",
                    "attributes": {},
                    "created_at": None,
                }
            ]
        return []


class _DepthGraphExpansionClient:
    async def execute_query(self, query: str, **params: object) -> list[dict[str, object]]:
        if "FROM mentions" in query:
            return []
        if 'out.entity_type = "community"' in query or "target_id IN $community_uuids" in query:
            return []
        if "FROM relates_to" in query:
            source_uuids = params.get("source_uuids")
            if source_uuids == ["task-seed"]:
                return [{"uuid": "intermediate-node", "relationship": "RELATED_TO"}]
            if source_uuids == ["intermediate-node"]:
                return [{"uuid": "decision-node", "relationship": "DECIDES"}]
            return []
        if "FROM entity" in query:
            return [
                {
                    "uuid": "intermediate-node",
                    "name": "Intermediate Context",
                    "entity_type": "task",
                    "content": "first-hop generic context",
                    "project_id": "project_123",
                    "attributes": {},
                    "created_at": None,
                },
                {
                    "uuid": "decision-node",
                    "name": "Decision Context",
                    "entity_type": "decision",
                    "content": "second-hop decision context",
                    "project_id": "project_123",
                    "attributes": {},
                    "created_at": None,
                },
            ]
        return []


class _CommunityGraphExpansionClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def execute_query(self, query: str, **params: object) -> list[dict[str, object]]:
        self.calls.append((query, params))
        if "FROM mentions" in query:
            return []
        if 'out.entity_type = "community"' in query:
            assert params["source_uuids"] == ["task-seed"]
            return [{"uuid": "community-auth"}]
        if "target_id IN $community_uuids" in query:
            assert params["community_uuids"] == ["community-auth"]
            assert params["source_uuids"] == ["task-seed"]
            return [{"uuid": "community-peer", "community_id": "community-auth"}]
        if "FROM relates_to" in query:
            return [{"uuid": "generic-node", "relationship": "RELATED_TO"}]
        if "FROM entity" in query:
            return [
                {
                    "uuid": "generic-node",
                    "name": "Generic Context",
                    "entity_type": "task",
                    "content": "generic nearby context",
                    "project_id": "project_123",
                    "attributes": {},
                    "created_at": None,
                },
                {
                    "uuid": "community-peer",
                    "name": "Community Peer",
                    "entity_type": "task",
                    "content": "shared community context",
                    "project_id": "project_123",
                    "attributes": {},
                    "created_at": None,
                },
            ]
        return []


@pytest.mark.asyncio
async def test_context_search_pushes_facet_types_into_graph_queries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = build_context_retrieval_plan(
        query="active task followup",
        organization_id="org-123",
        facets=[ContextFacet.ACTIVE_WORK],
        facet_types={ContextFacet.ACTIVE_WORK: ["task"]},
        principal_id="user-123",
        project="project_123",
        accessible_projects={"project_123"},
        limit=12,
    )
    provider = DeterministicEmbeddingProvider(
        EmbeddingMetadata(
            provider="deterministic",
            model="unit-test",
            dimensions=4,
            cache_namespace="retrieval-test",
            tokenizer_estimate_method="utf8-byte-length",
        )
    )
    client = _FacetSearchClient()

    class Runtime:
        pass

    runtime = Runtime()
    runtime.client = client

    async def fake_runtime(_organization_id: str, **_kwargs: object) -> Runtime:
        return runtime

    async def fake_raw_recall(**_kwargs: object) -> list[RawMemory]:
        raise AssertionError("active-work facet should not recall raw memories")

    monkeypatch.setattr(search_module, "get_surreal_graph_runtime", fake_runtime)

    response = await search_module.context_search(
        plan=plan,
        types=["task"],
        facet=ContextFacet.ACTIVE_WORK,
        limit=3,
        embedding_provider=provider,
        raw_memory_recall_fn=fake_raw_recall,
    )

    assert response.filters["vector_status"] == "empty"
    assert response.filters["vector_degraded"] is False
    assert response.filters["vector_candidate_count"] == 0
    assert response.filters["candidate_source_degraded"] is False
    assert response.filters["candidate_source_failure_count"] == 0
    stage_timings = response.filters["stage_timings_ms"]
    assert set(stage_timings) == {
        "runtime_setup",
        "lexical_candidates",
        "vector_candidates",
        "graph_expansion",
        "candidate_filtering",
        "fusion",
        "query_coverage",
        "materialization",
        "total",
    }
    assert all(value >= 0 for value in stage_timings.values())
    assert stage_timings["total"] >= max(stage_timings.values())
    assert client.calls
    assert all("FROM relates_to" not in query for query, _ in client.calls)
    assert all("FROM episode" not in query for query, _ in client.calls)
    assert all(params["node_types"] == ["task"] for _, params in client.calls)
    assert all(params["limit"] == 3 for _, params in client.calls)
    assert any("entity_type IN $node_types" in query for query, _ in client.calls)
    assert any("name_embedding <|3, 40|> $query_embedding" in query for query, _ in client.calls)


@pytest.mark.asyncio
async def test_context_search_reports_graph_expansion_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = build_context_retrieval_plan(
        query="active task followup",
        organization_id="org-123",
        facets=[ContextFacet.ACTIVE_WORK],
        facet_types={ContextFacet.ACTIVE_WORK: ["task"]},
        principal_id="user-123",
        project="project_123",
        accessible_projects={"project_123"},
        limit=12,
    )
    candidate = RetrievalCandidate(
        id="task-direct",
        type="task",
        name="Direct Task",
        content="direct task context",
        score=1.0,
        source=None,
        metadata={},
        project_id="project_123",
    )

    class Runtime:
        client = _FacetSearchClient()

    async def fake_runtime(_organization_id: str, **_kwargs: object) -> Runtime:
        return Runtime()

    async def fake_node_fulltext(**_kwargs: object) -> list[RetrievalCandidate]:
        return [candidate]

    async def fake_graph_expansion(**_kwargs: object) -> list[RetrievalCandidate]:
        raise RuntimeError("bfs unavailable")

    monkeypatch.setattr(search_module, "get_surreal_graph_runtime", fake_runtime)
    monkeypatch.setattr(search_module, "_node_fulltext_candidates", fake_node_fulltext)
    monkeypatch.setattr(search_module, "_graph_expansion_candidates", fake_graph_expansion)

    response = await search_module.context_search(
        plan=plan,
        types=["task"],
        facet=ContextFacet.ACTIVE_WORK,
        limit=3,
        raw_memory_recall_fn=lambda **_kwargs: [],
    )

    assert [result.id for result in response.results] == ["task-direct"]
    assert response.filters["candidate_source_degraded"] is True
    assert response.filters["candidate_source_failure_count"] == 1
    assert response.filters["candidate_source_failures"] == [
        {"source": "graph_expansion", "error_type": "RuntimeError"}
    ]


@pytest.mark.asyncio
async def test_context_search_reports_raw_recall_source_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = build_context_retrieval_plan(
        query="mailbox surrealdb",
        organization_id="org-123",
        facets=[ContextFacet.RECENT_MEMORY],
        facet_types={ContextFacet.RECENT_MEMORY: ["raw_memory"]},
        principal_id="user-123",
        project=None,
        accessible_projects=None,
        limit=8,
    )
    raw_memory = RawMemory(
        id="memory-1",
        organization_id="org-123",
        source_id="source-mail-1",
        principal_id="user-123",
        memory_scope=MemoryScope.PRIVATE,
        title="Mailbox thread",
        raw_content="SurrealDB recall notes",
        score=0.91,
    )

    class Runtime:
        client = _FacetSearchClient()

    async def fake_runtime(_organization_id: str, **_kwargs: object) -> Runtime:
        return Runtime()

    async def fake_raw_recall(**_kwargs: object) -> RawMemoryRecallResult:
        return RawMemoryRecallResult(
            memories=(raw_memory,),
            sources=(
                CandidateSourceResult.failed("raw_fulltext", "RuntimeError"),
                CandidateSourceResult.success("raw_lexical", [raw_memory]),
            ),
        )

    monkeypatch.setattr(search_module, "get_surreal_graph_runtime", fake_runtime)

    response = await search_module.context_search(
        plan=plan,
        types=["raw_memory"],
        facet=ContextFacet.RECENT_MEMORY,
        limit=3,
        raw_memory_recall_fn=fake_raw_recall,
    )

    assert [result.id for result in response.results] == ["raw_memory:memory-1"]
    assert response.filters["raw_recall_degraded"] is True
    assert response.filters["raw_recall_failure_count"] == 1
    assert response.filters["candidate_source_degraded"] is True
    assert response.filters["candidate_source_failure_count"] == 1
    assert response.filters["candidate_source_failures"] == [
        {"source": "raw_fulltext", "error_type": "RuntimeError"}
    ]


@pytest.mark.asyncio
async def test_context_search_reports_surreal_rrf_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = build_context_retrieval_plan(
        query="active task followup",
        organization_id="org-123",
        facets=[ContextFacet.ACTIVE_WORK],
        facet_types={ContextFacet.ACTIVE_WORK: ["task"]},
        principal_id="user-123",
        project="project_123",
        accessible_projects={"project_123"},
        limit=12,
    )
    candidate = RetrievalCandidate(
        id="task-direct",
        type="task",
        name="Direct Task",
        content="direct task context",
        score=1.0,
        source=None,
        metadata={},
        project_id="project_123",
    )

    class Runtime:
        client = _RrfOnlyFailingClient()

    async def fake_runtime(_organization_id: str, **_kwargs: object) -> Runtime:
        return Runtime()

    async def fake_node_fulltext(**_kwargs: object) -> list[RetrievalCandidate]:
        return [candidate]

    async def fake_graph_expansion(**_kwargs: object) -> list[RetrievalCandidate]:
        return []

    monkeypatch.setenv("SIBYL_FUSION_BACKEND", "surreal_rrf")
    monkeypatch.setattr(search_module, "get_surreal_graph_runtime", fake_runtime)
    monkeypatch.setattr(search_module, "_node_fulltext_candidates", fake_node_fulltext)
    monkeypatch.setattr(search_module, "_graph_expansion_candidates", fake_graph_expansion)

    response = await search_module.context_search(
        plan=plan,
        types=["task"],
        facet=ContextFacet.ACTIVE_WORK,
        limit=3,
        raw_memory_recall_fn=lambda **_kwargs: [],
    )

    assert [result.id for result in response.results] == ["task-direct"]
    assert response.filters["fusion_backend"] == "python_rrf"
    assert response.filters["fusion_backend_requested"] == "surreal_rrf"
    assert response.filters["fusion_backend_actual"] == "python_rrf"
    assert response.filters["fusion_degraded"] is True
    assert response.filters["fusion_failure_count"] == 1
    assert response.filters["fusion_failures"] == [
        {"source": "surreal_rrf", "error_type": "RuntimeError"}
    ]


@pytest.mark.asyncio
async def test_graph_expansion_skips_mentions_for_entity_seeds_and_limits_edges() -> None:
    plan = build_context_retrieval_plan(
        query="active task followup",
        organization_id="org-123",
        facets=[ContextFacet.ACTIVE_WORK],
        facet_types={ContextFacet.ACTIVE_WORK: ["task"]},
        principal_id="user-123",
        project="project_123",
        accessible_projects={"project_123"},
        limit=12,
    )
    client = _GraphExpansionClient()

    candidates = await search_module._graph_expansion_candidates(
        client=client,
        plan=plan,
        search_filter=search_module.SearchFilter(
            node_types=("task",),
            project_ids=("project_123",),
        ),
        seed_candidates=[
            RetrievalCandidate(
                id="task-seed",
                type="task",
                name="Seed Task",
                content="seed",
                score=1.0,
                source=None,
                metadata={},
                project_id="project_123",
            )
        ],
        limit=2,
    )

    assert [candidate.id for candidate in candidates] == ["related-node"]
    assert all("FROM mentions" not in query for query, _ in client.calls)
    relation_calls = [
        (query, params) for query, params in client.calls if "FROM relates_to" in query
    ]
    assert relation_calls
    assert relation_calls[0][1]["limit"] == search_module._graph_expansion_fetch_limit(2)
    assert "LIMIT $limit" in relation_calls[0][0]


@pytest.mark.asyncio
async def test_graph_expansion_uses_mentions_for_episode_seeds_with_limit() -> None:
    plan = build_context_retrieval_plan(
        query="recent episode followup",
        organization_id="org-123",
        facets=[ContextFacet.RECENT_MEMORY],
        facet_types={ContextFacet.RECENT_MEMORY: ["episode"]},
        principal_id="user-123",
        project="project_123",
        accessible_projects={"project_123"},
        limit=12,
    )
    client = _GraphExpansionClient()

    candidates = await search_module._graph_expansion_candidates(
        client=client,
        plan=plan,
        search_filter=search_module.SearchFilter(
            node_types=("task",),
            project_ids=("project_123",),
        ),
        seed_candidates=[
            RetrievalCandidate(
                id="episode-seed",
                type="episode",
                name="Seed Episode",
                content="seed",
                score=1.0,
                source=None,
                metadata={},
                project_id="project_123",
            )
        ],
        limit=2,
    )

    assert [candidate.id for candidate in candidates] == ["mentioned-node"]
    assert candidates[0].score == pytest.approx(0.58)
    assert candidates[0].metadata["graph_expansion_relationship"] == "MENTIONS"
    assert candidates[0].metadata["graph_expansion_depth"] == 1
    mention_calls = [(query, params) for query, params in client.calls if "FROM mentions" in query]
    assert mention_calls
    assert mention_calls[0][1]["episode_uuids"] == ["episode-seed"]
    assert mention_calls[0][1]["limit"] == search_module._graph_expansion_fetch_limit(2)
    assert "LIMIT $limit" in mention_calls[0][0]


@pytest.mark.asyncio
async def test_graph_expansion_ranks_typed_edges_above_generic_edges() -> None:
    plan = build_context_retrieval_plan(
        query="active task followup",
        organization_id="org-123",
        facets=[ContextFacet.ACTIVE_WORK],
        facet_types={ContextFacet.ACTIVE_WORK: ["task"]},
        principal_id="user-123",
        project="project_123",
        accessible_projects={"project_123"},
        limit=12,
    )

    candidates = await search_module._graph_expansion_candidates(
        client=_WeightedGraphExpansionClient(),
        plan=plan,
        search_filter=search_module.SearchFilter(project_ids=("project_123",)),
        seed_candidates=[
            RetrievalCandidate(
                id="task-seed",
                type="task",
                name="Seed Task",
                content="seed",
                score=1.0,
                source=None,
                metadata={},
                project_id="project_123",
            )
        ],
        limit=2,
    )

    assert [candidate.id for candidate in candidates] == [
        "decision-node",
        "generic-node",
    ]
    assert candidates[0].score > candidates[1].score
    assert candidates[0].metadata["graph_expansion_relationship"] == "DECIDES"
    assert candidates[0].metadata["graph_expansion_depth"] == 1
    assert candidates[1].metadata["graph_expansion_relationship"] == "RELATED_TO"

    limited_candidates = await search_module._graph_expansion_candidates(
        client=_WeightedGraphExpansionClient(),
        plan=plan,
        search_filter=search_module.SearchFilter(project_ids=("project_123",)),
        seed_candidates=[
            RetrievalCandidate(
                id="task-seed",
                type="task",
                name="Seed Task",
                content="seed",
                score=1.0,
                source=None,
                metadata={},
                project_id="project_123",
            )
        ],
        limit=1,
    )

    assert [candidate.id for candidate in limited_candidates] == ["decision-node"]


@pytest.mark.asyncio
async def test_graph_expansion_keeps_strongest_same_depth_path() -> None:
    plan = build_context_retrieval_plan(
        query="active task followup",
        organization_id="org-123",
        facets=[ContextFacet.ACTIVE_WORK, ContextFacet.RECENT_MEMORY],
        facet_types={
            ContextFacet.ACTIVE_WORK: ["task"],
            ContextFacet.RECENT_MEMORY: ["episode"],
        },
        principal_id="user-123",
        project="project_123",
        accessible_projects={"project_123"},
        limit=12,
    )

    candidates = await search_module._graph_expansion_candidates(
        client=_OverlappingGraphExpansionClient(),
        plan=plan,
        search_filter=search_module.SearchFilter(project_ids=("project_123",)),
        seed_candidates=[
            RetrievalCandidate(
                id="episode-seed",
                type="episode",
                name="Seed Episode",
                content="seed",
                score=1.0,
                source=None,
                metadata={},
                project_id="project_123",
            ),
            RetrievalCandidate(
                id="task-seed",
                type="task",
                name="Seed Task",
                content="seed",
                score=1.0,
                source=None,
                metadata={},
                project_id="project_123",
            ),
        ],
        limit=2,
    )

    assert [candidate.id for candidate in candidates] == ["shared-node"]
    assert candidates[0].score == pytest.approx(1.0)
    assert candidates[0].metadata["graph_expansion_relationship"] == "DECIDES"


@pytest.mark.asyncio
async def test_graph_expansion_applies_depth_decay() -> None:
    plan = replace(
        build_context_retrieval_plan(
            query="active task followup",
            organization_id="org-123",
            facets=[ContextFacet.ACTIVE_WORK],
            facet_types={ContextFacet.ACTIVE_WORK: ["task"]},
            principal_id="user-123",
            project="project_123",
            accessible_projects={"project_123"},
            limit=12,
        ),
        graph_expansion_depth=2,
    )

    candidates = await search_module._graph_expansion_candidates(
        client=_DepthGraphExpansionClient(),
        plan=plan,
        search_filter=search_module.SearchFilter(project_ids=("project_123",)),
        seed_candidates=[
            RetrievalCandidate(
                id="task-seed",
                type="task",
                name="Seed Task",
                content="seed",
                score=1.0,
                source=None,
                metadata={},
                project_id="project_123",
            )
        ],
        limit=2,
    )

    assert [candidate.id for candidate in candidates] == [
        "decision-node",
        "intermediate-node",
    ]
    assert candidates[0].score == pytest.approx(0.72)
    assert candidates[0].metadata["graph_expansion_relationship"] == "DECIDES"
    assert candidates[0].metadata["graph_expansion_depth"] == 2
    assert candidates[1].score == pytest.approx(0.64)


def test_graph_expansion_path_score_uses_fallback_and_floor() -> None:
    assert search_module._graph_expansion_path_score(
        "UNKNOWN_RELATIONSHIP",
        depth=1,
    ) == pytest.approx(0.64)
    assert search_module._graph_expansion_path_score(
        "MENTIONS",
        depth=99,
    ) == pytest.approx(0.1)


@pytest.mark.asyncio
async def test_graph_expansion_uses_shared_community_membership() -> None:
    plan = replace(
        build_context_retrieval_plan(
            query="active task followup",
            organization_id="org-123",
            facets=[ContextFacet.ACTIVE_WORK],
            facet_types={ContextFacet.ACTIVE_WORK: ["task"]},
            principal_id="user-123",
            project="project_123",
            accessible_projects={"project_123"},
            limit=12,
        ),
        graph_expansion_depth=2,
    )
    client = _CommunityGraphExpansionClient()

    candidates = await search_module._graph_expansion_candidates(
        client=client,
        plan=plan,
        search_filter=search_module.SearchFilter(project_ids=("project_123",)),
        seed_candidates=[
            RetrievalCandidate(
                id="task-seed",
                type="task",
                name="Seed Task",
                content="seed",
                score=1.0,
                source=None,
                metadata={},
                project_id="project_123",
            )
        ],
        limit=4,
    )

    assert [candidate.id for candidate in candidates] == [
        "community-peer",
        "generic-node",
    ]
    assert candidates[0].score == pytest.approx(0.74)
    assert candidates[0].metadata["graph_expansion_relationship"] == "SHARES_COMMUNITY"
    assert candidates[0].metadata["graph_expansion_community_id"] == "community-auth"
    assert sum('out.entity_type = "community"' in query for query, _ in client.calls) == 1
    assert sum("target_id IN $community_uuids" in query for query, _ in client.calls) == 1


@pytest.mark.asyncio
async def test_vector_candidate_sources_use_embedding_contract() -> None:
    plan = build_context_retrieval_plan(
        query="native vectors",
        organization_id="org-123",
        facets=[ContextFacet.ACTIVE_WORK],
        facet_types={ContextFacet.ACTIVE_WORK: ["task"]},
        principal_id="user-123",
        project="project_123",
        accessible_projects={"project_123"},
    )
    provider = DeterministicEmbeddingProvider(
        EmbeddingMetadata(
            provider="deterministic",
            model="unit-test",
            dimensions=4,
            cache_namespace="retrieval-test",
            tokenizer_estimate_method="utf8-byte-length",
        )
    )
    client = _VectorClient()

    node_candidates, edge_candidates = await search_module._vector_candidate_sources(
        client=client,
        plan=plan,
        search_filter=search_module.SearchFilter(project_ids=("project_123",)),
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


def test_vector_candidate_fetch_metadata_distinguishes_empty_and_failure() -> None:
    empty = VectorCandidateFetch(
        node_candidates=[],
        edge_candidates=[],
        requested=True,
        attempted=True,
    )
    failed = VectorCandidateFetch(
        node_candidates=[],
        edge_candidates=[],
        requested=True,
        attempted=True,
        failures=("embedding:RuntimeError",),
        reason="embedding_failed",
    )

    assert empty.as_metadata() == {
        "vector_status": "empty",
        "vector_requested": True,
        "vector_attempted": True,
        "vector_degraded": False,
        "vector_candidate_count": 0,
    }
    assert failed.as_metadata()["vector_status"] == "embedding_failed"
    assert failed.as_metadata()["vector_degraded"] is True
    assert failed.as_metadata()["vector_failures"] == ["embedding:RuntimeError"]


@pytest.mark.asyncio
async def test_candidate_source_gather_reports_failures() -> None:
    async def failing_raw_source() -> list[RetrievalCandidate]:
        raise RuntimeError("raw source offline")

    async def invalid_graph_source() -> object:
        return object()

    gathered = await search_module._gather_candidate_sources(
        failing_raw_source(),
        [(RetrievalSignal.NODE_FULLTEXT, invalid_graph_source())],
    )
    raw_source, graph_sources, _raw_failures, _raw_metadata = gathered
    metadata = search_module._candidate_source_metadata((raw_source, *graph_sources))

    assert raw_source.degraded is True
    assert graph_sources[0].degraded is True
    assert metadata["candidate_source_degraded"] is True
    assert metadata["candidate_source_failure_count"] == 2
    assert metadata["candidate_source_failures"] == [
        {"source": "raw_lexical", "error_type": "RuntimeError"},
        {"source": "node_fulltext", "error_type": "invalid:object"},
    ]


@pytest.mark.asyncio
async def test_vector_candidate_sources_report_embedding_failure() -> None:
    plan = build_context_retrieval_plan(
        query="native vectors",
        organization_id="org-123",
        facets=[ContextFacet.ACTIVE_WORK],
        facet_types={ContextFacet.ACTIVE_WORK: ["task"]},
        principal_id="user-123",
        project="project_123",
        accessible_projects={"project_123"},
    )

    class FailingProvider:
        metadata = EmbeddingMetadata(
            provider="failing",
            model="unit-test",
            dimensions=4,
            cache_namespace="retrieval-test",
            tokenizer_estimate_method="utf8-byte-length",
        )

        async def embed_texts(self, *_args: object, **_kwargs: object) -> list[list[float]]:
            raise RuntimeError("provider offline")

    result = await search_module._vector_candidate_sources_detailed(
        client=_VectorClient(),
        plan=plan,
        search_filter=search_module.SearchFilter(project_ids=("project_123",)),
        embedding_provider=FailingProvider(),
    )

    assert result.status == "embedding_failed"
    assert result.degraded is True
    assert result.failures == ("embedding:RuntimeError",)


@pytest.mark.asyncio
async def test_vector_candidate_sources_report_partial_query_failure() -> None:
    plan = build_context_retrieval_plan(
        query="native vectors",
        organization_id="org-123",
        facets=[ContextFacet.ACTIVE_WORK],
        facet_types={ContextFacet.ACTIVE_WORK: ["task"]},
        principal_id="user-123",
        project="project_123",
        accessible_projects={"project_123"},
    )
    provider = DeterministicEmbeddingProvider(
        EmbeddingMetadata(
            provider="deterministic",
            model="unit-test",
            dimensions=4,
            cache_namespace="retrieval-test",
            tokenizer_estimate_method="utf8-byte-length",
        )
    )

    class PartiallyFailingVectorClient(_VectorClient):
        async def execute_query(self, query: str, **params: object) -> list[dict[str, object]]:
            if "fact_embedding" in query:
                raise RuntimeError("edge index unavailable")
            return await super().execute_query(query, **params)

    result = await search_module._vector_candidate_sources_detailed(
        client=PartiallyFailingVectorClient(),
        plan=plan,
        search_filter=search_module.SearchFilter(project_ids=("project_123",)),
        embedding_provider=provider,
    )

    assert result.status == "partial"
    assert [candidate.id for candidate in result.node_candidates] == ["task-vector"]
    assert result.edge_candidates == []
    assert result.failures == ("edge_vector:RuntimeError",)


@pytest.mark.asyncio
async def test_vector_candidate_sources_use_configured_knn_effort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(search_module.core_config, "graph_knn_ef", 96)
    plan = build_context_retrieval_plan(
        query="native vectors",
        organization_id="org-123",
        facets=[ContextFacet.ACTIVE_WORK],
        facet_types={ContextFacet.ACTIVE_WORK: ["task"]},
        principal_id="user-123",
        project="project_123",
        accessible_projects={"project_123"},
    )
    provider = DeterministicEmbeddingProvider(
        EmbeddingMetadata(
            provider="deterministic",
            model="unit-test",
            dimensions=4,
            cache_namespace="retrieval-test",
            tokenizer_estimate_method="utf8-byte-length",
        )
    )
    client = _VectorClient()

    await search_module._vector_candidate_sources(
        client=client,
        plan=plan,
        search_filter=search_module.SearchFilter(project_ids=("project_123",)),
        embedding_provider=provider,
    )

    node_query = next(call for call in client.calls if "name_embedding" in call[0])[0]
    edge_query = next(call for call in client.calls if "fact_embedding" in call[0])[0]
    assert "name_embedding <|8, 96|> $query_embedding" in node_query
    assert "fact_embedding <|8, 96|> $query_embedding" in edge_query


def test_node_record_candidates_keep_top_level_provenance_metadata() -> None:
    candidate = search_module._candidate_from_node_record(
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
        signal=RetrievalSignal.NODE_FULLTEXT,
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
    candidate = search_module._candidate_from_edge_record(
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
        signal=RetrievalSignal.EDGE_FULLTEXT,
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
    plan = build_context_retrieval_plan(
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

    raw_fetch = await search_module._recall_raw_candidates(
        plan=plan,
        facet=ContextFacet.RECENT_MEMORY,
        requested_types={"session", "episode", "note"},
        limit=2,
        recall_fn=fake_recall,
    )

    assert [candidate.id for candidate in raw_fetch.candidates] == [
        "raw_memory:diary-1",
        "raw_memory:private-1",
        "raw_memory:private-2",
    ]


@pytest.mark.asyncio
async def test_raw_candidates_recall_accessible_project_scopes_concurrently() -> None:
    plan = build_context_retrieval_plan(
        query="parallel project recall",
        organization_id="org-123",
        facets=[ContextFacet.RECENT_MEMORY],
        facet_types={ContextFacet.RECENT_MEMORY: ["raw_memory"]},
        principal_id="user-123",
        project=None,
        accessible_projects={"project_123", "project_456"},
        limit=8,
    )
    started: list[str | None] = []
    gate = asyncio.Event()

    async def fake_recall(**kwargs: object) -> list[RawMemory]:
        scope_key = kwargs.get("scope_key")
        assert scope_key is None or isinstance(scope_key, str)
        started.append(scope_key)
        if len(started) == 3:
            gate.set()
        await gate.wait()
        memory_scope = MemoryScope.PROJECT if scope_key else MemoryScope.PRIVATE
        return [
            RawMemory(
                id=f"memory-{scope_key or 'private'}",
                organization_id="org-123",
                source_id=f"source-{scope_key or 'private'}",
                principal_id="user-123",
                memory_scope=memory_scope,
                scope_key=scope_key,
                title=f"Memory {scope_key or 'private'}",
                raw_content="parallel recall",
                score=0.5,
            )
        ]

    raw_fetch = await asyncio.wait_for(
        search_module._recall_raw_candidates(
            plan=plan,
            facet=ContextFacet.RECENT_MEMORY,
            requested_types={"raw_memory"},
            limit=2,
            recall_fn=fake_recall,
        ),
        timeout=1,
    )

    assert set(started) == {None, "project_123", "project_456"}
    assert {candidate.id for candidate in raw_fetch.candidates} == {
        "raw_memory:memory-private",
        "raw_memory:memory-project_123",
        "raw_memory:memory-project_456",
    }


@pytest.mark.asyncio
async def test_raw_candidates_degrade_failed_scope_without_dropping_successes() -> None:
    plan = build_context_retrieval_plan(
        query="partial project recall",
        organization_id="org-123",
        facets=[ContextFacet.RECENT_MEMORY],
        facet_types={ContextFacet.RECENT_MEMORY: ["raw_memory"]},
        principal_id="user-123",
        project=None,
        accessible_projects={"project_123", "project_456"},
        limit=8,
    )

    async def fake_recall(**kwargs: object) -> list[RawMemory]:
        scope_key = kwargs.get("scope_key")
        if scope_key == "project_456":
            raise RuntimeError("project recall unavailable")
        assert scope_key is None or isinstance(scope_key, str)
        return [
            RawMemory(
                id=f"memory-{scope_key or 'private'}",
                organization_id="org-123",
                source_id=f"source-{scope_key or 'private'}",
                principal_id="user-123",
                memory_scope=MemoryScope.PROJECT if scope_key else MemoryScope.PRIVATE,
                scope_key=scope_key,
                title=f"Memory {scope_key or 'private'}",
                raw_content="partial recall",
                score=0.5,
            )
        ]

    raw_fetch = await search_module._recall_raw_candidates(
        plan=plan,
        facet=ContextFacet.RECENT_MEMORY,
        requested_types={"raw_memory"},
        limit=2,
        recall_fn=fake_recall,
    )

    assert {candidate.id for candidate in raw_fetch.candidates} == {
        "raw_memory:memory-private",
        "raw_memory:memory-project_123",
    }
    assert raw_fetch.metadata["raw_recall_degraded"] is True
    assert raw_fetch.metadata["raw_recall_failure_count"] == 1
    assert raw_fetch.metadata["raw_recall_failures"] == [
        {"source": "raw_scope_recall", "error_type": "RuntimeError"}
    ]


@pytest.mark.asyncio
async def test_raw_candidates_propagate_cancelled_scope() -> None:
    plan = build_context_retrieval_plan(
        query="cancelled project recall",
        organization_id="org-123",
        facets=[ContextFacet.RECENT_MEMORY],
        facet_types={ContextFacet.RECENT_MEMORY: ["raw_memory"]},
        principal_id="user-123",
        project=None,
        accessible_projects={"project_123"},
        limit=8,
    )

    async def fake_recall(**kwargs: object) -> list[RawMemory]:
        if kwargs.get("scope_key") == "project_123":
            raise asyncio.CancelledError
        return [
            RawMemory(
                id="memory-private",
                organization_id="org-123",
                source_id="source-private",
                principal_id="user-123",
                title="Memory private",
                raw_content="cancelled recall",
                score=0.5,
            )
        ]

    with pytest.raises(asyncio.CancelledError):
        await search_module._recall_raw_candidates(
            plan=plan,
            facet=ContextFacet.RECENT_MEMORY,
            requested_types={"raw_memory"},
            limit=2,
            recall_fn=fake_recall,
        )


@pytest.mark.asyncio
async def test_raw_candidates_filter_lifecycle_hidden_memory() -> None:
    plan = build_context_retrieval_plan(
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

    raw_fetch = await search_module._recall_raw_candidates(
        plan=plan,
        facet=ContextFacet.RECENT_MEMORY,
        requested_types={"session", "episode", "note"},
        limit=5,
        recall_fn=fake_recall,
    )

    assert [candidate.id for candidate in raw_fetch.candidates] == ["raw_memory:visible-1"]


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
    plan = build_context_retrieval_plan(
        query="surreal planner warning",
        organization_id="org-123",
        facets=[ContextFacet.ACTIVE_WORK],
        facet_types={ContextFacet.ACTIVE_WORK: ["task"]},
        principal_id="user-123",
        project="project_123",
        accessible_projects={"project_123"},
    )
    client = _EdgeFulltextClient()

    candidates = await search_module._edge_fulltext_candidates(
        client=client,
        plan=plan,
        search_filter=search_module.SearchFilter(
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


# --- H6: one shared fusion+ranking core across both retrieval surfaces -------


def _fused_entry(
    candidate: RetrievalCandidate,
    score: float,
) -> tuple[RetrievalCandidate, float, dict[str, object]]:
    return (candidate, score, {"sources": [], "ranks": {}, "original_scores": {}})


def test_context_search_and_hybrid_share_one_query_coverage_core() -> None:
    """Both retrieval surfaces route final ranking through the same function."""

    assert (
        hybrid_module.rank_items_by_query_coverage
        is query_ranking_module.rank_items_by_query_coverage
    )
    assert (
        search_module.rank_items_by_query_coverage
        is query_ranking_module.rank_items_by_query_coverage
    )


def test_context_query_coverage_reranks_through_shared_core(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """context_search's post-fusion pass calls the shared coverage ranker."""

    calls: list[str] = []
    real = query_ranking_module.rank_items_by_query_coverage

    def spy(query, items, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(query)
        return real(query, items, **kwargs)

    monkeypatch.setattr(search_module, "rank_items_by_query_coverage", spy)

    strong = RetrievalCandidate(
        id="strong",
        type="session",
        name="Homegrown tomato basil dinner",
        content="User: my homegrown tomato and basil dinner recipe was a hit.",
        score=1.0,
        source=None,
        metadata={},
    )
    weak = RetrievalCandidate(
        id="weak",
        type="session",
        name="Unrelated",
        content="User: we talked about the weather forecast yesterday.",
        score=1.0,
        source=None,
        metadata={},
    )
    fused = [_fused_entry(weak, 0.9), _fused_entry(strong, 0.8)]

    reranked = search_module._apply_query_coverage_to_fused(
        "what homegrown tomato basil dinner recipe did I make",
        fused,
        temporal_target=None,
    )

    assert calls == ["what homegrown tomato basil dinner recipe did I make"]
    assert reranked[0][0].id == "strong"
    # Fusion metadata is preserved through the rerank for each candidate.
    assert {candidate.id for candidate, _score, _meta in reranked} == {"strong", "weak"}


def test_context_query_coverage_preserves_base_order_for_thin_query() -> None:
    """A query the coverage core cannot act on leaves the fused order intact."""

    first = RetrievalCandidate(
        id="first",
        type="session",
        name="First",
        content="A grounded lexical session hit.",
        score=1.0,
        source=None,
        metadata={},
    )
    second = RetrievalCandidate(
        id="second",
        type="session",
        name="Second",
        content="Another grounded session hit.",
        score=1.0,
        source=None,
        metadata={},
    )
    fused = [_fused_entry(first, 0.9), _fused_entry(second, 0.8)]

    # Single-keyword query: rank_by_query_coverage does not apply, so the
    # shared core returns the prior order unchanged.
    reranked = search_module._apply_query_coverage_to_fused(
        "coffee",
        fused,
        temporal_target=None,
    )

    assert [candidate.id for candidate, _score, _meta in reranked] == ["first", "second"]


def test_context_query_coverage_prefers_valid_at_timestamp() -> None:
    fact = RetrievalCandidate(
        id="fact",
        type="event",
        name="Event: Disney Plus free trial",
        content="Evidence: I started a Disney+ free trial last month.",
        score=1.0,
        source=None,
        metadata={"valid_at": "2026/01/08 09:00"},
        created_at=datetime(2026, 5, 1, tzinfo=UTC),
    )
    captured: list[datetime | None] = []

    def spy(query, items, **kwargs):  # type: ignore[no-untyped-def]
        captured.append(kwargs["timestamp_fn"](fact))
        return items, False, False

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(search_module, "rank_items_by_query_coverage", spy)
    try:
        search_module._apply_query_coverage_to_fused(
            "Which streaming service did I start using most recently?",
            [_fused_entry(fact, 1.0)],
            temporal_target=None,
        )
    finally:
        monkeypatch.undo()

    assert captured == [datetime(2026, 1, 8, 9, 0, tzinfo=UTC)]


def test_context_query_coverage_promotes_projected_fact_card() -> None:
    candidates = [
        RetrievalCandidate(
            id="streaming-device",
            type="session",
            name="Streaming advice",
            content="User: I asked about streaming device recommendations for my living room.",
            score=1.0,
            source=None,
            metadata={},
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
        ),
        RetrievalCandidate(
            id="internet-service",
            type="session",
            name="Internet service",
            content="User: I compared internet service bundles for the apartment.",
            score=0.99,
            source=None,
            metadata={},
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
        ),
        RetrievalCandidate(
            id="media-cabinet",
            type="session",
            name="Media cabinet",
            content="User: I updated media cabinet cable labels.",
            score=0.98,
            source=None,
            metadata={},
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
        ),
        RetrievalCandidate(
            id="free-trial-reminders",
            type="session",
            name="Trial reminders",
            content="User: I read about free trial cancellation reminders.",
            score=0.97,
            source=None,
            metadata={},
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
        ),
        RetrievalCandidate(
            id="movie-snacks",
            type="session",
            name="Movie snacks",
            content="User: I planned movie night snacks for Friday.",
            score=0.96,
            source=None,
            metadata={},
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
        ),
        RetrievalCandidate(
            id="projected-fact",
            type="event",
            name="Event: I started a Disney free trial last month",
            content=(
                "Evidence: I started a Disney+ free trial last month.\n"
                "Actions: use\n"
                "Categories: media, service\n"
                "Relations: recency\n"
                "Terms: started, disney, free, trial"
            ),
            score=0.75,
            source=None,
            metadata={"valid_at": "2026/01/08 09:00"},
            created_at=datetime(2026, 5, 1, tzinfo=UTC),
        ),
    ]

    reranked = search_module._apply_query_coverage_to_fused(
        "Which streaming service did I start using most recently?",
        [_fused_entry(candidate, candidate.score) for candidate in candidates],
        temporal_target=datetime(2026, 1, 10, tzinfo=UTC),
    )

    assert "projected-fact" in [candidate.id for candidate, _score, _meta in reranked[:5]]


def test_scope_decisions_includes_project_less_agent_diary_scope() -> None:
    """Agent recall must reach project-less diaries even with accessible projects.

    Regression: when the principal had any accessible project and the query
    named none, the agent scope was built per accessible project only, so a
    project-less agent diary (the default diary scope) was filtered out of
    context packs. The live context-pack eval's agent-diary case caught it.
    """

    decisions = search_module._scope_decisions(
        principal_id="user-1",
        project=None,
        accessible_projects=frozenset({"project-a", "project-b"}),
        agent_id="nova",
    )

    agent_scopes = {(proj, agent) for _decision, proj, agent in decisions if agent == "nova"}
    assert (None, "nova") in agent_scopes
    assert ("project-a", "nova") in agent_scopes


def test_hybrid_query_coverage_rerank_matches_direct_core_call() -> None:
    """hybrid_search ranking output is unchanged: the helper now just delegates."""

    results = [
        (
            {
                "id": "weak",
                "name": "Weather chatter",
                "content": "User: we chatted about the weather forecast.",
            },
            0.9,
        ),
        (
            {
                "id": "strong",
                "name": "Homegrown tomato basil dinner",
                "content": "User: my homegrown tomato and basil dinner recipe was a hit.",
            },
            0.8,
        ),
    ]
    query = "what homegrown tomato basil dinner recipe did I make"

    via_helper = hybrid_module._apply_query_coverage_rerank(query, list(results))
    via_core = query_ranking_module.rank_items_by_query_coverage(
        query,
        list(results),
        text_fn=hybrid_module._entity_text,
        id_fn=hybrid_module._entity_id,
        timestamp_fn=hybrid_module.get_entity_timestamp,
        temporal_target=None,
    )

    assert [entity["id"] for entity, _score in via_helper[0]] == [
        entity["id"] for entity, _score in via_core[0]
    ]
    assert via_helper[1:] == via_core[1:]


def test_query_coverage_refinement_reuses_candidate_text() -> None:
    """The guarded second pass must not re-tokenize via caller text hooks."""

    results = [
        (
            {
                "id": "weak",
                "name": "Weather chatter",
                "content": "User: we chatted about the weather forecast.",
            },
            0.9,
        ),
        (
            {
                "id": "strong",
                "name": "Homegrown tomato basil dinner",
                "content": "User: my homegrown tomato and basil dinner recipe was a hit.",
            },
            0.8,
        ),
    ]
    calls: list[str] = []

    def text_fn(item: dict[str, str]) -> str:
        calls.append(item["id"])
        return hybrid_module._entity_text(item)

    query_ranking_module.rank_items_by_query_coverage(
        "what homegrown tomato basil dinner recipe did I make",
        list(results),
        text_fn=text_fn,
        id_fn=hybrid_module._entity_id,
        timestamp_fn=hybrid_module.get_entity_timestamp,
        temporal_target=None,
    )

    assert calls == ["weak", "strong"]
