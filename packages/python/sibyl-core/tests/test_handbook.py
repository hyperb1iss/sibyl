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
    cite_each_source_once,
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
async def test_handbook_cites_each_source_under_one_heading_only() -> None:
    """Against the live graph one campaign note landed in all five sections.

    Sections select independently, so a broad memory renders under every
    heading it scores for and the file grows several times longer than the
    material in it.
    """
    run = await _plan(
        _populated_search(
            [
                _result(
                    "note:broad-campaign",
                    "task",
                    "Campaign handoff covering decisions, work, and gotchas",
                    content=(
                        "Decisions in force, current work, gotchas, key artifacts, "
                        "and orientation for the whole project."
                    ),
                    score=0.99,
                ),
                _result(
                    "decision:scope-law",
                    "decision",
                    "Scope every graph call",
                    content="Every graph operation carries an explicit org scope.",
                ),
            ]
        )
    )

    packs = cite_each_source_once(run)

    placements = [pack.section_id for pack in packs if "note:broad-campaign" in pack.source_ids]
    assert len(placements) == 1

    rendered = render_handbook_markdown(run)
    assert rendered.count("[note:broad-campaign]") == 1


@pytest.mark.asyncio
async def test_handbook_markdown_is_byte_stable_for_the_same_run() -> None:
    search_fn = _populated_search([_result("decision:a", "decision", "A decision")])
    first = render_handbook_markdown(await _plan(search_fn))
    second = render_handbook_markdown(await _plan(search_fn))

    assert first == second


# =============================================================================
# handbook-integrity-gate (1.2 exit criterion)
# =============================================================================
class TestHandbookIntegrityGate:
    """Zero hallucinated and zero self-referential writes from the distiller.

    The pipeline is extractive by construction (no LLM pass anywhere in
    plan/draft/verify), so integrity can only break two ways: a rendered line
    that traces to no selected source, or the distiller selecting synthesis
    and reflection output as its own input. Both are pinned here on a seeded
    fixture.
    """

    @staticmethod
    def _self_feeding_result(entity_id: str, capture_surface: str) -> SearchResult:
        return SearchResult(
            id=entity_id,
            type="artifact",
            name=f"Prior output {entity_id}",
            content="A previously distilled handbook body",
            score=0.99,
            source=f"source:{entity_id}",
            result_origin="graph",
            metadata={"entity_type": "artifact", "capture_surface": capture_surface},
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "capture_surface",
        ["synthesis_artifact", "reflection", "reflection_candidate", "reflection_source"],
    )
    async def test_self_feeding_surfaces_never_enter_the_plan(self, capture_surface: str) -> None:
        poisoned = self._self_feeding_result("artifact-poison", capture_surface)
        clean = _result("artifact-clean", "artifact", "Design doc", score=0.7)
        run = await _plan(_populated_search([poisoned, clean]))

        selected = {source.id for pack in run.source_packs for source in pack.sources}
        assert "artifact-poison" not in selected
        assert "artifact-clean" in selected
        assert "artifact-poison" not in render_handbook_markdown(run)

    @pytest.mark.asyncio
    async def test_capture_mode_synthesis_is_excluded_without_a_surface(self) -> None:
        poisoned = SearchResult(
            id="artifact-mode",
            type="artifact",
            name="Remembered synthesis",
            content="Synthesis-mode capture with no surface marker",
            score=0.99,
            source="source:artifact-mode",
            result_origin="graph",
            metadata={"entity_type": "artifact", "capture_mode": "synthesis"},
        )
        run = await _plan(_populated_search([poisoned]))

        selected = {source.id for pack in run.source_packs for source in pack.sources}
        assert "artifact-mode" not in selected

    @pytest.mark.asyncio
    async def test_every_rendered_line_is_scaffolding_or_traces_to_a_source(self) -> None:
        fixtures = [
            _result("decision-1", "decision", "Chose SurrealDB"),
            _result("task-1", "task", "Ship the exporter"),
            _result("artifact-1", "artifact", "Design doc"),
            _result("claim-1", "claim", "Exports are deterministic"),
        ]
        run = await _plan(_populated_search(fixtures))
        markdown = render_handbook_markdown(run)

        fixture_ids = {result.id for result in fixtures}
        fixture_names = {result.name for result in fixtures}
        section_titles = {section.title for section in HANDBOOK_SECTIONS}
        absence_scaffold = (
            "No citable memory backs these sections yet. Absence here is a gap in "
            "the graph, not a claim that nothing exists."
        )

        for line in markdown.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith("## "):
                assert stripped[3:] in ({"Not Covered"} | section_titles), (
                    f"unexpected heading: {stripped}"
                )
                continue
            if stripped.startswith("Sources: "):
                cited = {token.strip("`,") for token in stripped.removeprefix("Sources: ").split()}
                assert cited <= fixture_ids, f"hallucinated citation in: {stripped}"
                continue
            if stripped == "_No citable sources were available for this section._":
                continue
            if stripped == absence_scaffold:
                continue
            if stripped.startswith("- "):
                traces = any(name in stripped for name in fixture_names) and any(
                    f"[{source_id}]" in stripped for source_id in fixture_ids
                )
                uncovered_title = stripped.removeprefix("- ") in section_titles
                assert traces or uncovered_title, f"unsourced content line: {stripped}"
                continue
            raise AssertionError(f"unrecognized handbook line shape: {stripped}")

    @pytest.mark.asyncio
    async def test_a_run_with_no_sources_renders_only_the_absence_statement(self) -> None:
        run = await _plan(_empty_search)
        markdown = render_handbook_markdown(run)

        assert "## Not Covered" in markdown
        assert "Sources:" not in markdown
