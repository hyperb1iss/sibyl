"""Normalize foreign archive rows into inert, explicitly mapped preview candidates."""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import Any, Literal

from sibyl_core.auth.memory_policy import MEMORY_PROVENANCE_METADATA_KEYS
from sibyl_core.memory_pipeline.observations import SourceKind
from sibyl_core.migrate.graph_companions import relationship_from_archive
from sibyl_core.migrate.personal_archive_intake import ArchiveIntakeError, ParsedPersonalArchive
from sibyl_core.migrate.personal_archive_plan import (
    ArchiveAudience,
    ArchiveDisposition,
    ArchiveKind,
    ArchiveMappings,
    ArchiveSourceOrigin,
    PlannedArchiveRow,
    archive_digest,
    canonical_json,
    destination_identity,
)
from sibyl_core.migrate.source_integrity import validate_integrity_archive
from sibyl_core.models.entities import Entity, EntityType
from sibyl_core.models.memory_scope import MemoryScope
from sibyl_core.services.graph_records import entity_from_surreal_row

_CONTENT_KINDS = {
    "crawl_sources": ArchiveKind.CRAWL_SOURCE,
    "crawled_documents": ArchiveKind.CRAWLED_DOCUMENT,
    "document_chunks": ArchiveKind.DOCUMENT_CHUNK,
    "eval_attempts": ArchiveKind.EVAL_ATTEMPT,
    "eval_consolidations": ArchiveKind.EVAL_CONSOLIDATION,
    "api_idempotency_records": ArchiveKind.IDEMPOTENCY_RECORD,
    "source_imports": ArchiveKind.SOURCE_IMPORT,
    "content_changefeed_cursors": ArchiveKind.CHANGEFEED_CURSOR,
    "derived_from": ArchiveKind.DERIVED_FROM,
    "chunk_of": ArchiveKind.CHUNK_OF,
    "supersedes": ArchiveKind.SUPERSEDES,
    "extracted_into": ArchiveKind.EXTRACTED_INTO,
    "system_settings": ArchiveKind.SYSTEM_SETTING,
    "telemetry_rollups": ArchiveKind.TELEMETRY_ROLLUP,
    "backup_settings": ArchiveKind.BACKUP_SETTING,
    "backups": ArchiveKind.BACKUP_RECORD,
    "dream_source_checkpoints": ArchiveKind.DREAM_CHECKPOINT,
    "dream_source_cursors": ArchiveKind.DREAM_CURSOR,
    "memory_validation_executions": ArchiveKind.VALIDATION_EXECUTION,
    "memory_validation_attempts": ArchiveKind.VALIDATION_ATTEMPT,
}
_WORK_ITEMS = frozenset({"task", "epic", "milestone"})
_PROTECTED_KEYS = MEMORY_PROVENANCE_METADATA_KEYS | {
    "reflection_identity",
    "origin_execution_id",
    "operational_source_binding",
}
_RAW_MIRROR_FIELDS = (
    "raw_content",
    "title",
    "entity_type",
    "source_id",
    "principal_id",
    "memory_scope",
    "scope_key",
    "agent_id",
    "project_id",
    "review_state",
    "metadata",
    "tags",
)
_GRAPH_MIRROR_FIELDS = ("id", "entity_type", "name", "description", "content", "metadata")


@dataclass(frozen=True, slots=True)
class ArchiveCandidate:
    kind: ArchiveKind
    original_id: str
    audience: ArchiveAudience
    protection: Literal["ordinary", "protected", "retired", "inert"]
    semantic_json: str
    reason: str
    original_endpoint_ids: tuple[str, ...] = ()
    fixed_destination_id: str | None = None
    declarations: int = 1

    @property
    def semantic_sha256(self) -> str:
        return archive_digest(
            "sibyl-archive-candidate-v1",
            {
                "kind": self.kind.value,
                "body": self.semantic_json,
                "audience": self.audience.model_dump(mode="json"),
                "protection": self.protection,
            },
        )

    def initial_preview(
        self, *, organization_id: str, actor_id: str, origin: ArchiveSourceOrigin
    ) -> PlannedArchiveRow:
        disposition = (
            ArchiveDisposition.SKIPPED
            if self.fixed_destination_id is not None
            else ArchiveDisposition.CREATED
            if self.protection == "ordinary"
            else ArchiveDisposition.QUARANTINED
        )
        destination_id = self.fixed_destination_id
        if destination_id is None and self.protection == "ordinary":
            destination_id = destination_identity(
                organization_id=organization_id,
                actor_id=actor_id,
                origin=origin,
                kind=self.kind,
                original_id=self.original_id,
                audience=self.audience,
            )
        return PlannedArchiveRow(
            kind=self.kind,
            original_id=self.original_id,
            destination_id=destination_id,
            audience=self.audience,
            disposition=disposition,
            reason=self.reason,
            semantic_sha256=self.semantic_sha256,
            protection=self.protection,
            declarations=self.declarations,
        )


def _rows(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise ArchiveIntakeError("archive collection must contain row objects")
    return value


def _tables(content: dict[str, object] | None) -> dict[str, list[dict[str, Any]]]:
    value = content.get("tables", {}) if content is not None else {}
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ArchiveIntakeError("archive tables must be a named row collection")
    return {key: _rows(rows) for key, rows in value.items()}


def _identity(row: dict[str, Any], *, fallback: str | None = None) -> str:
    for field in ("uuid", "id", "key", "name"):
        value = row.get(field)
        if value is not None:
            if not isinstance(value, str) or not value.strip():
                raise ArchiveIntakeError("archive row identity must be a nonempty string")
            return value
    if fallback is not None:
        return fallback
    raise ArchiveIntakeError("archive row has no supported logical identity")


def _foreign_scope(row: dict[str, Any], origin: ArchiveSourceOrigin) -> None:
    for field in ("organization_id", "group_id"):
        value = row.get(field)
        if value is not None and value != origin.organization_id:
            raise ArchiveIntakeError("archive row is outside its declared source organization")


def _audience(
    fields: dict[str, Any],
    mappings: ArchiveMappings,
    *,
    actor_id: str,
    entity_type: str | None = None,
) -> tuple[ArchiveAudience, str | None]:
    scope = fields.get("memory_scope")
    key = fields.get("scope_key")
    if scope is None and entity_type in _WORK_ITEMS and fields.get("project_id"):
        scope, key = "project", fields["project_id"]
    if scope is None:
        return mappings.quarantine, "legacy_audience_unresolved"
    if not isinstance(scope, str) or scope not in {value.value for value in MemoryScope}:
        raise ArchiveIntakeError("archive row has an unsupported audience schema")
    if scope == "private":
        owner = fields.get("principal_id") or key
        if owner != mappings.source_private_owner_id or (
            key is not None and key != mappings.source_private_owner_id
        ):
            raise ArchiveIntakeError("archive private owner is ambiguous or unmapped")
        return ArchiveAudience(memory_scope="private", scope_key=actor_id), None
    if scope in {"project", "team"}:
        if not isinstance(key, str) or not key:
            raise ArchiveIntakeError("archive scoped row requires an explicit source scope key")
        mapping = mappings.projects if scope == "project" else mappings.teams
        if key not in mapping:
            raise ArchiveIntakeError("archive audience has no explicit destination mapping")
        if scope == "project" and fields.get("project_id") not in (None, key):
            raise ArchiveIntakeError("archive project audience declarations differ")
        return ArchiveAudience(memory_scope=scope, scope_key=mapping[key]), None
    return mappings.quarantine, "legacy_audience_quarantined"


def _protection(
    row: dict[str, Any],
    *,
    kind: SourceKind,
    current: bool,
    state: dict[str, Any] | None,
    has_association: bool,
) -> Literal["ordinary", "protected", "retired"]:
    if (state is not None and state["deleted"]) or row.get("deleted_at") is not None:
        return "retired"
    metadata = row.get("metadata", {})
    if (
        row.get("derivation_required") is True
        or has_association
        or (
            isinstance(metadata, dict)
            and any(metadata.get(key) not in (None, "", False) for key in _PROTECTED_KEYS)
        )
        or (
            not current
            and (kind is SourceKind.RAW_CAPTURE or row.get("entity_type") not in _WORK_ITEMS)
        )
    ):
        return "protected"
    return "ordinary"


def _source_collection(
    payload: dict[str, Any] | None, *, kind: SourceKind, origin: ArchiveSourceOrigin
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], set[str]]:
    if payload is None:
        return [], {}, set()
    section = payload.get("source_integrity")
    if section is None:
        return [], {}, set()
    if payload.get("version") not in (
        {"3.0"} if kind is SourceKind.GRAPH_ENTITY else {"2.0", "2.1", "2.2", "2.3"}
    ):
        raise ArchiveIntakeError("legacy archive cannot claim current source lineage")
    rows, states, associations = validate_integrity_archive(
        section, kind=kind, organizations=[origin.organization_id]
    )
    return (
        rows,
        {row["source_id"]: row for row in states},
        {row["target_id"] for row in associations},
    )


def _coalesce(rows: list[ArchiveCandidate]) -> tuple[ArchiveCandidate, ...]:
    grouped: dict[tuple[ArchiveKind, str], ArchiveCandidate] = {}
    for row in rows:
        key = row.kind, row.original_id
        old = grouped.get(key)
        if old is None:
            grouped[key] = row
        elif replace(old, declarations=1) != replace(row, declarations=1):
            raise ArchiveIntakeError("differing duplicate archive logical identity")
        else:
            grouped[key] = replace(old, declarations=old.declarations + row.declarations)
    return tuple(grouped[key] for key in sorted(grouped))


def normalize_archive_candidates(
    parsed: ParsedPersonalArchive, mappings: ArchiveMappings, *, actor_id: str
) -> tuple[ArchiveCandidate, ...]:
    """Build previews without adopting foreign evidence, memberships or histories."""
    mappings = ArchiveMappings.model_validate(mappings.model_dump(mode="python"))
    rows: list[ArchiveCandidate] = []
    graph, content, origin = parsed.graph, parsed.content, parsed.origin
    graph_sources, graph_states, graph_associations = _source_collection(
        graph, kind=SourceKind.GRAPH_ENTITY, origin=origin
    )
    raw_sources, raw_states, raw_associations = _source_collection(
        content, kind=SourceKind.RAW_CAPTURE, origin=origin
    )
    graph_by_id: dict[str, ArchiveCandidate] = {}
    graph_public = _rows(graph.get("entities", [])) if graph is not None else []
    graph_current = graph is not None and graph.get("version") == "3.0"
    graph_source_ids = {_identity(row) for row in graph_sources}
    if graph_current and graph_source_ids != {_identity(row) for row in graph_public}:
        raise ArchiveIntakeError("current graph mirrors differ from canonical source inventory")
    graph_bodies: dict[str, str] = {}
    for native in graph_sources:
        EntityType(native["entity_type"])
        entity = entity_from_surreal_row(native)
        graph_bodies[entity.id] = canonical_json(
            {key: entity.model_dump(mode="json")[key] for key in _GRAPH_MIRROR_FIELDS}
        )
    all_graph = [(row, True) for row in graph_sources] + [(row, False) for row in graph_public]
    for record, stored in all_graph:
        _foreign_scope(record, origin)
        if stored:
            entity = entity_from_surreal_row(record)
            public = entity.model_dump(mode="json")
        else:
            entity = Entity.model_validate(record)
            public = entity.model_dump(mode="json")
            if graph_current and entity.id not in graph_source_ids:
                raise ArchiveIntakeError("current graph mirror has no canonical source row")
        body = canonical_json({key: public[key] for key in _GRAPH_MIRROR_FIELDS})
        if not stored and entity.id in graph_bodies:
            if body != graph_bodies[entity.id]:
                raise ArchiveIntakeError("differing graph mirror body")
            canonical = graph_by_id[entity.id]
            rows.append(replace(canonical, declarations=1))
            continue
        attributes = dict(entity.metadata)
        protection = _protection(
            {**record, "metadata": attributes, "entity_type": entity.entity_type.value},
            kind=SourceKind.GRAPH_ENTITY,
            current=graph_current,
            state=graph_states.get(entity.id),
            has_association=entity.id in graph_associations,
        )
        fixed = None
        if entity.entity_type in {EntityType.PROJECT, EntityType.TEAM}:
            mapping = (
                mappings.projects if entity.entity_type is EntityType.PROJECT else mappings.teams
            )
            if entity.id not in mapping:
                raise ArchiveIntakeError("archive project or team anchor is unmapped")
            scope = "project" if entity.entity_type is EntityType.PROJECT else "team"
            audience = ArchiveAudience(memory_scope=scope, scope_key=mapping[entity.id])
            fixed = mapping[entity.id] if protection != "retired" else None
            reason = "existing_mapped_anchor" if fixed else "retired_anchor_quarantined"
        else:
            audience, reason = _audience(
                attributes, mappings, actor_id=actor_id, entity_type=entity.entity_type.value
            )
            if reason is not None and protection == "ordinary":
                protection = "protected"
        candidate = ArchiveCandidate(
            kind=ArchiveKind.GRAPH_ENTITY,
            original_id=entity.id,
            audience=audience,
            protection=protection,
            semantic_json=body,
            reason=reason or "canonical_candidate",
            fixed_destination_id=fixed,
        )
        graph_by_id.setdefault(entity.id, candidate)
        rows.append(candidate)

    tables = _tables(content)
    raw_current = content is not None and content.get("version") != "1.0"
    if raw_current and {_identity(row) for row in raw_sources} != {
        _identity(row) for row in tables.get("raw_captures", [])
    }:
        raise ArchiveIntakeError("current raw mirrors differ from canonical source inventory")
    raw_body_by_id = {
        _identity(row): canonical_json({key: row.get(key) for key in _RAW_MIRROR_FIELDS})
        for row in raw_sources
    }
    canonical_raw: dict[str, ArchiveCandidate] = {}
    for record, stored in [(row, True) for row in raw_sources] + [
        (row, False) for row in tables.get("raw_captures", [])
    ]:
        _foreign_scope(record, origin)
        identity = _identity(record)
        if not isinstance(record.get("raw_content"), str):
            raise ArchiveIntakeError("archive raw content must be a string")
        body = canonical_json({key: record.get(key) for key in _RAW_MIRROR_FIELDS})
        if not stored and identity in raw_body_by_id:
            if body != raw_body_by_id[identity]:
                raise ArchiveIntakeError("differing raw capture mirror body")
            canonical = canonical_raw[identity]
            mirror_protection = _protection(
                record,
                kind=SourceKind.RAW_CAPTURE,
                current=raw_current,
                state=raw_states.get(identity),
                has_association=identity in raw_associations,
            )
            if canonical.protection != mirror_protection:
                raise ArchiveIntakeError("differing raw capture mirror protection")
            rows.append(replace(canonical, declarations=1))
            continue
        if raw_current and not stored:
            raise ArchiveIntakeError("current raw mirror has no canonical source row")
        audience, reason = _audience(record, mappings, actor_id=actor_id)
        protection = _protection(
            record,
            kind=SourceKind.RAW_CAPTURE,
            current=raw_current,
            state=raw_states.get(identity),
            has_association=identity in raw_associations,
        )
        if reason is not None and protection == "ordinary":
            protection = "protected"
        candidate = ArchiveCandidate(
            kind=ArchiveKind.RAW_CAPTURE,
            original_id=identity,
            audience=audience,
            protection=protection,
            semantic_json=body,
            reason=reason or "canonical_candidate",
        )
        canonical_raw.setdefault(identity, candidate)
        rows.append(candidate)

    for table, collection in tables.items():
        if table == "raw_captures":
            continue
        for record in collection:
            _foreign_scope(record, origin)
            identity = _identity(
                record, fallback=origin.organization_id if table == "dream_source_cursors" else None
            )
            if table == "entity":
                if record.keys() - {"id", "uuid", "organization_id", "created_at", "updated_at"}:
                    raise ArchiveIntakeError(
                        "content entity reference contains unsupported body fields"
                    )
                canonical = graph_by_id.get(identity)
                if canonical is not None:
                    rows.append(replace(canonical, declarations=1))
                    continue
                kind = ArchiveKind.GRAPH_ENTITY
            else:
                kind = _CONTENT_KINDS[table]
            rows.append(
                ArchiveCandidate(
                    kind=kind,
                    original_id=identity,
                    audience=mappings.quarantine,
                    protection="inert",
                    semantic_json=canonical_json(record),
                    reason=(
                        "foreign_ingestion_authority_quarantined"
                        if table in {"crawl_sources", "crawled_documents", "document_chunks"}
                        else "foreign_runtime_row_quarantined"
                    ),
                )
            )

    for kind, payload in ((SourceKind.GRAPH_ENTITY, graph), (SourceKind.RAW_CAPTURE, content)):
        if payload is None:
            continue
        section = payload.get("source_integrity")
        if isinstance(section, dict):
            for field, archive_kind, identity_field in (
                ("source_states", ArchiveKind.SOURCE_STATE, "source_id"),
                ("derivations", ArchiveKind.SOURCE_ASSOCIATION, "target_id"),
            ):
                for record in section[field]:
                    rows.append(
                        ArchiveCandidate(
                            kind=archive_kind,
                            original_id=kind.value + ":" + record[identity_field],
                            audience=mappings.quarantine,
                            protection="inert",
                            semantic_json=canonical_json(record),
                            reason="foreign_source_authority_quarantined",
                        )
                    )
        receipts = payload.get("validation_receipts")
        if isinstance(receipts, dict):
            for record in receipts["executions"]:
                rows.append(
                    ArchiveCandidate(
                        kind=ArchiveKind.VALIDATION_RECEIPT,
                        original_id=record["execution_id"],
                        audience=mappings.quarantine,
                        protection="inert",
                        semantic_json=canonical_json(record),
                        reason="foreign_receipt_quarantined",
                    )
                )

    if graph is not None:
        for field, kind in (
            ("episodes", ArchiveKind.GRAPH_EPISODE),
            ("mentions", ArchiveKind.GRAPH_MENTION),
        ):
            for record in _rows(graph.get(field, [])):
                _foreign_scope(record, origin)
                rows.append(
                    ArchiveCandidate(
                        kind=kind,
                        original_id=_identity(record),
                        audience=mappings.quarantine,
                        protection="inert",
                        semantic_json=canonical_json(record),
                        reason="foreign_history_quarantined",
                    )
                )
        for record in _rows(graph.get("relationships", [])):
            _foreign_scope(record, origin)
            relationship = relationship_from_archive(record)
            if not math.isfinite(relationship.weight):
                raise ArchiveIntakeError("archive relationship weight must be finite")
            source, target = (
                graph_by_id.get(relationship.source_id),
                graph_by_id.get(relationship.target_id),
            )
            protected = (
                relationship.operational_derivation_required
                or relationship.operational_source_binding is not None
                or any(
                    relationship.metadata.get(key) not in (None, "", False)
                    for key in _PROTECTED_KEYS
                )
                or source is None
                or target is None
                or (source.protection != "ordinary" and source.fixed_destination_id is None)
                or (target.protection != "ordinary" and target.fixed_destination_id is None)
            )
            rows.append(
                ArchiveCandidate(
                    kind=ArchiveKind.GRAPH_RELATIONSHIP,
                    original_id=relationship.id,
                    audience=source.audience if source is not None else mappings.quarantine,
                    protection="protected" if protected else "ordinary",
                    semantic_json=canonical_json(
                        {
                            key: value
                            for key, value in relationship.model_dump(mode="json").items()
                            if key != "created_at"
                        }
                    ),
                    reason="dependent_edge_quarantined" if protected else "canonical_candidate",
                    original_endpoint_ids=(relationship.source_id, relationship.target_id),
                )
            )
    return _coalesce(rows)
