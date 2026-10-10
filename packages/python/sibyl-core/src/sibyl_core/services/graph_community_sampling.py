"""Choosing which graph nodes a level of detail can show within its budget."""

from __future__ import annotations

import heapq
from collections import Counter
from datetime import UTC, datetime

from sibyl_core.models.entities import Entity, Relationship

_GRAPH_DIVERSITY_THRESHOLD = 100
_GRAPH_PRIMARY_SAMPLE_SHARE = 0.8
_GRAPH_MISSING_TYPE_MIN_RESERVE = 5
_GRAPH_HUB_SEED_SHARE = 0.4


def _entity_timestamp(entity: Entity | None) -> datetime:
    if entity is None:
        return datetime.min.replace(tzinfo=UTC)
    return entity.updated_at or entity.created_at or datetime.min.replace(tzinfo=UTC)


def _entity_priority_key(
    entity_id: str,
    entity_by_id: dict[str, Entity],
    degrees: Counter[str],
) -> tuple[int, datetime, str]:
    return (
        degrees.get(entity_id, 0),
        _entity_timestamp(entity_by_id.get(entity_id)),
        entity_id,
    )


def _allocate_diversity_quotas(
    remaining_by_type: dict[str, list[str]],
    *,
    represented_types: set[str],
    budget: int,
) -> dict[str, int]:
    quotas = {entity_type: 0 for entity_type, ids in remaining_by_type.items() if ids}
    if budget <= 0 or not quotas:
        return quotas

    missing_types = [
        entity_type
        for entity_type, ids in remaining_by_type.items()
        if ids and entity_type not in represented_types
    ]
    for entity_type in missing_types:
        if budget <= 0:
            break
        reserve = min(_GRAPH_MISSING_TYPE_MIN_RESERVE, len(remaining_by_type[entity_type]), budget)
        quotas[entity_type] += reserve
        budget -= reserve

    while budget > 0:
        eligible_types = [
            entity_type
            for entity_type, ids in remaining_by_type.items()
            if quotas.get(entity_type, 0) < len(ids)
        ]
        if not eligible_types:
            break
        next_type = max(
            eligible_types,
            key=lambda entity_type: (
                len(remaining_by_type[entity_type]) - quotas.get(entity_type, 0),
                entity_type,
            ),
        )
        quotas[next_type] += 1
        budget -= 1

    return quotas


def _pick_representative_node_ids(
    focused_ids: set[str],
    entity_by_id: dict[str, Entity],
    degrees: Counter[str],
    *,
    max_nodes: int,
) -> list[str]:
    ranked_ids = sorted(
        focused_ids,
        key=lambda entity_id: _entity_priority_key(entity_id, entity_by_id, degrees),
        reverse=True,
    )
    if len(ranked_ids) <= max_nodes or max_nodes < _GRAPH_DIVERSITY_THRESHOLD:
        return ranked_ids[:max_nodes]

    primary_target = max(1, min(len(ranked_ids), int(max_nodes * _GRAPH_PRIMARY_SAMPLE_SHARE)))
    selected_ids = set(ranked_ids[:primary_target])
    remaining_budget = max_nodes - len(selected_ids)
    if remaining_budget <= 0:
        return ranked_ids[:max_nodes]

    represented_types = {
        entity.entity_type.value
        for entity_id in selected_ids
        if (entity := entity_by_id.get(entity_id)) is not None
    }

    remaining_by_type: dict[str, list[str]] = {}
    for entity_id in ranked_ids[primary_target:]:
        entity = entity_by_id.get(entity_id)
        if entity is None:
            continue
        remaining_by_type.setdefault(entity.entity_type.value, []).append(entity_id)

    quotas = _allocate_diversity_quotas(
        remaining_by_type,
        represented_types=represented_types,
        budget=remaining_budget,
    )
    for entity_type, quota in quotas.items():
        if quota <= 0:
            continue
        selected_ids.update(remaining_by_type[entity_type][:quota])

    if len(selected_ids) < max_nodes:
        for entity_id in ranked_ids[primary_target:]:
            if entity_id in selected_ids:
                continue
            selected_ids.add(entity_id)
            if len(selected_ids) >= max_nodes:
                break

    return [entity_id for entity_id in ranked_ids if entity_id in selected_ids][:max_nodes]


def _build_focused_adjacency(
    relationships: list[Relationship],
    focused_ids: set[str],
) -> dict[str, set[str]]:
    """Undirected adjacency among focused nodes, for connectivity-aware sampling."""
    adjacency: dict[str, set[str]] = {}
    for relationship in relationships:
        source = relationship.source_id
        target = relationship.target_id
        if source == target or source not in focused_ids or target not in focused_ids:
            continue
        adjacency.setdefault(source, set()).add(target)
        adjacency.setdefault(target, set()).add(source)
    return adjacency


def _pick_connected_node_ids(
    focused_ids: set[str],
    entity_by_id: dict[str, Entity],
    degrees: Counter[str],
    adjacency: dict[str, set[str]],
    *,
    max_nodes: int,
) -> list[str]:
    """Select up to max_nodes that form a DENSE, connected subgraph.

    The previous selector took the top nodes by degree/recency, which over a
    large graph picks hubs from unrelated neighborhoods whose neighbors fall
    outside the slice — so almost no edge has both endpoints selected and the
    render is a starfield. Instead: seed with the top-degree hubs, then grow by
    repeatedly attaching the highest-degree unselected neighbor of the current
    set, so every added node carries at least one surviving edge.
    """
    # Only nodes that actually connect to something. Isolated singletons (no
    # focused edge) add no signal and render as a starfield halo around the
    # connected core, so they are excluded from the displayed subgraph.
    connected_ids = {entity_id for entity_id in focused_ids if degrees.get(entity_id, 0) > 0}
    ranked_ids = sorted(
        connected_ids,
        key=lambda entity_id: _entity_priority_key(entity_id, entity_by_id, degrees),
        reverse=True,
    )
    if len(ranked_ids) <= max_nodes:
        return ranked_ids

    seed_count = max(1, min(max_nodes, int(max_nodes * _GRAPH_HUB_SEED_SHARE)))
    selected: set[str] = set(ranked_ids[:seed_count])

    queued: set[str] = set(selected)
    frontier: list[tuple[int, str]] = []

    def _enqueue_neighbors(node_id: str) -> None:
        for neighbor in adjacency.get(node_id, ()):
            if neighbor in queued or degrees.get(neighbor, 0) == 0:
                continue
            queued.add(neighbor)
            heapq.heappush(frontier, (-degrees.get(neighbor, 0), neighbor))

    for node_id in selected:
        _enqueue_neighbors(node_id)

    while len(selected) < max_nodes and frontier:
        _, neighbor = heapq.heappop(frontier)
        if neighbor in selected:
            continue
        selected.add(neighbor)
        _enqueue_neighbors(neighbor)

    # Disconnected remainder: spend any leftover budget on a type-diversity
    # reserve (so rare types still appear) then the highest-degree leftovers.
    if len(selected) < max_nodes:
        remaining_by_type: dict[str, list[str]] = {}
        for entity_id in ranked_ids:
            if entity_id in selected:
                continue
            entity = entity_by_id.get(entity_id)
            if entity is None:
                continue
            remaining_by_type.setdefault(entity.entity_type.value, []).append(entity_id)
        represented_types = {
            entity.entity_type.value
            for entity_id in selected
            if (entity := entity_by_id.get(entity_id)) is not None
        }
        quotas = _allocate_diversity_quotas(
            remaining_by_type,
            represented_types=represented_types,
            budget=max_nodes - len(selected),
        )
        for entity_type, quota in quotas.items():
            for entity_id in remaining_by_type[entity_type][:quota]:
                selected.add(entity_id)
        if len(selected) < max_nodes:
            for entity_id in ranked_ids:
                if entity_id in selected:
                    continue
                selected.add(entity_id)
                if len(selected) >= max_nodes:
                    break

    return [entity_id for entity_id in ranked_ids if entity_id in selected][:max_nodes]
