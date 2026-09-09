"""Validate graph publication ancestry through the existing final read gate."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

from sibyl_core.backends.surreal.records import normalize_records
from sibyl_core.memory_pipeline.observations import SourceIdentity, SourceKind, evidence_hash
from sibyl_core.runtime_ports import RuntimePortUnavailable, get_source_authority_resolver
from sibyl_core.services.memory_derivations import observation_from_record, validate_observations
from sibyl_core.services.memory_source_validation import source_authority_ceiling
from sibyl_core.services.source_observations import SourceUnavailableError, graph_evidence


def graph_target_digest(entity) -> str:
    """Bind published evidence and audience, excluding publication bookkeeping."""
    return evidence_hash(
        {
            "version": 1,
            "evidence": graph_evidence(entity),
            "audience": {
                key: entity.metadata.get(key)
                for key in ("memory_scope", "scope_key", "principal_id", "project_id")
            },
        }
    )


async def graph_association_current(entity, association, *, ancestors=frozenset()) -> bool:
    if association is None:
        return True
    if association.get("active") is not True or association.get(
        "body_sha256"
    ) != graph_target_digest(entity):
        return False
    principal_id = association.get("principal_id")
    if not isinstance(principal_id, str):
        return False
    ceiling = source_authority_ceiling(association.get("authority_ceiling"), principal_id)
    if ceiling is None:
        return False
    try:
        resolver = get_source_authority_resolver()
    except RuntimePortUnavailable:
        return False
    authority = await resolver(entity.organization_id, principal_id)
    if authority is None or authority.principal_id != principal_id:
        return False
    scopes = ceiling.scope_keys
    if authority.scope_keys is not None:
        scopes = authority.scope_keys if scopes is None else scopes & authority.scope_keys
    from dataclasses import replace

    authority = replace(
        authority,
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
        observations, authority, organization_id=entity.organization_id, ancestors=ancestors
    )


async def graph_derivation_current(entity, *, ancestors=frozenset()) -> bool:
    from sibyl_core.services.graph_runtime import get_surreal_graph_runtime

    runtime = await get_surreal_graph_runtime(entity.organization_id)
    rows = normalize_records(
        await runtime.client.execute_query(
            "SELECT * FROM memory_derivations WHERE organization_id=$org AND target_kind='graph_entity' AND target_id=$id LIMIT 1;",
            org=entity.organization_id,
            id=entity.id,
        )
    )
    return await graph_association_current(entity, rows[0] if rows else None, ancestors=ancestors)


async def unavailable_graph_derivation_ids(organization_id: str, ids: Sequence[str]) -> set[str]:
    from sibyl_core.services.graph_records import entity_from_surreal_row

    if not ids:
        return set()
    from sibyl_core.services.graph_runtime import get_surreal_graph_runtime

    runtime = await get_surreal_graph_runtime(organization_id)
    snapshots = normalize_records(
        await runtime.client.execute_query(
            """RETURN {
            RETURN {
                associations: (SELECT * FROM memory_derivations WHERE organization_id=$org
                    AND target_kind='graph_entity' AND target_id IN $ids),
                targets: (SELECT * FROM entity WHERE group_id=$org AND uuid IN $ids)
            };
        };""",
            org=organization_id,
            ids=list(ids),
        )
    )
    if len(snapshots) != 1:
        raise RuntimeError("graph derivation snapshot unavailable")
    target_rows = snapshots[0].get("targets")
    association_rows = snapshots[0].get("associations")
    if not isinstance(target_rows, list) or not isinstance(association_rows, list):
        raise RuntimeError("graph derivation snapshot unavailable")
    targets = {row["uuid"]: entity_from_surreal_row(row) for row in target_rows}

    async def current(association):
        target_id = association["target_id"]
        entity = targets.get(target_id)
        identity = SourceIdentity(organization_id, SourceKind.GRAPH_ENTITY, target_id)
        if entity is None or not await graph_association_current(
            entity, association, ancestors=frozenset({identity})
        ):
            return target_id
        return None

    return {
        value
        for value in await asyncio.gather(*(current(row) for row in association_rows))
        if value is not None
    }


async def load_graph_projection_source(client, *, organization_id: str, source_id: str):
    """Capture the protected parent snapshot before the projection cuts text."""
    from sibyl_core.memory_pipeline.lifecycle import graph_metadata_recallable
    from sibyl_core.services.source_observations import GraphSourceSnapshot
    from sibyl_core.services.source_state_store import load_source_snapshot

    associations = normalize_records(
        await client.execute_query(
            "SELECT * FROM memory_derivations WHERE organization_id=$org AND target_kind='graph_entity' AND target_id=$id LIMIT 1;",
            org=organization_id,
            id=source_id,
        )
    )
    if not associations:
        return None
    association = associations[0]
    snapshot = await load_source_snapshot(
        SourceIdentity(organization_id, SourceKind.GRAPH_ENTITY, source_id),
        organization_id=organization_id,
        execute_query=client.execute_query,
    )
    if (
        not isinstance(snapshot, GraphSourceSnapshot)
        or not graph_metadata_recallable(snapshot.entity.metadata)
        or not await graph_association_current(snapshot.entity, association)
    ):
        raise SourceUnavailableError()
    return snapshot, association
