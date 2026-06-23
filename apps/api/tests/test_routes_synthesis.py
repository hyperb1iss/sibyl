from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest

from sibyl.api.app import create_api_app
from sibyl.api.routes.synthesis import draft_synthesis_route, plan_synthesis_route
from sibyl.api.schemas import (
    SynthesisDraftRequest,
    SynthesisPlanRequest,
    SynthesisSectionPlanRequest,
)
from sibyl_core.auth import OrganizationRole
from sibyl_core.models.context import (
    ContextFacet,
    ContextIntent,
    ContextItem,
    ContextItemQualityMetadata,
    ContextPack,
    ContextSection,
)
from sibyl_core.models.synthesis import (
    SynthesisArtifactFormat,
    SynthesisOutputType,
    SynthesisRunStatus,
)
from sibyl_core.tools.responses import SearchResponse, SearchResult


def _org() -> SimpleNamespace:
    return SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))


def _ctx() -> SimpleNamespace:
    return SimpleNamespace(user_id="user-123", org_role=OrganizationRole.MEMBER)


def test_synthesis_plan_route_is_registered() -> None:
    paths = set(create_api_app().openapi()["paths"])

    assert "/synthesis/plan" in paths
    assert "/synthesis/draft" in paths


async def _empty_related(**kwargs: Any) -> list[Any]:
    return []


async def _fake_context_pack(**kwargs: Any) -> ContextPack:
    return ContextPack(
        goal=kwargs["goal"],
        intent=ContextIntent.RESEARCH,
        query=kwargs["goal"],
        domain=kwargs.get("domain"),
        project=kwargs.get("project"),
        sections=[
            ContextSection(
                facet=ContextFacet.ARTIFACTS,
                title="Artifacts",
                items=[
                    ContextItem(
                        id="artifact:context",
                        type="artifact",
                        name="Context artifact",
                        content="Only authorized source text enters the materialized pack.",
                        score=0.9,
                        facet=ContextFacet.ARTIFACTS,
                        reason="artifact supports synthesis",
                        source="source:context",
                        quality=ContextItemQualityMetadata(
                            project_id=kwargs.get("project"),
                            updated_at="2026-05-14T12:00:00Z",
                        ),
                        metadata={"source_id": "source:context"},
                    )
                ],
            )
        ],
        total_items=1,
    )


async def _fake_search(**kwargs: Any) -> SearchResponse:
    results = {
        ("decision",): [
            SearchResult(
                id="decision:citations",
                type="decision",
                name="Require citations",
                content="Every generated section needs source IDs.",
                score=0.9,
            )
        ],
        ("task", "epic", "plan"): [
            SearchResult(
                id="task:synthesis",
                type="task",
                name="Implement synthesis",
                content="Plan synthesis before drafting.",
                score=0.85,
            )
        ],
        ("artifact", "document", "source", "config_file"): [],
    }.get(tuple(kwargs["types"]), [])
    return SearchResponse(
        results=results,
        total=len(results),
        query=kwargs["query"],
        filters={"types": kwargs["types"]},
    )


@pytest.mark.asyncio
async def test_plan_synthesis_route_scopes_to_accessible_projects() -> None:
    with (
        patch(
            "sibyl.api.routes.synthesis.list_accessible_project_graph_ids",
            AsyncMock(return_value=["project-sibyl"]),
        ) as list_projects,
        patch(
            "sibyl_core.services.synthesis.default_search",
            _fake_search,
        ),
        patch(
            "sibyl_core.services.synthesis.default_related_sources",
            _empty_related,
        ),
        patch(
            "sibyl_core.services.synthesis.default_context_pack",
            _fake_context_pack,
        ),
    ):
        response = await plan_synthesis_route(
            SynthesisPlanRequest(
                goal="Write synthesis roadmap",
                output_type=SynthesisOutputType.ROADMAP,
                seed_query="synthesis roadmap",
            ),
            org=_org(),
            ctx=_ctx(),
        )

    list_projects.assert_awaited_once()
    assert response.status == SynthesisRunStatus.PLANNED
    assert response.outline.sections[0].title == "Current State"
    assert response.verification.gap_count == 0
    assert response.source_packs[0].source_ids == ["source:context"]
    assert (
        response.source_packs[0].sources[0].content_preview
        == "Only authorized source text enters the materialized pack."
    )


@pytest.mark.asyncio
async def test_plan_synthesis_route_verifies_explicit_project() -> None:
    with (
        patch(
            "sibyl.api.routes.synthesis.verify_entity_project_access",
            AsyncMock(),
        ) as verify_project,
        patch(
            "sibyl_core.services.synthesis.default_search",
            _fake_search,
        ),
        patch(
            "sibyl_core.services.synthesis.default_related_sources",
            _empty_related,
        ),
        patch(
            "sibyl_core.services.synthesis.default_context_pack",
            _fake_context_pack,
        ),
    ):
        response = await plan_synthesis_route(
            SynthesisPlanRequest(
                goal="Write synthesis roadmap",
                project="project-sibyl",
                output_type=SynthesisOutputType.ROADMAP,
            ),
            org=_org(),
            ctx=_ctx(),
        )

    verify_project.assert_awaited_once()
    assert response.request.project == "project-sibyl"
    assert response.source_packs[0].freshness == {"source:context": "2026-05-14T12:00:00Z"}


@pytest.mark.asyncio
async def test_plan_synthesis_route_returns_required_section_gaps() -> None:
    async def fake_search(**kwargs: Any) -> SearchResponse:
        return SearchResponse(results=[], total=0, query=kwargs["query"], filters={})

    with (
        patch(
            "sibyl.api.routes.synthesis.list_accessible_project_graph_ids",
            AsyncMock(return_value=[]),
        ),
        patch(
            "sibyl_core.services.synthesis.default_search",
            fake_search,
        ),
        patch(
            "sibyl_core.services.synthesis.default_related_sources",
            _empty_related,
        ),
        patch(
            "sibyl_core.services.synthesis.default_context_pack",
            _fake_context_pack,
        ),
    ):
        response = await plan_synthesis_route(
            SynthesisPlanRequest(
                goal="Plan unsupported launch",
                required_sections=[
                    SynthesisSectionPlanRequest(title="Mobile Launch"),
                ],
            ),
            org=_org(),
            ctx=_ctx(),
        )

    assert response.verification.status.value == "gaps"
    assert response.verification.gaps[0].reason == "no_source_supports_requested_section"


@pytest.mark.asyncio
async def test_draft_synthesis_route_returns_verified_artifact() -> None:
    with (
        patch(
            "sibyl.api.routes.synthesis.list_accessible_project_graph_ids",
            AsyncMock(return_value=["project-sibyl"]),
        ),
        patch(
            "sibyl_core.services.synthesis.default_search",
            _fake_search,
        ),
        patch(
            "sibyl_core.services.synthesis.default_related_sources",
            _empty_related,
        ),
        patch(
            "sibyl_core.services.synthesis.default_context_pack",
            _fake_context_pack,
        ),
    ):
        response = await draft_synthesis_route(
            SynthesisDraftRequest(
                goal="Write synthesis roadmap",
                output_type=SynthesisOutputType.ROADMAP,
                seed_query="synthesis roadmap",
            ),
            org=_org(),
            ctx=_ctx(),
        )

    assert response.status == SynthesisRunStatus.VERIFIED
    assert response.artifact.format is SynthesisArtifactFormat.MARKDOWN
    assert response.artifact.verification.status.value == "pass"
    assert "Only authorized source text" in response.artifact.markdown
    assert "[source:context]" in response.artifact.markdown
    assert response.artifact.json_payload["sections"][0]["source_ids"] == ["source:context"]


@pytest.mark.asyncio
async def test_draft_synthesis_route_can_remember_artifact() -> None:
    remember_calls: list[dict[str, Any]] = []

    async def fake_remember(**kwargs: Any) -> SimpleNamespace:
        remember_calls.append(kwargs)
        return SimpleNamespace(id="memory:artifact", source_id=kwargs["source_id"])

    with (
        patch(
            "sibyl.api.routes.synthesis.list_accessible_project_graph_ids",
            AsyncMock(return_value=["project-sibyl"]),
        ),
        patch(
            "sibyl_core.services.synthesis.default_search",
            _fake_search,
        ),
        patch(
            "sibyl_core.services.synthesis.default_related_sources",
            _empty_related,
        ),
        patch(
            "sibyl_core.services.synthesis.default_context_pack",
            _fake_context_pack,
        ),
        patch(
            "sibyl_core.services.synthesis.default_remember_artifact",
            fake_remember,
        ),
    ):
        response = await draft_synthesis_route(
            SynthesisDraftRequest(
                goal="Write synthesis roadmap",
                output_type=SynthesisOutputType.ROADMAP,
                output_format=SynthesisArtifactFormat.JSON,
                remember=True,
                tags=["roadmap"],
            ),
            org=_org(),
            ctx=_ctx(),
        )

    assert response.artifact.remembered_memory_id == "memory:artifact"
    assert response.artifact.remembered_source_id == remember_calls[0]["source_id"]
    assert remember_calls[0]["memory_scope"] == "private"
    assert remember_calls[0]["metadata"]["source_ids"] == ["source:context"]
    assert '"source:context"' in remember_calls[0]["raw_content"]
