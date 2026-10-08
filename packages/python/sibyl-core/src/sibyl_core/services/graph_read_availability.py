"""Current stored graph rows eligible for ancestry-aware public readers."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Coroutine, Hashable, Mapping, Sequence
from typing import Any

from pydantic import ValidationError

from sibyl_core.models.entities import Entity, Relationship
from sibyl_core.services.eval_publication_guards import available_graph_entity_rows
from sibyl_core.services.graph_read_validation import GraphReadValidation
from sibyl_core.services.graph_runtime import GraphRuntime, get_surreal_graph_runtime
from sibyl_core.services.source_observations import SourceUnavailableError

_READ_BATCH_SIZE = 512

type SourceVisible = Callable[[Any], bool]


class GraphReadMemo:
    """Settle each read-availability question once per retrieval request.

    One request asks the same question about the same rows several times
    over: the supersession gate before the graph walk and the gate after it,
    the pack's admission check and the related-item batch each re-derive
    availability for ids an earlier pass already settled, and inside one pass
    the edge proof re-reads its endpoints for every validation phase. Each
    (kind, id) is loaded here once; a later asker, concurrent or not, awaits
    the load already in flight instead of issuing its own.

    The memo lives for one request and one reader. A verdict is as fresh as
    the request's first read of that row, which is the snapshot the request
    answers from in any case. Every scoped question in one request must come
    from the same reader filter: the filter is part of the verdict, and only
    its presence is part of the key.
    """

    def __init__(self, organization_id: str) -> None:
        self.organization_id = organization_id
        self._loads: dict[tuple[str, Hashable], asyncio.Task[Mapping[Any, Any]]] = {}
        self.load_count: dict[str, int] = {}

    def _check_org(self, organization_id: str) -> None:
        if organization_id != self.organization_id:
            raise SourceUnavailableError()

    @staticmethod
    def reader_scope(source_visible: SourceVisible | None) -> str:
        return "unscoped" if source_visible is None else "scoped"

    async def once[K: Hashable, V](
        self,
        kind: str,
        keys: Sequence[K],
        load: Callable[[list[K]], Coroutine[Any, Any, Mapping[K, V]]],
        *,
        missing: V,
    ) -> dict[K, V]:
        """Answer ``keys`` for ``kind``, loading only the ones nobody asked yet.

        ``load`` receives the unsettled keys and returns a verdict per key it
        found; a key it leaves out settles to ``missing``. A load that raises
        settles nothing, so the next asker reloads those keys, and an asker
        cancelled mid-wait leaves the shared load running for the others.
        """
        ordered = list(dict.fromkeys(keys))
        pending = [key for key in ordered if (kind, key) not in self._loads]
        if pending:
            # Reserve every key before yielding so an overlapping asker shares
            # this load rather than starting a second one for the same ids.
            task = asyncio.create_task(load(pending))
            for key in pending:
                self._loads[(kind, key)] = task
            self.load_count[kind] = self.load_count.get(kind, 0) + 1
        by_task: dict[asyncio.Task[Mapping[Any, Any]], list[K]] = {}
        for key in ordered:
            by_task.setdefault(self._loads[(kind, key)], []).append(key)
        results: dict[K, V] = {}
        for task, task_keys in by_task.items():
            try:
                loaded = await asyncio.shield(task)
            except BaseException:
                if task.done() and not task.cancelled() and task.exception() is not None:
                    for key in task_keys:
                        if self._loads.get((kind, key)) is task:
                            del self._loads[(kind, key)]
                raise
            for key in task_keys:
                results[key] = loaded.get(key, missing)
        return results

    async def unavailable_publication_ids(
        self,
        organization_id: str,
        rows: Mapping[str, Mapping[str, object] | None],
        *,
        load: Callable[..., Awaitable[set[str]]],
    ) -> set[str]:
        """The protected-ledger verdict per row id, each id proven once."""
        self._check_org(organization_id)

        async def load_missing(missing: list[str]) -> dict[str, bool]:
            unavailable = await load(
                organization_id, {identifier: rows[identifier] for identifier in missing}
            )
            return dict.fromkeys(unavailable, True)

        verdicts = await self.once("publication", list(rows), load_missing, missing=False)
        return {identifier for identifier, unavailable in verdicts.items() if unavailable}

    async def available_capture_projection_rows[T](
        self,
        organization_id: str,
        rows: Mapping[str, T],
        *,
        load: Callable[..., Awaitable[Mapping[str, T]]],
        graph_client: Any,
        source_visible: SourceVisible | None,
    ) -> dict[str, T]:
        """The capture-ancestry verdict per row id, each id proven once."""
        self._check_org(organization_id)

        async def load_missing(missing: list[str]) -> dict[str, bool]:
            available = await load(
                organization_id,
                {identifier: rows[identifier] for identifier in missing},
                graph_client=graph_client,
                source_visible=source_visible,
            )
            return dict.fromkeys(available, True)

        verdicts = await self.once(
            f"capture_rows:{self.reader_scope(source_visible)}",
            list(rows),
            load_missing,
            missing=False,
        )
        return {
            identifier: rows[identifier] for identifier, available in verdicts.items() if available
        }


async def _each_batch[T, R](
    client: Any, read_batch: Callable[[T], Awaitable[R]], batches: Sequence[T]
) -> list[R]:
    """Read batches concurrently on a pooled client, in order on a single slot.

    A single-slot pool (every embedded store) only queues overlapping reads,
    and a queued waiter binds the pool's queue to the current event loop,
    which a later loop reusing the same client cannot wait on.
    """
    if getattr(client, "pool_size", 1) <= 1:
        return [await read_batch(batch) for batch in batches]
    return list(await asyncio.gather(*(read_batch(batch) for batch in batches)))


async def available_graph_entities(
    organization_id: str,
    entity_ids: Sequence[str],
    *,
    runtime: GraphRuntime | None = None,
    read: GraphReadValidation | None = None,
    source_visible: SourceVisible | None = None,
    memo: GraphReadMemo | None = None,
    include_embeddings: bool = True,
) -> dict[str, Entity]:
    """Refresh actual rows and reject missing, retired, or unavailable ancestry.

    A supplied runtime must be the existing GraphRuntime for this organization.
    Callers retain their principal/project scope filters and render these current
    rows, rather than cached values with the same IDs. Each standalone call owns
    a fresh validation phase unless its caller supplies one. A memo settles each
    id once for the request that owns it, and cannot share an explicit phase.
    Callers that only compare or render rows pass include_embeddings=False: the
    proofs never consult vectors, and a 1024-float column dominates the row
    payload. A memo keeps vector-free rows apart from full ones.
    """
    if read is not None and read.content_execute_query is not None:
        raise ValueError("explicit validation readers require supplied entity rows")
    ids = list(dict.fromkeys(entity_ids))
    if not ids:
        return {}
    if memo is not None:
        if read is not None:
            raise ValueError("a read memo cannot share an explicit validation phase")
        memo._check_org(organization_id)
        rows_kind = "rows" if include_embeddings else "rows_without_vectors"
        verdicts = await memo.once(
            f"entity:{memo.reader_scope(source_visible)}:{rows_kind}",
            ids,
            lambda missing: _load_available_graph_entities(
                organization_id,
                missing,
                runtime=runtime,
                source_visible=source_visible,
                include_embeddings=include_embeddings,
            ),
            missing=None,
        )
        return {identifier: row for identifier, row in verdicts.items() if row is not None}
    return await _load_available_graph_entities(
        organization_id,
        ids,
        runtime=runtime,
        read=read,
        source_visible=source_visible,
        include_embeddings=include_embeddings,
    )


async def _load_available_graph_entities(
    organization_id: str,
    ids: list[str],
    *,
    runtime: GraphRuntime | None,
    read: GraphReadValidation | None = None,
    source_visible: SourceVisible | None,
    include_embeddings: bool = True,
) -> dict[str, Entity]:
    graph = runtime or await get_surreal_graph_runtime(organization_id, ensure_schema=False)
    validation = read if read is not None else GraphReadValidation(organization_id)
    manager = graph.entity_manager
    vector_free = not include_embeddings and getattr(
        manager, "supports_embedding_free_reads", False
    )
    current: dict[str, Entity] = {}
    for offset in range(0, len(ids), _READ_BATCH_SIZE):
        batch = ids[offset : offset + _READ_BATCH_SIZE]
        rows = (
            await manager.get_many(batch, include_embeddings=False)
            if vector_free
            else await manager.get_many(batch)
        )
        for row in rows:
            if row.id in batch:
                current[row.id] = row
    return await available_graph_entity_rows(
        organization_id,
        current,
        graph_client=graph.client,
        read=validation,
        source_visible=source_visible,
    )


async def available_graph_relationships(
    organization_id: str,
    relationship_ids: Sequence[str],
    *,
    runtime: GraphRuntime | None = None,
    read: GraphReadValidation | None = None,
    source_visible: SourceVisible | None = None,
    memo: GraphReadMemo | None = None,
    endpoints: Mapping[str, Entity] | None = None,
) -> dict[str, Relationship]:
    """Refresh stored edges and require their protected operational generation.

    The first validation phase is private. The final phase can share its source
    proof with a caller that records the complete response read footprint.
    ``source_visible`` narrows the endpoints an edge may stand on to the rows
    this reader can see, which is the check an edge reader applies afterwards
    in any case; proving it here lets a memo settle each endpoint once.
    ``endpoints`` hands over the rows the caller already proved in the same
    phase; an edge then stands only on those, and its endpoints are compared
    with each snapshot instead of being proven again.
    """
    if read is not None and read.content_execute_query is not None:
        raise ValueError("explicit validation readers do not materialize relationships")
    ids = list(dict.fromkeys(relationship_ids))
    if not ids:
        return {}
    if endpoints is not None and memo is not None:
        raise ValueError("proven endpoints and a read memo settle the same rows twice")
    if memo is not None:
        if read is not None:
            raise ValueError("a read memo cannot share an explicit validation phase")
        memo._check_org(organization_id)
        verdicts = await memo.once(
            f"relationship:{memo.reader_scope(source_visible)}",
            ids,
            lambda missing: _load_available_graph_relationships(
                organization_id,
                missing,
                runtime=runtime,
                source_visible=source_visible,
                memo=memo,
            ),
            missing=None,
        )
        return {identifier: row for identifier, row in verdicts.items() if row is not None}
    return await _load_available_graph_relationships(
        organization_id,
        ids,
        runtime=runtime,
        read=read,
        source_visible=source_visible,
        endpoints=endpoints,
    )


async def _load_available_graph_relationships(
    organization_id: str,
    ids: list[str],
    *,
    runtime: GraphRuntime | None,
    read: GraphReadValidation | None = None,
    source_visible: SourceVisible | None,
    memo: GraphReadMemo | None = None,
    endpoints: Mapping[str, Entity] | None = None,
) -> dict[str, Relationship]:
    from sibyl_core.backends.surreal.records import normalize_records
    from sibyl_core.services.graph_records import (
        entity_from_surreal_row,
        readable_relationship_from_surreal_row,
    )
    from sibyl_core.services.operational_relationships import (
        _snapshot,
        operational_relationship_current,
        org_lookup_clumps,
        relationship_body_digest,
    )

    graph = runtime or await get_surreal_graph_runtime(organization_id, ensure_schema=False)
    endpoints_query = f"""RETURN array::flatten({org_lookup_clumps("ids")}.map(|$lookup|
        (SELECT in.uuid AS source_uuid, out.uuid AS target_uuid FROM relates_to
            WHERE group_id=$lookup[0] AND uuid IN $lookup[1])));"""

    async def capture(batch: list[str]) -> tuple[list[str], set[str], dict[str, Any]]:
        initial = normalize_records(
            await graph.client.execute_query(endpoints_query, org=organization_id, ids=batch)
        )
        endpoints = {
            value
            for r in initial
            for k in ("source_uuid", "target_uuid")
            if isinstance(value := r.get(k), str)
        }
        snapshot = await _snapshot(
            graph.client,
            organization_id=organization_id,
            ids=endpoints,
            relationship_ids=batch,
            include_embeddings=False,
        )
        return batch, endpoints, snapshot

    captured = await _each_batch(
        graph.client,
        capture,
        [ids[start : start + _READ_BATCH_SIZE] for start in range(0, len(ids), _READ_BATCH_SIZE)],
    )
    all_endpoints: set[str] = set().union(*(endpoints for _, endpoints, _ in captured))

    def endpoint_evidence(entity: Entity) -> tuple[Any, ...]:
        # Vectors are storage, not evidence: entity_read_evidence leaves them
        # out, and neither side was read with them.
        return (
            entity.model_dump(mode="json", exclude={"embedding"}),
            entity.derivation_required,
            entity.observed_revision,
        )

    async def validate(snapshots, read):
        result = {}
        current = (
            endpoints
            if endpoints is not None
            else await available_graph_entities(
                organization_id,
                sorted(all_endpoints),
                runtime=graph,
                read=None if memo is not None else read,
                source_visible=source_visible,
                memo=memo,
                include_embeddings=False,
            )
        )
        # Each side is decoded once per snapshot, however many edges share it.
        current_evidence: dict[str, tuple[Any, ...]] = {}

        def unchanged(endpoint: str, targets, stored_evidence) -> bool:
            if endpoint not in current_evidence:
                current_evidence[endpoint] = endpoint_evidence(current[endpoint])
            if endpoint not in stored_evidence:
                stored_evidence[endpoint] = endpoint_evidence(
                    entity_from_surreal_row(targets[endpoint])
                )
            return current_evidence[endpoint] == stored_evidence[endpoint]

        for snapshot in snapshots:
            targets = {r["uuid"]: r for r in snapshot["targets"]}
            states = {r["source_id"]: r for r in snapshot["states"]}
            associations = {r["target_id"]: r for r in snapshot["associations"]}
            stored_evidence: dict[str, tuple[Any, ...]] = {}
            for row in snapshot["relationships"]:
                if any(
                    row.get(key) not in current or row.get(key) not in targets
                    for key in ("source_uuid", "target_uuid")
                ):
                    continue
                if not all(
                    unchanged(row[key], targets, stored_evidence)
                    for key in ("source_uuid", "target_uuid")
                ):
                    continue
                relationship = readable_relationship_from_surreal_row(row)
                if relationship is None:
                    continue
                if await operational_relationship_current(
                    row,
                    targets=targets,
                    states=states,
                    associations=associations,
                    organization_id=organization_id,
                    read=read,
                ):
                    result[row["uuid"]] = relationship
        return result

    first = await validate(
        [snapshot for _, _, snapshot in captured], GraphReadValidation(organization_id)
    )

    async def recapture(
        entry: tuple[list[str], set[str], dict[str, Any]],
    ) -> dict[str, Any]:
        batch, endpoints, snapshot = entry
        fresh = await _snapshot(
            graph.client,
            organization_id=organization_id,
            ids=endpoints,
            relationship_ids=batch,
            include_embeddings=False,
        )
        originals = {row["uuid"]: row for row in snapshot["relationships"]}
        original_associations = {row["target_id"]: row for row in snapshot["associations"]}
        changed_associations = {
            row["target_id"]
            for row in fresh["associations"]
            if row != original_associations.get(row["target_id"])
        }
        changed_associations.update(
            original_associations.keys() - {row["target_id"] for row in fresh["associations"]}
        )
        # Compare semantic bodies and protected bindings, not write witnesses.
        unchanged_rows = []
        for row in fresh["relationships"]:
            if (
                row["uuid"] not in first
                or {row.get("source_uuid"), row.get("target_uuid")} & changed_associations
            ):
                continue
            original = originals[row["uuid"]]
            try:
                same_body = relationship_body_digest(row) == relationship_body_digest(original)
            except ValidationError:
                continue
            if (
                same_body
                and row.get("operational_source_binding")
                == original.get("operational_source_binding")
                and row.get("operational_derivation_required")
                == original.get("operational_derivation_required")
            ):
                unchanged_rows.append(row)
        fresh["relationships"] = unchanged_rows
        return fresh

    final_snapshots = await _each_batch(graph.client, recapture, captured)
    return await validate(
        final_snapshots, read if read is not None else GraphReadValidation(organization_id)
    )


async def unchanged_graph_relationships(
    organization_id: str,
    relationships: dict[str, Relationship],
    *,
    runtime: GraphRuntime | None = None,
) -> dict[str, Relationship]:
    """Keep previously validated edges only while their stored evidence matches.

    Node availability can await source reads after edge validation. Re-prove
    source and endpoint generations before comparing stored edge bodies once
    more to select or render paths. This read neither repairs graph rows nor
    reuses a source proof for new facts.
    """
    from sibyl_core.backends.surreal.records import normalize_records
    from sibyl_core.services.graph_records import readable_relationship_from_surreal_row

    def evidence(relationship: Relationship):
        body = relationship.model_dump(mode="json", exclude={"created_at", "metadata"})
        body["metadata"] = {
            key: value
            for key, value in relationship.metadata.items()
            if key
            not in {
                "record_id",
                "embedding",
                "fact_embedding",
                "embedding_metadata",
                "operational_write_witness",
            }
        }
        return body

    if not relationships:
        return {}
    graph = runtime or await get_surreal_graph_runtime(organization_id, ensure_schema=False)
    fresh = await available_graph_relationships(organization_id, list(relationships), runtime=graph)
    proven = {
        identifier: expected
        for identifier, expected in relationships.items()
        if (current := fresh.get(identifier)) is not None
        and evidence(current) == evidence(expected)
        and current.operational_source_binding == expected.operational_source_binding
        and current.operational_derivation_required == expected.operational_derivation_required
    }
    ids = list(proven)
    unchanged: dict[str, Relationship] = {}
    for start in range(0, len(ids), _READ_BATCH_SIZE):
        batch = ids[start : start + _READ_BATCH_SIZE]
        rows = normalize_records(
            await graph.client.execute_query(
                "RETURN { LET $edges=SELECT *,in.uuid AS source_uuid,out.uuid AS target_uuid "
                "FROM relates_to WHERE group_id=$org AND uuid IN $ids; "
                "LET $evidence=SELECT * OMIT fact_embedding,attributes.embedding,"
                "attributes.fact_embedding,attributes.embedding_metadata,"
                "attributes.operational_write_witness FROM $edges; RETURN $evidence; };",
                org=organization_id,
                ids=batch,
            )
        )
        for row in rows:
            expected = proven.get(row.get("uuid"))
            if expected is None or row.get("group_id") != organization_id:
                continue
            current = readable_relationship_from_surreal_row(row)
            if current is None:
                continue
            if (
                evidence(current) == evidence(expected)
                and current.operational_source_binding == expected.operational_source_binding
                and current.operational_derivation_required
                == expected.operational_derivation_required
            ):
                unchanged[expected.id] = expected
    return unchanged
