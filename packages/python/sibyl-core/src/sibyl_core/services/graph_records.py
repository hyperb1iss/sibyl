"""Surreal row conversion for native graph entities and relationships."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from enum import Enum
from typing import Any, cast

from sibyl_core.memory_pipeline.quality import (
    expand_memory_quality_storage_metadata,
    normalize_memory_quality_metadata,
)
from sibyl_core.models.entities import (
    Entity,
    EntityType,
    Procedure,
    ProcedureStep,
    Relationship,
    RelationshipType,
)
from sibyl_core.models.tasks import (
    Task,
    TaskComplexity,
    TaskPriority,
    TaskStatus,
)
from sibyl_core.services.graph_common import SurrealRecord

_ENTITY_LIST_FIELDS = "* OMIT content, embedding, name_embedding, attributes.content"
_RELATED_ENTITY_PROJECTION_FIELDS = (
    ("id", "record_id"),
    ("uuid", "uuid"),
    ("name", "name"),
    ("entity_type", "entity_type"),
    ("summary", "summary"),
    ("description", "description"),
    ("labels", "labels"),
    ("attributes", "attributes"),
    ("group_id", "group_id"),
    ("created_at", "created_at"),
    ("updated_at", "updated_at"),
    ("revision", "revision"),
    ("project_id", "project_id"),
    ("epic_id", "epic_id"),
    ("parent_task_id", "parent_task_id"),
    ("task_id", "task_id"),
    ("status", "status"),
    ("priority", "priority"),
    ("complexity", "complexity"),
    ("feature", "feature"),
    ("tags", "tags"),
    ("source_id", "source_id"),
    ("source_ids", "source_ids"),
    ("confidence", "confidence"),
    ("valid_at", "valid_at"),
    ("valid_from", "valid_from"),
    ("valid_to", "valid_to"),
    ("invalid_at", "invalid_at"),
    ("created_by", "created_by"),
    ("modified_by", "modified_by"),
    ("misled_count", "misled_count"),
    ("source_file", "source_file"),
)
_ENTITY_SEARCH_PROJECTION_FIELDS = (
    *_RELATED_ENTITY_PROJECTION_FIELDS,
    ("content", "content"),
)
_ENTITY_SEARCH_FIELDS = ",\n                       ".join(
    f"{field_name} AS {alias}" if field_name != alias else field_name
    for field_name, alias in _ENTITY_SEARCH_PROJECTION_FIELDS
)

_SNAPSHOT_SHADOWED_METADATA_KEYS = frozenset(
    {
        "agent_atomic",
        "agent_spans",
        "memory_probes",
        "probe_last_replay",
        "probe_rehearsal",
    }
)


def _snapshot_without_owned_keys(snapshot: Mapping[Any, Any]) -> dict[str, object]:
    return {
        str(key): value
        for key, value in snapshot.items()
        if str(key) not in _SNAPSHOT_SHADOWED_METADATA_KEYS
    }


def entity_from_surreal_row(row: Mapping[str, object]) -> Entity:
    normalized_row = {str(key): value for key, value in row.items()}
    attributes = _row_attributes(normalized_row)
    metadata = dict(attributes)
    raw_metadata = metadata.get("metadata", normalized_row.get("metadata"))
    if isinstance(raw_metadata, str):
        try:
            parsed = json.loads(raw_metadata)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            metadata.pop("metadata", None)
            metadata = _snapshot_without_owned_keys(parsed) | metadata
    elif isinstance(raw_metadata, dict):
        metadata.pop("metadata", None)
        metadata = _snapshot_without_owned_keys(raw_metadata) | metadata

    if metadata.get("category") is None:
        metadata.pop("category", None)

    for key in (
        "project_id",
        # The scope column is authoritative when attributes lost the stamp: a
        # row whose scope lives only in the column would otherwise parse as
        # unscoped and hand the read path its fail-open.
        "memory_scope",
        "epic_id",
        "parent_task_id",
        "task_id",
        "status",
        "priority",
        "complexity",
        "feature",
        "source_id",
        "source_ids",
        "confidence",
        "valid_at",
        "valid_from",
        "valid_to",
        "invalid_at",
        "created_by",
        "modified_by",
        "last_recalled_at",
        "last_used_at",
        "retrieval_count",
        "citation_count",
        "misled_count",
    ):
        value = normalized_row.get(key)
        if value is not None and metadata.get(key) is None:
            metadata[key] = value
    row_tags = normalized_row.get("tags")
    if row_tags is not None and metadata.get("tags") is None:
        metadata["tags"] = row_tags
    row_retrieval_keys = normalized_row.get("retrieval_keys")
    # An empty column is the cleared state, not a value: a write that removed
    # the keys stores empty lists so the removal reaches the promoted columns,
    # and copying that back would hand callers a key they just deleted.
    if row_retrieval_keys and metadata.get("retrieval_keys") is None:
        metadata["retrieval_keys"] = row_retrieval_keys

    entity_id = _entity_id_from_row(normalized_row)
    record_id = _row_record_id(normalized_row)
    if record_id and record_id != entity_id and metadata.get("record_id") is None:
        metadata["record_id"] = record_id
    metadata = normalize_memory_quality_metadata(metadata)

    entity = Entity(
        id=entity_id,
        entity_type=_entity_type_from_row(normalized_row, attributes=attributes),
        name=_first_text(normalized_row.get("name"), normalized_row.get("title"), entity_id),
        description=_first_text(
            normalized_row.get("description"),
            normalized_row.get("summary"),
            metadata.get("description"),
        ),
        content=_first_content(
            normalized_row.get("content"),
            metadata.get("content"),
            normalized_row.get("summary"),
        ),
        organization_id=_first_text(
            normalized_row.get("group_id"),
            metadata.get("group_id"),
            normalized_row.get("organization_id"),
            metadata.get("organization_id"),
        )
        or None,
        created_by=_first_text(normalized_row.get("created_by"), metadata.get("created_by"))
        or None,
        modified_by=_first_text(normalized_row.get("modified_by"), metadata.get("modified_by"))
        or None,
        revision=max(_int_value(normalized_row.get("revision")), 1),
        metadata=metadata,
        created_at=_row_datetime(normalized_row.get("created_at") or metadata.get("created_at"))
        or datetime.now(UTC),
        updated_at=_row_datetime(normalized_row.get("updated_at") or metadata.get("updated_at"))
        or datetime.now(UTC),
        source_file=_first_text(normalized_row.get("source_file"), metadata.get("source_file"))
        or None,
        embedding=_row_embedding(
            normalized_row.get("name_embedding") or normalized_row.get("embedding")
        ),
    )
    return _coerce_native_entity(entity)


def _entity_from_row(row: SurrealRecord) -> Entity:
    return entity_from_surreal_row(row)


def _related_entity_projection(side: str) -> str:
    return ",\n                       ".join(
        f"{side}.{field_name} AS related_{alias}"
        for field_name, alias in _RELATED_ENTITY_PROJECTION_FIELDS
    )


def _related_entity_from_row(row: Mapping[str, object]) -> Entity | None:
    related_row = {
        key.removeprefix("related_"): value
        for key, value in row.items()
        if key.startswith("related_")
    }
    if not _first_text(related_row.get("uuid")):
        return None
    return entity_from_surreal_row(related_row)


def _entity_select_fields(include_content: bool) -> str:
    return "*" if include_content else _ENTITY_LIST_FIELDS


def _entity_type_from_row(
    row: Mapping[str, object],
    *,
    attributes: Mapping[str, object] | None = None,
) -> EntityType:
    row_attributes = attributes if attributes is not None else _row_attributes(row)
    candidates: list[object] = [
        row.get("entity_type"),
        row_attributes.get("entity_type"),
    ]
    labels = row.get("labels")
    if isinstance(labels, list | tuple):
        candidates.extend(label for label in labels if str(label).lower() != "entity")
    for candidate in candidates:
        value = str(candidate or "").lower()
        if not value:
            continue
        try:
            return EntityType(value)
        except ValueError:
            continue
    return EntityType.ARTIFACT


def _entity_id_from_row(row: Mapping[str, object]) -> str:
    for key in ("uuid", "entity_id"):
        if text := _first_text(row.get(key)):
            return text
    raw_id = row.get("id")
    if raw_id is None:
        raw_id = row.get("record_id")
    if text := _first_text(raw_id):
        return _entity_id_from_record_text(text)
    return ""


def _entity_id_from_record_text(value: str) -> str:
    if ":" not in value:
        return value
    _table, record_key = value.split(":", 1)
    return record_key.strip("`'\"⟨⟩<>") or value


def _row_record_id(row: Mapping[str, object]) -> str | None:
    return _first_text(row.get("record_id"), row.get("id")) or None


def _row_attributes(row: Mapping[str, object]) -> dict[str, object]:
    attributes = row.get("attributes")
    if not isinstance(attributes, Mapping):
        return {}
    return {str(key): value for key, value in attributes.items()}


def _first_text(*values: object) -> str:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _first_content(*values: object) -> str:
    for value in values:
        if value is None:
            continue
        text = str(value)
        if text.strip():
            return text
    return ""


def _coerce_native_entity(entity: Entity) -> Entity:
    if entity.entity_type == EntityType.TASK:
        return _entity_to_task(entity)
    if entity.entity_type == EntityType.PROCEDURE:
        return _entity_to_procedure(entity)
    return entity


def _coerce_enum(enum_type: type[Enum], value: object, default: Enum) -> Enum:
    if value is None:
        return default
    if isinstance(value, enum_type):
        return value
    try:
        return enum_type(value)
    except (TypeError, ValueError):
        return default


def _entity_to_task(entity: Entity) -> Task:
    meta = entity.metadata or {}
    return Task(
        id=entity.id,
        entity_type=EntityType.TASK,
        name=entity.name,
        title=str(meta.get("title") or entity.name),
        description=entity.description or str(meta.get("description") or ""),
        content=entity.content or str(meta.get("content") or ""),
        organization_id=entity.organization_id,
        created_by=entity.created_by,
        modified_by=entity.modified_by,
        revision=entity.revision,
        metadata=meta,
        created_at=entity.created_at,
        updated_at=entity.updated_at,
        source_file=entity.source_file,
        embedding=entity.embedding,
        status=cast("TaskStatus", _coerce_enum(TaskStatus, meta.get("status"), TaskStatus.TODO)),
        priority=cast(
            "TaskPriority",
            _coerce_enum(TaskPriority, meta.get("priority"), TaskPriority.MEDIUM),
        ),
        task_order=_int_value(meta.get("task_order")),
        project_id=_optional_text(meta.get("project_id")),
        epic_id=_optional_text(meta.get("epic_id")),
        parent_task_id=_optional_text(meta.get("parent_task_id")),
        feature=_optional_text(meta.get("feature")),
        sprint=_optional_text(meta.get("sprint")),
        assignees=_metadata_str_list(meta.get("assignees")) or [],
        due_date=_row_datetime(meta.get("due_date")),
        estimated_hours=_float_value(meta.get("estimated_hours")),
        actual_hours=_float_value(meta.get("actual_hours")),
        domain=_optional_text(meta.get("domain")),
        technologies=_metadata_str_list(meta.get("technologies")) or [],
        complexity=cast(
            "TaskComplexity",
            _coerce_enum(TaskComplexity, meta.get("complexity"), TaskComplexity.MEDIUM),
        ),
        tags=_metadata_str_list(meta.get("tags")) or [],
        branch_name=_optional_text(meta.get("branch_name")),
        commit_shas=_metadata_str_list(meta.get("commit_shas")) or [],
        pr_url=_optional_text(meta.get("pr_url")),
        learnings=str(meta.get("learnings") or ""),
        blockers_encountered=_metadata_str_list(meta.get("blockers_encountered")) or [],
        started_at=_row_datetime(meta.get("started_at")),
        completed_at=_row_datetime(meta.get("completed_at")),
        reviewed_at=_row_datetime(meta.get("reviewed_at")),
    )


def _entity_to_procedure(entity: Entity) -> Procedure:
    meta = entity.metadata or {}
    steps: list[ProcedureStep] = []
    for raw_step in meta.get("steps") or []:
        if isinstance(raw_step, ProcedureStep):
            steps.append(raw_step)
        elif isinstance(raw_step, Mapping):
            steps.append(ProcedureStep.model_validate(raw_step))

    return Procedure(
        id=entity.id,
        entity_type=EntityType.PROCEDURE,
        name=entity.name,
        description=entity.description or str(meta.get("description") or ""),
        content=entity.content or str(meta.get("content") or ""),
        organization_id=entity.organization_id,
        created_by=entity.created_by,
        modified_by=entity.modified_by,
        revision=entity.revision,
        metadata=meta,
        created_at=entity.created_at,
        updated_at=entity.updated_at,
        source_file=entity.source_file,
        embedding=entity.embedding,
        steps=steps,
        required_tools=_metadata_str_list(meta.get("required_tools")) or [],
        category=str(meta.get("category") or ""),
        estimated_minutes=_optional_int(meta.get("estimated_minutes")),
        automation_level=str(meta.get("automation_level") or "manual"),
    )


def _optional_text(value: object) -> str | None:
    return _first_text(value) or None


def _int_value(value: object) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return 0
    return 0


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    return _int_value(value)


def _float_value(value: object) -> float | None:
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _row_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


def _row_embedding(value: object) -> list[float] | None:
    if not isinstance(value, list):
        return None
    embedding: list[float] = []
    for item in value:
        if not isinstance(item, int | float):
            return None
        embedding.append(float(item))
    return embedding


def _surreal_indexed_field_missing(field: str) -> str:
    return f"({field} IS NONE OR {field} = '')"


def _surreal_indexed_field_equals_or_missing(field: str) -> str:
    return f"({field} = ${field} OR {_surreal_indexed_field_missing(field)})"


def _surreal_indexed_field_in_or_missing(field: str, param: str) -> str:
    return f"({field} IN ${param} OR {_surreal_indexed_field_missing(field)})"


def relationship_from_surreal_row(row: Mapping[str, object]) -> Relationship:
    normalized_row = {str(key): value for key, value in row.items()}
    attributes = _row_attributes(normalized_row)
    metadata = dict(attributes)
    raw_metadata = metadata.get("metadata", normalized_row.get("metadata"))
    if isinstance(raw_metadata, str):
        try:
            parsed = json.loads(raw_metadata)
        except json.JSONDecodeError:
            metadata.pop("metadata", None)
        else:
            metadata.pop("metadata", None)
            if isinstance(parsed, dict):
                metadata = {str(key): value for key, value in parsed.items()} | metadata
    elif isinstance(raw_metadata, Mapping):
        metadata.pop("metadata", None)
        metadata = {str(key): value for key, value in raw_metadata.items()} | metadata

    source_id, source_key = _relationship_endpoint(normalized_row, "source")
    target_id, _target_key = _relationship_endpoint(normalized_row, "target")
    for key in (
        "fact",
        "fact_embedding",
        "group_id",
        "project_id",
        "source_id",
        "source_ids",
        "confidence",
        "valid_at",
        "valid_from",
        "valid_to",
        "invalid_at",
        "expired_at",
        "created_by",
        "modified_by",
        "direction",
        "episodes",
    ):
        if key == "source_id" and source_key == "source_id":
            continue
        value = normalized_row.get(key)
        if value is not None and metadata.get(key) is None:
            if key == "fact_embedding":
                if vector := _metadata_float_list(value):
                    metadata[key] = vector
            else:
                metadata[key] = value

    relationship_id = _relationship_id_from_row(normalized_row)
    record_id = _row_record_id(normalized_row)
    if record_id and record_id != relationship_id and metadata.get("record_id") is None:
        metadata["record_id"] = record_id
    metadata = normalize_memory_quality_metadata(metadata)

    return Relationship(
        id=relationship_id,
        relationship_type=_relationship_type_from_row(normalized_row, metadata=metadata),
        source_id=source_id,
        target_id=target_id,
        weight=_metadata_weight(metadata),
        metadata=metadata,
        created_at=_row_datetime(normalized_row.get("created_at")) or datetime.now(UTC),
    )


def _relationship_from_row(row: SurrealRecord) -> Relationship:
    return relationship_from_surreal_row(row)


def _relationship_id_from_row(row: Mapping[str, object]) -> str:
    for key in ("uuid", "relationship_id"):
        if text := _first_text(row.get(key)):
            return text
    raw_id = row.get("id")
    if raw_id is None:
        raw_id = row.get("record_id")
    if text := _first_text(raw_id):
        return _entity_id_from_record_text(text)
    return ""


def _relationship_endpoint(row: Mapping[str, object], side: str) -> tuple[str, str | None]:
    for key in (
        f"{side}_uuid",
        f"{side}_node_uuid",
        f"{side}_id",
    ):
        if text := _first_text(row.get(key)):
            return text, key
    return "", None


def _relationship_type_from_row(
    row: Mapping[str, object],
    *,
    metadata: Mapping[str, object] | None = None,
) -> RelationshipType:
    relationship_metadata = metadata or {}
    value = str(
        row.get("name")
        or row.get("relationship_type")
        or row.get("rel_type")
        or relationship_metadata.get("relationship_type")
        or RelationshipType.RELATED_TO.value
    )
    try:
        return RelationshipType(value)
    except ValueError:
        return RelationshipType.RELATED_TO


def _metadata_weight(metadata: Mapping[str, object]) -> float:
    weight = metadata.get("weight")
    if isinstance(weight, int | float):
        return float(weight)
    return 1.0


def _relationship_fact(relationship: Relationship) -> str:
    return (
        f"{relationship.source_id} {relationship.relationship_type.value.lower()} "
        f"{relationship.target_id}"
    )


def _metadata_str(metadata: dict[str, object], key: str) -> str | None:
    value = metadata.get(key)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _metadata_int(value: object) -> int:
    coerced = _metadata_optional_int(value)
    return 0 if coerced is None else coerced


def _metadata_optional_int(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str) and value.strip():
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _metadata_str_list(value: object) -> list[str] | None:
    if not isinstance(value, Iterable) or isinstance(value, str | bytes | dict):
        return None
    return [str(item) for item in value if str(item)]


def _metadata_float_list(value: object) -> list[float] | None:
    if not isinstance(value, Iterable) or isinstance(value, str | bytes | dict):
        return None
    vector: list[float] = []
    for item in value:
        if isinstance(item, int | float | str):
            try:
                vector.append(float(item))
            except ValueError:
                return None
        else:
            return None
    return vector


def _metadata_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


def _entity_metadata(entity: Entity) -> dict[str, object]:
    normalized_metadata = normalize_memory_quality_metadata(entity.metadata or {})
    metadata = {str(key): _jsonable(value) for key, value in normalized_metadata.items()}
    model_dump = entity.model_dump(
        mode="json",
        exclude={
            "id",
            "entity_type",
            "name",
            "description",
            "content",
            "organization_id",
            "created_by",
            "modified_by",
            "metadata",
            "created_at",
            "updated_at",
            "source_file",
            "embedding",
        },
    )
    for key, value in model_dump.items():
        if value not in (None, "", [], {}):
            metadata[key] = _jsonable(value)
    return expand_memory_quality_storage_metadata(metadata)


def _jsonable(value: object) -> object:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return model_dump(mode="json")
    if isinstance(value, dict):
        return {str(key): _jsonable(nested) for key, nested in value.items()}
    if isinstance(value, list | tuple | set):
        return [_jsonable(nested) for nested in value]
    return value


__all__ = ["entity_from_surreal_row", "relationship_from_surreal_row"]
