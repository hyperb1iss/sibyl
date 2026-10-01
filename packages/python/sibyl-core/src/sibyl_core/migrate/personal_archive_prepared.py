"""Canonical archive rendering and immutable checked record preparation.

These values carry checked content, never current authorization or permission
for a database write. Store-local writers must also fence the checked witnesses
and bind their actual outcomes to the same immutable run.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from sibyl_core.auth.memory_policy import stamp_memory_scope_metadata
from sibyl_core.memory_pipeline.audit import decode_audit_metadata
from sibyl_core.migrate.archive_phase_receipts import ArchivePhaseCredential, ArchiveRunBinding
from sibyl_core.migrate.personal_archive_candidates import (
    _GRAPH_MIRROR_FIELDS,
    ArchiveCandidate,
    normalize_archive_candidates,
)
from sibyl_core.migrate.personal_archive_intake import ParsedPersonalArchive
from sibyl_core.migrate.personal_archive_plan import (
    ArchiveDisposition,
    ArchiveKind,
    CheckedArchivePlan,
    PlannedArchiveRow,
    archive_digest,
    canonical_json,
    checked_plan_bytes,
    checked_plan_digest,
    verify_checked_plan,
)
from sibyl_core.models import Entity
from sibyl_core.services.content_models import RawMemory, raw_memory_from_record
from sibyl_core.services.graph_entity_store import _entity_record
from sibyl_core.services.graph_records import entity_from_surreal_row

_PRIVATE = "private"
_PHYSICAL_METADATA = frozenset(
    {
        "record_id",
        "organization_id",
        "group_id",
        "created_by",
        "modified_by",
        # Destination use is measured locally; foreign usage is staged inertly.
        "last_recalled_at",
        "last_used_at",
        "retrieval_count",
        "citation_count",
        "misled_count",
    }
)

_INLINE_REFERENCES = ("epic_id", "parent_task_id", "task_id", "milestone_id")


def semantic_archive_metadata(value: dict[str, Any]) -> dict[str, Any]:
    return {
        key: item
        for key, item in decode_audit_metadata(value).items()
        if key not in _PHYSICAL_METADATA
    }


def graph_archive_metadata(value: dict[str, Any]) -> dict[str, Any]:
    metadata = semantic_archive_metadata(value)
    # Canonical top-level fields retain the mirrored business values. Storage
    # clocks, revision and writer markers do not change the business body.
    # User timestamps under other metadata keys remain semantic.
    for key in (
        "updated_at",
        "revision",
        "_direct_insert",
        "description",
        "entity_type",
        "source_file",
    ):
        metadata.pop(key, None)
    return metadata


def graph_archive_body(entity: Entity, *, organization_id: str) -> dict[str, Any]:
    """Project ordinary writer defaults without foreign storage authority."""
    # The canonical reader supplies description/content fallbacks and normalizes
    # quality metadata. Mirrored writer fields belong to the top-level body;
    # source revisions remain in the exact destination witnesses.
    canonical = entity_from_surreal_row(_entity_record(entity, group_id=organization_id))
    public = canonical.model_dump(mode="json")
    body = {key: public[key] for key in _GRAPH_MIRROR_FIELDS}
    body["metadata"] = graph_archive_metadata(public["metadata"])
    return body


def raw_archive_body(memory: RawMemory) -> dict[str, Any]:
    """Use canonical capture defaults for both foreign candidates and native rows."""
    return {
        "raw_content": memory.raw_content,
        "title": memory.title,
        "entity_type": memory.entity_type,
        "source_id": memory.source_id,
        "principal_id": memory.principal_id,
        "memory_scope": memory.memory_scope.value,
        "scope_key": memory.scope_key,
        "agent_id": memory.agent_id,
        "project_id": memory.project_id,
        "review_state": memory.review_state,
        "metadata": semantic_archive_metadata(memory.metadata),
        "tags": memory.tags,
    }


def prepare_archive_body(
    candidate: ArchiveCandidate,
    preview: PlannedArchiveRow,
    *,
    actor_id: str,
    node_ids: dict[str, str],
    organization_id: str | None = None,
) -> dict[str, Any]:
    body = json.loads(candidate.semantic_json)
    if candidate.kind is ArchiveKind.GRAPH_ENTITY and not body.get("source_file"):
        source_file = body.get("metadata", {}).get("source_file")
        if isinstance(source_file, str) and source_file:
            body["source_file"] = source_file
    if candidate.kind is ArchiveKind.RAW_CAPTURE:
        body = raw_archive_body(raw_memory_from_record(body))
    if candidate.kind is ArchiveKind.GRAPH_RELATIONSHIP:
        body["metadata"] = edge_archive_metadata(body)
    metadata = stamp_memory_scope_metadata(
        (
            graph_archive_metadata
            if candidate.kind is ArchiveKind.GRAPH_ENTITY
            else semantic_archive_metadata
        )(body.get("metadata", {})),
        memory_scope=candidate.audience.memory_scope,
        scope_key=candidate.audience.scope_key,
        principal_id=actor_id,
    )
    metadata.pop("agent_id", None)
    metadata.pop("project_id", None)
    if candidate.audience.memory_scope == "project":
        metadata["project_id"] = candidate.audience.scope_key
    for key in _INLINE_REFERENCES:
        if key in metadata:
            original = metadata[key]
            if isinstance(original, str) and original in node_ids:
                metadata[key] = node_ids[original]
            else:
                # An unmapped foreign reference is retained only in the inert
                # artifact. It cannot point at an unrelated destination row.
                metadata.pop(key)
    body["metadata"] = metadata
    if candidate.kind is ArchiveKind.RAW_CAPTURE:
        body.update(
            source_id=preview.destination_id,
            principal_id=actor_id,
            memory_scope=candidate.audience.memory_scope,
            scope_key=None
            if candidate.audience.memory_scope == _PRIVATE
            else candidate.audience.scope_key,
            agent_id=None,
            project_id=candidate.audience.scope_key
            if candidate.audience.memory_scope == "project"
            else None,
        )
    else:
        body["id"] = preview.destination_id
    if candidate.kind is ArchiveKind.GRAPH_RELATIONSHIP:
        body["source_id"], body["target_id"] = preview.endpoint_ids[:2]
    if candidate.kind is ArchiveKind.GRAPH_ENTITY:
        if organization_id is None:
            raise ValueError("canonical graph rendering requires a destination organization")
        body = graph_archive_body(Entity.model_validate(body), organization_id=organization_id)
    return body


def edge_archive_metadata(body: dict[str, Any]) -> dict[str, Any]:
    metadata = dict(body["metadata"])
    # The ordinary writer/read adapter adds these redundant defaults. Other
    # facts and nonempty episode inventories remain part of the semantic body.
    metadata.pop("weight", None)
    if metadata.get("source_id") == body["source_id"]:
        metadata.pop("source_id")
    if metadata.get("episodes") == []:
        metadata.pop("episodes")
    default_fact = f"{body['source_id']} {body['relationship_type'].lower()} {body['target_id']}"
    if metadata.get("fact") in (None, "", default_fact):
        metadata.pop("fact", None)
    return metadata


def archive_semantic_body(body: dict[str, Any], kind: ArchiveKind) -> str:
    if kind is ArchiveKind.GRAPH_RELATIONSHIP:
        body = {**body, "metadata": edge_archive_metadata(body)}
    return canonical_json(body)


def destination_semantic_digest(row: PlannedArchiveRow, body: dict[str, Any]) -> str:
    """Bind the same canonical destination body used by checked preview."""
    return archive_digest(
        "sibyl-archive-destination-v1",
        {
            "kind": row.kind.value,
            "body": archive_semantic_body(body, row.kind),
            "audience": row.audience.model_dump(mode="json"),
        },
    )


@dataclass(frozen=True, slots=True)
class PreparedArchiveRow:
    """Immutable row snapshots; materialization returns independent values."""

    row_json: str
    body_json: str | None

    @property
    def row(self) -> PlannedArchiveRow:
        return PlannedArchiveRow.model_validate_json(self.row_json)

    @property
    def body(self) -> dict[str, Any] | None:
        if self.body_json is None:
            return None
        value = json.loads(self.body_json)
        if not isinstance(value, dict):
            raise ValueError("prepared archive body must be an object")
        return value


@dataclass(frozen=True, slots=True)
class PreparedArchiveRecords:
    """Checked snapshots bound to IDs supplied by the verified artifact loader.

    UUID validation establishes shape only. Loading the native run/artifact pair
    and checking current authorization remain the caller's responsibility.
    """

    run_id: str
    artifact_id: str
    checked_plan_json: str
    checked_plan_sha256: str
    rows: tuple[PreparedArchiveRow, ...]

    @property
    def plan(self) -> CheckedArchivePlan:
        """Validate the immutable plan and all writable snapshots on every use."""
        plan = verify_checked_plan(self.checked_plan_json, self.checked_plan_sha256)
        self._binding_for(plan)
        if tuple(item.row for item in self.rows) != plan.rows:
            raise ValueError("prepared archive rows differ from the checked plan")
        for item in self.rows:
            row, body = item.row, item.body
            if body is None:
                if row.disposition is ArchiveDisposition.CREATED:
                    raise ValueError("a prepared create requires a canonical body")
                continue
            if (
                row.protection != "ordinary"
                or row.destination_id is None
                or row.kind
                not in {
                    ArchiveKind.RAW_CAPTURE,
                    ArchiveKind.GRAPH_ENTITY,
                    ArchiveKind.GRAPH_RELATIONSHIP,
                }
                or row.disposition not in {ArchiveDisposition.CREATED, ArchiveDisposition.SKIPPED}
                or canonical_json(body) != item.body_json
                or destination_semantic_digest(row, body) != row.semantic_sha256
            ):
                raise ValueError("prepared archive body differs from the checked plan")
        return plan

    def _binding_for(self, plan: CheckedArchivePlan) -> ArchiveRunBinding:
        return ArchiveRunBinding(
            organization_id=plan.organization_id,
            actor_id=plan.actor_id,
            run_id=self.run_id,
            artifact_id=self.artifact_id,
            archive_sha256=plan.archive_sha256,
            artifact_sha256=plan.artifact_sha256,
            mappings_sha256=archive_digest("sibyl-archive-mappings-v1", plan.mappings),
            checked_plan_sha256=self.checked_plan_sha256,
            credential=ArchivePhaseCredential.model_validate(
                plan.credential.model_dump(mode="python")
            ),
        )

    @property
    def binding(self) -> ArchiveRunBinding:
        return self._binding_for(self.plan)


def prepare_archive_records(
    parsed: ParsedPersonalArchive, plan: CheckedArchivePlan, *, run_id: str, artifact_id: str
) -> PreparedArchiveRecords:
    """Recompute checked rendering without querying or granting destination writes."""
    plan = CheckedArchivePlan.model_validate(plan.model_dump(mode="python"))
    plan = verify_checked_plan(checked_plan_bytes(plan), checked_plan_digest(plan))
    if (
        parsed.origin != plan.origin
        or parsed.archive_sha256 != plan.archive_sha256
        or parsed.artifact_sha256 != plan.artifact_sha256
    ):
        raise ValueError("prepared archive origin or artifact binding differs")
    candidates = normalize_archive_candidates(parsed, plan.mappings, actor_id=plan.actor_id)
    by_identity = {(candidate.kind, candidate.original_id): candidate for candidate in candidates}
    if set(by_identity) != {(row.kind, row.original_id) for row in plan.rows}:
        raise ValueError("prepared archive candidate inventory differs")
    initial = {
        key: candidate.initial_preview(
            organization_id=plan.organization_id, actor_id=plan.actor_id, origin=plan.origin
        )
        for key, candidate in by_identity.items()
    }
    node_ids = {
        row.original_id: row.destination_id
        for row in initial.values()
        if row.kind is ArchiveKind.GRAPH_ENTITY and row.destination_id is not None
    }
    records = []
    for row in plan.rows:
        key = row.kind, row.original_id
        candidate, original = by_identity[key], initial[key]
        if (
            row.audience != candidate.audience
            or row.protection != candidate.protection
            or row.declarations != candidate.declarations
            or row.destination_id != original.destination_id
        ):
            raise ValueError("prepared archive identity, audience or protection differs")
        body = None
        digest = candidate.semantic_sha256
        if candidate.protection == "ordinary" and candidate.fixed_destination_id is None:
            metadata = (
                raw_memory_from_record(json.loads(candidate.semantic_json)).metadata
                if candidate.kind is ArchiveKind.RAW_CAPTURE
                else json.loads(candidate.semantic_json).get("metadata", {})
            )
            inline = [
                value
                for field in _INLINE_REFERENCES
                if isinstance(value := metadata.get(field), str) and value in node_ids
            ]
            endpoints = tuple(
                node_ids[value] for value in candidate.original_endpoint_ids if value in node_ids
            )
            endpoints += tuple(
                node_ids[value]
                for value in dict.fromkeys(inline)
                if node_ids[value] not in endpoints
            )
            if row.endpoint_ids != endpoints:
                raise ValueError("prepared archive endpoint binding differs")
            rendered = prepare_archive_body(
                candidate,
                row,
                actor_id=plan.actor_id,
                node_ids=node_ids,
                organization_id=plan.organization_id,
            )
            digest = destination_semantic_digest(row, rendered)
            if row.disposition in {ArchiveDisposition.CREATED, ArchiveDisposition.SKIPPED}:
                body = canonical_json(rendered)
        if row.semantic_sha256 != digest:
            raise ValueError("prepared archive semantic body differs")
        records.append(PreparedArchiveRow(canonical_json(row), body))
    result = PreparedArchiveRecords(
        run_id, artifact_id, checked_plan_bytes(plan), checked_plan_digest(plan), tuple(records)
    )
    _ = result.plan
    return result
