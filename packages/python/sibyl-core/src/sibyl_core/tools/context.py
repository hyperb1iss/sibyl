"""Compile precise context packs for agents."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import asdict
from typing import Any

from sibyl_core.models.context import (
    ContextFacet,
    ContextIntent,
    ContextItem,
    ContextPack,
    ContextSection,
)
from sibyl_core.tools.responses import SearchResponse, SearchResult
from sibyl_core.tools.search import search as default_search

SearchFn = Callable[..., Awaitable[SearchResponse]]

FACET_TITLES = {
    ContextFacet.ACTIVE_WORK: "Active Work",
    ContextFacet.ARTIFACTS: "Artifacts",
    ContextFacet.CONSTRAINTS: "Constraints",
    ContextFacet.DECISIONS: "Decisions",
    ContextFacet.DOMAIN: "Domain Knowledge",
    ContextFacet.GOTCHAS: "Gotchas",
    ContextFacet.IDEATION: "Ideas",
    ContextFacet.PLANNING: "Plans",
    ContextFacet.PROCEDURES: "Procedures",
    ContextFacet.RECENT_MEMORY: "Recent Memory",
    ContextFacet.VERIFICATION: "Verification",
}

FACET_TYPES = {
    ContextFacet.ACTIVE_WORK: ["task", "epic", "project"],
    ContextFacet.ARTIFACTS: ["artifact", "document", "source", "config_file"],
    ContextFacet.CONSTRAINTS: ["rule", "convention"],
    ContextFacet.DECISIONS: ["decision"],
    ContextFacet.DOMAIN: ["domain", "topic", "claim"],
    ContextFacet.GOTCHAS: ["error_pattern", "pattern"],
    ContextFacet.IDEATION: ["idea"],
    ContextFacet.PLANNING: ["plan"],
    ContextFacet.PROCEDURES: ["procedure", "template", "tool"],
    ContextFacet.RECENT_MEMORY: ["session", "episode", "note"],
    ContextFacet.VERIFICATION: ["claim", "rule", "procedure"],
}

INTENT_FACETS = {
    ContextIntent.BUILD: [
        ContextFacet.ACTIVE_WORK,
        ContextFacet.DECISIONS,
        ContextFacet.CONSTRAINTS,
        ContextFacet.PROCEDURES,
        ContextFacet.GOTCHAS,
        ContextFacet.ARTIFACTS,
        ContextFacet.RECENT_MEMORY,
    ],
    ContextIntent.PLAN: [
        ContextFacet.PLANNING,
        ContextFacet.DECISIONS,
        ContextFacet.IDEATION,
        ContextFacet.DOMAIN,
        ContextFacet.CONSTRAINTS,
        ContextFacet.ACTIVE_WORK,
    ],
    ContextIntent.IDEATE: [
        ContextFacet.IDEATION,
        ContextFacet.DOMAIN,
        ContextFacet.DECISIONS,
        ContextFacet.PLANNING,
        ContextFacet.RECENT_MEMORY,
    ],
    ContextIntent.RESEARCH: [
        ContextFacet.DOMAIN,
        ContextFacet.ARTIFACTS,
        ContextFacet.RECENT_MEMORY,
        ContextFacet.DECISIONS,
        ContextFacet.CONSTRAINTS,
    ],
    ContextIntent.DEBUG: [
        ContextFacet.GOTCHAS,
        ContextFacet.PROCEDURES,
        ContextFacet.ARTIFACTS,
        ContextFacet.ACTIVE_WORK,
        ContextFacet.RECENT_MEMORY,
    ],
    ContextIntent.DECIDE: [
        ContextFacet.DECISIONS,
        ContextFacet.IDEATION,
        ContextFacet.DOMAIN,
        ContextFacet.CONSTRAINTS,
        ContextFacet.PLANNING,
    ],
    ContextIntent.LEARN: [
        ContextFacet.RECENT_MEMORY,
        ContextFacet.DOMAIN,
        ContextFacet.PROCEDURES,
        ContextFacet.DECISIONS,
    ],
    ContextIntent.GENERAL: [
        ContextFacet.ACTIVE_WORK,
        ContextFacet.DECISIONS,
        ContextFacet.PLANNING,
        ContextFacet.IDEATION,
        ContextFacet.DOMAIN,
        ContextFacet.PROCEDURES,
        ContextFacet.RECENT_MEMORY,
    ],
}


def _coerce_intent(intent: str | ContextIntent) -> ContextIntent:
    if isinstance(intent, ContextIntent):
        return intent
    try:
        return ContextIntent(intent.lower())
    except ValueError:
        return ContextIntent.GENERAL


def _query_for(goal: str, domain: str | None) -> str:
    goal = " ".join(goal.strip().split())
    if domain:
        domain = " ".join(domain.strip().split())
        return f"{goal} {domain}".strip()
    return goal


def _reason_for(result: SearchResult, facet: ContextFacet) -> str:
    result_type = result.type or "memory"
    if facet == ContextFacet.ACTIVE_WORK:
        return f"{result_type} can change what the agent should do next"
    if facet == ContextFacet.DECISIONS:
        return f"{result_type} records a choice or rationale the agent should preserve"
    if facet == ContextFacet.IDEATION:
        return f"{result_type} may contain options, discarded paths, or raw ideas"
    if facet == ContextFacet.PLANNING:
        return f"{result_type} may define sequencing, scope, or milestones"
    if facet == ContextFacet.ARTIFACTS:
        return f"{result_type} points at concrete things the agent may need to inspect or change"
    if facet == ContextFacet.PROCEDURES:
        return f"{result_type} describes repeatable steps or tools"
    if facet == ContextFacet.CONSTRAINTS:
        return f"{result_type} constrains acceptable work"
    if facet == ContextFacet.GOTCHAS:
        return f"{result_type} can prevent repeated mistakes"
    if facet == ContextFacet.VERIFICATION:
        return f"{result_type} can help prove the work is correct"
    return f"{result_type} adds relevant background"


def _item_from_result(result: SearchResult, facet: ContextFacet) -> ContextItem:
    return ContextItem(
        id=result.id,
        type=result.type,
        name=result.name,
        content=result.content,
        score=result.score,
        facet=facet,
        reason=_reason_for(result, facet),
        source=result.source,
        metadata=dict(result.metadata),
    )


def _dedupe_sections(sections: list[ContextSection], limit: int) -> list[ContextSection]:
    seen: set[str] = set()
    remaining = limit
    deduped: list[ContextSection] = []

    for section in sections:
        items: list[ContextItem] = []
        for item in sorted(section.items, key=lambda candidate: candidate.score, reverse=True):
            if remaining <= 0:
                break
            key = item.id or f"{item.type}:{item.name}"
            if key in seen:
                continue
            seen.add(key)
            items.append(item)
            remaining -= 1
        if items:
            deduped.append(ContextSection(facet=section.facet, title=section.title, items=items))
        if remaining <= 0:
            break

    return deduped


async def compile_context(
    goal: str,
    *,
    intent: str | ContextIntent = ContextIntent.BUILD,
    domain: str | None = None,
    project: str | None = None,
    organization_id: str | None = None,
    limit: int = 24,
    search_fn: SearchFn = default_search,
) -> ContextPack:
    """Build a small, structured context pack for an agent goal."""

    goal = goal.strip()
    if not goal:
        msg = "goal is required"
        raise ValueError(msg)
    if organization_id is None:
        msg = "organization_id is required"
        raise ValueError(msg)

    normalized_intent = _coerce_intent(intent)
    query = _query_for(goal, domain)
    limit = max(1, min(limit, 50))
    facets = INTENT_FACETS[normalized_intent]
    per_facet_limit = max(2, min(8, (limit + len(facets) - 1) // len(facets)))

    sections: list[ContextSection] = []
    for facet in facets:
        response = await search_fn(
            query=query,
            types=FACET_TYPES[facet],
            category=domain,
            project=project,
            limit=per_facet_limit,
            include_content=True,
            include_documents=facet == ContextFacet.ARTIFACTS,
            include_graph=True,
            organization_id=organization_id,
        )
        items = [_item_from_result(result, facet) for result in response.results]
        if items:
            sections.append(ContextSection(facet=facet, title=FACET_TITLES[facet], items=items))

    sections = _dedupe_sections(sections, limit)
    return ContextPack(
        goal=goal,
        intent=normalized_intent,
        query=query,
        domain=domain,
        project=project,
        sections=sections,
        total_items=sum(len(section.items) for section in sections),
    )


def context_pack_to_dict(pack: ContextPack) -> dict[str, Any]:
    return asdict(pack)


__all__ = [
    "FACET_TYPES",
    "INTENT_FACETS",
    "compile_context",
    "context_pack_to_dict",
]
