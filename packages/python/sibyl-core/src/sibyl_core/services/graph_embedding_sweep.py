"""The graph plane of the embedding sweep: entity and relationship vectors.

Both tables are embedded by the configured graph provider and stamp
``attributes.embedding_metadata`` with that provider's metadata, so the
plane's configured stamp is simply ``provider.metadata.to_dict()``.

On its own a graph plane's legacy verdict weighs the stamps its namespace
carried when it upgraded; lifecycle repair also supplies the deployment's
model record and the chunk plane's evidence (see ``embedding_verdicts``).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from typing import TYPE_CHECKING, Any, cast

from sibyl_core.backends.surreal.schema import EMBEDDING_DIM
from sibyl_core.backends.surreal.schema_embedding_states import (
    GRAPH_EMBEDDING_STATE_PLANE,
    embedding_sweep_schema_ready,
)
from sibyl_core.backends.surreal.schema_version import get_schema_embedding_dimension
from sibyl_core.embeddings.providers import (
    EmbeddingProvider,
    configured_embedding_provider,
    entity_embedding_text,
    relationship_embedding_text,
)
from sibyl_core.services.embedding_evidence import classify_stamps, read_graph_snapshot
from sibyl_core.services.embedding_sweep import (
    SWEEP_SKIPPED_NO_PROVIDER,
    SWEEP_SKIPPED_SCHEMA_PENDING,
    EmbeddingSchemaPendingError,
    EmbeddingStamp,
    EmbeddingSweepResult,
    LegacyEvidence,
    SweepPlane,
    SweepRow,
    SweepTable,
    count_plane_reembed,
    ensure_legacy_decision,
    mark_plane_for_reembed,
    run_embedding_sweep,
)
from sibyl_core.services.graph_embeddings import _embed_texts_with_timeout
from sibyl_core.services.graph_records import (
    entity_from_surreal_row,
    relationship_from_surreal_row,
)

if TYPE_CHECKING:
    from sibyl_core.services.graph_runtime import GraphRuntime

GRAPH_EMBEDDING_PLANE = GRAPH_EMBEDDING_STATE_PLANE
_AUTO = object()

type EvidenceSource = Callable[[], Awaitable[LegacyEvidence]]

_ENTITY_FENCE = (
    "entity_type = $rows_by_uuid[uuid].entity_type "
    "AND name = $rows_by_uuid[uuid].name "
    "AND (description ?? '') = ($rows_by_uuid[uuid].description ?? '') "
    "AND (content ?? '') = ($rows_by_uuid[uuid].content ?? '') "
    "AND (attributes.summary ?? '') = ($rows_by_uuid[uuid].summary ?? '')"
)
_RELATIONSHIP_FENCE = (
    "name = $rows_by_uuid[uuid].name "
    "AND fact = $rows_by_uuid[uuid].fact "
    "AND (attributes.fact ?? '') = ($rows_by_uuid[uuid].attribute_fact ?? '')"
)


def entity_sweep_table(dimensions: int) -> SweepTable:
    return SweepTable(
        name="entity",
        scope_field="group_id",
        vector_field="name_embedding",
        metadata_path="attributes.embedding_metadata",
        projection="entity_type, name, description, content, attributes.summary AS summary",
        fence=_ENTITY_FENCE,
        dimensions=dimensions,
    )


def relationship_sweep_table(dimensions: int) -> SweepTable:
    return SweepTable(
        name="relates_to",
        scope_field="group_id",
        vector_field="fact_embedding",
        metadata_path="attributes.embedding_metadata",
        projection="name, fact, source_id, target_id, attributes.fact AS attribute_fact",
        fence=_RELATIONSHIP_FENCE,
        dimensions=dimensions,
    )


def entity_row_embedding_text(row: SweepRow) -> str:
    """The text the write path embeds, rebuilt from the stored columns."""
    return entity_embedding_text(
        entity_from_surreal_row(
            {
                "uuid": row["uuid"],
                "entity_type": row.get("entity_type"),
                "name": row.get("name") or "",
                "description": row.get("description"),
                "content": row.get("content"),
                "attributes": {"summary": row.get("summary")},
            }
        )
    )


def relationship_row_embedding_text(row: SweepRow) -> str:
    return relationship_embedding_text(
        relationship_from_surreal_row(
            {
                "uuid": row["uuid"],
                "name": row.get("name"),
                "fact": row.get("fact") or "",
                "source_id": row.get("source_id"),
                "target_id": row.get("target_id"),
                "attributes": (
                    {"fact": row["attribute_fact"]} if row.get("attribute_fact") else {}
                ),
            }
        )
    )


async def graph_embedding_plane(
    client: Any,
    provider: EmbeddingProvider,
    *,
    evidence: EvidenceSource | None = None,
) -> SweepPlane:
    """Build the graph plane for one organization's namespace.

    The schema dimension is the one the namespace recorded for its vector
    fields, which can lag the configured size on an embedded store that
    skipped a rebuild. ``evidence`` replaces the default, which weighs only
    this namespace's pre-upgrade stamps.
    """
    recorded = await get_schema_embedding_dimension(client.execute_query)
    dimensions = recorded or EMBEDDING_DIM
    tables = (entity_sweep_table(dimensions), relationship_sweep_table(dimensions))
    stamp: EmbeddingStamp = provider.metadata.to_dict()
    group_id = str(client.group_id)

    async def embed(
        table: SweepTable, rows: Sequence[SweepRow]
    ) -> tuple[list[list[float]], EmbeddingStamp]:
        text_of = (
            entity_row_embedding_text if table.name == "entity" else relationship_row_embedding_text
        )
        vectors = await _embed_texts_with_timeout(
            provider,
            [text_of(row) for row in rows],
            input_kind="document",
            operation=f"embedding_sweep_{table.name}",
        )
        return [[float(value) for value in vector] for vector in vectors], dict(stamp)

    async def own_evidence() -> LegacyEvidence:
        differs, matches = classify_stamps(
            await read_graph_snapshot(client.execute_query, group_id), stamp
        )
        return LegacyEvidence(differs=differs, matches=matches)

    return SweepPlane(
        name=GRAPH_EMBEDDING_PLANE,
        organization_id=group_id,
        execute=client.execute_query,
        tables=tables,
        stamp=stamp,
        embed=embed,
        provider_dimensions=provider.metadata.dimensions,
        schema_dimensions=dimensions,
        evidence=evidence or own_evidence,
    )


def _resolve_provider(embedding_provider: object) -> EmbeddingProvider | None:
    if embedding_provider is _AUTO:
        return configured_embedding_provider()
    return cast("EmbeddingProvider | None", embedding_provider)


async def decide_graph_legacy_vectors(
    runtime: GraphRuntime,
    *,
    embedding_provider: object = _AUTO,
    evidence: EvidenceSource | None = None,
) -> dict[str, Any] | None:
    """Record the graph plane's legacy verdict if no pass has yet."""
    provider = _resolve_provider(embedding_provider)
    if provider is None:
        return None
    await require_graph_sweep_schema(runtime.client)
    return await ensure_legacy_decision(
        await graph_embedding_plane(runtime.client, provider, evidence=evidence)
    )


async def require_graph_sweep_schema(client: Any) -> None:
    """Refuse to touch graph embedding evidence before the namespace's upgrade snapshot."""
    if not await embedding_sweep_schema_ready(client.execute_query, graph=True):
        raise EmbeddingSchemaPendingError(
            f"graph namespace for {client.group_id} has not run its embedding sweep migration"
        )


async def sweep_graph_embeddings(
    runtime: GraphRuntime,
    *,
    embedding_provider: object = _AUTO,
    **options: Any,
) -> EmbeddingSweepResult:
    """Re-embed entity and relationship vectors from another model.

    ``options`` forwards pass tuning (budget, page, batch, concurrency) to
    ``run_embedding_sweep``; production uses the configured defaults.
    """
    provider = _resolve_provider(embedding_provider)
    if provider is None:
        return EmbeddingSweepResult(plane=GRAPH_EMBEDDING_PLANE, status=SWEEP_SKIPPED_NO_PROVIDER)
    if not await embedding_sweep_schema_ready(runtime.client.execute_query, graph=True):
        return EmbeddingSweepResult(
            plane=GRAPH_EMBEDDING_PLANE, status=SWEEP_SKIPPED_SCHEMA_PENDING
        )
    plane = await graph_embedding_plane(runtime.client, provider)
    return await run_embedding_sweep(plane, **options)


async def _graph_tables(client: Any) -> tuple[SweepTable, SweepTable]:
    recorded = await get_schema_embedding_dimension(client.execute_query)
    dimensions = recorded or EMBEDDING_DIM
    return entity_sweep_table(dimensions), relationship_sweep_table(dimensions)


async def mark_graph_embeddings_for_reembed(client: Any) -> int:
    """Queue every entity and relationship vector for the sweep to replace."""
    return await mark_plane_for_reembed(
        plane=GRAPH_EMBEDDING_PLANE,
        organization_id=str(client.group_id),
        execute=client.execute_query,
        tables=await _graph_tables(client),
    )


async def count_graph_embeddings_for_reembed(client: Any) -> int:
    """How many entity and relationship rows a re-embed would replace."""
    return await count_plane_reembed(
        organization_id=str(client.group_id),
        execute=client.execute_query,
        tables=await _graph_tables(client),
    )


__all__ = [
    "GRAPH_EMBEDDING_PLANE",
    "EvidenceSource",
    "count_graph_embeddings_for_reembed",
    "decide_graph_legacy_vectors",
    "entity_row_embedding_text",
    "entity_sweep_table",
    "graph_embedding_plane",
    "mark_graph_embeddings_for_reembed",
    "relationship_row_embedding_text",
    "relationship_sweep_table",
    "require_graph_sweep_schema",
    "sweep_graph_embeddings",
]
