"""Reusable migration archive merge helpers."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from sibyl_core.migrate.archive import (
    AUTH_FILENAME,
    CONTENT_FILENAME,
    GRAPH_FILENAME,
    LoadedArchive,
    auth_payload_from_archive,
    build_manifest,
    content_payload_from_archive,
    graph_payload_from_archive,
    normalize_mention_payloads,
    normalize_relationship_payloads,
)

REFERENCE_FIELDS = {
    "api_key_id",
    "accepted_by_user_id",
    "actor_user_id",
    "created_by_user_id",
    "document_id",
    "entity_id",
    "group_id",
    "organization_id",
    "org_id",
    "owner_user_id",
    "principal_id",
    "project_id",
    "source_id",
    "source_node_uuid",
    "target_id",
    "target_node_uuid",
    "target_principal_id",
    "team_id",
    "user_id",
}
REFERENCE_LIST_FIELDS = {"entity_edges", "entity_ids"}
SCOPED_USER_KEY_FIELDS = {
    "scope_key": "memory_scope",
    "suggested_scope_key": "suggested_memory_scope",
    "target_scope_key": "target_memory_scope",
}
VOLATILE_AUTH_TABLES = {
    "device_authorization_requests",
    "login_history",
    "password_reset_tokens",
    "user_sessions",
}


class EntityCollisionPolicy(StrEnum):
    MERGE_BY_TYPE_NAME = "merge-by-type-name"
    KEEP_ALL = "keep-all"


@dataclass(frozen=True)
class ArchiveMergeOptions:
    canonical_org_id: str
    canonical_org_name: str = ""
    canonical_org_slug: str = ""
    entity_collision_policy: EntityCollisionPolicy = EntityCollisionPolicy.MERGE_BY_TYPE_NAME
    drop_volatile_auth: bool = True


@dataclass(frozen=True)
class ArchiveMergeResult:
    archive: LoadedArchive
    source_count: int
    source_org_ids: tuple[str, ...]
    entity_alias_count: int
    user_alias_count: int
    graph_counts: dict[str, int]
    auth_row_counts: dict[str, int]
    content_row_counts: dict[str, int]


def merge_archives(
    archives: list[LoadedArchive],
    *,
    options: ArchiveMergeOptions,
) -> ArchiveMergeResult:
    if not archives:
        msg = "At least one archive is required"
        raise ValueError(msg)
    if not options.canonical_org_id.strip():
        msg = "A canonical organization id is required"
        raise ValueError(msg)

    source_org_ids = _source_org_ids(archives)
    replacements = {org_id: options.canonical_org_id for org_id in source_org_ids}

    graph_payloads = [
        payload for archive in archives if (payload := graph_payload_from_archive(archive))
    ]
    auth_payloads = [
        payload for archive in archives if (payload := auth_payload_from_archive(archive))
    ]
    content_payloads = [
        payload for archive in archives if (payload := content_payload_from_archive(archive))
    ]

    files: dict[str, bytes] = {}
    file_metadata: dict[str, dict[str, Any]] = {}
    entity_alias_count = 0
    graph_counts: dict[str, int] = {}
    auth_row_counts: dict[str, int] = {}
    content_row_counts: dict[str, int] = {}

    if graph_payloads:
        graph_payload, entity_id_map, entity_alias_count = _merge_graph_payloads(
            graph_payloads,
            options=options,
            replacements=replacements,
        )
        graph_counts = {
            "entities": len(graph_payload.get("entities", [])),
            "relationships": len(graph_payload.get("relationships", [])),
            "episodes": len(graph_payload.get("episodes", [])),
            "mentions": len(graph_payload.get("mentions", [])),
        }
        files[GRAPH_FILENAME] = _json_bytes(graph_payload)
        file_metadata[GRAPH_FILENAME] = {
            "kind": "graph",
            "entity_count": graph_counts["entities"],
            "relationship_count": graph_counts["relationships"],
            "episode_count": graph_counts["episodes"],
            "mention_count": graph_counts["mentions"],
            "entity_alias_count": entity_alias_count,
        }
    else:
        entity_id_map = {}

    user_id_map: dict[str, str] = {}
    user_alias_count = 0

    if auth_payloads:
        merged_users, user_id_map, user_alias_count = _merge_user_payloads(auth_payloads)
        auth_payload = _merge_tabular_payloads(
            auth_payloads,
            replacements=replacements | user_id_map,
            canonical_org_id=options.canonical_org_id,
            canonical_org_name=options.canonical_org_name,
            canonical_org_slug=options.canonical_org_slug,
            force_canonical_org=True,
            drop_tables=VOLATILE_AUTH_TABLES if options.drop_volatile_auth else set(),
            table_overrides={"users": merged_users},
        )
        auth_row_counts = dict(auth_payload["row_counts"])
        files[AUTH_FILENAME] = _json_bytes(auth_payload)
        file_metadata[AUTH_FILENAME] = {
            "kind": "auth",
            "table_count": len(auth_payload["tables"]),
            "total_rows": auth_payload["total_rows"],
        }

    if content_payloads:
        content_payload = _merge_tabular_payloads(
            content_payloads,
            replacements=replacements | entity_id_map | user_id_map,
            canonical_org_id=options.canonical_org_id,
            canonical_org_name=options.canonical_org_name,
            canonical_org_slug=options.canonical_org_slug,
            force_canonical_org=False,
        )
        content_row_counts = dict(content_payload["row_counts"])
        files[CONTENT_FILENAME] = _json_bytes(content_payload)
        file_metadata[CONTENT_FILENAME] = {
            "kind": "content",
            "table_count": len(content_payload["tables"]),
            "total_rows": content_payload["total_rows"],
        }

    if not files:
        msg = "No mergeable graph, auth, or content payloads found"
        raise ValueError(msg)

    manifest = build_manifest(
        organization_id=options.canonical_org_id,
        source_store="merged",
        files=files,
        file_metadata=file_metadata,
        metadata={
            "merge": {
                "source_count": len(archives),
                "source_org_ids": list(source_org_ids),
                "canonical_org_name": options.canonical_org_name,
                "canonical_org_slug": options.canonical_org_slug,
                "entity_collision_policy": options.entity_collision_policy.value,
                "entity_alias_count": entity_alias_count,
                "user_alias_count": user_alias_count,
                "drop_volatile_auth": options.drop_volatile_auth,
            }
        },
    )
    merged_archive = LoadedArchive(
        source=Path("<merged>"),
        manifest=manifest,
        files=files,
    )
    return ArchiveMergeResult(
        archive=merged_archive,
        source_count=len(archives),
        source_org_ids=source_org_ids,
        entity_alias_count=entity_alias_count,
        user_alias_count=user_alias_count,
        graph_counts=graph_counts,
        auth_row_counts=auth_row_counts,
        content_row_counts=content_row_counts,
    )


def _json_bytes(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _source_org_ids(archives: list[LoadedArchive]) -> tuple[str, ...]:
    org_ids: list[str] = []
    for archive in archives:
        candidates = [archive.manifest.organization_id]
        graph_payload = graph_payload_from_archive(archive)
        if graph_payload is not None:
            candidates.append(str(graph_payload.get("organization_id") or ""))
        for org_id in candidates:
            normalized = org_id.strip()
            if normalized and normalized not in org_ids:
                org_ids.append(normalized)
    return tuple(org_ids)


def _rewrite_value(value: Any, replacements: dict[str, str], *, field: str = "") -> Any:
    if isinstance(value, str):
        return replacements.get(value, value) if field in REFERENCE_FIELDS else value
    if isinstance(value, list):
        return [
            replacements.get(item, item)
            if field in REFERENCE_LIST_FIELDS and isinstance(item, str)
            else _rewrite_value(item, replacements, field=field)
            for item in value
        ]
    if isinstance(value, dict):
        rewritten = {
            key: _rewrite_value(item, replacements, field=key) for key, item in value.items()
        }
        _rewrite_scoped_user_keys(rewritten, replacements)
        return rewritten
    return value


def _rewrite_scoped_user_keys(row: dict[str, Any], replacements: dict[str, str]) -> None:
    for key, scope_key in SCOPED_USER_KEY_FIELDS.items():
        value = str(row.get(key) or "").strip()
        scope = str(row.get(scope_key) or "").strip().casefold()
        if value and scope == "private" and value in replacements:
            row[key] = replacements[value]


def _row_uuid(row: dict[str, Any]) -> str:
    return str(row.get("uuid") or row.get("id") or "").strip()


def _normalized_email(row: dict[str, Any]) -> str:
    return str(row.get("email") or "").strip().casefold()


def _user_identity_key(row: dict[str, Any]) -> tuple[str, str]:
    email = _normalized_email(row)
    if email:
        return ("email", email)
    github_id = str(row.get("github_id") or "").strip()
    if github_id:
        return ("github_id", github_id)
    user_id = _row_uuid(row)
    if user_id:
        return ("uuid", user_id)
    return ("row", json.dumps(row, sort_keys=True, default=str))


def _merge_user_payloads(
    payloads: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, str], int]:
    users: list[dict[str, Any]] = []
    by_identity: dict[tuple[str, str], dict[str, Any]] = {}
    user_id_map: dict[str, str] = {}
    alias_count = 0

    for payload in payloads:
        tables = payload.get("tables", {})
        if not isinstance(tables, dict):
            continue
        raw_users = tables.get("users", [])
        if not isinstance(raw_users, list):
            continue
        for raw_user in raw_users:
            if not isinstance(raw_user, dict):
                continue
            user = dict(raw_user)
            user_id = _row_uuid(user)
            if email := _normalized_email(user):
                user["email"] = email
            key = _user_identity_key(user)
            existing = by_identity.get(key)
            if existing is None:
                by_identity[key] = user
                users.append(user)
                if user_id:
                    user_id_map[user_id] = _row_uuid(user)
                continue

            canonical_id = _row_uuid(existing)
            if user_id and canonical_id:
                user_id_map[user_id] = canonical_id
                if user_id != canonical_id:
                    alias_count += 1
            _merge_user(existing, user)

    return users, user_id_map, alias_count


def _merge_user(target: dict[str, Any], source: dict[str, Any]) -> None:
    for field in (
        "avatar_url",
        "bio",
        "email",
        "email_verified_at",
        "github_id",
        "last_login_at",
        "name",
        "password_hash",
        "password_iterations",
        "password_salt",
        "timezone",
    ):
        if not target.get(field) and source.get(field):
            target[field] = source[field]
    target["is_admin"] = bool(target.get("is_admin") or source.get("is_admin"))
    target_preferences = target.get("preferences")
    source_preferences = source.get("preferences")
    if isinstance(target_preferences, dict) and isinstance(source_preferences, dict):
        for key, value in source_preferences.items():
            target_preferences.setdefault(key, value)


def _merge_graph_payloads(
    payloads: list[dict[str, Any]],
    *,
    options: ArchiveMergeOptions,
    replacements: dict[str, str],
) -> tuple[dict[str, Any], dict[str, str], int]:
    entity_id_map: dict[str, str] = {}
    entities: list[dict[str, Any]] = []
    entity_index: dict[tuple[str, str], dict[str, Any]] = {}
    alias_count = 0

    for payload in payloads:
        source_org_id = str(payload.get("organization_id") or "").strip()
        for raw_entity in payload.get("entities", []):
            if not isinstance(raw_entity, dict):
                continue
            entity = _rewrite_value(dict(raw_entity), replacements)
            entity["organization_id"] = options.canonical_org_id
            if "group_id" in entity:
                entity["group_id"] = options.canonical_org_id

            entity_id = _entity_id(entity)
            key = _entity_key(entity, policy=options.entity_collision_policy)
            existing = entity_index.get(key)
            if existing is None:
                _add_entity_provenance(entity, source_org_id=source_org_id, entity_id=entity_id)
                entity_index[key] = entity
                entities.append(entity)
                if entity_id:
                    entity_id_map[entity_id] = _entity_id(entity)
                continue

            canonical_id = _entity_id(existing)
            if entity_id and canonical_id:
                entity_id_map[entity_id] = canonical_id
                alias_count += 1
            _merge_entity(existing, entity, source_org_id=source_org_id, entity_id=entity_id)

    relationships = _merge_relationships(payloads, replacements=replacements | entity_id_map)
    episodes = _merge_episodes(
        payloads,
        replacements=replacements | entity_id_map,
        canonical_org_id=options.canonical_org_id,
    )
    mentions = _merge_mentions(
        payloads,
        replacements=replacements | entity_id_map,
        canonical_org_id=options.canonical_org_id,
    )
    graph_payload = {
        "version": "2.0",
        "created_at": datetime.now(UTC).isoformat(),
        "organization_id": options.canonical_org_id,
        "entity_count": len(entities),
        "relationship_count": len(relationships),
        "episode_count": len(episodes),
        "mention_count": len(mentions),
        "entities": entities,
        "relationships": relationships,
        "episodes": episodes,
        "mentions": mentions,
    }
    return graph_payload, entity_id_map, alias_count


def _entity_id(entity: dict[str, Any]) -> str:
    return str(entity.get("id") or entity.get("uuid") or "").strip()


def _entity_key(
    entity: dict[str, Any],
    *,
    policy: EntityCollisionPolicy,
) -> tuple[str, str]:
    entity_id = _entity_id(entity)
    if policy == EntityCollisionPolicy.KEEP_ALL:
        return ("id", entity_id or json.dumps(entity, sort_keys=True, default=str))

    entity_type = str(entity.get("entity_type") or entity.get("type") or "").strip().casefold()
    name = str(entity.get("name") or "").strip().casefold()
    if entity_type and name:
        return (entity_type, name)
    return ("id", entity_id or json.dumps(entity, sort_keys=True, default=str))


def _add_entity_provenance(
    entity: dict[str, Any],
    *,
    source_org_id: str,
    entity_id: str,
) -> None:
    metadata = entity.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
        entity["metadata"] = metadata
    _append_unique(metadata, "merged_from_org_ids", source_org_id)
    _append_unique(metadata, "merged_from_entity_ids", entity_id)


def _merge_entity(
    target: dict[str, Any],
    source: dict[str, Any],
    *,
    source_org_id: str,
    entity_id: str,
) -> None:
    _add_entity_provenance(target, source_org_id=source_org_id, entity_id=entity_id)
    for field in ("description", "content", "source_file", "embedding"):
        if not target.get(field) and source.get(field):
            target[field] = source[field]
    target_metadata = target.get("metadata")
    source_metadata = source.get("metadata")
    if isinstance(target_metadata, dict) and isinstance(source_metadata, dict):
        for key, value in source_metadata.items():
            target_metadata.setdefault(key, value)


def _append_unique(metadata: dict[str, Any], key: str, value: str) -> None:
    if not value:
        return
    existing = metadata.get(key)
    values = existing if isinstance(existing, list) else []
    if value not in values:
        values.append(value)
    metadata[key] = values


def _merge_relationships(
    payloads: list[dict[str, Any]],
    *,
    replacements: dict[str, str],
) -> list[dict[str, Any]]:
    relationships: list[dict[str, Any]] = []
    for payload in payloads:
        for raw_relationship in payload.get("relationships", []):
            if not isinstance(raw_relationship, dict):
                continue
            relationship = _rewrite_value(dict(raw_relationship), replacements)
            if "group_id" in relationship:
                relationship["group_id"] = replacements.get(
                    str(relationship["group_id"]),
                    str(relationship["group_id"]),
                )
            relationships.append(relationship)
    return normalize_relationship_payloads(relationships)


def _merge_episodes(
    payloads: list[dict[str, Any]],
    *,
    replacements: dict[str, str],
    canonical_org_id: str,
) -> list[dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    passthrough: list[dict[str, Any]] = []

    for payload in payloads:
        for raw_episode in payload.get("episodes", []):
            if not isinstance(raw_episode, dict):
                continue
            episode = _rewrite_value(dict(raw_episode), replacements)
            if "group_id" in episode:
                episode["group_id"] = canonical_org_id
            if "organization_id" in episode:
                episode["organization_id"] = canonical_org_id
            episode_id = str(episode.get("uuid") or episode.get("id") or "").strip()
            if episode_id:
                by_id[episode_id] = episode
            else:
                passthrough.append(episode)

    return [*passthrough, *by_id.values()]


def _merge_mentions(
    payloads: list[dict[str, Any]],
    *,
    replacements: dict[str, str],
    canonical_org_id: str,
) -> list[dict[str, Any]]:
    mentions: list[dict[str, Any]] = []
    seen_edges: set[tuple[str, str]] = set()

    for payload in payloads:
        for raw_mention in payload.get("mentions", []):
            if not isinstance(raw_mention, dict):
                continue
            mention = _rewrite_value(dict(raw_mention), replacements)
            if "group_id" in mention:
                mention["group_id"] = canonical_org_id
            if "organization_id" in mention:
                mention["organization_id"] = canonical_org_id
            source_id = str(mention.get("source_id") or mention.get("source_node_uuid") or "")
            target_id = str(mention.get("target_id") or mention.get("target_node_uuid") or "")
            edge = (source_id, target_id)
            if all(edge):
                if edge in seen_edges:
                    continue
                seen_edges.add(edge)
            mentions.append(mention)

    return normalize_mention_payloads(mentions)


def _merge_tabular_payloads(
    payloads: list[dict[str, Any]],
    *,
    replacements: dict[str, str],
    canonical_org_id: str,
    canonical_org_name: str,
    canonical_org_slug: str,
    force_canonical_org: bool,
    drop_tables: set[str] | None = None,
    table_overrides: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    merged_tables: dict[str, list[dict[str, Any]]] = {}
    drop_tables = drop_tables or set()

    for payload in payloads:
        tables = payload.get("tables", {})
        if not isinstance(tables, dict):
            continue
        for table_name, raw_rows in tables.items():
            table_name = str(table_name)
            if table_name in drop_tables:
                continue
            if not isinstance(raw_rows, list):
                continue
            rows = merged_tables.setdefault(table_name, [])
            for raw_row in raw_rows:
                if isinstance(raw_row, dict):
                    rows.append(_rewrite_value(dict(raw_row), replacements))

    if force_canonical_org:
        merged_tables["organizations"] = [
            _canonical_org_row(
                merged_tables.get("organizations", []),
                canonical_org_id=canonical_org_id,
                canonical_org_name=canonical_org_name,
                canonical_org_slug=canonical_org_slug,
            )
        ]

    for table_name, rows in (table_overrides or {}).items():
        merged_tables[table_name] = rows

    deduped_tables = {
        table_name: _dedupe_rows(table_name, rows)
        for table_name, rows in sorted(merged_tables.items())
    }
    row_counts = {table_name: len(rows) for table_name, rows in deduped_tables.items()}
    return {
        "version": "1.0",
        "created_at": datetime.now(UTC).isoformat(),
        "tables": deduped_tables,
        "row_counts": row_counts,
        "total_rows": sum(row_counts.values()),
    }


def _canonical_org_row(
    rows: list[dict[str, Any]],
    *,
    canonical_org_id: str,
    canonical_org_name: str,
    canonical_org_slug: str,
) -> dict[str, Any]:
    row = dict(rows[0]) if rows else {}
    fallback_org_name = str(row.get("name") or canonical_org_id)
    fallback_org_slug = str(row.get("slug") or canonical_org_id)
    if "id" in row:
        row["id"] = canonical_org_id
    row["uuid"] = canonical_org_id
    row["name"] = canonical_org_name or fallback_org_name
    row["slug"] = canonical_org_slug or fallback_org_slug
    row["is_personal"] = False
    return row


def _dedupe_rows(table_name: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_identity: dict[tuple[str, ...], dict[str, Any]] = {}
    for row in rows:
        identity = _row_identity(table_name, row)
        existing = by_identity.get(identity)
        if existing is None:
            by_identity[identity] = row
        elif table_name == "organization_members":
            _merge_org_membership(existing, row)
    return list(by_identity.values())


def _row_identity(table_name: str, row: dict[str, Any]) -> tuple[str, ...]:
    if table_name == "users":
        return (table_name, *_user_identity_key(row))

    field_groups = {
        "organization_members": ("organization_id", "user_id"),
        "team_members": ("team_id", "user_id"),
        "project_members": ("project_id", "user_id"),
        "team_projects": ("team_id", "project_id"),
        "api_key_project_scopes": ("api_key_id", "project_id"),
        "api_key_memory_space_scopes": ("api_key_id", "memory_space_id"),
        "memory_spaces": ("organization_id", "memory_scope", "scope_key"),
        "memory_space_members": ("space_id", "principal_type", "principal_id"),
        "oauth_connections": ("provider", "provider_user_id"),
        "system_settings": ("key",),
        "backup_settings": ("key",),
        "teams": ("organization_id", "slug"),
        "projects": ("organization_id", "slug"),
        "crawl_sources": ("organization_id", "url"),
        "crawled_documents": ("source_id", "url"),
        "document_chunks": ("document_id", "chunk_index"),
    }
    fields = field_groups.get(table_name)
    if fields:
        values = tuple(str(row.get(field) or "").strip() for field in fields)
        if all(values):
            return (table_name, *values)

    for field in ("uuid", "id", "key", "email"):
        value = str(row.get(field) or "").strip()
        if value:
            return (table_name, field, value)
    return (table_name, json.dumps(row, sort_keys=True, default=str))


def _merge_org_membership(target: dict[str, Any], source: dict[str, Any]) -> None:
    role_rank = {"owner": 3, "admin": 2, "member": 1}
    target_role = str(target.get("role") or "")
    source_role = str(source.get("role") or "")
    if role_rank.get(source_role, 0) > role_rank.get(target_role, 0):
        target["role"] = source_role


__all__ = [
    "ArchiveMergeOptions",
    "ArchiveMergeResult",
    "EntityCollisionPolicy",
    "merge_archives",
]
