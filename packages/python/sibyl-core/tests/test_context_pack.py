from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any, Literal

import pytest

import sibyl_core.retrieval.native as native_module
import sibyl_core.tools.context as context_module
from sibyl_core.models.context import ContextFacet, ContextIntent, ContextLayer, ContextRelatedItem
from sibyl_core.services.surreal_content import MemoryScope, RawMemory
from sibyl_core.tools.context import compile_context, context_pack_to_dict, context_pack_to_markdown
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


def _raw_memory(
    memory_id: str,
    *,
    memory_scope: MemoryScope = MemoryScope.PRIVATE,
    scope_key: str | None = None,
    score: float = 0.7,
    metadata: dict[str, Any] | None = None,
    capture_surface: str = "cli",
) -> RawMemory:
    return RawMemory(
        id=memory_id,
        organization_id="org-123",
        source_id=f"source:{memory_id}",
        principal_id="user-123",
        memory_scope=memory_scope,
        scope_key=scope_key,
        title=f"Raw {memory_id}",
        raw_content=f"Raw {memory_id} content",
        tags=["raw"],
        metadata={"source_name": "session", **(metadata or {})},
        provenance={"message_id": memory_id},
        capture_surface=capture_surface,
        captured_at=datetime(2026, 4, 27, 12, 0, 0, tzinfo=UTC),
        created_at=datetime(2026, 4, 27, 12, 0, 0, tzinfo=UTC),
        score=score,
    )


async def _empty_search_response(**kwargs: Any) -> SearchResponse:
    return SearchResponse(results=[], total=0, query=kwargs["query"], filters={})


async def _compile_context_compat(*args: Any, **kwargs: Any):
    kwargs.setdefault("retrieval_mode", "graphiti")
    return await compile_context(*args, **kwargs)


@pytest.mark.asyncio
async def test_compile_context_groups_build_context_by_agent_facets() -> None:
    calls: list[dict[str, Any]] = []
    responses = {
        ("task", "epic", "project"): [_result("task-1", "task", "Build capture hook")],
        ("decision",): [_result("decision-1", "decision", "Use context packs")],
        ("rule", "guide"): [_result("rule-1", "rule", "Keep context precise")],
        ("procedure", "template", "tool"): [_result("procedure-1", "procedure", "Verify")],
        ("error_pattern", "pattern"): [_result("pattern-1", "pattern", "Avoid broad search")],
        ("artifact", "document", "source", "config_file"): [
            _result("artifact-1", "artifact", "Planning doc")
        ],
        ("session", "episode", "note"): [_result("session-1", "session", "Prior session")],
    }

    async def fake_search(**kwargs: Any) -> SearchResponse:
        calls.append(kwargs)
        items = responses.get(tuple(kwargs["types"]), [])
        return SearchResponse(
            results=items,
            total=len(items),
            query=kwargs["query"],
            filters={"types": kwargs["types"]},
        )

    pack = await _compile_context_compat(
        "help agents build faster",
        intent="build",
        domain="agent-memory",
        project="sibyl",
        organization_id="org-123",
        search_fn=fake_search,
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
    assert all(call["organization_id"] == "org-123" for call in calls)
    assert all(call["category"] == "agent-memory" for call in calls)
    assert all(call["project"] == "sibyl" for call in calls)


@pytest.mark.asyncio
async def test_compile_context_runs_facet_searches_concurrently() -> None:
    active_calls = 0
    max_active_calls = 0

    async def fake_search(**kwargs: Any) -> SearchResponse:
        nonlocal active_calls, max_active_calls
        active_calls += 1
        max_active_calls = max(max_active_calls, active_calls)
        try:
            await asyncio.sleep(0.01)
            return SearchResponse(results=[], total=0, query=kwargs["query"], filters={})
        finally:
            active_calls -= 1

    pack = await _compile_context_compat(
        "parallelize context facets",
        intent="build",
        organization_id="org-123",
        search_fn=fake_search,
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

    async def fake_search(**kwargs: Any) -> SearchResponse:
        if kwargs["types"] == ["decision"]:
            raise RuntimeError("decision search unavailable")
        if kwargs["types"] == ["task", "epic", "project"]:
            return SearchResponse(
                results=[_result("task-1", "task", "Keep moving")],
                total=1,
                query=kwargs["query"],
                filters={},
            )
        return SearchResponse(results=[], total=0, query=kwargs["query"], filters={})

    pack = await _compile_context_compat(
        "resilient context facets",
        intent="build",
        organization_id="org-123",
        search_fn=fake_search,
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
        raise AssertionError("graphiti search should not run in native mode")

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

    pack = await _compile_context_compat(
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
async def test_compile_context_includes_private_and_project_raw_memory() -> None:
    search_calls: list[dict[str, Any]] = []
    raw_calls: list[dict[str, Any]] = []

    async def fake_search(**kwargs: Any) -> SearchResponse:
        search_calls.append(kwargs)
        if kwargs["types"] == ["session", "episode", "note"]:
            return SearchResponse(
                results=[_result("session-1", "session", "Graph session", score=0.75)],
                total=1,
                query=kwargs["query"],
                filters={},
            )
        return SearchResponse(results=[], total=0, query=kwargs["query"], filters={})

    async def fake_raw_recall(**kwargs: Any) -> list[RawMemory]:
        raw_calls.append(kwargs)
        if kwargs["memory_scope"] == "private":
            return [_raw_memory("private-1", score=0.8)]
        return [
            _raw_memory(
                "project-1",
                memory_scope=MemoryScope.PROJECT,
                scope_key="project_123",
                score=0.9,
            )
        ]

    pack = await _compile_context_compat(
        "raw context",
        intent="build",
        project="project_123",
        accessible_projects={"project_123"},
        principal_id="user-123",
        organization_id="org-123",
        search_fn=fake_search,
        raw_memory_recall_fn=fake_raw_recall,
    )

    assert pack.layer == ContextLayer.RECALL
    assert pack.total_items == 3
    assert pack.sections[0].facet == ContextFacet.RECENT_MEMORY
    assert [item.id for item in pack.sections[0].items] == [
        "raw_memory:project-1",
        "raw_memory:private-1",
        "session-1",
    ]
    assert pack.sections[0].items[0].quality.origin == "raw_memory"
    assert pack.sections[0].items[0].quality.source == "source:project-1"
    assert pack.sections[0].items[0].quality.project_id == "project_123"
    assert "preserves verbatim source context" in pack.sections[0].items[0].reason
    assert [call["memory_scope"] for call in raw_calls] == ["private", "project"]
    assert raw_calls[0]["principal_id"] == "user-123"
    assert raw_calls[1]["scope_key"] == "project_123"
    assert ["session", "episode", "note"] in [call["types"] for call in search_calls]


@pytest.mark.asyncio
async def test_compile_context_can_include_agent_diary_with_raw_memory() -> None:
    raw_calls: list[dict[str, Any]] = []

    async def fake_raw_recall(**kwargs: Any) -> list[RawMemory]:
        raw_calls.append(kwargs)
        if kwargs.get("agent_id") == "nova":
            return [
                _raw_memory(
                    "diary-1",
                    score=0.95,
                    metadata={
                        "agent_id": "nova",
                        "memory_kind": "agent_diary",
                        "project_id": "project_123",
                    },
                    capture_surface="agent_diary",
                )
            ]
        if kwargs["memory_scope"] == "private":
            return [_raw_memory("private-1", score=0.8)]
        return []

    pack = await _compile_context_compat(
        "implementation stance",
        intent="build",
        project="project_123",
        accessible_projects={"project_123"},
        principal_id="user-123",
        agent_id="nova",
        organization_id="org-123",
        search_fn=_empty_search_response,
        raw_memory_recall_fn=fake_raw_recall,
    )

    assert [item.id for item in pack.sections[0].items] == [
        "raw_memory:diary-1",
        "raw_memory:private-1",
    ]
    assert pack.sections[0].items[0].metadata["agent_id"] == "nova"
    assert pack.sections[0].items[0].metadata["memory_kind"] == "agent_diary"
    assert pack.sections[0].items[0].metadata["project_id"] == "project_123"
    assert "agent diary matched the goal" in pack.sections[0].items[0].reason
    assert raw_calls[0]["agent_id"] is None
    assert raw_calls[2]["agent_id"] == "nova"
    assert raw_calls[2]["project_id"] == "project_123"


@pytest.mark.asyncio
async def test_compile_context_native_ranks_agent_diary_by_relevance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
                _raw_memory("private-1", score=0.3),
                _raw_memory("private-2", score=0.2),
            ]
        return []

    monkeypatch.setattr(native_module, "get_native_graph_runtime", fake_native_runtime)

    pack = await _compile_context_compat(
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


@pytest.mark.asyncio
async def test_compile_context_wake_layer_caps_items_and_skips_related() -> None:
    related_calls: list[str] = []

    async def fake_search(**kwargs: Any) -> SearchResponse:
        return SearchResponse(
            results=[
                _result(f"{kwargs['types'][0]}-{index}", kwargs["types"][0], f"Memory {index}")
                for index in range(2)
            ],
            total=2,
            query=kwargs["query"],
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

    pack = await _compile_context_compat(
        "wake up the coding session",
        intent="build",
        layer="wake",
        organization_id="org-123",
        limit=50,
        include_related=True,
        search_fn=fake_search,
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
async def test_compile_context_skips_raw_memory_without_principal() -> None:
    async def fake_raw_recall(**kwargs: Any) -> list[RawMemory]:
        raise AssertionError("raw recall requires a principal")

    pack = await _compile_context_compat(
        "raw context",
        intent="build",
        organization_id="org-123",
        search_fn=_empty_search_response,
        raw_memory_recall_fn=fake_raw_recall,
    )

    assert pack.total_items == 0


@pytest.mark.asyncio
async def test_compile_context_keeps_graph_context_when_raw_memory_fails() -> None:
    async def fake_search(**kwargs: Any) -> SearchResponse:
        return SearchResponse(
            results=[_result("decision-1", kwargs["types"][0], "Use layered packs")],
            total=1,
            query=kwargs["query"],
            filters={},
        )

    async def failing_raw_recall(**kwargs: Any) -> list[RawMemory]:
        raise RuntimeError("raw memory unavailable")

    pack = await _compile_context_compat(
        "raw context",
        intent="build",
        principal_id="user-123",
        organization_id="org-123",
        limit=1,
        search_fn=fake_search,
        raw_memory_recall_fn=failing_raw_recall,
    )

    assert pack.total_items == 1
    assert pack.items[0].id == "decision-1"


@pytest.mark.asyncio
async def test_compile_context_supports_non_software_ideation_domains() -> None:
    async def fake_search(**kwargs: Any) -> SearchResponse:
        results = []
        if kwargs["types"] == ["idea"]:
            results = [_result("idea-1", "idea", "Venue layout concept")]
        elif kwargs["types"] == ["domain", "topic", "claim"]:
            results = [_result("domain-1", "domain", "Aerial showcase")]
        return SearchResponse(
            results=results,
            total=len(results),
            query=kwargs["query"],
            filters={},
        )

    pack = await _compile_context_compat(
        "design a performance showcase",
        intent="ideate",
        domain="flow arts",
        organization_id="org-123",
        search_fn=fake_search,
    )

    assert pack.intent == ContextIntent.IDEATE
    assert pack.domain == "flow arts"
    assert [item.type for item in pack.items] == ["idea", "domain"]


@pytest.mark.asyncio
async def test_compile_context_dedupes_results_across_facets() -> None:
    async def fake_search(**kwargs: Any) -> SearchResponse:
        return SearchResponse(
            results=[_result("same-id", kwargs["types"][0], "Repeated memory")],
            total=1,
            query=kwargs["query"],
            filters={},
        )

    pack = await _compile_context_compat(
        "ship faster",
        intent="plan",
        organization_id="org-123",
        search_fn=fake_search,
    )

    assert pack.total_items == 1
    assert pack.items[0].id == "same-id"


@pytest.mark.asyncio
async def test_compile_context_falls_back_to_broad_project_search_when_facets_miss() -> None:
    calls: list[dict[str, Any]] = []

    async def fake_search(**kwargs: Any) -> SearchResponse:
        calls.append(kwargs)
        results = []
        if kwargs["types"] is None:
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
            query=kwargs["query"],
            filters={},
        )

    pack = await _compile_context_compat(
        "build project-scoped remember and recall",
        intent="build",
        domain="sibyl",
        project="project_123",
        accessible_projects={"project_123"},
        organization_id="org-123",
        search_fn=fake_search,
    )

    assert pack.total_items == 1
    assert pack.sections[0].facet == ContextFacet.DECISIONS
    assert pack.items[0].id == "decision-1"
    fallback_call = calls[-1]
    assert fallback_call["types"] is None
    assert fallback_call["category"] == "sibyl"
    assert fallback_call["project"] == "project_123"
    assert fallback_call["include_documents"] is True


@pytest.mark.asyncio
async def test_compile_context_can_attach_one_hop_related_items() -> None:
    async def fake_search(**kwargs: Any) -> SearchResponse:
        return SearchResponse(
            results=[_result("decision-1", kwargs["types"][0], "Use context packs")],
            total=1,
            query=kwargs["query"],
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

    pack = await _compile_context_compat(
        "ship faster",
        intent="decide",
        organization_id="org-123",
        limit=1,
        include_related=True,
        search_fn=fake_search,
        related_fn=fake_related,
    )

    assert pack.items[0].related[0].id == "plan-1"
    assert calls[0]["entity_id"] == "decision-1"
    assert calls[0]["organization_id"] == "org-123"


@pytest.mark.asyncio
async def test_compile_context_adds_compact_quality_metadata_from_search_result() -> None:
    async def fake_search(**kwargs: Any) -> SearchResponse:
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
            query=kwargs["query"],
            filters={},
        )

    pack = await _compile_context_compat(
        "judge memory freshness",
        intent="research",
        organization_id="org-123",
        limit=1,
        search_fn=fake_search,
    )

    quality = pack.items[0].quality
    assert quality.origin == "document"
    assert quality.source == "Sibyl docs"
    assert quality.url == "https://docs.example.test/sibyl/context"
    assert quality.project_id == "project-123"
    assert quality.updated_at == "2026-04-20T10:30:00Z"
    assert quality.created_at == "2026-04-01T09:00:00Z"


@pytest.mark.asyncio
async def test_compile_context_falls_back_to_graph_id_for_source_metadata() -> None:
    async def fake_search(**kwargs: Any) -> SearchResponse:
        return SearchResponse(
            results=[_result("decision-1", "decision", "Source-light graph decision")],
            total=1,
            query=kwargs["query"],
            filters={},
        )

    pack = await _compile_context_compat(
        "source metadata coverage",
        intent="decide",
        organization_id="org-123",
        limit=1,
        search_fn=fake_search,
    )

    item = pack.items[0]
    assert item.source == "decision-1"
    assert item.metadata["source_id"] == "decision-1"
    assert item.quality.source == "decision-1"


@pytest.mark.asyncio
async def test_compile_context_requires_goal_and_org() -> None:
    with pytest.raises(ValueError, match="goal is required"):
        await _compile_context_compat("", organization_id="org-123")

    with pytest.raises(ValueError, match="organization_id is required"):
        await _compile_context_compat("ship faster")


@pytest.mark.asyncio
async def test_context_pack_to_dict_serializes_dataclasses() -> None:
    pack = await async_compile_context_for_serialization()
    payload = context_pack_to_dict(pack)

    assert payload["goal"] == "ship faster"
    assert payload["layer"] == ContextLayer.RECALL
    assert payload["sections"][0]["items"][0]["id"] == "task-1"
    assert payload["sections"][0]["items"][0]["quality"]["origin"] == "graph"


@pytest.mark.asyncio
async def test_context_pack_to_markdown_renders_injection_shape() -> None:
    pack = await async_compile_context_for_serialization()
    markdown = context_pack_to_markdown(pack)

    assert "# Sibyl Context Pack: ship faster" in markdown
    assert "Layer: recall" in markdown
    assert "## Active Work" in markdown
    assert "**Task** (task) `task-1`" in markdown
    assert (
        "_graph; src=task-source.md; project=project-123; updated=2026-04-20T10:30:00Z"
    ) in markdown
    assert "Hint:" in markdown


async def async_compile_context_for_serialization():
    async def fake_search(**kwargs: Any) -> SearchResponse:
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
            query=kwargs["query"],
            filters={},
        )

    return await _compile_context_compat(
        "ship faster",
        intent="build",
        organization_id="org-123",
        limit=1,
        search_fn=fake_search,
    )
