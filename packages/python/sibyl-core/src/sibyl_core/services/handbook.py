"""Per-project handbook: the distilled projection served to filesystem agents.

The handbook is a synthesis run pinned to a project rather than a separate
distiller. Riding ``SynthesisOutputType.HANDBOOK`` through plan/draft/verify
buys source-grounded selection, per-source citations, scope and lifecycle
filtering, and honest gap reporting without generating a single unsourced
sentence -- every line traces back to a cited memory, which is what makes the
output safe to materialize into ``.sibyl/memory/handbook.md``.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from sibyl_core.models.synthesis import (
    SynthesisDepth,
    SynthesisOutputType,
    SynthesisRequest,
    SynthesisSectionRequest,
)
from sibyl_core.services.synthesis import (
    SECTION_TEMPLATES,
    SOURCE_ABSENCE_GAP_REASONS,
    _query_for,
    _section_markdown,
    _section_source_score,
    verify_synthesis_run,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sibyl_core.models.synthesis import (
        SynthesisRun,
        SynthesisSourcePack,
        SynthesisVerification,
    )

HANDBOOK_GOAL = "Distill the durable working knowledge of this project"
HANDBOOK_AUDIENCE = "an agent picking this project up cold"

HANDBOOK_SECTIONS: tuple[SynthesisSectionRequest, ...] = tuple(
    SynthesisSectionRequest(title=title, prompt=prompt)
    for title, prompt in SECTION_TEMPLATES[SynthesisOutputType.HANDBOOK]
)
HANDBOOK_MAX_SECTIONS = len(HANDBOOK_SECTIONS)


def handbook_synthesis_request(
    *,
    project_id: str,
    project_name: str | None = None,
) -> SynthesisRequest:
    """Build the synthesis request that defines a project's handbook.

    The request is the handbook's identity: ``_run_id`` hashes it, so a stable
    request means a stable run id and a re-materialization that only changes
    when the underlying graph does.
    """
    label = project_name or project_id
    return SynthesisRequest(
        goal=f"{HANDBOOK_GOAL}: {label}",
        output_type=SynthesisOutputType.HANDBOOK,
        audience=HANDBOOK_AUDIENCE,
        depth=SynthesisDepth.STANDARD,
        project=project_id,
        # Passing the template's sections explicitly rather than letting the
        # planner generate them turns off the "no section scored, so take the
        # globally strongest sources" fallback. That padding is right for a
        # one-off report, which would rather say something than nothing; a
        # handbook that files the same decision under four headings teaches the
        # reader to skim past all of them. An empty heading here is a true
        # statement about the graph.
        required_sections=list(HANDBOOK_SECTIONS),
        max_sections=HANDBOOK_MAX_SECTIONS,
        include_neighborhoods=True,
    )


def render_handbook_markdown(
    run: SynthesisRun,
    verification: SynthesisVerification | None = None,
) -> str:
    """Render a handbook run as the body of ``handbook.md``.

    Deliberately not ``render_synthesis_markdown``: that renderer leads with run
    bookkeeping (run id, output type, verification status), which is provenance
    for a one-off synthesis and noise in a file an agent greps every session.
    The frontmatter written by the exporter already carries the identity, so the
    body is sections, citations, and an explicit statement of what is missing.
    """
    current = verification or verify_synthesis_run(run)
    lines: list[str] = []
    for pack in cite_each_source_once(run):
        lines.extend(_section_markdown(pack))
        lines.append("")

    # Only absence gaps mean "nothing in the graph backs this". The other gap
    # reasons (missing freshness metadata, unresolved claims) describe sources
    # that are present but thin, and listing those as uncovered would tell the
    # reader the section is empty when it is right above them.
    uncovered = [gap for gap in current.gaps if gap.reason in SOURCE_ABSENCE_GAP_REASONS]
    if uncovered:
        lines.extend(["## Not Covered", ""])
        lines.append(
            "No citable memory backs these sections yet. Absence here is a gap in "
            "the graph, not a claim that nothing exists."
        )
        lines.append("")
        lines.extend(f"- {gap.title}" for gap in uncovered)

    body = "\n".join(lines).rstrip()
    return f"{body}\n" if body else ""


def cite_each_source_once(run: SynthesisRun) -> list[SynthesisSourcePack]:
    """Keep each source under the one section that fits it best.

    Sections are selected independently, so a memory that scores for several
    of them is rendered under every one: against the live Sibyl graph a single
    campaign note landed in all five headings and the file came out three
    times longer than the material in it. Length is the whole cost of a
    reference people re-read, so each source is kept where it scores highest
    and dropped elsewhere.

    Assignment is argmax over the same section-fit score the planner uses, not
    a threshold, so there is no tuned constant here. Ties keep the earlier
    section, which makes the output stable for a given run.
    """
    base_query = _query_for(run.request)
    sections = {
        section.section_id: section_request
        for section, section_request in zip(
            run.outline.sections, _section_requests(run), strict=False
        )
    }

    best: dict[str, tuple[float, str]] = {}
    for pack in run.source_packs:
        section = sections.get(pack.section_id)
        if section is None:
            continue
        for source in pack.sources:
            score = _section_source_score(
                section=section,
                source=source,
                base_query=base_query,
            )
            current = best.get(source.id)
            if current is None or score > current[0]:
                best[source.id] = (score, pack.section_id)

    return [_pack_keeping(pack, best) for pack in run.source_packs]


def _section_requests(run: SynthesisRun) -> Sequence[SynthesisSectionRequest]:
    """Recover the section requests that produced this run's outline."""
    if run.request.required_sections:
        return run.request.required_sections
    return [
        SynthesisSectionRequest(title=section.title, prompt=section.prompt)
        for section in run.outline.sections
    ]


def _pack_keeping(
    pack: SynthesisSourcePack,
    best: dict[str, tuple[float, str]],
) -> SynthesisSourcePack:
    kept = {
        source.id
        for source in pack.sources
        if best.get(source.id, (0.0, pack.section_id))[1] == pack.section_id
    }
    return replace(
        pack,
        sources=[source for source in pack.sources if source.id in kept],
        source_ids=[source_id for source_id in pack.source_ids if source_id in kept],
        freshness={
            source_id: value for source_id, value in pack.freshness.items() if source_id in kept
        },
    )


__all__ = [
    "HANDBOOK_AUDIENCE",
    "HANDBOOK_GOAL",
    "HANDBOOK_MAX_SECTIONS",
    "HANDBOOK_SECTIONS",
    "cite_each_source_once",
    "handbook_synthesis_request",
    "render_handbook_markdown",
]
