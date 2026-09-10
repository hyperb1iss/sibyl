"""Compose validated graph companions into their source restore transaction."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from surrealdb import RecordID

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
    _relationship_record,
)
from sibyl_core.services.source_archive_store import _graph_auxiliary_snapshot_sql


@dataclass(frozen=True)
class CompanionRestorePlan:
    preconditions: str
    statements: str
    parameters: dict[str, Any]
    episodes_restored: int
    episodes_skipped: int
    relationships_restored: int
    mentions_restored: int
    mentions_skipped: int


async def prepare_companion_restore(
    client: Any,
    *,
    organization_id: str,
    episodes: list[BackupEpisodeNode],
    relationships: list[Relationship],
    mentions: list[BackupMentionEdge],
    skip_existing: bool,
    clean: bool,
) -> CompanionRestorePlan:
    """Capture skip decisions and fence the exact companion state before any writes."""
    for item in [*episodes, *mentions]:
        if item.group_id != organization_id:
            raise ValueError("graph companion is outside restore organization")
    snapshot_sql = _graph_auxiliary_snapshot_sql()
    rows = normalize_graph_records(
        await client.execute_query(
            f"RETURN {{ {snapshot_sql} RETURN {{rows: $graph_auxiliary, "
            "fingerprint: crypto::sha256(type::string($graph_auxiliary))}; };",
            organizations=[organization_id],
        )
    )
    if len(rows) != 1 or not isinstance(rows[0].get("fingerprint"), str):
        raise ValueError("graph companion snapshot returned an invalid shape")
    snapshot = rows[0]["rows"]
    if not isinstance(snapshot, dict):
        raise ValueError("graph companion snapshot returned invalid rows")
    existing_episodes = {row["uuid"] for row in snapshot["episode"]}
    existing_mentions = {row["uuid"] for row in snapshot["mentions"]}
    selected_episodes = [
        row for row in episodes if clean or not skip_existing or row.uuid not in existing_episodes
    ]
    selected_mentions = [
        row for row in mentions if clean or not skip_existing or row.uuid not in existing_mentions
    ]
    episode_records = [episode_record(row) for row in selected_episodes]
    relationship_records = [
        _relationship_record(row, group_id=organization_id) for row in relationships
    ]
    mention_records = [
        {
            "uuid": row.uuid,
            "group_id": row.group_id,
            "source_node_uuid": row.source_node_uuid,
            "target_node_uuid": row.target_node_uuid,
            "created_at": row.created_at,
            "rel": RecordID("mentions", row.uuid),
        }
        for row in selected_mentions
    ]
    episode_bindings = "\n".join(
        f"LET ${name} = $episode.{name};"
        for name in (episode_records[0] if episode_records else {})
    )
    statements = f"""
        FOR $episode IN $archive_episodes {{
            {episode_bindings}
            IF array::len((SELECT uuid FROM episode
                WHERE uuid=$uuid AND group_id!=$group_id)) > 0 {{
                THROW 'archive episode identity belongs to another organization';
            }};
            {_EPISODE_UPSERT_STATEMENTS}
        }};
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
            IF array::len((SELECT uuid FROM relates_to
                WHERE uuid=$edge.uuid AND group_id!=$edge.group_id)) > 0 {{
                THROW 'archive relationship identity belongs to another organization';
            }};
        }};
        LET $edges = $rows.map(|$edge| {{uuid: $edge.uuid, src: $edge.in, tgt: $edge.out}});
        IF array::len($rows) > 0 {{ {_RELATIONSHIP_BULK_UPSERT_STATEMENTS} }};
        FOR $mention IN $archive_mentions {{
            LET $uuid = $mention.uuid;
            LET $group_id = $mention.group_id;
            LET $created_at = $mention.created_at;
            LET $rel = $mention.rel;
            LET $src = (SELECT VALUE id FROM episode
                WHERE uuid=$mention.source_node_uuid AND group_id=$group_id LIMIT 1)[0];
            LET $tgt = (SELECT VALUE id FROM entity
                WHERE uuid=$mention.target_node_uuid AND group_id=$group_id LIMIT 1)[0];
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
    return CompanionRestorePlan(
        preconditions=snapshot_sql
        + "IF crypto::sha256(type::string($graph_auxiliary)) != $archive_companion_fingerprint {"
        "THROW 'archive companion destination changed before restore'; };",
        statements=statements,
        parameters={
            "archive_companion_fingerprint": rows[0]["fingerprint"],
            "archive_episodes": native_archive_parameters(episode_records),
            "archive_relationships": native_archive_parameters(relationship_records),
            "archive_mentions": native_archive_parameters(mention_records),
        },
        episodes_restored=len(selected_episodes),
        episodes_skipped=len(episodes) - len(selected_episodes),
        relationships_restored=len(relationships),
        mentions_restored=len(selected_mentions),
        mentions_skipped=len(mentions) - len(selected_mentions),
    )
