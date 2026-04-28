from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import pytest

from sibyl_core.evals import (
    ContextPackCaseResult,
    ContextPackEvalCase,
    ContextPackEvalReport,
    ContextPackFixture,
    context_pack_from_dict,
    evaluate_context_pack,
    load_context_pack_cases,
)
from sibyl_core.models.context import (
    ContextFacet,
    ContextIntent,
    ContextItem,
    ContextLayer,
    ContextPack,
    ContextSection,
)
from sibyl_core.services.surreal_content import (
    AGENT_DIARY_CAPTURE_SURFACE,
    MemoryScope,
    RawMemory,
)
from sibyl_core.tools.context import compile_context
from sibyl_core.tools.responses import SearchResponse, SearchResult


def _result(
    entity_id: str,
    entity_type: str,
    name: str,
    content: str,
    *,
    score: float = 0.9,
    source: str | None = "seeded fixture",
    result_origin: Literal["graph", "document"] = "graph",
    metadata: dict[str, Any] | None = None,
) -> SearchResult:
    return SearchResult(
        id=entity_id,
        type=entity_type,
        name=name,
        content=content,
        score=score,
        source=source,
        result_origin=result_origin,
        metadata={"entity_type": entity_type, "source_id": f"src-{entity_id}", **(metadata or {})},
    )


def _raw_memory(
    memory_id: str,
    *,
    principal_id: str = "user-123",
    memory_scope: MemoryScope = MemoryScope.PRIVATE,
    scope_key: str | None = None,
    score: float = 0.8,
    metadata: dict[str, Any] | None = None,
    capture_surface: str = "cli",
) -> RawMemory:
    return RawMemory(
        id=memory_id,
        organization_id="org-123",
        source_id=f"source:{memory_id}",
        principal_id=principal_id,
        memory_scope=memory_scope,
        scope_key=scope_key,
        title=f"Raw {memory_id}",
        raw_content=f"Raw {memory_id} content anchors scoped context recall.",
        tags=["fixture"],
        metadata={"source_name": "eval-fixture", **(metadata or {})},
        provenance={"message_id": memory_id},
        capture_surface=capture_surface,
        captured_at=datetime(2026, 4, 27, 12, 0, 0, tzinfo=UTC),
        created_at=datetime(2026, 4, 27, 12, 0, 0, tzinfo=UTC),
        score=score,
    )


@pytest.mark.asyncio
async def test_context_pack_fixture_passes_coding_handoff_requirements() -> None:
    async def fake_search(**kwargs: Any) -> SearchResponse:
        by_types = {
            ("task", "epic", "project"): [
                _result(
                    "task-active",
                    "task",
                    "Implement native RawMemory slice",
                    "Current task is the native RawMemory baseline for scoped recall.",
                    metadata={"project_id": "project-sibyl"},
                )
            ],
            ("decision",): [
                _result(
                    "decision-source-law",
                    "decision",
                    "Raw memory stays source-grounded",
                    "Decision: preserve source IDs before extraction or graph traversal.",
                    metadata={"project_id": "project-sibyl"},
                )
            ],
            ("artifact", "document", "source", "config_file"): [
                _result(
                    "artifact-test",
                    "artifact",
                    "Context pack tests",
                    "Relevant tests include test_context_pack and test_context_pack_evals.",
                    result_origin="document",
                    metadata={"project_id": "project-sibyl"},
                )
            ],
            ("error_pattern", "pattern"): [
                _result(
                    "risk-privacy",
                    "pattern",
                    "Remaining risk: private memory leakage",
                    "Remaining risk is leaking private memory into project context packs.",
                    metadata={"project_id": "project-sibyl"},
                )
            ],
        }
        results = by_types.get(tuple(kwargs["types"] or ()), [])
        return SearchResponse(
            results=results,
            total=len(results),
            query=kwargs["query"],
            filters={"types": kwargs["types"]},
        )

    pack = await compile_context(
        "handoff the native memory implementation",
        intent="build",
        domain="sibyl",
        project="project-sibyl",
        organization_id="org-hyperbliss",
        search_fn=fake_search,
    )

    result = evaluate_context_pack(
        pack,
        ContextPackFixture(
            name="coding-handoff",
            required_item_ids={"task-active", "decision-source-law", "artifact-test"},
            required_facets={
                ContextFacet.ACTIVE_WORK,
                ContextFacet.DECISIONS,
                ContextFacet.ARTIFACTS,
            },
            required_facet_order=[
                ContextFacet.ACTIVE_WORK,
                ContextFacet.DECISIONS,
                ContextFacet.GOTCHAS,
                ContextFacet.ARTIFACTS,
            ],
            required_terms={"remaining risk", "test_context_pack"},
            max_items=8,
            max_markdown_chars=5000,
            require_source_metadata=True,
        ),
    )

    assert result.passed, result.failures
    assert result.metrics["required_item_coverage"] == 1.0
    assert result.metrics["items"] == 4
    assert result.metrics["facet_order_matches"] is True
    assert result.metrics["source_metadata_coverage"] == 1.0


@pytest.mark.asyncio
async def test_context_pack_fixture_passes_raw_memory_scope_requirements() -> None:
    async def fake_search(**kwargs: Any) -> SearchResponse:
        return SearchResponse(results=[], total=0, query=kwargs["query"], filters={})

    async def fake_raw_recall(**kwargs: Any) -> list[RawMemory]:
        if kwargs["memory_scope"] == "private":
            return [_raw_memory("private-1")]
        return [
            _raw_memory(
                "project-1",
                memory_scope=MemoryScope.PROJECT,
                scope_key="project_123",
                score=0.9,
            )
        ]

    pack = await compile_context(
        "raw scoped context",
        intent="build",
        project="project_123",
        principal_id="user-123",
        organization_id="org-123",
        search_fn=fake_search,
        raw_memory_recall_fn=fake_raw_recall,
    )

    result = evaluate_context_pack(
        pack,
        ContextPackFixture(
            name="raw-scope-grounding",
            required_item_ids={"raw_memory:project-1", "raw_memory:private-1"},
            required_facets={ContextFacet.RECENT_MEMORY},
            required_layer=ContextLayer.RECALL,
            required_terms={"verbatim source context", "scoped context recall"},
            required_item_metadata={
                "raw_memory:project-1": {
                    "memory_scope": "project",
                    "scope_key": "project_123",
                    "source_id": "source:project-1",
                },
                "raw_memory:private-1": {
                    "memory_scope": "private",
                    "scope_key": None,
                    "source_id": "source:private-1",
                },
            },
            require_source_metadata=True,
        ),
    )

    assert result.passed, result.failures
    assert result.metrics["metadata_requirement_coverage"] == 1.0


@pytest.mark.asyncio
async def test_context_pack_fixture_passes_multi_user_scope_requirements() -> None:
    async def fake_search(**kwargs: Any) -> SearchResponse:
        return SearchResponse(results=[], total=0, query=kwargs["query"], filters={})

    async def fake_raw_recall(**kwargs: Any) -> list[RawMemory]:
        assert kwargs["principal_id"] == "user-123"
        if kwargs["memory_scope"] == "private":
            return [_raw_memory("user-123-private", principal_id="user-123")]
        return [
            _raw_memory(
                "project-123-shared",
                principal_id="user-123",
                memory_scope=MemoryScope.PROJECT,
                scope_key="project_123",
                score=0.9,
            )
        ]

    pack = await compile_context(
        "handoff scoped retrieval",
        intent="build",
        project="project_123",
        principal_id="user-123",
        organization_id="org-123",
        search_fn=fake_search,
        raw_memory_recall_fn=fake_raw_recall,
    )

    result = evaluate_context_pack(
        pack,
        ContextPackFixture(
            name="multi-user-scoped-retrieval",
            required_item_ids={
                "raw_memory:user-123-private",
                "raw_memory:project-123-shared",
            },
            required_metadata_by_type={
                "raw_memory": {
                    "principal_id": "user-123",
                }
            },
            require_source_metadata=True,
        ),
    )

    assert result.passed, result.failures
    assert result.metrics["metadata_requirement_coverage"] == 1.0


def test_context_pack_fixture_reports_multi_user_raw_memory_leak() -> None:
    pack = ContextPack(
        goal="handoff scoped retrieval",
        intent=ContextIntent.BUILD,
        query="handoff scoped retrieval",
        domain="sibyl",
        project="project_123",
        sections=[
            ContextSection(
                facet=ContextFacet.RECENT_MEMORY,
                title="Recent Memory",
                items=[
                    ContextItem(
                        id="raw_memory:other-user-private",
                        type="raw_memory",
                        name="Other user private memory",
                        content="This private memory belongs to another principal.",
                        score=0.95,
                        facet=ContextFacet.RECENT_MEMORY,
                        reason="raw memory matched the goal",
                        source="source:other-user-private",
                        metadata={
                            "source_id": "source:other-user-private",
                            "principal_id": "user-456",
                            "memory_scope": "private",
                        },
                    )
                ],
            )
        ],
        total_items=1,
    )

    result = evaluate_context_pack(
        pack,
        ContextPackFixture(
            name="multi-user-scoped-retrieval",
            required_metadata_by_type={
                "raw_memory": {
                    "principal_id": "user-123",
                    "memory_scope": "private",
                }
            },
        ),
    )

    assert not result.passed
    assert result.failures == [
        "raw_memory item raw_memory:other-user-private metadata principal_id "
        "expected 'user-123' got 'user-456'"
    ]
    assert result.metrics["metadata_requirement_coverage"] == 0.5


@pytest.mark.asyncio
async def test_context_pack_fixture_passes_agent_diary_requirements() -> None:
    async def fake_search(**kwargs: Any) -> SearchResponse:
        return SearchResponse(results=[], total=0, query=kwargs["query"], filters={})

    async def fake_raw_recall(**kwargs: Any) -> list[RawMemory]:
        if kwargs.get("agent_id") == "nova":
            return [
                _raw_memory(
                    "nova-diary-1",
                    score=0.95,
                    metadata={
                        "agent_id": "nova",
                        "memory_kind": "agent_diary",
                        "project_id": "project_123",
                    },
                    capture_surface=AGENT_DIARY_CAPTURE_SURFACE,
                )
            ]
        if kwargs["memory_scope"] == "private":
            return [_raw_memory("private-1", score=0.8)]
        return []

    case = ContextPackEvalCase(
        name="agent-diary-opt-in",
        goal="handoff the current implementation stance",
        project="project_123",
        agent_id="nova",
        fixture=ContextPackFixture(
            name="agent-diary-opt-in",
            required_item_ids={"raw_memory:nova-diary-1", "raw_memory:private-1"},
            forbidden_item_ids={"raw_memory:other-agent-diary"},
            required_facets={ContextFacet.RECENT_MEMORY},
            required_item_metadata={
                "raw_memory:nova-diary-1": {
                    "agent_id": "nova",
                    "memory_kind": "agent_diary",
                    "project_id": "project_123",
                }
            },
            require_source_metadata=True,
        ),
    )
    pack = await compile_context(
        case.goal,
        intent=case.intent,
        layer=case.layer,
        project=case.project,
        principal_id="user-123",
        agent_id=case.agent_id,
        organization_id="org-123",
        search_fn=fake_search,
        raw_memory_recall_fn=fake_raw_recall,
    )

    result = evaluate_context_pack(pack, case.fixture)

    assert result.passed, result.failures
    assert result.metrics["metadata_requirement_coverage"] == 1.0


def test_context_pack_fixture_reports_raw_memory_scope_mismatch() -> None:
    item = ContextItem(
        id="raw_memory:private-1",
        type="raw_memory",
        name="Private raw memory",
        content="Private memory content.",
        score=0.9,
        facet=ContextFacet.RECENT_MEMORY,
        reason="raw memory matched the goal",
        source="source:private-1",
        metadata={
            "source_id": "source:private-1",
            "memory_scope": "private",
            "scope_key": None,
        },
    )
    pack = ContextPack(
        goal="evaluate scoped memory",
        intent=ContextIntent.BUILD,
        query="evaluate scoped memory",
        domain="sibyl",
        project="project_123",
        sections=[
            ContextSection(
                facet=ContextFacet.RECENT_MEMORY,
                title="Recent Memory",
                items=[item],
            )
        ],
        total_items=1,
    )

    result = evaluate_context_pack(
        pack,
        ContextPackFixture(
            name="raw-scope-grounding",
            required_item_metadata={
                "raw_memory:private-1": {
                    "memory_scope": "project",
                    "scope_key": "project_123",
                }
            },
        ),
    )

    assert not result.passed
    assert result.failures == [
        "item raw_memory:private-1 metadata memory_scope expected 'project' got 'private'",
        "item raw_memory:private-1 metadata scope_key expected 'project_123' got None",
    ]
    assert result.metrics["metadata_requirement_coverage"] == 0.0


@pytest.mark.asyncio
async def test_context_pack_fixture_passes_haven_privacy_requirements() -> None:
    async def fake_search(**kwargs: Any) -> SearchResponse:
        if kwargs["types"] == ["domain", "topic", "claim"]:
            results = [
                _result(
                    "haven-routine-evening",
                    "claim",
                    "Evening routine preference",
                    "Bliss prefers the hallway lights dimmed during evening wind-down.",
                    metadata={
                        "project_id": "project-haven",
                        "memory_space": "household",
                    },
                )
            ]
        else:
            results = []
        return SearchResponse(
            results=results,
            total=len(results),
            query=kwargs["query"],
            filters={"types": kwargs["types"]},
        )

    pack = await compile_context(
        "what should Haven remember about the evening routine?",
        intent="research",
        domain="haven",
        project="project-haven",
        organization_id="org-home",
        search_fn=fake_search,
    )

    result = evaluate_context_pack(
        pack,
        ContextPackFixture(
            name="haven-private-home-routine",
            required_item_ids={"haven-routine-evening"},
            forbidden_item_ids={"private-unrelated-medical-note"},
            forbidden_terms={"private health note"},
            required_facets={ContextFacet.DOMAIN},
            required_terms={"evening", "hallway lights"},
            max_items=4,
            require_source_metadata=True,
        ),
    )

    assert result.passed, result.failures


def test_context_pack_fixture_reports_forbidden_privacy_terms() -> None:
    item = ContextItem(
        id="unrelated-note",
        type="claim",
        name="Private unrelated note",
        content="This private health note must not appear in household recall.",
        score=0.8,
        facet=ContextFacet.DOMAIN,
        reason="claim matched the goal",
        source="seeded fixture",
        metadata={"source_id": "private-import"},
    )
    pack = ContextPack(
        goal="what should Haven remember about the evening routine?",
        intent=ContextIntent.RESEARCH,
        query="what should Haven remember about the evening routine?",
        domain="haven",
        project="project-haven",
        sections=[
            ContextSection(
                facet=ContextFacet.DOMAIN,
                title="Domain",
                items=[item],
            )
        ],
        total_items=1,
    )

    result = evaluate_context_pack(
        pack,
        ContextPackFixture(
            name="haven-private-home-routine",
            forbidden_terms={"private health note", "medical note"},
        ),
    )

    assert not result.passed
    assert result.failures == ["forbidden terms present: private health note"]
    assert result.metrics["forbidden_term_matches"] == 1


def test_context_pack_fixture_allows_forbidden_terms_in_goal_only() -> None:
    pack = ContextPack(
        goal="should private health note ever appear in Haven recall?",
        intent=ContextIntent.RESEARCH,
        query="should private health note ever appear in Haven recall?",
        domain="haven",
        project="project-haven",
        sections=[],
        total_items=0,
    )

    result = evaluate_context_pack(
        pack,
        ContextPackFixture(
            name="haven-private-home-routine",
            forbidden_terms={"private health note"},
        ),
    )

    assert result.passed, result.failures
    assert result.metrics["forbidden_term_matches"] == 0


@pytest.mark.asyncio
async def test_context_pack_fixture_reports_forbidden_haven_memory_leak() -> None:
    async def fake_search(**kwargs: Any) -> SearchResponse:
        if kwargs["types"] == ["domain", "topic", "claim"]:
            results = [
                _result(
                    "haven-routine-evening",
                    "claim",
                    "Evening routine preference",
                    "Bliss prefers the hallway lights dimmed during evening wind-down.",
                ),
                _result(
                    "private-unrelated-medical-note",
                    "claim",
                    "Private unrelated health note",
                    "This private health note must not appear in Haven household recall.",
                ),
            ]
        else:
            results = []
        return SearchResponse(
            results=results,
            total=len(results),
            query=kwargs["query"],
            filters={"types": kwargs["types"]},
        )

    pack = await compile_context(
        "what should Haven remember about the evening routine?",
        intent="research",
        domain="haven",
        project="project-haven",
        organization_id="org-home",
        search_fn=fake_search,
    )

    result = evaluate_context_pack(
        pack,
        ContextPackFixture(
            name="haven-private-home-routine",
            required_item_ids={"haven-routine-evening"},
            forbidden_item_ids={"private-unrelated-medical-note"},
        ),
    )

    assert not result.passed
    assert result.failures == ["forbidden items present: private-unrelated-medical-note"]


def test_context_pack_fixture_reports_missing_source_metadata() -> None:
    item = ContextItem(
        id="unsourced-memory",
        type="decision",
        name="Unsourced decision",
        content="This decision is missing provenance.",
        score=0.9,
        facet=ContextFacet.DECISIONS,
        reason="decision records a choice or rationale the agent should preserve",
        source=None,
        metadata={},
    )
    pack = ContextPack(
        goal="evaluate provenance",
        intent=ContextIntent.DECIDE,
        query="evaluate provenance",
        domain="sibyl",
        project="project-sibyl",
        sections=[
            ContextSection(
                facet=ContextFacet.DECISIONS,
                title="Decisions",
                items=[item],
            )
        ],
        total_items=1,
    )

    result = evaluate_context_pack(
        pack,
        ContextPackFixture(
            name="source-grounded-context",
            required_item_ids={"unsourced-memory"},
            require_source_metadata=True,
        ),
    )

    assert not result.passed
    assert result.failures == ["items missing source metadata: unsourced-memory"]
    assert result.metrics["source_metadata_coverage"] == 0.0


def test_context_pack_fixture_reports_facet_order_mismatch() -> None:
    decision = ContextItem(
        id="decision-1",
        type="decision",
        name="Decision first by mistake",
        content="This decision appears before active work.",
        score=0.9,
        facet=ContextFacet.DECISIONS,
        reason="decision records a choice or rationale the agent should preserve",
        source="seeded fixture",
        metadata={"source_id": "decision-fixture"},
    )
    task = ContextItem(
        id="task-1",
        type="task",
        name="Active task",
        content="Current task should lead a build context pack.",
        score=0.9,
        facet=ContextFacet.ACTIVE_WORK,
        reason="task can change what the agent should do next",
        source="seeded fixture",
        metadata={"source_id": "task-fixture"},
    )
    pack = ContextPack(
        goal="evaluate context order",
        intent=ContextIntent.BUILD,
        query="evaluate context order",
        domain="sibyl",
        project="project-sibyl",
        sections=[
            ContextSection(
                facet=ContextFacet.DECISIONS,
                title="Decisions",
                items=[decision],
            ),
            ContextSection(
                facet=ContextFacet.ACTIVE_WORK,
                title="Active Work",
                items=[task],
            ),
        ],
        total_items=2,
    )

    result = evaluate_context_pack(
        pack,
        ContextPackFixture(
            name="build-order",
            required_facet_order=[ContextFacet.ACTIVE_WORK, ContextFacet.DECISIONS],
        ),
    )

    assert not result.passed
    assert result.failures == [
        "facet order mismatch: expected prefix active_work, decisions got decisions, active_work"
    ]
    assert result.metrics["facet_order"] == ["decisions", "active_work"]
    assert result.metrics["facet_order_matches"] is False


def test_context_pack_fixture_reports_estimated_token_budget() -> None:
    item = ContextItem(
        id="large-memory",
        type="artifact",
        name="Large context payload",
        content="x" * 240,
        score=0.9,
        facet=ContextFacet.ARTIFACTS,
        reason="large artifact matched the goal",
        source="seeded fixture",
        metadata={"source_id": "large-fixture"},
    )
    pack = ContextPack(
        goal="evaluate token budget",
        intent=ContextIntent.BUILD,
        query="evaluate token budget",
        domain=None,
        project=None,
        sections=[
            ContextSection(
                facet=ContextFacet.ARTIFACTS,
                title="Artifacts",
                items=[item],
            )
        ],
        total_items=1,
    )

    result = evaluate_context_pack(
        pack,
        ContextPackFixture(name="token-budget", max_estimated_tokens=20),
    )

    assert not result.passed
    assert result.failures == [
        f"estimated tokens too high: {result.metrics['estimated_tokens']} > 20"
    ]
    assert result.metrics["estimated_tokens"] > 20


def test_load_context_pack_cases_parses_json_fixture(tmp_path: Path) -> None:
    path = tmp_path / "context_cases.json"
    path.write_text(
        json.dumps(
            {
                "cases": [
                    {
                        "name": "coding-handoff",
                        "goal": "handoff native memory implementation",
                        "intent": "build",
                        "layer": "wake",
                        "domain": "sibyl",
                        "project": "project-sibyl",
                        "agent_id": "nova",
                        "limit": 12,
                        "include_related": False,
                        "fixture": {
                            "required_item_ids": ["decision-source-law"],
                            "forbidden_item_ids": ["private-health-note"],
                            "required_facets": ["decisions", "artifacts"],
                            "required_facet_order": ["decisions", "artifacts"],
                            "required_layer": "wake",
                            "required_terms": ["raw memory"],
                            "forbidden_terms": ["private health note"],
                            "required_item_metadata": {
                                "decision-source-law": {
                                    "source_id": "northstar",
                                    "project_id": "project-sibyl",
                                }
                            },
                            "required_metadata_by_type": {
                                "raw_memory": {
                                    "principal_id": "user-123",
                                    "memory_scope": "private",
                                }
                            },
                            "max_items": 12,
                            "max_markdown_chars": 6000,
                            "max_estimated_tokens": 1200,
                            "max_latency_ms": 250.0,
                            "require_source_metadata": True,
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    cases = load_context_pack_cases(path)

    assert len(cases) == 1
    assert cases[0].name == "coding-handoff"
    assert cases[0].goal == "handoff native memory implementation"
    assert cases[0].layer == ContextLayer.WAKE
    assert cases[0].agent_id == "nova"
    assert cases[0].fixture.required_item_ids == {"decision-source-law"}
    assert cases[0].fixture.forbidden_item_ids == {"private-health-note"}
    assert cases[0].fixture.forbidden_terms == {"private health note"}
    assert cases[0].fixture.required_layer == ContextLayer.WAKE
    assert cases[0].fixture.required_item_metadata == {
        "decision-source-law": {
            "source_id": "northstar",
            "project_id": "project-sibyl",
        }
    }
    assert cases[0].fixture.required_metadata_by_type == {
        "raw_memory": {
            "principal_id": "user-123",
            "memory_scope": "private",
        }
    }
    assert cases[0].fixture.max_latency_ms == 250.0
    assert cases[0].fixture.max_estimated_tokens == 1200
    assert cases[0].fixture.required_facets == {
        ContextFacet.DECISIONS,
        ContextFacet.ARTIFACTS,
    }
    assert cases[0].fixture.required_facet_order == [
        ContextFacet.DECISIONS,
        ContextFacet.ARTIFACTS,
    ]
    assert cases[0].fixture.require_source_metadata is True
    assert cases[0].include_related is False


def test_context_pack_from_dict_parses_api_response() -> None:
    pack = context_pack_from_dict(
        {
            "goal": "ship context packs",
            "intent": "build",
            "layer": "deep_search",
            "query": "ship context packs sibyl",
            "domain": "sibyl",
            "project": "project-sibyl",
            "usage_hint": "use the pack",
            "total_items": 1,
            "sections": [
                {
                    "facet": "decisions",
                    "title": "Decisions",
                    "items": [
                        {
                            "id": "decision-source-law",
                            "type": "decision",
                            "name": "Raw memory is source law",
                            "content": "Keep raw source provenance.",
                            "score": 0.91,
                            "facet": "decisions",
                            "reason": "decision records a choice",
                            "source": "architecture doc",
                            "metadata": {"source_id": "northstar"},
                        }
                    ],
                }
            ],
        }
    )

    assert pack.intent == ContextIntent.BUILD
    assert pack.layer == ContextLayer.DEEP_SEARCH
    assert pack.sections[0].facet == ContextFacet.DECISIONS
    assert pack.items[0].id == "decision-source-law"
    assert pack.items[0].metadata["source_id"] == "northstar"


def test_context_pack_eval_report_exposes_pass_rate_metrics() -> None:
    result = evaluate_context_pack(
        ContextPack(
            goal="empty smoke",
            intent=ContextIntent.BUILD,
            query="empty smoke",
            domain=None,
            project=None,
            sections=[],
            total_items=0,
        ),
        ContextPackFixture(name="empty-smoke", max_items=1),
    )
    report = ContextPackEvalReport(
        cases=[],
        label="smoke",
    )
    report.cases.append(
        ContextPackCaseResult(
            case=ContextPackEvalCase(
                name="empty-smoke",
                goal="empty smoke",
                fixture=ContextPackFixture(name="empty-smoke"),
            ),
            result=result,
            latency_ms=5.0,
        )
    )

    payload = report.to_dict()

    assert payload["metrics"]["cases"] == 1
    assert payload["metrics"]["pass_rate"] == 1.0
    assert payload["metrics"]["avg_items"] == 0.0
    assert payload["metrics"]["max_items"] == 0.0
    assert payload["metrics"]["avg_markdown_chars"] > 0
    assert payload["metrics"]["max_markdown_chars"] == payload["metrics"]["avg_markdown_chars"]
    assert payload["metrics"]["avg_estimated_tokens"] > 0
    assert payload["metrics"]["max_estimated_tokens"] == payload["metrics"]["avg_estimated_tokens"]
    assert payload["metrics"]["source_metadata_coverage"] == 1.0
    assert payload["metrics"]["facet_order_match_rate"] == 1.0
    assert payload["metrics"]["forbidden_term_matches"] == 0
    assert payload["per_case"][0]["passed"] is True
    assert payload["per_case"][0]["metrics"]["estimated_tokens"] > 0
    assert payload["per_case"][0]["intent"] == "build"
    assert payload["per_case"][0]["layer"] == "recall"
    assert payload["per_case"][0]["agent_id"] is None
