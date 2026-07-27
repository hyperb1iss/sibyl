"""Per-project handbook: the distilled projection served to filesystem agents.

The handbook is a synthesis run pinned to a project rather than a separate
distiller. Riding ``SynthesisOutputType.HANDBOOK`` through plan/draft/verify
buys source-grounded selection, per-source citations, scope and lifecycle
filtering, and honest gap reporting without generating a single unsourced
sentence -- every line traces back to a cited memory, which is what makes the
output safe to materialize into ``.sibyl/memory/handbook.md``.
"""

from __future__ import annotations

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
    _section_markdown,
    verify_synthesis_run,
)

if TYPE_CHECKING:
    from sibyl_core.models.synthesis import SynthesisRun, SynthesisVerification

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
    for pack in run.source_packs:
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


__all__ = [
    "HANDBOOK_AUDIENCE",
    "HANDBOOK_GOAL",
    "HANDBOOK_MAX_SECTIONS",
    "HANDBOOK_SECTIONS",
    "handbook_synthesis_request",
    "render_handbook_markdown",
]
