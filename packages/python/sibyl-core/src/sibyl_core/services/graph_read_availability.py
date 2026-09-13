"""Current stored graph rows eligible for ancestry-aware public readers."""

from __future__ import annotations

from collections.abc import Sequence

from sibyl_core.models.entities import Entity, Relationship
from sibyl_core.services.eval_publication_guards import available_graph_entity_rows
from sibyl_core.services.graph_read_validation import GraphReadValidation
from sibyl_core.services.graph_runtime import GraphRuntime, get_surreal_graph_runtime

_READ_BATCH_SIZE = 512


async def available_graph_entities(
    organization_id: str,
    entity_ids: Sequence[str],
    *,
    runtime: GraphRuntime | None = None,
    read: GraphReadValidation | None = None,
) -> dict[str, Entity]:
    """Refresh actual rows and reject missing, retired, or unavailable ancestry.

    A supplied runtime must be the existing GraphRuntime for this organization.
    Callers retain their principal/project scope filters and render these current
    rows, rather than cached values with the same IDs.
    """
    ids = list(dict.fromkeys(entity_ids))
    if not ids:
        return {}
    graph = runtime or await get_surreal_graph_runtime(organization_id, ensure_schema=False)
    current: dict[str, Entity] = {}
    for offset in range(0, len(ids), _READ_BATCH_SIZE):
        batch = ids[offset : offset + _READ_BATCH_SIZE]
        rows = await graph.entity_manager.get_many(batch)
        for row in rows:
            if row.id in batch:
                current[row.id] = row
    return await available_graph_entity_rows(
        organization_id, current, graph_client=graph.client, read=read
    )


async def available_graph_relationships(
    organization_id: str,
    relationship_ids: Sequence[str],
    *,
    runtime: GraphRuntime | None = None,
) -> dict[str, Relationship]:
    """Refresh stored edges and require their protected operational generation."""
    from sibyl_core.backends.surreal.records import normalize_records
    from sibyl_core.services.graph_records import (
        entity_from_surreal_row,
        relationship_from_surreal_row,
    )
    from sibyl_core.services.operational_relationships import (
        _snapshot,
        operational_relationship_current,
        relationship_body_digest,
    )

    ids = list(dict.fromkeys(relationship_ids))
    if not ids:
        return {}
    graph = runtime or await get_surreal_graph_runtime(organization_id, ensure_schema=False)
    captured = []
    all_endpoints: set[str] = set()
    for start in range(0, len(ids), _READ_BATCH_SIZE):
        batch = ids[start : start + _READ_BATCH_SIZE]
        initial = normalize_records(
            await graph.client.execute_query(
                "SELECT in.uuid AS source_uuid,out.uuid AS target_uuid FROM relates_to "
                "WHERE group_id=$org AND uuid IN $ids;",
                org=organization_id,
                ids=batch,
            )
        )
        endpoints = {
            value
            for r in initial
            for k in ("source_uuid", "target_uuid")
            if isinstance(value := r.get(k), str)
        }
        snapshot = await _snapshot(
            graph.client, organization_id=organization_id, ids=endpoints, relationship_ids=batch
        )

        captured.append((batch, endpoints, snapshot))
        all_endpoints.update(endpoints)

    async def validate(snapshots, read):
        result = {}
        current = await available_graph_entities(
            organization_id, sorted(all_endpoints), runtime=graph, read=read
        )
        for snapshot in snapshots:
            targets = {r["uuid"]: r for r in snapshot["targets"]}
            states = {r["source_id"]: r for r in snapshot["states"]}
            associations = {r["target_id"]: r for r in snapshot["associations"]}
            for row in snapshot["relationships"]:
                if any(
                    row.get(key) not in current or row.get(key) not in targets
                    for key in ("source_uuid", "target_uuid")
                ):
                    continue
                if any(
                    current[endpoint].model_dump(mode="json")
                    != entity_from_surreal_row(targets[endpoint]).model_dump(mode="json")
                    or current[endpoint].derivation_required
                    != entity_from_surreal_row(targets[endpoint]).derivation_required
                    or current[endpoint].observed_revision
                    != entity_from_surreal_row(targets[endpoint]).observed_revision
                    for endpoint in (row["source_uuid"], row["target_uuid"])
                ):
                    continue
                if await operational_relationship_current(
                    row,
                    targets=targets,
                    states=states,
                    associations=associations,
                    organization_id=organization_id,
                    read=read,
                ):
                    result[row["uuid"]] = relationship_from_surreal_row(row)
        return result

    first = await validate(
        [snapshot for _, _, snapshot in captured], GraphReadValidation(organization_id)
    )
    final_snapshots = []
    for batch, endpoints, snapshot in captured:
        fresh = await _snapshot(
            graph.client, organization_id=organization_id, ids=endpoints, relationship_ids=batch
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
        fresh["relationships"] = [
            row
            for row in fresh["relationships"]
            if row["uuid"] in first
            and not {row.get("source_uuid"), row.get("target_uuid")} & changed_associations
            and relationship_body_digest(row) == relationship_body_digest(originals[row["uuid"]])
            and row.get("operational_source_binding")
            == originals[row["uuid"]].get("operational_source_binding")
            and row.get("operational_derivation_required")
            == originals[row["uuid"]].get("operational_derivation_required")
        ]
        final_snapshots.append(fresh)
    return await validate(final_snapshots, GraphReadValidation(organization_id))
