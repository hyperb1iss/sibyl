"""Finalize current graph nodes and edges against their recorded dependencies."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

import structlog
from pydantic import ValidationError

from sibyl_core.backends.surreal.records import normalize_records
from sibyl_core.memory_pipeline.observations import SourceIdentity, SourceKind
from sibyl_core.models.entities import Entity, Relationship
from sibyl_core.services import content_client
from sibyl_core.services.content_models import raw_memory_from_record
from sibyl_core.services.graph_read_availability import (
    available_graph_entities,
    available_graph_relationships,
)
from sibyl_core.services.graph_read_validation import (
    GraphReadValidation,
    association_read_evidence,
    capture_read_evidence,
    entity_read_evidence,
)
from sibyl_core.services.graph_records import (
    _jsonable,
    entity_from_surreal_row,
    relationship_from_surreal_row,
)
from sibyl_core.services.graph_runtime import GraphRuntime
from sibyl_core.services.memory_derivations import observation_from_record
from sibyl_core.services.operational_relationships import _snapshot
from sibyl_core.services.source_observations import graph_evidence

log = structlog.get_logger()


def _edge_evidence(edge: Relationship):
    body = edge.model_dump(mode="json", exclude={"created_at", "metadata"})
    body["metadata"] = {
        key: value
        for key, value in edge.metadata.items()
        if key
        not in {
            "record_id",
            "embedding",
            "fact_embedding",
            "embedding_metadata",
            "operational_write_witness",
        }
    }
    body["binding"] = edge.operational_source_binding
    body["required"] = edge.operational_derivation_required
    return _jsonable(body)


async def _capture_snapshot(organization_id: str, ids: list[str]) -> dict[str, Any]:
    if not ids:
        return {"captures": [], "states": [], "associations": [], "content_hashes": []}
    async with content_client.surreal_content_client() as client:
        rows = normalize_records(
            await client.execute_query(
                """RETURN {
                RETURN {
                    captures: (SELECT * OMIT raw_content,embedding FROM raw_captures
                        WHERE organization_id=$org AND uuid IN $ids),
                    states: (SELECT * OMIT validation_write_witness FROM source_states
                        WHERE organization_id=$org AND source_kind='raw_capture' AND source_id IN $ids),
                    associations: (SELECT * OMIT validation_write_witness FROM memory_derivations
                        WHERE organization_id=$org AND target_kind='raw_capture' AND target_id IN $ids),
                    content_hashes: (SELECT uuid,crypto::sha256(raw_content) AS content_sha256
                        FROM raw_captures WHERE organization_id=$org AND uuid IN $ids)
                };
            };""",
                org=organization_id,
                ids=ids,
            )
        )
    if len(rows) != 1:
        raise ValueError("Canonical graph-read snapshot unavailable")
    return rows[0]


def _changed_inputs(read: GraphReadValidation, graph, captures) -> set[SourceIdentity]:
    changed = set(read.conflicts)

    def indexed(rows, key, kind, decode=lambda row: row):
        result = {}
        duplicates = set()
        for row in rows:
            identifier = row.get(key) if isinstance(row, dict) else None
            if not isinstance(identifier, str):
                continue
            if identifier in result or identifier in duplicates:
                result.pop(identifier, None)
                duplicates.add(identifier)
                changed.add(SourceIdentity(read.organization_id, kind, identifier))
                continue
            try:
                result[identifier] = decode(row)
            except (TypeError, ValueError, KeyError):
                changed.add(SourceIdentity(read.organization_id, kind, identifier))
        return result

    targets = indexed(graph["targets"], "uuid", SourceKind.GRAPH_ENTITY, entity_from_surreal_row)
    raw = indexed(captures["captures"], "uuid", SourceKind.RAW_CAPTURE, raw_memory_from_record)
    graph_associations = indexed(graph["associations"], "target_id", SourceKind.GRAPH_ENTITY)
    raw_associations = indexed(captures["associations"], "target_id", SourceKind.RAW_CAPTURE)
    graph_states = indexed(graph["states"], "source_id", SourceKind.GRAPH_ENTITY)
    raw_states = indexed(captures["states"], "source_id", SourceKind.RAW_CAPTURE)
    for records, current, evidence in (
        (read.graph_rows, targets, entity_read_evidence),
        (read.graph_ancestry, targets, lambda row: entity_read_evidence(row, ancestry=True)),
        (read.raw_rows, raw, capture_read_evidence),
    ):
        for source, expected in records.items():
            row = current.get(source.id)
            if row is None or _jsonable(evidence(row)) != expected:
                changed.add(source)
    for source, expected in read.associations.items():
        current = graph_associations if source.kind is SourceKind.GRAPH_ENTITY else raw_associations
        if _jsonable(association_read_evidence(current.get(source.id))) != expected:
            changed.add(source)
    hashes = {row["uuid"]: row["content_sha256"] for row in captures["content_hashes"]}
    for source, expected in read.raw_content_digests.items():
        if hashes.get(source.id) != expected:
            changed.add(source)
    for source, observation in read.observations.items():
        states = graph_states if source.kind is SourceKind.GRAPH_ENTITY else raw_states
        state = states.get(source.id)
        if (
            state is None
            or not observation.durable
            or type(state.get("revision")) is not int
            or type(state.get("generation")) is not int
            or state.get("organization_id") != source.organization_id
            or state.get("source_kind") != source.kind.value
            or state.get("source_id") != source.id
            or state.get("deleted") is not False
            or state.get("revision") != observation.revision
            or state.get("generation") != observation.generation
            or state.get("incarnation") != observation.effective_incarnation
        ):
            changed.add(source)
        if source.kind is SourceKind.GRAPH_ENTITY:
            entity = targets.get(source.id)
            if entity is None or graph_evidence(entity) != observation.content_sha256:
                changed.add(source)
    recorded = (
        read.graph_rows.keys()
        | read.graph_ancestry.keys()
        | read.raw_rows.keys()
        | read.observations.keys()
    )
    for dependencies in read.dependencies.values():
        changed.update(dependencies - recorded)
    return changed


async def available_graph_view(
    organization_id: str,
    entity_ids: Sequence[str],
    relationships: Mapping[str, Relationship],
    *,
    runtime: GraphRuntime,
    source_visible: Callable[[Any], bool] | None = None,
) -> tuple[dict[str, Entity], dict[str, Relationship]]:
    """Return both maps after one bounded, read-only final comparison.

    Graph and canonical content each use a source-local snapshot. Their final
    facts must match the fresh validation pass; this is not a cross-store
    transaction or a reservation against writes after those captures.
    """
    read = GraphReadValidation(organization_id)
    # Every rendered edge must stand on rendered nodes, so the nodes are
    # proven once and the edge proof compares its snapshots against them
    # rather than proving each endpoint again in both of its phases.
    nodes = await available_graph_entities(
        organization_id,
        entity_ids,
        runtime=runtime,
        read=read,
        source_visible=source_visible,
        include_embeddings=False,
    )
    current_edges = await available_graph_relationships(
        organization_id, list(relationships), runtime=runtime, read=read, proven_endpoints=nodes
    )
    current_edges = {
        identifier: edge
        for identifier, edge in current_edges.items()
        if _edge_evidence(edge) == _edge_evidence(relationships[identifier])
    }
    edge_dependencies: dict[str, set[SourceIdentity]] = {}
    for identifier, edge in current_edges.items():
        dependencies = {
            SourceIdentity(organization_id, SourceKind.GRAPH_ENTITY, endpoint)
            for endpoint in (edge.source_id, edge.target_id)
        }
        binding = edge.operational_source_binding
        if binding is not None:
            for value in [binding.get("source"), *binding.get("endpoints", [])]:
                observation = observation_from_record(value)
                read.record_observation(observation)
                dependencies.add(observation.source)
        edge_dependencies[identifier] = dependencies
    identities = (
        read.graph_rows.keys()
        | read.graph_ancestry.keys()
        | read.raw_rows.keys()
        | read.observations.keys()
        | read.associations.keys()
        | read.dependencies.keys()
    )
    identities.update(source for values in read.dependencies.values() for source in values)
    identities.update(source for values in edge_dependencies.values() for source in values)
    graph_ids = sorted(
        {source.id for source in identities if source.kind is SourceKind.GRAPH_ENTITY} | set(nodes)
    )
    raw_ids = sorted({source.id for source in identities if source.kind is SourceKind.RAW_CAPTURE})
    final_graph = await _snapshot(
        runtime.client,
        organization_id=organization_id,
        ids=graph_ids,
        relationship_ids=list(current_edges),
        include_embeddings=False,
    )
    try:
        final_captures = await _capture_snapshot(organization_id, raw_ids)
    except Exception as exc:
        log.warning(
            "graph_view_capture_snapshot_failed",
            organization_id=organization_id,
            dependent_capture_count=len(raw_ids),
            error_type=type(exc).__name__,
        )
        # Canonical failure affects its dependents; ordinary graph facts survive.
        final_captures = {"captures": [], "states": [], "associations": [], "content_hashes": []}
    changed = _changed_inputs(read, final_graph, final_captures)
    nodes = {
        identifier: node
        for identifier, node in nodes.items()
        if not read.affected(
            SourceIdentity(organization_id, SourceKind.GRAPH_ENTITY, identifier), changed
        )
    }
    stored_edges: dict[str, Relationship] = {}
    for row in final_graph["relationships"]:
        try:
            stored_edges[row["uuid"]] = relationship_from_surreal_row(row)
        except ValidationError:
            log.warning(
                "graph_view_relationship_invalid",
                organization_id=organization_id,
                relationship_id=row["uuid"],
            )
    edges = {
        identifier: edge
        for identifier, edge in current_edges.items()
        if identifier in stored_edges
        and _edge_evidence(stored_edges[identifier]) == _edge_evidence(edge)
        and edge.source_id in nodes
        and edge.target_id in nodes
        and not any(read.affected(source, changed) for source in edge_dependencies[identifier])
    }
    return nodes, edges
