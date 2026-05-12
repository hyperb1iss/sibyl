"""Graphiti-free SurrealDB graph helpers for native memory paths."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

from surrealdb import RecordID

from sibyl_core.backends.surreal.dedicated_client import DedicatedSurrealClient
from sibyl_core.backends.surreal.schema import bootstrap_schema
from sibyl_core.config import settings
from sibyl_core.models.entities import Entity, Relationship

type SurrealRecord = dict[str, object]

_prepared_groups: set[str] = set()
_prepare_lock = asyncio.Lock()
_client_lock = asyncio.Lock()
_clients: dict[str, NativeSurrealGraphClient] = {}


@dataclass(frozen=True, slots=True)
class NativeGraphRuntime:
    client: NativeSurrealGraphClient
    entity_manager: NativeEntityManager
    relationship_manager: NativeRelationshipManager


class NativeSurrealGraphClient(DedicatedSurrealClient):
    """Dedicated SurrealDB graph client scoped to one organization namespace."""

    def __init__(
        self,
        *,
        group_id: str,
        url: str,
        username: str = "",
        password: str = "",
        token: str = "",
        namespace_prefix: str = "org_",
        database: str = "graph",
    ) -> None:
        self._group_id = group_id
        super().__init__(
            url=url,
            username=username,
            password=password,
            token=token,
            namespace=_namespace_for_group(namespace_prefix, group_id),
            database=database,
            client_kind="native_graph",
        )

    @property
    def group_id(self) -> str:
        return self._group_id


class NativeEntityManager:
    def __init__(self, client: NativeSurrealGraphClient, *, group_id: str) -> None:
        self._client = client
        self._group_id = group_id

    async def create_direct(self, entity: Entity, *, generate_embedding: bool = False) -> str:
        del generate_embedding
        await _replace_entity(self._client, entity, group_id=self._group_id)
        return entity.id


class NativeRelationshipManager:
    def __init__(self, client: NativeSurrealGraphClient, *, group_id: str) -> None:
        self._client = client
        self._group_id = group_id

    async def create_bulk(self, relationships: Sequence[Relationship]) -> tuple[int, int]:
        created = 0
        failed = 0
        for relationship in relationships:
            try:
                await _replace_relationship(self._client, relationship, group_id=self._group_id)
                created += 1
            except Exception:
                failed += 1
        return created, failed


async def get_native_graph_runtime(group_id: str) -> NativeGraphRuntime:
    client = await get_native_graph_client(group_id)
    await prepare_native_graph_schema(client)
    return NativeGraphRuntime(
        client=client,
        entity_manager=NativeEntityManager(client, group_id=group_id),
        relationship_manager=NativeRelationshipManager(client, group_id=group_id),
    )


async def get_native_graph_client(group_id: str) -> NativeSurrealGraphClient:
    async with _client_lock:
        client = _clients.get(group_id)
        if client is None:
            client = NativeSurrealGraphClient(
                group_id=group_id,
                url=settings.resolved_surreal_url,
                username=settings.surreal_username,
                password=settings.surreal_password.get_secret_value(),
                token=settings.surreal_token.get_secret_value(),
                namespace_prefix=settings.surreal_namespace_prefix,
                database=settings.surreal_database,
            )
            _clients[group_id] = client
        return client


async def close_native_graph_clients() -> None:
    async with _client_lock:
        clients = list(_clients.values())
        _clients.clear()
        _prepared_groups.clear()
    await asyncio.gather(*(client.close() for client in clients), return_exceptions=True)


async def prepare_native_graph_schema(client: NativeSurrealGraphClient) -> None:
    group_id = client.group_id
    if group_id in _prepared_groups:
        return
    async with _prepare_lock:
        if group_id in _prepared_groups:
            return
        await bootstrap_schema(cast("Any", client))
        _prepared_groups.add(group_id)


def _namespace_for_group(prefix: str, group_id: str) -> str:
    sanitized = group_id.replace("-", "").lower() if group_id else "default"
    return f"{prefix}{sanitized}"


def _normalize_record(record: object) -> SurrealRecord | None:
    if not isinstance(record, dict):
        return None
    payload = {str(key): value for key, value in record.items()}
    if "result" in payload and ("status" in payload or "time" in payload):
        return None
    payload.pop("id", None)
    return payload


def normalize_records(result: object) -> list[SurrealRecord]:
    if result is None:
        return []
    if isinstance(result, dict):
        payload = {str(key): value for key, value in result.items()}
        if "result" in payload and ("status" in payload or "time" in payload):
            return normalize_records(payload.get("result"))
        record = _normalize_record(payload)
        return [record] if record is not None else []
    if not isinstance(result, list):
        return []
    records: list[SurrealRecord] = []
    for item in result:
        records.extend(normalize_records(item))
    return records


async def _select_one(
    client: NativeSurrealGraphClient,
    query: str,
    **params: object,
) -> SurrealRecord | None:
    rows = normalize_records(await client.execute_query(query, **params))
    return rows[0] if rows else None


async def _replace_entity(
    client: NativeSurrealGraphClient,
    entity: Entity,
    *,
    group_id: str,
) -> SurrealRecord:
    record = _entity_record(entity, group_id=group_id)
    rows = normalize_records(
        await client.execute_query(
            """
            UPSERT entity SET
                uuid = $uuid,
                name = $name,
                entity_type = $entity_type,
                summary = $summary,
                description = $description,
                content = $content,
                labels = $labels,
                attributes = $attributes,
                group_id = $group_id,
                created_at = $created_at,
                updated_at = $updated_at,
                project_id = $project_id,
                epic_id = $epic_id,
                task_id = $task_id,
                status = $status,
                priority = $priority,
                complexity = $complexity,
                feature = $feature,
                tags = $tags,
                name_embedding = $name_embedding
            WHERE uuid = $uuid;
            """,
            **record,
        )
    )
    if rows:
        return rows[0]
    stored = await _select_one(
        client, "SELECT * FROM entity WHERE uuid = $uuid LIMIT 1;", uuid=entity.id
    )
    if stored is None:
        raise RuntimeError(f"failed to persist entity {entity.id}")
    return stored


def _entity_record(entity: Entity, *, group_id: str) -> SurrealRecord:
    metadata = dict(entity.metadata or {})
    now = datetime.now(UTC)
    updated_at = _metadata_str(metadata, "updated_at") or now.isoformat()
    created_at = entity.created_at or now
    project_id = _metadata_str(metadata, "project_id")
    epic_id = _metadata_str(metadata, "epic_id")
    task_id = _metadata_str(metadata, "task_id")
    status = _metadata_str(metadata, "status")
    priority = _metadata_str(metadata, "priority")
    complexity = _metadata_str(metadata, "complexity")
    feature = _metadata_str(metadata, "feature")
    tags = _metadata_str_list(metadata.get("tags"))
    attributes: dict[str, object] = {
        **metadata,
        "description": entity.description or "",
        "content": entity.content or "",
        "source_file": entity.source_file or "",
        "updated_at": updated_at,
        "_direct_insert": True,
        "metadata": json.dumps(metadata),
        "entity_type": entity.entity_type.value,
    }
    return {
        "uuid": entity.id,
        "name": entity.name,
        "entity_type": entity.entity_type.value,
        "summary": entity.description[:500] if entity.description else entity.name,
        "description": entity.description or "",
        "content": entity.content or "",
        "labels": [entity.entity_type.value, "Entity"],
        "attributes": attributes,
        "group_id": group_id,
        "created_at": created_at,
        "updated_at": updated_at,
        "project_id": project_id,
        "epic_id": epic_id,
        "task_id": task_id,
        "status": status,
        "priority": priority,
        "complexity": complexity,
        "feature": feature,
        "tags": tags,
        "name_embedding": entity.embedding,
    }


async def _replace_relationship(
    client: NativeSurrealGraphClient,
    relationship: Relationship,
    *,
    group_id: str,
) -> None:
    src = await _record_id(client, relationship.source_id)
    tgt = await _record_id(client, relationship.target_id)
    if src is None or tgt is None:
        raise ValueError(
            "relates_to endpoint not found: "
            f"{relationship.source_id!r} -> {relationship.target_id!r}"
        )
    payload = _relationship_record(relationship, group_id=group_id)
    await client.execute_query(
        """
        DELETE FROM relates_to WHERE uuid = $uuid AND (in != $src OR out != $tgt);
        LET $updated = (UPDATE relates_to SET
            in = $src,
            out = $tgt,
            uuid = $uuid,
            name = $name,
            fact = $fact,
            fact_embedding = $fact_embedding,
            group_id = $group_id,
            episodes = $episodes,
            attributes = $attributes,
            created_at = $created_at,
            expired_at = $expired_at,
            valid_at = $valid_at,
            invalid_at = $invalid_at
        WHERE uuid = $uuid RETURN id);
        IF array::len($updated) = 0 THEN
            RELATE $src->$rel->$tgt SET
                uuid = $uuid,
                name = $name,
                fact = $fact,
                fact_embedding = $fact_embedding,
                group_id = $group_id,
                episodes = $episodes,
                attributes = $attributes,
                created_at = $created_at,
                expired_at = $expired_at,
                valid_at = $valid_at,
                invalid_at = $invalid_at;
        END;
        """,
        src=src,
        tgt=tgt,
        rel=RecordID("relates_to", relationship.id),
        **payload,
    )


async def _record_id(client: NativeSurrealGraphClient, uuid: str) -> object | None:
    row = await _select_one(
        client,
        "SELECT id AS record_id FROM entity WHERE uuid = $uuid LIMIT 1;",
        uuid=uuid,
    )
    return row.get("record_id") if row else None


def _relationship_record(relationship: Relationship, *, group_id: str) -> SurrealRecord:
    metadata = dict(relationship.metadata or {})
    fact = _metadata_str(metadata, "fact") or _relationship_fact(relationship)
    return {
        "uuid": relationship.id,
        "name": relationship.relationship_type.value,
        "fact": fact,
        "fact_embedding": None,
        "group_id": group_id,
        "episodes": _metadata_str_list(metadata.get("episodes")),
        "attributes": metadata,
        "created_at": relationship.created_at,
        "expired_at": None,
        "valid_at": _metadata_datetime(metadata.get("valid_at")),
        "invalid_at": None,
    }


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


def _metadata_str_list(value: object) -> list[str] | None:
    if not isinstance(value, Iterable) or isinstance(value, str | bytes | dict):
        return None
    return [str(item) for item in value if str(item)]


def _metadata_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


__all__ = [
    "NativeEntityManager",
    "NativeGraphRuntime",
    "NativeRelationshipManager",
    "NativeSurrealGraphClient",
    "close_native_graph_clients",
    "get_native_graph_client",
    "get_native_graph_runtime",
    "normalize_records",
    "prepare_native_graph_schema",
]
