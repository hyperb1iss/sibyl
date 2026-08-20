"""Fail-closed lifecycle and supersession enforcement for retrieval."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

import structlog

from sibyl_core.memory_pipeline.lifecycle import graph_metadata_recallable
from sibyl_core.retrieval._search_candidates import _string_value
from sibyl_core.retrieval._search_database import _execute_query_records
from sibyl_core.retrieval._search_plan import RetrievalSignal
from sibyl_core.retrieval.candidates import RetrievalCandidate

_SUPERSEDES_PREDICATE = "SUPERSEDES"
_SUPERSESSION_LOOKUP_BATCH_SIZE = 512
_SUPERSESSION_EDGE_PAGE_SIZE = 512
_NON_ENTITY_CANDIDATE_TYPES = frozenset({"claim", "relationship", "raw_memory", "episode"})
log = structlog.get_logger()


async def _superseded_candidate_uuids(
    client: Any,
    *,
    group_id: str,
    uuids: Sequence[str],
) -> tuple[set[str], int]:
    """Resolve every inbound supersession edge for the candidate set.

    Candidate ids are batched to keep the indexed ``IN`` lookup bounded. Edge
    rows are paged independently because one retired candidate can have many
    inbound declarations. Returning a partial edge set would make a retired
    row look current, while treating a safety limit as an error would turn a
    dense but valid history into an availability failure.
    """

    if not uuids:
        return set(), 0
    rows: list[dict[str, object]] = []
    for batch_start in range(0, len(uuids), _SUPERSESSION_LOOKUP_BATCH_SIZE):
        batch = list(uuids[batch_start : batch_start + _SUPERSESSION_LOOKUP_BATCH_SIZE])
        upper_rows = await _execute_query_records(
            client,
            """
            SELECT uuid, target_id, source_id, created_at
            FROM relates_to WITH INDEX idx_relates_target_created
            WHERE name = $predicate
              AND target_id IN $uuids
              AND group_id = $group_id
            ORDER BY created_at DESC, uuid DESC
            LIMIT 1;
            """,
            predicate=_SUPERSEDES_PREDICATE,
            uuids=batch,
            group_id=group_id,
        )
        if not upper_rows:
            continue
        upper_created_at, upper_uuid, upper_key = _supersession_edge_cursor(upper_rows[0])
        after_created_at: object | None = None
        after_uuid: str | None = None
        after_key: tuple[str, str] | None = None
        while True:
            if after_key is None:
                page = await _execute_query_records(
                    client,
                    """
                    SELECT uuid, target_id, source_id, created_at
                    FROM relates_to WITH INDEX idx_relates_target_created
                    WHERE name = $predicate
                      AND target_id IN $uuids
                      AND group_id = $group_id
                      AND (
                        created_at < $upper_created_at
                        OR (created_at = $upper_created_at AND uuid <= $upper_uuid)
                      )
                    ORDER BY created_at, uuid
                    LIMIT $limit;
                    """,
                    predicate=_SUPERSEDES_PREDICATE,
                    uuids=batch,
                    group_id=group_id,
                    upper_created_at=upper_created_at,
                    upper_uuid=upper_uuid,
                    limit=_SUPERSESSION_EDGE_PAGE_SIZE,
                )
            else:
                page = await _execute_query_records(
                    client,
                    """
                    SELECT uuid, target_id, source_id, created_at
                    FROM relates_to WITH INDEX idx_relates_target_created
                    WHERE name = $predicate
                      AND target_id IN $uuids
                      AND group_id = $group_id
                      AND (
                        created_at > $after_created_at
                        OR (created_at = $after_created_at AND uuid > $after_uuid)
                      )
                      AND (
                        created_at < $upper_created_at
                        OR (created_at = $upper_created_at AND uuid <= $upper_uuid)
                      )
                    ORDER BY created_at, uuid
                    LIMIT $limit;
                    """,
                    predicate=_SUPERSEDES_PREDICATE,
                    uuids=batch,
                    group_id=group_id,
                    after_created_at=after_created_at,
                    after_uuid=after_uuid,
                    upper_created_at=upper_created_at,
                    upper_uuid=upper_uuid,
                    limit=_SUPERSESSION_EDGE_PAGE_SIZE,
                )
            rows.extend(page)
            if len(page) < _SUPERSESSION_EDGE_PAGE_SIZE:
                break
            after_created_at, after_uuid, next_key = _supersession_edge_cursor(page[-1])
            if after_key is not None and next_key <= after_key:
                raise RuntimeError("supersession edge cursor did not advance")
            if next_key > upper_key:
                raise RuntimeError("supersession edge cursor advanced beyond its snapshot")
            after_key = next_key
    return _resolve_superseded(rows), len(rows)


def _supersession_edge_cursor(row: Mapping[str, object]) -> tuple[object, str, tuple[str, str]]:
    created_at = row.get("created_at")
    uuid = _string_value(row.get("uuid"))
    if created_at is None or not uuid:
        raise RuntimeError("supersession edge is missing its pagination cursor")
    return created_at, uuid, (_edge_sort_key(created_at), uuid)


def _resolve_superseded(rows: Sequence[Mapping[str, object]]) -> set[str]:
    """Decide which endpoints an edge set actually retires.

    Two shapes have to be handled before a target can be trusted as retired.
    A self-edge says a row replaced itself, which retires nothing and would
    otherwise black out a live row. A cycle (A supersedes B, B supersedes A)
    would retire both endpoints and black out the pair, so the newest edge
    wins: it is the most recent statement about that pair, and the older
    edge in the opposite direction is treated as replaced by it.

    "Newest" has to be a total order or the winner becomes a function of the
    order the rows arrived in, which is a property of the query planner rather
    than of the data: the same two edges then retire A on one run and B on the
    next. The ordering key is therefore `(created_at, edge uuid)`, compared
    strictly, so equal timestamps resolve on the edge id instead of on
    whichever row the engine handed over last.

    `created_at` is stamped by whichever process wrote the edge
    (`models/entities.py`), so two writers with skewed clocks can order a
    causally later edge first. Skew of exactly the resolution of the stamp
    lands on the edge-id tie-breaker; larger skew inverts the pair. Both
    outcomes are stable and identical on every replica, which is the property
    recall needs: one of the two rows survives, and every reader agrees on
    which.
    """

    retired: set[str] = set()
    edges: list[tuple[str, str, tuple[str, str]]] = []
    for row in rows:
        target = _string_value(row.get("target_id"))
        if not target:
            continue
        source = _string_value(row.get("source_id"))
        if target == source:
            # A row cannot replace itself. Honoring it would retire a live row
            # on a statement that says nothing.
            continue
        if not source:
            # An edge with no recorded source still says this row was
            # replaced; it just cannot take part in resolving a cycle.
            retired.add(target)
            continue
        sort_key = (
            _edge_sort_key(row.get("created_at")),
            _string_value(row.get("uuid")) or "",
        )
        edges.append((target, source, sort_key))

    newest_between: dict[tuple[str, str], tuple[str, str, tuple[str, str]]] = {}
    for edge in edges:
        target, source, sort_key = edge
        pair = (target, source) if target < source else (source, target)
        current = newest_between.get(pair)
        if current is None or sort_key > current[2]:
            newest_between[pair] = edge
    retired.update(target for target, _source, _sort_key in newest_between.values())
    return retired


def _edge_sort_key(value: object) -> str:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    return _string_value(value) or ""


async def _apply_supersession_gate(
    *,
    client: Any,
    group_id: str,
    source_lists: Sequence[tuple[RetrievalSignal, list[RetrievalCandidate]]],
) -> tuple[list[tuple[RetrievalSignal, list[RetrievalCandidate]]], dict[str, Any]]:
    """Drop rows a writer has already retired, before anything is fused.

    Supersession and correction are declarations the graph lane never acted
    on: a corrected row kept its embedding, kept its rank, and kept being
    expanded into. Two independent signals retire a candidate here. Its own
    stamped lifecycle metadata, written by the correction path, covers rows
    whose replacement is not itself a graph entity. An inbound SUPERSEDES edge
    covers the reflection-promotion case, where the replacement exists and the
    edge is the only record of it. Because the successor carries neither
    signal, this is also what makes the newer row win whenever both match.
    """

    lifecycle_dropped = 0
    surviving: list[tuple[RetrievalSignal, list[RetrievalCandidate]]] = []
    for signal, candidates in source_lists:
        kept: list[RetrievalCandidate] = []
        for candidate in candidates:
            if graph_metadata_recallable(candidate.metadata):
                kept.append(candidate)
            else:
                lifecycle_dropped += 1
        surviving.append((signal, kept))

    node_uuids = list(
        dict.fromkeys(
            candidate.id
            for _signal, candidates in surviving
            for candidate in candidates
            if candidate.type not in _NON_ENTITY_CANDIDATE_TYPES
        )
    )
    superseded: set[str] = set()
    edge_rows = 0
    if node_uuids:
        try:
            superseded, edge_rows = await _superseded_candidate_uuids(
                client,
                group_id=group_id,
                uuids=node_uuids,
            )
        except Exception as exc:
            error_type = type(exc).__name__
            log.error(
                "supersession_lookup_failed",
                organization_id=group_id,
                candidate_count=len(node_uuids),
                error_type=error_type,
            )
            raise RuntimeError("supersession lifecycle lookup failed") from exc

    edge_dropped = 0
    if superseded:
        gated: list[tuple[RetrievalSignal, list[RetrievalCandidate]]] = []
        for signal, candidates in surviving:
            kept = [candidate for candidate in candidates if candidate.id not in superseded]
            edge_dropped += len(candidates) - len(kept)
            gated.append((signal, kept))
        surviving = gated

    receipt: dict[str, Any] = {
        "lifecycle_dropped": lifecycle_dropped,
        "superseded_dropped": edge_dropped,
        "superseded_uuids": sorted(superseded),
        "checked_candidates": len(node_uuids),
        "edge_rows_read": edge_rows,
    }
    return surviving, {"supersession_gate": receipt}


def _merged_supersession_metadata(
    *receipts: Mapping[str, Any],
) -> dict[str, Any]:
    """Fold the gate's passes into the one receipt a caller reads.

    The gate runs twice per search, once before the graph walk is seeded and
    once on what the walk brought back, and a receipt that reported only the
    second pass would say a corrected row was never dropped. Counts add and
    uuid sets union. Lookup failures do not produce receipts because the gate
    fails closed before returning unchecked candidates.
    """

    merged: dict[str, Any] = {
        "lifecycle_dropped": 0,
        "superseded_dropped": 0,
        "superseded_uuids": [],
        "checked_candidates": 0,
        "edge_rows_read": 0,
    }
    uuids: set[str] = set()
    for receipt in receipts:
        gate = receipt.get("supersession_gate")
        if not isinstance(gate, Mapping):
            continue
        merged["lifecycle_dropped"] += int(gate.get("lifecycle_dropped") or 0)
        merged["superseded_dropped"] += int(gate.get("superseded_dropped") or 0)
        merged["checked_candidates"] += int(gate.get("checked_candidates") or 0)
        merged["edge_rows_read"] += int(gate.get("edge_rows_read") or 0)
        uuids.update(str(value) for value in gate.get("superseded_uuids") or ())
    merged["superseded_uuids"] = sorted(uuids)
    return {"supersession_gate": merged}
