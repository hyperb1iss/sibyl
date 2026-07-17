from __future__ import annotations

import asyncio
from datetime import UTC, datetime
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
from sibyl_core.services.usage import (
    MemoryUsageItemKind,
    MemoryUsageStamp,
    MemoryUsageWriteResult,
)
from sibyl_core.tools.context import (
    FACET_TYPES,
    compile_context,
    context_item_freshness,
    context_item_lifecycle_flags,
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
    """Build a fake context_search keyed on facet.

    Native retrieval is the only runtime path; tests exercise context
    assembly by stubbing context_search and routing facet results
    through the plan-driven search the way compile_context does.
    """

    async def fake_native_context_search(**kwargs: Any) -> SearchResponse:
        if calls is not None:
            calls.append(kwargs)
        facet = kwargs.get("facet")
        requested_types = {str(value).lower() for value in kwargs.get("types") or []}
        facet_types = set(FACET_TYPES.get(facet, [])) if facet is not None else set()
        if not facet_types or requested_types - facet_types:
            items = [
                item
                for response_facet, results in responses.items()
                if not requested_types
                or requested_types.intersection(
                    {value.lower() for value in FACET_TYPES[response_facet]}
                )
                for item in results
            ]
        else:
            items = responses.get(facet, []) if facet is not None else []
        return SearchResponse(
            results=items,
            total=len(items),
            query=kwargs["plan"].query,
            filters={"types": kwargs.get("types")},
        )

    return fake_native_context_search


def _types_include(kwargs: dict[str, Any], facet: ContextFacet) -> bool:
    requested = {str(value).lower() for value in kwargs.get("types") or []}
    return not requested or bool(requested.intersection(FACET_TYPES[facet]))


@pytest.fixture(autouse=True)
def _stub_active_work_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    async def no_active_work(**kwargs: Any) -> list[ContextItem]:
        return []

    monkeypatch.setattr(context_module, "_default_active_work", no_active_work)


def test_recent_memory_types_include_projected_fact_cards() -> None:
    types = context_module._types_for_facets([ContextFacet.RECENT_MEMORY])

    assert {"claim", "event", "preference"} <= set(types)
    assert context_module._facet_for_type("event", [ContextFacet.RECENT_MEMORY]) == (
        ContextFacet.RECENT_MEMORY
    )


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
        "context_search",
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
    assert len(calls) == 1
    assert calls[0]["plan"].organization_id == "org-123"
    assert calls[0]["plan"].project == "sibyl"


@pytest.mark.asyncio
async def test_compile_context_accounts_for_exposed_items(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded_events: list[Any] = []

    async def fake_record_memory_usage(
        content_client: object,
        events: list[Any],
        *,
        graph_client: object | None = None,
    ) -> MemoryUsageWriteResult:
        recorded_events.extend(events)
        return MemoryUsageWriteResult(
            events_processed=len(events),
            stamps=tuple(
                MemoryUsageStamp(
                    item_kind=MemoryUsageItemKind(str(event.item_kind)),
                    item_id=event.item_id,
                    retrieval_count=1,
                    citation_count=0,
                    last_recalled_at=datetime.now(UTC),
                    last_used_at=None,
                )
                for event in events
            ),
        )

    responses = {
        ContextFacet.DECISIONS: [_result("decision-1", "decision", "Use context packs")],
        ContextFacet.ARTIFACTS: [
            _result(
                "document-1",
                "document",
                "Context docs",
                result_origin="document",
                metadata={"document_id": "document-1"},
            )
        ],
    }
    monkeypatch.setattr(context_module, "context_search", _facet_native_search(responses))

    with (
        patch("sibyl_core.tools.usage_exposure.get_shared_surreal_content_client", AsyncMock()),
        patch("sibyl_core.tools.usage_exposure.get_surreal_graph_client", AsyncMock()),
        patch(
            "sibyl_core.tools.usage_exposure.record_memory_usage",
            AsyncMock(side_effect=fake_record_memory_usage),
        ),
    ):
        pack = await compile_context(
            "ship context receipts",
            intent="build",
            project="project-123",
            organization_id="org-123",
            principal_id="user-123",
        )

    summary = pack.usage_metadata["usage_exposure"]
    assert summary["source_surface"] == "context_pack"
    assert summary["returned_count"] == 2
    assert summary["stamped_count"] == 1
    assert summary["excluded_count"] == 1
    assert summary["coverage_complete"] is True
    assert recorded_events[0].item_kind == MemoryUsageItemKind.GRAPH_ENTITY
    assert recorded_events[0].item_id == "decision-1"

    items = {item.id: item for item in pack.items}
    assert items["decision-1"].metadata["usage_exposure"]["status"] == "stamped"
    assert items["decision-1"].metadata["cite_id"] == "decision-1"
    assert items["document-1"].metadata["usage_exposure"] == {
        "status": "excluded",
        "signal_type": "exposure",
        "source_surface": "context_pack",
        "session_key": summary["session_key"],
        "message_key": summary["message_key"],
        "reason": "unsupported_item_kind",
    }


@pytest.mark.asyncio
async def test_compile_context_can_disable_exposure_recording(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = {
        ContextFacet.DECISIONS: [_result("decision-1", "decision", "Use context packs")],
    }
    recorder = AsyncMock()
    monkeypatch.setattr(context_module, "context_search", _facet_native_search(responses))
    monkeypatch.setattr(context_module, "annotate_context_item_exposures", recorder)

    pack = await compile_context(
        "ship context receipts",
        intent="decide",
        organization_id="org-123",
        record_exposure=False,
    )

    assert [item.id for item in pack.items] == ["decision-1"]
    assert pack.usage_metadata == {}
    recorder.assert_not_awaited()


@pytest.mark.asyncio
async def test_compile_context_supports_review_intent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []
    responses = {
        ContextFacet.VERIFICATION: [_result("claim-1", "claim", "Verify behavior")],
        ContextFacet.DECISIONS: [_result("decision-1", "decision", "Use full fidelity")],
        ContextFacet.CONSTRAINTS: [_result("guide-1", "guide", "Preserve quality")],
        ContextFacet.GOTCHAS: [_result("error-1", "error_pattern", "Avoid shortcuts")],
        ContextFacet.ARTIFACTS: [_result("artifact-1", "artifact", "Audit doc")],
        ContextFacet.ACTIVE_WORK: [_result("task-1", "task", "Review task")],
        ContextFacet.RECENT_MEMORY: [_result("note-1", "note", "Prior note")],
    }
    monkeypatch.setattr(
        context_module,
        "context_search",
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
    assert len(calls) == 1
    assert calls[0]["types"] == context_module._types_for_facets(
        [
            ContextFacet.VERIFICATION,
            ContextFacet.DECISIONS,
            ContextFacet.CONSTRAINTS,
            ContextFacet.GOTCHAS,
            ContextFacet.ARTIFACTS,
            ContextFacet.ACTIVE_WORK,
            ContextFacet.RECENT_MEMORY,
        ]
    )


@pytest.mark.asyncio
async def test_compile_context_learn_balances_operational_facets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []
    responses = {
        ContextFacet.RECENT_MEMORY: [
            _result(f"note-{index}", "note", f"Recent memory {index}", score=1.0)
            for index in range(5)
        ],
        ContextFacet.DOMAIN: [_result("topic-1", "topic", "Domain topic", score=0.2)],
        ContextFacet.PROCEDURES: [
            _result("procedure-1", "procedure", "Recovery procedure", score=0.2)
        ],
        ContextFacet.GOTCHAS: [_result("error-1", "error_pattern", "Recovery failure", score=0.1)],
        ContextFacet.DECISIONS: [_result("decision-1", "decision", "Recovery choice", score=0.2)],
    }
    monkeypatch.setattr(
        context_module,
        "context_search",
        _facet_native_search(responses, calls=calls),
    )

    pack = await compile_context(
        "learn from recovery failures",
        intent="learn",
        organization_id="org-123",
        limit=5,
    )

    assert [section.facet for section in pack.sections] == [
        ContextFacet.RECENT_MEMORY,
        ContextFacet.DOMAIN,
        ContextFacet.PROCEDURES,
        ContextFacet.GOTCHAS,
        ContextFacet.DECISIONS,
    ]
    assert [item.id for item in pack.items] == [
        "note-0",
        "topic-1",
        "procedure-1",
        "error-1",
        "decision-1",
    ]
    assert len(calls) == 1
    assert "error_pattern" in calls[0]["types"]


@pytest.mark.asyncio
async def test_compile_context_fills_facet_overflow_by_global_relevance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = {
        ContextFacet.RECENT_MEMORY: [
            _result(f"note-{index}", "note", f"Recent memory {index}", score=score)
            for index, score in enumerate([1.0, 0.9, 0.2, 0.1])
        ],
        ContextFacet.DOMAIN: [
            _result(f"topic-{index}", "topic", f"Domain topic {index}", score=score)
            for index, score in enumerate([0.8, 0.7, 0.6, 0.5])
        ],
        ContextFacet.PROCEDURES: [
            _result("procedure-1", "procedure", "Recovery procedure", score=0.4)
        ],
        ContextFacet.GOTCHAS: [_result("error-1", "error_pattern", "Recovery failure", score=0.3)],
        ContextFacet.DECISIONS: [_result("decision-1", "decision", "Recovery choice", score=0.25)],
    }
    monkeypatch.setattr(context_module, "context_search", _facet_native_search(responses))

    pack = await compile_context(
        "learn from recovery failures",
        intent="learn",
        organization_id="org-123",
        limit=10,
    )

    assert pack.total_items == 10
    assert "topic-3" in {item.id for item in pack.items}
    assert "note-3" not in {item.id for item in pack.items}


@pytest.mark.asyncio
async def test_compile_context_batches_native_facet_searches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    async def fake_native_context_search(**kwargs: Any) -> SearchResponse:
        calls.append(kwargs)
        await asyncio.sleep(0.01)
        return SearchResponse(results=[], total=0, query=kwargs["plan"].query, filters={})

    monkeypatch.setattr(context_module, "context_search", fake_native_context_search)

    pack = await compile_context(
        "parallelize context facets",
        intent="build",
        organization_id="org-123",
    )

    assert pack.total_items == 0
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_compile_context_falls_back_when_batched_native_search_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_native_context_search(**_kwargs: Any) -> SearchResponse:
        raise RuntimeError("native search unavailable")

    async def fallback_search(**kwargs: Any) -> SearchResponse:
        return SearchResponse(
            results=[_result("decision-1", "decision", "Fallback decision")],
            total=1,
            query=kwargs["query"],
            filters={},
        )

    monkeypatch.setattr(context_module, "context_search", fake_native_context_search)

    pack = await compile_context(
        "resilient native context",
        intent="build",
        organization_id="org-123",
        search_fn=fallback_search,
    )

    assert pack.total_items == 1
    assert pack.items[0].id == "decision-1"


@pytest.mark.asyncio
async def test_fallback_sections_disable_default_search_exposure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    async def fake_default_search(**kwargs: Any) -> SearchResponse:
        calls.append(kwargs)
        return SearchResponse(
            results=[_result("decision-1", "decision", "Fallback decision")],
            total=1,
            query=kwargs["query"],
            filters={},
        )

    monkeypatch.setattr(context_module, "default_search", fake_default_search)

    await context_module._compile_fallback_sections(
        query="fallback context",
        facets=[ContextFacet.DECISIONS],
        domain=None,
        project=None,
        accessible_projects=None,
        organization_id="org-123",
        limit=1,
        search_fn=fake_default_search,
    )

    assert calls[0]["record_exposure"] is False


@pytest.mark.asyncio
async def test_compile_context_defaults_to_native_retrieval_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    native_calls: list[dict[str, Any]] = []

    async def fake_native_context_search(**kwargs: Any) -> SearchResponse:
        native_calls.append(kwargs)
        if not _types_include(kwargs, ContextFacet.DECISIONS):
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

    monkeypatch.setattr(context_module, "context_search", fake_native_context_search)

    pack = await compile_context(
        "native retrieval mode",
        intent="decide",
        project="project_123",
        accessible_projects={"project_123"},
        principal_id="user-123",
        organization_id="org-123",
        search_fn=unexpected_search,
        audit=True,
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
        if not _types_include(kwargs, ContextFacet.DECISIONS):
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

    monkeypatch.setattr(context_module, "context_search", fake_native_context_search)

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
async def test_compile_context_wake_layer_caps_items_and_skips_related(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    related_calls: list[str] = []
    responses = {
        facet: [
            _result(
                f"{facet.value}-{index}",
                FACET_TYPES[facet][0],
                f"{facet.value} memory {index}",
            )
            for index in range(2)
        ]
        for facet in [
            ContextFacet.RECENT_MEMORY,
            ContextFacet.ACTIVE_WORK,
            ContextFacet.DECISIONS,
            ContextFacet.GOTCHAS,
            ContextFacet.PROCEDURES,
        ]
    }

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

    monkeypatch.setattr(context_module, "context_search", _facet_native_search(responses))

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
        ContextFacet.PROCEDURES,
    ]
    assert all(not item.related for item in pack.items)
    assert related_calls == []


@pytest.mark.asyncio
async def test_compile_context_supports_non_software_ideation_domains(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = {
        ContextFacet.IDEATION: [_result("idea-1", "idea", "Venue layout concept")],
        ContextFacet.DOMAIN: [_result("domain-1", "domain", "Aerial showcase")],
    }
    monkeypatch.setattr(context_module, "context_search", _facet_native_search(responses))

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
async def test_compile_context_filters_synthetic_relationship_claims(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = {
        ContextFacet.DOMAIN: [
            _result(
                "rel_pattern_123_belongs_to_project_456",
                "claim",
                "BELONGS_TO",
                metadata={
                    "relationship": "BELONGS_TO",
                    "source_id": "rel_pattern_123_belongs_to_project_456",
                    "source_node_uuid": "pattern_123",
                    "target_node_uuid": "project_456",
                },
            ),
            _result("claim-1", "claim", "LongMemEval receipts are required"),
        ],
    }
    monkeypatch.setattr(context_module, "context_search", _facet_native_search(responses))

    pack = await compile_context(
        "evaluate 1.0 readiness",
        intent="plan",
        domain="sibyl",
        organization_id="org-123",
    )

    assert [item.id for item in pack.items] == ["claim-1"]


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

    monkeypatch.setattr(context_module, "context_search", fake_native_context_search)

    pack = await compile_context(
        "ship faster",
        intent="plan",
        organization_id="org-123",
    )

    assert pack.total_items == 1
    assert pack.items[0].id == "same-id"


@pytest.mark.asyncio
async def test_compile_context_can_attach_one_hop_related_items(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_native_context_search(**kwargs: Any) -> SearchResponse:
        if not _types_include(kwargs, ContextFacet.DECISIONS):
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

    monkeypatch.setattr(context_module, "context_search", fake_native_context_search)

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
async def test_compile_context_batches_default_related_items(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    async def fake_native_context_search(**kwargs: Any) -> SearchResponse:
        if not _types_include(kwargs, ContextFacet.DECISIONS):
            return SearchResponse(results=[], total=0, query=kwargs["plan"].query, filters={})
        return SearchResponse(
            results=[
                _result("decision-1", "decision", "Use context packs"),
                _result("decision-2", "decision", "Batch related lookups"),
            ],
            total=2,
            query=kwargs["plan"].query,
            filters={},
        )

    monkeypatch.setattr(context_module, "context_search", fake_native_context_search)

    related_entity = SimpleNamespace(
        id="task-1",
        entity_type=SimpleNamespace(value="task"),
        name="Related task",
        content="source evidence",
        metadata={
            "operational_source_id": "experience-1",
            "project_id": "project-1",
            "source_observation_id": "state-4",
            "observation_ordinal": 4,
            "evidence_part_id": "chunk-7",
            "private_detail": "must not be exposed",
        },
    )
    relationship = SimpleNamespace(
        source_id="decision-1",
        target_id="task-1",
        relationship_type=SimpleNamespace(value="DERIVED_FROM"),
        metadata={"operational_source_id": "experience-1", "project_id": "project-1"},
    )
    relationship_manager = SimpleNamespace(
        get_related_entities=AsyncMock(return_value=[]),
        get_related_entities_batch=AsyncMock(
            return_value={"decision-1": [(related_entity, relationship)], "decision-2": []}
        ),
    )
    runtime = SimpleNamespace(relationship_manager=relationship_manager)

    with patch.object(context_module, "get_graph_runtime", AsyncMock(return_value=runtime)):
        pack = await compile_context(
            "ship faster",
            intent="decide",
            organization_id="org-123",
            limit=2,
            include_related=True,
        )

    relationship_manager.get_related_entities_batch.assert_awaited_once_with(
        ["decision-1", "decision-2"],
        limit_per_entity=3,
    )
    relationship_manager.get_related_entities.assert_not_awaited()
    assert pack.items[0].related[0].id == "task-1"
    assert pack.items[0].related[0].content == "source evidence"
    assert pack.items[0].related[0].metadata == {
        "operational_source_id": "experience-1",
        "source_observation_id": "state-4",
        "observation_ordinal": 4,
        "evidence_part_id": "chunk-7",
    }


def test_related_source_content_rejects_non_operational_lineage() -> None:
    from types import SimpleNamespace

    related_entity = SimpleNamespace(
        content="must remain hidden",
        metadata={"memory_scope": "private", "principal_id": "other-user"},
    )
    relationship = SimpleNamespace(
        source_id="decision-1",
        relationship_type=SimpleNamespace(value="DERIVED_FROM"),
        metadata={},
    )

    assert (
        context_module._related_source_content(
            related_entity,
            relationship,
            seed_id="decision-1",
        )
        is None
    )


@pytest.mark.asyncio
async def test_compile_context_filters_related_project_entities_by_own_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    async def fake_native_context_search(**kwargs: Any) -> SearchResponse:
        if not _types_include(kwargs, ContextFacet.DECISIONS):
            return SearchResponse(results=[], total=0, query=kwargs["plan"].query, filters={})
        return SearchResponse(
            results=[_result("decision-1", "decision", "Use context packs")],
            total=1,
            query=kwargs["plan"].query,
            filters={},
        )

    monkeypatch.setattr(context_module, "context_search", fake_native_context_search)

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

    monkeypatch.setattr(context_module, "context_search", fake_native_context_search)

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

    monkeypatch.setattr(context_module, "context_search", fake_native_context_search)

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

    import sibyl_core.retrieval.search as search_module
    from sibyl_core.services.surreal_content import RawMemory

    class EmptyNativeClient:
        async def execute_query(self, *_args: object, **_kwargs: object) -> list[object]:
            return []

    class EmptyNativeRuntime:
        client = EmptyNativeClient()

    async def fake_native_runtime(_organization_id: str, **_kwargs: object) -> EmptyNativeRuntime:
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

    monkeypatch.setattr(search_module, "get_surreal_graph_runtime", fake_native_runtime)

    pack = await compile_context(
        "What should Nova recall from the diary for delegated handoff?",
        intent="learn",
        domain="sibyl",
        principal_id="user-123",
        agent_id="nova",
        organization_id="org-123",
        limit=8,
        raw_memory_recall_fn=fake_raw_recall,
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
    assert context_item_lifecycle_state(item) == "active"
    assert context_item_lifecycle_flags(item) == ["redacted"]


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
    assert context_item_lifecycle_state(item) == "active"
    assert context_item_lifecycle_flags(item) == ["hidden"]


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
    assert "_src=task-source.md; project=project-123; updated=2026-04-20_" in markdown
    assert "Why:" not in markdown
    assert "Hint:" in markdown


async def async_compile_context_for_serialization(monkeypatch: pytest.MonkeyPatch):
    async def fake_native_context_search(**kwargs: Any) -> SearchResponse:
        if not _types_include(kwargs, ContextFacet.ACTIVE_WORK):
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

    monkeypatch.setattr(context_module, "context_search", fake_native_context_search)

    return await compile_context(
        "ship faster",
        intent="build",
        organization_id="org-123",
        limit=1,
    )


@pytest.mark.asyncio
async def test_compile_context_routes_done_tasks_to_prior_art(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = {
        ContextFacet.ACTIVE_WORK: [
            _result("task-doing", "task", "Live task", metadata={"status": "doing"}),
            _result(
                "task-done",
                "task",
                "Finished task",
                score=0.95,
                metadata={"status": "done", "learnings": "Pool size must match concurrency."},
            ),
            _result("task-archived", "task", "Dead task", metadata={"status": "archived"}),
        ],
    }
    monkeypatch.setattr(context_module, "context_search", _facet_native_search(responses))

    pack = await compile_context(
        "ship retrieval",
        intent="build",
        organization_id="org-123",
    )

    by_facet = {section.facet: section for section in pack.sections}
    active = by_facet[ContextFacet.ACTIVE_WORK]
    prior = by_facet[ContextFacet.PRIOR_ART]
    assert [item.id for item in active.items] == ["task-doing"]
    assert [item.id for item in prior.items] == ["task-done"]
    assert all(item.id != "task-archived" for item in pack.items)


@pytest.mark.asyncio
async def test_prior_art_items_promote_learnings_to_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = {
        ContextFacet.ACTIVE_WORK: [
            _result(
                "task-done",
                "task",
                "Finished task",
                metadata={"status": "done", "learnings": "Pool size must match concurrency."},
            ),
        ],
    }
    monkeypatch.setattr(context_module, "context_search", _facet_native_search(responses))

    pack = await compile_context(
        "ship retrieval",
        intent="build",
        organization_id="org-123",
    )

    item = pack.items[0]
    assert item.facet == ContextFacet.PRIOR_ART
    assert item.content == "Pool size must match concurrency."
    assert "learnings" in item.reason or "completed" in item.reason


@pytest.mark.asyncio
async def test_compile_context_drops_done_tasks_without_prior_art_facet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = {
        ContextFacet.ACTIVE_WORK: [
            _result("task-done", "task", "Finished task", metadata={"status": "done"}),
        ],
    }
    monkeypatch.setattr(context_module, "context_search", _facet_native_search(responses))

    pack = await compile_context(
        "learn from memory",
        intent="learn",
        organization_id="org-123",
    )

    assert all(item.id != "task-done" for item in pack.items)


@pytest.mark.asyncio
async def test_compile_context_statusless_work_items_stay_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = {
        ContextFacet.ACTIVE_WORK: [
            _result("epic-1", "epic", "Live epic"),
        ],
    }
    monkeypatch.setattr(context_module, "context_search", _facet_native_search(responses))

    pack = await compile_context(
        "ship retrieval",
        intent="build",
        organization_id="org-123",
    )

    assert pack.items[0].facet == ContextFacet.ACTIVE_WORK


@pytest.mark.asyncio
async def test_compile_context_direct_active_lookup_leads_active_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = {
        ContextFacet.ACTIVE_WORK: [
            _result(
                "task-semantic",
                "task",
                "Semantically matched task",
                score=1.4,
                metadata={"status": "doing"},
            ),
            _result(
                "task-direct",
                "task",
                "Currently doing task",
                score=1.2,
                metadata={"status": "doing"},
            ),
        ],
    }
    monkeypatch.setattr(context_module, "context_search", _facet_native_search(responses))

    async def fake_active_work(**kwargs: Any) -> list[ContextItem]:
        assert kwargs["organization_id"] == "org-123"
        assert kwargs["project"] == "project-1"
        return [
            ContextItem(
                id="task-direct",
                type="task",
                name="Currently doing task",
                content="In flight",
                score=0.0,
                facet=ContextFacet.ACTIVE_WORK,
                reason="task is currently in progress for this project",
                source="task-direct",
                metadata={"active_lookup": True, "status": "doing", "source_id": "task-direct"},
            )
        ]

    pack = await compile_context(
        "ship retrieval",
        intent="build",
        project="project-1",
        organization_id="org-123",
        active_work_fn=fake_active_work,
    )

    active = next(s for s in pack.sections if s.facet == ContextFacet.ACTIVE_WORK)
    assert [item.id for item in active.items] == ["task-direct", "task-semantic"]
    assert active.items[0].metadata.get("active_lookup") is True


@pytest.mark.asyncio
async def test_compile_context_direct_active_lookup_respects_api_key_project_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sibyl_core.auth.memory_policy import memory_scope_policy_key
    from sibyl_core.services.surreal_content import MemoryScope

    active_calls: list[dict[str, Any]] = []

    async def fake_native_context_search(**kwargs: Any) -> SearchResponse:
        assert kwargs["plan"].accessible_projects == frozenset()
        return SearchResponse(results=[], total=0, query=kwargs["plan"].query, filters={})

    async def fake_active_work(**kwargs: Any) -> list[ContextItem]:
        active_calls.append(kwargs)
        return [
            ContextItem(
                id="task-denied",
                type="task",
                name="Denied active task",
                content="Sensitive denied project work",
                score=0.0,
                facet=ContextFacet.ACTIVE_WORK,
                reason="task is currently in progress for this project",
            )
        ]

    monkeypatch.setattr(context_module, "context_search", fake_native_context_search)

    pack = await compile_context(
        "ship retrieval",
        intent="build",
        project="project-denied",
        accessible_projects={"project-denied"},
        principal_id="user-123",
        organization_id="org-123",
        active_work_fn=fake_active_work,
        allowed_memory_scope_keys={
            memory_scope_policy_key(MemoryScope.PRIVATE, "user-123"),
        },
    )

    assert active_calls == []
    assert pack.items == []


@pytest.mark.asyncio
async def test_compile_context_active_lookup_failure_degrades_gracefully(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = {
        ContextFacet.ACTIVE_WORK: [
            _result("task-1", "task", "Live task", metadata={"status": "doing"}),
        ],
    }
    monkeypatch.setattr(context_module, "context_search", _facet_native_search(responses))

    async def broken_active_work(**kwargs: Any) -> list[ContextItem]:
        msg = "graph offline"
        raise RuntimeError(msg)

    pack = await compile_context(
        "ship retrieval",
        intent="build",
        project="project-1",
        organization_id="org-123",
        active_work_fn=broken_active_work,
    )

    assert [item.id for item in pack.items] == ["task-1"]


_NOISY_METADATA = {
    "status": "done",
    "learnings": "Keep pool size at concurrency.",
    "tags": ["surreal"],
    "priority": "high",
    "updated_at": "2026-05-12T08:04:21Z",
    "metadata": '{"nested": "double-serialized copy"}',
    "retrieval_signals": ["node_vector"],
    "retrieval_ranks": {"node_vector": 1},
    "retrieval_scores": {"node_vector": 0.45},
    "candidate_kind": "node",
    "candidate_project_id": "project-1",
    "candidate_visibility": "project",
    "candidate_policy_reason": "project_access_verified",
    "policy_reason": "project_access_verified",
    "embedding_metadata": {"provider": "openai"},
    "graph_expansion_depth": 1,
    "graph_native_signal_boost": 1.2,
    "freshness": 1.01,
    "_direct_insert": True,
    "created_by": "user-uuid",
    "branch_name": "task/foo",
    "commit_shas": [],
    "assignees": ["system"],
}


@pytest.mark.asyncio
async def test_pack_items_trim_retrieval_plumbing_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = {
        ContextFacet.ACTIVE_WORK: [
            _result("task-1", "task", "Task", metadata=dict(_NOISY_METADATA)),
        ],
    }
    monkeypatch.setattr(context_module, "context_search", _facet_native_search(responses))

    pack = await compile_context("ship faster", intent="build", organization_id="org-123")

    item = pack.items[0]
    kept = set(item.metadata)
    assert "status" in kept
    assert "learnings" in kept
    assert "tags" in kept
    assert "priority" in kept
    assert "updated_at" in kept
    assert "source_id" in kept
    for noisy in (
        "metadata",
        "retrieval_signals",
        "retrieval_ranks",
        "retrieval_scores",
        "candidate_kind",
        "candidate_project_id",
        "candidate_visibility",
        "candidate_policy_reason",
        "policy_reason",
        "embedding_metadata",
        "graph_expansion_depth",
        "graph_native_signal_boost",
        "freshness",
        "_direct_insert",
        "created_by",
        "branch_name",
        "commit_shas",
        "assignees",
    ):
        assert noisy not in kept, f"{noisy} leaked into lean pack metadata"


@pytest.mark.asyncio
async def test_pack_items_keep_full_metadata_in_audit_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = {
        ContextFacet.ACTIVE_WORK: [
            _result("task-1", "task", "Task", metadata=dict(_NOISY_METADATA)),
        ],
    }
    monkeypatch.setattr(context_module, "context_search", _facet_native_search(responses))

    pack = await compile_context(
        "ship faster",
        intent="build",
        organization_id="org-123",
        audit=True,
    )

    item = pack.items[0]
    assert item.metadata.get("retrieval_signals") == ["node_vector"]
    assert item.metadata.get("candidate_kind") == "node"
    assert item.metadata.get("embedding_metadata") == {"provider": "openai"}


def test_markdown_renderer_drops_redundant_provenance() -> None:
    from sibyl_core.models.context import ContextPack, ContextSection

    item = ContextItem(
        id="task-1",
        type="task",
        name="Task",
        content="Do the thing",
        score=1.0,
        facet=ContextFacet.ACTIVE_WORK,
        reason="task can change what the agent should do next",
        source="task-1",
        quality=ContextItemQualityMetadata(
            origin="graph",
            source="task-1",
            project_id="project-1",
            updated_at="2026-05-12T10:51:38.001928+00:00",
        ),
        metadata={"status": "doing", "source_id": "task-1"},
        related=[
            ContextRelatedItem(
                id="project-1",
                type="project",
                name="sibyl",
                relationship="BELONGS_TO",
                direction="outgoing",
            ),
            ContextRelatedItem(
                id="task-2",
                type="task",
                name="Other task",
                relationship="DEPENDS_ON",
                direction="outgoing",
            ),
        ],
    )
    pack = ContextPack(
        goal="ship",
        intent=ContextIntent.BUILD,
        query="ship",
        domain=None,
        project="project-1",
        sections=[
            ContextSection(facet=ContextFacet.ACTIVE_WORK, title="Active Work", items=[item])
        ],
        total_items=1,
        layer=ContextLayer.RECALL,
    )

    markdown = context_pack_to_markdown(pack)

    assert "**Task** (task · doing) `task-1`" in markdown
    assert "src=" not in markdown
    assert "project=" not in markdown
    assert "_graph" not in markdown
    assert "updated=2026-05-12_" in markdown or "updated=2026-05-12;" in markdown
    assert "BELONGS_TO sibyl" not in markdown
    assert "DEPENDS_ON Other task (task)" in markdown
    assert "Why:" not in markdown


def test_markdown_renderer_omits_related_line_when_only_project_edges() -> None:
    from sibyl_core.models.context import ContextPack, ContextSection

    item = ContextItem(
        id="decision-1",
        type="decision",
        name="Decision",
        content="We chose X",
        score=1.0,
        facet=ContextFacet.DECISIONS,
        reason="decision records a choice",
        source="decision-1",
        metadata={"source_id": "decision-1"},
        related=[
            ContextRelatedItem(
                id="project-1",
                type="project",
                name="sibyl",
                relationship="BELONGS_TO",
                direction="outgoing",
            ),
        ],
    )
    pack = ContextPack(
        goal="ship",
        intent=ContextIntent.BUILD,
        query="ship",
        domain=None,
        project="project-1",
        sections=[ContextSection(facet=ContextFacet.DECISIONS, title="Decisions", items=[item])],
        total_items=1,
        layer=ContextLayer.RECALL,
    )

    markdown = context_pack_to_markdown(pack)

    assert "Related:" not in markdown


@pytest.mark.asyncio
async def test_lineage_dedup_drops_procedure_mirrors_of_tasks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = {
        ContextFacet.ACTIVE_WORK: [
            _result(
                "task-1",
                "task",
                "Build native retrieval baseline",
                score=1.2,
                metadata={"status": "doing"},
            ),
        ],
        ContextFacet.PROCEDURES: [
            _result(
                "procedure-1",
                "procedure",
                "Procedure: Build native retrieval baseline",
                score=1.4,
            ),
            _result("procedure-2", "procedure", "Independent runbook", score=0.7),
        ],
    }
    monkeypatch.setattr(context_module, "context_search", _facet_native_search(responses))

    pack = await compile_context("ship retrieval", intent="build", organization_id="org-123")

    ids = [item.id for item in pack.items]
    assert "task-1" in ids
    assert "procedure-1" not in ids
    assert "procedure-2" in ids


@pytest.mark.asyncio
async def test_lineage_dedup_prefers_decision_over_raw_memory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = {
        ContextFacet.DECISIONS: [
            _result("decision-1", "decision", "RC packet separates closure", score=0.8),
        ],
        ContextFacet.RECENT_MEMORY: [
            _result(
                "raw_memory:abc",
                "raw_memory",
                "RC packet separates closure",
                score=1.5,
            ),
        ],
    }
    monkeypatch.setattr(context_module, "context_search", _facet_native_search(responses))

    pack = await compile_context("continue rc work", intent="build", organization_id="org-123")

    ids = [item.id for item in pack.items]
    assert "decision-1" in ids
    assert "raw_memory:abc" not in ids


@pytest.mark.asyncio
async def test_lineage_dedup_collapses_same_name_duplicates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = {
        ContextFacet.RECENT_MEMORY: [
            _result("raw_memory:one", "raw_memory", "Raw saves refresh embeddings", score=0.9),
            _result("raw_memory:two", "raw_memory", "Raw saves refresh embeddings", score=0.5),
        ],
    }
    monkeypatch.setattr(context_module, "context_search", _facet_native_search(responses))

    pack = await compile_context("debug recall", intent="learn", organization_id="org-123")

    matching = [item for item in pack.items if item.name == "Raw saves refresh embeddings"]
    assert len(matching) == 1
    assert matching[0].id == "raw_memory:one"


def _budget_pack(item_count: int):
    from sibyl_core.models.context import ContextPack, ContextSection

    items = [
        ContextItem(
            id=f"decision-{index}",
            type="decision",
            name=f"Decision {index}",
            content="A decision body that takes up meaningful space in the pack " * 4,
            score=1.0,
            facet=ContextFacet.DECISIONS,
            reason="decision records a choice",
            source=f"decision-{index}",
            metadata={"source_id": f"decision-{index}"},
        )
        for index in range(item_count)
    ]
    return ContextPack(
        goal="ship",
        intent=ContextIntent.BUILD,
        query="ship",
        domain=None,
        project=None,
        sections=[ContextSection(facet=ContextFacet.DECISIONS, title="Decisions", items=items)],
        total_items=item_count,
        layer=ContextLayer.RECALL,
    )


def test_markdown_token_budget_trims_items() -> None:
    pack = _budget_pack(8)

    full = context_pack_to_markdown(pack, max_items=8, items_per_section=8)
    trimmed = context_pack_to_markdown(pack, max_items=8, items_per_section=8, token_budget=200)

    assert len(trimmed) < len(full)
    assert len(trimmed) <= 200 * 4 + 120
    assert "Decision 0" in trimmed
    assert "Trimmed to ~200 tokens" in trimmed


def test_markdown_renderer_prioritizes_relevant_recent_memory() -> None:
    from sibyl_core.models.context import ContextPack, ContextSection

    gotcha = ContextItem(
        id="error-1",
        type="error_pattern",
        name="Generic deploy gotcha",
        content="This is less specific context.",
        score=0.9,
        facet=ContextFacet.GOTCHAS,
        reason="error_pattern can prevent repeated mistakes",
        metadata={"source_id": "error-1"},
    )
    recent = ContextItem(
        id="raw_memory:ha-runbook",
        type="raw_memory",
        name="HA deploy runbook",
        content="Use the GitHub sync path, then sudo git pull on the box.",
        score=0.7,
        facet=ContextFacet.RECENT_MEMORY,
        reason="raw memory matched the goal",
        metadata={"source_id": "cli:manual"},
    )
    pack = ContextPack(
        goal="deploy HA",
        intent=ContextIntent.DEBUG,
        query="deploy HA",
        domain=None,
        project=None,
        sections=[
            ContextSection(facet=ContextFacet.GOTCHAS, title="Gotchas", items=[gotcha]),
            ContextSection(facet=ContextFacet.RECENT_MEMORY, title="Recent Memory", items=[recent]),
        ],
        total_items=2,
        layer=ContextLayer.RECALL,
    )

    markdown = context_pack_to_markdown(pack, max_items=1)

    assert "HA deploy runbook" in markdown
    assert "Generic deploy gotcha" not in markdown


@pytest.mark.parametrize("intent", [ContextIntent.BUILD, ContextIntent.GENERAL])
def test_markdown_renderer_anchors_active_work_for_work_intents(
    intent: ContextIntent,
) -> None:
    from sibyl_core.models.context import ContextPack, ContextSection

    active = ContextItem(
        id="task-1",
        type="task",
        name="Ship context work",
        content="Current task should stay visible in work-oriented context.",
        score=0.0,
        facet=ContextFacet.ACTIVE_WORK,
        reason="active task lookup",
        metadata={"active_lookup": True, "status": "doing"},
    )
    decision = ContextItem(
        id="decision-1",
        type="decision",
        name="High-scoring decision",
        content="Relevant, but not the current task spine.",
        score=1.4,
        facet=ContextFacet.DECISIONS,
        reason="decision matched the goal",
        metadata={"source_id": "decision-1"},
    )
    pack = ContextPack(
        goal="ship context work",
        intent=intent,
        query="ship context work",
        domain=None,
        project=None,
        sections=[
            ContextSection(facet=ContextFacet.DECISIONS, title="Decisions", items=[decision]),
            ContextSection(facet=ContextFacet.ACTIVE_WORK, title="Active Work", items=[active]),
        ],
        total_items=2,
        layer=ContextLayer.WAKE,
    )

    markdown = context_pack_to_markdown(pack, max_items=1)

    assert "Ship context work" in markdown
    assert "High-scoring decision" not in markdown


def test_markdown_renderer_gives_active_lookup_a_floor_for_debug() -> None:
    from sibyl_core.models.context import ContextPack, ContextSection

    active = ContextItem(
        id="task-1",
        type="task",
        name="Current debugging task",
        content="Active work should not vanish behind a weak gotcha match.",
        score=0.0,
        facet=ContextFacet.ACTIVE_WORK,
        reason="active task lookup",
        metadata={"active_lookup": True, "status": "doing"},
    )
    gotcha = ContextItem(
        id="error-1",
        type="error_pattern",
        name="Weak gotcha",
        content="Potentially relevant, but lower signal.",
        score=0.7,
        facet=ContextFacet.GOTCHAS,
        reason="error pattern matched loosely",
        metadata={"source_id": "error-1"},
    )
    pack = ContextPack(
        goal="debug context output",
        intent=ContextIntent.DEBUG,
        query="debug context output",
        domain=None,
        project=None,
        sections=[
            ContextSection(facet=ContextFacet.GOTCHAS, title="Gotchas", items=[gotcha]),
            ContextSection(facet=ContextFacet.ACTIVE_WORK, title="Active Work", items=[active]),
        ],
        total_items=2,
        layer=ContextLayer.RECALL,
    )

    markdown = context_pack_to_markdown(pack, max_items=1)

    assert "Current debugging task" in markdown
    assert "Weak gotcha" not in markdown


def test_markdown_token_budget_always_renders_first_item() -> None:
    pack = _budget_pack(3)

    trimmed = context_pack_to_markdown(pack, token_budget=100)

    assert "Decision 0" in trimmed


@pytest.mark.asyncio
async def test_lean_metadata_drops_description_equal_to_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = {
        ContextFacet.DECISIONS: [
            _result(
                "decision-1",
                "decision",
                "Decision",
                metadata={"description": "Decision content"},
            ),
            _result(
                "decision-2",
                "decision",
                "Other",
                metadata={"description": "A longer different summary"},
            ),
        ],
    }
    monkeypatch.setattr(context_module, "context_search", _facet_native_search(responses))

    pack = await compile_context("ship", intent="decide", organization_id="org-123")

    by_id = {item.id: item for item in pack.items}
    assert "description" not in by_id["decision-1"].metadata
    assert by_id["decision-2"].metadata["description"] == "A longer different summary"


@pytest.mark.asyncio
async def test_completed_epics_route_to_prior_art(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = {
        ContextFacet.ACTIVE_WORK: [
            _result("epic-done", "epic", "Shipped epic", metadata={"status": "completed"}),
            _result("epic-live", "epic", "Live epic", metadata={"status": "in_progress"}),
        ],
    }
    monkeypatch.setattr(context_module, "context_search", _facet_native_search(responses))

    pack = await compile_context("ship retrieval", intent="build", organization_id="org-123")

    by_facet = {item.id: item.facet for item in pack.items}
    assert by_facet["epic-done"] == ContextFacet.PRIOR_ART
    assert by_facet["epic-live"] == ContextFacet.ACTIVE_WORK


@pytest.mark.asyncio
async def test_lean_metadata_keeps_policy_gate_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = {
        ContextFacet.RECENT_MEMORY: [
            _result(
                "raw_memory:abc",
                "raw_memory",
                "Private capture",
                metadata={
                    "memory_scope": "private",
                    "principal_id": "user-1",
                    "scope_key": "user-1",
                    "redacted": True,
                    "superseded_by_source_id": "raw_memory:def",
                    "unresolved_claims": ["claim-1"],
                    "supported": False,
                    "retrieval_signals": ["raw_lexical"],
                },
            ),
        ],
    }
    monkeypatch.setattr(context_module, "context_search", _facet_native_search(responses))

    pack = await compile_context("recall private", intent="learn", organization_id="org-123")

    metadata = pack.items[0].metadata
    assert metadata["memory_scope"] == "private"
    assert metadata["principal_id"] == "user-1"
    assert metadata["scope_key"] == "user-1"
    assert metadata["redacted"] is True
    assert metadata["superseded_by_source_id"] == "raw_memory:def"
    assert metadata["unresolved_claims"] == ["claim-1"]
    assert metadata["supported"] is False
    assert "retrieval_signals" not in metadata


@pytest.mark.asyncio
async def test_lineage_dedup_keeps_in_flight_task_over_same_named_decision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = {
        ContextFacet.ACTIVE_WORK: [
            _result(
                "task-doing",
                "task",
                "Ship native retrieval",
                score=2.0,
                metadata={"status": "doing"},
            ),
        ],
        ContextFacet.DECISIONS: [
            _result("decision-1", "decision", "Ship native retrieval", score=0.5),
        ],
    }
    monkeypatch.setattr(context_module, "context_search", _facet_native_search(responses))

    pack = await compile_context("ship retrieval", intent="build", organization_id="org-123")

    ids = [item.id for item in pack.items]
    assert "task-doing" in ids
    assert "decision-1" not in ids


def test_merge_active_work_tolerates_unknown_section_facets() -> None:
    from sibyl_core.models.context import ContextSection

    item = ContextItem(
        id="task-1",
        type="task",
        name="Live task",
        content="In flight",
        score=0.0,
        facet=ContextFacet.ACTIVE_WORK,
        reason="task is currently in progress for this project",
        metadata={"active_lookup": True},
    )
    sections = [
        ContextSection(facet=ContextFacet.GOTCHAS, title="Gotchas", items=[]),
    ]

    merged = context_module._merge_active_work(
        sections,
        [item],
        [ContextFacet.ACTIVE_WORK, ContextFacet.DECISIONS],
    )

    assert any(section.facet is ContextFacet.ACTIVE_WORK for section in merged)


def test_markdown_skips_memory_line_when_content_equals_name() -> None:
    from sibyl_core.models.context import ContextPack, ContextSection

    item = ContextItem(
        id="task-1",
        type="task",
        name="De-noise context packs",
        content="De-noise context packs",
        score=0.0,
        facet=ContextFacet.ACTIVE_WORK,
        reason="task is currently in progress for this project",
        metadata={"status": "doing"},
    )
    pack = ContextPack(
        goal="ship",
        intent=ContextIntent.BUILD,
        query="ship",
        domain=None,
        project=None,
        sections=[
            ContextSection(facet=ContextFacet.ACTIVE_WORK, title="Active Work", items=[item])
        ],
        total_items=1,
        layer=ContextLayer.RECALL,
    )

    markdown = context_pack_to_markdown(pack)

    assert "**De-noise context packs** (task · doing)" in markdown
    assert "Memory:" not in markdown
