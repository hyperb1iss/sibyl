"""Compile precise context packs for agents."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import asdict, replace
from typing import Any

import structlog

from sibyl_core.auth.memory_policy import memory_scope_policy_key
from sibyl_core.embeddings.providers import configured_embedding_provider
from sibyl_core.models.context import (
    ContextFacet,
    ContextIntent,
    ContextItem,
    ContextItemQualityMetadata,
    ContextLayer,
    ContextPack,
    ContextRelatedItem,
    ContextSection,
)
from sibyl_core.retrieval.search import (
    build_context_retrieval_plan,
    context_search,
)
from sibyl_core.services.graph import get_surreal_graph_runtime
from sibyl_core.services.surreal_content import (
    MemoryScope,
    RawMemory,
    recall_raw_memory,
)
from sibyl_core.tools.helpers import _project_id_for_policy
from sibyl_core.tools.responses import SearchResponse, SearchResult

SearchFn = Callable[..., Awaitable[SearchResponse]]
RelatedFn = Callable[..., Awaitable[list[ContextRelatedItem]]]
RelatedBatchFn = Callable[..., Awaitable[dict[str, list[ContextRelatedItem]]]]
RawMemoryRecallFn = Callable[..., Awaitable[list[RawMemory]]]
ActiveWorkFn = Callable[..., Awaitable[list["ContextItem"]]]

log = structlog.get_logger()

FACET_TITLES = {
    ContextFacet.ACTIVE_WORK: "Active Work",
    ContextFacet.PRIOR_ART: "Prior Art",
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
    ContextFacet.PRIOR_ART: ["task", "epic"],
    ContextFacet.ARTIFACTS: ["artifact", "document", "source", "config_file"],
    ContextFacet.CONSTRAINTS: ["rule", "guide"],
    ContextFacet.DECISIONS: ["decision"],
    ContextFacet.DOMAIN: ["domain", "topic", "claim"],
    ContextFacet.GOTCHAS: ["error_pattern", "pattern"],
    ContextFacet.IDEATION: ["idea"],
    ContextFacet.PLANNING: ["plan"],
    ContextFacet.PROCEDURES: ["procedure", "template", "tool"],
    ContextFacet.RECENT_MEMORY: ["session", "episode", "note"],
    ContextFacet.VERIFICATION: ["claim", "rule", "procedure"],
}

_RECENT_MEMORY_FACT_TYPES = ("claim", "event", "preference")

INTENT_FACETS = {
    ContextIntent.BUILD: [
        ContextFacet.ACTIVE_WORK,
        ContextFacet.DECISIONS,
        ContextFacet.CONSTRAINTS,
        ContextFacet.PROCEDURES,
        ContextFacet.GOTCHAS,
        ContextFacet.PRIOR_ART,
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
        ContextFacet.PRIOR_ART,
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
    ContextIntent.REVIEW: [
        ContextFacet.VERIFICATION,
        ContextFacet.DECISIONS,
        ContextFacet.CONSTRAINTS,
        ContextFacet.GOTCHAS,
        ContextFacet.ARTIFACTS,
        ContextFacet.ACTIVE_WORK,
        ContextFacet.PRIOR_ART,
        ContextFacet.RECENT_MEMORY,
    ],
    ContextIntent.DEBUG: [
        ContextFacet.GOTCHAS,
        ContextFacet.PROCEDURES,
        ContextFacet.ARTIFACTS,
        ContextFacet.ACTIVE_WORK,
        ContextFacet.PRIOR_ART,
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
        ContextFacet.PRIOR_ART,
        ContextFacet.RECENT_MEMORY,
    ],
}

LAYER_LIMITS = {
    ContextLayer.WAKE: 8,
    ContextLayer.RECALL: 24,
    ContextLayer.DEEP_SEARCH: 50,
}


_WORK_ITEM_TYPES = {"task", "epic"}
# Tasks finish as "done"; epic container status derives to "completed".
_PRIOR_WORK_STATUSES = {"done", "completed"}
_DROPPED_WORK_STATUSES = {"archived"}
_IN_FLIGHT_WORK_STATUSES = {"doing", "in_progress", "blocked", "review"}


def _facet_for_type(entity_type: str, facets: list[ContextFacet]) -> ContextFacet:
    normalized_type = entity_type.lower()
    for facet in facets:
        if normalized_type in FACET_TYPES[facet]:
            return facet
    for fallback in (ContextFacet.RECENT_MEMORY, ContextFacet.DOMAIN, ContextFacet.ACTIVE_WORK):
        if fallback in facets:
            return fallback
    return facets[0]


def _work_item_status(result: SearchResult) -> str | None:
    metadata = result.metadata or {}
    status = metadata.get("status")
    if status is None:
        return None
    return str(getattr(status, "value", status)).lower() or None


def _facet_for_result(result: SearchResult, facets: list[ContextFacet]) -> ContextFacet | None:
    """Route a result to a facet, keeping completed work out of Active Work."""

    normalized_type = (result.type or "").lower()
    if normalized_type in _WORK_ITEM_TYPES:
        status = _work_item_status(result)
        if status in _DROPPED_WORK_STATUSES:
            return None
        if status in _PRIOR_WORK_STATUSES:
            return ContextFacet.PRIOR_ART if ContextFacet.PRIOR_ART in facets else None
    return _facet_for_type(normalized_type, facets)


def _coerce_intent(intent: str | ContextIntent) -> ContextIntent:
    if isinstance(intent, ContextIntent):
        return intent
    try:
        return ContextIntent(intent.lower())
    except ValueError:
        return ContextIntent.GENERAL


def _coerce_layer(layer: str | ContextLayer) -> ContextLayer:
    if isinstance(layer, ContextLayer):
        return layer
    try:
        return ContextLayer(layer.lower())
    except ValueError:
        return ContextLayer.RECALL


def _facets_for_layer(intent: ContextIntent, layer: ContextLayer) -> list[ContextFacet]:
    facets = list(INTENT_FACETS[intent])
    if layer is not ContextLayer.WAKE:
        return facets

    priority = [
        ContextFacet.RECENT_MEMORY,
        ContextFacet.ACTIVE_WORK,
        ContextFacet.DECISIONS,
        ContextFacet.GOTCHAS,
        ContextFacet.PROCEDURES,
    ]
    wake_facets = [facet for facet in priority if facet in facets]
    return wake_facets or facets[:3]


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
    if facet == ContextFacet.PRIOR_ART:
        return f"completed {result_type} whose learnings may transfer to this goal"
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
    if facet == ContextFacet.RECENT_MEMORY and result_type == "raw_memory":
        return "raw memory matched the goal and preserves verbatim source context"
    if facet == ContextFacet.CONSTRAINTS:
        return f"{result_type} constrains acceptable work"
    if facet == ContextFacet.GOTCHAS:
        return f"{result_type} can prevent repeated mistakes"
    if facet == ContextFacet.VERIFICATION:
        return f"{result_type} can help prove the work is correct"
    return f"{result_type} adds relevant background"


async def get_graph_runtime(group_id: str):
    return await get_surreal_graph_runtime(
        group_id,
        embedding_provider=configured_embedding_provider(),
    )


async def default_search(**kwargs: Any) -> SearchResponse:
    from sibyl_core.tools.search import search

    return await search(**kwargs)


def _project_id_for(entity: Any) -> str | None:
    return _project_id_for_policy(entity)


def _compact_metadata_value(value: Any, max_chars: int = 120) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        value = value.isoformat()
    elif isinstance(value, bool | int | float):
        value = str(value)
    elif not isinstance(value, str):
        return None

    compact = " ".join(value.strip().split())
    if not compact:
        return None
    if len(compact) <= max_chars:
        return compact
    return compact[: max_chars - 3].rstrip() + "..."


def _first_metadata_value(metadata: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        if value := _compact_metadata_value(metadata.get(key)):
            return value
    return None


def _quality_metadata_from_result(result: SearchResult) -> Any:
    metadata = result.metadata or {}
    values = dict(
        origin=_compact_metadata_value(result.result_origin),
        source=(
            _compact_metadata_value(result.source)
            or _first_metadata_value(
                metadata,
                "source",
                "source_file",
                "source_name",
                "source_title",
                "source_id",
                "reflection_source_title",
            )
            or _compact_metadata_value(result.id)
        ),
        url=_compact_metadata_value(result.url) or _first_metadata_value(metadata, "url"),
        created_at=_first_metadata_value(metadata, "created_at", "created", "captured_at"),
        updated_at=_first_metadata_value(metadata, "updated_at", "modified_at", "last_updated"),
        valid_at=_first_metadata_value(metadata, "valid_at", "timestamp", "event_time"),
        project_id=_first_metadata_value(metadata, "project_id", "project"),
    )
    return ContextItemQualityMetadata(**values)


def _is_synthetic_relationship_result(result: SearchResult) -> bool:
    metadata = result.metadata or {}
    source_id = str(metadata.get("source_id") or result.id)
    return bool(
        metadata.get("relationship")
        and metadata.get("source_node_uuid")
        and metadata.get("target_node_uuid")
        and source_id.startswith("rel_")
    )


async def _default_related_items(
    *,
    entity_id: str,
    organization_id: str,
    accessible_projects: set[str] | None = None,
    limit: int = 3,
) -> list[ContextRelatedItem]:
    runtime = await get_graph_runtime(organization_id)
    raw_results = await runtime.relationship_manager.get_related_entities(
        entity_id=entity_id,
        max_depth=1,
        limit=limit,
    )

    related: list[ContextRelatedItem] = []
    for entity, relationship in raw_results:
        if accessible_projects is not None:
            entity_project = _project_id_for(entity)
            if entity_project is not None and entity_project not in accessible_projects:
                continue

        related.append(
            ContextRelatedItem(
                id=str(entity.id),
                type=str(entity.entity_type.value),
                name=str(entity.name),
                relationship=str(relationship.relationship_type.value),
                direction="outgoing" if relationship.source_id == entity_id else "incoming",
            )
        )
        if len(related) >= limit:
            break
    return related


def _relationship_value(value: Any) -> str:
    enum_value = getattr(value, "value", None)
    return str(enum_value if enum_value is not None else value)


async def _default_related_items_batch(
    *,
    entity_ids: Sequence[str],
    organization_id: str,
    accessible_projects: set[str] | None = None,
    limit: int = 3,
) -> dict[str, list[ContextRelatedItem]]:
    ids = list(dict.fromkeys(str(entity_id) for entity_id in entity_ids if entity_id))
    if not ids:
        return {}

    runtime = await get_graph_runtime(organization_id)
    relationship_manager = runtime.relationship_manager
    batch_lookup = getattr(relationship_manager, "get_related_entities_batch", None)
    if callable(batch_lookup):
        raw_by_seed = await batch_lookup(ids, limit_per_entity=limit)
    else:
        raw_by_seed = {
            entity_id: await relationship_manager.get_related_entities(
                entity_id=entity_id,
                max_depth=1,
                limit=limit,
            )
            for entity_id in ids
        }

    related_by_seed: dict[str, list[ContextRelatedItem]] = {}
    for seed_id, raw_results in raw_by_seed.items():
        related: list[ContextRelatedItem] = []
        for entity, relationship in raw_results:
            if accessible_projects is not None:
                entity_project = _project_id_for(entity)
                if entity_project is not None and entity_project not in accessible_projects:
                    continue

            source_id = str(getattr(relationship, "source_id", ""))
            related.append(
                ContextRelatedItem(
                    id=str(entity.id),
                    type=_relationship_value(entity.entity_type),
                    name=str(entity.name),
                    relationship=_relationship_value(relationship.relationship_type),
                    direction="outgoing" if source_id == seed_id else "incoming",
                )
            )
            if len(related) >= limit:
                break
        related_by_seed[str(seed_id)] = related
    return related_by_seed


_ITEM_METADATA_KEYS = (
    "status",
    "priority",
    "complexity",
    "lifecycle_state",
    "review_state",
    "tags",
    "category",
    "language",
    "domain",
    "visibility",
    "kind",
    "capture_mode",
    "capture_surface",
    "thread_id",
    "label",
    "project_id",
    "epic_id",
    "parent_task_id",
    "created_at",
    "updated_at",
    "completed_at",
    "occurred_at",
    "valid_at",
    "learnings",
    "description",
    # Policy and correction gates downstream (synthesis render filters) read
    # these; dropping them silently disables defense-in-depth checks.
    "memory_scope",
    "principal_id",
    "scope_key",
    "redacted",
    "superseded_by_source_id",
    "duplicate_of_source_id",
    "unresolved_claims",
    "supported",
    "claim",
)


def _lean_item_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    """Project full candidate metadata down to fields agents act on.

    Retrieval plumbing (signal scores, candidate policy fields, embedding
    provenance, double-serialized record copies) stays out of packs unless
    the caller asks for an audit view.
    """

    lean: dict[str, Any] = {}
    for key in _ITEM_METADATA_KEYS:
        value = metadata.get(key)
        if value is None or value == "" or value == [] or value == {}:
            continue
        lean[key] = value
    return lean


def _item_from_result(
    result: SearchResult,
    facet: ContextFacet,
    *,
    audit: bool = False,
) -> ContextItem:
    full_metadata = dict(result.metadata)
    metadata = full_metadata if audit else _lean_item_metadata(full_metadata)
    quality = _quality_metadata_from_result(result)
    source = _compact_metadata_value(result.source) or quality.source or result.id
    metadata.setdefault("source_id", source)
    content = result.content
    if facet is ContextFacet.PRIOR_ART:
        learnings = metadata.get("learnings")
        if isinstance(learnings, str) and learnings.strip():
            content = learnings.strip()
    if not audit:
        description = metadata.get("description")
        if isinstance(description, str) and description.strip() == (content or "").strip():
            metadata.pop("description", None)
    kwargs: dict[str, Any] = {
        "id": result.id,
        "type": result.type,
        "name": result.name,
        "content": content,
        "score": result.score,
        "facet": facet,
        "reason": _reason_for(result, facet),
        "source": source,
        "metadata": metadata,
    }
    if "quality" in getattr(ContextItem, "__dataclass_fields__", {}):
        kwargs["quality"] = quality
    else:
        metadata["quality"] = quality
    return ContextItem(**kwargs)


def _item_sort_key(item: ContextItem) -> tuple[int, float]:
    return (0 if item.metadata.get("active_lookup") else 1, -item.score)


_LINEAGE_TYPE_RANK = {
    "decision": 0,
    "plan": 0,
    "idea": 0,
    "claim": 0,
    "rule": 0,
    "guide": 0,
    "pattern": 0,
    "error_pattern": 0,
    "artifact": 0,
    "task": 1,
    "epic": 1,
    "project": 1,
    "procedure": 2,
    "template": 2,
    "tool": 2,
    "document": 2,
    "source": 2,
    "config_file": 2,
}
_LINEAGE_DEFAULT_RANK = 3
_PROCEDURE_NAME_PREFIX = "procedure: "


def _lineage_key(item: ContextItem) -> str:
    name = " ".join((item.name or "").strip().lower().split())
    if name.startswith(_PROCEDURE_NAME_PREFIX):
        name = name[len(_PROCEDURE_NAME_PREFIX) :]
    return name


def _lineage_rank(item: ContextItem) -> tuple[int, float]:
    if item.metadata.get("active_lookup"):
        return (-1, -item.score)
    item_type = (item.type or "").lower()
    if item_type in _WORK_ITEM_TYPES:
        status = str(item.metadata.get("status") or "").lower()
        if status in _IN_FLIGHT_WORK_STATUSES:
            return (-1, -item.score)
    type_rank = _LINEAGE_TYPE_RANK.get(item_type, _LINEAGE_DEFAULT_RANK)
    return (type_rank, -item.score)


def _dedupe_lineage(sections: list[ContextSection]) -> list[ContextSection]:
    """Collapse derivation-lineage duplicates across sections.

    The graph stores the same fact in multiple shapes: a raw memory and the
    decision reflected from it, a task and its auto-generated procedure
    mirror. ID-based dedup can't see these; name-stem grouping keeps the
    most distilled shape (decision over task over procedure over raw).
    """

    winners: dict[str, ContextItem] = {}
    for section in sections:
        for item in section.items:
            key = _lineage_key(item)
            if not key:
                continue
            best = winners.get(key)
            if best is None or _lineage_rank(item) < _lineage_rank(best):
                winners[key] = item

    kept = {id(item) for item in winners.values()}
    deduped: list[ContextSection] = []
    for section in sections:
        items = [item for item in section.items if not _lineage_key(item) or id(item) in kept]
        if items:
            deduped.append(replace(section, items=items))
    return deduped


def _dedupe_sections(sections: list[ContextSection], limit: int) -> list[ContextSection]:
    seen: set[str] = set()
    remaining = limit
    deduped: list[ContextSection] = []

    for section in sections:
        items: list[ContextItem] = []
        for item in sorted(section.items, key=_item_sort_key):
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


def _compact_text(value: str, max_chars: int) -> str:
    compact = " ".join(value.strip().split())
    if len(compact) <= max_chars:
        return compact
    cutoff = compact.rfind(" ", 0, max_chars + 1)
    if cutoff < max_chars // 2:
        cutoff = max_chars
    return compact[:cutoff].rstrip() + "..."


def _quality_value(quality: Any, key: str) -> str | None:
    if isinstance(quality, dict):
        return _compact_metadata_value(quality.get(key))
    return _compact_metadata_value(getattr(quality, key, None))


def context_item_source_id(item: ContextItem) -> str:
    return (
        _compact_metadata_value(item.metadata.get("source_id"))
        or _compact_metadata_value(item.source)
        or item.id
    )


def context_item_project_id(item: ContextItem) -> str | None:
    quality = getattr(item, "quality", item.metadata.get("quality", {}))
    return (
        _quality_value(quality, "project_id")
        or _compact_metadata_value(item.metadata.get("project_id"))
        or _compact_metadata_value(item.metadata.get("project"))
    )


def context_item_freshness(item: ContextItem) -> str | None:
    quality = getattr(item, "quality", item.metadata.get("quality", {}))
    return (
        _quality_value(quality, "valid_at")
        or _quality_value(quality, "updated_at")
        or _quality_value(quality, "created_at")
        or _compact_metadata_value(item.metadata.get("freshness"))
    )


def context_item_lifecycle_state(item: ContextItem) -> str | None:
    return _compact_metadata_value(
        item.metadata.get("lifecycle_state") or item.metadata.get("review_state")
    )


def _date_only(value: str | None) -> str | None:
    if not value:
        return None
    if len(value) >= 10 and value[4:5] == "-" and value[7:8] == "-":
        return value[:10]
    return value


def _quality_metadata_to_markdown(
    quality: Any,
    *,
    item_id: str | None = None,
    pack_project: str | None = None,
) -> str:
    """Render provenance that adds signal, skipping values the pack already states."""

    parts: list[str] = []
    origin = _quality_value(quality, "origin")
    if origin and origin != "graph":
        parts.append(origin)
    source = _quality_value(quality, "source")
    if source and source != item_id:
        parts.append(f"src={source}")
    project_id = _quality_value(quality, "project_id")
    if project_id and project_id != pack_project:
        parts.append(f"project={project_id}")
    if updated_at := _date_only(_quality_value(quality, "updated_at")):
        parts.append(f"updated={updated_at}")
    elif created_at := _date_only(_quality_value(quality, "created_at")):
        parts.append(f"created={created_at}")
    if valid_at := _date_only(_quality_value(quality, "valid_at")):
        parts.append(f"valid={valid_at}")
    if url := _quality_value(quality, "url"):
        parts.append(f"url={url}")
    return "; ".join(parts)


_MARKDOWN_CHARS_PER_TOKEN = 4
_ACTIVE_WORK_ANCHOR_INTENTS = frozenset({ContextIntent.BUILD, ContextIntent.GENERAL})


def _has_active_lookup(section: ContextSection) -> bool:
    return section.facet is ContextFacet.ACTIVE_WORK and any(
        item.metadata.get("active_lookup") for item in section.items
    )


def _section_render_score(section: ContextSection) -> float:
    score = max((item.score for item in section.items), default=0.0)
    if section.facet is ContextFacet.RECENT_MEMORY:
        score += 0.25
    elif _has_active_lookup(section):
        score = max(score, 0.75)
    return score


def _section_render_key(
    section: ContextSection,
    *,
    index: int,
    intent: ContextIntent,
) -> tuple[int, float, int]:
    if intent in _ACTIVE_WORK_ANCHOR_INTENTS and _has_active_lookup(section):
        return (0, 0.0, index)
    return (1, -_section_render_score(section), index)


def _sections_for_markdown(
    sections: Sequence[ContextSection],
    *,
    intent: ContextIntent,
) -> list[ContextSection]:
    return [
        section
        for _key, section in sorted(
            (
                (_section_render_key(section, index=index, intent=intent), section)
                for index, section in enumerate(sections)
            ),
            key=lambda item: item[0],
        )
    ]


def _item_markdown_lines(
    item: ContextItem,
    *,
    pack_project: str | None,
    max_content_chars: int,
    include_related: bool,
) -> list[str]:
    status = _compact_metadata_value(item.metadata.get("status"))
    if item.type and status:
        type_label = f" ({item.type} · {status})"
    elif item.type:
        type_label = f" ({item.type})"
    else:
        type_label = ""
    item_quality = getattr(item, "quality", item.metadata.get("quality", {}))
    quality = _quality_metadata_to_markdown(
        item_quality,
        item_id=item.id,
        pack_project=pack_project,
    )
    quality_label = f" _{quality}_" if quality else ""
    lines = [f"- **{item.name}**{type_label} `{item.id}`{quality_label}"]
    if item.content and item.content.strip() != (item.name or "").strip():
        lines.append(f"  - Memory: {_compact_text(item.content, max_content_chars)}")
    if include_related and item.related:
        related_entries = [
            candidate
            for candidate in item.related
            if not (candidate.relationship == "BELONGS_TO" and candidate.type == "project")
        ]
        if related_entries:
            related = "; ".join(
                f"{candidate.relationship} {candidate.name} ({candidate.type})"
                for candidate in related_entries[:3]
            )
            lines.append(f"  - Related: {related}")
    return lines


def context_pack_to_markdown(
    pack: ContextPack,
    *,
    max_items: int = 8,
    items_per_section: int = 3,
    max_content_chars: int = 280,
    include_related: bool = True,
    token_budget: int | None = None,
) -> str:
    """Render a context pack as compact Markdown for agent injection.

    token_budget caps the rendered size at roughly that many tokens
    (chars/4 estimate); at least one item always renders so a tight
    budget degrades to a minimal brief instead of an empty pack.
    """

    max_items = max(1, min(max_items, 50))
    items_per_section = max(1, min(items_per_section, 10))
    max_content_chars = max(80, min(max_content_chars, 1200))
    char_budget = (
        max(400, token_budget * _MARKDOWN_CHARS_PER_TOKEN) if token_budget is not None else None
    )

    lines = [
        f"# Sibyl Context Pack: {pack.goal}",
        f"Intent: {pack.intent.value}",
        f"Layer: {pack.layer.value}",
        f"Query: {pack.query}",
    ]
    if pack.domain:
        lines.append(f"Domain: {pack.domain}")
    if pack.project:
        lines.append(f"Project: {pack.project}")

    used = sum(len(line) + 1 for line in lines)
    remaining = max_items
    emitted_items = 0
    trimmed = False
    for section in _sections_for_markdown(pack.sections, intent=pack.intent):
        if remaining <= 0 or trimmed:
            break
        section_lines = ["", f"## {section.title}"]
        section_emitted = False
        for item in section.items[:items_per_section]:
            if remaining <= 0:
                break
            item_lines = _item_markdown_lines(
                item,
                pack_project=pack.project,
                max_content_chars=max_content_chars,
                include_related=include_related,
            )
            block = [*section_lines, *item_lines] if not section_emitted else item_lines
            block_chars = sum(len(line) + 1 for line in block)
            if char_budget is not None and emitted_items > 0 and used + block_chars > char_budget:
                trimmed = True
                break
            lines.extend(block)
            used += block_chars
            section_emitted = True
            emitted_items += 1
            remaining -= 1

    if trimmed:
        lines.extend(["", f"_Trimmed to ~{token_budget} tokens; raise --budget for more._"])
    elif pack.usage_hint:
        lines.extend(["", f"_Hint: {pack.usage_hint}_"])

    return "\n".join(lines)


async def _attach_related_items(
    sections: list[ContextSection],
    *,
    organization_id: str,
    accessible_projects: set[str] | None,
    related_limit: int,
    related_fn: RelatedFn,
    related_batch_fn: RelatedBatchFn | None = _default_related_items_batch,
) -> list[ContextSection]:
    related_limit = max(0, min(related_limit, 5))
    if related_limit == 0:
        return sections

    eligible_ids = [
        item.id
        for section in sections
        for item in section.items
        if item.type != "document" and not item.id.startswith("document:")
    ]
    related_by_item_id: dict[str, list[ContextRelatedItem]] = {}
    if related_fn is _default_related_items and related_batch_fn is not None:
        try:
            related_by_item_id = await related_batch_fn(
                entity_ids=eligible_ids,
                organization_id=organization_id,
                accessible_projects=accessible_projects,
                limit=related_limit,
            )
        except Exception:
            related_by_item_id = {}

    enriched_sections: list[ContextSection] = []
    for section in sections:
        items: list[ContextItem] = []
        for item in section.items:
            if item.type == "document" or item.id.startswith("document:"):
                items.append(item)
                continue
            if related_fn is _default_related_items and related_batch_fn is not None:
                items.append(replace(item, related=related_by_item_id.get(item.id, [])))
                continue
            try:
                related = await related_fn(
                    entity_id=item.id,
                    organization_id=organization_id,
                    accessible_projects=accessible_projects,
                    limit=related_limit,
                )
            except Exception:
                related = []
            items.append(replace(item, related=related))
        enriched_sections.append(replace(section, items=items))
    return enriched_sections


async def _compile_fallback_sections(
    *,
    query: str,
    facets: list[ContextFacet],
    domain: str | None,
    project: str | None,
    accessible_projects: set[str] | None,
    organization_id: str,
    limit: int,
    search_fn: SearchFn,
    audit: bool = False,
) -> list[ContextSection]:
    response = await search_fn(
        query=query,
        types=None,
        category=domain,
        project=project,
        accessible_projects=accessible_projects,
        limit=limit,
        include_content=True,
        include_documents=True,
        include_graph=True,
        organization_id=organization_id,
    )

    grouped: dict[ContextFacet, list[ContextItem]] = {facet: [] for facet in facets}
    for result in response.results:
        if _is_synthetic_relationship_result(result):
            continue
        facet = _facet_for_result(result, facets)
        if facet is None:
            continue
        grouped[facet].append(_item_from_result(result, facet, audit=audit))

    return [
        ContextSection(facet=facet, title=FACET_TITLES[facet], items=items)
        for facet in facets
        if (items := grouped[facet])
    ]


def _types_for_facets(facets: Sequence[ContextFacet]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for facet in facets:
        entity_types = FACET_TYPES[facet]
        if facet is ContextFacet.RECENT_MEMORY:
            entity_types = [*entity_types, *_RECENT_MEMORY_FACT_TYPES]
        for entity_type in entity_types:
            normalized = entity_type.lower()
            if normalized in seen:
                continue
            seen.add(normalized)
            ordered.append(entity_type)
    return ordered


def _sections_from_response(
    response: SearchResponse,
    *,
    facets: Sequence[ContextFacet],
    audit: bool = False,
) -> list[ContextSection]:
    grouped: dict[ContextFacet, list[ContextItem]] = {facet: [] for facet in facets}
    for result in response.results:
        if _is_synthetic_relationship_result(result):
            continue
        facet = _facet_for_result(result, list(facets))
        if facet is None:
            continue
        grouped[facet].append(_item_from_result(result, facet, audit=audit))

    return [
        ContextSection(facet=facet, title=FACET_TITLES[facet], items=items)
        for facet in facets
        if (items := grouped[facet])
    ]


_ACTIVE_WORK_LOOKUP_STATUSES = "doing,blocked,review"
_ACTIVE_WORK_LOOKUP_LIMIT = 5


def _enum_value(value: Any) -> str:
    return str(getattr(value, "value", value))


def _item_from_active_entity(entity: Any) -> ContextItem:
    entity_id = str(entity.id)
    status = _enum_value(getattr(entity, "status", "") or "")
    quality = ContextItemQualityMetadata(
        origin="graph",
        source=entity_id,
        created_at=_compact_metadata_value(getattr(entity, "created_at", None)),
        updated_at=_compact_metadata_value(getattr(entity, "updated_at", None)),
        project_id=_compact_metadata_value(getattr(entity, "project_id", None)),
    )
    metadata: dict[str, Any] = {
        "source_id": entity_id,
        "active_lookup": True,
        "status": status,
        "priority": _enum_value(getattr(entity, "priority", "") or ""),
    }
    content = str(getattr(entity, "description", "") or getattr(entity, "content", "") or "")
    reason = "task is currently in progress for this project"
    if status == "blocked":
        reason = "task is currently blocked for this project"
    return ContextItem(
        id=entity_id,
        type=_enum_value(getattr(entity, "entity_type", "task")),
        name=str(entity.name),
        content=content,
        score=0.0,
        facet=ContextFacet.ACTIVE_WORK,
        reason=reason,
        source=entity_id,
        quality=quality,
        metadata=metadata,
    )


def _active_work_project_allowed(
    *,
    project: str,
    accessible_projects: set[str] | None,
    scoped_accessible_projects: frozenset[str] | None,
    allowed_memory_scope_keys: set[str] | None,
) -> bool:
    if scoped_accessible_projects is not None:
        return project in scoped_accessible_projects
    if accessible_projects is not None and project not in accessible_projects:
        return False
    if allowed_memory_scope_keys is not None:
        return memory_scope_policy_key(MemoryScope.PROJECT, project) in allowed_memory_scope_keys
    return True


async def _default_active_work(
    *,
    organization_id: str,
    project: str,
    limit: int,
) -> list[ContextItem]:
    from sibyl_core.models.entities import EntityType

    runtime = await get_graph_runtime(organization_id)
    entities = await runtime.entity_manager.list_by_type(
        EntityType.TASK,
        limit=limit,
        project_id=project,
        status=_ACTIVE_WORK_LOOKUP_STATUSES,
    )
    return [_item_from_active_entity(entity) for entity in entities]


def _merge_active_work(
    sections: list[ContextSection],
    active_items: list[ContextItem],
    facets: Sequence[ContextFacet],
) -> list[ContextSection]:
    if not active_items:
        return sections

    existing_ids = {item.id for item in active_items}
    merged: list[ContextSection] = []
    inserted = False
    facet_positions = {facet: index for index, facet in enumerate(facets)}
    active_index = facet_positions.get(ContextFacet.ACTIVE_WORK, 0)
    for section in sections:
        if section.facet is ContextFacet.ACTIVE_WORK:
            retained = [item for item in section.items if item.id not in existing_ids]
            merged.append(replace(section, items=[*active_items, *retained]))
            inserted = True
            continue
        if not inserted and facet_positions.get(section.facet, len(facet_positions)) > active_index:
            merged.append(
                ContextSection(
                    facet=ContextFacet.ACTIVE_WORK,
                    title=FACET_TITLES[ContextFacet.ACTIVE_WORK],
                    items=list(active_items),
                )
            )
            inserted = True
        merged.append(section)
    if not inserted:
        merged.append(
            ContextSection(
                facet=ContextFacet.ACTIVE_WORK,
                title=FACET_TITLES[ContextFacet.ACTIVE_WORK],
                items=list(active_items),
            )
        )
    return merged


async def _compile_native_sections(
    *,
    plan: Any,
    facets: Sequence[ContextFacet],
    limit: int,
    per_facet_limit: int,
    raw_memory_recall_fn: RawMemoryRecallFn,
    audit: bool = False,
) -> list[ContextSection]:
    search_limit = min(50, max(limit, per_facet_limit * len(facets)))
    response = await context_search(
        plan=plan,
        types=_types_for_facets(facets),
        facet=ContextFacet.RECENT_MEMORY if ContextFacet.RECENT_MEMORY in facets else None,
        limit=search_limit,
        include_content=True,
        embedding_provider=configured_embedding_provider(),
        raw_memory_recall_fn=raw_memory_recall_fn,
    )
    return _sections_from_response(response, facets=facets, audit=audit)


async def compile_context(
    goal: str,
    *,
    intent: str | ContextIntent = ContextIntent.BUILD,
    layer: str | ContextLayer = ContextLayer.RECALL,
    domain: str | None = None,
    project: str | None = None,
    accessible_projects: set[str] | None = None,
    principal_id: str | None = None,
    agent_id: str | None = None,
    organization_id: str | None = None,
    limit: int = 24,
    include_related: bool = False,
    related_limit: int = 3,
    audit: bool = False,
    search_fn: SearchFn = default_search,
    related_fn: RelatedFn = _default_related_items,
    raw_memory_recall_fn: RawMemoryRecallFn = recall_raw_memory,
    active_work_fn: ActiveWorkFn | None = None,
    allowed_memory_scope_keys: set[str] | None = None,
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
    normalized_layer = _coerce_layer(layer)
    query = _query_for(goal, domain)
    limit = max(1, min(limit, LAYER_LIMITS[normalized_layer]))
    facets = _facets_for_layer(normalized_intent, normalized_layer)
    per_facet_limit = max(2, min(8, (limit + len(facets) - 1) // len(facets)))
    plan = build_context_retrieval_plan(
        query=query,
        organization_id=organization_id,
        facets=facets,
        facet_types=FACET_TYPES,
        principal_id=principal_id,
        project=project,
        accessible_projects=accessible_projects,
        agent_id=agent_id,
        limit=limit,
        allowed_memory_scope_keys=allowed_memory_scope_keys,
    )

    sections: list[ContextSection] = []
    retrieval_failed = False
    try:
        sections = await _compile_native_sections(
            plan=plan,
            facets=facets,
            limit=limit,
            per_facet_limit=per_facet_limit,
            raw_memory_recall_fn=raw_memory_recall_fn,
            audit=audit,
        )
    except Exception as exc:
        retrieval_failed = True
        log.warning(
            "context_native_search_failed",
            error_type=type(exc).__name__,
        )

    if (
        ContextFacet.ACTIVE_WORK in facets
        and project
        and _active_work_project_allowed(
            project=project,
            accessible_projects=accessible_projects,
            scoped_accessible_projects=plan.accessible_projects,
            allowed_memory_scope_keys=allowed_memory_scope_keys,
        )
    ):
        lookup = active_work_fn if active_work_fn is not None else _default_active_work
        try:
            active_items = await lookup(
                organization_id=organization_id,
                project=project,
                limit=min(per_facet_limit, _ACTIVE_WORK_LOOKUP_LIMIT),
            )
        except Exception as exc:
            active_items = []
            log.warning(
                "context_active_work_lookup_failed",
                error_type=type(exc).__name__,
            )
        sections = _merge_active_work(sections, active_items, facets)

    sections = _dedupe_lineage(sections)
    sections = _dedupe_sections(sections, limit)
    if not sections and retrieval_failed:
        sections = _dedupe_sections(
            await _compile_fallback_sections(
                query=query,
                facets=facets,
                domain=domain,
                project=project,
                accessible_projects=accessible_projects,
                organization_id=organization_id,
                limit=limit,
                search_fn=search_fn,
                audit=audit,
            ),
            limit,
        )
    if include_related and normalized_layer is not ContextLayer.WAKE:
        related_projects = (
            set(plan.accessible_projects) if plan.accessible_projects is not None else None
        )
        sections = await _attach_related_items(
            sections,
            organization_id=organization_id,
            accessible_projects=related_projects,
            related_limit=related_limit,
            related_fn=related_fn,
        )
    return ContextPack(
        goal=goal,
        intent=normalized_intent,
        layer=normalized_layer,
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
    "context_item_freshness",
    "context_item_lifecycle_state",
    "context_item_project_id",
    "context_item_source_id",
    "context_pack_to_dict",
    "context_pack_to_markdown",
]
