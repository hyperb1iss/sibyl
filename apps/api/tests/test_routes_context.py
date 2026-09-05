from __future__ import annotations

import asyncio
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any
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
        assert compile_context.await_args.kwargs["retrieval_query"] == "ship faster"
        assert compile_context.await_args.kwargs["layer"] == ContextLayer.RECALL
        assert compile_context.await_args.kwargs["principal_id"] == "user-123"
        assert compile_context.await_args.kwargs["agent_id"] is None
        assert compile_context.await_args.kwargs["project"] is None
        assert compile_context.await_args.kwargs["include_related"] is True
        assert compile_context.await_args.kwargs["related_limit"] == 3
        assert compile_context.await_args.kwargs["record_exposure"] is True
        assert compile_context.await_args.kwargs["include_documents"] is True

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
        all_started = asyncio.Event()

        async def compile_pack(**_kwargs: object) -> ContextPack:
            started.add("context")
            if len(started) == 3:
                all_started.set()
            await asyncio.wait_for(all_started.wait(), timeout=1)
            return _pack()

        async def retrieve_evidence(
            search_request: SearchRequest,
            **_kwargs: object,
        ) -> SearchResponse:
            started.add("typed" if search_request.types == ["note"] else "raw")
            if len(started) == 3:
                all_started.set()
            await asyncio.wait_for(all_started.wait(), timeout=1)
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

        assert started == {"context", "raw", "typed"}
        assert response.evidence is not None
        assert response.evidence.filters["stage_timings_ms"] == {"total": 12.5}
        evidence_requests = [call.args[0] for call in execute_search.call_args_list]
        raw_request = next(item for item in evidence_requests if item.types == ["session"])
        typed_request = next(item for item in evidence_requests if item.types == ["note"])
        assert raw_request.limit == 12
        assert raw_request.content_max_chars == 18_000
        assert raw_request.include_retrieval_diagnostics is True
        assert raw_request.record_exposure is False
        assert typed_request.category == "operational_distillation"
        assert typed_request.include_raw_memory is False
        assert typed_request.record_exposure is False

    @pytest.mark.asyncio
    async def test_context_pack_reserves_three_of_eight_slots_for_distilled_notes(self) -> None:
        raw_response = _search_response(
            "ship faster",
            *((f"raw-{index}", 1.0 - index / 100) for index in range(8)),
        )
        typed_response = SearchResponse(
            results=[
                SearchResult(
                    id=f"note-{index}",
                    type="note",
                    name=f"note-{index}",
                    content=f"distilled evidence {index}",
                    score=1.0 - index / 100,
                    metadata={
                        "projection_kind": "distilled_note",
                        "operational_source_id": f"capture-{index}",
                    },
                )
                for index in range(4)
            ],
            total=4,
            query="ship faster",
            filters={"typed": True},
            graph_count=4,
            limit=8,
        )

        async def retrieve(
            search_request: SearchRequest,
            **_kwargs: object,
        ) -> SearchResponse:
            return typed_response if search_request.types == ["note"] else raw_response

        async def record_exposures(
            results: list[SearchResult],
            **_kwargs: object,
        ) -> dict[str, object]:
            for result in results:
                result.metadata["usage_exposure"] = {"status": "stamped"}
            return {"stamped_count": len(results), "coverage_complete": True}

        exposure = AsyncMock(side_effect=record_exposures)
        with (
            patch(
                "sibyl.api.routes.context.list_accessible_project_graph_ids",
                AsyncMock(return_value=["proj_1"]),
            ),
            patch("sibyl_core.tools.context.compile_context", AsyncMock(return_value=_pack())),
            patch("sibyl.api.routes.search.execute_search_request", side_effect=retrieve) as search,
            patch("sibyl.api.routes.context.configured_embedding_provider", return_value=None),
            patch(
                "sibyl_core.tools.usage_exposure.annotate_search_result_exposures",
                exposure,
            ),
        ):
            response = await context_pack(
                request=ContextPackRequest(
                    goal="ship faster",
                    evidence={"types": ["session"], "limit": 8},
                ),
                org=SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111")),
                ctx=_ctx(),
            )

        assert response.evidence is not None
        assert [result.id for result in response.evidence.results] == [
            "note-0",
            "note-1",
            "note-2",
            "raw-0",
            "raw-1",
            "raw-2",
            "raw-3",
            "raw-4",
        ]
        receipt = response.evidence.filters["evidence_composition"]
        assert receipt["typed_reservation"] == 3
        assert receipt["selected_typed_count"] == 3
        assert receipt["selected_raw_count"] == 5
        assert receipt["typed_search_status"] == "success"
        assert "activity_receipt" not in receipt
        assert response.evidence.filters["usage_exposure"] == {
            "stamped_count": 8,
            "coverage_complete": True,
        }
        assert [result.id for result in exposure.await_args.args[0]] == [
            result.id for result in response.evidence.results
        ]
        assert all(call.args[0].record_exposure is False for call in search.call_args_list)

    @pytest.mark.asyncio
    async def test_treatment_composition_receipts_source_kind_dedupe_and_raw_parity(
        self,
    ) -> None:
        raw_response = _search_response(
            "ship faster",
            *((f"raw-{index}", 1.0 - index / 100) for index in range(5)),
        )
        typed_response = SearchResponse(
            results=[
                SearchResult(
                    id=result_id,
                    type="note",
                    name=result_id,
                    content=f"distilled evidence {result_id}",
                    score=1.0 - index / 100,
                    metadata={
                        "projection_kind": "distilled_note",
                        "operational_source_id": source_id,
                        "note_kind": note_kind,
                    },
                )
                for index, (result_id, source_id, note_kind) in enumerate(
                    (
                        ("workflow-a", "capture-a", "workflow"),
                        ("facts-a", "capture-a", "facts"),
                        ("workflow-a-copy", "capture-a", "workflow"),
                        ("workflow-b", "capture-b", "workflow"),
                    )
                )
            ],
            total=4,
            query="ship faster",
            filters={"typed": True},
            graph_count=4,
            limit=5,
        )

        async def retrieve(
            search_request: SearchRequest,
            **_kwargs: object,
        ) -> SearchResponse:
            return typed_response if search_request.types == ["note"] else raw_response

        with (
            patch(
                "sibyl.api.routes.context.list_accessible_project_graph_ids",
                AsyncMock(return_value=["proj_1"]),
            ),
            patch("sibyl_core.tools.context.compile_context", AsyncMock(return_value=_pack())),
            patch("sibyl.api.routes.search.execute_search_request", side_effect=retrieve),
            patch("sibyl.api.routes.context.configured_embedding_provider", return_value=None),
        ):
            response = await context_pack(
                request=ContextPackRequest(
                    goal="ship faster",
                    evidence={
                        "types": ["session"],
                        "limit": 5,
                        "char_budget": 500,
                        "operational_note_dedupe_mode": "source_kind",
                        "operational_note_lane_mode": "additive",
                    },
                    record_exposure=False,
                ),
                org=SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111")),
                ctx=_ctx(),
            )

        assert response.evidence is not None
        assert [result.id for result in response.evidence.results] == [
            "workflow-a",
            "facts-a",
            "workflow-b",
            "raw-0",
            "raw-1",
            "raw-2",
            "raw-3",
            "raw-4",
        ]
        activity = response.evidence.filters["evidence_composition"]["activity_receipt"]
        assert activity["note_dedupe"] == {
            "mode": "source_kind",
            "duplicate_source_count": 2,
            "duplicate_source_kind_count": 1,
        }
        assert activity["additive_note_lane"]["admitted_note_ids"] == [
            "workflow-a",
            "facts-a",
            "workflow-b",
        ]
        assert activity["raw_parity"]["membership_equal"] is True
        assert activity["raw_parity"]["order_equal"] is True

    @pytest.mark.asyncio
    async def test_context_pack_degrades_to_raw_when_distilled_search_fails(self) -> None:
        raw_response = _search_response(
            "ship faster",
            ("raw-0", 0.9),
            ("raw-1", 0.8),
        )

        async def retrieve(
            search_request: SearchRequest,
            **_kwargs: object,
        ) -> SearchResponse:
            if search_request.types == ["note"]:
                raise RuntimeError("typed search unavailable")
            return raw_response

        with (
            patch(
                "sibyl.api.routes.context.list_accessible_project_graph_ids",
                AsyncMock(return_value=["proj_1"]),
            ),
            patch("sibyl_core.tools.context.compile_context", AsyncMock(return_value=_pack())),
            patch("sibyl.api.routes.search.execute_search_request", side_effect=retrieve),
            patch("sibyl.api.routes.context.configured_embedding_provider", return_value=None),
        ):
            response = await context_pack(
                request=ContextPackRequest(
                    goal="ship faster",
                    evidence={"types": ["session"], "limit": 8},
                    record_exposure=False,
                ),
                org=SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111")),
                ctx=_ctx(),
            )

        assert response.evidence is not None
        assert [result.id for result in response.evidence.results] == ["raw-0", "raw-1"]
        receipt = response.evidence.filters["evidence_composition"]
        assert receipt["typed_search_status"] == "degraded"
        assert receipt["typed_search_error_type"] == "RuntimeError"

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

        async def compile_pack(**kwargs: object) -> ContextPack:
            assert kwargs["include_documents"] is False
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
            principal_id=ctx.user_id,
            accessible_projects={"proj_1"},
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


def test_context_pack_request_defaults_to_a_markdown_budget() -> None:
    """A request that states no budget still gets one.

    An unbudgeted render falls back to the count defaults, which expose a
    fraction of the content retrieval already paid for, so the product surface
    supplies a budget rather than leaving the pack bounded by item counts.
    """
    from sibyl.api.schemas.context import ContextPackRequest
    from sibyl_core.tools.context import DEFAULT_MARKDOWN_TOKEN_BUDGET

    request = ContextPackRequest(goal="ship the thing")

    assert request.markdown_token_budget == DEFAULT_MARKDOWN_TOKEN_BUDGET
    assert ContextPackRequest(goal="x", markdown_token_budget=None).markdown_token_budget is None


def test_operational_note_composition_defaults_preserve_the_baseline() -> None:
    from sibyl.api.schemas.context import ContextEvidenceRequest

    evidence = ContextEvidenceRequest()

    assert evidence.operational_note_dedupe_mode == "source"
    assert evidence.operational_note_lane_mode == "reserved"
    assert "operational_note_dedupe_mode" not in evidence.model_fields_set
    assert "operational_note_lane_mode" not in evidence.model_fields_set


def test_explicit_note_composition_requires_the_note_lane() -> None:
    from sibyl.api.schemas.context import ContextEvidenceRequest

    with pytest.raises(ValueError, match="reserve_distilled_notes=true"):
        ContextEvidenceRequest(
            reserve_distilled_notes=False,
            operational_note_lane_mode="additive",
        )

    with pytest.raises(ValueError, match="requires char_budget"):
        ContextEvidenceRequest(operational_note_lane_mode="additive")


class TestNaiveRetrievalArm:
    """The 1.3 Phase 0 control arm: selectable, off by default, and total."""

    @staticmethod
    def _evidence(mode: str) -> dict[str, Any]:
        return {"retrieval_mode": mode, "types": ["session"], "limit": 5}

    @staticmethod
    def _arm_response() -> Any:
        """What the arm actually hands back: the core dataclass, not the schema."""

        from sibyl_core.tools.responses import (
            SearchResponse as CoreSearchResponse,
            SearchResult as CoreSearchResult,
        )

        return CoreSearchResponse(
            results=[
                CoreSearchResult(
                    id="span_1",
                    type="session",
                    name="span_1",
                    content="verbatim span",
                    score=0.02,
                    result_origin="graph",
                )
            ],
            total=1,
            query="ship faster",
            filters={"retrieval_mode": "naive", "retrieval_arm": "naive"},
            graph_count=1,
            limit=5,
        )

    @pytest.mark.asyncio
    async def test_selecting_naive_routes_evidence_to_the_arm(self) -> None:
        org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
        arm = AsyncMock(return_value=self._arm_response())

        with (
            patch(
                "sibyl.api.routes.context.list_accessible_project_graph_ids",
                AsyncMock(return_value=["proj_1"]),
            ),
            patch("sibyl_core.tools.context.compile_context", AsyncMock(return_value=_pack())),
            patch("sibyl_core.retrieval.naive.naive_search", arm),
            patch(
                "sibyl.api.routes.search.execute_search_request",
                AsyncMock(side_effect=AssertionError("the arm must not reach the machine")),
            ),
            patch("sibyl.api.routes.context.configured_embedding_provider", return_value=None),
        ):
            response = await context_pack(
                request=ContextPackRequest(
                    goal="ship faster",
                    evidence=self._evidence("naive"),
                ),
                org=org,
                ctx=_ctx(),
            )

        arm.assert_awaited_once()
        assert response.evidence is not None
        assert response.evidence.filters["retrieval_mode"] == "naive"
        assert response.evidence.filters["retrieval_arm"] == "naive"
        # The arm runs one search and no planner, so it reports the planner
        # receipt fast reports rather than inventing a third shape.
        assert response.evidence.filters["planner_status"] == "not_requested"
        assert response.evidence.filters["query_count"] == 1

    @pytest.mark.asyncio
    async def test_the_arm_governs_the_pack_not_only_the_evidence_block(self) -> None:
        """A pack still compiled by the machine would contaminate the race."""

        org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
        compile_context = AsyncMock(return_value=_pack())

        with (
            patch(
                "sibyl.api.routes.context.list_accessible_project_graph_ids",
                AsyncMock(return_value=["proj_1"]),
            ),
            patch("sibyl_core.tools.context.compile_context", compile_context),
            patch(
                "sibyl_core.retrieval.naive.naive_search",
                AsyncMock(return_value=self._arm_response()),
            ),
            patch("sibyl.api.routes.context.configured_embedding_provider", return_value=None),
        ):
            await context_pack(
                request=ContextPackRequest(
                    goal="ship faster",
                    evidence=self._evidence("naive"),
                ),
                org=org,
                ctx=_ctx(),
            )

        assert compile_context.await_args.kwargs["naive_retrieval"] is True

    @pytest.mark.asyncio
    async def test_the_arm_turns_off_the_reserved_distilled_notes_lane(self) -> None:
        """The reserved typed lane is machine surface, so the arm suppresses it."""

        org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
        distilled = AsyncMock(return_value=(None, None))

        with (
            patch(
                "sibyl.api.routes.context.list_accessible_project_graph_ids",
                AsyncMock(return_value=["proj_1"]),
            ),
            patch("sibyl_core.tools.context.compile_context", AsyncMock(return_value=_pack())),
            patch(
                "sibyl_core.retrieval.naive.naive_search",
                AsyncMock(return_value=self._arm_response()),
            ),
            patch(
                "sibyl.api.routes.context._execute_distilled_context_evidence_search",
                distilled,
            ),
            patch("sibyl.api.routes.context.configured_embedding_provider", return_value=None),
        ):
            response = await context_pack(
                request=ContextPackRequest(
                    goal="ship faster",
                    # Explicitly requested, and still suppressed: the arm's
                    # composition is fixed, not negotiable per request.
                    evidence={**self._evidence("naive"), "reserve_distilled_notes": True},
                ),
                org=org,
                ctx=_ctx(),
            )

        distilled.assert_not_awaited()
        assert response.evidence is not None
        assert response.evidence.filters["reserve_distilled_notes"] is False

    @pytest.mark.asyncio
    async def test_the_arm_still_records_exposure(self) -> None:
        """Exposure receipts must exist under both arms or they cannot be compared."""

        org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
        exposure = AsyncMock(return_value={"status": "stamped", "item_count": 1})

        with (
            patch(
                "sibyl.api.routes.context.list_accessible_project_graph_ids",
                AsyncMock(return_value=["proj_1"]),
            ),
            patch("sibyl_core.tools.context.compile_context", AsyncMock(return_value=_pack())),
            patch(
                "sibyl_core.retrieval.naive.naive_search",
                AsyncMock(return_value=self._arm_response()),
            ),
            patch("sibyl_core.tools.usage_exposure.annotate_search_result_exposures", exposure),
            patch("sibyl.api.routes.context.configured_embedding_provider", return_value=None),
        ):
            response = await context_pack(
                request=ContextPackRequest(
                    goal="ship faster",
                    evidence=self._evidence("naive"),
                    record_exposure=True,
                ),
                org=org,
                ctx=_ctx(),
            )

        exposure.assert_awaited_once()
        assert exposure.await_args.kwargs["source_surface"] == "context_pack_evidence"
        assert response.evidence is not None
        assert response.evidence.filters["usage_exposure"] == {
            "status": "stamped",
            "item_count": 1,
        }

    @pytest.mark.asyncio
    async def test_the_arm_honors_a_request_that_declines_exposure(self) -> None:
        org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
        exposure = AsyncMock(side_effect=AssertionError("exposure recorded against the request"))

        with (
            patch(
                "sibyl.api.routes.context.list_accessible_project_graph_ids",
                AsyncMock(return_value=["proj_1"]),
            ),
            patch("sibyl_core.tools.context.compile_context", AsyncMock(return_value=_pack())),
            patch(
                "sibyl_core.retrieval.naive.naive_search",
                AsyncMock(return_value=self._arm_response()),
            ),
            patch("sibyl_core.tools.usage_exposure.annotate_search_result_exposures", exposure),
            patch("sibyl.api.routes.context.configured_embedding_provider", return_value=None),
        ):
            await context_pack(
                request=ContextPackRequest(
                    goal="ship faster",
                    evidence=self._evidence("naive"),
                    record_exposure=False,
                ),
                org=org,
                ctx=_ctx(),
            )

        exposure.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_the_default_path_never_reaches_the_arm(self) -> None:
        """The hard invariant: an unselected arm is an unreachable arm."""

        org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
        arm = AsyncMock(side_effect=AssertionError("the default path reached the naive arm"))
        compile_context = AsyncMock(return_value=_pack())
        machine = AsyncMock(return_value=_search_response("ship faster", ("evidence_1", 0.9)))

        for evidence in (None, self._evidence("fast")):
            with (
                patch(
                    "sibyl.api.routes.context.list_accessible_project_graph_ids",
                    AsyncMock(return_value=["proj_1"]),
                ),
                patch("sibyl_core.tools.context.compile_context", compile_context),
                patch("sibyl_core.retrieval.naive.naive_search", arm),
                patch("sibyl.api.routes.search.execute_search_request", machine),
                patch("sibyl.api.routes.context.configured_embedding_provider", return_value=None),
            ):
                await context_pack(
                    request=ContextPackRequest(goal="ship faster", evidence=evidence),
                    org=org,
                    ctx=_ctx(),
                )

            arm.assert_not_awaited()
            assert compile_context.await_args.kwargs["naive_retrieval"] is False

    @pytest.mark.asyncio
    async def test_the_arm_refuses_a_machine_knob_it_cannot_honour(self) -> None:
        """Half-applied is worse than refused: the run would look configured.

        knn_type_overfetch tunes the machine's typed vector read. The pack plan
        and the evidence plan read it from different places, so under the arm a
        request setting it got it applied to one half of its own pack and
        dropped from the other, and the arm has no such stage in either case.
        """

        org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
        for payload in (
            {"knn_type_overfetch": 17},
            {"evidence_overfetch": 17},
        ):
            evidence = self._evidence("naive")
            if "evidence_overfetch" in payload:
                evidence["knn_type_overfetch"] = payload["evidence_overfetch"]
                request = ContextPackRequest(goal="ship faster", evidence=evidence)
            else:
                request = ContextPackRequest(
                    goal="ship faster",
                    evidence=evidence,
                    knn_type_overfetch=payload["knn_type_overfetch"],
                )
            with (
                patch(
                    "sibyl.api.routes.context.list_accessible_project_graph_ids",
                    AsyncMock(return_value=["proj_1"]),
                ),
                patch("sibyl_core.tools.context.compile_context", AsyncMock(return_value=_pack())),
                patch("sibyl.api.routes.context.configured_embedding_provider", return_value=None),
                pytest.raises(HTTPException) as excinfo,
            ):
                await context_pack(request=request, org=org, ctx=_ctx())

            assert excinfo.value.status_code == 400
            assert "knn_type_overfetch" in str(excinfo.value.detail)

    @pytest.mark.asyncio
    async def test_the_machine_still_accepts_the_knob(self) -> None:
        """The refusal is scoped to the arm."""

        org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
        compile_context = AsyncMock(return_value=_pack())

        with (
            patch(
                "sibyl.api.routes.context.list_accessible_project_graph_ids",
                AsyncMock(return_value=["proj_1"]),
            ),
            patch("sibyl_core.tools.context.compile_context", compile_context),
            patch("sibyl.api.routes.context.configured_embedding_provider", return_value=None),
        ):
            await context_pack(
                request=ContextPackRequest(goal="ship faster", knn_type_overfetch=17),
                org=org,
                ctx=_ctx(),
            )

        assert compile_context.await_args.kwargs["knn_type_overfetch"] == 17

    def test_the_arm_is_off_unless_a_request_names_it(self) -> None:
        assert ContextPackRequest(goal="x").evidence is None
        assert ContextPackRequest(goal="x", evidence={}).evidence.retrieval_mode == "fast"

    def test_retrieval_mode_schema_documents_the_control_arm(self) -> None:
        from sibyl.api.schemas.context import ContextEvidenceRequest

        field = ContextEvidenceRequest.model_fields["retrieval_mode"]
        assert "naive" in str(field.annotation)
        assert "EXPERIMENTAL" in field.description


@pytest.mark.parametrize("mode", ["fast", "naive"])
def test_context_evidence_supports_current_retrieval_modes(mode: str) -> None:
    from sibyl.api.schemas.context import ContextEvidenceRequest

    evidence = ContextEvidenceRequest(retrieval_mode=mode)
    assert evidence.retrieval_mode == mode
    assert ContextEvidenceRequest().retrieval_mode == "fast"


def test_context_evidence_rejects_accurate_with_migration_instructions() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="set retrieval_mode=fast") as exc:
        ContextPackRequest(
            goal="find the release procedure", evidence={"retrieval_mode": "accurate"}
        )

    assert exc.value.errors()[0]["loc"] == ("evidence", "retrieval_mode")
    assert "pinned Sibyl version" in str(exc.value)


def test_context_evidence_schema_excludes_removed_mode() -> None:
    from sibyl.api.schemas.context import ContextEvidenceRequest

    schema = ContextEvidenceRequest.model_json_schema()
    assert schema["properties"]["retrieval_mode"]["enum"] == ["fast", "naive"]
    assert "max_planned_queries" not in schema["properties"]
    assert "max_results_per_source" not in schema["properties"]


@pytest.mark.parametrize("mode", ["fast", "naive"])
def test_context_evidence_ignores_retired_planner_fields(mode: str) -> None:
    from sibyl.api.schemas.context import ContextEvidenceRequest

    legacy = ContextEvidenceRequest.model_validate(
        {"retrieval_mode": mode, "max_planned_queries": 3, "max_results_per_source": 4}
    )
    current = ContextEvidenceRequest(retrieval_mode=mode)
    assert legacy.model_dump() == current.model_dump()
    assert legacy.model_fields_set == current.model_fields_set


def test_removed_accurate_mode_returns_http_422_before_retrieval() -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from sibyl.api.routes.context import router
    from sibyl.auth.dependencies import get_auth_context, get_current_organization

    app = FastAPI()
    app.include_router(router, prefix="/api")
    for dependency in router.dependencies:
        app.dependency_overrides[dependency.dependency] = lambda: None
    app.dependency_overrides[get_auth_context] = _ctx
    app.dependency_overrides[get_current_organization] = lambda: SimpleNamespace(
        id=UUID("00000000-0000-0000-0000-000000000111")
    )
    with (
        patch("sibyl_core.tools.context.compile_context", AsyncMock()) as compile_pack,
        patch("sibyl.api.routes.search.execute_search_request", AsyncMock()) as search,
        TestClient(app) as client,
    ):
        response = client.post(
            "/api/context/pack",
            json={"goal": "find the release procedure", "evidence": {"retrieval_mode": "accurate"}},
        )

    assert response.status_code == 422
    error = response.json()["detail"][0]
    assert error["loc"] == ["body", "evidence", "retrieval_mode"]
    assert "set retrieval_mode=fast" in error["msg"]
    compile_pack.assert_not_awaited()
    search.assert_not_awaited()
