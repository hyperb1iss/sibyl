"""Validate graph publication ancestry through the existing final read gate."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sibyl_core.services.graph_read_validation import GraphReadValidation

from sibyl_core.backends.surreal.records import normalize_records
from sibyl_core.memory_pipeline.observations import SourceIdentity, SourceKind, evidence_hash
from sibyl_core.models.entities import Entity
from sibyl_core.runtime_ports import RuntimePortUnavailable, get_source_authority_resolver
from sibyl_core.services.graph_client import SurrealGraphClient
from sibyl_core.services.memory_derivations import observation_from_record, validate_observations
from sibyl_core.services.memory_source_validation import source_authority_ceiling
from sibyl_core.services.source_observations import SourceUnavailableError, graph_evidence

# The planner unions point lookups for an IN list up to this size; one more
# id and it scans the table. Each batch is one round trip of that shape.
_VERDICT_BATCH_SIZE = 32
_VERDICT_SNAPSHOT_QUERY = """RETURN {
    RETURN {
        associations: (SELECT * FROM memory_derivations WHERE organization_id=$org
            AND target_kind='graph_entity' AND target_id IN $ids),
        targets: (SELECT * OMIT embedding, name_embedding FROM entity
            WHERE group_id=$org AND uuid IN $ids)
    };
};"""


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


async def graph_association_current(
    entity, association, *, ancestors=frozenset(), read: GraphReadValidation | None = None
) -> bool:
    if read is not None:
        return await read.association_proof(
            entity,
            association,
            ancestors,
            lambda: _graph_association_current(entity, association, ancestors=ancestors, read=read),
        )
    return await _graph_association_current(entity, association, ancestors=ancestors)


async def _graph_association_current(
    entity, association, *, ancestors=frozenset(), read: GraphReadValidation | None = None
) -> bool:
    if association is None:
        return not entity.derivation_required
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
        resolver = (
            read.source_authority_resolver
            if read is not None and read.source_authority_resolver is not None
            else get_source_authority_resolver()
        )
    except RuntimePortUnavailable:
        return False
    authority = (
        await read.resolve_authority(entity.organization_id, principal_id, resolver)
        if read is not None
        else await resolver(entity.organization_id, principal_id)
    )
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
    if any(o.source.kind is SourceKind.RAW_CAPTURE for o in observations):
        from sibyl_core.services.validation_execution import ValidationExecutionUnavailable
        from sibyl_core.services.validation_promotion import validated_graph_current

        try:
            if not (
                await read.graph_validated(entity.organization_id, entity.id)
                if read is not None
                else await validated_graph_current(entity.organization_id, entity.id)
            ):
                return False
        except ValidationExecutionUnavailable:
            return False
    return await validate_observations(
        observations,
        authority,
        organization_id=entity.organization_id,
        ancestors=ancestors,
        read=read,
    )


async def graph_derivation_current(
    entity, *, ancestors=frozenset(), read: GraphReadValidation | None = None
) -> bool:
    from sibyl_core.services.graph_runtime import get_surreal_graph_runtime

    execute_query = read.graph_execute_query if read is not None else None
    if read is not None:
        read._check_org(entity.organization_id)
    if execute_query is None:
        execute_query = (
            await get_surreal_graph_runtime(entity.organization_id)
        ).client.execute_query
    rows = normalize_records(
        await execute_query(
            "SELECT * FROM memory_derivations WHERE organization_id=$org AND target_kind='graph_entity' AND target_id=$id LIMIT 1;",
            org=entity.organization_id,
            id=entity.id,
        )
    )
    return await graph_association_current(
        entity, rows[0] if rows else None, ancestors=ancestors, read=read
    )


async def unavailable_graph_derivation_ids(
    organization_id: str,
    ids: Sequence[str],
    *,
    expected_entities: Mapping[str, Entity] | None = None,
    client: SurrealGraphClient | None = None,
    read: GraphReadValidation | None = None,
) -> set[str]:
    verdicts = await _graph_derivation_verdicts(
        organization_id,
        ids,
        expected_entities=expected_entities,
        client=client,
        read=read,
    )
    return {identifier for identifier, current in verdicts.items() if current is False}


async def graph_publication_verdicts(
    organization_id: str,
    rows: Mapping[str, Any],
    *,
    client: SurrealGraphClient | None,
    read: GraphReadValidation,
) -> dict[str, bool | None]:
    """Distinguish legacy ancestry from current protected publication proof.

    None means no stored association requires authority expansion. A present
    association must verify even when this reader can read every source.
    """
    return await _graph_derivation_verdicts(
        organization_id,
        list(rows),
        client=client,
        read=read,
        expected_publications=rows,
    )


async def _graph_derivation_verdicts(
    organization_id: str,
    ids: Sequence[str],
    *,
    expected_entities: Mapping[str, Entity] | None = None,
    client: SurrealGraphClient | None = None,
    read: GraphReadValidation | None = None,
    expected_publications: Mapping[str, Any] | None = None,
) -> dict[str, bool | None]:
    from sibyl_core.services.graph_records import entity_from_surreal_row

    if not ids:
        return {}
    from sibyl_core.services.graph_runtime import get_surreal_graph_runtime

    execute_query = read.graph_execute_query if read is not None else None
    if read is not None:
        read._check_org(organization_id)
    # An explicit reader is a transaction executor on one connection; it
    # cannot take overlapping statements. A pooled client takes as many as it
    # has slots; on a single-slot pool (every embedded store) overlapping
    # batches only queue behind each other, and a queued waiter binds the
    # pool's queue to the current event loop, which a later loop reusing the
    # same client cannot wait on.
    sequential = execute_query is not None
    if execute_query is None:
        if client is None:
            client = (await get_surreal_graph_runtime(organization_id)).client
        execute_query = client.execute_query
        sequential = getattr(client, "pool_size", 1) <= 1
    unique_ids = list(dict.fromkeys(ids))
    batches = [
        unique_ids[start : start + _VERDICT_BATCH_SIZE]
        for start in range(0, len(unique_ids), _VERDICT_BATCH_SIZE)
    ]

    async def snapshot(batch: list[str]) -> tuple[list[Any], list[Any]]:
        rows = normalize_records(
            await execute_query(_VERDICT_SNAPSHOT_QUERY, org=organization_id, ids=batch)
        )
        if len(rows) != 1:
            raise RuntimeError("graph derivation snapshot unavailable")
        batch_targets = rows[0].get("targets")
        batch_associations = rows[0].get("associations")
        if not isinstance(batch_targets, list) or not isinstance(batch_associations, list):
            raise RuntimeError("graph derivation snapshot unavailable")
        return batch_targets, batch_associations

    if sequential:
        snapshots = [await snapshot(batch) for batch in batches]
    else:
        snapshots = await asyncio.gather(*(snapshot(batch) for batch in batches))
    target_rows = [row for batch_targets, _ in snapshots for row in batch_targets]
    association_rows = [row for _, batch_associations in snapshots for row in batch_associations]
    targets = {}
    for row in target_rows:
        try:
            target = entity_from_surreal_row(row)
            if target.organization_id == organization_id:
                targets[target.id] = target
        except (TypeError, ValueError, KeyError):
            continue

    associations = {}
    duplicate_associations = set()
    for row in association_rows:
        if not isinstance(row, Mapping) or not isinstance(row.get("target_id"), str):
            continue
        target_id = row["target_id"]
        if target_id in associations:
            duplicate_associations.add(target_id)
        associations[target_id] = row
    if read is not None:
        await read.prepare_graph(
            list(targets.keys() & associations.keys())
            if expected_publications is not None
            else list(targets)
        )

    async def current(target_id):
        association = associations.get(target_id)
        entity = targets.get(target_id)
        identity = SourceIdentity(organization_id, SourceKind.GRAPH_ENTITY, target_id)
        if expected_publications is not None:
            if association is None:
                return False if entity is not None and entity.derivation_required else None
            if (
                target_id in duplicate_associations
                or association.get("organization_id") != organization_id
                or association.get("target_kind") != "graph_entity"
                or association.get("target_id") != target_id
                or entity is None
                or not _same_publication_row(expected_publications.get(target_id), entity)
            ):
                return False
        if expected_entities is not None:
            expected = expected_entities.get(target_id)
            # The snapshot rows above carry no vectors, so the comparison
            # must not either; entity_read_evidence draws the same line.
            if (
                expected is None
                or entity is None
                or (
                    expected.model_dump(mode="json", exclude={"embedding"})
                    != entity.model_dump(mode="json", exclude={"embedding"})
                    or expected.derivation_required != entity.derivation_required
                    or expected.observed_revision != entity.observed_revision
                )
            ):
                return False
        if entity is None:
            return False
        try:
            current = await graph_association_current(
                entity, association, ancestors=frozenset({identity}), read=read
            )
        except Exception:
            if expected_publications is None:
                raise
            return False
        return current

    identifiers = sorted(
        targets.keys()
        | associations.keys()
        | (expected_entities.keys() if expected_entities is not None else set())
        | (expected_publications.keys() if expected_publications is not None else set())
    )
    verdicts = await asyncio.gather(*(current(identifier) for identifier in identifiers))
    return dict(zip(identifiers, verdicts, strict=True))


def _same_publication_row(row, entity: Entity) -> bool:
    if row is None or entity.observed_revision is None:
        return False
    revision = getattr(row, "observed_revision", None) or getattr(row, "source_revision", None)
    organization_id = getattr(row, "organization_id", None) or getattr(
        getattr(row, "scope", None), "organization_id", None
    )
    metadata = getattr(row, "metadata", None)
    if (
        row.id != entity.id
        or organization_id != entity.organization_id
        or type(revision) is not int
        or revision != entity.observed_revision
        or not isinstance(metadata, Mapping)
    ):
        return False
    # List rows may omit prose; the stored revision and proof still bind it.
    return all(
        metadata.get(key) == entity.metadata.get(key)
        for key in (
            "memory_scope",
            "scope_key",
            "principal_id",
            "project_id",
            "source_bindings",
            "raw_memory_id",
            "raw_source_ids",
            "parent_entity_id",
            "source_entity_id",
        )
    )


async def load_graph_projection_source(
    client, *, organization_id: str, source_id: str, read: GraphReadValidation | None = None
):
    """Capture the protected parent snapshot before the projection cuts text."""
    from sibyl_core.memory_pipeline.lifecycle import graph_metadata_recallable
    from sibyl_core.services.source_observations import GraphSourceSnapshot
    from sibyl_core.services.source_state_store import load_source_snapshot

    execute_query = read.graph_execute_query if read is not None else None
    if read is not None:
        read._check_org(organization_id)
    if execute_query is None:
        execute_query = client.execute_query
    associations = normalize_records(
        await execute_query(
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
        execute_query=execute_query,
    )
    if (
        not isinstance(snapshot, GraphSourceSnapshot)
        or not graph_metadata_recallable(snapshot.entity.metadata)
        or not await graph_association_current(snapshot.entity, association, read=read)
    ):
        raise SourceUnavailableError()
    return snapshot, association
