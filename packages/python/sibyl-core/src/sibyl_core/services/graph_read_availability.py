"""Current stored graph rows eligible for ancestry-aware public readers."""

from __future__ import annotations

from collections.abc import Sequence

from sibyl_core.memory_pipeline.lifecycle import graph_metadata_recallable
from sibyl_core.models.entities import Entity
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
