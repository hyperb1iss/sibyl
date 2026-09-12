"""Current stored graph rows eligible for ancestry-aware public readers."""

from __future__ import annotations

from collections.abc import Sequence

from sibyl_core.memory_pipeline.lifecycle import graph_metadata_recallable
from sibyl_core.models.entities import Entity, Relationship
from sibyl_core.services.eval_publication_guards import unavailable_publication_ids
from sibyl_core.services.graph_runtime import GraphRuntime, get_surreal_graph_runtime

_READ_BATCH_SIZE = 512


async def available_graph_entities(
    organization_id: str,
    entity_ids: Sequence[str],
    *,
    runtime: GraphRuntime | None = None,
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
            if (
                row.id in batch
                and row.organization_id == organization_id
                and graph_metadata_recallable(row.metadata)
            ):
                current[row.id] = row
    unavailable = await unavailable_publication_ids(
        organization_id,
        {key: row.metadata for key, row in current.items()},
        graph_entities=current,
        graph_client=graph.client,
    )
    return {key: row for key, row in current.items() if key not in unavailable}


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
    )

    ids = list(dict.fromkeys(relationship_ids))
    if not ids:
        return {}
    graph = runtime or await get_surreal_graph_runtime(organization_id, ensure_schema=False)
    available = {}
    for start in range(0, len(ids), 512):
        batch = ids[start : start + 512]
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
        targets = {r["uuid"]: r for r in snapshot["targets"]}
        states = {r["source_id"]: r for r in snapshot["states"]}
        associations = {r["target_id"]: r for r in snapshot["associations"]}
        current = await available_graph_entities(organization_id, sorted(endpoints), runtime=graph)
        for row in snapshot["relationships"]:
            if row.get("source_uuid") not in current or row.get("target_uuid") not in current:
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
            ):
                available[row["uuid"]] = relationship_from_surreal_row(row)
    return available
