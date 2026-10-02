"""Pure compilation of the closed canonical graph archive APPLY batch.

Run/artifact loading and current authorization remain caller duties. UUIDs and
saved digests establish neither authority. Native phase composition owns CAS,
actual counts and receipts; this module only supplies fixed application SQL.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, cast
from uuid import UUID

from sibyl_core.auth.memory_policy import MEMORY_PROVENANCE_METADATA_KEYS
from sibyl_core.backends.surreal.schema_source_witness import SOURCE_STATE_WRITE_WITNESS
from sibyl_core.backends.surreal.url_schemes import is_embedded_surreal_url
from sibyl_core.memory_pipeline.lifecycle import graph_metadata_recallable
from sibyl_core.memory_pipeline.source_lifecycle import declared_source_ids
from sibyl_core.migrate.archive_phase_receipts import ArchiveCreatedIdentity, ArchivePhaseKey
from sibyl_core.migrate.personal_archive_plan import (
    ArchiveDisposition,
    ArchiveKind,
    ArchiveStoreWitness,
    PlannedArchiveRow,
)
from sibyl_core.migrate.personal_archive_prepared import (
    PreparedArchiveRecords,
    archive_semantic_body,
    graph_archive_body,
    semantic_archive_metadata,
)
from sibyl_core.migrate.source_integrity import ArchiveDatetime
from sibyl_core.models import Entity, Relationship
from sibyl_core.services.archive_phase_store import (
    PreparedArchivePhaseTransaction,
    prepare_archive_phase_transaction,
)
from sibyl_core.services.archive_raw_compiler import _metadata_expression
from sibyl_core.services.graph_capture_availability import _capture_ids
from sibyl_core.services.graph_entity_store import _entity_record
from sibyl_core.services.graph_records import relationship_from_surreal_row
from sibyl_core.services.graph_relationships import _relationship_record

_GRAPH_KINDS = frozenset(
    {
        ArchiveKind.GRAPH_ENTITY,
        ArchiveKind.GRAPH_RELATIONSHIP,
        ArchiveKind.GRAPH_EPISODE,
        ArchiveKind.GRAPH_MENTION,
    }
)
_INLINE_REFERENCES = ("epic_id", "parent_task_id", "task_id", "milestone_id")
_PROJECTION_TYPES = ("passage", "memory_fact", "proper_noun", "action_object", "quoted_phrase")
_PROVENANCE = MEMORY_PROVENANCE_METADATA_KEYS | {
    "reflection_identity",
    "origin_execution_id",
    "operational_source_binding",
}
_DIGEST = re.compile(r"[0-9a-f]{64}")

# Every checked cut is captured before source fences or canonical row writes.
# The context is explicit because embedded map closures do not capture LETs.
_CUT = """
LET $archive_graph_cut = array::combine($archive_graph_guards, [{
    org: $sibyl_archive_phase_org, scheme: $archive_graph_witness_scheme
}]).map(|$pair| {
    LET $guard = $pair[0];
    LET $context = $pair[1];
    LET $rows = IF $guard.kind = 'graph_entity' THEN
        (SELECT * FROM entity WHERE uuid = $guard.destination_id)
        ELSE (SELECT * FROM relates_to WHERE uuid = $guard.destination_id) END;
    LET $states = IF $guard.kind = 'graph_entity' THEN
        (SELECT * OMIT validation_write_witness FROM source_states
            WHERE organization_id = $context.org AND source_kind = 'graph_entity'
                AND source_id = $guard.destination_id) ELSE [] END;
    LET $associations = IF $guard.kind = 'graph_entity' THEN
        IF $context.scheme = 'graph-association-authority-v2' THEN
            (SELECT * OMIT validation_write_witness FROM memory_derivations
                WHERE organization_id = $context.org AND target_kind = 'graph_entity'
                    AND target_id = $guard.destination_id)
        ELSE (SELECT * FROM memory_derivations
            WHERE organization_id = $context.org AND target_kind = 'graph_entity'
                AND target_id = $guard.destination_id) END ELSE [] END;
    IF array::len($rows) > 1 OR array::len($states) > 1 OR array::len($associations) > 1 {
        THROW 'Archive graph checked inventory is ambiguous';
    };
    LET $row = $rows[0];
    LET $state = $states[0];
    LET $association = $associations[0];
    LET $actual = [
        IF $row = NONE THEN NULL ELSE crypto::sha256(type::string($row)) END,
        IF $state = NONE THEN NULL ELSE crypto::sha256(type::string($state)) END,
        IF $association = NONE THEN NULL ELSE crypto::sha256(type::string($association)) END
    ];
    IF ($row != NONE AND $row.group_id != $context.org) OR $actual != [
        $guard.witness[0] ?? NULL, $guard.witness[1] ?? NULL, $guard.witness[2] ?? NULL
    ] { THROW 'Archive graph checked witness changed'; };
    IF $guard.kind = 'graph_entity' AND $guard.witness[1] != NONE {
        IF $state = NONE OR !type::is::string($state.incarnation)
            OR string::len($state.incarnation) = 0 OR !type::is::int($state.generation)
            OR $state.generation < 1 OR !type::is::int($state.revision)
            OR $state.revision < 0 OR !type::is::bool($state.deleted)
            OR ($row != NONE AND $state.revision != $row.revision)
            OR ($row = NONE AND !$state.deleted) {
            THROW 'Archive graph retained source state is invalid';
        };
    };
    RETURN {guard: $guard, row: $row, state: $state, association: $association};
});
"""

_ORDINARY = """
FOR $cut IN $archive_graph_cut {
    LET $guard = $cut.guard;
    IF $guard.ordinary AND $cut.row != NONE {
        LET $row = $cut.row;
        LET $metadata = $row.attributes;
        IF !type::is::object($metadata) OR $row.derivation_required = true
            OR $row.operational_derivation_required = true
            OR $row.operational_source_binding != NONE OR $row.deleted_at != NONE {
            THROW 'Archive graph retained row is protected';
        };
        IF $guard.kind = 'graph_entity' AND (
            $cut.state = NONE OR $cut.state.deleted OR $cut.association != NONE
            OR $row.entity_type IN $archive_graph_projection_types) {
            THROW 'Archive graph retained node is not ordinary';
        };
        IF object::keys($metadata) CONTAINS 'parent_entity_id'
            OR ($metadata.projection_kind ?? NULL) NOT IN [NULL, '', false, 0, [], {}] {
            THROW 'Archive graph retained row requires projection guards';
        };
        FOR $field IN $archive_graph_provenance_fields {
            IF ($metadata[$field] ?? NULL) NOT IN [NULL, '', false] {
                THROW 'Archive graph retained row requires provenance guards';
            };
        };
        IF ($metadata.source_bindings ?? NULL) != NULL OR ($metadata.raw_memory_id ?? NULL) != NULL {
            THROW 'Archive graph retained row requires capture guards';
        };
        FOR $field IN ['review_capture_id', 'source_entity_id'] {
            LET $value = $metadata[$field] ?? NULL;
            IF $value NOT IN [NULL, '', false] {
                THROW 'Archive graph retained row requires source guards';
            };
        };
        LET $sources = $metadata.raw_source_ids ?? NULL;
        IF $sources != NULL {
            IF type::is::string($sources) {
                IF string::trim($sources) != '' { THROW 'Archive graph retained raw sources are unsupported'; };
            } ELSE IF type::is::array($sources) {
                FOR $source IN $sources {
                    IF !type::is::string($source) OR string::trim($source) != '' {
                        THROW 'Archive graph retained raw sources are unsupported';
                    };
                };
            } ELSE { THROW 'Archive graph retained raw sources are invalid'; };
        };
        IF ($metadata.metadata ?? NULL) NOT IN [NULL, '', false, 0, [], {}]
            OR ($metadata.episodes ?? NULL) NOT IN [NULL, '', false, 0, [], {}] {
            THROW 'Archive graph retained metadata snapshots require explicit guards';
        };
        FOR $field IN ['correction_blockers', 'excluded_from_recall', 'superseded_by_source_id',
            'superseded_by_raw_memory_id', 'duplicate_of_source_id', 'lifecycle_reconciliation_pending'] {
            IF ($metadata[$field] ?? NULL) NOT IN [NULL, '', false] {
                THROW 'Archive graph retained row has lifecycle dependencies';
            };
        };
        IF string::lowercase(string::trim(type::string($metadata.review_state ?? '')))
                IN ['archived', 'deleted', 'hidden', 'redacted', 'superseded']
            OR string::lowercase(string::trim(type::string($metadata.lifecycle_state ?? '')))
                IN ['archived', 'contested', 'deleted', 'superseded'] {
            THROW 'Archive graph retained row is excluded';
        };
        LET $flags = IF type::is::string($metadata.lifecycle_flags) THEN
            [$metadata.lifecycle_flags] ELSE IF type::is::array($metadata.lifecycle_flags)
            THEN $metadata.lifecycle_flags ELSE [] END;
        FOR $flag IN $flags {
            IF string::lowercase(string::trim(type::string($flag ?? '')))
                IN ['hidden', 'redacted', 'sensitive'] {
                THROW 'Archive graph retained lifecycle flag is excluded';
            };
        };
        IF $guard.kind = 'graph_relationship' AND (
            $metadata.memory_scope != $guard.audience.memory_scope
            OR $metadata.principal_id != $guard.actor
            OR ($metadata.scope_key ?? NULL) !=
                (IF $guard.audience.memory_scope = 'private' THEN NULL
                    ELSE $guard.audience.scope_key END)
            OR ($metadata.project_id ?? NULL) !=
                (IF $guard.audience.memory_scope = 'project' THEN $guard.audience.scope_key
                    ELSE NULL END)
            OR $metadata.agent_id != NONE) {
            THROW 'Archive graph retained relationship audience differs';
        };
        IF $guard.kind = 'graph_entity' {
            IF $guard.anchor_type != NONE {
                IF $row.entity_type != $guard.anchor_type
                    OR $guard.audience.memory_scope != $guard.anchor_type
                    OR $guard.audience.scope_key != $row.uuid {
                    THROW 'Archive graph mapped anchor changed';
                };
            } ELSE {
                IF ($row.memory_scope ?? $metadata.memory_scope) != $guard.audience.memory_scope
                    OR $metadata.principal_id != $guard.actor
                    OR ($guard.audience.memory_scope != 'private'
                        AND $metadata.scope_key != $guard.audience.scope_key)
                    OR ($guard.audience.memory_scope = 'private'
                        AND ($metadata.scope_key ?? $guard.actor) != $guard.actor)
                    OR ($guard.audience.memory_scope = 'project'
                        AND $row.project_id != $guard.audience.scope_key) {
                    THROW 'Archive graph retained audience differs from checked binding';
                };
            };
        };
    };
};
FOR $candidate IN $archive_graph_candidates {
    IF $candidate.kind = 'graph_relationship' AND $candidate.disposition IN ['skipped', 'conflicted'] {
        LET $edge = $archive_graph_cut[WHERE guard.kind = 'graph_relationship'
            AND guard.destination_id = $candidate.destination_id][0].row;
        LET $source = $archive_graph_cut[WHERE guard.kind = 'graph_entity'
            AND guard.destination_id = $candidate.endpoint_ids[0]][0].row;
        LET $target = $archive_graph_cut[WHERE guard.kind = 'graph_entity'
            AND guard.destination_id = $candidate.endpoint_ids[1]][0].row;
        IF $edge = NONE OR $source = NONE OR $target = NONE OR $edge.in != $source.id
            OR $edge.out != $target.id
            OR ($edge.source_id != NONE AND $edge.source_id != $source.uuid)
            OR ($edge.target_id != NONE AND $edge.target_id != $target.uuid) {
            THROW 'Archive graph retained edge endpoint binding changed';
        };
    };
};
"""

_FENCES = (
    """
LET $source_states_to_fence = $archive_graph_cut[WHERE guard.kind = 'graph_entity' AND state != NONE].state ?? [];
"""
    + SOURCE_STATE_WRITE_WITNESS
    + """
FOR $cut IN $archive_graph_cut {
    IF $cut.guard.kind = 'graph_relationship' AND $cut.row != NONE {
        LET $old = $cut.row;
        LET $physical = $old.id;
        LET $expected = $cut.guard.witness[0];
        LET $current = (SELECT * FROM relates_to WHERE uuid = $cut.guard.destination_id)[0];
        IF $current = NONE OR $current.id != $physical
            OR crypto::sha256(type::string($current)) != $expected {
            THROW 'Archive graph edge changed before fence';
        };
        UPDATE $physical SET attributes.operational_write_witness = type::string(rand::uuid());
        LET $changed = (SELECT * FROM $physical)[0];
        IF $changed.attributes.operational_write_witness = $old.attributes.operational_write_witness {
            THROW 'Archive graph transient edge fence did not change';
        };
        UPDATE $physical SET attributes = $old.attributes;
        LET $restored = (SELECT * FROM $physical)[0];
        IF crypto::sha256(type::string($restored)) != $expected {
            THROW 'Archive graph restored full edge differs';
        };
    };
};
"""
)

_CREATES = """
FOR $candidate IN $archive_graph_candidates {
    IF $candidate.kind = 'graph_entity' AND $candidate.disposition = 'created' {
        LET $clock = time::now();
        LET $attributes = object::from_entries(array::concat(
            object::entries($archive_graph_attributes[$candidate.attributes_index]),
            [['updated_at', $clock]]
        ));
        LET $record = object::from_entries(array::concat(object::entries($candidate.record),
            [['created_at', $clock], ['updated_at', $clock], ['attributes', $attributes]]));
        CREATE entity CONTENT $record;
    };
};
LET $archive_graph_nodes = SELECT * FROM entity WHERE uuid IN $archive_graph_active_node_ids;
FOR $identity IN $archive_graph_active_node_ids {
    LET $nodes = $archive_graph_nodes[WHERE uuid = $identity];
    IF array::len($nodes) != 1 OR $nodes[0].group_id != $sibyl_archive_phase_org {
        THROW 'Archive graph active native endpoint is missing or ambiguous';
    };
};
FOR $candidate IN $archive_graph_candidates {
    IF $candidate.kind = 'graph_relationship' AND $candidate.disposition = 'created' {
        LET $source = $archive_graph_nodes[WHERE uuid = $candidate.endpoint_ids[0]][0].id;
        LET $target = $archive_graph_nodes[WHERE uuid = $candidate.endpoint_ids[1]][0].id;
        LET $record = object::from_entries(array::concat(object::entries($candidate.record),
            [['created_at', time::now()], ['attributes', $archive_graph_attributes[$candidate.attributes_index]],
                ['expired_at', $archive_graph_dates[$candidate.dates_index][0]],
                ['valid_at', $archive_graph_dates[$candidate.dates_index][1]],
                ['invalid_at', $archive_graph_dates[$candidate.dates_index][2]]]));
        RELATE $source->relates_to->$target CONTENT $record;
    };
};
LET $sibyl_archive_phase_outcomes = $archive_graph_candidates.map(|$candidate| {
    kind: $candidate.kind, disposition: $candidate.disposition, destination_id: $candidate.destination_id
});
"""


def _uuid(value: str | None) -> str:
    if value is None or str(UUID(value)) != value:
        raise ValueError("archive graph identity must be a canonical UUID")
    return value


def _neutral(value: object) -> bool:
    return value is None or value is False or (type(value) is str and value == "")


def _metadata_preflight(metadata: dict[str, Any], *, entity_type: str | None = None) -> None:
    # Parent key presence is significant even for NULL. Capture lists instead
    # permit validated blank declarations: those name no runtime identities.
    if (
        "parent_entity_id" in metadata
        or metadata.get("projection_kind")
        or entity_type in _PROJECTION_TYPES
        or not _neutral(metadata.get("review_capture_id"))
        or not _neutral(metadata.get("source_entity_id"))
        or declared_source_ids(metadata)
        or _capture_ids(metadata)
        or any(not _neutral(metadata.get(field)) for field in _PROVENANCE)
        or metadata.get("metadata")
        or metadata.get("episodes")
        or any(
            metadata.get(field)
            for field in (
                "excluded_from_recall",
                "superseded_by_source_id",
                "superseded_by_raw_memory_id",
                "duplicate_of_source_id",
                "lifecycle_reconciliation_pending",
            )
        )
        or not graph_metadata_recallable(metadata)
    ):
        raise ValueError("archive graph dependency or provenance guards are not implemented")


def _audience(row: PlannedArchiveRow, metadata: dict[str, Any], *, actor_id: str) -> None:
    if (
        metadata.get("memory_scope") != row.audience.memory_scope
        or metadata.get("principal_id") != actor_id
        or metadata.get("scope_key")
        != (None if row.audience.memory_scope == "private" else row.audience.scope_key)
        or metadata.get("project_id")
        != (row.audience.scope_key if row.audience.memory_scope == "project" else None)
        or metadata.get("agent_id") is not None
    ):
        raise ValueError("archive graph body audience differs from its checked binding")


def _node_record(
    row: PlannedArchiveRow, body: dict[str, Any], *, org: str, actor: str
) -> dict[str, object]:
    if row.protection != "ordinary" or body.get("id") != row.destination_id:
        raise ValueError("archive graph canonical node body is invalid")
    entity = Entity.model_validate(body)
    _metadata_preflight(entity.metadata, entity_type=entity.entity_type.value)
    _audience(row, entity.metadata, actor_id=actor)
    entity.organization_id = org
    entity.created_by = actor
    entity.modified_by = actor
    if graph_archive_body(entity, organization_id=org) != body:
        raise ValueError("archive graph node adapter changes its checked semantic body")
    record = _entity_record(entity, group_id=org)
    # The ordinary generated clocks are native server defaults, not foreign
    # history and not Python's reduced-precision datetime transport.
    record.pop("created_at")
    record.pop("updated_at")
    cast(dict[str, object], record["attributes"]).pop("updated_at")
    return record


def _edge_record(
    row: PlannedArchiveRow, body: dict[str, Any], *, org: str, actor: str
) -> dict[str, object]:
    if (
        row.protection != "ordinary"
        or body.get("id") != row.destination_id
        or tuple((body.get("source_id"), body.get("target_id"))) != row.endpoint_ids[:2]
    ):
        raise ValueError("archive graph canonical relationship body is invalid")
    relationship = Relationship.model_validate(body)
    _metadata_preflight(relationship.metadata)
    _audience(row, relationship.metadata, actor_id=actor)
    # The ordinary adapter reads weight from metadata; the checked body owns
    # the top-level weight after redundant metadata was removed by preparation.
    relationship.metadata = {**relationship.metadata, "weight": relationship.weight}
    record = _relationship_record(relationship, group_id=org)
    decoded = relationship_from_surreal_row(record).model_dump(mode="json", exclude={"created_at"})
    decoded["metadata"] = semantic_archive_metadata(decoded["metadata"])
    if archive_semantic_body(decoded, ArchiveKind.GRAPH_RELATIONSHIP) != archive_semantic_body(
        body, ArchiveKind.GRAPH_RELATIONSHIP
    ):
        raise ValueError("archive graph relationship adapter changes its checked semantic body")
    record.pop("created_at")
    # Preserve nanoseconds in ordinary promoted date columns without casting
    # arbitrary date-looking strings elsewhere in semantic metadata.
    for column, fields in {
        "expired_at": ("expired_at",),
        "valid_at": ("valid_at", "valid_from"),
        "invalid_at": ("invalid_at", "valid_to"),
    }.items():
        value = next(
            (
                relationship.metadata.get(field)
                for field in fields
                if relationship.metadata.get(field)
            ),
            None,
        )
        if record[column] is not None and isinstance(value, str):
            record[column] = ArchiveDatetime.parse(value)
    return record


def _witnesses(row: PlannedArchiveRow) -> dict[str, ArchiveStoreWitness]:
    if row.disposition is ArchiveDisposition.QUARANTINED:
        return {}
    identity = _uuid(row.destination_id)
    prefix = "entity:" if row.kind is ArchiveKind.GRAPH_ENTITY else "relates_to:"
    expected = {prefix + identity} | {"entity:" + _uuid(value) for value in row.endpoint_ids}
    values = {witness.identity: witness for witness in row.witnesses}
    if len(values) != len(row.witnesses) or set(values) != expected:
        raise ValueError("archive graph witness inventory differs from checked dependencies")
    for witness in values.values():
        hashes = (witness.row_sha256, witness.state_sha256, witness.associations_sha256)
        if witness.store != "graph" or any(
            value is not None and _DIGEST.fullmatch(value) is None for value in hashes
        ):
            raise ValueError("archive graph witness store or digest is unsupported")
        if witness.identity.startswith("relates_to:") and any(hashes[1:]):
            raise ValueError("archive graph relationships cannot claim source ledgers")
    own = values[prefix + identity]
    if row.disposition is ArchiveDisposition.CREATED and any(
        (own.row_sha256, own.state_sha256, own.associations_sha256)
    ):
        raise ValueError("archive graph create requires exact checked absence")
    if row.disposition in {ArchiveDisposition.SKIPPED, ArchiveDisposition.CONFLICTED}:
        if row.kind is ArchiveKind.GRAPH_ENTITY and own.state_sha256 is None:
            raise ValueError("archive graph retained node requires exact source state")
        if row.kind is ArchiveKind.GRAPH_RELATIONSHIP and own.row_sha256 is None:
            raise ValueError("archive graph retained relationship requires its exact row")
        if row.disposition is ArchiveDisposition.SKIPPED and (
            own.row_sha256 is None or own.associations_sha256 is not None
        ):
            raise ValueError("archive graph skip requires an ordinary retained row")
    return values


def _anchor_type(row: PlannedArchiveRow, projects: dict[str, str], teams: dict[str, str]) -> str:
    scope = row.audience.memory_scope
    mapping = projects if scope == "project" else teams if scope == "team" else {}
    if (
        row.disposition is not ArchiveDisposition.SKIPPED
        or row.protection != "ordinary"
        or row.reason != "existing_mapped_anchor"
        or mapping.get(row.original_id) != row.destination_id
        or row.audience.scope_key != row.destination_id
    ):
        raise ValueError("archive graph bodyless skip is not a checked mapped anchor")
    return scope


def prepare_archive_graph_apply(
    *,
    prepared: PreparedArchiveRecords,
    key: ArchivePhaseKey,
    url: str,
    expected_revision: int,
    expected_token: str,
) -> PreparedArchivePhaseTransaction:
    """Compile all immutable graph decisions as graph APPLY batch zero.

    Source/projection/provenance dependencies outside the supported inline and
    edge closure reject the entire compilation. All native witness cuts precede
    canonical writes. No executor or public authorization is provided.
    """
    plan = prepared.plan
    key = ArchivePhaseKey.model_validate(key.model_dump(mode="python"))
    if key.binding != prepared.binding or (key.store, key.action, key.batch_sequence) != (
        "graph",
        "apply",
        0,
    ):
        raise ValueError("archive graph phase key differs from the prepared graph batch")
    selected = [item for item in prepared.rows if item.row.kind in _GRAPH_KINDS]
    if not selected:
        raise ValueError("archive graph selection is empty")
    node_rows: dict[str, list[PlannedArchiveRow]] = {}
    for item in selected:
        row = item.row
        if (
            row.kind is ArchiveKind.GRAPH_ENTITY
            and row.disposition is not ArchiveDisposition.QUARANTINED
        ):
            node_rows.setdefault(_uuid(row.destination_id), []).append(row)
    active = {
        identity
        for identity, rows in node_rows.items()
        if all(
            row.protection == "ordinary"
            and row.disposition in {ArchiveDisposition.CREATED, ArchiveDisposition.SKIPPED}
            for row in rows
        )
    }
    candidates: list[dict[str, object]] = []
    guards: dict[str, dict[str, Any]] = {}
    attributes: list[str] = []
    dates: list[str] = []
    parameters: dict[str, object] = {}
    creates: list[ArchiveCreatedIdentity] = []
    anchors: dict[str, str] = {}
    for item in selected:
        row, body = item.row, item.body
        if (
            row.kind in {ArchiveKind.GRAPH_EPISODE, ArchiveKind.GRAPH_MENTION}
            and row.disposition is not ArchiveDisposition.QUARANTINED
        ):
            raise ValueError("archive graph history may only be quarantined")
        witnesses = _witnesses(row)
        record = None
        anchor_type = None
        if row.disposition in {ArchiveDisposition.CREATED, ArchiveDisposition.SKIPPED}:
            if row.protection != "ordinary" or any(
                identity not in active for identity in row.endpoint_ids
            ):
                raise ValueError("archive graph dependencies are outside the active node closure")
            if row.kind is ArchiveKind.GRAPH_RELATIONSHIP and len(row.endpoint_ids) < 2:
                raise ValueError("archive graph relationship requires its ordered endpoints")
            if body is None:
                if row.kind is not ArchiveKind.GRAPH_ENTITY:
                    raise ValueError("archive graph relationship skip requires its canonical body")
                anchor_type = _anchor_type(row, plan.mappings.projects, plan.mappings.teams)
                anchors[cast(str, row.destination_id)] = anchor_type
            else:
                record = (_node_record if row.kind is ArchiveKind.GRAPH_ENTITY else _edge_record)(
                    row, body, org=plan.organization_id, actor=plan.actor_id
                )
                inline = tuple(
                    dict.fromkeys(
                        body["metadata"][field]
                        for field in _INLINE_REFERENCES
                        if field in body["metadata"]
                    )
                )
                expected = set(
                    row.endpoint_ids[2:]
                    if row.kind is ArchiveKind.GRAPH_RELATIONSHIP
                    else row.endpoint_ids
                )
                if set(inline) - set(row.endpoint_ids) or expected - set(inline):
                    raise ValueError("archive graph inline reference binding differs")
        for identity, witness in witnesses.items():
            node = identity.startswith("entity:")
            destination = identity.split(":", 1)[1]
            if node and destination not in node_rows:
                raise ValueError("archive graph checked dependency is outside the closed selection")
            hashes = [witness.row_sha256, witness.state_sha256, witness.associations_sha256]
            if node:
                endpoint = node_rows[destination][0]
                if endpoint.disposition is ArchiveDisposition.CREATED:
                    if any(hashes):
                        raise ValueError("archive graph new endpoint requires checked absence")
                elif witness.state_sha256 is None:
                    raise ValueError("archive graph retained endpoint requires its source ledger")
            guard = {
                "kind": "graph_entity" if node else "graph_relationship",
                "destination_id": destination,
                "witness": hashes,
                "ordinary": destination in active
                if node
                else row.disposition is ArchiveDisposition.SKIPPED,
                "anchor_type": anchors.get(destination) if node else None,
                "audience": node_rows[destination][0].audience.model_dump(mode="json")
                if node
                else row.audience.model_dump(mode="json"),
                "actor": plan.actor_id,
            }
            existing = guards.get(identity)
            if existing is not None and existing["witness"] != hashes:
                raise ValueError("archive graph shared checked witnesses disagree")
            guards[identity] = guard
        attributes_index = dates_index = None
        if row.disposition is ArchiveDisposition.CREATED:
            assert record is not None
            attributes_index = len(attributes)
            attributes.append(_metadata_expression(record.pop("attributes"), parameters))
            if row.kind is ArchiveKind.GRAPH_RELATIONSHIP:
                dates_index = len(dates)
                expressions = []
                for column in ("expired_at", "valid_at", "invalid_at"):
                    value = record.pop(column)
                    if value is None:
                        expressions.append("NONE")
                    else:
                        assert isinstance(value, datetime)
                        name = "archive_graph_date_" + str(len(parameters))
                        parameters[name] = getattr(value, "native_text", value.isoformat())
                        expressions.append("<datetime>$" + name)
                dates.append("[" + ", ".join(expressions) + "]")
            creates.append(
                ArchiveCreatedIdentity(
                    kind="graph_entity"
                    if row.kind is ArchiveKind.GRAPH_ENTITY
                    else "graph_relationship",
                    destination_id=cast(str, row.destination_id),
                )
            )
        candidates.append(
            {
                "kind": row.kind.value,
                "disposition": row.disposition.value,
                "destination_id": None
                if row.disposition is ArchiveDisposition.QUARANTINED
                else row.destination_id,
                "endpoint_ids": list(row.endpoint_ids),
                "record": record if row.disposition is ArchiveDisposition.CREATED else None,
                "attributes_index": attributes_index,
                "dates_index": dates_index,
            }
        )
    for identity, rows in node_rows.items():
        if len(rows) > 1 and (
            identity not in anchors
            or any(row.disposition is not ArchiveDisposition.SKIPPED for row in rows)
            or any(
                row.audience != rows[0].audience or row.witnesses != rows[0].witnesses
                for row in rows
            )
            or any(item.body is not None for item in selected if item.row in rows)
        ):
            raise ValueError("archive graph destinations repeat without consistent mapped anchors")
    edges = [
        cast(str, row["destination_id"])
        for row in candidates
        if row["kind"] == "graph_relationship" and row["disposition"] != "quarantined"
    ]
    if len(edges) != len(set(edges)):
        raise ValueError("archive graph relationship destinations repeat")
    # Later node anchors can be referenced by earlier rows in immutable order.
    for identity, anchor_type in anchors.items():
        guards["entity:" + identity]["anchor_type"] = anchor_type
    return prepare_archive_phase_transaction(
        key=key,
        url=url,
        expected_revision=expected_revision,
        expected_token=expected_token,
        writer_statements="LET $archive_graph_attributes = ["
        + ", ".join(attributes)
        + "];\n"
        + "LET $archive_graph_dates = ["
        + ", ".join(dates)
        + "];\n"
        + (
            _CUT
            if is_embedded_surreal_url(url)
            else _CUT.replace("type::is::bool", "type::is_bool")
        )
        + _ORDINARY
        + _FENCES
        + _CREATES,
        writer_parameters={
            **parameters,
            "archive_graph_candidates": candidates,
            "archive_graph_guards": list(guards.values()),
            "archive_graph_active_node_ids": sorted(active),
            "archive_graph_witness_scheme": plan.effective_witness_scheme,
            "archive_graph_projection_types": list(_PROJECTION_TYPES),
            "archive_graph_provenance_fields": sorted(_PROVENANCE),
        },
        planned_creates=tuple(creates),
    )
