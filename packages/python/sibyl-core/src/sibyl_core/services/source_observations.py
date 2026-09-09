"""Authorized adapters for raw captures and graph source-state snapshots."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from sibyl_core.auth.memory_policy import (
    memory_metadata_read_allowed,
    memory_row_project_id,
    private_scope_granted_for,
)
from sibyl_core.memory_pipeline.lifecycle import (
    graph_metadata_recallable,
    raw_memory_lifecycle_recallable,
)
from sibyl_core.memory_pipeline.observations import (
    SourceIdentity,
    SourceKind,
    SourceObservation,
    evidence_hash,
)
from sibyl_core.memory_pipeline.source_lifecycle import correction_event
from sibyl_core.models.entities import Entity
from sibyl_core.services.content_models import RawMemory
from sibyl_core.services.memory_policy import _authorize_share_source_read
from sibyl_core.services.memory_source_validation import SourceReadAuthority


class SourceUnavailableError(ValueError):
    """Missing, inaccessible, stale and unverified sources share one denial."""

    def __init__(self) -> None:
        super().__init__("Source observation is unavailable.")


@dataclass(frozen=True, slots=True)
class GraphSourceSnapshot:
    """Entity and durable observation loaded together by the source-store owner.

    The adapter checks their binding, but cannot create durability. The storage
    implementation must load both in one source-local transaction. No fallback
    to an entity's mutable metadata or revision alone is permitted.
    """

    entity: Entity
    observation: SourceObservation
    deleted: bool = False


type GraphSnapshotLoader = Callable[[SourceIdentity], Awaitable[GraphSourceSnapshot | None]]


def graph_evidence(entity: Entity) -> str:
    return evidence_hash(
        {
            "version": 1,
            "entity_type": entity.entity_type.value,
            "name": entity.name,
            "description": entity.description,
            "content": entity.content,
        }
    )


def observe_raw_capture(memory: RawMemory, authority: SourceReadAuthority) -> SourceObservation:
    """Translate the existing raw content epoch without inventing durability.

    This compatibility observation binds live source content. Until purge and
    restore preserve a durable source-state row, it must not authorize replay
    across source deletion/reincarnation.
    """
    if (
        type(memory.observed_revision) is not int
        or memory.observed_revision != memory.revision
        or memory.deleted_at is not None
        or not raw_memory_lifecycle_recallable(memory)
        or not _authorize_share_source_read(
            memory=memory,
            principal_id=authority.principal_id,
            accessible_projects=authority.projects,
            accessible_teams=authority.teams,
            accessible_delegations=authority.delegations,
            allowed_memory_scope_keys=authority.scope_keys,
        ).allowed
    ):
        raise SourceUnavailableError()
    try:
        generation = correction_event(memory, blocking=False).content_revision
        return SourceObservation(
            source=SourceIdentity(memory.organization_id, SourceKind.RAW_CAPTURE, memory.id),
            generation=generation,
            content_sha256=evidence_hash({"version": 1, "raw_content": memory.raw_content}),
            revision=memory.revision,
            durable=False,
        )
    except ValueError as exc:
        raise SourceUnavailableError() from exc


def observe_graph_snapshot(
    snapshot: GraphSourceSnapshot, source: SourceIdentity, authority: SourceReadAuthority
) -> SourceObservation:
    entity, observation = snapshot.entity, snapshot.observation
    if (
        source.kind is not SourceKind.GRAPH_ENTITY
        or snapshot.deleted is not False
        or observation.source != source
        or not observation.durable
        or entity.id != source.id
        or entity.organization_id != source.organization_id
        or type(entity.observed_revision) is not int
        or entity.observed_revision != observation.revision
        or entity.revision != observation.revision
        or graph_evidence(entity) != observation.content_sha256
        or not graph_metadata_recallable(entity.metadata)
        or not memory_metadata_read_allowed(
            entity.metadata,
            principal_id=authority.principal_id,
            private_scope_granted=private_scope_granted_for(
                authority.scope_keys, principal_id=authority.principal_id
            ),
            accessible_projects=authority.projects,
            row_project_id=memory_row_project_id(
                entity.metadata, entity_type=entity.entity_type.value, entity_id=entity.id
            ),
            allowed_memory_scope_keys=authority.scope_keys,
        )
    ):
        raise SourceUnavailableError()
    return observation


async def load_source_observation(
    source: SourceIdentity,
    authority: SourceReadAuthority,
    *,
    organization_id: str,
    graph_loader: GraphSnapshotLoader | None = None,
) -> SourceObservation:
    """Resolve by explicit source kind; graph loading requires durable state."""
    if source.organization_id != organization_id:
        raise SourceUnavailableError()
    if source.kind is SourceKind.RAW_CAPTURE:
        from sibyl_core.services.content_raw_persistence import get_raw_memory

        memory = await get_raw_memory(memory_id=source.id, organization_id=source.organization_id)
        if (
            memory is None
            or memory.id != source.id
            or memory.organization_id != source.organization_id
        ):
            raise SourceUnavailableError()
        return observe_raw_capture(memory, authority)
    if graph_loader is None:
        raise SourceUnavailableError()
    snapshot = await graph_loader(source)
    if snapshot is None:
        raise SourceUnavailableError()
    return observe_graph_snapshot(snapshot, source, authority)
