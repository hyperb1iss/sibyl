from __future__ import annotations

from typing import Any, Literal

import pytest

from sibyl_core.models.context import ContextFacet, ContextIntent, ContextRelatedItem
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


@pytest.mark.asyncio
async def test_compile_context_groups_build_context_by_agent_facets() -> None:
    calls: list[dict[str, Any]] = []
    responses = {
        ("task", "epic", "project"): [_result("task-1", "task", "Build capture hook")],
        ("decision",): [_result("decision-1", "decision", "Use context packs")],
        ("rule", "convention"): [_result("rule-1", "rule", "Keep context precise")],
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

    pack = await compile_context(
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

    pack = await compile_context(
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

    pack = await compile_context(
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

    pack = await compile_context(
        "build project-scoped remember and recall",
        intent="build",
        domain="sibyl",
        project="project_123",
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

    pack = await compile_context(
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

    pack = await compile_context(
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
async def test_compile_context_requires_goal_and_org() -> None:
    with pytest.raises(ValueError, match="goal is required"):
        await compile_context("", organization_id="org-123")

    with pytest.raises(ValueError, match="organization_id is required"):
        await compile_context("ship faster")


@pytest.mark.asyncio
async def test_context_pack_to_dict_serializes_dataclasses() -> None:
    pack = await async_compile_context_for_serialization()
    payload = context_pack_to_dict(pack)

    assert payload["goal"] == "ship faster"
    assert payload["sections"][0]["items"][0]["id"] == "task-1"
    assert payload["sections"][0]["items"][0]["quality"]["origin"] == "graph"


@pytest.mark.asyncio
async def test_context_pack_to_markdown_renders_injection_shape() -> None:
    pack = await async_compile_context_for_serialization()
    markdown = context_pack_to_markdown(pack)

    assert "# Sibyl Context Pack: ship faster" in markdown
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

    return await compile_context(
        "ship faster",
        intent="build",
        organization_id="org-123",
        limit=1,
        search_fn=fake_search,
    )
