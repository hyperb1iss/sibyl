"""Authorized graph expansion for native retrieval and traversal."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from sibyl_core.models.relations import (
    PREDICATE_EXPANSION_PATH_SCORES,
    predicate_direction_allows,
    predicate_policy,
)
from sibyl_core.retrieval._search_candidates import (
    _candidate_from_node_record,
    _record_score,
    _string_value,
)
from sibyl_core.retrieval._search_database import _execute_query_records
from sibyl_core.retrieval._search_plan import RetrievalPlan, RetrievalSignal, SearchFilter
from sibyl_core.retrieval._search_sources import _node_filter_clause, _where_clause
from sibyl_core.retrieval.candidates import RetrievalCandidate

_GRAPH_EXPANSION_RELATIONSHIP_WEIGHTS = PREDICATE_EXPANSION_PATH_SCORES
_SUPERSEDES_PREDICATE = "SUPERSEDES"
_GRAPH_EXPANSION_DEPTH_DECAY = 0.72
_GRAPH_EXPANSION_FETCH_HEADROOM = 4


@dataclass(frozen=True, slots=True)
class _GraphExpansionHop:
    uuid: str
    depth: int
    relationship: str
    score: float
    community_id: str | None = None
    direction: str = "outgoing"


async def _graph_expansion_candidates(
    *,
    client: Any,
    plan: RetrievalPlan,
    search_filter: SearchFilter,
    seed_candidates: Sequence[RetrievalCandidate],
    limit: int,
) -> list[RetrievalCandidate]:
    entity_seed_uuids = [
        candidate.id
        for candidate in seed_candidates
        if candidate.type not in {"claim", "relationship", "raw_memory", "episode"}
    ][:limit]
    episode_seed_uuids = [
        candidate.id for candidate in seed_candidates if candidate.type == "episode"
    ][:limit]
    if not entity_seed_uuids and not episode_seed_uuids:
        return []

    rows = await _node_bfs_records(
        client=client,
        origin_uuids=entity_seed_uuids,
        episode_origin_uuids=episode_seed_uuids,
        search_filter=search_filter,
        group_id=plan.organization_id,
        max_depth=plan.graph_expansion_depth,
        limit=limit,
    )
    return [
        _candidate_from_node_record(
            row,
            signal=RetrievalSignal.GRAPH_EXPANSION,
            score=_record_score(row),
        )
        for row in rows
    ]


async def _node_bfs_records(
    *,
    client: Any,
    origin_uuids: Sequence[str],
    episode_origin_uuids: Sequence[str] = (),
    search_filter: SearchFilter,
    group_id: str,
    max_depth: int,
    limit: int,
    relationship_names: Sequence[str] = (),
    include_incoming: bool = False,
    include_community_hops: bool = True,
) -> list[dict[str, object]]:
    if (not origin_uuids and not episode_origin_uuids) or max_depth < 1:
        return []

    wanted = {str(name).upper() for name in relationship_names if str(name).strip()}
    discovered: list[_GraphExpansionHop] = []
    seen_discovered: set[str] = set()
    visited_entities = set(origin_uuids)
    entity_frontier = _dedupe_strings(origin_uuids)
    episode_frontier = _dedupe_strings(episode_origin_uuids)

    for depth in range(1, max_depth + 1):
        next_entities: list[str] = []
        remaining = max(int(limit) - len(discovered), 0)
        if remaining <= 0:
            break
        fetch_limit = _graph_expansion_fetch_limit(remaining)
        next_hops: list[_GraphExpansionHop] = []
        if depth == 1 and _hop_relationship_wanted("MENTIONS", wanted):
            next_hops.extend(
                await _mentioned_entity_hops(
                    client=client,
                    episode_uuids=episode_frontier,
                    group_id=group_id,
                    depth=depth,
                    limit=fetch_limit,
                )
            )
        next_hops.extend(
            await _relation_target_hops(
                client=client,
                source_uuids=entity_frontier,
                group_id=group_id,
                depth=depth,
                limit=fetch_limit,
                relationship_names=sorted(wanted),
                exclude_relationship_names=(
                    () if _SUPERSEDES_PREDICATE in wanted else (_SUPERSEDES_PREDICATE,)
                ),
            )
        )
        if include_incoming:
            next_hops.extend(
                await _relation_source_hops(
                    client=client,
                    target_uuids=entity_frontier,
                    group_id=group_id,
                    depth=depth,
                    limit=fetch_limit,
                    relationship_names=sorted(wanted),
                )
            )
        if (
            depth == 1
            and include_community_hops
            and _hop_relationship_wanted("SHARES_COMMUNITY", wanted)
        ):
            next_hops.extend(
                await _community_member_hops(
                    client=client,
                    source_uuids=entity_frontier,
                    group_id=group_id,
                    depth=depth,
                    limit=fetch_limit,
                )
            )
        next_entities.extend(hop.uuid for hop in next_hops)

        for hop in sorted(
            _dedupe_expansion_hops(next_hops),
            key=lambda value: value.score,
            reverse=True,
        ):
            if hop.uuid in seen_discovered:
                continue
            seen_discovered.add(hop.uuid)
            discovered.append(hop)
            if len(discovered) >= limit:
                return await _hydrate_graph_expansion_records(
                    client=client,
                    hops=discovered,
                    search_filter=search_filter,
                    group_id=group_id,
                    limit=limit,
                )

        entity_frontier = [
            uuid for uuid in _dedupe_strings(next_entities) if uuid not in visited_entities
        ]
        visited_entities.update(entity_frontier)
        if not entity_frontier:
            break

    return await _hydrate_graph_expansion_records(
        client=client,
        hops=discovered,
        search_filter=search_filter,
        group_id=group_id,
        limit=limit,
    )


async def _mentioned_entity_hops(
    *,
    client: Any,
    episode_uuids: Sequence[str],
    group_id: str,
    depth: int,
    limit: int,
) -> list[_GraphExpansionHop]:
    if not episode_uuids:
        return []
    rows = await _execute_query_records(
        client,
        """
        SELECT target_id AS uuid
        FROM mentions
        WHERE source_id IN $episode_uuids
          AND group_id = $group_id
          AND out.group_id = $group_id
        LIMIT $limit;
        """,
        episode_uuids=list(episode_uuids),
        group_id=group_id,
        limit=max(int(limit), 1),
    )
    return [
        _GraphExpansionHop(
            uuid=uuid,
            depth=depth,
            relationship="MENTIONS",
            score=_graph_expansion_path_score("MENTIONS", depth=depth),
        )
        for uuid in _dedupe_strings(_record_uuids(rows))
    ]


def _hop_relationship_wanted(relationship: str, wanted: set[str]) -> bool:
    """Whether a synthesized hop label survives the caller's relationship filter.

    ``MENTIONS`` and ``SHARES_COMMUNITY`` are labels the expanders mint rather
    than edge names a query can filter on, so the filter is applied by skipping
    the expander instead of narrowing its ``WHERE``.
    """
    return not wanted or relationship in wanted


async def _relation_target_hops(
    *,
    client: Any,
    source_uuids: Sequence[str],
    group_id: str,
    depth: int,
    limit: int,
    relationship_names: Sequence[str] = (),
    exclude_relationship_names: Sequence[str] = (),
) -> list[_GraphExpansionHop]:
    """Walk edges that point away from the frontier.

    `exclude_relationship_names` exists for predicates whose written direction
    makes the outgoing endpoint the wrong answer. A SUPERSEDES edge is stored
    new-row to old-row, so walking it outwards lands on the row the writer
    declared replaced, and the weight table would then score that stale row at
    0.95 -- a supersession would raise its own victim.
    """
    if not source_uuids:
        return []
    name_clause = "AND name IN $relationship_names" if relationship_names else ""
    exclude_clause = (
        "AND name NOT IN $exclude_relationship_names" if exclude_relationship_names else ""
    )
    rows = await _execute_query_records(
        client,
        f"""
        SELECT target_id AS uuid, name AS relationship
        FROM relates_to
        WHERE source_id IN $source_uuids
          AND group_id = $group_id
          AND out.group_id = $group_id
          {name_clause}
          {exclude_clause}
        LIMIT $limit;
        """,
        source_uuids=list(source_uuids),
        group_id=group_id,
        limit=max(int(limit), 1),
        **({"relationship_names": list(relationship_names)} if relationship_names else {}),
        **(
            {"exclude_relationship_names": list(exclude_relationship_names)}
            if exclude_relationship_names
            else {}
        ),
    )
    hops: list[_GraphExpansionHop] = []
    for row in rows:
        uuid = _string_value(row.get("uuid"))
        if not uuid:
            continue
        relationship = _string_value(row.get("relationship")) or "RELATED_TO"
        if not predicate_direction_allows(relationship, "outgoing"):
            continue
        hops.append(
            _GraphExpansionHop(
                uuid=uuid,
                depth=depth,
                relationship=relationship,
                score=_graph_expansion_path_score(relationship, depth=depth),
            )
        )
    return _dedupe_expansion_hops(hops)


async def _relation_source_hops(
    *,
    client: Any,
    target_uuids: Sequence[str],
    group_id: str,
    depth: int,
    limit: int,
    relationship_names: Sequence[str] = (),
) -> list[_GraphExpansionHop]:
    """Walk edges that point AT the frontier rather than away from it.

    Retrieval's own expansion lane only follows outgoing edges, which is fine
    when the seeds came from a scored search. A caller steering the walk itself
    needs the other side too: the tasks that depend on this one and the passages
    cut from this memory are all inbound, and an outgoing-only neighborhood
    reports them as absent.
    """
    if not target_uuids:
        return []
    name_clause = "AND name IN $relationship_names" if relationship_names else ""
    rows = await _execute_query_records(
        client,
        f"""
        SELECT source_id AS uuid, name AS relationship
        FROM relates_to
        WHERE target_id IN $target_uuids
          AND group_id = $group_id
          AND in.group_id = $group_id
          {name_clause}
        LIMIT $limit;
        """,
        target_uuids=list(target_uuids),
        group_id=group_id,
        limit=max(int(limit), 1),
        **({"relationship_names": list(relationship_names)} if relationship_names else {}),
    )
    hops: list[_GraphExpansionHop] = []
    for row in rows:
        uuid = _string_value(row.get("uuid"))
        if not uuid:
            continue
        relationship = _string_value(row.get("relationship")) or "RELATED_TO"
        if not predicate_direction_allows(relationship, "incoming"):
            continue
        hops.append(
            _GraphExpansionHop(
                uuid=uuid,
                depth=depth,
                relationship=relationship,
                score=_graph_expansion_path_score(relationship, depth=depth),
                direction="incoming",
            )
        )
    return _dedupe_expansion_hops(hops)


async def _community_member_hops(
    *,
    client: Any,
    source_uuids: Sequence[str],
    group_id: str,
    depth: int,
    limit: int,
) -> list[_GraphExpansionHop]:
    if not source_uuids:
        return []
    community_ids = await _community_ids_for_entities(
        client=client,
        source_uuids=source_uuids,
        group_id=group_id,
        limit=limit,
    )
    if not community_ids:
        return []
    rows = await _execute_query_records(
        client,
        """
        SELECT source_id AS uuid, target_id AS community_id
        FROM relates_to
        WHERE target_id IN $community_uuids
          AND source_id NOT IN $source_uuids
          AND name = "BELONGS_TO"
          AND group_id = $group_id
          AND in.group_id = $group_id
          AND in.entity_type != "community"
        ORDER BY target_id
        LIMIT $limit;
        """,
        community_uuids=community_ids,
        source_uuids=list(source_uuids),
        group_id=group_id,
        limit=max(int(limit), 1),
    )
    hops: list[_GraphExpansionHop] = []
    for row in rows:
        uuid = _string_value(row.get("uuid"))
        if not uuid:
            continue
        hops.append(
            _GraphExpansionHop(
                uuid=uuid,
                depth=depth,
                relationship="SHARES_COMMUNITY",
                score=_graph_expansion_path_score("SHARES_COMMUNITY", depth=depth),
                community_id=_string_value(row.get("community_id")),
            )
        )
    return _dedupe_expansion_hops(hops)


async def _community_ids_for_entities(
    *,
    client: Any,
    source_uuids: Sequence[str],
    group_id: str,
    limit: int,
) -> list[str]:
    rows = await _execute_query_records(
        client,
        """
        SELECT target_id AS uuid
        FROM relates_to
        WHERE source_id IN $source_uuids
          AND name = "BELONGS_TO"
          AND group_id = $group_id
          AND out.group_id = $group_id
          AND out.entity_type = "community"
        LIMIT $limit;
        """,
        source_uuids=list(source_uuids),
        group_id=group_id,
        limit=max(int(limit), 1),
    )
    return _dedupe_strings(_record_uuids(rows))


async def _hydrate_graph_expansion_records(
    *,
    client: Any,
    hops: Sequence[_GraphExpansionHop],
    search_filter: SearchFilter,
    group_id: str,
    limit: int,
) -> list[dict[str, object]]:
    uuids = [hop.uuid for hop in hops]
    rows = await _hydrate_entity_records(
        client=client,
        uuids=uuids,
        search_filter=search_filter,
        group_id=group_id,
        limit=limit,
    )
    hops_by_uuid = {hop.uuid: hop for hop in hops}
    records: list[dict[str, object]] = []
    for row in rows:
        uuid = _string_value(row.get("uuid"))
        hop = hops_by_uuid.get(uuid or "")
        if hop is None:
            continue
        record = dict(row)
        record["score"] = hop.score
        record["graph_expansion_depth"] = hop.depth
        record["graph_expansion_relationship"] = hop.relationship
        record["graph_expansion_score"] = hop.score
        # Direction travels with the scored candidate so receipts can separate
        # incoming rescue from ordinary outgoing expansion.
        record["graph_expansion_direction"] = hop.direction
        if hop.community_id:
            record["graph_expansion_community_id"] = hop.community_id
        records.append(record)
    records.sort(key=_record_score, reverse=True)
    return records


async def expand_neighbor_records(
    *,
    client: Any,
    origin_uuids: Sequence[str],
    group_id: str,
    max_depth: int,
    limit: int,
    relationship_names: Sequence[str] = (),
    include_incoming: bool = True,
    search_filter: SearchFilter | None = None,
    row_allowed: Callable[[Mapping[str, object]], bool] | None = None,
    row_included: Callable[[Mapping[str, object]], bool] | None = None,
) -> list[dict[str, object]]:
    """Walk one bounded neighborhood for a caller that steers its own retrieval.

    Hop-tagged entity rows, highest path score first, capped at ``limit``.

    ``row_allowed`` is the reader's authorization check, and it gates the walk
    rather than only its output. The walk advances one hop at a time and only
    authorized rows join the next frontier, so a row the reader may not see is
    not merely withheld: it is not a route. Filtering only the final result would
    still return a depth-two neighbor reachable exclusively through another
    principal's private memory, which discloses that something sits between them.

    The check runs before the cap, and each hop is widened by the same headroom
    the scored lanes read with, because a handful of unreadable rows outranking
    the readable ones would otherwise consume the whole budget and report a
    neighborhood as empty when it is not.

    ``row_included`` narrows what the caller sees without narrowing where the walk
    may go. The two are deliberately separate: authorization is about reachability,
    while a presentational filter such as an entity-type restriction must not
    silently sever a route, or asking for one type of neighbor would hide the
    two-hop rows that are only reachable through another type.
    """
    limit = max(int(limit), 0)
    depth_budget = max(int(max_depth), 1)
    effective_filter = search_filter if search_filter is not None else SearchFilter()
    if not limit:
        return []

    collected: dict[str, dict[str, object]] = {}
    # Origins are the caller's own rows. Excluding them from results as well as
    # from re-expansion is what stops a seed being returned as its own neighbor,
    # which every inbound edge makes reachable at depth two.
    seen = {str(uuid) for uuid in origin_uuids if str(uuid)}
    frontier = _dedupe_strings(origin_uuids)

    for depth in range(1, depth_budget + 1):
        if not frontier or len(collected) >= limit:
            break
        hop_rows = await _node_bfs_records(
            client=client,
            origin_uuids=frontier,
            search_filter=effective_filter,
            group_id=group_id,
            max_depth=1,
            limit=_graph_expansion_fetch_limit(limit),
            relationship_names=relationship_names,
            include_incoming=include_incoming,
            # Every round is depth 1 as far as the shared function can tell, so
            # the first-hop-only community lane would otherwise re-fire on each
            # one and widen the neighborhood in a way the scored lane never does.
            include_community_hops=depth == 1,
        )
        next_frontier: list[str] = []
        for row in hop_rows:
            uuid = _string_value(row.get("uuid"))
            if not uuid or uuid in seen:
                continue
            seen.add(uuid)
            if row_allowed is not None and not row_allowed(row):
                # Unauthorized, so not a result and not a route either.
                continue
            # Authorized, so it is a route whatever the caller wants to see.
            next_frontier.append(uuid)
            if row_included is not None and not row_included(row):
                # Filtered from the answer, but still walked through.
                continue
            # The shared function reports every round as adjacent, because each
            # round is one hop from its own frontier. True distance from the
            # caller's seeds, and the decay it earns, are this walk's to state.
            relationship = _string_value(row.get("graph_expansion_relationship")) or "RELATED_TO"
            score = _graph_expansion_path_score(relationship, depth=depth)
            row["graph_expansion_depth"] = depth
            row["graph_expansion_score"] = score
            row["score"] = score
            collected[uuid] = row
        frontier = next_frontier

    ordered = sorted(collected.values(), key=_record_score, reverse=True)
    return ordered[:limit]


async def _hydrate_entity_records(
    *,
    client: Any,
    uuids: Sequence[str],
    search_filter: SearchFilter,
    group_id: str,
    limit: int,
) -> list[dict[str, object]]:
    if not uuids:
        return []
    filter_clauses, filter_params = _node_filter_clause(search_filter)
    rows = await _execute_query_records(
        client,
        "SELECT * FROM entity WHERE "
        + _where_clause(["uuid IN $uuids", "group_id = $group_id", *filter_clauses])
        + " LIMIT $limit;",
        uuids=list(uuids),
        group_id=group_id,
        limit=max(int(limit), 1),
        **filter_params,
    )
    rows_by_uuid = {str(row["uuid"]): row for row in rows if row.get("uuid")}
    return [rows_by_uuid[uuid] for uuid in uuids if uuid in rows_by_uuid]


def _record_uuids(rows: Sequence[Mapping[str, object]]) -> list[str]:
    return [str(row["uuid"]) for row in rows if row.get("uuid")]


def _dedupe_strings(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(str(value) for value in values if str(value)))


def _dedupe_expansion_hops(
    hops: Iterable[_GraphExpansionHop],
) -> list[_GraphExpansionHop]:
    deduped: dict[str, _GraphExpansionHop] = {}
    for hop in hops:
        if hop.uuid not in deduped or hop.score > deduped[hop.uuid].score:
            deduped[hop.uuid] = hop
    return list(deduped.values())


def _graph_expansion_path_score(relationship: str, *, depth: int) -> float:
    normalized = relationship.upper()
    base = _GRAPH_EXPANSION_RELATIONSHIP_WEIGHTS.get(normalized, 0.64)
    depth_multiplier = _GRAPH_EXPANSION_DEPTH_DECAY ** max(depth - 1, 0)
    return max(min(base * depth_multiplier, 1.0), 0.1)


def _predicate_hop_receipt(
    source_lists: Sequence[tuple[RetrievalSignal, Sequence[RetrievalCandidate]]],
) -> dict[str, object]:
    by_predicate: dict[str, int] = {}
    by_direction: dict[str, int] = {}
    for signal, candidates in source_lists:
        if signal is not RetrievalSignal.GRAPH_EXPANSION:
            continue
        for candidate in candidates:
            metadata = candidate.metadata if isinstance(candidate.metadata, Mapping) else {}
            predicate = str(metadata.get("graph_expansion_relationship") or "RELATED_TO")
            policy = predicate_policy(predicate)
            label = policy.receipt_label if policy is not None else predicate.lower()
            direction = str(metadata.get("graph_expansion_direction") or "outgoing")
            by_predicate[label] = by_predicate.get(label, 0) + 1
            by_direction[direction] = by_direction.get(direction, 0) + 1
    return {
        "predicate_hops": {
            "total": sum(by_predicate.values()),
            "by_predicate": by_predicate,
            "by_direction": by_direction,
        }
    }


def _graph_expansion_fetch_limit(limit: int) -> int:
    return max(int(limit) * _GRAPH_EXPANSION_FETCH_HEADROOM, 1)
