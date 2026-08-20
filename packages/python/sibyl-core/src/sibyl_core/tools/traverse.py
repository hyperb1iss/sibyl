"""Client-exposed bounded traversal verbs.

Retrieval already walks the graph, but only inside one scored pass whose shape
the caller cannot steer. These two verbs hand the steering wheel to the agent
without handing over composition: `expand_neighbors` widens a neighborhood by a
bounded number of hops, `fetch_slice` widens one hit into the span window it was
cut into, and the deterministic evidence composer still renders the answer.

Both verbs are stateless and bounded per call. The round budget lives in the
tool docstrings the agent reads, because there is no session to enforce it in
and a limit an agent cannot see is not a limit.

Every row either verb returns passes the same reader authorization the scored
lanes apply, per row rather than once per seed: project membership is not
permission to read a private memory that happens to sit in that project, and a
traversal that skipped the row check would be a way to reach one by walking to
it.

The check gates the walk and not merely its output. Only authorized rows join
the next frontier, so an unreadable memory is not a route either, and a reader
cannot infer that something sits between two rows by receiving the far one.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

from sibyl_core.models.entities import EntityType, RelationshipType
from sibyl_core.projection.passages import (
    MAX_PASSAGE_CONTENT_CHARS,
    MAX_PASSAGES_PER_SOURCE,
    PASSAGE_COVERS_PARENT_KEY,
    spans_cover_parent,
)
from sibyl_core.retrieval._search_expansion import expand_neighbor_records
from sibyl_core.retrieval._search_plan import DEFAULT_CANDIDATES_PER_SIGNAL
from sibyl_core.retrieval.operational_sources import PASSAGE_WINDOW_UNITS
from sibyl_core.tools.helpers import ScopeGuard, memory_scope_guard
from sibyl_core.tools.responses import (
    ExpandNeighborsResponse,
    FetchSliceResponse,
    NeighborEntity,
    SlicePassage,
)
from sibyl_core.tools.search import (
    DEFAULT_SEARCH_CONTENT_MAX_CHARS,
    MAX_SEARCH_CONTENT_MAX_CHARS,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from sibyl_core.models.entities import Entity

log = structlog.get_logger()

# Two retrieval iterations capture most of the gain of five, so a walk that can
# reach three hops can already overshoot what the measurement supports. The
# ceiling matches the depth clamp graph browsing has always used.
MAX_TRAVERSAL_DEPTH = 3
DEFAULT_TRAVERSAL_DEPTH = 1

# A traversal step is one lane of the retrieval budget, so it seeds from at most
# what a lane contributes and yields at most one lane's worth per hop it may
# take. Both numbers are the scored budget rather than a new one, which is what
# keeps an agent-steered walk from outspending the pass it is refining.
MAX_EXPAND_ORIGINS = DEFAULT_CANDIDATES_PER_SIGNAL
DEFAULT_EXPAND_LIMIT = DEFAULT_CANDIDATES_PER_SIGNAL
MAX_EXPAND_LIMIT = DEFAULT_CANDIDATES_PER_SIGNAL * MAX_TRAVERSAL_DEPTH

# Neighbors are a scan surface: the caller reads names and relationships to
# decide where to widen, so previews are sized exactly like search previews and
# share their ceiling.
DEFAULT_NEIGHBOR_CONTENT_MAX_CHARS = DEFAULT_SEARCH_CONTENT_MAX_CHARS
MAX_TRAVERSAL_CONTENT_MAX_CHARS = MAX_SEARCH_CONTENT_MAX_CHARS
# Spans are the read surface rather than a scan surface, so a window's whole
# budget is one worst-case span's ceiling.
DEFAULT_SLICE_CONTENT_MAX_CHARS = MAX_PASSAGE_CONTENT_CHARS

DEFAULT_SLICE_WINDOW = PASSAGE_WINDOW_UNITS
MAX_SLICE_WINDOW = MAX_PASSAGES_PER_SOURCE

PASSAGE_ENTITY_TYPE = EntityType.PASSAGE.value

# Both passage projections point a span at the memory it was cut from, but they
# do not agree on the edge: prose memories write PART_OF, operational evidence
# writes DERIVED_FROM. Asking for both is what makes one verb serve either.
_PASSAGE_PARENT_RELATIONSHIPS = (RelationshipType.PART_OF, RelationshipType.DERIVED_FROM)

_TRAVERSAL_ROUND_BUDGET = 3


async def get_graph_runtime(group_id: str) -> Any:
    from sibyl_core.services.graph import get_surreal_graph_runtime

    return await get_surreal_graph_runtime(group_id)


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(int(value), high))


def _entity_project_id(entity: Any) -> str | None:
    metadata = getattr(entity, "metadata", None) or {}
    value = getattr(entity, "project_id", None) or metadata.get("project_id")
    return str(value) if value else None


def _coerce_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    return None


def _int_metadata(metadata: Mapping[str, Any], key: str) -> int | None:
    return _coerce_int(metadata.get(key))


def _coerce_float(value: object) -> float:
    if isinstance(value, int | float) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return 0.0
    return 0.0


def _passage_total(metadata: Mapping[str, Any]) -> int | None:
    # The prose projection stamps passage_total, the operational one stamps
    # passage_count. Same number, two writers.
    return _int_metadata(metadata, "passage_total") or _int_metadata(metadata, "passage_count")


def _compact(value: str, max_chars: int) -> tuple[str, bool]:
    """Bound one field's characters, reporting whether anything was dropped."""
    text = value or ""
    if max_chars <= 0:
        return "", bool(text)
    if len(text) <= max_chars:
        return text, False
    cutoff = text.rfind(" ", 0, max_chars + 1)
    if cutoff < max_chars // 2:
        cutoff = max_chars
    return text[:cutoff].rstrip() + "...", True


def _row_scope_entity(row: Mapping[str, object]) -> Entity:
    """Parse a Surreal entity row, keeping the scope the column carries.

    ``entity_from_surreal_row`` promotes denormalized columns into metadata but
    not ``memory_scope``, and the read check treats an absent scope as
    unscoped. The two representations are written in lockstep and agree on every
    row in the live store today, so this overlay is a guard against a future
    write that sets only the column rather than a repair of a known divergence:
    it can only add a scope where metadata had none, never relax one.
    """
    from sibyl_core.services.graph import entity_from_surreal_row

    entity = entity_from_surreal_row(row)
    column_scope = row.get("memory_scope")
    if not entity.metadata.get("memory_scope") and column_scope:
        entity.metadata["memory_scope"] = str(column_scope)
    return entity


async def expand_neighbors(
    entity_ids: Sequence[str],
    *,
    organization_id: str,
    relationship_types: Sequence[str] | None = None,
    depth: int = DEFAULT_TRAVERSAL_DEPTH,
    limit: int = DEFAULT_EXPAND_LIMIT,
    content_max_chars: int = DEFAULT_NEIGHBOR_CONTENT_MAX_CHARS,
    include_incoming: bool = True,
    types: Sequence[str] | None = None,
    principal_id: str | None = None,
    accessible_projects: set[str] | None = None,
    allowed_memory_scope_keys: set[str] | None = None,
    enforce_memory_scope: bool = True,
) -> ExpandNeighborsResponse:
    """Widen a set of known memories into their bounded graph neighborhood.

    ROUND BUDGET: this is step two of at most three. Round one is `search` or
    `context`, round two widens with `expand_neighbors` or `fetch_slice`, round
    three widens once more. A question answerable from one hop should skip this
    verb entirely and read `context`, which composes the answer for you.

    Composition is not this verb's job. It returns previews and adjacency so you
    can decide what to gather; `context` still renders the evidence.

    Args:
        entity_ids: Seed entity IDs, capped at MAX_EXPAND_ORIGINS. Ids that
            resolve to nothing you may read come back in `unresolved`.
        relationship_types: Restrict hops to these relationship names, e.g.
            DEPENDS_ON, PART_OF, SUPERSEDES. Empty walks every relationship.
        depth: Hops to walk, clamped to 1-MAX_TRAVERSAL_DEPTH.
        limit: Neighbors to return, clamped to 1-MAX_EXPAND_LIMIT.
        content_max_chars: Preview characters per neighbor.
        include_incoming: Follow edges that point at the seeds as well as away
            from them. Inbound edges are how dependents and spans are reached.
        types: Restrict the neighbors returned to these entity types. The walk
            still routes through other types, so a two-hop neighbor of the
            requested type is reachable through one that is not.
        principal_id: Reader identity used to authorize scoped rows.
        accessible_projects: Projects this reader may read, or None when the
            caller could not resolve memberships (which denies project rows).
        allowed_memory_scope_keys: API-key memory-space grants, when the caller
            is a restricted credential.
        enforce_memory_scope: Apply memory-scope filtering. Only an operator
            tool dumping its own namespace turns this off.

    Returns:
        ExpandNeighborsResponse with hop-tagged neighbors, highest path score
        first, plus the seeds that resolved and the ones that did not.
    """
    if not organization_id:
        raise ValueError("organization_id is required - cannot traverse without org context")

    depth = _clamp(depth, 1, MAX_TRAVERSAL_DEPTH)
    limit = _clamp(limit, 1, MAX_EXPAND_LIMIT)
    content_max_chars = _clamp(content_max_chars, 0, MAX_TRAVERSAL_CONTENT_MAX_CHARS)
    requested_ids = list(dict.fromkeys(str(value) for value in entity_ids if str(value).strip()))
    seed_ids = requested_ids[:MAX_EXPAND_ORIGINS]
    dropped_ids = requested_ids[MAX_EXPAND_ORIGINS:]
    wanted_relationships = [
        str(name).upper() for name in (relationship_types or ()) if str(name).strip()
    ]
    node_types = tuple(str(value).lower() for value in (types or ()) if str(value).strip())

    filters: dict[str, Any] = {
        "depth": depth,
        "limit": limit,
        "include_incoming": include_incoming,
        "round_budget": _TRAVERSAL_ROUND_BUDGET,
    }
    if wanted_relationships:
        filters["relationship_types"] = wanted_relationships
    if node_types:
        filters["types"] = list(node_types)
    if dropped_ids:
        # Silently walking a prefix would make the response look complete.
        filters["origin_limit"] = MAX_EXPAND_ORIGINS

    if not seed_ids:
        return ExpandNeighborsResponse(
            origins=[],
            neighbors=[],
            total=0,
            depth=depth,
            limit=limit,
            unresolved=dropped_ids,
            filters=filters,
        )

    scope_guard = memory_scope_guard(
        principal_id=principal_id,
        accessible_projects=accessible_projects,
        allowed_memory_scope_keys=allowed_memory_scope_keys,
        enforce_memory_scope=enforce_memory_scope,
        surface="expand_neighbors",
    )

    runtime = await get_graph_runtime(organization_id)
    seeds = await runtime.entity_manager.get_many(seed_ids)
    authorized_seeds = [seed for seed in seeds if _seed_visible(seed, scope_guard)]
    origins = [seed.id for seed in authorized_seeds]
    # A seed the reader may not see and a seed that does not exist are reported
    # the same way on purpose: telling them apart confirms the existence of a row
    # the reader has no right to know about.
    unresolved = [entity_id for entity_id in seed_ids if entity_id not in set(origins)]
    unresolved.extend(dropped_ids)

    if not origins:
        return ExpandNeighborsResponse(
            origins=[],
            neighbors=[],
            total=0,
            depth=depth,
            limit=limit,
            unresolved=unresolved,
            filters=filters,
        )

    # One parse per row, reused by the authorization check and the response, so a
    # row is never read as two slightly different entities.
    parsed: dict[int, Entity] = {}

    def row_allowed(row: Mapping[str, object]) -> bool:
        entity = _row_scope_entity(row)
        if not scope_guard(entity):
            return False
        parsed[id(row)] = entity
        return True

    def row_included(row: Mapping[str, object]) -> bool:
        if not node_types:
            return True
        entity = parsed.get(id(row))
        return entity is not None and entity.entity_type.value.lower() in node_types

    rows = await expand_neighbor_records(
        client=runtime.client,
        origin_uuids=origins,
        group_id=organization_id,
        max_depth=depth,
        # One past the cap, so "there were more" is observed rather than assumed.
        limit=limit + 1,
        relationship_names=wanted_relationships,
        include_incoming=include_incoming,
        # Deliberately not a SearchFilter(node_types=...): that lands in the
        # hydration WHERE, so a row of the wrong type is never read and therefore
        # never walked through. A type restriction narrows the answer, not the
        # routes, or asking for decisions would hide every decision that is only
        # reachable through a task.
        row_allowed=row_allowed,
        row_included=row_included,
    )
    truncated = len(rows) > limit
    neighbors = [
        _neighbor_from_row(row, parsed[id(row)], content_max_chars=content_max_chars)
        for row in rows[:limit]
    ]

    log.info(
        "expand_neighbors",
        origins=len(origins),
        neighbors=len(neighbors),
        depth=depth,
        include_incoming=include_incoming,
        relationship_types=wanted_relationships or None,
    )
    return ExpandNeighborsResponse(
        origins=origins,
        neighbors=neighbors,
        total=len(neighbors),
        depth=depth,
        limit=limit,
        unresolved=unresolved,
        truncated=truncated,
        filters=filters,
    )


def _seed_visible(seed: Any, scope_guard: ScopeGuard) -> bool:
    return seed is not None and scope_guard(seed)


def _neighbor_from_row(
    row: Mapping[str, object],
    entity: Entity,
    *,
    content_max_chars: int,
) -> NeighborEntity:
    relationship = str(row.get("graph_expansion_relationship") or RelationshipType.RELATED_TO.value)
    direction = "incoming" if row.get("graph_expansion_direction") == "incoming" else "outgoing"
    distance = _coerce_int(row.get("graph_expansion_depth"))
    content, content_truncated = _compact(
        entity.content or entity.description or "",
        content_max_chars,
    )
    metadata: dict[str, Any] = {"content_truncated": content_truncated}
    if community_id := row.get("graph_expansion_community_id"):
        metadata["community_id"] = str(community_id)
    passage_index = _int_metadata(entity.metadata, "passage_index")
    if passage_index is not None:
        # A span neighbor is only useful next to its siblings, so name the
        # widening move rather than making the caller infer it.
        metadata["passage_index"] = passage_index
        metadata["parent_entity_id"] = entity.metadata.get("parent_entity_id")
        metadata["widen_with"] = "fetch_slice"
    return NeighborEntity(
        id=entity.id,
        type=entity.entity_type.value,
        name=entity.name,
        relationship=relationship,
        direction=direction,
        distance=distance if distance is not None else 1,
        score=_coerce_float(row.get("graph_expansion_score") or row.get("score")),
        content=content,
        project_id=_entity_project_id(entity),
        metadata=metadata,
    )


async def fetch_slice(
    entity_id: str,
    *,
    organization_id: str,
    window: int = DEFAULT_SLICE_WINDOW,
    content_max_chars: int = DEFAULT_SLICE_CONTENT_MAX_CHARS,
    principal_id: str | None = None,
    accessible_projects: set[str] | None = None,
    allowed_memory_scope_keys: set[str] | None = None,
    enforce_memory_scope: bool = True,
) -> FetchSliceResponse:
    """Read one memory at span granularity, centered on the span you name.

    ROUND BUDGET: this is step two of at most three, and it is the widening move
    for a hit that is a span rather than a whole memory. Cite the parent memory
    the response names, never the span id: the parent is what a reader can
    resolve later, and spans are re-cut when their memory is edited.

    Accepts either end of the relationship. Given a span, the window is centered
    on it. Given a memory, the window starts at its first span. A memory short
    enough never to have been cut comes back whole with `sliced=False`, which is
    the answer, not a failure.

    Composition is not this verb's job either: it hands back spans, and `context`
    still renders the evidence.

    Args:
        entity_id: A passage entity ID or the ID of the memory it was cut from.
        window: Adjacent spans to return, clamped to 1-MAX_SLICE_WINDOW. The
            default is the measured 3-adjacent window.
        content_max_chars: Character budget for the whole window, spent in span
            order. The span that exhausts it is truncated and says so.
        principal_id: Reader identity used to authorize scoped rows.
        accessible_projects: Projects this reader may read, or None when the
            caller could not resolve memberships (which denies project rows).
        allowed_memory_scope_keys: API-key memory-space grants, when the caller
            is a restricted credential.
        enforce_memory_scope: Apply memory-scope filtering. Only an operator
            tool dumping its own namespace turns this off.

    Returns:
        FetchSliceResponse with the ordered window and the parent metadata a
        citation resolves to.

    Raises:
        KeyError: The id does not resolve to a memory this reader may read.
    """
    if not organization_id:
        raise ValueError("organization_id is required - cannot traverse without org context")
    if not entity_id or not str(entity_id).strip():
        raise ValueError("entity_id is required")

    entity_id = str(entity_id).strip()
    window = _clamp(window, 1, MAX_SLICE_WINDOW)
    content_max_chars = _clamp(content_max_chars, 0, MAX_TRAVERSAL_CONTENT_MAX_CHARS)

    scope_guard = memory_scope_guard(
        principal_id=principal_id,
        accessible_projects=accessible_projects,
        allowed_memory_scope_keys=allowed_memory_scope_keys,
        enforce_memory_scope=enforce_memory_scope,
        surface="fetch_slice",
    )

    runtime = await get_graph_runtime(organization_id)
    requested = await _load_authorized(runtime.entity_manager, entity_id, scope_guard)
    if requested is None:
        # Same answer for absent and unauthorized, for the same reason the
        # expansion verb conflates them.
        raise KeyError(entity_id)

    anchor_index: int | None = None
    parent = requested
    if requested.entity_type.value == PASSAGE_ENTITY_TYPE:
        anchor_index = _int_metadata(requested.metadata, "passage_index")
        parent_id = str(requested.metadata.get("parent_entity_id") or "")
        resolved_parent = (
            await _load_authorized(runtime.entity_manager, parent_id, scope_guard)
            if parent_id
            else None
        )
        if resolved_parent is None:
            # A span is never more readable than the memory it was cut from, so an
            # unreadable or missing parent denies the span too.
            #
            # This is the invariant and not merely a precaution. A span inherits
            # its parent's scope once, at write time, and the reprojection that
            # would refresh it runs only when the body changes: a scope-only edit
            # tightening a memory to private leaves its spans carrying the old
            # scope. Trusting the span's own stamp here would serve that memory's
            # text through the span door while the parent correctly refused.
            # Authorizing the parent makes the stale stamp unreachable rather than
            # merely unlikely.
            raise KeyError(entity_id)
        parent = resolved_parent

    spans = await _authorized_spans(runtime, parent_id=parent.id, scope_guard=scope_guard)
    if not spans:
        return _unsliced_response(
            parent,
            requested_id=entity_id,
            window=window,
            content_max_chars=content_max_chars,
        )

    start = _window_start(spans, anchor_index=anchor_index, window=window)
    if start is None:
        # The named span exists and was authorized, but its parent's span set does
        # not contain it: the discovery read hit its own ceiling, or the span
        # belongs to a retired generation. Serve the span alone rather than a
        # window that quietly omits it.
        return _single_span_response(
            requested,
            parent=parent,
            window=window,
            content_max_chars=content_max_chars,
            anchor_index=anchor_index,
        )
    selected = spans[start : start + window]
    passage_total = _passage_total(spans[0].metadata) or len(spans)
    # A window is a subset by design, so the per-span flag alone would report a
    # 3-of-4 window as covering the parent and invite the reader to drop a quarter
    # of the body. Coverage is a property of the exact set returned.
    covers_parent = spans_cover_parent(
        [
            (
                _int_metadata(span.metadata, "passage_index"),
                _passage_total(span.metadata),
                bool(span.metadata.get(PASSAGE_COVERS_PARENT_KEY)),
            )
            for span in selected
        ]
    )

    passages: list[SlicePassage] = []
    remaining = content_max_chars
    for span in selected:
        content, truncated = _compact(span.content or "", remaining)
        remaining = max(remaining - len(content), 0)
        passages.append(
            SlicePassage(
                id=span.id,
                name=span.name,
                content=content,
                passage_index=_int_metadata(span.metadata, "passage_index"),
                passage_total=_passage_total(span.metadata),
                breadcrumb=(
                    str(span.metadata.get("passage_breadcrumb"))
                    if span.metadata.get("passage_breadcrumb")
                    else None
                ),
                truncated=truncated,
            )
        )

    window_start = _int_metadata(selected[0].metadata, "passage_index") if selected else None
    log.info(
        "fetch_slice",
        entity_id=entity_id,
        parent_id=parent.id,
        spans=len(passages),
        window=window,
        sliced=True,
    )
    return FetchSliceResponse(
        entity_id=entity_id,
        parent_id=parent.id,
        parent_name=parent.name,
        parent_type=parent.entity_type.value,
        passages=passages,
        window=window,
        sliced=True,
        total=len(passages),
        window_start=window_start,
        passage_total=passage_total,
        covers_parent=covers_parent,
        project_id=_entity_project_id(parent),
        content_chars=sum(len(passage.content) for passage in passages),
        filters={"window": window, "round_budget": _TRAVERSAL_ROUND_BUDGET},
    )


async def _load_authorized(
    entity_manager: Any,
    entity_id: str,
    scope_guard: ScopeGuard,
) -> Entity | None:
    if not entity_id:
        return None
    try:
        entity = await entity_manager.get(entity_id)
    except Exception:
        return None
    if entity is None or not scope_guard(entity):
        return None
    return entity


async def _authorized_spans(
    runtime: Any,
    *,
    parent_id: str,
    scope_guard: ScopeGuard,
) -> list[Entity]:
    """The spans cut from one memory, ordered by position, reader-authorized.

    Discovery goes through the edge rather than the deterministic span id: the
    two projections mint ids differently but both write an edge from span to
    memory, so the edge is the part that holds for either.
    """
    related = await runtime.relationship_manager.get_related_entities(
        entity_id=parent_id,
        relationship_types=list(_PASSAGE_PARENT_RELATIONSHIPS),
        limit=MAX_PASSAGES_PER_SOURCE,
    )
    span_ids: list[str] = []
    for entity, relationship in related:
        if relationship.target_id != parent_id or entity.entity_type.value != PASSAGE_ENTITY_TYPE:
            continue
        if _int_metadata(entity.metadata, "passage_index") is None:
            continue
        span_ids.append(entity.id)
    if not span_ids:
        return []

    # The related-entity projection omits content, so the spans are re-read whole
    # once their ids are known.
    spans = await runtime.entity_manager.get_many(span_ids)
    authorized = [span for span in spans if scope_guard(span)]
    authorized.sort(key=lambda span: _int_metadata(span.metadata, "passage_index") or 0)
    return authorized


def _window_start(spans: Sequence[Entity], *, anchor_index: int | None, window: int) -> int | None:
    """Center the window on the anchor, sliding it inside the available spans.

    Returns None when the anchor is not among the spans, which the caller turns
    into a single-span response rather than a window. Falling back to index zero
    would serve a window that silently excludes the very span the caller named,
    and a caller reading a coherent-looking window has no way to notice.
    """
    if anchor_index is None:
        return 0
    positions = [_int_metadata(span.metadata, "passage_index") for span in spans]
    if anchor_index not in positions:
        return None
    anchor_position = positions.index(anchor_index)
    start = anchor_position - (window - 1) // 2
    return max(0, min(start, max(len(spans) - window, 0)))


def _single_span_response(
    span: Entity,
    *,
    parent: Entity,
    window: int,
    content_max_chars: int,
    anchor_index: int | None,
) -> FetchSliceResponse:
    """Serve one authorized span whose position its parent's span set does not hold.

    Only reachable once the parent itself is authorized, so this is a
    completeness gap rather than a way around the parent check.
    """
    content, truncated = _compact(span.content or "", content_max_chars)
    return FetchSliceResponse(
        entity_id=span.id,
        parent_id=parent.id,
        parent_name=parent.name,
        parent_type=parent.entity_type.value,
        passages=[
            SlicePassage(
                id=span.id,
                name=span.name,
                content=content,
                passage_index=anchor_index,
                passage_total=_passage_total(span.metadata),
                truncated=truncated,
            )
        ],
        window=window,
        sliced=True,
        total=1,
        window_start=anchor_index,
        passage_total=_passage_total(span.metadata),
        covers_parent=False,
        project_id=_entity_project_id(parent),
        content_chars=len(content),
        filters={
            "window": window,
            "round_budget": _TRAVERSAL_ROUND_BUDGET,
            "anchor_outside_span_set": True,
        },
    )


def _unsliced_response(
    parent: Entity,
    *,
    requested_id: str,
    window: int,
    content_max_chars: int,
) -> FetchSliceResponse:
    """Serve a memory that was never cut as its own single span."""
    content, truncated = _compact(parent.content or parent.description or "", content_max_chars)
    return FetchSliceResponse(
        entity_id=requested_id,
        parent_id=parent.id,
        parent_name=parent.name,
        parent_type=parent.entity_type.value,
        passages=[
            SlicePassage(
                id=parent.id,
                name=parent.name,
                content=content,
                truncated=truncated,
            )
        ],
        window=window,
        sliced=False,
        total=1,
        passage_total=None,
        covers_parent=True,
        project_id=_entity_project_id(parent),
        content_chars=len(content),
        filters={"window": window, "round_budget": _TRAVERSAL_ROUND_BUDGET},
    )


__all__ = [
    "DEFAULT_EXPAND_LIMIT",
    "DEFAULT_NEIGHBOR_CONTENT_MAX_CHARS",
    "DEFAULT_SLICE_CONTENT_MAX_CHARS",
    "DEFAULT_SLICE_WINDOW",
    "DEFAULT_TRAVERSAL_DEPTH",
    "MAX_EXPAND_LIMIT",
    "MAX_EXPAND_ORIGINS",
    "MAX_SLICE_WINDOW",
    "MAX_TRAVERSAL_CONTENT_MAX_CHARS",
    "MAX_TRAVERSAL_DEPTH",
    "expand_neighbors",
    "fetch_slice",
]
