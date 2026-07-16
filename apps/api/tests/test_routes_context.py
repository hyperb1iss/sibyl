from __future__ import annotations

import asyncio
from collections.abc import Iterator
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest
from fastapi import HTTPException

from sibyl.api.routes.context import context_pack
from sibyl.api.schemas import (
    ContextPackRequest,
    ReflectionRequest,
    SearchRequest,
    SearchResponse,
    SearchResult,
)
from sibyl.auth.authorization import ProjectAuthorizationError
from sibyl.auth.errors import ProjectAccessDeniedError
from sibyl_core.auth import ProjectRole
from sibyl_core.embeddings.providers import (
    CachedEmbeddingProvider,
    EmbeddingMetadata,
    OpenAIEmbeddingProvider,
)
from sibyl_core.models.context import (
    ContextFacet,
    ContextIntent,
    ContextItem,
    ContextItemQualityMetadata,
    ContextLayer,
    ContextPack,
    ContextSection,
)
from sibyl_core.models.reflection import (
    ClaimRecord,
    ReflectionCandidate,
    ReflectionFinding,
    ReflectionFindingKind,
    ReflectionPack,
    ReflectionRelationshipRecord,
)
from sibyl_core.retrieval.query_planning import (
    EvidenceQuery,
    EvidenceQueryFacet,
    EvidenceQueryPlan,
)


@pytest.fixture(autouse=True)
def context_audit_event() -> Iterator[AsyncMock]:
    with patch("sibyl.api.context_audit.log_memory_audit_event", AsyncMock()) as audit:
        yield audit


def _pack() -> ContextPack:
    return ContextPack(
        goal="ship faster",
        intent=ContextIntent.BUILD,
        query="ship faster",
        domain=None,
        project=None,
        sections=[],
        total_items=0,
    )


def _pack_with_quality(
    *,
    layer: ContextLayer = ContextLayer.RECALL,
    project: str | None = None,
) -> ContextPack:
    return ContextPack(
        goal="ship faster",
        intent=ContextIntent.BUILD,
        query="ship faster",
        domain=None,
        project=project,
        sections=[
            ContextSection(
                facet=ContextFacet.DECISIONS,
                title="Decisions",
                items=[
                    ContextItem(
                        id="decision_1",
                        type="decision",
                        name="Use context packs",
                        content="Agents should receive precise grouped memory.",
                        score=0.91,
                        facet=ContextFacet.DECISIONS,
                        reason="decision records a choice",
                        source="Northstar",
                        quality=ContextItemQualityMetadata(
                            origin="graph",
                            source="docs/architecture/SIBYL_NORTHSTAR.md",
                            project_id="project-sibyl",
                        ),
                    )
                ],
            )
        ],
        total_items=1,
        layer=layer,
    )


def _pack_with_usage() -> ContextPack:
    pack = _pack_with_quality()
    pack.sections[0].items[0].metadata["usage_exposure"] = {
        "status": "stamped",
        "signal_type": "exposure",
        "source_surface": "context_pack",
        "item_kind": "graph_entity",
        "item_id": "decision_1",
    }
    return ContextPack(
        goal=pack.goal,
        intent=pack.intent,
        query=pack.query,
        domain=pack.domain,
        project=pack.project,
        sections=pack.sections,
        total_items=pack.total_items,
        layer=pack.layer,
        usage_metadata={
            "usage_exposure": {
                "source_surface": "context_pack",
                "returned_count": 1,
                "stamped_count": 1,
                "excluded_count": 0,
                "coverage_complete": True,
            }
        },
    )


def _ctx() -> SimpleNamespace:
    return SimpleNamespace(user_id="user-123", api_key_memory_scope_keys=None)


def _http_request() -> SimpleNamespace:
    return SimpleNamespace(
        client=SimpleNamespace(host="10.0.0.5"),
        headers={"user-agent": "SibylTest/1.0"},
    )


def _search_response(
    query: str,
    *results: tuple[str, float],
) -> SearchResponse:
    return SearchResponse(
        results=[
            SearchResult(
                id=result_id,
                type="session",
                name=result_id,
                content=f"evidence for {result_id}",
                score=score,
                result_origin="graph",
            )
            for result_id, score in results
        ],
        total=len(results),
        query=query,
        filters={"source_query": query},
        graph_count=len(results),
        limit=12,
    )


class TestContextPackRoute:
    @pytest.mark.asyncio
    async def test_context_pack_scopes_to_accessible_projects(self) -> None:
        org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
        ctx = _ctx()

        with (
            patch(
                "sibyl.api.routes.context.list_accessible_project_graph_ids",
                AsyncMock(return_value=["proj_1"]),
            ) as list_projects,
            patch(
                "sibyl_core.tools.context.compile_context", AsyncMock(return_value=_pack())
            ) as compile_context,
        ):
            response = await context_pack(
                request=ContextPackRequest(goal="ship faster"),
                org=org,
                ctx=ctx,
            )

        list_projects.assert_awaited_once_with(ctx)
        assert response.goal == "ship faster"
        assert response.layer == ContextLayer.RECALL
        assert response.markdown is not None
        assert response.markdown.startswith("# Sibyl Context Pack")
        assert compile_context.await_args.kwargs["accessible_projects"] == {"proj_1"}
        assert compile_context.await_args.kwargs["layer"] == ContextLayer.RECALL
        assert compile_context.await_args.kwargs["principal_id"] == "user-123"
        assert compile_context.await_args.kwargs["agent_id"] is None
        assert compile_context.await_args.kwargs["project"] is None
        assert compile_context.await_args.kwargs["include_related"] is True
        assert compile_context.await_args.kwargs["related_limit"] == 3
        assert compile_context.await_args.kwargs["record_exposure"] is True

    @pytest.mark.asyncio
    async def test_context_pack_can_disable_exposure_recording(self) -> None:
        org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
        ctx = _ctx()

        with (
            patch(
                "sibyl.api.routes.context.list_accessible_project_graph_ids",
                AsyncMock(return_value=["proj_1"]),
            ),
            patch(
                "sibyl_core.tools.context.compile_context",
                AsyncMock(return_value=_pack()),
            ) as compile_context,
        ):
            await context_pack(
                request=ContextPackRequest(goal="ship faster", record_exposure=False),
                org=org,
                ctx=ctx,
            )

        assert compile_context.await_args.kwargs["record_exposure"] is False

    @pytest.mark.asyncio
    async def test_context_pack_retrieves_evidence_concurrently(self) -> None:
        org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
        started: set[str] = set()
        both_started = asyncio.Event()

        async def compile_pack(**_kwargs: object) -> ContextPack:
            started.add("context")
            if len(started) == 2:
                both_started.set()
            await asyncio.wait_for(both_started.wait(), timeout=1)
            return _pack()

        async def retrieve_evidence(*_args: object, **_kwargs: object) -> SearchResponse:
            started.add("evidence")
            if len(started) == 2:
                both_started.set()
            await asyncio.wait_for(both_started.wait(), timeout=1)
            return SearchResponse(
                results=[],
                total=0,
                query="ship faster",
                filters={"stage_timings_ms": {"total": 12.5}},
            )

        with (
            patch(
                "sibyl.api.routes.context.list_accessible_project_graph_ids",
                AsyncMock(return_value=["proj_1"]),
            ),
            patch("sibyl_core.tools.context.compile_context", side_effect=compile_pack),
            patch(
                "sibyl.api.routes.search.execute_search_request",
                side_effect=retrieve_evidence,
            ) as execute_search,
            patch("sibyl.api.routes.context.configured_embedding_provider", return_value=None),
        ):
            response = await context_pack(
                request=ContextPackRequest(
                    goal="ship faster",
                    evidence={
                        "types": ["session"],
                        "limit": 12,
                        "content_max_chars": 18_000,
                        "include_retrieval_diagnostics": True,
                    },
                    record_exposure=False,
                ),
                org=org,
                ctx=_ctx(),
            )

        assert started == {"context", "evidence"}
        assert response.evidence is not None
        assert response.evidence.filters["stage_timings_ms"] == {"total": 12.5}
        evidence_request = execute_search.call_args.args[0]
        assert evidence_request.types == ["session"]
        assert evidence_request.limit == 12
        assert evidence_request.content_max_chars == 18_000
        assert evidence_request.include_retrieval_diagnostics is True
        assert evidence_request.record_exposure is False

    @pytest.mark.asyncio
    async def test_context_pack_combines_concurrent_embedding_usage(self) -> None:
        class Embeddings:
            def __init__(self) -> None:
                self.calls = 0

            async def create(self, **kwargs: object) -> SimpleNamespace:
                self.calls += 1
                inputs = kwargs["input"]
                assert isinstance(inputs, list)
                await asyncio.sleep(0)
                return SimpleNamespace(
                    data=[SimpleNamespace(embedding=[0.1, 0.2]) for _ in inputs],
                    usage=SimpleNamespace(prompt_tokens=7, total_tokens=7, cost=0.0001),
                )

        embeddings = Embeddings()
        provider = CachedEmbeddingProvider(
            OpenAIEmbeddingProvider(
                metadata=EmbeddingMetadata(
                    provider="openai",
                    model="text-embedding-test",
                    dimensions=2,
                    cache_namespace="context-route-test",
                    tokenizer_estimate_method="test",
                ),
                client=SimpleNamespace(embeddings=embeddings),
            )
        )

        async def compile_pack(**_kwargs: object) -> ContextPack:
            await provider.embed_texts(["ship faster"], input_kind="query")
            return _pack()

        async def retrieve_evidence(*_args: object, **_kwargs: object) -> SearchResponse:
            await provider.embed_texts(["ship faster"], input_kind="query")
            return SearchResponse(results=[], total=0, query="ship faster", filters={})

        with (
            patch(
                "sibyl.api.routes.context.list_accessible_project_graph_ids",
                AsyncMock(return_value=["proj_1"]),
            ),
            patch("sibyl_core.tools.context.compile_context", side_effect=compile_pack),
            patch(
                "sibyl.api.routes.search.execute_search_request",
                side_effect=retrieve_evidence,
            ),
            patch(
                "sibyl.api.routes.context.configured_embedding_provider",
                return_value=provider,
            ),
        ):
            response = await context_pack(
                request=ContextPackRequest(goal="ship faster", evidence={}),
                org=SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111")),
                ctx=_ctx(),
            )

        assert embeddings.calls == 1
        assert response.evidence is not None
        assert response.evidence.filters["embedding_usage"] == {
            "provider": "openai",
            "model": "text-embedding-test",
            "requests": 1,
            "inputs": 1,
            "prompt_tokens": 7,
            "total_tokens": 7,
            "cost_reported_requests": 1,
            "cost_usd": 0.0001,
        }

    @pytest.mark.asyncio
    async def test_context_pack_accurate_evidence_preserves_query_coverage(self) -> None:
        plan = EvidenceQueryPlan(
            queries=[
                EvidenceQuery(
                    query="deployment workflow state",
                    facet=EvidenceQueryFacet.STATE,
                ),
                EvidenceQuery(
                    query="deployment final outcome",
                    facet=EvidenceQueryFacet.OUTCOME,
                ),
            ]
        )
        responses = {
            "ship faster": _search_response(
                "ship faster",
                ("shared", 0.95),
                ("original-only", 0.8),
            ),
            "deployment workflow state": _search_response(
                "deployment workflow state",
                ("shared", 0.99),
                ("state", 0.9),
            ),
            "deployment final outcome": _search_response(
                "deployment final outcome",
                ("outcome", 0.92),
            ),
        }

        async def retrieve(request: SearchRequest, **_kwargs: object) -> SearchResponse:
            await asyncio.sleep(0)
            return responses[request.query]

        with (
            patch(
                "sibyl.api.routes.context.list_accessible_project_graph_ids",
                AsyncMock(return_value=["proj_1"]),
            ),
            patch("sibyl_core.tools.context.compile_context", AsyncMock(return_value=_pack())),
            patch(
                "sibyl.api.routes.context.plan_evidence_queries",
                AsyncMock(return_value=plan),
            ) as planner,
            patch("sibyl.api.routes.search.execute_search_request", side_effect=retrieve) as search,
            patch("sibyl.api.routes.context.configured_embedding_provider", return_value=None),
        ):
            response = await context_pack(
                request=ContextPackRequest(
                    goal="ship faster",
                    evidence={"retrieval_mode": "accurate", "limit": 4},
                ),
                org=SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111")),
                ctx=_ctx(),
            )

        planner.assert_awaited_once_with("ship faster", max_queries=3)
        assert {call.args[0].query for call in search.call_args_list} == set(responses)
        assert response.evidence is not None
        assert [result.id for result in response.evidence.results] == [
            "shared",
            "state",
            "outcome",
            "original-only",
        ]
        assert response.evidence.filters["planner_status"] == "success"
        assert response.evidence.filters["query_count"] == 3
        assert response.evidence.filters["successful_query_count"] == 3
        assert response.evidence.filters["query_failures"] == []
        assert response.evidence.filters["query_filters"] == {
            "original": {"source_query": "ship faster"},
            "supplemental_1": {"source_query": "deployment workflow state"},
            "supplemental_2": {"source_query": "deployment final outcome"},
        }
        assert response.evidence.results[0].metadata["retrieval_fusion"]["sources"] == [
            "original",
            "supplemental_1",
        ]

    @pytest.mark.asyncio
    async def test_context_pack_accurate_evidence_falls_back_when_planner_fails(self) -> None:
        search = AsyncMock(return_value=_search_response("ship faster", ("original", 0.9)))

        with (
            patch(
                "sibyl.api.routes.context.list_accessible_project_graph_ids",
                AsyncMock(return_value=["proj_1"]),
            ),
            patch("sibyl_core.tools.context.compile_context", AsyncMock(return_value=_pack())),
            patch(
                "sibyl.api.routes.context.plan_evidence_queries",
                AsyncMock(side_effect=RuntimeError("planner unavailable")),
            ),
            patch("sibyl.api.routes.search.execute_search_request", search),
            patch("sibyl.api.routes.context.configured_embedding_provider", return_value=None),
        ):
            response = await context_pack(
                request=ContextPackRequest(
                    goal="ship faster",
                    evidence={"retrieval_mode": "accurate"},
                ),
                org=SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111")),
                ctx=_ctx(),
            )

        search.assert_awaited_once()
        assert response.evidence is not None
        assert [result.id for result in response.evidence.results] == ["original"]
        assert response.evidence.filters["retrieval_mode"] == "accurate"
        assert response.evidence.filters["planner_status"] == "fallback"
        assert response.evidence.filters["planner_error_type"] == "RuntimeError"
        assert response.evidence.filters["query_count"] == 1

    @pytest.mark.asyncio
    async def test_context_pack_accurate_evidence_keeps_partial_results(self) -> None:
        plan = EvidenceQueryPlan(
            queries=[
                EvidenceQuery(
                    query="deployment workflow state",
                    facet=EvidenceQueryFacet.STATE,
                )
            ]
        )

        async def retrieve(request: SearchRequest, **_kwargs: object) -> SearchResponse:
            if request.query != "ship faster":
                raise RuntimeError("supplemental search failed")
            return _search_response("ship faster", ("original", 0.9))

        with (
            patch(
                "sibyl.api.routes.context.list_accessible_project_graph_ids",
                AsyncMock(return_value=["proj_1"]),
            ),
            patch("sibyl_core.tools.context.compile_context", AsyncMock(return_value=_pack())),
            patch(
                "sibyl.api.routes.context.plan_evidence_queries",
                AsyncMock(return_value=plan),
            ),
            patch("sibyl.api.routes.search.execute_search_request", side_effect=retrieve),
            patch("sibyl.api.routes.context.configured_embedding_provider", return_value=None),
        ):
            response = await context_pack(
                request=ContextPackRequest(
                    goal="ship faster",
                    evidence={"retrieval_mode": "accurate"},
                ),
                org=SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111")),
                ctx=_ctx(),
            )

        assert response.evidence is not None
        assert [result.id for result in response.evidence.results] == ["original"]
        assert response.evidence.filters["query_count"] == 2
        assert response.evidence.filters["successful_query_count"] == 1
        assert response.evidence.filters["query_failures"] == [
            {
                "query_index": 1,
                "query": "deployment workflow state",
                "facet": "state",
                "error_type": "RuntimeError",
            }
        ]

    @pytest.mark.asyncio
    async def test_context_pack_cancels_evidence_when_context_fails(self) -> None:
        both_started = asyncio.Event()
        evidence_cancelled = asyncio.Event()

        async def compile_pack(**_kwargs: object) -> ContextPack:
            both_started.set()
            raise ValueError("invalid context request")

        async def retrieve_evidence(*_args: object, **_kwargs: object) -> SearchResponse:
            await both_started.wait()
            try:
                await asyncio.Event().wait()
            finally:
                evidence_cancelled.set()
            raise AssertionError("unreachable")

        with (
            patch(
                "sibyl.api.routes.context.list_accessible_project_graph_ids",
                AsyncMock(return_value=["proj_1"]),
            ),
            patch("sibyl_core.tools.context.compile_context", side_effect=compile_pack),
            patch(
                "sibyl.api.routes.search.execute_search_request",
                side_effect=retrieve_evidence,
            ),
            patch("sibyl.api.routes.context.configured_embedding_provider", return_value=None),
            pytest.raises(HTTPException) as exc_info,
        ):
            await context_pack(
                request=ContextPackRequest(goal="ship faster", evidence={}),
                org=SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111")),
                ctx=_ctx(),
            )

        assert exc_info.value.status_code == 400
        assert evidence_cancelled.is_set()

    @pytest.mark.asyncio
    async def test_context_pack_cancels_context_and_returns_500_when_evidence_fails(self) -> None:
        context_started = asyncio.Event()
        context_cancelled = asyncio.Event()

        async def compile_pack(**_kwargs: object) -> ContextPack:
            context_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                context_cancelled.set()
            raise AssertionError("unreachable")

        async def retrieve_evidence(*_args: object, **_kwargs: object) -> SearchResponse:
            await context_started.wait()
            raise ValueError("invalid embedding configuration")

        with (
            patch(
                "sibyl.api.routes.context.list_accessible_project_graph_ids",
                AsyncMock(return_value=["proj_1"]),
            ),
            patch("sibyl_core.tools.context.compile_context", side_effect=compile_pack),
            patch(
                "sibyl.api.routes.search.execute_search_request",
                side_effect=retrieve_evidence,
            ),
            patch("sibyl.api.routes.context.configured_embedding_provider", return_value=None),
            pytest.raises(HTTPException) as exc_info,
        ):
            await context_pack(
                request=ContextPackRequest(goal="ship faster", evidence={}),
                org=SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111")),
                ctx=_ctx(),
            )

        assert exc_info.value.status_code == 500
        assert context_cancelled.is_set()

    @pytest.mark.asyncio
    async def test_context_pack_returns_500_when_evidence_provider_is_misconfigured(self) -> None:
        compile_context = AsyncMock(return_value=_pack())

        with (
            patch(
                "sibyl.api.routes.context.list_accessible_project_graph_ids",
                AsyncMock(return_value=["proj_1"]),
            ),
            patch("sibyl_core.tools.context.compile_context", compile_context),
            patch(
                "sibyl.api.routes.context.configured_embedding_provider",
                side_effect=ValueError("unsupported provider"),
            ),
            pytest.raises(HTTPException) as exc_info,
        ):
            await context_pack(
                request=ContextPackRequest(goal="ship faster", evidence={}),
                org=SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111")),
                ctx=_ctx(),
            )

        assert exc_info.value.status_code == 500
        compile_context.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_context_pack_forwards_api_key_memory_scope_keys(self) -> None:
        org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
        ctx = SimpleNamespace(
            user_id="user-123",
            api_key_memory_scope_keys=frozenset({"private\x1fuser-123"}),
        )

        with (
            patch(
                "sibyl.api.routes.context.list_accessible_project_graph_ids",
                AsyncMock(return_value=["proj_1"]),
            ),
            patch(
                "sibyl_core.tools.context.compile_context", AsyncMock(return_value=_pack())
            ) as compile_context,
        ):
            await context_pack(
                request=ContextPackRequest(goal="ship faster"),
                org=org,
                ctx=ctx,
            )

        assert compile_context.await_args.kwargs["allowed_memory_scope_keys"] == {
            "private\x1fuser-123"
        }

    @pytest.mark.asyncio
    async def test_context_pack_preserves_quality_metadata(self) -> None:
        org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))

        with (
            patch(
                "sibyl.api.routes.context.list_accessible_project_graph_ids",
                AsyncMock(return_value=["proj_1"]),
            ),
            patch(
                "sibyl_core.tools.context.compile_context",
                AsyncMock(return_value=_pack_with_quality()),
            ),
        ):
            response = await context_pack(
                request=ContextPackRequest(goal="ship faster"),
                org=org,
                ctx=_ctx(),
            )

        item = response.sections[0].items[0]
        assert item.quality.origin == "graph"
        assert item.quality.source == "docs/architecture/SIBYL_NORTHSTAR.md"
        assert item.quality.project_id == "project-sibyl"

    @pytest.mark.asyncio
    async def test_context_pack_preserves_usage_metadata(self) -> None:
        org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))

        with (
            patch(
                "sibyl.api.routes.context.list_accessible_project_graph_ids",
                AsyncMock(return_value=["proj_1"]),
            ),
            patch(
                "sibyl_core.tools.context.compile_context",
                AsyncMock(return_value=_pack_with_usage()),
            ),
        ):
            response = await context_pack(
                request=ContextPackRequest(goal="ship faster"),
                org=org,
                ctx=_ctx(),
            )

        assert response.usage_metadata["usage_exposure"]["coverage_complete"] is True
        assert response.sections[0].items[0].metadata["usage_exposure"]["status"] == "stamped"

    @pytest.mark.asyncio
    async def test_context_pack_uses_requested_accessible_project(self) -> None:
        org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
        ctx = _ctx()

        with (
            patch(
                "sibyl.api.routes.context.list_accessible_project_graph_ids",
                AsyncMock(return_value=["proj_1"]),
            ) as list_projects,
            patch(
                "sibyl.api.routes.context.verify_entity_project_access",
                AsyncMock(),
            ) as verify_project,
            patch(
                "sibyl_core.tools.context.compile_context", AsyncMock(return_value=_pack())
            ) as compile_context,
        ):
            await context_pack(
                request=ContextPackRequest(
                    goal="ship faster",
                    project="proj_1",
                    related_limit=5,
                ),
                org=org,
                ctx=ctx,
            )

        list_projects.assert_not_awaited()
        verify_project.assert_awaited_once_with(
            None,
            ctx,
            "proj_1",
            required_role=ProjectRole.VIEWER,
        )
        assert compile_context.await_args.kwargs["project"] == "proj_1"
        assert compile_context.await_args.kwargs["accessible_projects"] == {"proj_1"}
        assert compile_context.await_args.kwargs["include_related"] is True
        assert compile_context.await_args.kwargs["related_limit"] == 5

    @pytest.mark.asyncio
    async def test_context_pack_passes_requested_layer(self) -> None:
        org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))

        with (
            patch(
                "sibyl.api.routes.context.list_accessible_project_graph_ids",
                AsyncMock(return_value=["proj_1"]),
            ),
            patch(
                "sibyl_core.tools.context.compile_context", AsyncMock(return_value=_pack())
            ) as compile_context,
        ):
            await context_pack(
                request=ContextPackRequest(goal="ship faster", layer=ContextLayer.WAKE),
                org=org,
                ctx=_ctx(),
            )

        assert compile_context.await_args.kwargs["layer"] == ContextLayer.WAKE

    @pytest.mark.asyncio
    async def test_context_pack_audits_render_receipt(
        self,
        context_audit_event: AsyncMock,
    ) -> None:
        org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
        ctx = _ctx()

        with (
            patch(
                "sibyl.api.routes.context.verify_entity_project_access",
                AsyncMock(),
            ),
            patch(
                "sibyl_core.tools.context.compile_context",
                AsyncMock(
                    return_value=_pack_with_quality(
                        layer=ContextLayer.WAKE,
                        project="proj_1",
                    )
                ),
            ),
        ):
            http_request = _http_request()
            response = await context_pack(
                request=ContextPackRequest(
                    goal="ship faster",
                    project="proj_1",
                    layer=ContextLayer.WAKE,
                    agent_id="nova",
                    limit=8,
                    include_related=False,
                    related_limit=0,
                ),
                http_request=http_request,
                org=org,
                ctx=ctx,
            )

        assert response.layer == ContextLayer.WAKE
        context_audit_event.assert_awaited_once()
        kwargs = context_audit_event.await_args.kwargs
        assert kwargs["action"] == "memory.context_pack"
        assert kwargs["user_id"] == "user-123"
        assert kwargs["organization_id"] == "00000000-0000-0000-0000-000000000111"
        assert kwargs["request"] is http_request
        assert kwargs["memory_scope"] == "project"
        assert kwargs["scope_key"] == "proj_1"
        assert kwargs["project_id"] == "proj_1"
        assert kwargs["source_surface"] == "context_pack"
        assert kwargs["source_ids"] == [
            "Northstar",
            "docs/architecture/SIBYL_NORTHSTAR.md",
        ]
        assert kwargs["derived_ids"] == ["decision_1"]
        assert kwargs["policy_allowed"] is True
        assert kwargs["policy_reason"] == "context_pack_rendered"
        assert kwargs["details"] == {
            "agent_id": "nova",
            "domain": None,
            "goal_length": 11,
            "include_related": False,
            "intent": "build",
            "layer": "wake",
            "limit": 8,
            "related_limit": 0,
            "result_count": 1,
            "section_count": 1,
            "accessible_project_count": 1,
        }

    @pytest.mark.asyncio
    async def test_context_pack_audits_unscoped_mixed_receipt(
        self,
        context_audit_event: AsyncMock,
    ) -> None:
        org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))

        with (
            patch(
                "sibyl.api.routes.context.list_accessible_project_graph_ids",
                AsyncMock(return_value=["proj_1", "proj_2"]),
            ),
            patch("sibyl_core.tools.context.compile_context", AsyncMock(return_value=_pack())),
        ):
            await context_pack(
                request=ContextPackRequest(goal="ship faster"),
                org=org,
                ctx=_ctx(),
            )

        context_audit_event.assert_awaited_once()
        kwargs = context_audit_event.await_args.kwargs
        assert kwargs["memory_scope"] == "mixed"
        assert kwargs["scope_key"] is None
        assert kwargs["project_id"] is None
        assert kwargs["source_surface"] == "context_pack"
        assert kwargs["source_ids"] == []
        assert kwargs["derived_ids"] == []
        assert kwargs["details"]["layer"] == "recall"
        assert kwargs["details"]["accessible_project_count"] == 2

    @pytest.mark.asyncio
    async def test_context_pack_passes_agent_id(self) -> None:
        org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))

        with (
            patch(
                "sibyl.api.routes.context.list_accessible_project_graph_ids",
                AsyncMock(return_value=["proj_1"]),
            ),
            patch(
                "sibyl_core.tools.context.compile_context", AsyncMock(return_value=_pack())
            ) as compile_context,
        ):
            await context_pack(
                request=ContextPackRequest(goal="ship faster", agent_id="nova"),
                org=org,
                ctx=_ctx(),
            )

        assert compile_context.await_args.kwargs["agent_id"] == "nova"

    @pytest.mark.asyncio
    async def test_context_pack_rejects_inaccessible_project(self) -> None:
        org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
        ctx = _ctx()

        with (
            patch(
                "sibyl.api.routes.context.list_accessible_project_graph_ids",
                AsyncMock(return_value=["proj_1"]),
            ) as list_projects,
            patch(
                "sibyl.api.routes.context.verify_entity_project_access",
                AsyncMock(
                    side_effect=ProjectAccessDeniedError(
                        project_id="proj_2",
                        required_role="viewer",
                    )
                ),
            ) as verify_project,
            patch("sibyl_core.tools.context.compile_context", AsyncMock()) as compile_context,
            pytest.raises(ProjectAccessDeniedError) as exc,
        ):
            await context_pack(
                request=ContextPackRequest(goal="ship faster", project="proj_2"),
                org=org,
                ctx=ctx,
            )

        list_projects.assert_not_awaited()
        verify_project.assert_awaited_once_with(
            None,
            ctx,
            "proj_2",
            required_role=ProjectRole.VIEWER,
        )
        compile_context.assert_not_awaited()
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_context_pack_audits_project_access_denial(
        self,
        context_audit_event: AsyncMock,
    ) -> None:
        org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
        ctx = _ctx()
        http_request = _http_request()

        with (
            patch(
                "sibyl.api.routes.context.verify_entity_project_access",
                AsyncMock(
                    side_effect=ProjectAuthorizationError(
                        project_id="proj_2",
                        required_role=ProjectRole.VIEWER,
                        actual_role=None,
                    )
                ),
            ),
            patch("sibyl_core.tools.context.compile_context", AsyncMock()) as compile_context,
            pytest.raises(ProjectAuthorizationError),
        ):
            await context_pack(
                request=ContextPackRequest(goal="ship faster", project="proj_2"),
                http_request=http_request,
                org=org,
                ctx=ctx,
            )

        compile_context.assert_not_awaited()
        context_audit_event.assert_awaited_once()
        kwargs = context_audit_event.await_args.kwargs
        assert kwargs["action"] == "memory.context_pack.deny"
        assert kwargs["user_id"] == "user-123"
        assert kwargs["organization_id"] == "00000000-0000-0000-0000-000000000111"
        assert kwargs["request"] is http_request
        assert kwargs["memory_scope"] == "project"
        assert kwargs["scope_key"] == "proj_2"
        assert kwargs["project_id"] == "proj_2"
        assert kwargs["source_surface"] == "context_pack"
        assert kwargs["source_ids"] == []
        assert kwargs["derived_ids"] == []
        assert kwargs["policy_allowed"] is False
        assert kwargs["policy_reason"] == "project_access_denied"
        assert kwargs["details"] == {
            "requested_project_id": "proj_2",
            "route_action": "context_pack",
        }

    @pytest.mark.asyncio
    async def test_context_pack_audit_failure_keeps_project_denial_closed(
        self,
        context_audit_event: AsyncMock,
    ) -> None:
        org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
        ctx = _ctx()
        context_audit_event.side_effect = RuntimeError("audit backend unavailable")

        with (
            patch(
                "sibyl.api.routes.context.verify_entity_project_access",
                AsyncMock(
                    side_effect=ProjectAuthorizationError(
                        project_id="proj_2",
                        required_role=ProjectRole.VIEWER,
                        actual_role=None,
                    )
                ),
            ),
            patch("sibyl_core.tools.context.compile_context", AsyncMock()) as compile_context,
            pytest.raises(ProjectAuthorizationError) as exc,
        ):
            await context_pack(
                request=ContextPackRequest(goal="ship faster", project="proj_2"),
                org=org,
                ctx=ctx,
            )

        compile_context.assert_not_awaited()
        context_audit_event.assert_awaited_once()
        assert exc.value.status_code == 403


def _reflection_pack(
    *,
    project: str | None = "proj_1",
    source_id: str | None = "session_1",
    persisted_id: str | None = None,
    raw_source_ids: list[str] | None = None,
    metadata: dict[str, object] | None = None,
) -> ReflectionPack:
    return ReflectionPack(
        source_title="Planning",
        source_id=source_id,
        intent="build",
        domain="sibyl",
        project=project,
        candidates=[
            ReflectionCandidate(
                kind="decision",
                title="Decision: Use reflect",
                content="We decided to add reflect.",
                reason="captures a choice",
                confidence=0.86,
                metadata=metadata or {},
                raw_source_ids=raw_source_ids or [],
                persisted_id=persisted_id,
            )
        ],
        total_candidates=1,
        persisted_count=1 if persisted_id else 0,
    )


class TestReflectRoute:
    @pytest.mark.asyncio
    async def test_reflect_scopes_to_accessible_project(self) -> None:
        from sibyl.api.routes.context import reflect_context

        org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
        ctx = _ctx()

        with (
            patch(
                "sibyl.api.routes.context.list_accessible_project_graph_ids",
                AsyncMock(return_value=["proj_1"]),
            ) as list_projects,
            patch(
                "sibyl.api.routes.context.verify_entity_project_access",
                AsyncMock(),
            ) as verify_project,
            patch(
                "sibyl_core.tools.core.reflect_memory",
                AsyncMock(return_value=_reflection_pack()),
            ) as reflect_memory,
            patch(
                "sibyl_core.tools.core.explore",
                AsyncMock(return_value=SimpleNamespace(entities=[])),
            ) as explore,
        ):
            response = await reflect_context(
                request=ReflectionRequest(
                    content="We decided to add reflect.",
                    source_title="Planning",
                    intent=ContextIntent.BUILD,
                    project="proj_1",
                    persist=True,
                ),
                org=org,
                ctx=ctx,
            )

        list_projects.assert_not_awaited()
        verify_project.assert_awaited_once_with(
            None,
            ctx,
            "proj_1",
            required_role=ProjectRole.CONTRIBUTOR,
        )
        assert response.source_title == "Planning"
        assert response.source_id == "session_1"
        assert response.markdown is not None
        assert response.persisted_count == 0
        assert reflect_memory.await_args.kwargs["organization_id"] == str(org.id)
        assert reflect_memory.await_args.kwargs["project"] == "proj_1"
        assert reflect_memory.await_args.kwargs["related_to"] is None
        assert reflect_memory.await_args.kwargs["principal_id"] == "user-123"
        assert reflect_memory.await_args.kwargs["accessible_projects"] == {"proj_1"}
        assert reflect_memory.await_args.kwargs["memory_scope"] == "project"
        assert reflect_memory.await_args.kwargs["scope_key"] == "proj_1"
        assert reflect_memory.await_args.kwargs["persist"] is True
        assert reflect_memory.await_args.kwargs["persist_source"] is True
        assert reflect_memory.await_args.kwargs["persist_review"] is False
        explore.assert_awaited_once_with(
            mode="list",
            types=["task"],
            project="proj_1",
            status="doing",
            limit=2,
            organization_id=str(org.id),
        )

    @pytest.mark.asyncio
    async def test_reflect_records_cited_memories(self) -> None:
        from sibyl.api.routes.context import reflect_context

        org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
        ctx = _ctx()
        record_citations = AsyncMock(
            return_value={
                "cited_count": 2,
                "coverage_complete": True,
                "stamped_count": 2,
            }
        )

        with (
            patch(
                "sibyl.api.routes.context.list_accessible_project_graph_ids",
                AsyncMock(return_value=["proj_1"]),
            ),
            patch("sibyl.api.routes.context.verify_entity_project_access", AsyncMock()),
            patch(
                "sibyl_core.tools.core.reflect_memory",
                AsyncMock(return_value=_reflection_pack(project="proj_1")),
            ),
            patch(
                "sibyl_core.tools.core.explore",
                AsyncMock(return_value=SimpleNamespace(entities=[])),
            ),
            patch(
                "sibyl_core.tools.usage_citation.record_cited_item_usages",
                record_citations,
            ),
        ):
            response = await reflect_context(
                request=ReflectionRequest(
                    content="We decided to add reflect.",
                    source_title="Planning",
                    intent=ContextIntent.BUILD,
                    project="proj_1",
                    cited_ids=["decision-1", "raw_memory:raw-1"],
                ),
                org=org,
                ctx=ctx,
            )

        record_citations.assert_awaited_once_with(
            ["decision-1", "raw_memory:raw-1"],
            organization_id=str(org.id),
            principal_id="user-123",
            project_id="proj_1",
            source_surface="context_reflect",
            request_metadata={
                "intent": "build",
                "persist": False,
                "source_title": "Planning",
            },
        )
        assert response.citation_usage["stamped_count"] == 2

    @pytest.mark.asyncio
    async def test_reflect_response_includes_structured_extraction_receipts(self) -> None:
        from sibyl.api.routes.context import reflect_context

        org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
        candidate = ReflectionCandidate(
            kind="claim",
            title="Claim: Reflection receipts are source-grounded",
            content="Reflection receipts are source-grounded.",
            reason="captures a sourced assertion",
            confidence=0.91,
            raw_source_ids=["raw_1"],
            claim_records=[
                ClaimRecord(
                    title="Claim: Reflection receipts are source-grounded",
                    content="Reflection receipts are source-grounded.",
                    confidence=0.91,
                    source_ids=["raw_1"],
                )
            ],
            reflection_findings=[
                ReflectionFinding(
                    kind=ReflectionFindingKind.CLAIM,
                    target_source_id="raw_1",
                    reason="captures a sourced assertion",
                    confidence=0.91,
                    source_ids=["raw_1"],
                )
            ],
            relationship_records=[
                ReflectionRelationshipRecord(
                    source_id="candidate:0",
                    target_id="proj_1",
                    relationship_type="BELONGS_TO",
                    reason="candidate was reflected in project scope",
                    source_ids=["raw_1"],
                )
            ],
        )
        pack = ReflectionPack(
            source_title="Planning",
            source_id="raw_1",
            intent="build",
            domain="sibyl",
            project="proj_1",
            candidates=[candidate],
            total_candidates=1,
            persisted_count=0,
        )

        with (
            patch(
                "sibyl.api.routes.context.verify_entity_project_access",
                AsyncMock(),
            ),
            patch(
                "sibyl_core.tools.core.reflect_memory",
                AsyncMock(return_value=pack),
            ),
            patch(
                "sibyl_core.tools.core.explore",
                AsyncMock(return_value=SimpleNamespace(entities=[])),
            ),
        ):
            response = await reflect_context(
                request=ReflectionRequest(
                    content="Reflection receipts are source-grounded.",
                    source_title="Planning",
                    intent=ContextIntent.BUILD,
                    project="proj_1",
                    persist=True,
                ),
                org=org,
                ctx=_ctx(),
            )

        reflected = response.candidates[0]
        assert reflected.claim_records[0]["source_ids"] == ["raw_1"]
        assert reflected.reflection_findings[0]["kind"] == "claim"
        assert reflected.relationship_records[0]["target_id"] == "proj_1"

    @pytest.mark.asyncio
    async def test_reflect_links_explicit_and_single_active_task(self) -> None:
        from sibyl.api.routes.context import reflect_context

        org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
        ctx = _ctx()
        explore = AsyncMock(return_value=SimpleNamespace(entities=[SimpleNamespace(id="task_2")]))

        with (
            patch(
                "sibyl.api.routes.context.list_accessible_project_graph_ids",
                AsyncMock(return_value=["proj_1"]),
            ) as list_projects,
            patch(
                "sibyl.api.routes.context.verify_entity_project_access",
                AsyncMock(),
            ) as verify_project,
            patch(
                "sibyl_core.tools.core.reflect_memory",
                AsyncMock(return_value=_reflection_pack()),
            ) as reflect_memory,
            patch("sibyl_core.tools.core.explore", explore),
        ):
            await reflect_context(
                request=ReflectionRequest(
                    content="We decided to add reflect.",
                    source_title="Planning",
                    intent=ContextIntent.BUILD,
                    project="proj_1",
                    related_to=["plan_1"],
                    task_ids=["task_1", "plan_1"],
                    persist=True,
                ),
                org=org,
                ctx=ctx,
            )

        list_projects.assert_not_awaited()
        verify_project.assert_awaited_once_with(
            None,
            ctx,
            "proj_1",
            required_role=ProjectRole.CONTRIBUTOR,
        )
        assert reflect_memory.await_args.kwargs["related_to"] == ["plan_1", "task_1", "task_2"]
        assert reflect_memory.await_args.kwargs["principal_id"] == "user-123"
        assert reflect_memory.await_args.kwargs["accessible_projects"] == {"proj_1"}
        assert reflect_memory.await_args.kwargs["memory_scope"] == "project"
        assert reflect_memory.await_args.kwargs["scope_key"] == "proj_1"
        explore.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_reflect_can_request_review_queue_persistence(self) -> None:
        from sibyl.api.routes.context import reflect_context

        org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
        ctx = _ctx()

        with (
            patch(
                "sibyl.api.routes.context.verify_entity_project_access",
                AsyncMock(),
            ),
            patch(
                "sibyl_core.tools.core.reflect_memory",
                AsyncMock(return_value=_reflection_pack()),
            ) as reflect_memory,
            patch(
                "sibyl_core.tools.core.explore",
                AsyncMock(return_value=SimpleNamespace(entities=[])),
            ),
        ):
            await reflect_context(
                request=ReflectionRequest(
                    content="We decided to add reflect.",
                    project="proj_1",
                    persist=True,
                    persist_review=True,
                ),
                org=org,
                ctx=ctx,
            )

        assert reflect_memory.await_args.kwargs["persist"] is True
        assert reflect_memory.await_args.kwargs["persist_review"] is True

    @pytest.mark.asyncio
    async def test_reflect_audits_render_receipt(
        self,
        context_audit_event: AsyncMock,
    ) -> None:
        from sibyl.api.routes.context import reflect_context

        org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
        http_request = _http_request()

        with (
            patch(
                "sibyl.api.routes.context.verify_entity_project_access",
                AsyncMock(),
            ),
            patch(
                "sibyl_core.tools.core.reflect_memory",
                AsyncMock(
                    return_value=_reflection_pack(
                        persisted_id="decision_1",
                        raw_source_ids=["raw_1"],
                        metadata={
                            "policy_allowed": True,
                            "policy_reasons": ["same_scope_write_allowed"],
                        },
                    )
                ),
            ),
            patch(
                "sibyl_core.tools.core.explore",
                AsyncMock(return_value=SimpleNamespace(entities=[])),
            ),
        ):
            response = await reflect_context(
                request=ReflectionRequest(
                    content="We decided to add reflect.",
                    source_title="Planning",
                    intent=ContextIntent.BUILD,
                    project="proj_1",
                    persist=True,
                    persist_review=True,
                ),
                http_request=http_request,
                org=org,
                ctx=_ctx(),
            )

        assert response.persisted_count == 1
        context_audit_event.assert_awaited_once()
        kwargs = context_audit_event.await_args.kwargs
        assert kwargs["action"] == "memory.reflect"
        assert kwargs["user_id"] == "user-123"
        assert kwargs["organization_id"] == "00000000-0000-0000-0000-000000000111"
        assert kwargs["request"] is http_request
        assert kwargs["memory_scope"] == "project"
        assert kwargs["scope_key"] == "proj_1"
        assert kwargs["project_id"] == "proj_1"
        assert kwargs["source_surface"] == "context_reflect"
        assert kwargs["source_ids"] == ["session_1", "raw_1"]
        assert kwargs["derived_ids"] == ["decision_1"]
        assert kwargs["policy_allowed"] is True
        assert kwargs["policy_reason"] == "same_scope_write_allowed"
        assert kwargs["details"]["candidate_count"] == 1
        assert kwargs["details"]["persist"] is True
        assert kwargs["details"]["persist_review"] is True
        assert kwargs["details"]["persisted_count"] == 1
        assert kwargs["details"]["source_title_length"] == 8
        assert kwargs["details"]["accessible_project_count"] == 1

    @pytest.mark.asyncio
    async def test_reflect_audits_render_only_receipt(
        self,
        context_audit_event: AsyncMock,
    ) -> None:
        from sibyl.api.routes.context import reflect_context

        org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))

        with (
            patch(
                "sibyl.api.routes.context.list_accessible_project_graph_ids",
                AsyncMock(return_value=["proj_1"]),
            ),
            patch(
                "sibyl_core.tools.core.reflect_memory",
                AsyncMock(return_value=_reflection_pack(project=None, source_id=None)),
            ),
        ):
            await reflect_context(
                request=ReflectionRequest(
                    content="We decided to add reflect.",
                    source_title="Planning",
                    intent=ContextIntent.BUILD,
                    persist=False,
                ),
                org=org,
                ctx=_ctx(),
            )

        context_audit_event.assert_awaited_once()
        kwargs = context_audit_event.await_args.kwargs
        assert kwargs["memory_scope"] == "private"
        assert kwargs["scope_key"] is None
        assert kwargs["project_id"] is None
        assert kwargs["source_surface"] == "context_reflect"
        assert kwargs["source_ids"] == []
        assert kwargs["derived_ids"] == []
        assert kwargs["policy_allowed"] is True
        assert kwargs["policy_reason"] == "reflection_rendered"
        assert kwargs["details"]["persist"] is False
        assert kwargs["details"]["accessible_project_count"] == 1

    @pytest.mark.asyncio
    async def test_reflect_audits_persist_policy_denial(
        self,
        context_audit_event: AsyncMock,
    ) -> None:
        from sibyl.api.routes.context import reflect_context

        org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))

        with (
            patch(
                "sibyl.api.routes.context.verify_entity_project_access",
                AsyncMock(),
            ),
            patch(
                "sibyl_core.tools.core.reflect_memory",
                AsyncMock(
                    return_value=_reflection_pack(
                        source_id=None,
                        metadata={
                            "policy_allowed": False,
                            "policy_reasons": [
                                "unverified_membership",
                                "scope_not_enabled",
                            ],
                        },
                    )
                ),
            ),
            patch(
                "sibyl_core.tools.core.explore",
                AsyncMock(return_value=SimpleNamespace(entities=[])),
            ),
        ):
            await reflect_context(
                request=ReflectionRequest(
                    content="We decided to add reflect.",
                    source_title="Planning",
                    intent=ContextIntent.BUILD,
                    project="proj_1",
                    persist=True,
                ),
                org=org,
                ctx=_ctx(),
            )

        context_audit_event.assert_awaited_once()
        kwargs = context_audit_event.await_args.kwargs
        assert kwargs["memory_scope"] == "project"
        assert kwargs["scope_key"] == "proj_1"
        assert kwargs["source_ids"] == []
        assert kwargs["derived_ids"] == []
        assert kwargs["policy_allowed"] is False
        assert kwargs["policy_reason"] == "unverified_membership,scope_not_enabled"
        assert kwargs["details"]["persist"] is True
        assert kwargs["details"]["persisted_count"] == 0

    @pytest.mark.asyncio
    async def test_reflect_skips_active_task_lookup_when_not_persisting(self) -> None:
        from sibyl.api.routes.context import reflect_context

        org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
        ctx = _ctx()

        with (
            patch(
                "sibyl.api.routes.context.list_accessible_project_graph_ids",
                AsyncMock(return_value=["proj_1"]),
            ) as list_projects,
            patch(
                "sibyl.api.routes.context.verify_entity_project_access",
                AsyncMock(),
            ) as verify_project,
            patch(
                "sibyl_core.tools.core.reflect_memory",
                AsyncMock(return_value=_reflection_pack()),
            ) as reflect_memory,
            patch("sibyl_core.tools.core.explore", AsyncMock()) as explore,
        ):
            await reflect_context(
                request=ReflectionRequest(
                    content="We decided to add reflect.",
                    source_title="Planning",
                    intent=ContextIntent.BUILD,
                    project="proj_1",
                    task_ids=["task_1"],
                    persist=False,
                ),
                org=org,
                ctx=ctx,
            )

        list_projects.assert_not_awaited()
        verify_project.assert_awaited_once_with(
            None,
            ctx,
            "proj_1",
            required_role=ProjectRole.VIEWER,
        )
        assert reflect_memory.await_args.kwargs["related_to"] == ["task_1"]
        assert reflect_memory.await_args.kwargs["principal_id"] == "user-123"
        assert reflect_memory.await_args.kwargs["accessible_projects"] == {"proj_1"}
        assert reflect_memory.await_args.kwargs["memory_scope"] == "project"
        assert reflect_memory.await_args.kwargs["scope_key"] == "proj_1"
        explore.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_reflect_skips_active_task_lookup_without_project(self) -> None:
        from sibyl.api.routes.context import reflect_context

        org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))

        with (
            patch(
                "sibyl.api.routes.context.list_accessible_project_graph_ids",
                AsyncMock(return_value=["proj_1"]),
            ),
            patch(
                "sibyl_core.tools.core.reflect_memory",
                AsyncMock(return_value=_reflection_pack()),
            ) as reflect_memory,
            patch("sibyl_core.tools.core.explore", AsyncMock()) as explore,
        ):
            await reflect_context(
                request=ReflectionRequest(
                    content="We decided to add reflect.",
                    source_title="Planning",
                    intent=ContextIntent.BUILD,
                    task_ids=["task_1"],
                    persist=True,
                ),
                org=org,
                ctx=_ctx(),
            )

        assert reflect_memory.await_args.kwargs["related_to"] == ["task_1"]
        assert reflect_memory.await_args.kwargs["principal_id"] == "user-123"
        assert reflect_memory.await_args.kwargs["accessible_projects"] == {"proj_1"}
        assert reflect_memory.await_args.kwargs["memory_scope"] == "private"
        assert reflect_memory.await_args.kwargs["scope_key"] is None
        explore.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_reflect_rejects_inaccessible_project(self) -> None:
        from sibyl.api.routes.context import reflect_context

        org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
        ctx = _ctx()

        with (
            patch(
                "sibyl.api.routes.context.list_accessible_project_graph_ids",
                AsyncMock(return_value=["proj_1"]),
            ) as list_projects,
            patch(
                "sibyl.api.routes.context.verify_entity_project_access",
                AsyncMock(
                    side_effect=ProjectAccessDeniedError(
                        project_id="proj_2",
                        required_role="viewer",
                    )
                ),
            ) as verify_project,
            patch("sibyl_core.tools.core.reflect_memory", AsyncMock()) as reflect_memory,
            pytest.raises(ProjectAccessDeniedError) as exc,
        ):
            await reflect_context(
                request=ReflectionRequest(content="notes", project="proj_2"),
                org=org,
                ctx=ctx,
            )

        list_projects.assert_not_awaited()
        verify_project.assert_awaited_once_with(
            None,
            ctx,
            "proj_2",
            required_role=ProjectRole.VIEWER,
        )
        reflect_memory.assert_not_awaited()
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_reflect_audits_project_access_denial(
        self,
        context_audit_event: AsyncMock,
    ) -> None:
        from sibyl.api.routes.context import reflect_context

        org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
        ctx = _ctx()
        http_request = _http_request()

        with (
            patch(
                "sibyl.api.routes.context.verify_entity_project_access",
                AsyncMock(
                    side_effect=ProjectAuthorizationError(
                        project_id="proj_2",
                        required_role=ProjectRole.VIEWER,
                        actual_role=None,
                    )
                ),
            ),
            patch("sibyl_core.tools.core.reflect_memory", AsyncMock()) as reflect_memory,
            pytest.raises(ProjectAuthorizationError),
        ):
            await reflect_context(
                request=ReflectionRequest(content="notes", project="proj_2"),
                http_request=http_request,
                org=org,
                ctx=ctx,
            )

        reflect_memory.assert_not_awaited()
        context_audit_event.assert_awaited_once()
        kwargs = context_audit_event.await_args.kwargs
        assert kwargs["action"] == "memory.reflect.deny"
        assert kwargs["user_id"] == "user-123"
        assert kwargs["organization_id"] == "00000000-0000-0000-0000-000000000111"
        assert kwargs["request"] is http_request
        assert kwargs["memory_scope"] == "project"
        assert kwargs["scope_key"] == "proj_2"
        assert kwargs["project_id"] == "proj_2"
        assert kwargs["source_surface"] == "context_reflect"
        assert kwargs["source_ids"] == []
        assert kwargs["derived_ids"] == []
        assert kwargs["policy_allowed"] is False
        assert kwargs["policy_reason"] == "project_access_denied"
        assert kwargs["details"] == {
            "requested_project_id": "proj_2",
            "route_action": "context_reflect",
        }
