"""Restore validated graph companions after their sources, in bounded batches."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from surrealdb import RecordID

from sibyl_core.embeddings.provenance import mark_unverified_vector
from sibyl_core.migrate.legacy_graph_archive import (
    _EPISODE_UPSERT_STATEMENTS,
    _MENTION_UPSERT_STATEMENTS,
    BackupEpisodeNode,
    BackupMentionEdge,
    episode_record,
)
from sibyl_core.migrate.source_integrity import native_archive_parameters
from sibyl_core.models.entities import Relationship
from sibyl_core.services.graph_common import normalize_graph_records
from sibyl_core.services.graph_relationships import (
    _RELATIONSHIP_BULK_UPSERT_STATEMENTS,
    _relationship_bulk_payloads,
    _relationship_record,
)
from sibyl_core.services.source_archive_store import UniqueWrite, restore_batches

_COMPANION_CHANGED = "archive companion destination changed before restore"

# Loops bind a row's fields to plain variables before filtering on them.
# SurrealDB 3.2 plans `uuid = $row.uuid` as a scan of the whole table and
# `uuid = $uuid` as an index lookup, so the first form makes every batch cost
# grow with the organization.

_RELATIONSHIP_STATEMENTS = f"""
    LET $rows = $archive_relationships.map(|$edge|
        object::from_entries(array::concat(object::entries($edge), [
            ['in', (SELECT VALUE id FROM entity
                WHERE uuid=$edge.source_id AND group_id=$edge.group_id LIMIT 1)[0]],
            ['out', (SELECT VALUE id FROM entity
                WHERE uuid=$edge.target_id AND group_id=$edge.group_id LIMIT 1)[0]]
        ]))
    );
    IF array::len($rows[WHERE in=NONE OR out=NONE]) > 0 {{
        THROW 'archive relationship endpoint is missing from restore organization';
    }};
    FOR $edge IN $rows {{
        LET $edge_uuid = $edge.uuid;
        LET $edge_group = $edge.group_id;
        IF array::len((SELECT uuid FROM relates_to
            WHERE uuid=$edge_uuid AND group_id!=$edge_group)) > 0 {{
            THROW 'archive relationship identity belongs to another organization';
        }};
    }};
    LET $edges = SELECT uuid, in AS src, out AS tgt, group_id,
        $archive_relationship_indexes[uuid] AS index FROM $rows;
    LET $updates = $archive_relationship_updates;
    IF array::len($rows) > 0 {{ {_RELATIONSHIP_BULK_UPSERT_STATEMENTS} }};
"""

_MENTION_STATEMENTS = f"""
    FOR $mention IN $archive_mentions {{
        LET $uuid = $mention.uuid;
        LET $group_id = $mention.group_id;
        LET $created_at = $mention.created_at;
        LET $rel = $mention.rel;
        LET $source_uuid = $mention.source_node_uuid;
        LET $target_uuid = $mention.target_node_uuid;
        LET $src = (SELECT VALUE id FROM episode
            WHERE uuid=$source_uuid AND group_id=$group_id LIMIT 1)[0];
        LET $tgt = (SELECT VALUE id FROM entity
            WHERE uuid=$target_uuid AND group_id=$group_id LIMIT 1)[0];
        IF $src=NONE OR $tgt=NONE {{
            THROW 'archive mention endpoint is missing from restore organization';
        }};
        IF array::len((SELECT uuid FROM mentions
            WHERE uuid=$uuid AND group_id!=$group_id)) > 0 {{
            THROW 'archive mention identity belongs to another organization';
        }};
        {_MENTION_UPSERT_STATEMENTS}
    }};
"""


def _episode_statements(records: list[dict[str, Any]]) -> str:
    bindings = "\n".join(f"LET ${name} = $episode.{name};" for name in records[0])
    return f"""
    FOR $episode IN $archive_episodes {{
        {bindings}
        IF array::len((SELECT uuid FROM episode
            WHERE uuid=$uuid AND group_id!=$group_id)) > 0 {{
            THROW 'archive episode identity belongs to another organization';
        }};
        {_EPISODE_UPSERT_STATEMENTS}
    }};
"""


def _destination_projection(table: str) -> str:
    """The destination companions a batch decides on, found by identity."""
    return (
        "$companion_uuids.map(|$companion_uuid| "
        f"(SELECT * FROM {table} WHERE uuid = $companion_uuid LIMIT 1)[0])"
    )


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
) -> tuple[list[dict[str, Any] | None], str]:
    """Read the destination companions a batch decides on, with their digest."""
    rows = normalize_graph_records(
        await client.execute_query(
            f"RETURN {{ LET $fenced = {_destination_projection(table)}; "
            "RETURN { rows: $fenced, digest: crypto::sha256(type::string($fenced)) }; };",
            companion_uuids=[row["uuid"] for row in batch],
        )
    )
    found = rows[0].get("rows") if len(rows) == 1 else None
    digest = rows[0].get("digest") if len(rows) == 1 else None
    if not isinstance(found, list) or len(found) != len(batch) or not isinstance(digest, str):
        raise ValueError("graph companion restore read returned an invalid shape")
    return [row if isinstance(row, dict) else None for row in found], digest


async def _destination_identities(
    client: Any, table: str, uuids: set[str], organization_id: str
) -> set[str]:
    """Which of these identities the destination already holds in the organization."""
    present: set[str] = set()
    for batch in restore_batches(sorted(uuids)):
        # The embedded engine does not bind query parameters inside a closure.
        found = await client.execute_query(
            "RETURN $companion_probes.map(|$probe| (SELECT VALUE uuid FROM "
            f"{table} WHERE uuid = $probe.uuid AND group_id = $probe.organization LIMIT 1)[0]);",
            companion_probes=[{"uuid": uuid, "organization": organization_id} for uuid in batch],
        )
        if not isinstance(found, list):
            raise ValueError("graph companion endpoint read returned an invalid shape")
        present.update(uuid for uuid in found if isinstance(uuid, str))
    return present


@dataclass(frozen=True)
class _CompanionWrite:
    statements: str
    parameters: dict[str, Any]
    # The table a merge batch is fenced on. A clean batch follows a fenced
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
        found, digest = await _destination_rows(client, table, batch)
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
            writes.append(
                _CompanionWrite(
                    statement,
                    {
                        **parameters,
                        "companion_uuids": [row["uuid"] for row in batch],
                        "companion_digest": digest,
                    },
                    None if clean else table,
                )
            )
    return chosen


def _relationship_parameters(selected: list[dict[str, Any]]) -> tuple[str, dict[str, Any]]:
    payloads = _relationship_bulk_payloads(selected)
    return _RELATIONSHIP_STATEMENTS, {
        "archive_relationships": payloads["rows"],
        "archive_relationship_updates": payloads["updates"],
        "archive_relationship_indexes": {
            row["uuid"]: index for index, row in enumerate(payloads["rows"])
        },
    }


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
        statements=lambda selected: (
            _episode_statements(selected),
            {"archive_episodes": native_archive_parameters(selected)},
        ),
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
        statements=lambda selected: (
            _MENTION_STATEMENTS,
            {"archive_mentions": native_archive_parameters(selected)},
        ),
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
                f"LET $fenced = {_destination_projection(write.fence)};\n"
                "IF crypto::sha256(type::string($fenced)) != $companion_digest "
                f"{{ THROW '{_COMPANION_CHANGED}'; }};\n"
            )
        await client.execute_query(
            f"BEGIN TRANSACTION;\n{guard}{write.statements}\nCOMMIT TRANSACTION;",
            **write.parameters,
        )
