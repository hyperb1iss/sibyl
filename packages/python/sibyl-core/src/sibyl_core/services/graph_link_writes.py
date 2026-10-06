"""Add graph links without replacing an entity's body or existing relationships."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from sibyl_core.backends.surreal.records import raise_on_error
from sibyl_core.backends.surreal.schema_source_witness import SOURCE_STATE_WRITE_WITNESS
from sibyl_core.models.entities import Entity, Relationship
from sibyl_core.services.graph_client import SurrealGraphClient
from sibyl_core.services.graph_common import SurrealRecord, normalize_graph_records
from sibyl_core.services.graph_records import entity_from_surreal_row
from sibyl_core.services.graph_relationships import (
    _PROTECTED_RELATIONSHIP_WRITE_GUARD,
    _RELATIONSHIP_BULK_MUTATIONS,
    _relationship_bulk_parameters,
    _relationship_record,
)

_LINK_CUT = """
LET $rows = SELECT * FROM entity WHERE uuid = $approved.uuid;
LET $states = SELECT * OMIT validation_write_witness FROM source_states
    WHERE organization_id = $approved.group_id AND source_kind = 'graph_entity'
        AND source_id = $approved.uuid ORDER BY id;
LET $associations = SELECT * OMIT validation_write_witness FROM memory_derivations
    WHERE target_kind = 'graph_entity' AND target_id = $approved.uuid ORDER BY id;
LET $valid = array::len($rows) = 1 AND $rows[0].group_id = $approved.group_id
    AND array::len($states) = 1 AND $states[0].deleted = false
    AND array::len($associations.filter(|$a|
        $a.organization_id != $approved.group_id)) = 0;
"""

_LOAD_LINK_SNAPSHOT = (
    """
RETURN {
    LET $approved = {uuid: $uuid, group_id: $group_id};
"""
    + _LINK_CUT
    + """
    IF $valid = false { RETURN NONE; };
    RETURN {
        row: $rows[0], physical_id: $rows[0].id, state_id: $states[0].id,
        sha256: crypto::sha256(type::string([$rows, $states, $associations]))
    };
};
"""
)

_ADD_LINKS = (
    """
BEGIN TRANSACTION;
LET $result = {
    LET $checks = $approved_entities.map(|$approved| {
"""
    + _LINK_CUT
    + """
        RETURN {
            valid: $valid AND $rows[0].id = $approved.physical_id
                AND $states[0].id = $approved.state_id
                AND crypto::sha256(type::string([$rows, $states, $associations]))
                    = $approved.sha256
        };
    });
    IF array::len($checks.filter(|$check| $check.valid = false)) > 0 {
        RETURN {error: 'changed'};
    };
    LET $source = (SELECT * FROM $approved_entities[0].physical_id)[0];
    LET $edge_rows = SELECT * FROM relates_to WHERE uuid IN $relationship_ids;
    IF array::len($edge_rows) != array::len(array::distinct($edge_rows.uuid)) {
        RETURN {error: 'identity'};
    };
    LET $existing = $edges.map(|$edge| {
        edge: $edge,
        stored: (SELECT * FROM relates_to WHERE uuid = $edge.uuid)[0]
    });
    IF array::len($existing.filter(|$entry| $entry.stored != NONE AND (
        $entry.stored.group_id != $entry.edge.group_id
        OR $entry.stored.in != $entry.edge.src OR $entry.stored.out != $entry.edge.tgt
        OR $entry.stored.name != $entry.edge.name
        OR $entry.stored.source_id != $entry.edge.source_id
        OR $entry.stored.target_id != $entry.edge.target_id
        OR $entry.stored.invalid_at != NONE OR $entry.stored.expired_at != NONE
        OR $entry.stored.operational_derivation_required = true
        OR $entry.stored.operational_source_binding != NONE
    ))) > 0 { RETURN {error: 'identity'}; };
    IF $topology_conflict { RETURN {error: 'topology'}; };
    LET $present_ids = SELECT VALUE stored.uuid FROM $existing WHERE stored != NONE;
    LET $new_ids = SELECT VALUE edge.uuid FROM $existing WHERE stored = NONE;
    LET $replayed = array::len($new_ids) = 0 AND $topology_complete;
    IF $replayed = false AND $source.revision != $expected_revision {
        RETURN {error: 'revision'};
    };
    IF $replayed = false {
"""
    + _PROTECTED_RELATIONSHIP_WRITE_GUARD
    + """
        LET $source_states_to_fence = $approved_entities.map(|$approved| {
            id: $approved.state_id
        });
"""
    + SOURCE_STATE_WRITE_WITNESS
    + """
        UPDATE $source.id SET
            epic_id = $epic_id,
            parent_task_id = $parent_task_id,
            attributes.epic_id = $epic_id,
            attributes.parent_task_id = $parent_task_id,
            attributes.depends_on = $depends_on,
            modified_by = $modified_by ?? modified_by,
            revision += 1, updated_at = time::now(), attributes.updated_at = time::now()
            RETURN NONE;
        LET $existing = $existing.filter(|$entry| $entry.stored = NONE);
"""
    + _RELATIONSHIP_BULK_MUTATIONS
    + """
    };
    LET $stored = (SELECT revision FROM $source.id)[0];
    RETURN {
        entity_id: $source.uuid, revision: $stored.revision,
        added_relationship_ids: $new_ids, existing_relationship_ids: $present_ids,
        epic_id: $epic_id, parent_task_id: $parent_task_id,
        depends_on: $depends_on, replayed: $replayed
    };
};
RETURN $result;
COMMIT TRANSACTION;
"""
)


class EntityLinkConflictError(ValueError):
    """The saved link intent no longer agrees with the native graph."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"Entity links conflict: {reason}")


@dataclass(frozen=True, slots=True)
class EntityLinkSnapshot:
    """An exact native row and authority cut authorized by the request owner."""

    entity: Entity
    physical_id: object
    state_id: object
    sha256: str

    def parameters(self, group_id: str) -> dict[str, object]:
        return {
            "uuid": self.entity.id,
            "group_id": group_id,
            "physical_id": self.physical_id,
            "state_id": self.state_id,
            "sha256": self.sha256,
        }


@dataclass(frozen=True, slots=True)
class EntityLinksResult:
    entity_id: str
    revision: int
    added_relationship_ids: list[str]
    existing_relationship_ids: list[str]
    epic_id: str | None
    parent_task_id: str | None
    depends_on: list[str]
    replayed: bool


async def _execute_link_query(
    client: SurrealGraphClient, query: str, **params: object
) -> list[SurrealRecord]:
    result = await client.execute_query_raw(query, **params)
    if isinstance(result, dict) and result.get("error") is not None:
        raise RuntimeError("Native entity link RPC failed", result["error"])
    raise_on_error(result, query=query)
    return normalize_graph_records(result)


async def load_entity_link_snapshot(
    client: SurrealGraphClient, entity_id: str, *, group_id: str
) -> EntityLinkSnapshot | None:
    """Load native identities before applying the caller's normal API policy."""
    rows = await _execute_link_query(client, _LOAD_LINK_SNAPSHOT, uuid=entity_id, group_id=group_id)
    if not rows:
        return None
    if len(rows) != 1 or not isinstance(rows[0].get("row"), dict):
        raise RuntimeError("Entity link snapshot returned an invalid native shape")
    row = rows[0]
    native_row = row.get("row")
    if not isinstance(native_row, dict):
        raise RuntimeError("Entity link snapshot omitted its native row")
    physical_id, state_id, sha256 = row.get("physical_id"), row.get("state_id"), row.get("sha256")
    if physical_id is None or state_id is None or not isinstance(sha256, str):
        raise RuntimeError("Entity link snapshot omitted its native identity")
    return EntityLinkSnapshot(entity_from_surreal_row(native_row), physical_id, state_id, sha256)


def _binding(metadata: dict[str, object], key: str) -> str | None:
    value = metadata.get(key)
    return str(value) if value not in (None, "") else None


async def add_entity_links_if_revision(
    client: SurrealGraphClient,
    *,
    group_id: str,
    source: EntityLinkSnapshot,
    targets: Sequence[EntityLinkSnapshot],
    expected_revision: int,
    relationships: Sequence[Relationship] = (),
    epic_id: str | None = None,
    parent_task_id: str | None = None,
    depends_on: Sequence[str] = (),
    modified_by: str | None = None,
) -> EntityLinksResult:
    """Atomically add authorized links, or confirm an exact read-only replay."""
    if (
        isinstance(expected_revision, bool)
        or not isinstance(expected_revision, int)
        or expected_revision < 1
    ):
        raise ValueError("expected_revision must be a positive integer")
    snapshots = {source.entity.id: source}
    for target in targets:
        previous = snapshots.get(target.entity.id)
        if previous is not None and previous != target:
            raise ValueError("Conflicting endpoint snapshots")
        snapshots[target.entity.id] = target
    wanted = {str(value) for value in depends_on}
    wanted.update(value for value in (epic_id, parent_task_id) if value)
    unique: dict[str, Relationship] = {}
    for relationship in relationships:
        if relationship.source_id != source.entity.id:
            raise ValueError("A link must originate at the authorized source")
        previous = unique.get(relationship.id)
        if previous is not None and previous != relationship:
            raise ValueError("Conflicting relationship declarations")
        unique[relationship.id] = relationship
        wanted.add(relationship.target_id)
    if not wanted <= snapshots.keys():
        raise ValueError("Every link endpoint requires an authorized native snapshot")
    metadata = source.entity.metadata or {}
    current_epic = _binding(metadata, "epic_id")
    current_parent = _binding(metadata, "parent_task_id")
    previous_dependencies = metadata.get("depends_on") or []
    if not isinstance(previous_dependencies, list) or any(
        not isinstance(value, str) for value in previous_dependencies
    ):
        raise EntityLinkConflictError("topology")
    merged_dependencies = list(dict.fromkeys([*previous_dependencies, *depends_on]))
    topology_conflict = bool(
        (epic_id and current_epic and epic_id != current_epic)
        or (parent_task_id and current_parent and parent_task_id != current_parent)
    )
    topology_complete = (
        (not epic_id or current_epic == epic_id)
        and (not parent_task_id or current_parent == parent_task_id)
        and set(depends_on) <= set(previous_dependencies)
    )
    records = []
    for relationship in unique.values():
        record = _relationship_record(relationship, group_id=group_id)
        record["in"] = source.physical_id
        record["out"] = snapshots[relationship.target_id].physical_id
        records.append(record)
    parameters = _relationship_bulk_parameters(records)
    for edge, relationship in zip(parameters["edges"], unique.values(), strict=True):
        edge.update(
            {
                "name": relationship.relationship_type.value,
                "source_id": relationship.source_id,
                "target_id": relationship.target_id,
            }
        )
    rows = await _execute_link_query(
        client,
        _ADD_LINKS,
        **parameters,
        relationship_ids=list(unique),
        approved_entities=[snapshot.parameters(group_id) for snapshot in snapshots.values()],
        expected_revision=expected_revision,
        topology_conflict=topology_conflict,
        topology_complete=topology_complete,
        epic_id=epic_id or current_epic,
        parent_task_id=parent_task_id or current_parent,
        depends_on=merged_dependencies,
        modified_by=modified_by,
    )
    if len(rows) != 1:
        raise RuntimeError("Entity link transaction returned an invalid native cardinality")
    result = rows[0]
    error = result.get("error")
    if isinstance(error, str):
        raise EntityLinkConflictError(error)
    revision = result.get("revision")
    replayed = result.get("replayed")
    if (
        result.get("entity_id") != source.entity.id
        or isinstance(revision, bool)
        or not isinstance(revision, int)
        or revision < 1
        or not isinstance(replayed, bool)
    ):
        raise RuntimeError("Entity link transaction returned an invalid native receipt")
    added, existing = result.get("added_relationship_ids"), result.get("existing_relationship_ids")
    if (
        not isinstance(added, list)
        or not isinstance(existing, list)
        or any(not isinstance(value, str) for value in [*added, *existing])
        or set(added) & set(existing)
        or set(added) | set(existing) != set(unique)
    ):
        raise RuntimeError("Entity link transaction omitted a requested relationship")
    return EntityLinksResult(
        source.entity.id,
        revision,
        added,
        existing,
        epic_id or current_epic,
        parent_task_id or current_parent,
        merged_dependencies,
        replayed,
    )
