"""Deterministic projection of operational experience into graph memory."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from itertools import pairwise
from typing import Any, Protocol

from sibyl_core.models.entities import Entity, EntityType, Relationship, RelationshipType
from sibyl_core.models.experience import (
    OperationalExperience,
    OperationalExperienceManifest,
    OperationalExperienceProjection,
    OperationalExperienceWriteResult,
    OperationalObservation,
)

OPERATIONAL_EXPERIENCE_SCHEMA_VERSION = 1


class OperationalEntityManager(Protocol):
    async def get(self, entity_id: str) -> Entity: ...

    async def create_direct_bulk(
        self,
        entities: Iterable[Entity],
        *,
        generate_embeddings: bool = False,
    ) -> list[str]: ...

    async def delete(self, entity_id: str) -> bool: ...


class OperationalRelationshipManager(Protocol):
    async def create_direct_bulk(
        self,
        relationships: Iterable[Relationship],
        *,
        generate_embeddings: bool = False,
    ) -> list[str]: ...

    async def delete_bulk(self, relationship_ids: Iterable[str]) -> int: ...


def _stable_id(prefix: str, *parts: object) -> str:
    material = ":".join(str(part) for part in parts)
    digest = hashlib.sha256(material.encode()).hexdigest()[:16]
    return f"{prefix}_{digest}"


def _content_hash(experience: OperationalExperience) -> str:
    payload = experience.model_dump(mode="json", exclude_none=True)
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _clean_lines(parts: Iterable[str | None]) -> str:
    return "\n".join(part.strip() for part in parts if part and part.strip())


def _observation_entity_id(
    source_id: str,
    observation_id: str,
    evidence_id: str,
) -> str:
    return _stable_id(
        "session",
        "operational-observation",
        source_id,
        observation_id,
        evidence_id,
    )


def _observation_content(
    experience: OperationalExperience,
    observation: OperationalObservation,
    *,
    evidence_index: int,
) -> str:
    evidence = observation.evidence[evidence_index]
    return _clean_lines(
        (
            f"Goal: {experience.goal}",
            f"Reported outcome: {experience.outcome}" if experience.outcome else None,
            f"Observation: {observation.ordinal}",
            f"URI: {observation.uri}" if observation.uri else None,
            f"Action producing this observation: {observation.action}"
            if observation.action
            else "Initial observation before any recorded action.",
            f"Reasoning: {observation.reasoning}" if observation.reasoning else None,
            f"Evidence part: {evidence_index + 1}/{len(observation.evidence)}",
            f"Evidence content type: {evidence.content_type}",
            f"Evidence:\n{evidence.content}" if evidence.content else None,
        )
    )


def _common_metadata(
    experience: OperationalExperience,
    *,
    content_hash: str,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "category": "operational_experience",
        "operational_source_id": experience.source_id,
        "operational_schema_version": OPERATIONAL_EXPERIENCE_SCHEMA_VERSION,
        "operational_content_hash": content_hash,
        **experience.metadata,
    }
    if experience.project_id:
        metadata["project_id"] = experience.project_id
    if experience.scope_key:
        metadata["scope_key"] = experience.scope_key
    return metadata


def _support_span(observation: OperationalObservation) -> dict[str, Any]:
    fields = [
        field
        for field, value in (
            ("action", observation.action),
            ("reasoning", observation.reasoning),
        )
        if value
    ]
    return {
        "observation_id": observation.id,
        "ordinal": observation.ordinal,
        "fields": fields,
        "image_refs": list(observation.image_refs),
        "evidence_part_ids": [part.id for part in observation.evidence],
    }


def _relationship(
    *,
    relationship_type: RelationshipType,
    source_id: str,
    target_id: str,
    source_key: str,
    metadata: dict[str, Any],
) -> Relationship:
    return Relationship(
        id=_stable_id(
            "rel",
            "operational-experience",
            source_key,
            relationship_type.value,
            source_id,
            target_id,
        ),
        relationship_type=relationship_type,
        source_id=source_id,
        target_id=target_id,
        metadata=metadata,
    )


def _procedure_content(experience: OperationalExperience) -> str:
    actions = [
        (observation.ordinal, observation.action)
        for observation in experience.observations
        if observation.action
    ]
    lines = [f"Goal: {experience.goal}"]
    lines.extend(f"{ordinal}. {action}" for ordinal, action in actions)
    if experience.outcome:
        lines.append(f"Reported outcome: {experience.outcome}")
    return "\n".join(lines)


def project_operational_experience(
    experience: OperationalExperience,
    *,
    organization_id: str | None = None,
    created_by: str | None = None,
) -> OperationalExperienceProjection:
    """Build raw evidence and typed projections without inventing facts."""
    ordinals = [observation.ordinal for observation in experience.observations]
    if len(ordinals) != len(set(ordinals)):
        raise ValueError("operational observation ordinals must be unique")
    observation_ids = [observation.id for observation in experience.observations]
    if len(observation_ids) != len(set(observation_ids)):
        raise ValueError("operational observation ids must be unique")

    observations = sorted(experience.observations, key=lambda item: item.ordinal)
    content_hash = _content_hash(experience)
    common = _common_metadata(experience, content_hash=content_hash)
    entities: list[Entity] = []
    relationships: list[Relationship] = []
    raw_ids: dict[str, list[str]] = {}

    for observation in observations:
        raw_ids[observation.id] = []
        for evidence_index, evidence in enumerate(observation.evidence):
            entity_id = _observation_entity_id(
                experience.source_id,
                observation.id,
                evidence.id,
            )
            raw_ids[observation.id].append(entity_id)
            entities.append(
                Entity(
                    id=entity_id,
                    entity_type=EntityType.SESSION,
                    name=(
                        f"Operational observation {observation.ordinal} "
                        f"part {evidence_index + 1}/{len(observation.evidence)}"
                    ),
                    content=_observation_content(
                        experience,
                        observation,
                        evidence_index=evidence_index,
                    ),
                    organization_id=organization_id,
                    created_by=created_by,
                    modified_by=created_by,
                    metadata={
                        **common,
                        "projection_kind": "raw_observation",
                        "source_observation_id": observation.id,
                        "observation_ordinal": observation.ordinal,
                        "evidence_part_id": evidence.id,
                        "evidence_part_index": evidence_index,
                        "evidence_part_count": len(observation.evidence),
                        "evidence_content_type": evidence.content_type,
                        "uri": observation.uri,
                        "action": observation.action,
                        "image_refs": list(observation.image_refs),
                        "source_metadata": observation.metadata,
                        "evidence_metadata": evidence.metadata,
                    },
                )
            )

    for previous, current in pairwise(observations):
        if not current.action:
            continue
        event_id = _stable_id("event", "operational-transition", experience.source_id, current.id)
        entities.append(
            Entity(
                id=event_id,
                entity_type=EntityType.EVENT,
                name=f"Action at observation {current.ordinal}",
                content=_clean_lines(
                    (
                        f"Goal: {experience.goal}",
                        f"Action: {current.action}",
                        f"Before URI: {previous.uri}" if previous.uri else None,
                        f"After URI: {current.uri}" if current.uri else None,
                        f"Observed reasoning: {current.reasoning}" if current.reasoning else None,
                    )
                ),
                organization_id=organization_id,
                created_by=created_by,
                modified_by=created_by,
                metadata={
                    **common,
                    "projection_kind": "transition",
                    "source_observation_ids": [previous.id, current.id],
                    "support_spans": [_support_span(previous), _support_span(current)],
                },
            )
        )
        for observation in (previous, current):
            for raw_id in raw_ids[observation.id]:
                relationships.append(
                    _relationship(
                        relationship_type=RelationshipType.DERIVED_FROM,
                        source_id=event_id,
                        target_id=raw_id,
                        source_key=experience.source_id,
                        metadata={**common, "source_observation_id": observation.id},
                    )
                )

    procedure_id = _stable_id("procedure", "operational", experience.source_id)
    entities.append(
        Entity(
            id=procedure_id,
            entity_type=EntityType.PROCEDURE,
            name=f"Procedure for {experience.goal[:120]}",
            content=_procedure_content(experience),
            organization_id=organization_id,
            created_by=created_by,
            modified_by=created_by,
            metadata={
                **common,
                "projection_kind": "procedure",
                "reported_outcome": experience.outcome,
                "source_observation_ids": [item.id for item in observations],
                "support_spans": [_support_span(item) for item in observations],
            },
        )
    )
    for observation in observations:
        for raw_id in raw_ids[observation.id]:
            relationships.append(
                _relationship(
                    relationship_type=RelationshipType.DERIVED_FROM,
                    source_id=procedure_id,
                    target_id=raw_id,
                    source_key=experience.source_id,
                    metadata={**common, "source_observation_id": observation.id},
                )
            )

    if experience.outcome and experience.outcome.casefold() in {"failure", "failed", "error"}:
        final = observations[-1]
        error_id = _stable_id("error_pattern", "operational", experience.source_id)
        entities.append(
            Entity(
                id=error_id,
                entity_type=EntityType.ERROR_PATTERN,
                name=f"Failed operational attempt: {experience.goal[:100]}",
                content=_clean_lines(
                    (
                        f"Goal: {experience.goal}",
                        f"Reported outcome: {experience.outcome}",
                        f"Final action: {final.action}" if final.action else None,
                        f"Final reasoning: {final.reasoning}" if final.reasoning else None,
                    )
                ),
                organization_id=organization_id,
                created_by=created_by,
                modified_by=created_by,
                metadata={
                    **common,
                    "projection_kind": "reported_failure",
                    "resolution_status": "unknown",
                    "source_observation_ids": [final.id],
                    "support_spans": [_support_span(final)],
                },
            )
        )
        for raw_id in raw_ids[final.id]:
            relationships.append(
                _relationship(
                    relationship_type=RelationshipType.DERIVED_FROM,
                    source_id=error_id,
                    target_id=raw_id,
                    source_key=experience.source_id,
                    metadata={**common, "source_observation_id": final.id},
                )
            )

    projected_ids = [entity.id for entity in entities]
    manifest_id = _stable_id("artifact", "operational-manifest", experience.source_id)
    for entity_id in projected_ids:
        relationships.append(
            _relationship(
                relationship_type=RelationshipType.PART_OF,
                source_id=entity_id,
                target_id=manifest_id,
                source_key=experience.source_id,
                metadata=common,
            )
        )

    relationship_ids = tuple(relationship.id for relationship in relationships)
    manifest = OperationalExperienceManifest(
        source_id=experience.source_id,
        schema_version=OPERATIONAL_EXPERIENCE_SCHEMA_VERSION,
        content_hash=content_hash,
        manifest_entity_id=manifest_id,
        entity_ids=tuple([*projected_ids, manifest_id]),
        relationship_ids=relationship_ids,
    )
    entities.append(
        Entity(
            id=manifest_id,
            entity_type=EntityType.ARTIFACT,
            name=f"Operational experience manifest {experience.source_id}",
            content=json.dumps(manifest.model_dump(mode="json"), sort_keys=True),
            organization_id=organization_id,
            created_by=created_by,
            modified_by=created_by,
            metadata={
                **common,
                "projection_kind": "manifest",
                "expected_entity_ids": list(manifest.entity_ids),
                "expected_relationship_ids": list(manifest.relationship_ids),
            },
        )
    )
    return OperationalExperienceProjection(
        entities=tuple(entities),
        relationships=tuple(relationships),
        manifest=manifest,
    )


def _manifest_inventory(entity: Entity | None, key: str) -> set[str]:
    if entity is None or entity.entity_type is not EntityType.ARTIFACT:
        return set()
    values = entity.metadata.get(key)
    if not isinstance(values, list):
        return set()
    return {str(value) for value in values if value}


async def persist_operational_experience(
    *,
    entity_manager: OperationalEntityManager,
    relationship_manager: OperationalRelationshipManager,
    experience: OperationalExperience,
    organization_id: str,
    created_by: str | None = None,
    generate_embeddings: bool = False,
) -> OperationalExperienceWriteResult:
    """Replay a projection and remove only records owned by its prior manifest."""
    projection = project_operational_experience(
        experience,
        organization_id=organization_id,
        created_by=created_by,
    )
    try:
        previous_manifest = await entity_manager.get(projection.manifest.manifest_entity_id)
    except KeyError:
        previous_manifest = None

    current_entity_ids = set(projection.manifest.entity_ids)
    current_relationship_ids = set(projection.manifest.relationship_ids)
    stale_entity_ids = tuple(
        sorted(_manifest_inventory(previous_manifest, "expected_entity_ids") - current_entity_ids)
    )
    stale_relationship_ids = tuple(
        sorted(
            _manifest_inventory(previous_manifest, "expected_relationship_ids")
            - current_relationship_ids
        )
    )

    written_entity_ids = await entity_manager.create_direct_bulk(
        projection.entities,
        generate_embeddings=generate_embeddings,
    )
    written_relationship_ids = await relationship_manager.create_direct_bulk(
        projection.relationships,
        generate_embeddings=generate_embeddings,
    )

    if stale_relationship_ids:
        await relationship_manager.delete_bulk(stale_relationship_ids)
    deleted_entity_ids: list[str] = []
    for entity_id in stale_entity_ids:
        if await entity_manager.delete(entity_id):
            deleted_entity_ids.append(entity_id)

    return OperationalExperienceWriteResult(
        projection=projection,
        written_entity_ids=tuple(written_entity_ids),
        written_relationship_ids=tuple(written_relationship_ids),
        deleted_entity_ids=tuple(deleted_entity_ids),
        deleted_relationship_ids=stale_relationship_ids,
    )
