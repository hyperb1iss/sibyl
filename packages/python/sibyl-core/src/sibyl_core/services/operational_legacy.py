"""Prove deterministic legacy inventory before attaching retained source lineage."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sibyl_core.backends.surreal.records import normalize_records
from sibyl_core.memory_pipeline.observations import evidence_hash
from sibyl_core.migrate.source_integrity import encode_record
from sibyl_core.services.graph_derivations import graph_target_digest
from sibyl_core.services.graph_records import entity_from_surreal_row, relationship_from_surreal_row
from sibyl_core.services.memory_derivations import observation_from_record
from sibyl_core.services.source_observations import SourceUnavailableError

if TYPE_CHECKING:
    from sibyl_core.services.operational_projection import OperationalProjectionSource


@dataclass(frozen=True, slots=True)
class OperationalLegacyProof:
    """Exact observed row bytes for adoption under each writer's snapshot CAS."""

    entity_rows: tuple[tuple[str, str], ...]
    relationship_rows: tuple[tuple[str, str], ...]
    relationship_snapshot_sha256: str

    def permits_entity(self, row: dict) -> bool:
        return (row.get("uuid"), _row_digest(row)) in self.entity_rows

    def permits_relationship(self, row: dict) -> bool:
        return (row.get("uuid"), _row_digest(row)) in self.relationship_rows


def _row_digest(row: dict) -> str:
    # Both supported row normalizers discard the physical target id (one
    # preserves a record_id alias). UUID identity and the native transaction
    # snapshot bind the target; endpoint references remain part of the proof.
    encoded = {key: value for key, value in row.items() if key not in {"id", "record_id"}}
    for key in ("in", "out"):
        if key in encoded:
            encoded[key] = str(encoded[key])
    return evidence_hash(encode_record(encoded))


def _body(model) -> dict:
    payload = model.model_dump(mode="python")
    for key in ("created_at", "updated_at", "embedding", "revision", "derivation_required"):
        payload.pop(key, None)
    metadata = payload.get("metadata", {})
    for key in (
        "record_id",
        "created_at",
        "updated_at",
        "embedding_metadata",
        "operational_write_witness",
        "fact_embedding",
        "embedding",
        "retrieval_count",
        "citation_count",
        "last_recalled_at",
        "last_used_at",
        "_direct_insert",
        "misled_count",
        "revision",
    ):
        metadata.pop(key, None)
    payload["metadata"] = {key: value for key, value in metadata.items() if value is not None}
    return encode_record(payload)


async def legacy_adoption_proof(
    client, source: OperationalProjectionSource
) -> OperationalLegacyProof:
    """Accept complete originals or resumable, source-bound pending entities.

    No historical bytes are recovered here. The candidate bytes are the newly
    authorized resubmission, compared with the actual retained graph inventory.
    """
    from sibyl_core.projection.experience import project_operational_experience

    _, experience = await source.current()
    original = await asyncio.to_thread(
        project_operational_experience,
        experience,
        organization_id=source.observation.source.organization_id,
        created_by=source.creator_id,
    )
    protected = await source.projection()
    org = source.observation.source.organization_id
    ids = sorted(original.manifest.entity_ids)
    edge_ids = sorted(original.manifest.relationship_ids)
    snapshots = normalize_records(
        await client.execute_query(
            """RETURN {
        LET $entities = SELECT * FROM entity WHERE group_id=$org AND uuid IN $ids ORDER BY uuid;
        LET $edges = SELECT *, in.uuid AS source_uuid, out.uuid AS target_uuid FROM relates_to
            WHERE group_id=$org AND uuid IN $edges ORDER BY uuid;
        LET $bindings = SELECT * FROM memory_derivations WHERE organization_id=$org
            AND target_kind='graph_entity' AND target_id IN $ids ORDER BY target_id;
        LET $states = SELECT * FROM source_states WHERE organization_id=$org
            AND source_kind='graph_entity' AND source_id IN $ids ORDER BY source_id;
        RETURN {entities:$entities, edges:$edges, bindings:$bindings, states:$states,
            edge_fingerprint:crypto::sha256(type::string($edges))};
        };""",
            org=org,
            ids=ids,
            edges=edge_ids,
        )
    )
    if len(snapshots) != 1:
        raise SourceUnavailableError()
    proof = await asyncio.to_thread(_prove_snapshot, snapshots[0], source, original, protected)
    await source.current()
    return proof


def _row_uuid(row: dict) -> str:
    value = row.get("uuid")
    if not isinstance(value, str) or not value:
        raise SourceUnavailableError()
    return value


def _prove_snapshot(snapshot, source, original, protected) -> OperationalLegacyProof:
    from sibyl_core.services.graph_entity_store import _entity_record
    from sibyl_core.services.graph_relationships import _relationship_record

    org = source.observation.source.organization_id
    ids = set(original.manifest.entity_ids)
    edge_ids = set(original.manifest.relationship_ids)
    rows = normalize_records(snapshot.get("entities"))
    edges = normalize_records(snapshot.get("edges"))
    if {_row_uuid(row) for row in rows} != set(ids) or {_row_uuid(row) for row in edges} != set(
        edge_ids
    ):
        raise SourceUnavailableError()
    associations = {row["target_id"]: row for row in normalize_records(snapshot.get("bindings"))}
    states = {row["source_id"]: row for row in normalize_records(snapshot.get("states"))}
    by_id = {_row_uuid(row): row for row in rows}
    manifest = by_id[original.manifest.manifest_entity_id]
    protected_inventory = all(row.get("derivation_required") is True for row in rows)
    if protected_inventory:
        expected = protected
    else:
        if associations or any(row.get("derivation_required") is True for row in rows):
            raise SourceUnavailableError()
        attributes = manifest.get("attributes")
        if (
            not isinstance(attributes, dict)
            or attributes.get("operational_projection_state") != "complete"
        ):
            raise SourceUnavailableError()
        expected = original
    for entity in expected.entities:
        row = by_id[entity.id]
        actual = entity_from_surreal_row(row)
        desired = entity_from_surreal_row(_entity_record(entity, group_id=org))
        if _body(actual) != _body(desired):
            raise SourceUnavailableError()
        state = states.get(entity.id)
        if (
            not isinstance(state, dict)
            or state.get("deleted") is not False
            or state.get("revision") != row.get("revision")
        ):
            raise SourceUnavailableError()
        if protected_inventory:
            binding = associations.get(entity.id)
            if (
                not isinstance(binding, dict)
                or binding.get("active") is not True
                or binding.get("body_sha256") != graph_target_digest(actual)
            ):
                raise SourceUnavailableError()
            observations = binding.get("observations")
            if (
                not isinstance(observations, list)
                or len(observations) != 1
                or not observation_from_record(observations[0]).same_evidence(source.observation)
            ):
                raise SourceUnavailableError()
            if (
                binding.get("principal_id") != source.authority.principal_id
                or binding.get("authority_ceiling") != source.authority.ceiling_metadata()
            ):
                raise SourceUnavailableError()
    expected_edges = {edge.id: edge for edge in original.relationships}
    for row in edges:
        edge = expected_edges[_row_uuid(row)]
        if row.get("source_uuid") != edge.source_id or row.get("target_uuid") != edge.target_id:
            raise SourceUnavailableError()
        actual = relationship_from_surreal_row(
            {key: value for key, value in row.items() if key not in {"source_uuid", "target_uuid"}}
        )
        desired = relationship_from_surreal_row(
            {**_relationship_record(edge, group_id=org), "episodes": []}
        )
        if _body(actual) != _body(desired):
            raise SourceUnavailableError()
    fingerprint = snapshot.get("edge_fingerprint")
    if not isinstance(fingerprint, str) or len(fingerprint) != 64:
        raise SourceUnavailableError()
    return OperationalLegacyProof(
        entity_rows=tuple((_row_uuid(row), _row_digest(row)) for row in rows),
        relationship_rows=tuple((_row_uuid(row), _row_digest(row)) for row in edges),
        relationship_snapshot_sha256=fingerprint,
    )
