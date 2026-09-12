"""Publish distilled operational notes through the retained-source transaction."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from sibyl_core.ai.operational_distillation import (
    OperationalDistillationOutput,
    OperationalNoteDistillationProfile,
    admit_observed_operational_absence,
    build_operational_experience_digest_with_receipt,
    build_operational_note_entities_with_receipt,
    operational_distilled_note_id,
)
from sibyl_core.services.graph_derivations import graph_target_digest
from sibyl_core.services.graph_embeddings import _entities_with_native_embeddings
from sibyl_core.services.graph_entity_store import _publish_operational_inventory
from sibyl_core.services.source_observations import SourceUnavailableError

if TYPE_CHECKING:
    from sibyl_core.services.operational_projection import OperationalProjectionSource


async def publish_operational_notes(
    manager,
    source: OperationalProjectionSource,
    notes: OperationalDistillationOutput,
    *,
    provider: str | None,
    model: str | None,
    profile: OperationalNoteDistillationProfile,
):
    """Derive note identity and scope from current evidence, never queued entities."""
    _, experience = await source.current()
    projection = await source.projection()
    _, digest_receipt = await asyncio.to_thread(
        build_operational_experience_digest_with_receipt, experience, profile=profile
    )
    admitted, absence_receipt = admit_observed_operational_absence(
        notes, digest_receipt=digest_receipt, profile=profile
    )
    entities, render_receipt = await asyncio.to_thread(
        build_operational_note_entities_with_receipt,
        notes,
        experience=experience,
        organization_id=source.observation.source.organization_id,
        created_by=source.creator_id,
        content_hash=projection.manifest.content_hash,
        provider=provider,
        model=model,
        profile=profile,
        admitted_observed_absence=admitted,
    )
    kinds = (
        ("workflow", "facts", "gotchas", "observed_absence")
        if profile == "render_v1"
        else ("workflow", "facts", "gotchas")
    )
    inventory_ids = [operational_distilled_note_id(experience.source_id, kind) for kind in kinds]
    current = {entity.id: entity for entity in await manager.get_many(inventory_ids)}
    if any(not entity.derivation_required for entity in current.values()):
        raise SourceUnavailableError()
    prepared = []
    for entity in entities:
        entity = entity.model_copy(
            update={
                "derivation_required": True,
                "metadata": {
                    **entity.metadata,
                    "memory_scope": "project",
                    "scope_key": source.project_id,
                    "principal_id": source.authority.principal_id,
                },
            }
        )
        old = current.get(entity.id)
        if old is not None and not old.derivation_required:
            raise SourceUnavailableError()
        if (
            old is not None
            and graph_target_digest(old) == graph_target_digest(entity)
            and manager._embedding_provider is not None
            and old.metadata.get("embedding_metadata")
            == manager._embedding_provider.metadata.to_dict()
        ):
            entity = entity.model_copy(
                update={
                    "embedding": old.embedding,
                    "metadata": {
                        **entity.metadata,
                        "embedding_metadata": old.metadata.get("embedding_metadata"),
                    },
                }
            )
        prepared.append(entity)
    await source.current()
    prepared = await _entities_with_native_embeddings(
        prepared, manager._embedding_provider, batch_size=64
    )
    await source.current()
    emitted = {entity.id for entity in prepared}
    retired = tuple(
        operational_distilled_note_id(experience.source_id, kind)
        for kind in kinds
        if operational_distilled_note_id(experience.source_id, kind) not in emitted
    )
    await _publish_operational_inventory(
        manager._client,
        source,
        projection.model_copy(update={"entities": tuple(prepared)}),
        group_id=manager._group_id,
        retired_ids=retired,
    )
    return prepared, {
        "retired_note_ids": sorted(set(retired) & set(current)),
        "profile": profile,
        "digest": digest_receipt,
        "render": render_receipt,
        "observed_absence": absence_receipt,
    }
