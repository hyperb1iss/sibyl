"""Bind operational relationship facts to their retained source generation."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from sibyl_core.backends.surreal.records import normalize_records
from sibyl_core.backends.surreal.schema_source_witness import SOURCE_STATE_WRITE_WITNESS
from sibyl_core.embeddings.providers import EmbeddingProvider
from sibyl_core.memory_pipeline.observations import SourceIdentity, SourceKind, evidence_hash
from sibyl_core.migrate.source_integrity import encode_record, native_archive_parameters
from sibyl_core.runtime_ports import RuntimePortUnavailable, get_source_authority_resolver
from sibyl_core.services.graph_derivations import graph_association_current, graph_target_digest
from sibyl_core.services.memory_derivations import observation_from_record, validate_observations
from sibyl_core.services.memory_source_validation import (
    resolve_current_source_authority,
    source_authority_ceiling,
)
from sibyl_core.services.operational_projection import OperationalProjectionSource
from sibyl_core.services.source_observations import (
    GraphSourceSnapshot,
    SourceUnavailableError,
    observe_graph_snapshot,
)
from sibyl_core.services.source_state_store import source_snapshot_from_records

_SNAPSHOT = """
LET $targets = SELECT * FROM entity WHERE group_id=$org AND uuid IN $ids ORDER BY uuid;
LET $associations = SELECT * OMIT validation_write_witness FROM memory_derivations WHERE organization_id=$org
    AND target_kind='graph_entity' AND target_id IN $ids ORDER BY target_id;
LET $states = SELECT * OMIT validation_write_witness FROM source_states
    WHERE organization_id=$org AND source_kind='graph_entity' AND source_id IN $ids ORDER BY source_id;
LET $relationships = SELECT *, in.uuid AS source_uuid, out.uuid AS target_uuid FROM relates_to
    WHERE group_id=$org AND uuid IN $relationship_ids ORDER BY uuid;
"""

_ENDPOINT_WRITE_WITNESS = (
    "LET $source_states_to_fence = $states;"
    + SOURCE_STATE_WRITE_WITNESS
    + """
    FOR $association IN $associations {
        UPDATE $association.id SET validation_write_witness = type::string(rand::uuid());
    };
    """
)

# Completion writes a manifest rather than its supporting edges. Enlist those
# rows too; the changing field is bookkeeping, never publication authority.
OPERATIONAL_SNAPSHOT_WRITE_WITNESS = (
    _ENDPOINT_WRITE_WITNESS
    + """
    FOR $relationship IN $relationships {
        UPDATE $relationship.id
            SET attributes.operational_write_witness = type::string(rand::uuid());
    };
    """
)


def relationship_body_digest(row: dict[str, Any]) -> str:
    """Hash canonical relationship evidence, excluding storage-only fields."""
    from sibyl_core.services.graph_records import relationship_from_surreal_row

    fields = (
        "uuid",
        "name",
        "fact",
        "group_id",
        "source_id",
        "target_id",
        "episodes",
        "attributes",
        "expired_at",
        "valid_at",
        "invalid_at",
    )
    native = {key: row.get(key) for key in fields}
    native["episodes"] = row.get("episodes") or []
    relationship = relationship_from_surreal_row(native)
    metadata = {
        key: value
        for key, value in relationship.metadata.items()
        if key
        not in {
            "record_id",
            "fact_embedding",
            "embedding",
            "embedding_metadata",
            "operational_write_witness",
        }
    }
    body = {
        "id": relationship.id,
        "type": relationship.relationship_type.value,
        "source_id": relationship.source_id,
        "target_id": relationship.target_id,
        "weight": relationship.weight,
        "metadata": metadata,
    }
    return evidence_hash({"version": 1, "body": encode_record(body)})


async def _snapshot(client, *, organization_id, ids, relationship_ids):
    rows = normalize_records(
        await client.execute_query(
            "RETURN {"
            + _SNAPSHOT
            + """
        RETURN {targets:$targets, associations:$associations, states:$states,
            relationships:$relationships,
            fingerprint:crypto::sha256(type::string([$targets,$associations,$states,$relationships]))};
        };""",
            org=organization_id,
            ids=sorted(ids),
            relationship_ids=sorted(relationship_ids),
        )
    )
    if len(rows) != 1:
        raise SourceUnavailableError()
    return rows[0]


async def publish_operational_relationships(
    client,
    source: OperationalProjectionSource,
    *,
    group_id: str,
    embedding_provider: EmbeddingProvider | None = None,
    embedding_batch_size: int = 64,
    retired_ids: tuple[str, ...] = (),
) -> list[str]:
    """Write only the recomputed deterministic inventory and protected binding."""
    from sibyl_core.services.graph_relationships import (
        _RELATIONSHIP_BULK_UPSERT_STATEMENTS,
        _relationship_record,
    )

    if source.observation.source.organization_id != group_id:
        raise SourceUnavailableError()
    projection = await source.projection()
    relationships = projection.relationships
    if not relationships and not retired_ids:
        return []
    ids = {endpoint for row in relationships for endpoint in (row.source_id, row.target_id)}
    relationship_ids = [row.id for row in relationships]
    if set(retired_ids).intersection(relationship_ids):
        raise SourceUnavailableError()
    captured_relationship_ids = sorted(set(relationship_ids).union(retired_ids))
    snapshot = await _snapshot(
        client, organization_id=group_id, ids=ids, relationship_ids=captured_relationship_ids
    )
    retirements = []
    for old in snapshot["relationships"]:
        if old["uuid"] not in retired_ids:
            continue
        binding = old.get("operational_source_binding")
        if old.get("operational_derivation_required") is not True or not isinstance(binding, dict):
            raise SourceUnavailableError()
        previous = observation_from_record(binding.get("source"))
        if (
            old.get("group_id") != group_id
            or binding.get("body_sha256") != relationship_body_digest(old)
            or previous.source != source.observation.source
            or previous.effective_incarnation != source.observation.effective_incarnation
            or previous.generation > source.observation.generation
        ):
            raise SourceUnavailableError()
        if old.get("invalid_at") is None and old.get("expired_at") is None:
            retirements.append(old["uuid"])
    targets = {r["uuid"]: r for r in snapshot["targets"]}
    associations = {r["target_id"]: r for r in snapshot["associations"]}
    states = {r["source_id"]: r for r in snapshot["states"]}
    old_edges = {r["uuid"]: r for r in snapshot["relationships"]}
    legacy = (
        await source.legacy_adoption_proof(client)
        if any(
            row.get("operational_derivation_required") is not True
            and row.get("operational_source_binding") is None
            for row in old_edges.values()
        )
        else None
    )
    endpoint_observations = {}
    for identity in ids:
        endpoint_source = SourceIdentity(group_id, SourceKind.GRAPH_ENTITY, identity)
        observed = source_snapshot_from_records(
            endpoint_source, targets.get(identity), states.get(identity)
        )
        association = associations.get(identity)
        if not isinstance(observed, GraphSourceSnapshot) or not isinstance(association, dict):
            raise SourceUnavailableError()
        observation = observe_graph_snapshot(observed, endpoint_source, source.authority)
        if (
            not await graph_association_current(observed.entity, association)
            or association.get("body_sha256") != graph_target_digest(observed.entity)
            or not isinstance(association.get("observations"), list)
            or len(association["observations"]) != 1
            or not observation_from_record(association["observations"][0]).same_evidence(
                source.observation
            )
        ):
            raise SourceUnavailableError()
        endpoint_observations[identity] = observation
    rows = []
    for relationship in relationships:
        old = old_edges.get(relationship.id)
        if old is not None:
            binding = old.get("operational_source_binding")
            adopting = (
                old.get("operational_derivation_required") is not True
                and binding is None
                and legacy is not None
                and legacy.permits_relationship(old)
            )
            if not adopting:
                if (
                    old.get("operational_derivation_required") is not True
                    or not isinstance(binding, dict)
                    or observation_from_record(binding.get("source")).source
                    != source.observation.source
                    or old.get("source_uuid") != relationship.source_id
                    or old.get("target_uuid") != relationship.target_id
                ):
                    raise SourceUnavailableError()
                # Explicit retraction survives replay. A new authoritative
                # observation may regenerate this deterministic relationship.
                if (
                    old.get("invalid_at") is not None or old.get("expired_at") is not None
                ) and observation_from_record(binding.get("source")).same_evidence(
                    source.observation
                ):
                    raise SourceUnavailableError()
        row = _relationship_record(relationship, group_id=group_id)
        row["in"] = targets[relationship.source_id]["id"]
        row["out"] = targets[relationship.target_id]["id"]
        if old is not None:
            row["created_at"] = old["created_at"]
        row["operational_derivation_required"] = True
        row["operational_source_binding"] = {
            "version": 1,
            "body_sha256": relationship_body_digest(row),
            "principal_id": source.authority.principal_id,
            "authority_ceiling": source.authority.ceiling_metadata(),
            "source": asdict(source.observation),
            "endpoints": [
                asdict(endpoint_observations[relationship.source_id]),
                asdict(endpoint_observations[relationship.target_id]),
            ],
        }
        rows.append(row)
    # Reuse current vectors for exact replay, including a later deferred call.
    # Provider metadata is storage bookkeeping, not relationship evidence.
    prepared = []
    for relationship, row in zip(relationships, rows, strict=True):
        old = old_edges.get(relationship.id)
        metadata = dict(relationship.metadata)
        if old is not None and relationship_body_digest(old) == relationship_body_digest(row):
            attributes = old.get("attributes") or {}
            vector = old.get("fact_embedding")
            if vector and (
                embedding_provider is None
                or attributes.get("embedding_metadata") == embedding_provider.metadata.to_dict()
            ):
                metadata["fact_embedding"] = vector
                metadata["embedding_metadata"] = attributes.get("embedding_metadata")
        prepared.append(relationship.model_copy(update={"metadata": metadata}))
    await source.current()
    from sibyl_core.services.graph_embeddings import _relationships_with_native_embeddings

    prepared = await _relationships_with_native_embeddings(
        prepared,
        embedding_provider,
        batch_size=embedding_batch_size,
    )
    for relationship, row in zip(prepared, rows, strict=True):
        embedded = _relationship_record(relationship, group_id=group_id)
        row["fact_embedding"] = embedded["fact_embedding"]
        row["attributes"] = embedded["attributes"]
    await source.current()
    await client.execute_query(
        "RETURN {"
        + _SNAPSHOT
        + """
        IF crypto::sha256(type::string([$targets,$associations,$states,$relationships])) != $fingerprint {
            THROW 'operational relationship source changed before publication';
        };
        """
        + _ENDPOINT_WRITE_WITNESS
        + _RELATIONSHIP_BULK_UPSERT_STATEMENTS
        + "UPDATE relates_to SET invalid_at=time::now(),expired_at=time::now() WHERE group_id=$org AND uuid IN $retirements; RETURN true; };",
        org=group_id,
        ids=sorted(ids),
        relationship_ids=captured_relationship_ids,
        retirements=retirements,
        fingerprint=snapshot["fingerprint"],
        rows=native_archive_parameters(rows),
        edges=[{"uuid": row["uuid"], "src": row["in"], "tgt": row["out"]} for row in rows],
    )
    await source.current()
    return relationship_ids


async def operational_relationship_current(
    row, *, targets, states, associations, organization_id
) -> bool:
    """Validate native provenance without accepting caller metadata as authority."""
    binding = row.get("operational_source_binding")
    required = row.get("operational_derivation_required") is True
    attributes = row.get("attributes") or {}
    if not required and binding is None:
        # Unbound operational projections cannot inherit a current generation
        # merely because their stable endpoint IDs have been republished.
        return attributes.get("category") != "operational_experience"
    if (
        not required
        or row.get("invalid_at") is not None
        or row.get("expired_at") is not None
        or not isinstance(binding, dict)
        or binding.get("version") != 1
        or binding.get("body_sha256") != relationship_body_digest(row)
    ):
        return False
    principal = binding.get("principal_id")
    ceiling = source_authority_ceiling(binding.get("authority_ceiling"), principal)
    if ceiling is None:
        return False
    try:
        resolver = get_source_authority_resolver()
    except RuntimePortUnavailable:
        return False
    authority = await resolve_current_source_authority(ceiling, organization_id, resolver)
    if authority is None:
        return False
    try:
        source = observation_from_record(binding.get("source"))
        values = binding.get("endpoints")
        if (
            source.source.kind is not SourceKind.RAW_CAPTURE
            or source.source.organization_id != organization_id
            or not isinstance(values, list)
            or len(values) != 2
        ):
            return False
        endpoints = [observation_from_record(v) for v in values]
        if [o.source.id for o in endpoints] != [row.get("source_uuid"), row.get("target_uuid")]:
            return False
        for observation in endpoints:
            if (
                observation.source.kind is not SourceKind.GRAPH_ENTITY
                or observation.source.organization_id != organization_id
            ):
                return False
            current = source_snapshot_from_records(
                observation.source,
                targets.get(observation.source.id),
                states.get(observation.source.id),
            )
            if not isinstance(current, GraphSourceSnapshot) or not observation.same_evidence(
                observe_graph_snapshot(current, observation.source, authority)
            ):
                return False
            association = associations.get(observation.source.id)
            if (
                not isinstance(association, dict)
                or association.get("organization_id") != organization_id
                or association.get("target_kind") != SourceKind.GRAPH_ENTITY.value
                or association.get("target_id") != observation.source.id
                or not await graph_association_current(current.entity, association)
                or not isinstance(association.get("observations"), list)
                or len(association["observations"]) != 1
                or not observation_from_record(association["observations"][0]).same_evidence(source)
            ):
                return False
        return await validate_observations([source], authority, organization_id=organization_id)
    except (SourceUnavailableError, ValueError, TypeError):
        return False


async def operational_relationship_inventory_current(
    source: OperationalProjectionSource, snapshot: dict[str, Any]
) -> bool:
    """Check the exact deterministic inventory before its owner's final CAS.

    The caller must fence this same snapshot before recording completion.
    Successful validation alone does not reserve the captured graph rows.
    """
    from sibyl_core.services.graph_relationships import _relationship_record

    projection = await source.projection()
    expected = {edge.id: edge for edge in projection.relationships}
    rows = snapshot["relationships"]
    if len(rows) != len(expected) or {row["uuid"] for row in rows} != set(expected):
        return False
    targets = {row["uuid"]: row for row in snapshot["targets"]}
    states = {row["source_id"]: row for row in snapshot["states"]}
    associations = {row["target_id"]: row for row in snapshot["associations"]}
    org = source.observation.source.organization_id
    for row in rows:
        binding = row.get("operational_source_binding")
        if not isinstance(binding, dict):
            return False
        try:
            if not observation_from_record(binding.get("source")).same_evidence(source.observation):
                return False
        except (TypeError, ValueError):
            return False
        projected = _relationship_record(expected[row["uuid"]], group_id=org)
        if relationship_body_digest(projected) != relationship_body_digest(row):
            return False
        if not await operational_relationship_current(
            row, targets=targets, states=states, associations=associations, organization_id=org
        ):
            return False
    await source.current()
    return True
