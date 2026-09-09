from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest

from sibyl.api.app import create_api_app
from sibyl.api.routes.synthesis import (
    draft_synthesis_route,
    handbook_synthesis_route,
    plan_synthesis_route,
)
from sibyl.api.schemas import (
    SynthesisDraftRequest,
    SynthesisPlanRequest,
    SynthesisSectionPlanRequest,
)
from sibyl.auth.context import AuthContext
from sibyl_core.auth import OrganizationRole
from sibyl_core.models.context import (
    ContextFacet,
    ContextIntent,
    ContextItem,
    ContextItemQualityMetadata,
    ContextPack,
    ContextSection,
)
from sibyl_core.models.entities import Entity, EntityType
from sibyl_core.models.synthesis import (
    SynthesisArtifactFormat,
    SynthesisOutputType,
    SynthesisRunStatus,
)
from sibyl_core.tools.responses import SearchResponse, SearchResult
from tests.harness.auth import stub_auth_context
from tests.harness.source_observations import observed_graph_sources


def _org() -> SimpleNamespace:
    return SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))


def _ctx() -> AuthContext:
    return stub_auth_context(
        user_id=UUID("00000000-0000-0000-0000-000000000123"),
        org_role=OrganizationRole.MEMBER,
    )


@pytest.fixture(autouse=True)
async def materialization_source():
    async with observed_graph_sources(
        str(_org().id),
        [
            Entity(
                id="artifact:context",
                entity_type=EntityType.ARTIFACT,
                name="Context artifact",
                content="Only authorized source text enters the materialized pack.",
            )
        ],
    ):
        yield


def test_synthesis_plan_route_is_registered() -> None:
    paths = set(create_api_app().openapi()["paths"])

    assert "/synthesis/plan" in paths
    assert "/synthesis/draft" in paths
    assert "/synthesis/handbook" in paths


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
    assert response.source_packs[0].source_ids == ["graph_entity:artifact:context"]
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
    assert response.source_packs[0].freshness == {
        "graph_entity:artifact:context": "2026-05-14T12:00:00Z"
    }


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
    assert "[graph_entity:artifact:context]" in response.artifact.markdown
    assert response.artifact.json_payload["sections"][0]["source_ids"] == [
        "graph_entity:artifact:context"
    ]


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
    assert remember_calls[0]["metadata"]["source_ids"] == ["graph_entity:artifact:context"]
    assert '"source:context"' in remember_calls[0]["raw_content"]


@pytest.mark.asyncio
async def test_handbook_route_composes_a_cited_body_for_one_project() -> None:
    with (
        patch(
            "sibyl.api.routes.synthesis.verify_entity_project_access",
            AsyncMock(),
        ) as verify_project,
        patch("sibyl_core.services.synthesis.default_search", _fake_search),
        patch("sibyl_core.services.synthesis.default_related_sources", _empty_related),
        patch("sibyl_core.services.synthesis.default_context_pack", _fake_context_pack),
    ):
        response = await handbook_synthesis_route(
            project="project-sibyl",
            org=_org(),
            ctx=_ctx(),
        )

    verify_project.assert_awaited_once()
    assert response.project == "project-sibyl"
    assert response.source_ids == ["graph_entity:artifact:context"]
    # Run bookkeeping belongs in the response envelope, never in the file body.
    assert response.run_id not in response.markdown
    assert "[graph_entity:artifact:context]" in response.markdown


@pytest.mark.asyncio
async def test_handbook_route_is_stable_across_identical_requests() -> None:
    async def compose() -> Any:
        with (
            patch("sibyl.api.routes.synthesis.verify_entity_project_access", AsyncMock()),
            patch("sibyl_core.services.synthesis.default_search", _fake_search),
            patch("sibyl_core.services.synthesis.default_related_sources", _empty_related),
            patch("sibyl_core.services.synthesis.default_context_pack", _fake_context_pack),
        ):
            return await handbook_synthesis_route(
                project="project-sibyl",
                org=_org(),
                ctx=_ctx(),
            )

    first = await compose()
    second = await compose()

    assert first.run_id == second.run_id
    assert first.markdown == second.markdown


@pytest.mark.parametrize("surface", ["plan", "draft", "remember"])
async def test_synthesis_keeps_source_clocks_out_of_responses_and_saved_json(surface):
    packs = []

    async def context_with_clock(**kwargs):
        pack = await _fake_context_pack(**kwargs)
        pack.items[0].metadata.update(
            {
                "correction_blockers": {"private-root-clock": {"revision": 9, "blocking": False}},
                "source_bindings": {"declared-source": 2},
                "authored": {"correction_blockers": "literal documentation"},
            }
        )
        packs.append(pack)
        return pack

    remember = AsyncMock(return_value=SimpleNamespace(id="saved", source_id="saved-source"))
    with (
        patch(
            "sibyl.api.routes.synthesis.list_accessible_project_graph_ids",
            AsyncMock(return_value=["project-sibyl"]),
        ),
        patch("sibyl_core.services.synthesis.default_search", _fake_search),
        patch("sibyl_core.services.synthesis.default_related_sources", _empty_related),
        patch("sibyl_core.services.synthesis.default_context_pack", context_with_clock),
        patch("sibyl_core.services.synthesis.default_remember_artifact", remember),
    ):
        if surface == "plan":
            response = await plan_synthesis_route(
                SynthesisPlanRequest(goal="Write a sourced roadmap"), org=_org(), ctx=_ctx()
            )
        else:
            response = await draft_synthesis_route(
                SynthesisDraftRequest(
                    goal="Write a sourced roadmap",
                    output_format=SynthesisArtifactFormat.JSON,
                    remember=surface == "remember",
                ),
                org=_org(),
                ctx=_ctx(),
            )
    assert "private-root-clock" not in response.model_dump_json()
    for pack in response.source_packs:
        for source in pack.sources:
            assert "correction_blockers" not in source.metadata
            assert "source_bindings" not in source.metadata
            assert source.metadata["authored"] == {"correction_blockers": "literal documentation"}
    assert packs
    assert all("correction_blockers" in pack.items[0].metadata for pack in packs)
    if surface == "remember":
        remember.assert_awaited_once()
        assert "private-root-clock" not in remember.await_args.kwargs["raw_content"]
        assert '"declared-source"' not in remember.await_args.kwargs["raw_content"]
    else:
        remember.assert_not_awaited()
