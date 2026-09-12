"""Retain validated experience bytes before publishing or resuming projections."""

from __future__ import annotations

import asyncio

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict

from sibyl.services.operational_authority import (
    OperationalPublicationSource,
    OperationalWriteAuthority,
)
from sibyl_core.memory_pipeline.observations import SourceIdentity, SourceKind, SourceObservation
from sibyl_core.models.entities import Entity
from sibyl_core.models.experience import OperationalExperience
from sibyl_core.models.memory_scope import MemoryScope
from sibyl_core.services.operational_capture import (
    SURFACE,
    OperationalSourceWrite,
    canonical_experience,
)
from sibyl_core.services.operational_projection import load_operational_projection_source
from sibyl_core.services.source_observations import SourceUnavailableError
from sibyl_core.services.surreal_content import get_raw_memory, remember_raw_memory


class OperationalPublicationJob(BaseModel):
    """A deferred write retains the exact source observation and intake ceiling."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    observation: SourceObservation
    authority: OperationalWriteAuthority

    async def source(self) -> OperationalPublicationSource:
        if self.observation.source.organization_id != self.authority.organization_id:
            raise SourceUnavailableError
        source = await _load_source(self.observation.source, self.authority)
        if source.observation != self.observation:
            raise SourceUnavailableError
        return source


async def _load_source(
    identity: SourceIdentity, authority: OperationalWriteAuthority
) -> OperationalPublicationSource:
    retained = await load_operational_projection_source(identity, authority.read_ceiling())
    source = OperationalPublicationSource(
        observation=retained.observation,
        authority=retained.authority,
        canonical_payload=retained.canonical_payload,
        creator_id=retained.creator_id,
        project_id=retained.project_id,
        write_authority=authority,
    )
    await source.current()
    return source


async def retain_operational_experience(
    experience: OperationalExperience,
    authority: OperationalWriteAuthority,
    *,
    existing_manifest: Entity | None,
) -> OperationalPublicationSource:
    """Use stable raw identity, preserving its creator even before graph creation.

    Callers serialize the logical source with the existing operational lock.
    The raw writer still compares its own complete row and lifecycle snapshot.
    Unkeyed writes intentionally provide no replay guarantee across intervening
    source changes; an HTTP idempotency key binds an exact retry separately.
    """
    if experience.project_id != authority.project_id:
        raise SourceUnavailableError
    intent = OperationalSourceWrite(
        organization_id=authority.organization_id,
        source_id=experience.source_id,
        principal_id=authority.actor_id,
        project_id=authority.project_id,
    )
    raw = await get_raw_memory(organization_id=authority.organization_id, memory_id=intent.id)
    creator = raw.principal_id if raw is not None else authority.actor_id
    if raw is not None and raw.project_id != authority.project_id:
        raise HTTPException(409, "Operational source_id is already bound to another project")
    if existing_manifest is not None:
        if existing_manifest.metadata.get("project_id") != authority.project_id:
            raise HTTPException(409, "Operational source_id is already bound to another project")
        if not existing_manifest.created_by:
            raise SourceUnavailableError
        if raw is not None and creator != existing_manifest.created_by:
            raise SourceUnavailableError
        creator = existing_manifest.created_by
    await authority.current(creator_id=creator)
    canonical = await asyncio.to_thread(canonical_experience, experience)
    intent = OperationalSourceWrite(
        organization_id=authority.organization_id,
        source_id=experience.source_id,
        principal_id=creator,
        project_id=authority.project_id,
        expected_revision=raw.revision if raw is not None else None,
    )
    saved = await remember_raw_memory(
        organization_id=authority.organization_id,
        principal_id=creator,
        source_id=experience.source_id,
        raw_content=canonical,
        title=experience.goal,
        memory_scope=MemoryScope.PROJECT,
        scope_key=authority.project_id,
        metadata={"project_id": authority.project_id},
        capture_surface=SURFACE,
        embedding_provider=None,
        operational_write=intent,
    )
    await authority.current(creator_id=creator)
    return await _load_source(
        SourceIdentity(authority.organization_id, SourceKind.RAW_CAPTURE, saved.id), authority
    )
