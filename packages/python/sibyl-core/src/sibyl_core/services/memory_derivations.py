"""Protected observations validated by the existing admission and read gates."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import asdict, replace

from sibyl_core.auth.memory_policy import memory_scope_policy_key
from sibyl_core.memory_pipeline.observations import SourceIdentity, SourceKind, SourceObservation
from sibyl_core.services import content_client
from sibyl_core.services.content_models import MemoryScope
from sibyl_core.services.memory_source_validation import SourceReadAuthority
from sibyl_core.services.observed_sources import load_authorized_source_snapshot
from sibyl_core.services.source_observations import SourceUnavailableError
from sibyl_core.services.source_state_store import RawSourceSnapshot


def observation_from_record(value: object) -> SourceObservation:
    if not isinstance(value, Mapping) or not isinstance(value.get("source"), Mapping):
        raise SourceUnavailableError()
    source = value["source"]
    try:
        return SourceObservation(
            source=SourceIdentity(
                source["organization_id"], SourceKind(source["kind"]), source["id"]
            ),
            generation=value["generation"],
            content_sha256=value["content_sha256"],
            revision=value["revision"],
            durable=value["durable"],
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise SourceUnavailableError() from exc


def raw_derivation_record(
    memory, observations: Sequence[SourceObservation], authority: SourceReadAuthority
) -> dict[str, object]:
    if not observations or any(
        not observation.durable or observation.source.organization_id != memory.organization_id
        for observation in observations
    ):
        raise SourceUnavailableError()
    return {
        "organization_id": memory.organization_id,
        "target_kind": SourceKind.RAW_CAPTURE.value,
        "target_id": memory.id,
        "body_sha256": hashlib.sha256(memory.raw_content.encode()).hexdigest(),
        "principal_id": authority.principal_id,
        "authority_ceiling": authority.ceiling_metadata(),
        "observations": [asdict(observation) for observation in observations],
        "active": True,
    }


async def load_raw_derivation(organization_id: str, memory_id: str) -> dict[str, object] | None:
    async with content_client.surreal_content_client() as client:
        return await content_client.select_one(
            client,
            "SELECT * FROM memory_derivations WHERE organization_id=$org AND target_kind='raw_capture' AND target_id=$id LIMIT 1;",
            org=organization_id,
            id=memory_id,
        )


async def validate_observations(
    observations: Sequence[SourceObservation],
    authority: SourceReadAuthority,
    *,
    organization_id: str,
    ancestors: frozenset[SourceIdentity] = frozenset(),
) -> bool:
    async def current(observation: SourceObservation) -> bool:
        if observation.source in ancestors or not observation.durable:
            return False
        try:
            snapshot = await load_authorized_source_snapshot(
                observation.source, authority, organization_id=organization_id
            )
        except SourceUnavailableError:
            return False
        if not observation.same_evidence(snapshot.observation):
            return False
        if isinstance(snapshot, RawSourceSnapshot):
            return await raw_derivation_current(
                snapshot.memory, authority, ancestors=ancestors | {observation.source}
            )
        return True

    return all(await asyncio.gather(*(current(observation) for observation in observations)))


async def raw_derivation_current(
    memory, authority: SourceReadAuthority, *, ancestors: frozenset[SourceIdentity] = frozenset()
) -> bool:
    association = await load_raw_derivation(memory.organization_id, memory.id)
    return await _raw_association_current(memory, association, authority, ancestors=ancestors)


async def _raw_association_current(
    memory, association, authority: SourceReadAuthority, *, ancestors: frozenset[SourceIdentity]
) -> bool:
    if association is None:
        return memory.capture_surface != "synthesis_artifact"
    if (
        association.get("active") is not True
        or association.get("body_sha256") != hashlib.sha256(memory.raw_content.encode()).hexdigest()
    ):
        return False
    from sibyl_core.services.memory_source_validation import (
        SOURCE_VALIDATION_CONTEXT_KEY,
        _saved_ceiling,
    )

    ceiling = _saved_ceiling(
        replace(
            memory, metadata={SOURCE_VALIDATION_CONTEXT_KEY: association.get("authority_ceiling")}
        )
    )
    if ceiling is None or association.get("principal_id") != memory.principal_id:
        return False
    scopes = ceiling.scope_keys
    if authority.scope_keys is not None:
        scopes = authority.scope_keys if scopes is None else scopes & authority.scope_keys
    authority = SourceReadAuthority(
        principal_id=authority.principal_id,
        projects=authority.projects & ceiling.projects,
        teams=authority.teams & ceiling.teams,
        delegations=authority.delegations & ceiling.delegations,
        scope_keys=scopes,
    )
    values = association.get("observations")
    if not isinstance(values, list) or not values:
        return False
    try:
        observations = [observation_from_record(value) for value in values]
    except SourceUnavailableError:
        return False
    return await validate_observations(
        observations, authority, organization_id=memory.organization_id, ancestors=ancestors
    )


async def unavailable_raw_derivation_ids(
    organization_id: str,
    memories,
    authority: SourceReadAuthority,
) -> set[str]:
    """Use the existing final retrieval gate with a batched target snapshot."""
    from sibyl_core.memory_pipeline.lifecycle import raw_memory_lifecycle_recallable
    from sibyl_core.services.content_models import raw_memory_from_record
    from sibyl_core.services.source_observations import observe_raw_capture

    if not memories:
        return set()
    unavailable = set()
    by_id = {memory.id: memory for memory in memories}
    async with content_client.surreal_content_client() as client:
        for batch in content_client.value_batches(sorted(by_id)):
            rows = content_client.normalize_records(
                await client.execute_query(
                    """RETURN {
                    RETURN {
                        targets: (SELECT * FROM raw_captures WHERE organization_id=$org AND uuid IN $ids),
                        associations: (SELECT * FROM memory_derivations WHERE organization_id=$org
                            AND target_kind='raw_capture' AND target_id IN $ids)
                    };
                };""",
                    org=organization_id,
                    ids=batch,
                )
            )
            if len(rows) != 1:
                raise RuntimeError("derivation retrieval snapshot is unavailable")
            target_rows = rows[0].get("targets")
            association_rows = rows[0].get("associations")
            if not isinstance(target_rows, list) or not isinstance(association_rows, list):
                raise RuntimeError("derivation retrieval snapshot is malformed")
            targets = {row["uuid"]: raw_memory_from_record(row) for row in target_rows}
            associations = {row["target_id"]: row for row in association_rows}

            async def current(memory_id, targets=targets, associations=associations):
                memory = targets.get(memory_id)
                if memory is None or memory.raw_content != by_id[memory_id].raw_content:
                    return memory_id
                if memory.memory_scope is MemoryScope.ORGANIZATION:
                    # Imported organization captures already passed the scoped
                    # recall query. Promotion-source admission excludes this
                    # legacy scope, so validate its current audience separately.
                    candidate = by_id[memory_id]
                    if (
                        candidate.memory_scope is not MemoryScope.ORGANIZATION
                        or memory.organization_id != candidate.organization_id
                        or memory.scope_key != candidate.scope_key
                        or memory.project_id != candidate.project_id
                        or memory.agent_id != candidate.agent_id
                        or (
                            authority.scope_keys is not None
                            and memory_scope_policy_key(memory.memory_scope, memory.scope_key)
                            not in authority.scope_keys
                        )
                    ):
                        return memory_id
                else:
                    try:
                        observe_raw_capture(memory, authority)
                    except SourceUnavailableError:
                        return memory_id
                if not raw_memory_lifecycle_recallable(memory):
                    return memory_id
                # Ordinary authored captures require no source traversal.
                if memory_id not in associations and memory.capture_surface != "synthesis_artifact":
                    return None
                if not await _raw_association_current(
                    memory, associations.get(memory_id), authority, ancestors=frozenset()
                ):
                    return memory_id
                return None

            unavailable.update(
                value
                for value in await asyncio.gather(*(current(memory_id) for memory_id in batch))
                if value is not None
            )
    return unavailable
