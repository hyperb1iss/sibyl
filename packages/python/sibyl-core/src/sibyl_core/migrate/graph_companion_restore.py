"""Restore validated graph companions after their sources, in bounded batches."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from surrealdb import RecordID

from sibyl_core.backends.surreal.in_clumps import clumped
from sibyl_core.embeddings.provenance import mark_unverified_vector
from sibyl_core.migrate.legacy_graph_archive import (
    _EPISODE_FIELDS,
    _MENTION_FIELDS,
    BackupEpisodeNode,
    BackupMentionEdge,
    episode_record,
)
from sibyl_core.migrate.source_integrity import native_archive_parameters
from sibyl_core.models.entities import Relationship
from sibyl_core.services.graph_common import normalize_graph_records
from sibyl_core.services.graph_relationships import (
    _RELATIONSHIP_BULK_MUTATIONS,
    _relationship_bulk_payloads,
    _relationship_record,
)
from sibyl_core.services.source_archive_store import UniqueWrite, restore_batches

_COMPANION_CHANGED = "archive companion destination changed before restore"

# A batch finds every destination row it reads by uuid through client
# parameters, and its loops write to the record ids those reads return.
# SurrealDB 4.0 plans a statement before it runs and serves a filter from an
# index only when its operand is a literal or a client parameter: a filter on
# a closure argument, a FOR variable or a block LET scans the whole table, so
# each restored row would cost more with every row already in place.


def _held(
    table: str, fields: str, parameter: str, uuids: Iterable[str]
) -> tuple[str, dict[str, Any]]:
    """The rows of ``table`` holding these uuids, in uuid order, and the read's params."""
    lookup, parameters = clumped(
        f"SELECT {fields} FROM {table} WHERE uuid IN ${parameter}", parameter, list(uuids)
    )
    return f"(SELECT * FROM {lookup} ORDER BY uuid)", parameters


def _by_uuid(rows: str) -> str:
    return f"object::from_entries({rows}.map(|$row| [$row.uuid, $row]))"


def _relationship_statements(rows: list[dict[str, Any]]) -> tuple[str, dict[str, Any]]:
    endpoints, endpoint_parameters = _held(
        "entity",
        "id, uuid, group_id",
        "relationship_endpoints",
        (uuid for row in rows for uuid in (row["source_id"], row["target_id"])),
    )
    held, held_parameters = _held(
        "relates_to",
        "id, uuid, in, out, group_id",
        "relationship_uuids",
        (row["uuid"] for row in rows),
    )
    return (
        f"""
    LET $endpoints = {_by_uuid(endpoints)};
    LET $rows = SELECT *,
        (IF $endpoints[source_id].group_id = group_id
            THEN $endpoints[source_id].id ELSE NONE END) AS in,
        (IF $endpoints[target_id].group_id = group_id
            THEN $endpoints[target_id].id ELSE NONE END) AS out
        FROM $archive_relationships;
    IF array::len($rows[WHERE in=NONE OR out=NONE]) > 0 {{
        THROW 'archive relationship endpoint is missing from restore organization';
    }};
    LET $edges = SELECT uuid, in AS src, out AS tgt, group_id,
        $archive_relationship_indexes[uuid] AS index FROM $rows;
    LET $updates = $archive_relationship_updates;
    LET $held = {_by_uuid(held)};
    LET $existing = SELECT VALUE {{
        edge: {{ uuid: uuid, src: src, tgt: tgt, group_id: group_id, index: index }},
        stored: $held[uuid]
    }} FROM $edges;
    IF array::len($existing.filter(|$entry|
        $entry.stored != NONE AND $entry.stored.group_id != $entry.edge.group_id
    )) > 0 {{ THROW 'archive relationship identity belongs to another organization'; }};
    {_RELATIONSHIP_BULK_MUTATIONS}
""",
        {**endpoint_parameters, **held_parameters},
    )


def _mention_statements(mentions: list[dict[str, Any]]) -> tuple[str, dict[str, Any]]:
    sources, source_parameters = _held(
        "episode",
        "id, uuid, group_id",
        "mention_sources",
        (mention["source_node_uuid"] for mention in mentions),
    )
    targets, target_parameters = _held(
        "entity",
        "id, uuid, group_id",
        "mention_targets",
        (mention["target_node_uuid"] for mention in mentions),
    )
    held, held_parameters = _held(
        "mentions",
        "id, uuid, in, out, group_id",
        "mention_uuids",
        (mention["uuid"] for mention in mentions),
    )
    return (
        f"""
    LET $sources = {_by_uuid(sources)};
    LET $targets = {_by_uuid(targets)};
    LET $held = {_by_uuid(held)};
    FOR $mention IN $archive_mentions {{
        LET $uuid = $mention.uuid;
        LET $group_id = $mention.group_id;
        LET $created_at = $mention.created_at;
        LET $rel = $mention.rel;
        LET $source = $sources[$mention.source_node_uuid];
        LET $target = $targets[$mention.target_node_uuid];
        LET $src = IF $source.group_id = $group_id THEN $source.id ELSE NONE END;
        LET $tgt = IF $target.group_id = $group_id THEN $target.id ELSE NONE END;
        IF $src=NONE OR $tgt=NONE {{
            THROW 'archive mention endpoint is missing from restore organization';
        }};
        LET $stored = $held[$uuid];
        IF $stored != NONE AND $stored.group_id != $group_id {{
            THROW 'archive mention identity belongs to another organization';
        }};
        IF $stored != NONE AND $stored.in = $src AND $stored.out = $tgt {{
            UPDATE $stored.id SET
                in = $src,
                out = $tgt,{_MENTION_FIELDS};
        }} ELSE {{
            IF $stored != NONE {{ DELETE $stored.id; }};
            RELATE $src->$rel->$tgt SET{_MENTION_FIELDS};
        }};
    }};
""",
        {**source_parameters, **target_parameters, **held_parameters},
    )


def _episode_statements(records: list[dict[str, Any]]) -> tuple[str, dict[str, Any]]:
    bindings = "\n".join(f"LET ${name} = $episode.{name};" for name in records[0])
    held, held_parameters = _held(
        "episode", "id, uuid, group_id", "episode_uuids", (record["uuid"] for record in records)
    )
    return (
        f"""
    LET $held = {_by_uuid(held)};
    FOR $episode IN $archive_episodes {{
        {bindings}
        LET $stored = $held[$uuid];
        IF $stored != NONE AND $stored.group_id != $group_id {{
            THROW 'archive episode identity belongs to another organization';
        }};
        IF $stored = NONE {{ CREATE episode SET{_EPISODE_FIELDS}; }}
        ELSE {{ UPDATE $stored.id SET{_EPISODE_FIELDS}; }};
    }};
""",
        held_parameters,
    )


def _destination_projection(table: str, uuids: Iterable[str]) -> tuple[str, dict[str, Any]]:
    """The destination companions a batch decides on, found by identity."""
    return _held(table, "*", "companion_uuids", uuids)


@dataclass(frozen=True)
class CompanionRestorePlan:
    """Archive companions validated and serialized before any restore write."""

    organization_id: str
    episodes: list[dict[str, Any]]
    relationships: list[dict[str, Any]]
    mentions: list[dict[str, Any]]


@dataclass(frozen=True)
class CompanionRestoreResult:
    episodes_restored: int
    episodes_skipped: int
    relationships_restored: int
    relationships_skipped: int
    mentions_restored: int
    mentions_skipped: int


def prepare_companion_restore(
    *,
    organization_id: str,
    episodes: list[BackupEpisodeNode],
    relationships: list[Relationship],
    mentions: list[BackupMentionEdge],
) -> CompanionRestorePlan:
    """Validate and serialize every companion before any source or companion changes."""
    for item in [*episodes, *mentions]:
        if item.group_id != organization_id:
            raise ValueError("graph companion is outside restore organization")
    relationship_records = [
        _relationship_record(row, group_id=organization_id, archive_binding=True)
        for row in relationships
    ]
    # A vector that does not name its model arrives unverified; stamped ones
    # keep their stamp. Provenance is not relationship evidence, so an
    # operational binding's body digest is unchanged either way.
    for record in relationship_records:
        attributes = record["attributes"]
        if isinstance(attributes, dict):
            mark_unverified_vector(attributes, has_vector=bool(record.get("fact_embedding")))
    # Reject an archive that names one relationship with two sets of endpoints
    # now, while nothing is written, rather than in whichever batch meets both.
    _relationship_bulk_payloads(relationship_records)
    return CompanionRestorePlan(
        organization_id=organization_id,
        episodes=[episode_record(row) for row in episodes],
        relationships=relationship_records,
        mentions=[
            {
                "uuid": row.uuid,
                "group_id": row.group_id,
                "source_node_uuid": row.source_node_uuid,
                "target_node_uuid": row.target_node_uuid,
                "created_at": row.created_at,
                "rel": RecordID("mentions", row.uuid),
            }
            for row in mentions
        ],
    )


async def _destination_rows(
    client: Any, table: str, batch: list[dict[str, Any]]
) -> tuple[list[dict[str, Any] | None], str, str, dict[str, Any]]:
    """Read the destination companions a batch decides on, with their digest and fence."""
    projection, parameters = _destination_projection(table, (row["uuid"] for row in batch))
    rows = normalize_graph_records(
        await client.execute_query(
            f"RETURN {{ LET $fenced = {projection}; "
            "RETURN { rows: $fenced, digest: crypto::sha256(type::string($fenced)) }; };",
            **parameters,
        )
    )
    found = rows[0].get("rows") if len(rows) == 1 else None
    digest = rows[0].get("digest") if len(rows) == 1 else None
    if not isinstance(found, list) or not isinstance(digest, str):
        raise ValueError("graph companion restore read returned an invalid shape")
    held = {row.get("uuid"): row for row in found if isinstance(row, dict)}
    return [held.get(row["uuid"]) for row in batch], digest, projection, parameters


async def _destination_identities(
    client: Any, table: str, uuids: set[str], organization_id: str
) -> set[str]:
    """Which of these identities the destination already holds in the organization."""
    present: set[str] = set()
    for batch in restore_batches(sorted(uuids)):
        lookup, parameters = clumped(
            f"SELECT uuid, group_id FROM {table} WHERE uuid IN $companion_probes",
            "companion_probes",
            batch,
        )
        found = await client.execute_query(f"RETURN {lookup};", **parameters)
        if not isinstance(found, list):
            raise ValueError("graph companion endpoint read returned an invalid shape")
        present.update(
            row["uuid"]
            for row in found
            if isinstance(row, dict) and row.get("group_id") == organization_id
        )
    return present


@dataclass(frozen=True)
class _CompanionWrite:
    statements: str
    parameters: dict[str, Any]
    # The read a merge batch is fenced on. A clean batch follows a fenced
    # wipe of every companion and writes into an emptied organization.
    fence: str | None


@dataclass
class CompanionRestoreWrites:
    """Every companion write a restore makes, decided before the first one is sent."""

    counts: CompanionRestoreResult
    writes: list[_CompanionWrite] = field(default_factory=list)
    unique_writes: list[UniqueWrite] = field(default_factory=list)


def _protected(existing: dict[str, Any]) -> bool:
    return (
        existing.get("operational_derivation_required") is True
        or existing.get("operational_source_binding") is not None
    )


async def _plan_kind(
    client: Any,
    table: str,
    records: list[dict[str, Any]],
    *,
    label: str,
    organization_id: str,
    clean: bool,
    keep: Callable[[dict[str, Any]], bool],
    statements: Callable[[list[dict[str, Any]]], tuple[str, dict[str, Any]]],
    writes: list[_CompanionWrite],
) -> list[dict[str, Any]]:
    """Select one companion kind's writes, a bounded batch at a time."""
    chosen: list[dict[str, Any]] = []
    for batch in restore_batches(records):
        found, digest, fence, fence_parameters = await _destination_rows(client, table, batch)
        selected = []
        for record, existing in zip(batch, found, strict=True):
            if existing is not None and existing.get("group_id") != organization_id:
                raise ValueError(f"archive {label} identity belongs to another organization")
            # A clean restore reconstructs the archived state; a merge keeps
            # the destination companions ``keep`` names.
            if clean or existing is None or not keep(existing):
                selected.append(record)
        chosen += selected
        if selected:
            statement, parameters = statements(selected)
            if clean:
                writes.append(_CompanionWrite(statement, parameters, None))
            else:
                fenced = {**parameters, **fence_parameters, "companion_digest": digest}
                writes.append(_CompanionWrite(statement, fenced, fence))
    return chosen


def _relationship_parameters(selected: list[dict[str, Any]]) -> tuple[str, dict[str, Any]]:
    payloads = _relationship_bulk_payloads(selected)
    statements, parameters = _relationship_statements(selected)
    return statements, {
        **parameters,
        "archive_relationships": payloads["rows"],
        "archive_relationship_updates": payloads["updates"],
        "archive_relationship_indexes": {
            row["uuid"]: index for index, row in enumerate(payloads["rows"])
        },
    }


def _episode_parameters(selected: list[dict[str, Any]]) -> tuple[str, dict[str, Any]]:
    # The batch reads the destination once, so an episode the archive carries
    # twice is written as its last copy, where the loop would end up.
    records = list({record["uuid"]: record for record in selected}.values())
    statements, parameters = _episode_statements(records)
    return statements, {**parameters, "archive_episodes": native_archive_parameters(records)}


def _mention_parameters(selected: list[dict[str, Any]]) -> tuple[str, dict[str, Any]]:
    # Same single read as episodes. Mentions an old archive carries without a
    # uuid all share one record id, so they collapse to the last copy too.
    records = list({str(record.get("uuid") or ""): record for record in selected}.values())
    statements, parameters = _mention_statements(records)
    return statements, {**parameters, "archive_mentions": native_archive_parameters(records)}


async def plan_companions(
    client: Any,
    plan: CompanionRestorePlan,
    *,
    skip_existing: bool,
    clean: bool,
    entities: set[str],
    retired: set[str],
) -> CompanionRestoreWrites:
    """Decide every companion write and check every endpoint before any write.

    ``entities`` are the archive entities that hold a row once the sources
    land and ``retired`` the archive entities that do not, whatever the
    destination holds for them now. Any other endpoint must already be in the
    destination, and a clean restore deletes every entity the archive does
    not carry.
    """
    organization_id = plan.organization_id
    writes: list[_CompanionWrite] = []
    common = {"organization_id": organization_id, "clean": clean, "writes": writes}
    episodes = await _plan_kind(
        client,
        "episode",
        plan.episodes,
        label="episode",
        keep=lambda _existing: skip_existing,
        statements=_episode_parameters,
        **common,
    )
    # Merge imports retain all protected destination edges.
    relationships = await _plan_kind(
        client,
        "relates_to",
        plan.relationships,
        label="relationship",
        keep=lambda existing: skip_existing or _protected(existing),
        statements=_relationship_parameters,
        **common,
    )
    mentions = await _plan_kind(
        client,
        "mentions",
        plan.mentions,
        label="mention",
        keep=lambda _existing: skip_existing,
        statements=_mention_parameters,
        **common,
    )

    endpoints = {
        uuid for edge in relationships for uuid in (edge["source_id"], edge["target_id"])
    } | {mention["target_node_uuid"] for mention in mentions}
    sources = {mention["source_node_uuid"] for mention in mentions}
    present = set(entities)
    present_episodes = {row["uuid"] for row in plan.episodes}
    if not clean:
        present |= await _destination_identities(
            client, "entity", endpoints - present - retired, organization_id
        )
        present_episodes |= await _destination_identities(
            client, "episode", sources - present_episodes, organization_id
        )
    if any(
        edge["source_id"] not in present or edge["target_id"] not in present
        for edge in relationships
    ):
        raise ValueError("archive relationship endpoint is missing from restore organization")
    if any(
        mention["source_node_uuid"] not in present_episodes
        or mention["target_node_uuid"] not in present
        for mention in mentions
    ):
        raise ValueError("archive mention endpoint is missing from restore organization")

    return CompanionRestoreWrites(
        counts=CompanionRestoreResult(
            episodes_restored=len(episodes),
            episodes_skipped=len(plan.episodes) - len(episodes),
            relationships_restored=len(relationships),
            relationships_skipped=len(plan.relationships) - len(relationships),
            mentions_restored=len(mentions),
            mentions_skipped=len(plan.mentions) - len(mentions),
        ),
        writes=writes,
        # Every companion write replaces a destination companion by uuid.
        unique_writes=[
            UniqueWrite(table, record, identity=("uuid",))
            for table, records in (
                ("episode", episodes),
                ("relates_to", relationships),
                ("mentions", mentions),
            )
            for record in records
        ],
    )


async def apply_companions(client: Any, planned: CompanionRestoreWrites) -> None:
    """Write episodes, then relationships, then mentions, a bounded batch at a time.

    Each kind follows what it points at: relationships resolve their endpoints
    among restored entities and mentions among restored episodes. A merge
    batch refuses to commit when a destination companion it decided on has
    moved, down to a nanosecond.
    """
    for write in planned.writes:
        guard = ""
        if write.fence is not None:
            guard = (
                f"LET $fenced = {write.fence};\n"
                "IF crypto::sha256(type::string($fenced)) != $companion_digest "
                f"{{ THROW '{_COMPANION_CHANGED}'; }};\n"
            )
        await client.execute_query(
            f"BEGIN TRANSACTION;\n{guard}{write.statements}\nCOMMIT TRANSACTION;",
            **write.parameters,
        )
