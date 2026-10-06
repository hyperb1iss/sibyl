"""Fail-closed lifecycle and supersession enforcement for retrieval."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import structlog

from sibyl_core.memory_pipeline.lifecycle import graph_metadata_recallable
from sibyl_core.retrieval._search_candidates import (
    _candidate_allowed,
    _candidate_from_edge_record,
    _string_value,
)
from sibyl_core.retrieval._search_database import _execute_query_records
from sibyl_core.retrieval._search_plan import RetrievalPlan, RetrievalSignal
from sibyl_core.retrieval.candidates import CandidateKind, RetrievalCandidate
from sibyl_core.services.eval_publication_guards import unavailable_publication_ids
from sibyl_core.services.graph_capture_availability import available_capture_projection_rows
from sibyl_core.services.graph_entities import EntityManager
from sibyl_core.services.graph_read_availability import (
    GraphReadMemo,
    available_graph_entities,
    available_graph_relationships,
)
from sibyl_core.services.graph_relationships import RelationshipManager
from sibyl_core.services.graph_runtime import GraphRuntime

_SUPERSEDES_PREDICATE = "SUPERSEDES"
_SUPERSESSION_LOOKUP_BATCH_SIZE = 512
_SUPERSESSION_EDGE_PAGE_SIZE = 512
_NON_ENTITY_CANDIDATE_TYPES = frozenset({"claim", "relationship", "raw_memory"})
log = structlog.get_logger()


async def _superseded_candidate_uuids(
    client: Any,
    *,
    group_id: str,
    uuids: Sequence[str],
    memo: GraphReadMemo | None = None,
) -> tuple[set[str], int]:
    """Resolve every inbound supersession edge for the candidate set.

    Candidate ids are batched to keep the indexed ``IN`` lookup bounded. Edge
    rows are paged independently because one retired candidate can have many
    inbound declarations. Returning a partial edge set would make a retired
    row look current, while treating a safety limit as an error would turn a
    dense but valid history into an availability failure.

    A memo keeps the inbound edges per target, so a request that checks the
    same id again resolves it from the rows it already read. The resolution
    itself still runs over the whole requested set: a cycle between two
    candidates is settled by the newest edge between them, and that needs
    both ends' edges on the table.
    """

    if not uuids:
        return set(), 0
    if memo is None:
        rows = await _supersession_edge_rows(client, group_id=group_id, uuids=uuids)
        return _resolve_superseded(rows), len(rows)
    memo._check_org(group_id)

    async def load(missing: list[str]) -> dict[str, tuple[dict[str, object], ...]]:
        by_target: dict[str, list[dict[str, object]]] = {}
        for row in await _supersession_edge_rows(client, group_id=group_id, uuids=missing):
            target = _string_value(row.get("target_id"))
            if target:
                by_target.setdefault(target, []).append(row)
        return {target: tuple(rows) for target, rows in by_target.items()}

    edges_by_target = await memo.once("supersession_edges", list(uuids), load, missing=())
    rows = [row for edges in edges_by_target.values() for row in edges]
    return _resolve_superseded(rows), len(rows)


async def _supersession_edge_rows(
    client: Any,
    *,
    group_id: str,
    uuids: Sequence[str],
) -> list[dict[str, object]]:
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
    return rows


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


async def _edge_rows_by_uuid(
    client: Any, group_id: str, edge_ids: list[str]
) -> dict[str, dict[str, object]]:
    rows: list[dict[str, object]] = []
    for offset in range(0, len(edge_ids), 512):
        rows.extend(
            await _execute_query_records(
                client,
                "SELECT *, in.uuid AS source_uuid, out.uuid AS target_uuid, in.uuid AS source_node_uuid, out.uuid AS target_node_uuid FROM relates_to WHERE group_id=$group_id AND uuid IN $ids;",
                group_id=group_id,
                ids=edge_ids[offset : offset + 512],
            )
        )
    return {uuid: row for row in rows if (uuid := _string_value(row.get("uuid")))}


async def _available_edge_endpoints(
    client: Any,
    group_id: str,
    candidates: Sequence[RetrievalCandidate],
    plan: RetrievalPlan | None,
    *,
    memo: GraphReadMemo | None = None,
) -> tuple[dict[str, tuple[str, str]], dict[str, RetrievalCandidate]]:
    if memo is None:
        memo = GraphReadMemo(group_id)
    edge_ids = list(
        dict.fromkeys(
            candidate.id
            for candidate in candidates
            if candidate.kind == CandidateKind.EDGE or candidate.type in {"claim", "relationship"}
        )
    )
    if not edge_ids:
        return {}, {}
    source_visible = (
        (lambda row: _source_candidate_allowed(row, plan)) if plan is not None else None
    )
    runtime = GraphRuntime(
        client,
        EntityManager(client, group_id=group_id),
        RelationshipManager(client, group_id=group_id),
    )
    rows_by_uuid = await memo.once(
        "edge_row",
        edge_ids,
        lambda missing: _edge_rows_by_uuid(client, group_id, missing),
        missing=None,
    )
    rows = [row for row in rows_by_uuid.values() if row is not None]
    from sibyl_core.services.graph_records import relationship_from_surreal_row

    current_relationships = await available_graph_relationships(
        group_id,
        edge_ids,
        runtime=runtime,
        source_visible=source_visible,
        memo=memo,
    )
    rows = [
        row
        for row in rows
        if str(row.get("uuid")) in current_relationships
        and relationship_from_surreal_row(row).model_dump(
            mode="json", exclude={"metadata": {"operational_write_witness"}}
        )
        == current_relationships[str(row["uuid"])].model_dump(
            mode="json", exclude={"metadata": {"operational_write_witness"}}
        )
        and row.get("operational_source_binding")
        == current_relationships[str(row["uuid"])].operational_source_binding
    ]
    endpoints = {
        str(row["uuid"]): (str(row["source_uuid"]), str(row["target_uuid"]))
        for row in rows
        if row.get("group_id") == group_id
        and row.get("uuid") in edge_ids
        and row.get("source_uuid")
        and row.get("target_uuid")
    }
    entities = await available_graph_entities(
        group_id,
        list({endpoint for pair in endpoints.values() for endpoint in pair}),
        runtime=runtime,
        source_visible=source_visible,
        memo=memo,
    )
    allowed = set()
    if plan is not None:
        for identifier, entity in entities.items():
            candidate = RetrievalCandidate(
                id=identifier,
                type=entity.entity_type.value,
                name=entity.name,
                content=entity.content,
                score=0,
                source=None,
                metadata=entity.metadata,
                project_id=identifier
                if entity.entity_type.value == "project"
                else entity.metadata.get("project_id"),
            )
            if _candidate_allowed(candidate, plan=plan, requested_types=set(), facet=None):
                allowed.add(identifier)
    available_edges: dict[str, RetrievalCandidate] = {}
    if plan is not None:
        for row in rows:
            identifier = str(row.get("uuid", ""))
            if identifier not in endpoints or not set(endpoints[identifier]) <= allowed:
                continue
            current = _candidate_from_edge_record(
                row, signal=RetrievalSignal.EDGE_FULLTEXT, score=0
            )
            if graph_metadata_recallable(current.metadata) and _candidate_allowed(
                current, plan=plan, requested_types=set(), facet=None
            ):
                available_edges[identifier] = current
    unavailable = await memo.unavailable_publication_ids(
        group_id,
        {key: candidate.metadata for key, candidate in available_edges.items()},
        load=unavailable_publication_ids,
    )
    return endpoints, {
        key: candidate for key, candidate in available_edges.items() if key not in unavailable
    }


async def _apply_supersession_gate(
    *,
    client: Any,
    group_id: str,
    source_lists: Sequence[tuple[RetrievalSignal, list[RetrievalCandidate]]],
    plan: RetrievalPlan | None = None,
    memo: GraphReadMemo | None = None,
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

    The capture-ancestry, publication-ledger and edge proofs answer different
    questions about disjoint inputs, so they run together; the supersession
    lookup waits for them because its id set is what survived. A memo shared
    across the gate's passes settles each id once for the whole request.
    """

    if memo is None:
        memo = GraphReadMemo(group_id)
    source_rows = {
        candidate.id: candidate
        for _signal, candidates in source_lists
        for candidate in candidates
        if candidate.kind != CandidateKind.EDGE and candidate.type not in {"claim", "relationship"}
    }
    (
        available_sources,
        unavailable_publications,
        (edge_endpoints, available_edges),
    ) = await asyncio.gather(
        memo.available_capture_projection_rows(
            group_id,
            source_rows,
            load=available_capture_projection_rows,
            graph_client=client,
            source_visible=(lambda row: _source_candidate_allowed(row, plan))
            if plan is not None
            else None,
        ),
        memo.unavailable_publication_ids(
            group_id,
            {candidate.id: candidate.metadata for candidate in source_rows.values()},
            load=unavailable_publication_ids,
        ),
        _available_edge_endpoints(
            client,
            group_id,
            [candidate for _, candidates in source_lists for candidate in candidates],
            plan,
            memo=memo,
        ),
    )
    lifecycle_dropped = 0
    surviving: list[tuple[RetrievalSignal, list[RetrievalCandidate]]] = []
    for signal, candidates in source_lists:
        kept: list[RetrievalCandidate] = []
        for candidate in candidates:
            if candidate.kind == CandidateKind.EDGE or candidate.type in {"claim", "relationship"}:
                current = available_edges.get(candidate.id)
                if current is None:
                    lifecycle_dropped += 1
                    continue
                candidate = replace(
                    current,
                    score=candidate.score,
                    retrieval_signals=candidate.retrieval_signals,
                    metadata={
                        **current.metadata,
                        "retrieval_signals": list(candidate.retrieval_signals or (signal.value,)),
                    },
                )
            if (
                graph_metadata_recallable(candidate.metadata)
                and candidate.id not in unavailable_publications
                and (candidate.id not in source_rows or candidate.id in available_sources)
            ):
                kept.append(candidate)
            else:
                lifecycle_dropped += 1
        surviving.append((signal, kept))

    # An archived `episode` row is not an entity, so no SUPERSEDES edge can
    # name it. A native episode is one and is checked like every other.
    node_uuids = list(
        dict.fromkeys(
            candidate.id
            for _signal, candidates in surviving
            for candidate in candidates
            if candidate.kind != CandidateKind.EPISODE
            and candidate.type not in _NON_ENTITY_CANDIDATE_TYPES
        )
    )
    node_uuids = list(
        dict.fromkeys(
            [*node_uuids, *(endpoint for pair in edge_endpoints.values() for endpoint in pair)]
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
                memo=memo,
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
            kept = [
                candidate
                for candidate in candidates
                if candidate.id not in superseded
                and not (set(edge_endpoints.get(candidate.id, ())) & superseded)
            ]
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


def _source_candidate_allowed(row, plan: RetrievalPlan) -> bool:
    raw_type = getattr(row, "entity_type", None)
    row_type = getattr(raw_type, "value", raw_type) or "raw_memory"
    project_id = row.id if row_type == "project" else getattr(row, "project_id", None)
    candidate = RetrievalCandidate(
        id=row.id,
        type=row_type,
        name=getattr(row, "name", None) or getattr(row, "title", None) or "",
        content="",
        score=0,
        source=None,
        metadata=row.metadata,
        project_id=project_id or row.metadata.get("project_id"),
    )
    return _candidate_allowed(candidate, plan=plan, requested_types=set(), facet=None)
