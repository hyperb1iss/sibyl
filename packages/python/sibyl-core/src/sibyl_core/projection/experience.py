"""Deterministic projection of operational experience into graph memory."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Iterable
from itertools import pairwise
from typing import Any, Literal, Protocol

from sibyl_core.models.entities import Entity, EntityType, Relationship, RelationshipType
from sibyl_core.models.experience import (
    OperationalExperience,
    OperationalExperienceManifest,
    OperationalExperienceProjection,
    OperationalExperienceWriteResult,
    OperationalObservation,
)

OPERATIONAL_EXPERIENCE_SCHEMA_VERSION = 4
MAX_TYPED_ENTITY_CONTENT_CHARS = 18_000
MAX_UI_INVENTORY_CHARS = 16_000
MAX_UI_INVENTORY_ITEMS = 160
MANIFEST_STATE_PENDING = "pending"
MANIFEST_STATE_EMBEDDING_PENDING = "embedding_pending"
MANIFEST_STATE_COMPLETE = "complete"
type OperationalManifestState = Literal["pending", "embedding_pending", "complete"]


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
    header = _clean_lines(
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
        )
    )
    return f"{header}\nEvidence:\n{evidence.content}"


def _common_metadata(
    experience: OperationalExperience,
    *,
    content_hash: str,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        **experience.metadata,
        "category": "operational_experience",
        "operational_source_id": experience.source_id,
        "operational_schema_version": OPERATIONAL_EXPERIENCE_SCHEMA_VERSION,
        "operational_content_hash": content_hash,
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


_ACCESSIBILITY_NODE_PATTERN = re.compile(
    r"^(?:\[[^\]]+\]\s+)?(?P<role>[A-Za-z][\w-]*)\s+"
    r"(?P<quote>['\"])(?P<name>.*?)(?P=quote)(?P<attributes>,.*)?$"
)
_UI_STATE_ATTRIBUTES = frozenset(
    {
        "autocomplete",
        "checked",
        "disabled",
        "expanded",
        "haspopup",
        "multiselectable",
        "orientation",
        "pressed",
        "required",
        "selected",
        "value",
    }
)
_UI_ROLE_NAMES = {
    "LabelText": "label",
    "RootWebArea": "page",
    "StaticText": "text",
}


def _clean_accessibility_name(value: str) -> str:
    return " ".join(
        "".join(character for character in value if unicodedata.category(character) != "Co")
        .replace("\\'", "'")
        .replace('\\"', '"')
        .split()
    )


def _ui_state_attributes(raw_attributes: str) -> str:
    selected: list[str] = []
    for raw_attribute in _split_accessibility_attributes(raw_attributes.removeprefix(",")):
        attribute = raw_attribute.strip()
        key, separator, value = attribute.partition("=")
        if separator and key.casefold() in _UI_STATE_ATTRIBUTES:
            selected.append(f"{key}={value}")
        elif not separator and key.casefold() == "disabled":
            selected.append(key)
    return ", ".join(selected)


def _split_accessibility_attributes(value: str) -> list[str]:
    attributes: list[str] = []
    current: list[str] = []
    quote: str | None = None
    escaped = False
    for character in value:
        if escaped:
            current.append(character)
            escaped = False
            continue
        if character == "\\" and quote is not None:
            current.append(character)
            escaped = True
            continue
        if character in {"'", '"'}:
            quote = None if quote == character else character if quote is None else quote
        if character == "," and quote is None:
            attributes.append("".join(current))
            current = []
            continue
        current.append(character)
    attributes.append("".join(current))
    return attributes


def _accessibility_inventory(
    observation: OperationalObservation,
    *,
    max_chars: int = MAX_UI_INVENTORY_CHARS,
) -> tuple[str | None, int, bool]:
    prefix = "Observed UI inventory:"
    effective_max_chars = min(max_chars, MAX_UI_INVENTORY_CHARS)
    if effective_max_chars <= len(prefix) + 1:
        has_accessibility_evidence = any(
            "profile=accessibility-tree" in evidence.content_type.casefold()
            for evidence in observation.evidence
        )
        return None, 0, has_accessibility_evidence
    available_chars = effective_max_chars - len(prefix) - 1
    inventory: list[str] = []
    seen: set[tuple[str, str, str]] = set()
    truncated = False
    used_chars = 0
    for evidence in observation.evidence:
        if "profile=accessibility-tree" not in evidence.content_type.casefold():
            continue
        for raw_line in evidence.content.splitlines():
            match = _ACCESSIBILITY_NODE_PATTERN.fullmatch(raw_line.strip())
            if match is None:
                continue
            name = _clean_accessibility_name(match.group("name"))
            if not name:
                continue
            role = _UI_ROLE_NAMES.get(match.group("role"), match.group("role").casefold())
            attributes = _ui_state_attributes(match.group("attributes") or "")
            key = (role, name, attributes)
            if key in seen:
                continue
            entry = f"- {role}: {name}"
            if attributes:
                entry += f" [{attributes}]"
            entry_budget = available_chars - used_chars - 1
            if len(inventory) >= MAX_UI_INVENTORY_ITEMS or entry_budget <= 0:
                truncated = True
                break
            if len(entry) > entry_budget:
                if entry_budget <= 3:
                    entry = "." * entry_budget
                else:
                    entry = entry[: entry_budget - 3].rstrip() + "..."
                truncated = True
            inventory.append(entry)
            seen.add(key)
            used_chars += len(entry) + 1
            if truncated:
                break
        if truncated:
            break
    if not inventory:
        return None, 0, truncated
    return prefix + "\n" + "\n".join(inventory), len(inventory), truncated


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


def _procedure_segments(
    experience: OperationalExperience,
    observations: list[OperationalObservation],
) -> list[tuple[str, list[OperationalObservation]]]:
    header = f"Goal: {experience.goal}"
    footer = f"Reported outcome: {experience.outcome}" if experience.outcome else None
    action_observations = [observation for observation in observations if observation.action]
    groups: list[list[OperationalObservation]] = []
    current: list[OperationalObservation] = []
    reserved_chars = len(header) + (len(footer) if footer else 0) + 2
    for observation in action_observations:
        candidate_chars = reserved_chars + sum(
            len(f"{item.ordinal}. {item.action}") + 1 for item in [*current, observation]
        )
        if current and candidate_chars > MAX_TYPED_ENTITY_CONTENT_CHARS:
            groups.append(current)
            current = []
        current.append(observation)
    if current or not groups:
        groups.append(current)

    segments: list[tuple[str, list[OperationalObservation]]] = []
    for index, group in enumerate(groups):
        lines = [header]
        lines.extend(f"{item.ordinal}. {item.action}" for item in group)
        if index == len(groups) - 1 and footer:
            lines.append(footer)
        support = list(group)
        if index == 0 and observations[0] not in support:
            support.insert(0, observations[0])
        if index == len(groups) - 1 and observations[-1] not in support:
            support.append(observations[-1])
        segments.append(("\n".join(lines), support))
    return segments


def _validate_typed_entity_sizes(entities: Iterable[Entity]) -> None:
    oversized = [
        entity.id
        for entity in entities
        if entity.entity_type in {EntityType.EVENT, EntityType.PROCEDURE, EntityType.ERROR_PATTERN}
        and len(entity.content or "") > MAX_TYPED_ENTITY_CONTENT_CHARS
    ]
    if oversized:
        raise ValueError(
            "operational projection exceeds the typed entity content limit: " + ", ".join(oversized)
        )


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
        event_base_content = _clean_lines(
            (
                f"Goal: {experience.goal}",
                f"Action: {current.action}",
                f"Before URI: {previous.uri}" if previous.uri else None,
                f"After URI: {current.uri}" if current.uri else None,
                f"Observed reasoning: {current.reasoning}" if current.reasoning else None,
            )
        )
        ui_inventory, ui_inventory_item_count, ui_inventory_truncated = _accessibility_inventory(
            current,
            max_chars=MAX_TYPED_ENTITY_CONTENT_CHARS - len(event_base_content) - 1,
        )
        event_id = _stable_id("event", "operational-transition", experience.source_id, current.id)
        entities.append(
            Entity(
                id=event_id,
                entity_type=EntityType.EVENT,
                name=f"Action at observation {current.ordinal}",
                content=_clean_lines((event_base_content, ui_inventory)),
                organization_id=organization_id,
                created_by=created_by,
                modified_by=created_by,
                metadata={
                    **common,
                    "projection_kind": "transition",
                    "source_observation_ids": [previous.id, current.id],
                    "support_spans": [_support_span(previous), _support_span(current)],
                    "ui_inventory_item_count": ui_inventory_item_count,
                    "ui_inventory_truncated": ui_inventory_truncated,
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

    procedure_segments = _procedure_segments(experience, observations)
    for segment_index, (content, support) in enumerate(procedure_segments):
        procedure_id = _stable_id(
            "procedure",
            "operational",
            experience.source_id,
            segment_index,
        )
        entities.append(
            Entity(
                id=procedure_id,
                entity_type=EntityType.PROCEDURE,
                name=(
                    f"Procedure for {experience.goal[:100]} "
                    f"part {segment_index + 1}/{len(procedure_segments)}"
                ),
                content=content,
                organization_id=organization_id,
                created_by=created_by,
                modified_by=created_by,
                metadata={
                    **common,
                    "projection_kind": "procedure",
                    "procedure_part_index": segment_index,
                    "procedure_part_count": len(procedure_segments),
                    "reported_outcome": experience.outcome,
                    "source_observation_ids": [item.id for item in support],
                    "support_spans": [_support_span(item) for item in support],
                },
            )
        )
        for observation in support:
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

    _validate_typed_entity_sizes(entities)
    projected_ids = [entity.id for entity in entities]
    manifest_id = operational_experience_manifest_id(experience.source_id)
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
                "operational_projection_state": MANIFEST_STATE_COMPLETE,
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


def operational_experience_manifest_id(source_id: str) -> str:
    """Return the lock and ownership anchor for an operational source."""
    return _stable_id("artifact", "operational-manifest", source_id)


def _manifest_matches_state(
    entity: Entity | None,
    manifest: OperationalExperienceManifest,
    state: OperationalManifestState,
) -> bool:
    if entity is None or entity.entity_type is not EntityType.ARTIFACT:
        return False
    return (
        entity.metadata.get("operational_projection_state") == state
        and entity.metadata.get("operational_schema_version") == manifest.schema_version
        and entity.metadata.get("operational_content_hash") == manifest.content_hash
        and _manifest_inventory(entity, "expected_entity_ids") == set(manifest.entity_ids)
        and _manifest_inventory(entity, "expected_relationship_ids")
        == set(manifest.relationship_ids)
    )


def _manifest_entity(projection: OperationalExperienceProjection) -> Entity:
    return next(
        entity
        for entity in projection.entities
        if entity.id == projection.manifest.manifest_entity_id
    )


def operational_experience_manifest_with_state(
    projection: OperationalExperienceProjection,
    state: OperationalManifestState,
    *,
    retained_manifest: Entity | None = None,
) -> Entity:
    """Build a non-authoritative or complete manifest publication."""
    manifest_entity = _manifest_entity(projection)
    metadata = {
        **manifest_entity.metadata,
        "operational_projection_state": state,
    }
    if retained_manifest is not None:
        metadata["expected_entity_ids"] = sorted(
            set(projection.manifest.entity_ids)
            | _manifest_inventory(retained_manifest, "expected_entity_ids")
        )
        metadata["expected_relationship_ids"] = sorted(
            set(projection.manifest.relationship_ids)
            | _manifest_inventory(retained_manifest, "expected_relationship_ids")
        )
    elif state == MANIFEST_STATE_PENDING:
        metadata["expected_entity_ids"] = []
        metadata["expected_relationship_ids"] = []
    return manifest_entity.model_copy(update={"metadata": metadata})


def _require_complete_write(
    *,
    expected_ids: Iterable[str],
    written_ids: Iterable[str],
    record_kind: str,
) -> tuple[str, ...]:
    expected = tuple(expected_ids)
    written = tuple(written_ids)
    missing = sorted(set(expected) - set(written))
    if missing:
        raise RuntimeError(
            f"operational experience failed to persist {record_kind}: {', '.join(missing)}"
        )
    return written


async def persist_operational_experience(
    *,
    entity_manager: OperationalEntityManager,
    relationship_manager: OperationalRelationshipManager,
    experience: OperationalExperience,
    organization_id: str,
    created_by: str | None = None,
    generate_embeddings: bool = False,
    commit_manifest: bool = True,
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

    if _manifest_matches_state(
        previous_manifest,
        projection.manifest,
        MANIFEST_STATE_COMPLETE,
    ):
        return OperationalExperienceWriteResult(
            projection=projection,
            written_entity_ids=(),
            written_relationship_ids=(),
            deleted_entity_ids=(),
            deleted_relationship_ids=(),
        )
    if _manifest_matches_state(
        previous_manifest,
        projection.manifest,
        MANIFEST_STATE_EMBEDDING_PENDING,
    ):
        return OperationalExperienceWriteResult(
            projection=projection,
            written_entity_ids=(),
            written_relationship_ids=(),
            deleted_entity_ids=(),
            deleted_relationship_ids=(),
            embedding_backfill_required=True,
        )

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

    manifest_entity = _manifest_entity(projection)
    projected_entities = tuple(
        entity for entity in projection.entities if entity.id != manifest_entity.id
    )
    pending_manifest = operational_experience_manifest_with_state(
        projection,
        MANIFEST_STATE_PENDING,
        retained_manifest=previous_manifest,
    )
    _require_complete_write(
        expected_ids=(pending_manifest.id,),
        written_ids=await entity_manager.create_direct_bulk(
            (pending_manifest,),
            generate_embeddings=False,
        ),
        record_kind="pending manifest",
    )
    written_entity_ids = _require_complete_write(
        expected_ids=(entity.id for entity in projected_entities),
        written_ids=await entity_manager.create_direct_bulk(
            projected_entities,
            generate_embeddings=generate_embeddings,
        ),
        record_kind="entities",
    )
    written_relationship_ids = _require_complete_write(
        expected_ids=projection.manifest.relationship_ids,
        written_ids=await relationship_manager.create_direct_bulk(
            projection.relationships,
            generate_embeddings=generate_embeddings,
        ),
        record_kind="relationships",
    )

    if stale_relationship_ids:
        await relationship_manager.delete_bulk(stale_relationship_ids)
    deleted_entity_ids: list[str] = []
    for entity_id in stale_entity_ids:
        if await entity_manager.delete(entity_id):
            deleted_entity_ids.append(entity_id)
    final_manifest = (
        manifest_entity
        if commit_manifest
        else operational_experience_manifest_with_state(
            projection,
            MANIFEST_STATE_EMBEDDING_PENDING,
        )
    )
    written_manifest_ids = _require_complete_write(
        expected_ids=(final_manifest.id,),
        written_ids=await entity_manager.create_direct_bulk(
            (final_manifest,),
            generate_embeddings=False,
        ),
        record_kind="manifest",
    )

    return OperationalExperienceWriteResult(
        projection=projection,
        written_entity_ids=tuple(dict.fromkeys([*written_entity_ids, *written_manifest_ids])),
        written_relationship_ids=tuple(written_relationship_ids),
        deleted_entity_ids=tuple(deleted_entity_ids),
        deleted_relationship_ids=stale_relationship_ids,
        embedding_backfill_required=not generate_embeddings and not commit_manifest,
    )
