from __future__ import annotations

from typing import Any, Literal

import pytest

from sibyl_core.models.synthesis import (
    SynthesisOutputType,
    SynthesisSourceReference,
    SynthesisVerificationStatus,
)
from sibyl_core.services.handbook import (
    HANDBOOK_SECTIONS,
    handbook_synthesis_request,
    render_handbook_markdown,
)
from sibyl_core.services.synthesis import (
    SECTION_SOURCE_HINTS,
    plan_synthesis,
    verify_synthesis_run,
)
from sibyl_core.tools.responses import SearchResponse, SearchResult

_ORG = "org-handbook"
_PROJECT = "project-sibyl"


def _result(
    entity_id: str,
    entity_type: str,
    name: str,
    *,
    content: str | None = None,
    score: float = 0.8,
    origin: Literal["graph", "document"] = "graph",
) -> SearchResult:
    return SearchResult(
        id=entity_id,
        type=entity_type,
        name=name,
        content=content or f"{name} content",
        score=score,
        source=f"source:{entity_id}",
        result_origin=origin,
        metadata={"entity_type": entity_type},
    )


async def _empty_related(**kwargs: Any) -> list[SynthesisSourceReference]:
    return []


def _populated_search(results: list[SearchResult]):
    async def fake_search(**kwargs: Any) -> SearchResponse:
        requested = set(kwargs["types"])
        matched = [result for result in results if result.type in requested]
        return SearchResponse(
            results=matched,
            total=len(matched),
            query=kwargs["query"],
            filters={"types": kwargs["types"]},
        )

    return fake_search


async def _empty_search(**kwargs: Any) -> SearchResponse:
    return SearchResponse(results=[], total=0, query=kwargs["query"], filters={})


async def _plan(search_fn: Any):
    return await plan_synthesis(
        handbook_synthesis_request(project_id=_PROJECT, project_name="Sibyl"),
        organization_id=_ORG,
        accessible_projects={_PROJECT},
        search_fn=search_fn,
        related_fn=_empty_related,
    )


def test_handbook_request_is_stable_for_a_project() -> None:
    first = handbook_synthesis_request(project_id=_PROJECT, project_name="Sibyl")
    second = handbook_synthesis_request(project_id=_PROJECT, project_name="Sibyl")

    assert first == second
    assert first.output_type is SynthesisOutputType.HANDBOOK
    assert first.project == _PROJECT


@pytest.mark.asyncio
async def test_handbook_plan_uses_the_handbook_section_template() -> None:
    run = await _plan(_empty_search)

    assert [section.title for section in run.outline.sections] == [
        "Orientation",
        "Decisions In Force",
        "Current Work",
        "Gotchas",
        "Key Artifacts",
    ]


def test_each_handbook_section_claims_exactly_one_source_family() -> None:
    """A compound title double-fires the +4 hint bonus and blurs the sections.

    "Gotchas And Risks" matched both the ``gotcha`` family (error patterns) and
    the ``risk`` family (decisions, artifacts), so decisions scored the same
    bonus as the error patterns the heading is for. Every handbook heading has
    to claim exactly one family or the sections stop meaning different things.
    """
    for section in HANDBOOK_SECTIONS:
        normalized = section.title.lower()
        matched = [token for token in SECTION_SOURCE_HINTS if token in normalized]
        assert len(matched) == 1, f"{section.title!r} matched hint families {matched}"


@pytest.mark.asyncio
async def test_handbook_run_id_is_stable_across_identical_plans() -> None:
    first = await _plan(_empty_search)
    second = await _plan(_empty_search)

    assert first.run_id == second.run_id


@pytest.mark.asyncio
async def test_handbook_markdown_cites_every_rendered_source() -> None:
    run = await _plan(
        _populated_search(
            [
                _result(
                    "decision:scope-law",
                    "decision",
                    "Scope every graph call",
                    content="Every graph operation carries an explicit org scope.",
                    score=0.95,
                ),
                _result(
                    "task:ship-handbook",
                    "task",
                    "Ship the handbook",
                    content="Distil the project into a greppable handbook.",
                    score=0.9,
                ),
            ]
        )
    )

    markdown = render_handbook_markdown(run)

    assert "## Decisions In Force" in markdown
    assert "Scope every graph call" in markdown
    assert "decision:scope-law" in markdown


@pytest.mark.asyncio
async def test_handbook_markdown_omits_run_bookkeeping() -> None:
    run = await _plan(_populated_search([_result("decision:a", "decision", "A decision")]))

    markdown = render_handbook_markdown(run)

    assert "Run:" not in markdown
    assert "Output type:" not in markdown
    assert run.run_id not in markdown


@pytest.mark.asyncio
async def test_handbook_markdown_states_uncovered_sections_as_graph_gaps() -> None:
    run = await _plan(_empty_search)
    verification = verify_synthesis_run(run)

    markdown = render_handbook_markdown(run, verification)

    assert verification.status is SynthesisVerificationStatus.GAPS
    assert "## Not Covered" in markdown
    assert "gap in the graph" in markdown
    assert "Orientation" in markdown


@pytest.mark.asyncio
async def test_handbook_markdown_is_byte_stable_for_the_same_run() -> None:
    search_fn = _populated_search([_result("decision:a", "decision", "A decision")])
    first = render_handbook_markdown(await _plan(search_fn))
    second = render_handbook_markdown(await _plan(search_fn))

    assert first == second
