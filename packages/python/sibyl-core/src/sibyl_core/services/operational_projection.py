"""Operational graph projections bound to an authorized retained raw source."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from sibyl_core.memory_pipeline.observations import SourceIdentity, SourceKind, SourceObservation
from sibyl_core.models.experience import OperationalExperience, OperationalExperienceProjection
from sibyl_core.models.memory_scope import MemoryScope
from sibyl_core.projection.experience import (
    MANIFEST_STATE_PENDING,
    operational_experience_manifest_with_state,
    project_operational_experience,
)
from sibyl_core.runtime_ports import get_source_authority_resolver
from sibyl_core.services.memory_derivations import raw_derivation_current
from sibyl_core.services.memory_source_validation import (
    SourceReadAuthority,
    resolve_current_source_authority,
)
from sibyl_core.services.observed_sources import load_authorized_source_snapshot
from sibyl_core.services.source_observations import SourceUnavailableError
from sibyl_core.services.source_state_store import RawSourceSnapshot


@dataclass(frozen=True, slots=True)
class OperationalProjectionSource:
    """An immutable evidence binding, reauthorized at each publication boundary."""

    observation: SourceObservation
    authority: SourceReadAuthority
    canonical_payload: str
    creator_id: str
    project_id: str

    async def current(self) -> tuple[RawSourceSnapshot, OperationalExperience]:
        resolver = get_source_authority_resolver()
        identity = self.observation.source
        authority = await resolve_current_source_authority(
            self.authority, identity.organization_id, resolver
        )
        if authority is None:
            raise SourceUnavailableError()
        snapshot = await load_authorized_source_snapshot(
            identity, authority, organization_id=identity.organization_id
        )
        if (
            not isinstance(snapshot, RawSourceSnapshot)
            or not self.observation.same_evidence(snapshot.observation)
            or snapshot.memory.raw_content != self.canonical_payload
            or snapshot.memory.principal_id != self.creator_id
            or snapshot.memory.project_id != self.project_id
            or not await raw_derivation_current(snapshot.memory, authority)
        ):
            raise SourceUnavailableError()
        return snapshot, await asyncio.to_thread(_experience, snapshot)

    async def projection(self) -> OperationalExperienceProjection:
        _, experience = await self.current()
        projection = await asyncio.to_thread(
            project_operational_experience,
            experience,
            organization_id=self.observation.source.organization_id,
            created_by=self.creator_id,
        )
        pending = operational_experience_manifest_with_state(projection, MANIFEST_STATE_PENDING)
        entities = []
        for projected in projection.entities:
            entity = pending if projected.id == pending.id else projected
            entities.append(
                entity.model_copy(
                    update={
                        "derivation_required": True,
                        "metadata": {
                            **entity.metadata,
                            "memory_scope": "project",
                            "scope_key": self.project_id,
                            "principal_id": self.authority.principal_id,
                        },
                    }
                )
            )
        return projection.model_copy(update={"entities": tuple(entities)})


def _experience(snapshot: RawSourceSnapshot) -> OperationalExperience:
    from sibyl_core.services.operational_capture import (
        SURFACE,
        OperationalSourceWrite,
        canonical_experience,
    )

    memory = snapshot.memory
    experience = OperationalExperience.model_validate_json(memory.raw_content)
    if not experience.project_id:
        raise SourceUnavailableError()
    intent = OperationalSourceWrite(
        organization_id=memory.organization_id,
        source_id=experience.source_id,
        principal_id=memory.principal_id,
        project_id=experience.project_id,
    )
    if (
        memory.id != intent.id
        or memory.source_id != experience.source_id
        or memory.project_id != experience.project_id
        or memory.scope_key != experience.project_id
        or memory.capture_surface != SURFACE
        or memory.memory_scope is not MemoryScope.PROJECT
        or not snapshot.observation.durable
        or snapshot.observation.source.kind is not SourceKind.RAW_CAPTURE
        or memory.raw_content != canonical_experience(experience)
    ):
        raise SourceUnavailableError()
    return experience


async def load_operational_projection_source(
    source: SourceIdentity, authority: SourceReadAuthority
) -> OperationalProjectionSource:
    snapshot = await load_authorized_source_snapshot(
        source, authority, organization_id=source.organization_id
    )
    if not isinstance(snapshot, RawSourceSnapshot):
        raise SourceUnavailableError()
    if not snapshot.memory.project_id:
        raise SourceUnavailableError()
    binding = OperationalProjectionSource(
        observation=snapshot.observation,
        authority=authority,
        canonical_payload=snapshot.memory.raw_content,
        creator_id=snapshot.memory.principal_id,
        project_id=snapshot.memory.project_id,
    )
    await binding.current()
    return binding
