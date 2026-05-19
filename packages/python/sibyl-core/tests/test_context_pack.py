from __future__ import annotations

import asyncio
from typing import Any, Literal
from unittest.mock import AsyncMock, patch

import pytest

import sibyl_core.tools.context as context_module
from sibyl_core.models.context import (
    ContextFacet,
    ContextIntent,
    ContextItem,
    ContextItemQualityMetadata,
    ContextLayer,
    ContextRelatedItem,
)
from sibyl_core.tools.context import (
    FACET_TYPES,
    compile_context,
    context_item_freshness,
    context_item_lifecycle_state,
    context_item_project_id,
    context_item_source_id,
    context_pack_to_dict,
    context_pack_to_markdown,
)
from sibyl_core.tools.responses import SearchResponse, SearchResult


def _result(
    entity_id: str,
    entity_type: str,
    name: str,
    *,
    score: float = 0.8,
    source: str | None = None,
    url: str | None = None,
    result_origin: Literal["graph", "document"] = "graph",
    metadata: dict[str, Any] | None = None,
) -> SearchResult:
    return SearchResult(
        id=entity_id,
        type=entity_type,
        name=name,
        content=f"{name} content",
        score=score,
        source=source,
        url=url,
        result_origin=result_origin,
        metadata={"entity_type": entity_type, **(metadata or {})},
    )


def _facet_native_search(
    responses: dict[ContextFacet, list[SearchResult]],
    *,
    calls: list[dict[str, Any]] | None = None,
):
    """Build a fake native_context_search keyed on facet.

    Native retrieval is the only runtime path; tests exercise context
    assembly by stubbing native_context_search and routing facet results
    through the plan-driven search the way compile_context does.
    """

    async def fake_native_context_search(**kwargs: Any) -> SearchResponse:
        if calls is not None:
            calls.append(kwargs)
        facet = kwargs.get("facet")
        items = responses.get(facet, []) if facet is not None else []
        return SearchResponse(
            results=items,
            total=len(items),
            query=kwargs["plan"].query,
            filters={"types": kwargs.get("types")},
        )

    return fake_native_context_search


@pytest.mark.asyncio
async def test_compile_context_groups_build_context_by_agent_facets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []
    responses = {
        ContextFacet.ACTIVE_WORK: [_result("task-1", "task", "Build capture hook")],
        ContextFacet.DECISIONS: [_result("decision-1", "decision", "Use context packs")],
        ContextFacet.CONSTRAINTS: [_result("rule-1", "rule", "Keep context precise")],
        ContextFacet.PROCEDURES: [_result("procedure-1", "procedure", "Verify")],
        ContextFacet.GOTCHAS: [_result("pattern-1", "pattern", "Avoid broad search")],
        ContextFacet.ARTIFACTS: [_result("artifact-1", "artifact", "Planning doc")],
        ContextFacet.RECENT_MEMORY: [_result("session-1", "session", "Prior session")],
    }
    monkeypatch.setattr(
        context_module,
        "native_context_search",
        _facet_native_search(responses, calls=calls),
    )

    pack = await compile_context(
        "help agents build faster",
        intent="build",
        domain="agent-memory",
        project="sibyl",
        organization_id="org-123",
    )

    assert pack.intent == ContextIntent.BUILD
    assert pack.query == "help agents build faster agent-memory"
    assert [section.facet for section in pack.sections] == [
        ContextFacet.ACTIVE_WORK,
        ContextFacet.DECISIONS,
        ContextFacet.CONSTRAINTS,
        ContextFacet.PROCEDURES,
        ContextFacet.GOTCHAS,
        ContextFacet.ARTIFACTS,
        ContextFacet.RECENT_MEMORY,
    ]
    assert pack.total_items == 7
    assert all(call["plan"].organization_id == "org-123" for call in calls)
    assert all(call["plan"].project == "sibyl" for call in calls)


@pytest.mark.asyncio
async def test_compile_context_supports_review_intent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []
    responses = {
        ContextFacet.VERIFICATION: [_result("claim-1", "claim", "Verify behavior")],
        ContextFacet.DECISIONS: [_result("decision-1", "decision", "Use full fidelity")],
        ContextFacet.CONSTRAINTS: [_result("rule-1", "rule", "Preserve quality")],
        ContextFacet.GOTCHAS: [_result("error-1", "error_pattern", "Avoid shortcuts")],
        ContextFacet.ARTIFACTS: [_result("artifact-1", "artifact", "Audit doc")],
        ContextFacet.ACTIVE_WORK: [_result("task-1", "task", "Review task")],
        ContextFacet.RECENT_MEMORY: [_result("note-1", "note", "Prior note")],
    }
    monkeypatch.setattr(
        context_module,
        "native_context_search",
        _facet_native_search(responses, calls=calls),
    )

    pack = await compile_context(
        "review the patch",
        intent="review",
        organization_id="org-123",
    )

    assert pack.intent == ContextIntent.REVIEW
    assert [section.facet for section in pack.sections] == [
        ContextFacet.VERIFICATION,
        ContextFacet.DECISIONS,
        ContextFacet.CONSTRAINTS,
        ContextFacet.GOTCHAS,
        ContextFacet.ARTIFACTS,
        ContextFacet.ACTIVE_WORK,
        ContextFacet.RECENT_MEMORY,
    ]
    assert [call["types"] for call in calls] == [
        FACET_TYPES[ContextFacet.VERIFICATION],
        FACET_TYPES[ContextFacet.DECISIONS],
        FACET_TYPES[ContextFacet.CONSTRAINTS],
        FACET_TYPES[ContextFacet.GOTCHAS],
        FACET_TYPES[ContextFacet.ARTIFACTS],
        FACET_TYPES[ContextFacet.ACTIVE_WORK],
        FACET_TYPES[ContextFacet.RECENT_MEMORY],
    ]


@pytest.mark.asyncio
async def test_compile_context_runs_facet_searches_concurrently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active_calls = 0
    max_active_calls = 0

    async def fake_native_context_search(**kwargs: Any) -> SearchResponse:
        nonlocal active_calls, max_active_calls
        active_calls += 1
        max_active_calls = max(max_active_calls, active_calls)
        try:
            await asyncio.sleep(0.01)
            return SearchResponse(results=[], total=0, query=kwargs["plan"].query, filters={})
        finally:
            active_calls -= 1

    monkeypatch.setattr(context_module, "native_context_search", fake_native_context_search)

    pack = await compile_context(
        "parallelize context facets",
        intent="build",
        organization_id="org-123",
    )

    assert pack.total_items == 0
    assert max_active_calls > 1


@pytest.mark.asyncio
async def test_compile_context_keeps_successful_facets_when_one_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    warnings: list[dict[str, str]] = []

    class FakeLog:
        def warning(self, _event: str, **kwargs: str) -> None:
            warnings.append(kwargs)

    monkeypatch.setattr(context_module, "log", FakeLog())

    async def fake_native_context_search(**kwargs: Any) -> SearchResponse:
        facet = kwargs.get("facet")
        if facet is ContextFacet.DECISIONS:
            raise RuntimeError("decision search unavailable")
        if facet is ContextFacet.ACTIVE_WORK:
            return SearchResponse(
                results=[_result("task-1", "task", "Keep moving")],
                total=1,
                query=kwargs["plan"].query,
                filters={},
            )
        return SearchResponse(results=[], total=0, query=kwargs["plan"].query, filters={})

    monkeypatch.setattr(context_module, "native_context_search", fake_native_context_search)

    pack = await compile_context(
        "resilient context facets",
        intent="build",
        organization_id="org-123",
    )

    assert pack.total_items == 1
    assert pack.items[0].id == "task-1"
    assert warnings == [{"facet": "decisions", "error_type": "RuntimeError"}]


@pytest.mark.asyncio
async def test_compile_context_defaults_to_native_retrieval_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    native_calls: list[dict[str, Any]] = []

    async def fake_native_context_search(**kwargs: Any) -> SearchResponse:
        native_calls.append(kwargs)
        if kwargs["facet"] is not ContextFacet.DECISIONS:
            return SearchResponse(results=[], total=0, query=kwargs["plan"].query, filters={})
        return SearchResponse(
            results=[
                _result(
                    "decision-native",
                    "decision",
                    "Use native Surreal retrieval",
                    source="source:native",
                    metadata={
                        "source_id": "source:native",
                        "visibility": "project",
                        "freshness": 1.5,
                        "policy_reason": "project_access_verified",
                        "retrieval_signals": ["node_fulltext"],
                    },
                )
            ],
            total=1,
            query=kwargs["plan"].query,
            filters={},
        )

    async def unexpected_search(**_kwargs: Any) -> SearchResponse:
        raise AssertionError("fallback search should not run in native mode")

    monkeypatch.setattr(context_module, "native_context_search", fake_native_context_search)

    pack = await compile_context(
        "native retrieval mode",
        intent="decide",
        project="project_123",
        accessible_projects={"project_123"},
        principal_id="user-123",
        organization_id="org-123",
        search_fn=unexpected_search,
    )

    assert [item.id for item in pack.items] == ["decision-native"]
    assert native_calls[0]["plan"].scopes[1].policy_reason == "project_access_verified"
    assert pack.items[0].metadata["retrieval_signals"] == ["node_fulltext"]


@pytest.mark.asyncio
async def test_compile_context_scopes_related_items_to_api_key_grants(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sibyl_core.auth.memory_policy import memory_scope_policy_key
    from sibyl_core.services.surreal_content import MemoryScope

    related_calls: list[dict[str, Any]] = []

    async def fake_native_context_search(**kwargs: Any) -> SearchResponse:
        if kwargs["facet"] is not ContextFacet.DECISIONS:
            return SearchResponse(results=[], total=0, query=kwargs["plan"].query, filters={})
        return SearchResponse(
            results=[_result("decision-1", "decision", "Granted decision")],
            total=1,
            query=kwargs["plan"].query,
            filters={},
        )

    async def fake_related(**kwargs: Any) -> list[ContextRelatedItem]:
        related_calls.append(kwargs)
        return []

    monkeypatch.setattr(context_module, "native_context_search", fake_native_context_search)

    await compile_context(
        "scoped related items",
        intent="decide",
        accessible_projects={"project_a", "project_b"},
        principal_id="user-123",
        organization_id="org-123",
        include_related=True,
        related_fn=fake_related,
        allowed_memory_scope_keys={
            memory_scope_policy_key(MemoryScope.PRIVATE, "user-123"),
            memory_scope_policy_key(MemoryScope.PROJECT, "project_a"),
        },
    )

    assert related_calls
    assert all(call["accessible_projects"] == {"project_a"} for call in related_calls)


@pytest.mark.asyncio
async def test_compile_context_compare_mode_logs_policy_safe_diff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    info_calls: list[dict[str, Any]] = []

    class FakeLog:
        def info(self, _event: str, **kwargs: Any) -> None:
            info_calls.append(kwargs)

        def warning(self, *_args: Any, **_kwargs: Any) -> None:
            pass

    async def fake_native_context_search(**kwargs: Any) -> SearchResponse:
        if kwargs["facet"] is not ContextFacet.DECISIONS:
            return SearchResponse(results=[], total=0, query=kwargs["plan"].query, filters={})
        return SearchResponse(
            results=[
                _result(
                    "decision-native",
                    "decision",
                    "Native decision",
                    source="source:native",
                    metadata={
                        "source_id": "source:native",
                        "policy_reason": "project_access_verified",
                    },
                )
            ],
            total=1,
            query=kwargs["plan"].query,
            filters={},
        )

    async def fallback_search(**kwargs: Any) -> SearchResponse:
        if kwargs["types"] != ["decision"]:
            return SearchResponse(results=[], total=0, query=kwargs["query"], filters={})
        return SearchResponse(
            results=[
                _result(
                    "decision-other-project",
                    "decision",
                    "Filtered fallback decision",
                    metadata={
                        "project_id": "project_other",
                        "source_id": "source:filtered",
                    },
                )
            ],
            total=1,
            query=kwargs["query"],
            filters={},
        )

    monkeypatch.setattr(context_module, "log", FakeLog())
    monkeypatch.setattr(context_module, "native_context_search", fake_native_context_search)

    pack = await compile_context(
        "compare retrieval mode",
        intent="decide",
        project="project_123",
        accessible_projects={"project_123"},
        principal_id="user-123",
        organization_id="org-123",
        search_fn=fallback_search,
        retrieval_mode="compare",
    )

    assert [item.id for item in pack.items] == ["decision-native"]
    decision_log = next(call for call in info_calls if call["facet"] == "decisions")
    assert decision_log["native_count"] == 1
    assert decision_log["fallback_count"] == 0
    assert decision_log["fallback_only_ids"] == []


@pytest.mark.asyncio
async def test_compile_context_wake_layer_caps_items_and_skips_related(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    related_calls: list[str] = []

    async def fake_native_context_search(**kwargs: Any) -> SearchResponse:
        facet = kwargs.get("facet")
        prefix = facet.value if facet is not None else "memory"
        return SearchResponse(
            results=[_result(f"{prefix}-{index}", "note", f"Memory {index}") for index in range(2)],
            total=2,
            query=kwargs["plan"].query,
            filters={},
        )

    async def fake_related(**kwargs: Any) -> list[ContextRelatedItem]:
        related_calls.append(kwargs["entity_id"])
        return [
            ContextRelatedItem(
                id="related-1",
                type="decision",
                name="Related decision",
                relationship="RELATED_TO",
                direction="outgoing",
            )
        ]

    monkeypatch.setattr(context_module, "native_context_search", fake_native_context_search)

    pack = await compile_context(
        "wake up the coding session",
        intent="build",
        layer="wake",
        organization_id="org-123",
        limit=50,
        include_related=True,
        related_fn=fake_related,
    )

    assert pack.layer == ContextLayer.WAKE
    assert pack.total_items == 8
    assert [section.facet for section in pack.sections] == [
        ContextFacet.RECENT_MEMORY,
        ContextFacet.ACTIVE_WORK,
        ContextFacet.DECISIONS,
        ContextFacet.GOTCHAS,
    ]
    assert all(not item.related for item in pack.items)
    assert related_calls == []


@pytest.mark.asyncio
async def test_compile_context_keeps_graph_context_when_a_facet_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_native_context_search(**kwargs: Any) -> SearchResponse:
        facet = kwargs.get("facet")
        if facet is ContextFacet.RECENT_MEMORY:
            raise RuntimeError("recent memory unavailable")
        return SearchResponse(
            results=[_result("decision-1", "decision", "Use layered packs")],
            total=1,
            query=kwargs["plan"].query,
            filters={},
        )

    monkeypatch.setattr(context_module, "native_context_search", fake_native_context_search)

    pack = await compile_context(
        "graph context resilience",
        intent="build",
        principal_id="user-123",
        organization_id="org-123",
        limit=1,
    )

    assert pack.total_items == 1
    assert pack.items[0].id == "decision-1"


@pytest.mark.asyncio
async def test_compile_context_supports_non_software_ideation_domains(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = {
        ContextFacet.IDEATION: [_result("idea-1", "idea", "Venue layout concept")],
        ContextFacet.DOMAIN: [_result("domain-1", "domain", "Aerial showcase")],
    }
    monkeypatch.setattr(context_module, "native_context_search", _facet_native_search(responses))

    pack = await compile_context(
        "design a performance showcase",
        intent="ideate",
        domain="flow arts",
        organization_id="org-123",
    )

    assert pack.intent == ContextIntent.IDEATE
    assert pack.domain == "flow arts"
    assert [item.type for item in pack.items] == ["idea", "domain"]


@pytest.mark.asyncio
async def test_compile_context_dedupes_results_across_facets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_native_context_search(**kwargs: Any) -> SearchResponse:
        return SearchResponse(
            results=[_result("same-id", "plan", "Repeated memory")],
            total=1,
            query=kwargs["plan"].query,
            filters={},
        )

    monkeypatch.setattr(context_module, "native_context_search", fake_native_context_search)

    pack = await compile_context(
        "ship faster",
        intent="plan",
        organization_id="org-123",
    )

    assert pack.total_items == 1
    assert pack.items[0].id == "same-id"


@pytest.mark.asyncio
async def test_compile_context_falls_back_to_broad_search_when_facets_miss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    async def fake_native_context_search(**kwargs: Any) -> SearchResponse:
        calls.append(kwargs)
        results = []
        if kwargs.get("types") is None:
            results = [
                _result(
                    "decision-1",
                    "decision",
                    "Scoped remember captures linked project context",
                    metadata={"project_id": "project_123"},
                )
            ]
        return SearchResponse(
            results=results,
            total=len(results),
            query=kwargs["plan"].query,
            filters={},
        )

    monkeypatch.setattr(context_module, "native_context_search", fake_native_context_search)

    pack = await compile_context(
        "build project-scoped remember and recall",
        intent="build",
        domain="sibyl",
        project="project_123",
        accessible_projects={"project_123"},
        organization_id="org-123",
    )

    assert pack.total_items == 1
    assert pack.sections[0].facet == ContextFacet.DECISIONS
    assert pack.items[0].id == "decision-1"
    fallback_call = calls[-1]
    assert fallback_call["types"] is None
    assert fallback_call["plan"].project == "project_123"


@pytest.mark.asyncio
async def test_compile_context_can_attach_one_hop_related_items(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_native_context_search(**kwargs: Any) -> SearchResponse:
        if kwargs.get("facet") is not ContextFacet.DECISIONS:
            return SearchResponse(results=[], total=0, query=kwargs["plan"].query, filters={})
        return SearchResponse(
            results=[_result("decision-1", "decision", "Use context packs")],
            total=1,
            query=kwargs["plan"].query,
            filters={},
        )

    calls: list[dict[str, Any]] = []

    async def fake_related(**kwargs: Any) -> list[ContextRelatedItem]:
        calls.append(kwargs)
        return [
            ContextRelatedItem(
                id="plan-1",
                type="plan",
                name="Agent memory plan",
                relationship="RELATED_TO",
                direction="outgoing",
            )
        ]

    monkeypatch.setattr(context_module, "native_context_search", fake_native_context_search)

    pack = await compile_context(
        "ship faster",
        intent="decide",
        organization_id="org-123",
        limit=1,
        include_related=True,
        related_fn=fake_related,
    )

    assert pack.items[0].related[0].id == "plan-1"
    assert calls[0]["entity_id"] == "decision-1"
    assert calls[0]["organization_id"] == "org-123"


@pytest.mark.asyncio
async def test_compile_context_filters_related_project_entities_by_own_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    async def fake_native_context_search(**kwargs: Any) -> SearchResponse:
        if kwargs.get("facet") is not ContextFacet.DECISIONS:
            return SearchResponse(results=[], total=0, query=kwargs["plan"].query, filters={})
        return SearchResponse(
            results=[_result("decision-1", "decision", "Use context packs")],
            total=1,
            query=kwargs["plan"].query,
            filters={},
        )

    monkeypatch.setattr(context_module, "native_context_search", fake_native_context_search)

    def entity(entity_id: str, entity_type: str, project_id: str | None = None) -> SimpleNamespace:
        metadata = {"project_id": project_id} if project_id else {}
        return SimpleNamespace(
            id=entity_id,
            entity_type=SimpleNamespace(value=entity_type),
            name=entity_id,
            metadata=metadata,
        )

    def relationship(target_id: str) -> SimpleNamespace:
        return SimpleNamespace(
            source_id="decision-1",
            target_id=target_id,
            relationship_type=SimpleNamespace(value="RELATED_TO"),
        )

    relationship_manager = SimpleNamespace(
        get_related_entities=AsyncMock(
            return_value=[
                (entity("task-hidden", "task", "project-hidden"), relationship("task-hidden")),
                (entity("project-hidden", "project"), relationship("project-hidden")),
                (entity("project-visible", "project"), relationship("project-visible")),
                (entity("pattern-unassigned", "pattern"), relationship("pattern-unassigned")),
            ]
        )
    )
    runtime = SimpleNamespace(relationship_manager=relationship_manager)

    with patch.object(context_module, "get_graph_runtime", AsyncMock(return_value=runtime)):
        pack = await compile_context(
            "ship trust",
            intent="decide",
            organization_id="org-123",
            accessible_projects={"project-visible"},
            limit=1,
            include_related=True,
            related_limit=5,
        )

    assert pack.items[0].related is not None
    assert [item.id for item in pack.items[0].related] == [
        "project-visible",
        "pattern-unassigned",
    ]


@pytest.mark.asyncio
async def test_compile_context_adds_compact_quality_metadata_from_search_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_native_context_search(**kwargs: Any) -> SearchResponse:
        return SearchResponse(
            results=[
                _result(
                    "doc-1",
                    "document",
                    "Surreal docs",
                    source="Sibyl docs",
                    url="https://docs.example.test/sibyl/context",
                    result_origin="document",
                    metadata={
                        "source_id": "source-1",
                        "project_id": "project-123",
                        "updated_at": "2026-04-20T10:30:00Z",
                        "created_at": "2026-04-01T09:00:00Z",
                        "heading_path": ["Context", "Packs"],
                    },
                )
            ],
            total=1,
            query=kwargs["plan"].query,
            filters={},
        )

    monkeypatch.setattr(context_module, "native_context_search", fake_native_context_search)

    pack = await compile_context(
        "judge memory freshness",
        intent="research",
        organization_id="org-123",
        limit=1,
    )

    quality = pack.items[0].quality
    assert quality.origin == "document"
    assert quality.source == "Sibyl docs"
    assert quality.url == "https://docs.example.test/sibyl/context"
    assert quality.project_id == "project-123"
    assert quality.updated_at == "2026-04-20T10:30:00Z"
    assert quality.created_at == "2026-04-01T09:00:00Z"


@pytest.mark.asyncio
async def test_compile_context_falls_back_to_graph_id_for_source_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_native_context_search(**kwargs: Any) -> SearchResponse:
        return SearchResponse(
            results=[_result("decision-1", "decision", "Source-light graph decision")],
            total=1,
            query=kwargs["plan"].query,
            filters={},
        )

    monkeypatch.setattr(context_module, "native_context_search", fake_native_context_search)

    pack = await compile_context(
        "source metadata coverage",
        intent="decide",
        organization_id="org-123",
        limit=1,
    )

    item = pack.items[0]
    assert item.source == "decision-1"
    assert item.metadata["source_id"] == "decision-1"
    assert item.quality.source == "decision-1"


@pytest.mark.asyncio
async def test_compile_context_native_ranks_agent_diary_by_relevance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import UTC, datetime

    import sibyl_core.retrieval.native as native_module
    from sibyl_core.services.surreal_content import RawMemory

    class EmptyNativeClient:
        async def execute_query(self, *_args: object, **_kwargs: object) -> list[object]:
            return []

    class EmptyNativeRuntime:
        client = EmptyNativeClient()

    async def fake_native_runtime(_organization_id: str) -> EmptyNativeRuntime:
        return EmptyNativeRuntime()

    async def fake_raw_recall(**kwargs: Any) -> list[RawMemory]:
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
                    metadata={"agent_id": "nova", "memory_kind": "agent_diary"},
                    capture_surface="agent_diary",
                    captured_at=datetime(2026, 4, 27, 12, 0, 0, tzinfo=UTC),
                    created_at=datetime(2026, 4, 27, 12, 0, 0, tzinfo=UTC),
                    score=0.7,
                )
            ]
        if kwargs["memory_scope"] == "private":
            return [
                RawMemory(
                    id="private-1",
                    organization_id="org-123",
                    source_id="baseline:delegated-recall",
                    principal_id="user-123",
                    title="Delegated Recall Baseline",
                    raw_content="Delegated recall baseline covers storage handoff.",
                    captured_at=datetime(2026, 4, 27, 12, 0, 0, tzinfo=UTC),
                    created_at=datetime(2026, 4, 27, 12, 0, 0, tzinfo=UTC),
                    score=0.3,
                ),
            ]
        return []

    monkeypatch.setattr(native_module, "get_native_graph_runtime", fake_native_runtime)

    pack = await compile_context(
        "What should Nova recall from the diary for delegated handoff?",
        intent="learn",
        domain="sibyl",
        principal_id="user-123",
        agent_id="nova",
        organization_id="org-123",
        limit=8,
        raw_memory_recall_fn=fake_raw_recall,
        retrieval_mode="native",
    )

    assert pack.sections[0].facet == ContextFacet.RECENT_MEMORY
    assert [item.id for item in pack.sections[0].items] == [
        "raw_memory:diary-1",
        "raw_memory:private-1",
    ]
    assert "Neon Thread" in pack.sections[0].items[0].content


def test_context_item_metadata_helpers_normalize_source_policy_fields() -> None:
    item = ContextItem(
        id="artifact:fallback",
        type="artifact",
        name="Artifact",
        content="Artifact content",
        score=0.8,
        facet=ContextFacet.ARTIFACTS,
        reason="supports the section",
        source="source:fallback",
        quality=ContextItemQualityMetadata(
            project_id="project-quality",
            updated_at="2026-05-14T10:00:00Z",
        ),
        metadata={
            "project_id": "project-metadata",
            "lifecycle_state": "redacted",
        },
    )

    assert context_item_source_id(item) == "source:fallback"
    assert context_item_project_id(item) == "project-quality"
    assert context_item_freshness(item) == "2026-05-14T10:00:00Z"
    assert context_item_lifecycle_state(item) == "redacted"


def test_context_item_metadata_helpers_fall_back_to_metadata() -> None:
    item = ContextItem(
        id="artifact:id",
        type="artifact",
        name="Artifact",
        content="Artifact content",
        score=0.8,
        facet=ContextFacet.ARTIFACTS,
        reason="supports the section",
        metadata={
            "freshness": "snapshot:2026-05-14",
            "project": "project-metadata",
            "review_state": "hidden",
        },
    )

    assert context_item_source_id(item) == "artifact:id"
    assert context_item_project_id(item) == "project-metadata"
    assert context_item_freshness(item) == "snapshot:2026-05-14"
    assert context_item_lifecycle_state(item) == "hidden"


@pytest.mark.asyncio
async def test_compile_context_requires_goal_and_org() -> None:
    with pytest.raises(ValueError, match="goal is required"):
        await compile_context("", organization_id="org-123")

    with pytest.raises(ValueError, match="organization_id is required"):
        await compile_context("ship faster")


@pytest.mark.asyncio
async def test_context_pack_to_dict_serializes_dataclasses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pack = await async_compile_context_for_serialization(monkeypatch)
    payload = context_pack_to_dict(pack)

    assert payload["goal"] == "ship faster"
    assert payload["layer"] == ContextLayer.RECALL
    assert payload["sections"][0]["items"][0]["id"] == "task-1"
    assert payload["sections"][0]["items"][0]["quality"]["origin"] == "graph"


@pytest.mark.asyncio
async def test_context_pack_to_markdown_renders_injection_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pack = await async_compile_context_for_serialization(monkeypatch)
    markdown = context_pack_to_markdown(pack)

    assert "# Sibyl Context Pack: ship faster" in markdown
    assert "Layer: recall" in markdown
    assert "## Active Work" in markdown
    assert "**Task** (task) `task-1`" in markdown
    assert (
        "_graph; src=task-source.md; project=project-123; updated=2026-04-20T10:30:00Z"
    ) in markdown
    assert "Hint:" in markdown


async def async_compile_context_for_serialization(monkeypatch: pytest.MonkeyPatch):
    async def fake_native_context_search(**kwargs: Any) -> SearchResponse:
        if kwargs.get("facet") is not ContextFacet.ACTIVE_WORK:
            return SearchResponse(results=[], total=0, query=kwargs["plan"].query, filters={})
        return SearchResponse(
            results=[
                _result(
                    "task-1",
                    "task",
                    "Task",
                    source="task-source.md",
                    metadata={
                        "project_id": "project-123",
                        "updated_at": "2026-04-20T10:30:00Z",
                    },
                )
            ],
            total=1,
            query=kwargs["plan"].query,
            filters={},
        )

    monkeypatch.setattr(context_module, "native_context_search", fake_native_context_search)

    return await compile_context(
        "ship faster",
        intent="build",
        organization_id="org-123",
        limit=1,
    )
