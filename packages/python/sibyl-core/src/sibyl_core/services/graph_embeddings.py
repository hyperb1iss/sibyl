"""Embedding preparation shared by native graph entity and relationship writes."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence

import structlog

from sibyl_core.config import settings
from sibyl_core.embeddings.providers import (
    EmbeddingInputKind,
    EmbeddingProvider,
    entity_embedding_text,
    relationship_embedding_text,
)
from sibyl_core.models.entities import Entity, Relationship
from sibyl_core.services.graph_records import _metadata_float_list

log = structlog.get_logger()


async def _entity_with_native_embedding(
    entity: Entity,
    provider: EmbeddingProvider | None,
) -> Entity:
    if provider is None or entity.embedding:
        return entity
    embeddings = await _embed_texts_for_write(
        provider,
        [entity_embedding_text(entity)],
        operation="entity_create",
    )
    if embeddings is None:
        return entity
    embedding = _embedding_vector_from_batch(embeddings, provider.metadata.dimensions)
    metadata = {
        **dict(entity.metadata or {}),
        "embedding_metadata": provider.metadata.to_dict(),
    }
    return entity.model_copy(update={"embedding": embedding, "metadata": metadata})


async def _entities_with_native_embeddings(
    entities: Sequence[Entity],
    provider: EmbeddingProvider | None,
    *,
    batch_size: int,
) -> list[Entity]:
    if provider is None:
        return list(entities)

    updated_entities = list(entities)
    pending_indexes = [
        index for index, entity in enumerate(updated_entities) if not entity.embedding
    ]
    if not pending_indexes:
        return updated_entities

    dimensions = provider.metadata.dimensions
    for start in range(0, len(pending_indexes), max(int(batch_size), 1)):
        batch_indexes = pending_indexes[start : start + max(int(batch_size), 1)]
        embeddings = await _embed_texts_for_write(
            provider,
            [entity_embedding_text(updated_entities[index]) for index in batch_indexes],
            operation="entity_bulk_create",
        )
        if embeddings is None:
            continue
        if len(embeddings) != len(batch_indexes):
            raise ValueError(
                "embedding provider returned "
                f"{len(embeddings)} vectors for {len(batch_indexes)} entities"
            )
        for index, embedding_values in zip(batch_indexes, embeddings, strict=True):
            embedding = _embedding_vector_from_batch([embedding_values], dimensions)
            entity = updated_entities[index]
            metadata = {
                **dict(entity.metadata or {}),
                "embedding_metadata": provider.metadata.to_dict(),
            }
            updated_entities[index] = entity.model_copy(
                update={"embedding": embedding, "metadata": metadata}
            )

    return updated_entities


async def _relationship_with_native_embedding(
    relationship: Relationship,
    provider: EmbeddingProvider | None,
) -> Relationship:
    metadata = dict(relationship.metadata or {})
    if provider is None or _metadata_float_list(metadata.get("fact_embedding")):
        return relationship
    embeddings = await _embed_texts_for_write(
        provider,
        [relationship_embedding_text(relationship)],
        operation="relationship_create",
    )
    if embeddings is None:
        return relationship
    metadata["fact_embedding"] = _embedding_vector_from_batch(
        embeddings,
        provider.metadata.dimensions,
    )
    metadata["embedding_metadata"] = provider.metadata.to_dict()
    return relationship.model_copy(update={"metadata": metadata})


async def _relationships_with_native_embeddings(
    relationships: Sequence[Relationship],
    provider: EmbeddingProvider | None,
    *,
    batch_size: int,
) -> list[Relationship]:
    if provider is None:
        return list(relationships)

    updated = list(relationships)
    pending_indexes = [
        index
        for index, relationship in enumerate(updated)
        if not _metadata_float_list(dict(relationship.metadata or {}).get("fact_embedding"))
    ]
    if not pending_indexes:
        return updated

    dimensions = provider.metadata.dimensions
    for start in range(0, len(pending_indexes), max(int(batch_size), 1)):
        batch_indexes = pending_indexes[start : start + max(int(batch_size), 1)]
        embeddings = await _embed_texts_for_write(
            provider,
            [relationship_embedding_text(updated[index]) for index in batch_indexes],
            operation="relationship_bulk_create",
        )
        if embeddings is None:
            continue
        if len(embeddings) != len(batch_indexes):
            raise ValueError(
                "embedding provider returned "
                f"{len(embeddings)} vectors for {len(batch_indexes)} relationships"
            )
        for index, embedding_values in zip(batch_indexes, embeddings, strict=True):
            relationship = updated[index]
            metadata = dict(relationship.metadata or {})
            metadata["fact_embedding"] = _embedding_vector_from_batch(
                [embedding_values], dimensions
            )
            metadata["embedding_metadata"] = provider.metadata.to_dict()
            updated[index] = relationship.model_copy(update={"metadata": metadata})

    return updated


async def _embed_texts_for_write(
    provider: EmbeddingProvider,
    texts: Sequence[str],
    *,
    operation: str,
) -> list[list[float]] | None:
    started = time.perf_counter()
    try:
        return await _embed_texts_with_timeout(
            provider,
            texts,
            input_kind="document",
            operation=operation,
        )
    except Exception as exc:
        log.warning(
            "graph_embedding_failed",
            operation=operation,
            provider=provider.metadata.provider,
            model=provider.metadata.model,
            items=len(texts),
            timeout_seconds=settings.graph_embedding_timeout_seconds,
            elapsed_ms=round((time.perf_counter() - started) * 1000, 2),
            error_type=type(exc).__name__,
        )
        return None


async def _embed_texts_with_timeout(
    provider: EmbeddingProvider,
    texts: Sequence[str],
    *,
    input_kind: EmbeddingInputKind,
    operation: str,
    timeout_seconds: float | None = None,
) -> list[list[float]]:
    timeout_seconds = (
        settings.graph_embedding_timeout_seconds if timeout_seconds is None else timeout_seconds
    )
    started = time.perf_counter()
    if timeout_seconds > 0:
        embeddings = await asyncio.wait_for(
            provider.embed_texts(texts, input_kind=input_kind),
            timeout=timeout_seconds,
        )
    else:
        embeddings = await provider.embed_texts(texts, input_kind=input_kind)

    log.info(
        "graph_embedding_complete",
        operation=operation,
        provider=provider.metadata.provider,
        model=provider.metadata.model,
        items=len(texts),
        timeout_seconds=timeout_seconds,
        elapsed_ms=round((time.perf_counter() - started) * 1000, 2),
    )
    return embeddings


def _embedding_vector_from_batch(
    embeddings: Sequence[Sequence[float]],
    dimensions: int,
) -> list[float]:
    if not embeddings:
        raise ValueError("embedding provider returned no vectors")
    embedding = [float(value) for value in embeddings[0]]
    if len(embedding) != dimensions:
        raise ValueError(
            f"embedding provider returned {len(embedding)} dimensions, expected {dimensions}"
        )
    return embedding
