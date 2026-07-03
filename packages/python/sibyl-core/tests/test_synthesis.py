from __future__ import annotations

from importlib import import_module
from types import SimpleNamespace
from typing import Any, Literal
from unittest.mock import AsyncMock, patch

import pytest

from sibyl_core.models.context import (
    ContextFacet,
    ContextIntent,
    ContextItem,
    ContextItemQualityMetadata,
    ContextLayer,
    ContextPack,
    ContextSection,
)
from sibyl_core.models.synthesis import (
    SynthesisArtifactFormat,
    SynthesisOutputType,
    SynthesisRequest,
    SynthesisSectionRequest,
    SynthesisSourceReference,
    SynthesisVerificationStatus,
)
from sibyl_core.services.synthesis import (
    default_context_pack,
    default_search,
    draft_synthesis_artifact,
    materialize_synthesis_section_packs,
    plan_synthesis,
    remember_synthesis_artifact,
)
from sibyl_core.tools.responses import SearchResponse, SearchResult


def _result(
    entity_id: str,
    entity_type: str,
    name: str,
    *,
    content: str | None = None,
    score: float = 0.8,
    origin: Literal["graph", "document"] = "graph",
    metadata: dict[str, Any] | None = None,
) -> SearchResult:
    return SearchResult(
        id=entity_id,
        type=entity_type,
        name=name,
        content=content or f"{name} content",
        score=score,
        source=f"source:{entity_id}",
        result_origin=origin,
        metadata={"entity_type": entity_type, **(metadata or {})},
    )


async def _empty_related(**kwargs: Any) -> list[SynthesisSourceReference]:
    return []


@pytest.mark.asyncio
async def test_default_synthesis_search_opts_out_of_exposure_stamping() -> None:
    search = AsyncMock(
        return_value=SearchResponse(
            results=[],
            total=0,
            query="synthesis roadmap",
            filters={},
        )
    )
    search_module = import_module("sibyl_core.tools.search")

    with patch.object(search_module, "search", search):
        await default_search(query="synthesis roadmap", organization_id="org-123")

    search.assert_awaited_once()
    assert search.await_args.kwargs["record_exposure"] is False


@pytest.mark.asyncio
async def test_default_synthesis_context_pack_opts_out_of_exposure_stamping() -> None:
    compile_context = AsyncMock(
        return_value=ContextPack(
            goal="synthesis roadmap",
            query="synthesis roadmap",
            intent=ContextIntent.BUILD,
            layer=ContextLayer.RECALL,
            domain=None,
            project=None,
            sections=[],
            total_items=0,
        )
    )
    context_module = import_module("sibyl_core.tools.context")

    with patch.object(context_module, "compile_context", compile_context):
        await default_context_pack(goal="synthesis roadmap", organization_id="org-123")

    compile_context.assert_awaited_once()
    assert compile_context.await_args.kwargs["record_exposure"] is False


@pytest.mark.asyncio
async def test_plan_synthesis_builds_deterministic_source_aware_outline() -> None:
    calls: list[dict[str, Any]] = []
    responses = {
        ("decision",): [
            _result(
                "decision:source-law",
                "decision",
                "Require source citations",
                content="Every roadmap claim needs source IDs and visible provenance.",
                score=0.95,
            )
        ],
        ("task", "epic", "plan"): [
            _result(
                "task:d1",
                "task",
                "Implement synthesis planner",
                content="Next milestone is deterministic synthesis planning before drafting.",
                score=0.9,
            )
        ],
        ("artifact", "document", "source", "config_file"): [
            _result(
                "artifact:post-v08-plan",
                "artifact",
                "Post v0.8 synthesis plan",
                content="The roadmap moves from ingest into synthesis and workspace work.",
                score=0.85,
            )
        ],
    }

    async def fake_search(**kwargs: Any) -> SearchResponse:
        calls.append(kwargs)
        results = responses.get(tuple(kwargs["types"]), [])
        return SearchResponse(
            results=results,
            total=len(results),
            query=kwargs["query"],
            filters={"types": kwargs["types"]},
        )

    request = SynthesisRequest(
        goal="Write the v0.9 roadmap",
        output_type=SynthesisOutputType.ROADMAP,
        seed_query="v0.9 synthesis roadmap",
        project="project-sibyl",
        domain="memory",
    )

    first = await plan_synthesis(
        request,
        organization_id="org-123",
        accessible_projects={"project-sibyl"},
        search_fn=fake_search,
        related_fn=_empty_related,
    )
    second = await plan_synthesis(
        request,
        organization_id="org-123",
        accessible_projects={"project-sibyl"},
        search_fn=fake_search,
        related_fn=_empty_related,
    )

    assert first.run_id == second.run_id
    assert [section.title for section in first.outline.sections] == [
        "Current State",
        "Completed Work",
        "Next Milestones",
        "Risks And Open Questions",
    ]
    assert first.verification.status == SynthesisVerificationStatus.PENDING
    assert first.verification.gap_count == 0
    assert first.source_packs[2].source_ids[0] == "task:d1"
    assert {call["organization_id"] for call in calls} == {"org-123"}
    assert {call["project"] for call in calls} == {"project-sibyl"}
    assert {frozenset(call["accessible_projects"]) for call in calls} == {
        frozenset({"project-sibyl"})
    }


@pytest.mark.asyncio
async def test_plan_synthesis_returns_gap_for_unsupported_required_section() -> None:
    async def fake_search(**kwargs: Any) -> SearchResponse:
        return SearchResponse(
            results=[
                _result(
                    "decision:database",
                    "decision",
                    "Use SurrealDB",
                    content="The database migration is complete.",
                )
            ],
            total=1,
            query=kwargs["query"],
            filters={"types": kwargs["types"]},
        )

    run = await plan_synthesis(
        SynthesisRequest(
            goal="Plan mobile launch",
            required_sections=[
                SynthesisSectionRequest(
                    title="Mobile Launch",
                    prompt="Describe the supported iOS and Android release plan.",
                )
            ],
        ),
        organization_id="org-123",
        search_fn=fake_search,
        related_fn=_empty_related,
    )

    assert run.verification.status == SynthesisVerificationStatus.GAPS
    assert run.verification.gaps[0].reason == "no_source_supports_requested_section"
    assert run.outline.sections[0].source_ids == []
    assert run.source_packs[0].sources == []


@pytest.mark.asyncio
async def test_plan_synthesis_reports_missing_required_sources_once() -> None:
    async def fake_search(**kwargs: Any) -> SearchResponse:
        return SearchResponse(results=[], total=0, query=kwargs["query"], filters={})

    run = await plan_synthesis(
        SynthesisRequest(
            goal="Write release notes",
            required_sections=[
                SynthesisSectionRequest(
                    title="Verification",
                    required_source_ids=["artifact:missing"],
                )
            ],
        ),
        organization_id="org-123",
        search_fn=fake_search,
        related_fn=_empty_related,
    )

    assert run.verification.gap_count == 1
    assert run.verification.gaps[0].reason == "required_source_ids_not_found"
    assert run.verification.gaps[0].missing_source_ids == ["artifact:missing"]


@pytest.mark.asyncio
async def test_plan_synthesis_uses_entity_ids_and_graph_neighborhoods() -> None:
    related_calls: list[str] = []

    async def fake_search(**kwargs: Any) -> SearchResponse:
        return SearchResponse(results=[], total=0, query=kwargs["query"], filters={})

    async def fake_related(**kwargs: Any) -> list[SynthesisSourceReference]:
        related_calls.append(kwargs["entity_id"])
        return [
            SynthesisSourceReference(
                id="task:neighbor",
                type="task",
                name="Neighbor task",
                content_preview="Follow-up task related to the seed decision.",
                score=0.5,
                origin="neighborhood",
                relation="RELATED_TO",
            )
        ]

    run = await plan_synthesis(
        SynthesisRequest(
            goal="Summarize the synthesis path",
            output_type=SynthesisOutputType.BRIEFING,
            entity_ids=["decision:root"],
        ),
        organization_id="org-123",
        search_fn=fake_search,
        related_fn=fake_related,
    )

    all_source_ids = {source_id for pack in run.source_packs for source_id in pack.source_ids}
    assert related_calls == ["decision:root"]
    assert "decision:root" in all_source_ids
    assert "task:neighbor" in all_source_ids


@pytest.mark.asyncio
async def test_plan_synthesis_caps_total_neighborhood_expansion_ids() -> None:
    related_calls: list[str] = []

    async def fake_search(**kwargs: Any) -> SearchResponse:
        return SearchResponse(results=[], total=0, query=kwargs["query"], filters={})

    async def fake_related(**kwargs: Any) -> list[SynthesisSourceReference]:
        related_calls.append(kwargs["entity_id"])
        return []

    run = await plan_synthesis(
        SynthesisRequest(
            goal="Bound neighborhood lookups",
            entity_ids=[f"entity:{idx}" for idx in range(100)],
            decision_ids=[f"decision:{idx}" for idx in range(100)],
            task_ids=[f"task:{idx}" for idx in range(100)],
            artifact_ids=[f"artifact:{idx}" for idx in range(100)],
        ),
        organization_id="org-123",
        search_fn=fake_search,
        related_fn=fake_related,
    )

    assert len(related_calls) == 100
    assert related_calls[0] == "entity:0"
    assert related_calls[-1] == "entity:99"
    assert run.verification.source_count >= 100


@pytest.mark.asyncio
async def test_materialize_synthesis_section_packs_filters_unauthorized_text() -> None:
    async def fake_search(**kwargs: Any) -> SearchResponse:
        return SearchResponse(results=[], total=0, query=kwargs["query"], filters={})

    run = await plan_synthesis(
        SynthesisRequest(
            goal="Write source-safe report",
            required_sections=[SynthesisSectionRequest(title="Evidence")],
        ),
        organization_id="org-123",
        search_fn=fake_search,
        related_fn=_empty_related,
    )

    context_calls: list[dict[str, Any]] = []

    async def fake_context(**kwargs: Any) -> ContextPack:
        context_calls.append(kwargs)
        return ContextPack(
            goal=kwargs["goal"],
            intent=ContextIntent.RESEARCH,
            query=kwargs["goal"],
            domain=None,
            project="proj-allowed",
            sections=[
                ContextSection(
                    facet=ContextFacet.ARTIFACTS,
                    title="Artifacts",
                    items=[
                        ContextItem(
                            id="artifact:allowed",
                            type="artifact",
                            name="Allowed artifact",
                            content="Allowed source text.",
                            score=0.95,
                            facet=ContextFacet.ARTIFACTS,
                            reason="allowed source",
                            source="source:allowed",
                            quality=ContextItemQualityMetadata(
                                project_id="proj-allowed",
                                updated_at="2026-05-14T12:00:00Z",
                            ),
                            metadata={"source_id": "source:allowed"},
                        ),
                        ContextItem(
                            id="artifact:hidden",
                            type="artifact",
                            name="Hidden artifact",
                            content="Unauthorized source text must not leak.",
                            score=0.91,
                            facet=ContextFacet.ARTIFACTS,
                            reason="hidden source",
                            source="source:hidden",
                            quality=ContextItemQualityMetadata(project_id="proj-secret"),
                            metadata={"source_id": "source:hidden"},
                        ),
                        ContextItem(
                            id="artifact:scope-hidden",
                            type="artifact",
                            name="Scope hidden artifact",
                            content="Scope-key-only project text must not leak.",
                            score=0.89,
                            facet=ContextFacet.ARTIFACTS,
                            reason="scope hidden source",
                            source="source:scope-hidden",
                            metadata={
                                "source_id": "source:scope-hidden",
                                "memory_scope": "project",
                                "scope_key": "proj-secret",
                            },
                        ),
                    ],
                )
            ],
            total_items=3,
        )

    materialized = await materialize_synthesis_section_packs(
        run,
        organization_id="org-123",
        principal_id="user-123",
        accessible_projects={"proj-allowed"},
        context_fn=fake_context,
    )

    pack = materialized.source_packs[0]
    assert context_calls[0]["accessible_projects"] == {"proj-allowed"}
    assert context_calls[0]["principal_id"] == "user-123"
    assert pack.source_ids == ["source:allowed"]
    assert materialized.outline.sections[0].source_ids == ["source:allowed"]
    assert materialized.verification.source_count == 1
    assert pack.hidden_count == 2
    assert pack.sources[0].content_preview == "Allowed source text."
    assert "Unauthorized source text" not in repr(pack)
    assert "Scope-key-only project text" not in repr(pack)
    assert pack.freshness == {"source:allowed": "2026-05-14T12:00:00Z"}


@pytest.mark.asyncio
async def test_materialize_synthesis_section_packs_redacts_visible_text() -> None:
    async def fake_search(**kwargs: Any) -> SearchResponse:
        return SearchResponse(results=[], total=0, query=kwargs["query"], filters={})

    run = await plan_synthesis(
        SynthesisRequest(
            goal="Write redaction report",
            required_sections=[SynthesisSectionRequest(title="Evidence")],
        ),
        organization_id="org-123",
        search_fn=fake_search,
        related_fn=_empty_related,
    )

    async def fake_context(**kwargs: Any) -> ContextPack:
        return ContextPack(
            goal=kwargs["goal"],
            intent=ContextIntent.RESEARCH,
            query=kwargs["goal"],
            domain=None,
            project=None,
            sections=[
                ContextSection(
                    facet=ContextFacet.RECENT_MEMORY,
                    title="Recent Memory",
                    items=[
                        ContextItem(
                            id="raw_memory:1",
                            type="raw_memory",
                            name="Sensitive source",
                            content="Sensitive text should be blank.",
                            score=0.8,
                            facet=ContextFacet.RECENT_MEMORY,
                            reason="redacted source",
                            source="source:redacted",
                            metadata={
                                "source_id": "source:redacted",
                                "lifecycle_state": "redacted",
                                "unresolved_claims": ["needs citation"],
                            },
                        )
                    ],
                )
            ],
            total_items=1,
        )

    materialized = await materialize_synthesis_section_packs(
        run,
        organization_id="org-123",
        principal_id="user-123",
        context_fn=fake_context,
    )

    pack = materialized.source_packs[0]
    assert pack.redaction_count == 1
    assert pack.sources[0].content_preview == ""
    assert pack.unresolved_claims == ["needs citation"]
    assert pack.sources[0].metadata == {
        "facet": ContextFacet.RECENT_MEMORY.value,
        "freshness": None,
        "project_id": None,
        "reason": "redacted source",
    }


@pytest.mark.asyncio
async def test_materialize_synthesis_section_packs_hides_private_sources_by_principal() -> None:
    async def fake_search(**kwargs: Any) -> SearchResponse:
        return SearchResponse(results=[], total=0, query=kwargs["query"], filters={})

    run = await plan_synthesis(
        SynthesisRequest(
            goal="Write private report",
            required_sections=[SynthesisSectionRequest(title="Private Evidence")],
        ),
        organization_id="org-123",
        search_fn=fake_search,
        related_fn=_empty_related,
    )

    async def fake_context(**kwargs: Any) -> ContextPack:
        return ContextPack(
            goal=kwargs["goal"],
            intent=ContextIntent.RESEARCH,
            query=kwargs["goal"],
            domain=None,
            project=None,
            sections=[
                ContextSection(
                    facet=ContextFacet.RECENT_MEMORY,
                    title="Recent Memory",
                    items=[
                        ContextItem(
                            id="raw_memory:private",
                            type="raw_memory",
                            name="Other user's source",
                            content="Other user's private text must not leak.",
                            score=0.8,
                            facet=ContextFacet.RECENT_MEMORY,
                            reason="private source",
                            source="source:private",
                            metadata={
                                "source_id": "source:private",
                                "memory_scope": "private",
                                "principal_id": "user-other",
                            },
                        ),
                        ContextItem(
                            id="raw_memory:ownerless",
                            type="raw_memory",
                            name="Ownerless private source",
                            content="Ownerless private text must not leak.",
                            score=0.78,
                            facet=ContextFacet.RECENT_MEMORY,
                            reason="ownerless private source",
                            source="source:ownerless",
                            metadata={
                                "source_id": "source:ownerless",
                                "memory_scope": "private",
                            },
                        ),
                    ],
                )
            ],
            total_items=2,
        )

    materialized = await materialize_synthesis_section_packs(
        run,
        organization_id="org-123",
        principal_id="user-123",
        context_fn=fake_context,
    )

    pack = materialized.source_packs[0]
    assert pack.source_ids == []
    assert pack.hidden_count == 2
    assert "Other user's private text" not in repr(pack)
    assert "Ownerless private text" not in repr(pack)
    assert materialized.verification.status is SynthesisVerificationStatus.GAPS
    assert materialized.verification.gaps[-1].reason == "no_materialized_sources"


@pytest.mark.asyncio
async def test_materialize_synthesis_section_packs_omits_corrected_sources() -> None:
    async def fake_search(**kwargs: Any) -> SearchResponse:
        return SearchResponse(results=[], total=0, query=kwargs["query"], filters={})

    run = await plan_synthesis(
        SynthesisRequest(
            goal="Write corrected-source report",
            required_sections=[SynthesisSectionRequest(title="Evidence")],
        ),
        organization_id="org-123",
        search_fn=fake_search,
        related_fn=_empty_related,
    )

    async def fake_context(**kwargs: Any) -> ContextPack:
        return ContextPack(
            goal=kwargs["goal"],
            intent=ContextIntent.RESEARCH,
            query=kwargs["goal"],
            domain=None,
            project=None,
            sections=[
                ContextSection(
                    facet=ContextFacet.RECENT_MEMORY,
                    title="Recent Memory",
                    items=[
                        ContextItem(
                            id="raw_memory:current",
                            type="raw_memory",
                            name="Current source",
                            content="Current source text.",
                            score=0.9,
                            facet=ContextFacet.RECENT_MEMORY,
                            reason="current source",
                            source="source:current",
                            quality=ContextItemQualityMetadata(updated_at="2026-05-16T12:00:00Z"),
                            metadata={"source_id": "source:current"},
                        ),
                        ContextItem(
                            id="raw_memory:stale",
                            type="raw_memory",
                            name="Stale source",
                            content="Stale correction text must not leak.",
                            score=0.89,
                            facet=ContextFacet.RECENT_MEMORY,
                            reason="stale source",
                            source="source:stale",
                            metadata={
                                "source_id": "source:stale",
                                "lifecycle_state": "stale",
                            },
                        ),
                        ContextItem(
                            id="raw_memory:superseded",
                            type="raw_memory",
                            name="Superseded source",
                            content="Superseded correction text must not leak.",
                            score=0.88,
                            facet=ContextFacet.RECENT_MEMORY,
                            reason="superseded source",
                            source="source:superseded",
                            metadata={
                                "source_id": "source:superseded",
                                "superseded_by_source_id": "source:current",
                            },
                        ),
                        ContextItem(
                            id="raw_memory:duplicate",
                            type="raw_memory",
                            name="Duplicate source",
                            content="Duplicate correction text must not leak.",
                            score=0.87,
                            facet=ContextFacet.RECENT_MEMORY,
                            reason="duplicate source",
                            source="source:duplicate",
                            metadata={
                                "source_id": "source:duplicate",
                                "duplicate_of_source_id": "source:current",
                            },
                        ),
                    ],
                )
            ],
            total_items=4,
        )

    materialized = await materialize_synthesis_section_packs(
        run,
        organization_id="org-123",
        principal_id="user-123",
        context_fn=fake_context,
    )
    artifact = draft_synthesis_artifact(materialized)
    pack = materialized.source_packs[0]

    assert pack.source_ids == ["source:current"]
    assert pack.correction_count == 3
    assert pack.correction_reasons == {
        "duplicate_of_source_id": 1,
        "lifecycle_stale": 1,
        "superseded_by_source_id": 1,
    }
    assert artifact.verification.status is SynthesisVerificationStatus.PASS
    assert "Current source text. [source:current]" in artifact.markdown
    assert "Corrected sources omitted: 3" in artifact.markdown
    assert "Stale correction text" not in artifact.markdown
    assert "Superseded correction text" not in artifact.markdown
    assert "Duplicate correction text" not in artifact.markdown
    assert artifact.json_payload["sections"][0]["correction_count"] == 3


@pytest.mark.asyncio
async def test_draft_synthesis_artifact_renders_citable_markdown_and_json() -> None:
    async def fake_search(**kwargs: Any) -> SearchResponse:
        return SearchResponse(results=[], total=0, query=kwargs["query"], filters={})

    run = await plan_synthesis(
        SynthesisRequest(
            goal="Write citable report",
            required_sections=[SynthesisSectionRequest(title="Evidence")],
        ),
        organization_id="org-123",
        search_fn=fake_search,
        related_fn=_empty_related,
    )

    async def fake_context(**kwargs: Any) -> ContextPack:
        return ContextPack(
            goal=kwargs["goal"],
            intent=ContextIntent.RESEARCH,
            query=kwargs["goal"],
            domain=None,
            project="proj-allowed",
            sections=[
                ContextSection(
                    facet=ContextFacet.ARTIFACTS,
                    title="Artifacts",
                    items=[
                        ContextItem(
                            id="artifact:allowed",
                            type="artifact",
                            name="Allowed artifact",
                            content="Allowed artifact text.",
                            score=0.95,
                            facet=ContextFacet.ARTIFACTS,
                            reason="allowed source",
                            source="source:allowed",
                            quality=ContextItemQualityMetadata(
                                project_id="proj-allowed",
                                updated_at="2026-05-14T12:00:00Z",
                            ),
                            metadata={"source_id": "source:allowed"},
                        ),
                        ContextItem(
                            id="artifact:hidden",
                            type="artifact",
                            name="Hidden artifact",
                            content="Hidden text must never draft.",
                            score=0.9,
                            facet=ContextFacet.ARTIFACTS,
                            reason="hidden source",
                            source="source:hidden",
                            quality=ContextItemQualityMetadata(project_id="proj-secret"),
                            metadata={"source_id": "source:hidden"},
                        ),
                    ],
                )
            ],
            total_items=2,
        )

    materialized = await materialize_synthesis_section_packs(
        run,
        organization_id="org-123",
        principal_id="user-123",
        accessible_projects={"proj-allowed"},
        context_fn=fake_context,
    )

    artifact = draft_synthesis_artifact(materialized)

    assert artifact.verification.status is SynthesisVerificationStatus.PASS
    assert artifact.source_ids == ["source:allowed"]
    assert "Allowed artifact text. [source:allowed]" in artifact.markdown
    assert "Hidden text must never draft" not in artifact.markdown
    assert "Hidden context omitted: 1" in artifact.markdown
    assert artifact.json_payload["sections"][0]["source_ids"] == ["source:allowed"]
    assert artifact.json_payload["sections"][0]["hidden_count"] == 1


@pytest.mark.asyncio
async def test_draft_synthesis_artifact_reports_unresolved_and_freshness_gaps() -> None:
    async def fake_search(**kwargs: Any) -> SearchResponse:
        return SearchResponse(results=[], total=0, query=kwargs["query"], filters={})

    run = await plan_synthesis(
        SynthesisRequest(
            goal="Write gap report",
            required_sections=[SynthesisSectionRequest(title="Claims")],
        ),
        organization_id="org-123",
        search_fn=fake_search,
        related_fn=_empty_related,
    )

    async def fake_context(**kwargs: Any) -> ContextPack:
        return ContextPack(
            goal=kwargs["goal"],
            intent=ContextIntent.RESEARCH,
            query=kwargs["goal"],
            domain=None,
            project=None,
            sections=[
                ContextSection(
                    facet=ContextFacet.RECENT_MEMORY,
                    title="Memory",
                    items=[
                        ContextItem(
                            id="raw_memory:claim",
                            type="raw_memory",
                            name="Unsupported claim",
                            content="This claim needs follow-up.",
                            score=0.7,
                            facet=ContextFacet.RECENT_MEMORY,
                            reason="claim source",
                            source="source:claim",
                            metadata={
                                "source_id": "source:claim",
                                "unresolved_claims": ["needs external confirmation"],
                            },
                        )
                    ],
                )
            ],
            total_items=1,
        )

    materialized = await materialize_synthesis_section_packs(
        run,
        organization_id="org-123",
        principal_id="user-123",
        context_fn=fake_context,
    )

    artifact = draft_synthesis_artifact(materialized)

    assert artifact.verification.status is SynthesisVerificationStatus.GAPS
    assert {gap.reason for gap in artifact.verification.gaps} == {
        "missing_freshness_metadata",
        "unresolved_claim",
    }
    assert "Verification Gaps" in artifact.markdown
    assert artifact.json_payload["verification"]["gap_count"] == 2


@pytest.mark.asyncio
async def test_remember_synthesis_artifact_persists_source_link_metadata() -> None:
    async def fake_search(**kwargs: Any) -> SearchResponse:
        return SearchResponse(results=[], total=0, query=kwargs["query"], filters={})

    run = await plan_synthesis(
        SynthesisRequest(
            goal="Remember generated report",
            output_type=SynthesisOutputType.REPORT,
            required_sections=[SynthesisSectionRequest(title="Evidence")],
        ),
        organization_id="org-123",
        search_fn=fake_search,
        related_fn=_empty_related,
    )

    async def fake_context(**kwargs: Any) -> ContextPack:
        return ContextPack(
            goal=kwargs["goal"],
            intent=ContextIntent.RESEARCH,
            query=kwargs["goal"],
            domain=None,
            project=None,
            sections=[
                ContextSection(
                    facet=ContextFacet.ARTIFACTS,
                    title="Artifacts",
                    items=[
                        ContextItem(
                            id="artifact:allowed",
                            type="artifact",
                            name="Allowed artifact",
                            content="Remembered artifact text.",
                            score=0.95,
                            facet=ContextFacet.ARTIFACTS,
                            reason="allowed source",
                            source="source:allowed",
                            quality=ContextItemQualityMetadata(updated_at="2026-05-14T12:00:00Z"),
                            metadata={"source_id": "source:allowed"},
                        )
                    ],
                )
            ],
            total_items=1,
        )

    materialized = await materialize_synthesis_section_packs(
        run,
        organization_id="org-123",
        principal_id="user-123",
        context_fn=fake_context,
    )
    artifact = draft_synthesis_artifact(
        materialized,
        output_format=SynthesisArtifactFormat.JSON,
    )
    calls: list[dict[str, Any]] = []

    async def fake_remember(**kwargs: Any) -> SimpleNamespace:
        calls.append(kwargs)
        return SimpleNamespace(id="memory:artifact", source_id=kwargs["source_id"])

    remembered = await remember_synthesis_artifact(
        artifact,
        materialized,
        organization_id="org-123",
        principal_id="user-123",
        memory_scope="project",
        scope_key="project-sibyl",
        tags=["roadmap"],
        remember_fn=fake_remember,
    )

    payload = calls[0]
    assert remembered.remembered_memory_id == "memory:artifact"
    assert remembered.remembered_source_id == artifact.artifact_id + ":generated"
    assert payload["entity_type"] == "artifact"
    assert payload["capture_surface"] == "synthesis_artifact"
    assert payload["memory_scope"] == "project"
    assert payload["scope_key"] == "project-sibyl"
    assert payload["metadata"]["source_ids"] == ["source:allowed"]
    assert payload["metadata"]["generated_text_hash"] == artifact.generated_text_hash
    assert payload["provenance"]["section_source_ids"] == artifact.section_source_ids
    assert payload["tags"] == ["synthesis", "report", "roadmap"]
    assert '"source:allowed"' in payload["raw_content"]
