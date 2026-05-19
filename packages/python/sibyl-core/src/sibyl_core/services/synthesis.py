"""Deterministic source-aware synthesis planning."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import asdict, replace
from typing import Any

from sibyl_core.models.context import ContextIntent, ContextLayer, ContextPack
from sibyl_core.models.synthesis import (
    SynthesisArtifact,
    SynthesisArtifactFormat,
    SynthesisGap,
    SynthesisOutline,
    SynthesisOutlineSection,
    SynthesisOutputType,
    SynthesisRequest,
    SynthesisRun,
    SynthesisRunStatus,
    SynthesisSectionRequest,
    SynthesisSourcePack,
    SynthesisSourceReference,
    SynthesisVerification,
    SynthesisVerificationStatus,
)
from sibyl_core.tools.responses import ExploreResponse, SearchResponse, SearchResult

SynthesisSearchFn = Callable[..., Awaitable[SearchResponse]]
SynthesisRelatedFn = Callable[..., Awaitable[list[SynthesisSourceReference]]]
SynthesisContextFn = Callable[..., Awaitable[ContextPack]]
SynthesisRememberFn = Callable[..., Awaitable[Any]]

SECTION_TEMPLATES: dict[SynthesisOutputType, list[tuple[str, str]]] = {
    SynthesisOutputType.DOCUMENTATION: [
        ("Overview", "Summarize the supported system shape and purpose."),
        ("Key Decisions", "Capture source-backed architectural choices and rationale."),
        ("Implementation Notes", "List relevant implementation details and contracts."),
        ("Risks And Gaps", "Call out unsupported claims, unknowns, and missing sources."),
    ],
    SynthesisOutputType.REPORT: [
        ("Executive Summary", "Summarize the source-backed findings."),
        ("Evidence", "Organize the strongest supporting sources."),
        ("Risks And Gaps", "Identify missing, weak, or stale source coverage."),
    ],
    SynthesisOutputType.BRIEFING: [
        ("Situation", "Describe the current state from available sources."),
        ("Signals", "List important source-backed signals and decisions."),
        ("Next Actions", "Identify supported follow-up work."),
    ],
    SynthesisOutputType.ROADMAP: [
        ("Current State", "Describe what is already supported by the source graph."),
        ("Completed Work", "Summarize completed source-backed decisions and tasks."),
        ("Next Milestones", "Plan the next milestones from tasks, decisions, and artifacts."),
        ("Risks And Open Questions", "List unsupported claims and source gaps."),
    ],
    SynthesisOutputType.RELEASE_NOTES: [
        ("Highlights", "Summarize source-backed user-facing changes."),
        ("Behavior Changes", "List changed behavior with supporting sources."),
        ("Verification", "Collect test, audit, and gate evidence."),
        ("Known Gaps", "Disclose unsupported or incomplete release claims."),
    ],
    SynthesisOutputType.AUDIT_PACKET: [
        ("Scope", "Define the audited surface from available sources."),
        ("Findings", "List source-backed findings and evidence."),
        ("Evidence", "Collect source IDs, tasks, decisions, and artifacts."),
        ("Gaps And Risks", "Identify missing evidence and unsupported sections."),
    ],
    SynthesisOutputType.CUSTOM: [
        ("Overview", "Summarize the requested synthesis from available sources."),
        ("Evidence", "Organize source-backed details."),
        ("Gaps", "List unsupported claims and missing sources."),
    ],
}

SECTION_SOURCE_HINTS: dict[str, tuple[str, ...]] = {
    "completed": ("task", "procedure", "artifact"),
    "current": ("task", "decision", "artifact", "raw_memory"),
    "decision": ("decision",),
    "evidence": ("artifact", "document", "source", "claim", "procedure"),
    "gap": ("task", "claim", "decision", "artifact"),
    "highlight": ("artifact", "task", "decision"),
    "implementation": ("procedure", "task", "artifact", "document"),
    "milestone": ("task", "plan", "epic"),
    "risk": ("claim", "task", "decision", "artifact"),
    "verification": ("claim", "rule", "procedure", "artifact"),
}
TOKEN_STOPWORDS = {
    "and",
    "are",
    "from",
    "into",
    "needs",
    "plan",
    "the",
    "this",
    "with",
}
SOURCE_ABSENCE_GAP_REASONS = {
    "no_source_supports_requested_section",
    "no_materialized_sources",
    "no_citable_sources",
}
CORRECTED_LIFECYCLE_STATES = {
    "duplicate",
    "stale",
    "superseded",
    "wrong",
}
MAX_EXPLICIT_NEIGHBORHOOD_IDS = 100


async def default_search(**kwargs: Any) -> SearchResponse:
    from sibyl_core.tools.search import search

    return await search(**kwargs)


async def default_related_sources(
    *,
    entity_id: str,
    organization_id: str,
    accessible_projects: set[str] | None = None,
    limit: int = 5,
) -> list[SynthesisSourceReference]:
    from sibyl_core.tools.explore import explore

    response = await explore(
        mode="related",
        entity_id=entity_id,
        organization_id=organization_id,
        accessible_projects=accessible_projects,
        limit=limit,
    )
    return _sources_from_explore(response)


async def default_context_pack(**kwargs: Any) -> ContextPack:
    from sibyl_core.tools.context import compile_context

    return await compile_context(**kwargs)


async def default_remember_artifact(**kwargs: Any) -> Any:
    from sibyl_core.services.surreal_content import remember_raw_memory

    return await remember_raw_memory(**kwargs)


def _sources_from_explore(response: ExploreResponse) -> list[SynthesisSourceReference]:
    sources: list[SynthesisSourceReference] = []
    for entity in response.entities:
        metadata = dict(getattr(entity, "metadata", {}) or {})
        relation = getattr(entity, "relationship", None)
        if direction := getattr(entity, "direction", None):
            metadata["direction"] = direction
        if distance := getattr(entity, "distance", None):
            metadata["distance"] = distance
        sources.append(
            SynthesisSourceReference(
                id=str(entity.id),
                type=str(entity.type),
                name=str(entity.name),
                content_preview=str(getattr(entity, "description", "") or ""),
                score=0.5,
                origin="neighborhood",
                relation=str(relation) if relation else None,
                metadata=metadata,
            )
        )
    return sources


def _compact_text(value: str, max_chars: int = 320) -> str:
    compact = " ".join(value.strip().split())
    if len(compact) <= max_chars:
        return compact
    cutoff = compact.rfind(" ", 0, max_chars + 1)
    if cutoff < max_chars // 2:
        cutoff = max_chars
    return compact[:cutoff].rstrip() + "..."


def _tokens(value: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9][a-z0-9_-]+", value.lower())
        if len(token) > 2 and token not in TOKEN_STOPWORDS
    }


def _slug(value: str, *, fallback: str) -> str:
    slug = "-".join(re.findall(r"[a-z0-9]+", value.lower()))
    return slug or fallback


def _query_for(request: SynthesisRequest) -> str:
    parts = [request.seed_query or request.goal, request.domain or ""]
    return " ".join(" ".join(part.strip().split()) for part in parts if part).strip()


def _source_type_from_id(source_id: str, fallback: str) -> str:
    prefix = source_id.split(":", 1)[0].split("_", 1)[0].lower()
    if prefix and prefix != source_id.lower():
        return prefix
    return fallback


def _explicit_source(source_id: str, fallback_type: str, origin: str) -> SynthesisSourceReference:
    source_type = _source_type_from_id(source_id, fallback_type)
    return SynthesisSourceReference(
        id=source_id,
        type=source_type,
        name=source_id,
        score=1.0,
        origin=origin,
        metadata={"explicit": True},
    )


def _source_from_result(result: SearchResult, origin: str) -> SynthesisSourceReference:
    metadata = dict(result.metadata or {})
    source = result.source or metadata.get("source_id") or result.id
    metadata.setdefault("source_id", source)
    return SynthesisSourceReference(
        id=result.id,
        type=result.type,
        name=result.name,
        content_preview=_compact_text(result.content),
        score=float(result.score),
        source=str(source) if source is not None else None,
        origin=origin,
        metadata=metadata,
    )


def _dedupe_sources(
    sources: Iterable[SynthesisSourceReference],
) -> list[SynthesisSourceReference]:
    seen: set[str] = set()
    deduped: list[SynthesisSourceReference] = []
    for source in sources:
        if source.id in seen:
            continue
        seen.add(source.id)
        deduped.append(source)
    return deduped


async def _search_sources(
    *,
    query: str,
    organization_id: str,
    project: str | None,
    domain: str | None,
    accessible_projects: set[str] | None,
    search_fn: SynthesisSearchFn,
) -> list[SynthesisSourceReference]:
    specs: list[tuple[list[str], str, int]] = [
        (["decision"], "decision", 6),
        (["task", "epic", "plan"], "task", 8),
        (["artifact", "document", "source", "config_file"], "artifact", 8),
    ]
    sources: list[SynthesisSourceReference] = []
    for types, origin, limit in specs:
        response = await search_fn(
            query=query,
            types=types,
            category=domain,
            project=project,
            accessible_projects=accessible_projects,
            organization_id=organization_id,
            limit=limit,
            include_content=True,
        )
        sources.extend(_source_from_result(result, origin) for result in response.results)
    return sources


async def _neighborhood_sources(
    *,
    entity_ids: list[str],
    organization_id: str,
    accessible_projects: set[str] | None,
    related_fn: SynthesisRelatedFn,
) -> list[SynthesisSourceReference]:
    sources: list[SynthesisSourceReference] = []
    for entity_id in entity_ids:
        related = await related_fn(
            entity_id=entity_id,
            organization_id=organization_id,
            accessible_projects=accessible_projects,
            limit=5,
        )
        sources.extend(related)
    return sources


def _section_requests(request: SynthesisRequest) -> list[SynthesisSectionRequest]:
    if request.required_sections:
        return request.required_sections[: max(1, min(request.max_sections, 12))]
    template = SECTION_TEMPLATES[request.output_type]
    max_sections = max(1, min(request.max_sections, len(template), 12))
    return [
        SynthesisSectionRequest(title=title, prompt=prompt)
        for title, prompt in template[:max_sections]
    ]


def _source_search_text(source: SynthesisSourceReference) -> str:
    metadata_text = " ".join(str(value) for value in source.metadata.values() if value is not None)
    return " ".join(
        [
            source.id,
            source.type,
            source.name,
            source.content_preview,
            source.source or "",
            source.origin,
            source.relation or "",
            metadata_text,
        ]
    )


def _section_source_score(
    *,
    section: SynthesisSectionRequest,
    source: SynthesisSourceReference,
    base_query: str,
) -> float:
    section_text = " ".join([section.title, section.prompt or "", base_query])
    section_tokens = _tokens(section_text)
    source_tokens = _tokens(_source_search_text(source))
    overlap = len(section_tokens & source_tokens)
    hint_bonus = 0.0
    normalized_title = section.title.lower()
    for title_token, source_types in SECTION_SOURCE_HINTS.items():
        if title_token in normalized_title and source.type in source_types:
            hint_bonus += 4.0
    explicit_bonus = 4.0 if source.id in section.required_source_ids else 0.0
    if source.metadata.get("explicit"):
        explicit_bonus += 1.5
    return overlap + hint_bonus + explicit_bonus + source.score


def _select_section_sources(
    *,
    section: SynthesisSectionRequest,
    sources: list[SynthesisSourceReference],
    base_query: str,
    fallback_when_generated: bool,
) -> list[SynthesisSourceReference]:
    by_id = {source.id: source for source in sources}
    required_sources = [
        by_id[source_id] for source_id in section.required_source_ids if source_id in by_id
    ]
    scored = [
        (
            _section_source_score(section=section, source=source, base_query=base_query),
            index,
            source,
        )
        for index, source in enumerate(sources)
        if source.id not in section.required_source_ids
    ]
    matched = [
        source
        for score, _, source in sorted(scored, key=lambda item: (-item[0], item[1], item[2].id))
        if score > source.score
    ][:4]
    if not matched and fallback_when_generated and sources:
        matched = sources[: min(3, len(sources))]
    return _dedupe_sources([*required_sources, *matched])


def _gap_for_section(
    *,
    section_id: str,
    section: SynthesisSectionRequest,
    base_query: str,
) -> SynthesisGap:
    missing_source_ids = list(section.required_source_ids)
    reason = (
        "required_source_ids_not_found"
        if missing_source_ids
        else "no_source_supports_requested_section"
    )
    return SynthesisGap(
        section_id=section_id,
        title=section.title,
        reason=reason,
        query=" ".join([section.title, section.prompt or "", base_query]).strip(),
        missing_source_ids=missing_source_ids,
    )


def _run_id(request: SynthesisRequest) -> str:
    payload = repr(asdict(request)).encode()
    digest = hashlib.sha256(payload).hexdigest()[:16]
    return f"synthesis:{digest}"


def _metadata_unresolved_claims(metadata: dict[str, Any]) -> list[str]:
    value = metadata.get("unresolved_claims")
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    if metadata.get("supported") is False:
        return [str(metadata.get("claim") or "unsupported_claim")]
    return []


def _context_item_is_redacted(metadata: dict[str, Any], lifecycle_state: str | None) -> bool:
    return bool(metadata.get("redacted")) or lifecycle_state == "redacted"


def _context_item_is_hidden(lifecycle_state: str | None) -> bool:
    return lifecycle_state in {"deleted", "hidden"}


def _context_item_correction_reason(
    metadata: dict[str, Any],
    lifecycle_state: str | None,
) -> str | None:
    if lifecycle_state in CORRECTED_LIFECYCLE_STATES:
        return f"lifecycle_{lifecycle_state}"
    if metadata.get("superseded_by_source_id"):
        return "superseded_by_source_id"
    if metadata.get("duplicate_of_source_id"):
        return "duplicate_of_source_id"
    return None


def _context_item_allowed_for_render(
    *,
    metadata: dict[str, Any],
    project_id: str | None,
    principal_id: str | None,
    accessible_projects: set[str] | None,
) -> bool:
    memory_scope = metadata.get("memory_scope")
    memory_scope_value = str(memory_scope) if memory_scope is not None else None
    if memory_scope_value == "private":
        item_principal = metadata.get("principal_id")
        if item_principal is None or principal_id != str(item_principal):
            return False
    if memory_scope_value:
        from sibyl_core.auth.memory_policy import authorize_memory_read

        decision = authorize_memory_read(
            principal_id=principal_id,
            memory_scope=memory_scope_value,
            scope_key=str(metadata.get("scope_key") or project_id or "") or None,
            project_id=project_id,
            accessible_projects=accessible_projects,
        )
        return decision.allowed
    return not (
        accessible_projects is not None
        and project_id is not None
        and project_id not in accessible_projects
    )


async def materialize_synthesis_section_packs(
    run: SynthesisRun,
    *,
    organization_id: str,
    principal_id: str | None,
    accessible_projects: set[str] | None = None,
    context_fn: SynthesisContextFn = default_context_pack,
) -> SynthesisRun:
    """Populate section packs with policy-filtered context-pack sources."""

    from sibyl_core.tools.context import (
        context_item_freshness,
        context_item_lifecycle_state,
        context_item_project_id,
        context_item_source_id,
    )

    materialized_packs: list[SynthesisSourcePack] = []
    outline_sections: list[SynthesisOutlineSection] = []
    materialization_gaps: list[SynthesisGap] = []
    for section in run.outline.sections:
        pack = await context_fn(
            goal=section.source_query,
            intent=ContextIntent.RESEARCH,
            layer=ContextLayer.RECALL,
            domain=run.request.domain,
            project=run.request.project,
            accessible_projects=accessible_projects,
            principal_id=principal_id,
            organization_id=organization_id,
            limit=8,
            include_related=True,
            related_limit=3,
        )
        sources: list[SynthesisSourceReference] = []
        freshness: dict[str, str | None] = {}
        unresolved_claims: list[str] = []
        hidden_count = 0
        redaction_count = 0
        correction_count = 0
        correction_reasons: dict[str, int] = {}
        seen_source_ids: set[str] = set()
        for item in pack.items:
            source_id = context_item_source_id(item)
            lifecycle_state = context_item_lifecycle_state(item)
            metadata = dict(item.metadata)
            project_id = context_item_project_id(item)
            if _context_item_is_hidden(lifecycle_state) or not _context_item_allowed_for_render(
                metadata=metadata,
                project_id=project_id,
                principal_id=principal_id,
                accessible_projects=accessible_projects,
            ):
                hidden_count += 1
                continue
            if correction_reason := _context_item_correction_reason(metadata, lifecycle_state):
                correction_count += 1
                correction_reasons[correction_reason] = (
                    correction_reasons.get(correction_reason, 0) + 1
                )
                continue
            redacted = _context_item_is_redacted(metadata, lifecycle_state)
            if redacted:
                redaction_count += 1
            if source_id in seen_source_ids:
                continue
            seen_source_ids.add(source_id)
            item_freshness = context_item_freshness(item)
            freshness[source_id] = item_freshness
            unresolved_claims.extend(_metadata_unresolved_claims(metadata))
            source_metadata = {
                **metadata,
                "facet": item.facet.value,
                "freshness": item_freshness,
                "project_id": project_id,
                "reason": item.reason,
            }
            sources.append(
                SynthesisSourceReference(
                    id=source_id,
                    type=item.type,
                    name=item.name,
                    content_preview="" if redacted else _compact_text(item.content),
                    score=item.score,
                    source=item.source,
                    origin="context_pack",
                    metadata=source_metadata,
                )
            )

        if not sources:
            materialization_gaps.append(
                SynthesisGap(
                    section_id=section.section_id,
                    title=section.title,
                    reason="no_materialized_sources",
                    query=section.source_query,
                )
            )
        source_ids = [source.id for source in sources]
        section_gaps = [
            *section.gaps,
            *[gap for gap in materialization_gaps if gap.section_id == section.section_id],
        ]
        outline_sections.append(replace(section, source_ids=source_ids, gaps=section_gaps))
        materialized_packs.append(
            SynthesisSourcePack(
                section_id=section.section_id,
                title=section.title,
                query=section.source_query,
                source_ids=source_ids,
                sources=sources,
                hidden_count=hidden_count,
                redaction_count=redaction_count,
                correction_count=correction_count,
                correction_reasons=correction_reasons,
                freshness=freshness,
                unresolved_claims=list(dict.fromkeys(unresolved_claims)),
            )
        )

    gaps = [*run.verification.gaps, *materialization_gaps]
    verification = SynthesisVerification(
        status=(SynthesisVerificationStatus.GAPS if gaps else run.verification.status),
        source_count=len(
            {source_id for pack in materialized_packs for source_id in pack.source_ids}
        ),
        gap_count=len(gaps),
        gaps=gaps,
    )
    return replace(
        run,
        outline=replace(run.outline, sections=outline_sections),
        source_packs=materialized_packs,
        verification=verification,
    )


def _source_citation(source_id: str) -> str:
    return f"[{source_id}]"


def _verification_gap(
    *,
    pack: SynthesisSourcePack,
    reason: str,
    query: str,
) -> SynthesisGap:
    return SynthesisGap(
        section_id=pack.section_id,
        title=pack.title,
        reason=reason,
        query=query,
    )


def _dedupe_gaps(gaps: Iterable[SynthesisGap]) -> list[SynthesisGap]:
    seen: set[tuple[str, str, str]] = set()
    deduped: list[SynthesisGap] = []
    for gap in gaps:
        key = (gap.section_id, gap.reason, gap.query)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(gap)
    return deduped


def verify_synthesis_run(run: SynthesisRun) -> SynthesisVerification:
    sourceful_sections = {pack.section_id for pack in run.source_packs if pack.source_ids}
    gaps: list[SynthesisGap] = [
        gap
        for gap in run.verification.gaps
        if not (gap.section_id in sourceful_sections and gap.reason in SOURCE_ABSENCE_GAP_REASONS)
    ]
    for pack in run.source_packs:
        if not pack.source_ids:
            gaps.append(
                _verification_gap(
                    pack=pack,
                    reason="no_citable_sources",
                    query=pack.query,
                )
            )
            continue
        missing_freshness = [
            source_id for source_id in pack.source_ids if not pack.freshness.get(source_id)
        ]
        if missing_freshness:
            gaps.append(
                _verification_gap(
                    pack=pack,
                    reason="missing_freshness_metadata",
                    query=", ".join(missing_freshness),
                )
            )
        for claim in pack.unresolved_claims:
            gaps.append(
                _verification_gap(
                    pack=pack,
                    reason="unresolved_claim",
                    query=claim,
                )
            )

    source_ids = {source_id for pack in run.source_packs for source_id in pack.source_ids}
    deduped_gaps = _dedupe_gaps(gaps)
    return SynthesisVerification(
        status=(
            SynthesisVerificationStatus.GAPS if deduped_gaps else SynthesisVerificationStatus.PASS
        ),
        source_count=len(source_ids),
        gap_count=len(deduped_gaps),
        gaps=deduped_gaps,
    )


def apply_synthesis_verification(run: SynthesisRun) -> SynthesisRun:
    verification = verify_synthesis_run(run)
    return replace(
        run,
        status=(
            SynthesisRunStatus.VERIFIED
            if verification.status is SynthesisVerificationStatus.PASS
            else run.status
        ),
        verification=verification,
    )


def _source_to_json(source: SynthesisSourceReference, pack: SynthesisSourcePack) -> dict[str, Any]:
    return {
        "id": source.id,
        "type": source.type,
        "name": source.name,
        "content_preview": source.content_preview,
        "score": source.score,
        "source": source.source,
        "origin": source.origin,
        "relation": source.relation,
        "freshness": pack.freshness.get(source.id),
        "metadata": dict(source.metadata),
    }


def _section_to_json(pack: SynthesisSourcePack) -> dict[str, Any]:
    return {
        "section_id": pack.section_id,
        "title": pack.title,
        "query": pack.query,
        "source_ids": list(pack.source_ids),
        "sources": [_source_to_json(source, pack) for source in pack.sources],
        "hidden_count": pack.hidden_count,
        "redaction_count": pack.redaction_count,
        "correction_count": pack.correction_count,
        "correction_reasons": dict(pack.correction_reasons),
        "freshness": dict(pack.freshness),
        "unresolved_claims": list(pack.unresolved_claims),
    }


def _artifact_json_payload(
    run: SynthesisRun,
    verification: SynthesisVerification,
) -> dict[str, Any]:
    return {
        "run_id": run.run_id,
        "title": run.outline.title,
        "output_type": run.outline.output_type.value,
        "audience": run.outline.audience,
        "sections": [_section_to_json(pack) for pack in run.source_packs],
        "verification": asdict(verification),
    }


def _source_markdown(source: SynthesisSourceReference) -> str:
    citation = _source_citation(source.id)
    if not source.content_preview:
        return f"- {source.name} {citation}"
    return f"- {source.name}: {source.content_preview} {citation}"


def _section_markdown(pack: SynthesisSourcePack) -> list[str]:
    lines = [f"## {pack.title}", ""]
    if pack.sources:
        lines.extend(_source_markdown(source) for source in pack.sources)
    else:
        lines.append("_No citable sources were available for this section._")
    lines.append("")
    if pack.source_ids:
        cited_sources = ", ".join(f"`{source_id}`" for source_id in pack.source_ids)
        lines.append(f"Sources: {cited_sources}")
    if pack.hidden_count:
        lines.append(f"Hidden context omitted: {pack.hidden_count}")
    if pack.redaction_count:
        lines.append(f"Redacted source previews: {pack.redaction_count}")
    if pack.correction_count:
        correction_reasons = ", ".join(
            f"`{reason}`={count}" for reason, count in sorted(pack.correction_reasons.items())
        )
        lines.append(
            f"Corrected sources omitted: {pack.correction_count}"
            + (f" ({correction_reasons})" if correction_reasons else "")
        )
    if pack.freshness:
        freshness = ", ".join(
            f"`{source_id}`={value or 'unknown'}"
            for source_id, value in sorted(pack.freshness.items())
        )
        lines.append(f"Freshness: {freshness}")
    if pack.unresolved_claims:
        lines.append("Unresolved claims:")
        lines.extend(f"- {claim}" for claim in pack.unresolved_claims)
    return lines


def render_synthesis_markdown(
    run: SynthesisRun,
    verification: SynthesisVerification | None = None,
) -> str:
    current_verification = verification or verify_synthesis_run(run)
    lines = [
        f"# {run.outline.title}",
        "",
        f"Run: `{run.run_id}`",
        f"Output type: `{run.outline.output_type.value}`",
    ]
    if run.outline.audience:
        lines.append(f"Audience: {run.outline.audience}")
    lines.extend(
        [
            f"Verification: `{current_verification.status.value}`",
            "",
        ]
    )
    for pack in run.source_packs:
        lines.extend(_section_markdown(pack))
        lines.append("")
    if current_verification.gaps:
        lines.extend(["## Verification Gaps", ""])
        for gap in current_verification.gaps:
            lines.append(f"- {gap.title}: {gap.reason} ({gap.query})")
    return "\n".join(lines).rstrip() + "\n"


def draft_synthesis_artifact(
    run: SynthesisRun,
    *,
    output_format: SynthesisArtifactFormat = SynthesisArtifactFormat.MARKDOWN,
) -> SynthesisArtifact:
    verification = verify_synthesis_run(run)
    markdown = render_synthesis_markdown(run, verification)
    json_payload = _artifact_json_payload(run, verification)
    generated_text_hash = hashlib.sha256(markdown.encode()).hexdigest()
    source_ids = list(
        dict.fromkeys(source_id for pack in run.source_packs for source_id in pack.source_ids)
    )
    section_source_ids = {pack.section_id: list(pack.source_ids) for pack in run.source_packs}
    artifact_id = f"artifact:{run.run_id.split(':', 1)[-1]}:{generated_text_hash[:12]}"
    return SynthesisArtifact(
        artifact_id=artifact_id,
        format=output_format,
        title=run.outline.title,
        markdown=markdown,
        json_payload=json_payload,
        source_ids=source_ids,
        section_source_ids=section_source_ids,
        generated_text_hash=generated_text_hash,
        verification=verification,
    )


async def remember_synthesis_artifact(
    artifact: SynthesisArtifact,
    run: SynthesisRun,
    *,
    organization_id: str,
    principal_id: str,
    memory_scope: str = "private",
    scope_key: str | None = None,
    tags: list[str] | None = None,
    remember_fn: SynthesisRememberFn = default_remember_artifact,
) -> SynthesisArtifact:
    if not principal_id:
        raise ValueError("principal_id is required")
    content = (
        artifact.markdown
        if artifact.format is SynthesisArtifactFormat.MARKDOWN
        else json.dumps(artifact.json_payload, indent=2, sort_keys=True)
    )
    source_id = f"{artifact.artifact_id}:generated"
    memory = await remember_fn(
        organization_id=organization_id,
        principal_id=principal_id,
        source_id=source_id,
        raw_content=content,
        title=artifact.title,
        memory_scope=memory_scope,
        scope_key=scope_key,
        tags=list(dict.fromkeys(["synthesis", run.request.output_type.value, *(tags or [])])),
        metadata={
            "capture_mode": "synthesis",
            "capture_surface": "synthesis_artifact",
            "remember_kind": "artifact",
            "synthesis_run_id": run.run_id,
            "synthesis_artifact_id": artifact.artifact_id,
            "generated_text_hash": artifact.generated_text_hash,
            "output_type": run.request.output_type.value,
            "audience": run.request.audience,
            "source_ids": list(artifact.source_ids),
            "section_source_ids": dict(artifact.section_source_ids),
            "verification": asdict(artifact.verification),
            "unresolved_claims": [
                claim for pack in run.source_packs for claim in pack.unresolved_claims
            ],
        },
        provenance={
            "synthesis_run_id": run.run_id,
            "source_ids": list(artifact.source_ids),
            "section_source_ids": dict(artifact.section_source_ids),
        },
        capture_surface="synthesis_artifact",
        entity_type="artifact",
    )
    return replace(
        artifact,
        remembered_memory_id=str(getattr(memory, "id", "")) or None,
        remembered_source_id=str(getattr(memory, "source_id", "")) or None,
    )


async def plan_synthesis(
    request: SynthesisRequest,
    *,
    organization_id: str,
    accessible_projects: set[str] | None = None,
    search_fn: SynthesisSearchFn = default_search,
    related_fn: SynthesisRelatedFn = default_related_sources,
) -> SynthesisRun:
    goal = " ".join(request.goal.strip().split())
    if not goal:
        raise ValueError("goal is required")
    if not organization_id:
        raise ValueError("organization_id is required")

    request = SynthesisRequest(
        goal=goal,
        output_type=request.output_type,
        audience=request.audience,
        depth=request.depth,
        seed_query=request.seed_query,
        project=request.project,
        domain=request.domain,
        entity_ids=list(dict.fromkeys(request.entity_ids)),
        decision_ids=list(dict.fromkeys(request.decision_ids)),
        task_ids=list(dict.fromkeys(request.task_ids)),
        artifact_ids=list(dict.fromkeys(request.artifact_ids)),
        required_sections=request.required_sections,
        constraints=list(request.constraints),
        max_sections=request.max_sections,
        include_neighborhoods=request.include_neighborhoods,
    )
    base_query = _query_for(request)
    explicit_sources = [
        *[_explicit_source(source_id, "entity", "entity") for source_id in request.entity_ids],
        *[
            _explicit_source(source_id, "decision", "decision")
            for source_id in request.decision_ids
        ],
        *[_explicit_source(source_id, "task", "task") for source_id in request.task_ids],
        *[
            _explicit_source(source_id, "artifact", "artifact")
            for source_id in request.artifact_ids
        ],
    ]
    explicit_sources = _dedupe_sources(explicit_sources)[:MAX_EXPLICIT_NEIGHBORHOOD_IDS]
    searched_sources = await _search_sources(
        query=base_query,
        organization_id=organization_id,
        project=request.project,
        domain=request.domain,
        accessible_projects=accessible_projects,
        search_fn=search_fn,
    )
    neighborhood_sources = (
        await _neighborhood_sources(
            entity_ids=[source.id for source in explicit_sources],
            organization_id=organization_id,
            accessible_projects=accessible_projects,
            related_fn=related_fn,
        )
        if request.include_neighborhoods and explicit_sources
        else []
    )
    sources = _dedupe_sources([*explicit_sources, *searched_sources, *neighborhood_sources])

    section_inputs = _section_requests(request)
    generated_outline = not request.required_sections
    outline_sections: list[SynthesisOutlineSection] = []
    source_packs: list[SynthesisSourcePack] = []
    gaps: list[SynthesisGap] = []
    for index, section in enumerate(section_inputs, start=1):
        section_id = f"section:{index:02d}-{_slug(section.title, fallback='section')}"
        selected_sources = _select_section_sources(
            section=section,
            sources=sources,
            base_query=base_query,
            fallback_when_generated=generated_outline,
        )
        section_gaps: list[SynthesisGap] = []
        if not selected_sources:
            section_gaps.append(
                _gap_for_section(section_id=section_id, section=section, base_query=base_query)
            )
        missing_required = []
        if selected_sources:
            missing_required = [
                source_id
                for source_id in section.required_source_ids
                if source_id not in {source.id for source in selected_sources}
            ]
        if missing_required:
            section_gaps.append(
                SynthesisGap(
                    section_id=section_id,
                    title=section.title,
                    reason="required_source_ids_not_found",
                    query=" ".join([section.title, section.prompt or "", base_query]).strip(),
                    missing_source_ids=missing_required,
                )
            )
        gaps.extend(section_gaps)
        source_ids = [source.id for source in selected_sources]
        source_query = " ".join([section.title, section.prompt or "", base_query]).strip()
        outline_sections.append(
            SynthesisOutlineSection(
                section_id=section_id,
                title=section.title,
                prompt=section.prompt or f"Draft {section.title} from cited sources.",
                source_query=source_query,
                source_ids=source_ids,
                gaps=section_gaps,
            )
        )
        source_packs.append(
            SynthesisSourcePack(
                section_id=section_id,
                title=section.title,
                query=source_query,
                source_ids=source_ids,
                sources=selected_sources,
            )
        )

    outline = SynthesisOutline(
        title=goal,
        output_type=request.output_type,
        audience=request.audience,
        sections=outline_sections,
    )
    verification = SynthesisVerification(
        status=(SynthesisVerificationStatus.GAPS if gaps else SynthesisVerificationStatus.PENDING),
        source_count=len({source.id for source in sources}),
        gap_count=len(gaps),
        gaps=gaps,
    )
    return SynthesisRun(
        run_id=_run_id(request),
        status=SynthesisRunStatus.PLANNED,
        request=request,
        outline=outline,
        source_packs=source_packs,
        verification=verification,
    )


def synthesis_run_to_dict(run: SynthesisRun) -> dict[str, Any]:
    return asdict(run)


def synthesis_artifact_to_dict(artifact: SynthesisArtifact) -> dict[str, Any]:
    return asdict(artifact)


__all__ = [
    "SynthesisContextFn",
    "SynthesisRelatedFn",
    "SynthesisRememberFn",
    "SynthesisSearchFn",
    "apply_synthesis_verification",
    "default_context_pack",
    "default_related_sources",
    "default_remember_artifact",
    "default_search",
    "draft_synthesis_artifact",
    "materialize_synthesis_section_packs",
    "plan_synthesis",
    "remember_synthesis_artifact",
    "render_synthesis_markdown",
    "synthesis_artifact_to_dict",
    "synthesis_run_to_dict",
    "verify_synthesis_run",
]
