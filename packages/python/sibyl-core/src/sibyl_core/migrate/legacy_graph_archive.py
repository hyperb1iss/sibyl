"""Archive compatibility for legacy graph episode payloads."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from surrealdb import RecordID

from sibyl_core.services.graph import normalize_records

ARCHIVE_GRAPH_TABLES = ("episode",)
ARCHIVE_GRAPH_EDGES = ("mentions",)
BACKUP_RECORD_TABLES = (*ARCHIVE_GRAPH_TABLES, "entity")


@dataclass
class BackupEpisodeSource:
    value: str


@dataclass
class BackupEpisodeNode:
    uuid: str
    name: str
    group_id: str
    source: BackupEpisodeSource
    source_description: str
    content: str
    entity_edges: list[str]
    created_at: datetime
    valid_at: datetime
    labels: list[str] = field(default_factory=lambda: ["Episodic"])


@dataclass
class BackupMentionEdge:
    uuid: str
    group_id: str
    source_node_uuid: str
    target_node_uuid: str
    created_at: datetime


def serialize_backup_datetime(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value or "")


def parse_backup_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    raise ValueError(f"Invalid backup datetime: {value!r}")


def coerce_episode_source(value: Any) -> BackupEpisodeSource:
    raw_value = str(value or "").strip().lower()
    return BackupEpisodeSource(raw_value or "message")


def string_list(value: Any, *, default: list[str] | None = None) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    return list(default or [])


def episode_payload_from_node(node: Any, *, organization_id: str) -> dict[str, Any]:
    source = getattr(node, "source", None)
    source_value = getattr(source, "value", source)
    return {
        "uuid": node.uuid,
        "name": node.name,
        "source": str(source_value or "message"),
        "source_description": node.source_description,
        "content": node.content,
        "labels": list(node.labels),
        "group_id": node.group_id or organization_id,
        "created_at": serialize_backup_datetime(node.created_at),
        "valid_at": serialize_backup_datetime(node.valid_at),
        "entity_edges": list(node.entity_edges or []),
    }


def mention_payload_from_edge(edge: Any, *, organization_id: str) -> dict[str, Any]:
    return {
        "uuid": edge.uuid,
        "source_id": edge.source_node_uuid,
        "target_id": edge.target_node_uuid,
        "group_id": edge.group_id or organization_id,
        "created_at": serialize_backup_datetime(edge.created_at),
    }


def episode_from_payload(payload: dict[str, Any], *, organization_id: str) -> BackupEpisodeNode:
    created_at = parse_backup_datetime(payload.get("created_at"))
    valid_at = parse_backup_datetime(payload.get("valid_at") or created_at)
    return BackupEpisodeNode(
        uuid=str(payload.get("uuid") or ""),
        name=str(payload.get("name") or ""),
        group_id=organization_id,
        source=coerce_episode_source(payload.get("source")),
        source_description=str(payload.get("source_description") or ""),
        content=str(payload.get("content") or ""),
        entity_edges=list(payload.get("entity_edges") or []),
        created_at=created_at,
        valid_at=valid_at,
    )


def mention_from_payload(payload: dict[str, Any], *, organization_id: str) -> BackupMentionEdge:
    return BackupMentionEdge(
        uuid=str(payload.get("uuid") or ""),
        group_id=organization_id,
        source_node_uuid=str(payload.get("source_id") or payload.get("source_node_uuid") or ""),
        target_node_uuid=str(payload.get("target_id") or payload.get("target_node_uuid") or ""),
        created_at=parse_backup_datetime(payload.get("created_at")),
    )


async def record_id(client: Any, table: str, uuid: str) -> Any | None:
    if table not in BACKUP_RECORD_TABLES:
        raise ValueError(f"Unsupported backup record table: {table}")

    rows = normalize_records(
        await client.execute_query(
            f"SELECT id AS record_id FROM {table} WHERE uuid = $uuid LIMIT 1;",
            uuid=uuid,
        )
    )
    if not rows:
        return None
    return rows[0].get("record_id")


async def mention_exists(client: Any, uuid: str) -> bool:
    rows = normalize_records(
        await client.execute_query(
            "SELECT uuid FROM mentions WHERE uuid = $uuid LIMIT 1;",
            uuid=uuid,
        )
    )
    return bool(rows)


async def save_native_episode(client: Any, episode: BackupEpisodeNode) -> None:
    await client.execute_query(
        """
        UPSERT episode SET
            uuid = $uuid,
            name = $name,
            source = $source,
            source_description = $source_description,
            content = $content,
            labels = $labels,
            group_id = $group_id,
            created_at = $created_at,
            valid_at = $valid_at,
            entity_edges = $entity_edges
        WHERE uuid = $uuid;
        """,
        uuid=episode.uuid,
        name=episode.name,
        source=episode.source.value,
        source_description=episode.source_description,
        content=episode.content,
        labels=list(episode.labels),
        group_id=episode.group_id,
        created_at=episode.created_at,
        valid_at=episode.valid_at,
        entity_edges=list(episode.entity_edges),
    )


async def save_native_mention(client: Any, mention: BackupMentionEdge) -> None:
    source_record_id = await record_id(client, "episode", mention.source_node_uuid)
    target_record_id = await record_id(client, "entity", mention.target_node_uuid)
    if source_record_id is None or target_record_id is None:
        msg = (
            f"Cannot save mention {mention.uuid!r}: source episode "
            f"{mention.source_node_uuid!r} or target entity "
            f"{mention.target_node_uuid!r} not found"
        )
        raise ValueError(msg)

    await client.execute_query(
        """
        DELETE FROM mentions WHERE uuid = $uuid AND (in != $src OR out != $tgt);
        LET $updated = (UPDATE mentions SET
            in = $src,
            out = $tgt,
            uuid = $uuid,
            group_id = $group_id,
            created_at = $created_at
        WHERE uuid = $uuid RETURN id);
        IF array::len($updated) = 0 THEN
            RELATE $src->$rel->$tgt SET
                uuid = $uuid,
                group_id = $group_id,
                created_at = $created_at;
        END;
        """,
        rel=RecordID("mentions", mention.uuid),
        src=source_record_id,
        tgt=target_record_id,
        uuid=mention.uuid,
        group_id=mention.group_id,
        created_at=mention.created_at,
    )


async def list_native_backup_episodes(
    *,
    organization_id: str,
    client: Any,
    page_size: int,
) -> list[dict[str, Any]]:
    episodes: list[dict[str, Any]] = []
    offset = 0
    while True:
        rows = normalize_records(
            await client.execute_query(
                """
                SELECT uuid,
                       name,
                       source,
                       source_description,
                       content,
                       labels,
                       group_id,
                       created_at,
                       valid_at,
                       entity_edges
                FROM episode
                WHERE group_id = $group_id
                ORDER BY uuid DESC
                LIMIT $limit START $offset;
                """,
                group_id=organization_id,
                limit=page_size,
                offset=offset,
            )
        )
        if not rows:
            break
        for row in rows:
            episodes.append(
                {
                    "uuid": str(row.get("uuid") or ""),
                    "name": str(row.get("name") or ""),
                    "source": str(row.get("source") or "message"),
                    "source_description": row.get("source_description"),
                    "content": str(row.get("content") or ""),
                    "labels": string_list(row.get("labels"), default=["Episodic"]),
                    "group_id": str(row.get("group_id") or organization_id),
                    "created_at": serialize_backup_datetime(row.get("created_at")),
                    "valid_at": serialize_backup_datetime(
                        row.get("valid_at") or row.get("created_at")
                    ),
                    "entity_edges": string_list(row.get("entity_edges")),
                }
            )
        if len(rows) < page_size:
            break
        offset += len(rows)
    return episodes


async def list_native_backup_mentions(
    *,
    organization_id: str,
    client: Any,
    page_size: int,
) -> list[dict[str, Any]]:
    mentions: list[dict[str, Any]] = []
    offset = 0
    while True:
        rows = normalize_records(
            await client.execute_query(
                """
                SELECT uuid,
                       in.uuid AS source_id,
                       out.uuid AS target_id,
                       group_id,
                       created_at
                FROM mentions
                WHERE group_id = $group_id
                ORDER BY uuid DESC
                LIMIT $limit START $offset;
                """,
                group_id=organization_id,
                limit=page_size,
                offset=offset,
            )
        )
        if not rows:
            break
        for row in rows:
            mentions.append(
                {
                    "uuid": str(row.get("uuid") or ""),
                    "source_id": str(row.get("source_id") or ""),
                    "target_id": str(row.get("target_id") or ""),
                    "group_id": str(row.get("group_id") or organization_id),
                    "created_at": serialize_backup_datetime(row.get("created_at")),
                }
            )
        if len(rows) < page_size:
            break
        offset += len(rows)
    return mentions


__all__ = [
    "ARCHIVE_GRAPH_EDGES",
    "ARCHIVE_GRAPH_TABLES",
    "BACKUP_RECORD_TABLES",
    "BackupEpisodeNode",
    "BackupEpisodeSource",
    "BackupMentionEdge",
    "episode_from_payload",
    "episode_payload_from_node",
    "list_native_backup_episodes",
    "list_native_backup_mentions",
    "mention_exists",
    "mention_from_payload",
    "mention_payload_from_edge",
    "parse_backup_datetime",
    "record_id",
    "save_native_episode",
    "save_native_mention",
]
