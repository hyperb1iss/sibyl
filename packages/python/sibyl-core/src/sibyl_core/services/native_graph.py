"""Graphiti-free SurrealDB graph helpers for native memory paths."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from difflib import SequenceMatcher
from enum import Enum
from typing import Any, cast

from surrealdb import RecordID

from sibyl_core.backends.surreal.dedicated_client import DedicatedSurrealClient
from sibyl_core.backends.surreal.fulltext import build_fulltext_query
from sibyl_core.backends.surreal.schema import bootstrap_schema
from sibyl_core.config import settings
from sibyl_core.models.entities import Entity, EntityType, Relationship, RelationshipType

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

    async def create(self, entity: Entity) -> str:
        return await self.create_direct(entity, generate_embedding=False)

    async def get(self, entity_id: str) -> Entity:
        row = await _select_one(
            self._client,
            """
            SELECT *
            FROM entity
            WHERE group_id = $group_id AND uuid = $uuid
            LIMIT 1;
            """,
            group_id=self._group_id,
            uuid=entity_id,
        )
        if row is None:
            raise KeyError(entity_id)
        return _entity_from_row(row)

    async def get_notes_for_task(self, task_id: str, limit: int = 50) -> list[Entity]:
        rows = normalize_records(
            await self._client.execute_query(
                """
                SELECT *
                FROM entity
                WHERE group_id = $group_id
                  AND entity_type = 'note'
                  AND task_id = $task_id
                ORDER BY created_at DESC, uuid DESC
                LIMIT $limit;
                """,
                group_id=self._group_id,
                task_id=task_id,
                limit=max(int(limit), 1),
            )
        )
        return [_entity_from_row(row) for row in rows]

    async def update(self, entity_id: str, updates: dict[str, Any]) -> Entity | None:
        if not updates:
            return await self.get(entity_id)

        existing = await self.get(entity_id)
        merged_metadata = {**(existing.metadata or {})}
        update_metadata = updates.get("metadata")
        if isinstance(update_metadata, dict):
            merged_metadata.update(
                {str(key): _jsonable(value) for key, value in update_metadata.items()}
            )

        excluded_keys = {
            "content",
            "description",
            "embedding",
            "metadata",
            "name",
            "source_file",
            "title",
        }
        merged_metadata.update(
            {
                str(key): _jsonable(value)
                for key, value in updates.items()
                if key not in excluded_keys
            }
        )

        source_file = updates.get("source_file", existing.source_file)
        embedding = updates.get("embedding", existing.embedding)
        updated = Entity(
            id=existing.id,
            entity_type=existing.entity_type,
            name=str(updates.get("name") or updates.get("title") or existing.name),
            description=str(updates.get("description", existing.description) or ""),
            content=str(updates.get("content", existing.content) or ""),
            organization_id=existing.organization_id,
            created_by=existing.created_by,
            modified_by=str(updates.get("modified_by") or existing.modified_by or "") or None,
            metadata=merged_metadata,
            created_at=existing.created_at,
            updated_at=datetime.now(UTC),
            source_file=str(source_file) if source_file else None,
            embedding=embedding if isinstance(embedding, list) else None,
        )
        await _replace_entity(self._client, updated, group_id=self._group_id)
        return updated

    async def search(
        self,
        *,
        query: str,
        entity_types: Sequence[EntityType] | None = None,
        limit: int = 10,
    ) -> list[tuple[Entity, float]]:
        search_query = build_fulltext_query(query)
        if not search_query:
            return []
        type_values = [entity_type.value for entity_type in entity_types or ()]
        type_clause = "AND entity_type IN $entity_types" if type_values else ""
        rows = normalize_records(
            await self._client.execute_query(
                """
                SELECT *,
                       math::max([
                           search::score(0),
                           search::score(1),
                           search::score(2),
                           search::score(3)
                       ]) AS score
                FROM entity
                WHERE group_id = $group_id
                """
                + type_clause
                + """
                  AND (
                      name @0@ $search_query
                      OR summary @1@ $search_query
                      OR description @2@ $search_query
                      OR content @3@ $search_query
                  )
                ORDER BY score DESC, created_at DESC, uuid DESC
                LIMIT $limit;
                """,
                group_id=self._group_id,
                search_query=search_query,
                entity_types=type_values,
                limit=max(int(limit), 1),
            )
        )
        results: list[tuple[Entity, float]] = []
        for row in rows:
            entity = _entity_from_row(row)
            results.append((entity, _bounded_similarity_score(query, entity)))
        if not results:
            results = await self._fallback_text_search(
                query=query,
                entity_types=entity_types,
                limit=limit,
            )
        return results

    async def _fallback_text_search(
        self,
        *,
        query: str,
        entity_types: Sequence[EntityType] | None = None,
        limit: int = 10,
    ) -> list[tuple[Entity, float]]:
        normalized_query = _normalize_search_text(query)
        if not normalized_query:
            return []

        type_values = [entity_type.value for entity_type in entity_types or ()]
        type_clause = "AND entity_type IN $entity_types" if type_values else ""
        candidate_limit = min(max(int(limit) * 8, 50), 500)
        rows = normalize_records(
            await self._client.execute_query(
                """
                SELECT *
                FROM entity
                WHERE group_id = $group_id
                """
                + type_clause
                + """
                ORDER BY created_at DESC, uuid DESC
                LIMIT $candidate_limit;
                """,
                group_id=self._group_id,
                entity_types=type_values,
                candidate_limit=candidate_limit,
            )
        )

        scored: list[tuple[Entity, float]] = []
        for row in rows:
            entity = _entity_from_row(row)
            score = _bounded_similarity_score(query, entity)
            if score > 0:
                scored.append((entity, score))

        scored.sort(key=lambda item: (item[1], item[0].created_at, item[0].id), reverse=True)
        return scored[: max(int(limit), 1)]

    async def search_exact_name(
        self,
        *,
        query: str,
        entity_types: Sequence[EntityType] | None = None,
        limit: int = 10,
    ) -> list[tuple[Entity, float]]:
        type_values = [entity_type.value for entity_type in entity_types or ()]
        type_clause = "AND entity_type IN $entity_types" if type_values else ""
        rows = normalize_records(
            await self._client.execute_query(
                """
                SELECT *
                FROM entity
                WHERE group_id = $group_id
                  AND name = $name_query
                """
                + type_clause
                + """
                ORDER BY created_at DESC, uuid DESC
                LIMIT $limit;
                """,
                group_id=self._group_id,
                name_query=query,
                entity_types=type_values,
                limit=max(int(limit), 1),
            )
        )
        return [(_entity_from_row(row), 1.0) for row in rows]

    async def list_by_type(
        self,
        entity_type: EntityType,
        *,
        limit: int = 100,
        offset: int = 0,
        project_id: str | None = None,
        epic_id: str | None = None,
        no_epic: bool = False,
        status: str | None = None,
        priority: str | None = None,
        complexity: str | None = None,
        feature: str | None = None,
        tags: Sequence[str] | None = None,
        include_archived: bool = False,
    ) -> list[Entity]:
        if limit <= 0:
            return []

        status_values = _lower_filter_values(status)
        priority_values = _lower_filter_values(priority)
        complexity_values = _lower_filter_values(complexity)
        tag_values = _lower_sequence_values(tags)
        requires_recheck = any(
            [
                project_id is not None,
                epic_id is not None,
                no_epic,
                bool(status_values),
                bool(priority_values),
                bool(complexity_values),
                bool(feature),
                bool(tag_values),
                not include_archived,
            ]
        )
        target_count = max(int(offset), 0) + max(int(limit), 1) if requires_recheck else limit
        query_offset = 0 if requires_recheck else max(int(offset), 0)
        page_size = min(max(target_count, 1), 1000)
        entities: list[Entity] = []
        seen_entity_ids: set[str] = set()
        seen_pages: set[tuple[str | None, ...]] = set()

        while len(entities) < target_count:
            rows = normalize_records(
                await self._client.execute_query(
                    """
                    SELECT *
                    FROM entity
                    WHERE group_id = $group_id
                      AND entity_type = $entity_type
                    ORDER BY created_at DESC, uuid DESC
                    LIMIT $limit START $offset;
                    """,
                    group_id=self._group_id,
                    entity_type=entity_type.value,
                    limit=page_size,
                    offset=query_offset,
                )
            )
            if not rows:
                break

            page_signature = tuple(
                row_uuid if isinstance(row_uuid := row.get("uuid"), str) else None for row in rows
            )
            if page_signature in seen_pages:
                break
            seen_pages.add(page_signature)

            for row in rows:
                entity = _entity_from_row(row)
                if entity.id in seen_entity_ids:
                    continue
                if not _entity_matches_list_filters(
                    entity,
                    project_id=project_id,
                    epic_id=epic_id,
                    no_epic=no_epic,
                    status_values=status_values,
                    priority_values=priority_values,
                    complexity_values=complexity_values,
                    feature=feature,
                    tag_values=tag_values,
                    include_archived=include_archived,
                ):
                    continue

                seen_entity_ids.add(entity.id)
                entities.append(entity)
                if len(entities) >= target_count:
                    break

            query_offset += len(rows)
            if len(rows) < page_size:
                break

        if requires_recheck:
            start = max(int(offset), 0)
            return entities[start : start + max(int(limit), 1)]
        return entities[: max(int(limit), 1)]

    async def list_all(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        include_archived: bool = False,
    ) -> list[Entity]:
        if limit <= 0:
            return []
        target_count = max(int(offset), 0) + max(int(limit), 1) if not include_archived else limit
        query_offset = 0 if not include_archived else max(int(offset), 0)
        page_size = min(max(target_count, 1), 1000)
        entities: list[Entity] = []
        seen_entity_ids: set[str] = set()
        seen_pages: set[tuple[str | None, ...]] = set()

        while len(entities) < target_count:
            rows = normalize_records(
                await self._client.execute_query(
                    """
                    SELECT *
                    FROM entity
                    WHERE group_id = $group_id
                    ORDER BY created_at DESC, uuid DESC
                    LIMIT $limit START $offset;
                    """,
                    group_id=self._group_id,
                    limit=page_size,
                    offset=query_offset,
                )
            )
            if not rows:
                break

            page_signature = tuple(
                row_uuid if isinstance(row_uuid := row.get("uuid"), str) else None for row in rows
            )
            if page_signature in seen_pages:
                break
            seen_pages.add(page_signature)

            for row in rows:
                entity = _entity_from_row(row)
                if entity.id in seen_entity_ids:
                    continue
                if (
                    not include_archived
                    and str(_metadata_scalar(entity, "status") or "").lower() == "archived"
                ):
                    continue
                seen_entity_ids.add(entity.id)
                entities.append(entity)
                if len(entities) >= target_count:
                    break

            query_offset += len(rows)
            if len(rows) < page_size:
                break

        if not include_archived:
            start = max(int(offset), 0)
            return entities[start : start + max(int(limit), 1)]
        return entities[: max(int(limit), 1)]


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

    async def create(self, relationship: Relationship) -> str:
        await _replace_relationship(self._client, relationship, group_id=self._group_id)
        return relationship.id

    async def get_for_entity(
        self,
        entity_id: str,
        relationship_types: Sequence[RelationshipType] | None = None,
        direction: str = "both",
    ) -> list[Relationship]:
        type_values = [rel_type.value for rel_type in relationship_types or ()]
        type_clause = " AND name IN $relationship_types" if type_values else ""
        if direction == "outgoing":
            direction_clause = " AND in.uuid = $entity_id"
        elif direction == "incoming":
            direction_clause = " AND out.uuid = $entity_id"
        else:
            direction_clause = " AND (in.uuid = $entity_id OR out.uuid = $entity_id)"

        rows = normalize_records(
            await self._client.execute_query(
                """
                SELECT uuid,
                       name,
                       fact,
                       attributes,
                       created_at,
                       in.uuid AS source_uuid,
                       out.uuid AS target_uuid
                FROM relates_to
                WHERE group_id = $group_id
                """
                + direction_clause
                + type_clause
                + """
                ORDER BY created_at DESC, uuid DESC;
                """,
                group_id=self._group_id,
                entity_id=entity_id,
                relationship_types=type_values,
            )
        )
        return [_relationship_from_row(row) for row in rows]

    async def get_related_entities(
        self,
        entity_id: str,
        relationship_types: Sequence[RelationshipType] | None = None,
        max_depth: int = 1,
        limit: int = 50,
    ) -> list[tuple[Entity, Relationship]]:
        del max_depth
        type_values = [rel_type.value for rel_type in relationship_types or ()]
        type_clause = "AND name IN $relationship_types" if type_values else ""
        edge_rows = normalize_records(
            await self._client.execute_query(
                """
                SELECT uuid,
                       name,
                       fact,
                       attributes,
                       created_at,
                       in.uuid AS source_uuid,
                       out.uuid AS target_uuid
                FROM relates_to
                WHERE group_id = $group_id
                  AND (in.uuid = $entity_id OR out.uuid = $entity_id)
                """
                + type_clause
                + """
                ORDER BY created_at DESC, uuid DESC
                LIMIT $limit;
                """,
                group_id=self._group_id,
                entity_id=entity_id,
                relationship_types=type_values,
                limit=max(int(limit), 1),
            )
        )
        edge_pairs: list[tuple[SurrealRecord, str]] = []
        for row in edge_rows:
            other_id = (
                row.get("target_uuid")
                if row.get("source_uuid") == entity_id
                else row.get("source_uuid")
            )
            if isinstance(other_id, str) and other_id:
                edge_pairs.append((row, other_id))
        other_ids = [other_id for _, other_id in edge_pairs]
        if not other_ids:
            return []

        entity_rows = normalize_records(
            await self._client.execute_query(
                """
                SELECT *
                FROM entity
                WHERE group_id = $group_id AND uuid IN $entity_ids;
                """,
                group_id=self._group_id,
                entity_ids=other_ids,
            )
        )
        entities_by_id = {str(row.get("uuid")): _entity_from_row(row) for row in entity_rows}
        results: list[tuple[Entity, Relationship]] = []
        for row, other_id in edge_pairs:
            entity = entities_by_id.get(other_id)
            if entity is None:
                continue
            results.append((entity, _relationship_from_row(row)))
        return results

    async def list_all(
        self,
        relationship_types: Sequence[RelationshipType] | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Relationship]:
        if limit <= 0:
            return []
        type_values = [rel_type.value for rel_type in relationship_types or ()]
        type_clause = "AND name IN $relationship_types" if type_values else ""
        rows = normalize_records(
            await self._client.execute_query(
                """
                SELECT uuid,
                       name,
                       fact,
                       attributes,
                       created_at,
                       in.uuid AS source_uuid,
                       out.uuid AS target_uuid
                FROM relates_to
                WHERE group_id = $group_id
                """
                + type_clause
                + """
                ORDER BY created_at DESC, uuid DESC
                LIMIT $limit START $offset;
                """,
                group_id=self._group_id,
                relationship_types=type_values,
                limit=max(int(limit), 1),
                offset=max(int(offset), 0),
            )
        )
        return [_relationship_from_row(row) for row in rows]

    async def delete_between(
        self,
        source_id: str,
        target_id: str,
        relationship_type: RelationshipType,
    ) -> int:
        rows = normalize_records(
            await self._client.execute_query(
                """
                DELETE FROM relates_to
                WHERE group_id = $group_id
                  AND in.uuid = $source_id
                  AND out.uuid = $target_id
                  AND name = $relationship_type
                RETURN BEFORE;
                """,
                group_id=self._group_id,
                source_id=source_id,
                target_id=target_id,
                relationship_type=relationship_type.value,
            )
        )
        return len(rows)


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


def _entity_from_row(row: SurrealRecord) -> Entity:
    attributes = row.get("attributes")
    metadata = dict(attributes) if isinstance(attributes, dict) else {}
    raw_metadata = metadata.get("metadata")
    if isinstance(raw_metadata, str):
        try:
            parsed = json.loads(raw_metadata)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            metadata.update({str(key): value for key, value in parsed.items()})
    for key in ("project_id", "epic_id", "task_id", "status", "priority", "complexity", "feature"):
        value = row.get(key)
        if value is not None and metadata.get(key) is None:
            metadata[key] = value
    row_tags = row.get("tags")
    if row_tags is not None and metadata.get("tags") is None:
        metadata["tags"] = row_tags

    return Entity(
        id=str(row.get("uuid") or ""),
        entity_type=_entity_type_from_row(row),
        name=str(row.get("name") or ""),
        description=str(row.get("description") or metadata.get("description") or ""),
        content=str(row.get("content") or metadata.get("content") or ""),
        organization_id=str(row.get("group_id") or "") or None,
        metadata=metadata,
        created_at=_row_datetime(row.get("created_at")) or datetime.now(UTC),
        updated_at=_row_datetime(row.get("updated_at")) or datetime.now(UTC),
        source_file=str(metadata.get("source_file") or "") or None,
        embedding=_row_embedding(row.get("name_embedding")),
    )


def _entity_type_from_row(row: SurrealRecord) -> EntityType:
    value = str(row.get("entity_type") or "").lower()
    try:
        return EntityType(value)
    except ValueError:
        return EntityType.ARTIFACT


def _row_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


def _row_embedding(value: object) -> list[float] | None:
    if not isinstance(value, list):
        return None
    embedding: list[float] = []
    for item in value:
        if not isinstance(item, int | float):
            return None
        embedding.append(float(item))
    return embedding


def _row_score(row: SurrealRecord) -> float:
    score = row.get("score")
    if isinstance(score, int | float):
        return float(score)
    return 1.0


def _entity_matches_list_filters(
    entity: Entity,
    *,
    project_id: str | None,
    epic_id: str | None,
    no_epic: bool,
    status_values: Sequence[str],
    priority_values: Sequence[str],
    complexity_values: Sequence[str],
    feature: str | None,
    tag_values: Sequence[str],
    include_archived: bool,
) -> bool:
    if project_id and _metadata_scalar(entity, "project_id") != project_id:
        return False
    entity_epic_id = _metadata_scalar(entity, "epic_id")
    if epic_id and entity_epic_id != epic_id:
        return False
    if no_epic and entity_epic_id:
        return False
    entity_status = _metadata_scalar(entity, "status")
    if status_values and str(entity_status or "").lower() not in status_values:
        return False
    if not include_archived and str(entity_status or "").lower() == "archived":
        return False
    if (
        priority_values
        and str(_metadata_scalar(entity, "priority") or "").lower() not in priority_values
    ):
        return False
    if (
        complexity_values
        and str(_metadata_scalar(entity, "complexity") or "").lower() not in complexity_values
    ):
        return False
    if feature and str(_metadata_scalar(entity, "feature") or "").lower() != feature.lower():
        return False
    if tag_values:
        entity_tags = _metadata_str_values(entity, "tags")
        if not any(tag in entity_tags for tag in tag_values):
            return False
    return True


def _metadata_scalar(entity: Entity, key: str) -> object | None:
    return dict(entity.metadata or {}).get(key)


def _metadata_str_values(entity: Entity, key: str) -> list[str]:
    value = _metadata_scalar(entity, key)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return [value.lower()]
        if isinstance(parsed, list):
            return [str(item).lower() for item in parsed if str(item)]
        return [value.lower()]
    if isinstance(value, Iterable) and not isinstance(value, bytes | dict):
        return [str(item).lower() for item in value if str(item)]
    return []


def _lower_filter_values(value: str | None) -> list[str]:
    if not value:
        return []
    return [part.strip().lower() for part in value.split(",") if part.strip()]


def _lower_sequence_values(values: Sequence[str] | None) -> list[str]:
    return [str(value).strip().lower() for value in values or () if str(value).strip()]


def _bounded_similarity_score(query: str, entity: Entity) -> float:
    query_text = _normalize_search_text(query)
    entity_text = _normalize_search_text(
        " ".join(
            part
            for part in (
                entity.name,
                entity.description,
                entity.content,
                str(entity.metadata.get("summary") or ""),
            )
            if part
        )
    )
    if not query_text or not entity_text:
        return 0.0
    if query_text in entity_text or entity_text in query_text:
        return 1.0

    query_tokens = set(_SEARCH_TOKEN_RE.findall(query_text))
    entity_tokens = set(_SEARCH_TOKEN_RE.findall(entity_text))
    if not query_tokens or not entity_tokens:
        return 0.0

    overlap = query_tokens & entity_tokens
    jaccard = len(overlap) / len(query_tokens | entity_tokens)
    coverage = len(overlap) / len(query_tokens)
    sequence = SequenceMatcher(None, query_text[:1000], entity_text[:1000]).ratio()
    return min(max(jaccard, coverage * 0.85, sequence * 0.9), 1.0)


_SEARCH_TOKEN_RE = re.compile(r"[a-z0-9_]+")


def _normalize_search_text(value: str) -> str:
    return " ".join(_SEARCH_TOKEN_RE.findall(value.lower()))


def _relationship_from_row(row: SurrealRecord) -> Relationship:
    attributes = row.get("attributes")
    metadata = dict(attributes) if isinstance(attributes, dict) else {}
    fact = row.get("fact")
    if isinstance(fact, str):
        metadata.setdefault("fact", fact)
    return Relationship(
        id=str(row.get("uuid") or ""),
        relationship_type=_relationship_type_from_row(row),
        source_id=str(row.get("source_uuid") or ""),
        target_id=str(row.get("target_uuid") or ""),
        weight=_metadata_weight(metadata),
        metadata=metadata,
        created_at=_row_datetime(row.get("created_at")) or datetime.now(UTC),
    )


def _relationship_type_from_row(row: SurrealRecord) -> RelationshipType:
    value = str(row.get("name") or RelationshipType.RELATED_TO.value)
    try:
        return RelationshipType(value)
    except ValueError:
        return RelationshipType.RELATED_TO


def _metadata_weight(metadata: dict[object, object]) -> float:
    weight = metadata.get("weight")
    if isinstance(weight, int | float):
        return float(weight)
    return 1.0


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
    metadata = _entity_metadata(entity)
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


def _entity_metadata(entity: Entity) -> dict[str, object]:
    metadata = {str(key): _jsonable(value) for key, value in dict(entity.metadata or {}).items()}
    model_dump = entity.model_dump(
        mode="json",
        exclude={
            "id",
            "entity_type",
            "name",
            "description",
            "content",
            "organization_id",
            "created_by",
            "modified_by",
            "metadata",
            "created_at",
            "updated_at",
            "source_file",
            "embedding",
        },
    )
    for key, value in model_dump.items():
        if value not in (None, "", [], {}):
            metadata[key] = _jsonable(value)
    return metadata


def _jsonable(value: object) -> object:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return model_dump(mode="json")
    if isinstance(value, dict):
        return {str(key): _jsonable(nested) for key, nested in value.items()}
    if isinstance(value, list | tuple | set):
        return [_jsonable(nested) for nested in value]
    return value


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
