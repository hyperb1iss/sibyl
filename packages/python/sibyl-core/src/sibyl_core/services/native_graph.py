"""Graphiti-free SurrealDB graph helpers for native memory paths."""

from __future__ import annotations

import asyncio
import json
import re
from collections import OrderedDict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from difflib import SequenceMatcher
from enum import Enum
from typing import Any, cast

import structlog
from surrealdb import RecordID

from sibyl_core.backends.surreal.connection import _is_transient_connection_error
from sibyl_core.backends.surreal.dedicated_client import DedicatedSurrealClient
from sibyl_core.backends.surreal.fulltext import build_fulltext_query
from sibyl_core.backends.surreal.schema import EMBEDDING_DIM, bootstrap_schema
from sibyl_core.config import settings
from sibyl_core.embeddings.native import (
    NativeEmbeddingProvider,
    native_entity_embedding_text,
    native_relationship_embedding_text,
)
from sibyl_core.models.entities import (
    Entity,
    EntityType,
    Procedure,
    ProcedureStep,
    Relationship,
    RelationshipType,
)
from sibyl_core.models.tasks import (
    Task,
    TaskComplexity,
    TaskPriority,
    TaskStatus,
)

type SurrealRecord = dict[str, object]

_prepared_groups: set[str] = set()
_prepare_lock = asyncio.Lock()
_client_lock = asyncio.Lock()
_clients: OrderedDict[str, NativeSurrealGraphClient] = OrderedDict()
_ENTITY_LIST_FIELDS = "* OMIT content, embedding, name_embedding, attributes.content"
log = structlog.get_logger()


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
    supports_bounded_entity_list = True
    supports_lightweight_entity_list = True

    def __init__(
        self,
        client: NativeSurrealGraphClient,
        *,
        group_id: str,
        embedding_provider: NativeEmbeddingProvider | None = None,
    ) -> None:
        self._client = client
        self._group_id = group_id
        self._embedding_provider = embedding_provider

    async def create_direct(self, entity: Entity, *, generate_embedding: bool = False) -> str:
        if generate_embedding:
            entity = await _entity_with_native_embedding(entity, self._embedding_provider)
        await _replace_entity(self._client, entity, group_id=self._group_id)
        return entity.id

    async def create(self, entity: Entity) -> str:
        return await self.create_direct(entity, generate_embedding=True)

    async def delete(self, entity_id: str) -> bool:
        rows = normalize_records(
            await self._client.execute_query(
                """
                DELETE FROM relates_to
                WHERE group_id = $group_id
                  AND (in.uuid = $uuid OR out.uuid = $uuid)
                RETURN BEFORE;
                DELETE FROM mentions
                WHERE group_id = $group_id
                  AND (in.uuid = $uuid OR out.uuid = $uuid)
                RETURN BEFORE;
                DELETE FROM entity
                WHERE group_id = $group_id AND uuid = $uuid
                RETURN BEFORE;
                """,
                group_id=self._group_id,
                uuid=entity_id,
            )
        )
        return any(row.get("uuid") == entity_id for row in rows)

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
        result_limit = max(int(limit), 1)
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
                limit=result_limit,
            )
        )
        fulltext_results: list[tuple[Entity, float]] = []
        for row in rows:
            entity = _entity_from_row(row)
            fulltext_results.append((entity, _bounded_similarity_score(query, entity)))

        vector_results = await self._vector_search(
            query=query,
            entity_types=entity_types,
            limit=result_limit,
        )

        results = _merge_ranked_entity_results(
            [
                (vector_results, 1.2),
                (fulltext_results, 1.0),
            ],
            limit=result_limit,
        )
        if not results:
            results = await self._fallback_text_search(
                query=query,
                entity_types=entity_types,
                limit=limit,
            )
        return results

    async def _vector_search(
        self,
        *,
        query: str,
        entity_types: Sequence[EntityType] | None,
        limit: int,
    ) -> list[tuple[Entity, float]]:
        if self._embedding_provider is None:
            return []
        type_values = [entity_type.value for entity_type in entity_types or ()]
        type_clause = "AND entity_type IN $entity_types" if type_values else ""
        candidate_limit = min(max(int(limit) * 4, 32), 200)
        try:
            embeddings = await self._embedding_provider.embed_texts(
                [query],
                input_kind="query",
            )
            query_embedding = _embedding_vector_from_batch(
                embeddings,
                self._embedding_provider.metadata.dimensions,
            )
            rows = normalize_records(
                await self._client.execute_query(
                    """
                    SELECT *
                    FROM (
                        SELECT *,
                               (1 - vector::distance::knn()) AS score
                        FROM entity
                        WHERE group_id = $group_id
                    """
                    + type_clause
                    + f"""
                          AND name_embedding <|{candidate_limit}, 40|> $query_embedding
                    )
                    ORDER BY score DESC, created_at DESC, uuid DESC
                    LIMIT $limit;
                    """,
                    group_id=self._group_id,
                    query_embedding=query_embedding,
                    entity_types=type_values,
                    limit=candidate_limit,
                )
            )
        except Exception as exc:
            log.warning(
                "native_entity_vector_search_failed",
                error_type=type(exc).__name__,
            )
            return []

        return [(_entity_from_row(row), _row_score(row)) for row in rows]

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
        query: str,
        *,
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

    async def list_epics_for_project(
        self,
        project_id: str,
        status: str | None = None,
        limit: int = 50,
        enrich_progress: bool = False,
    ) -> list[Entity]:
        return await self.list_by_type(
            EntityType.EPIC,
            project_id=project_id,
            status=status,
            limit=limit,
            enrich_epic_progress=enrich_progress,
        )

    async def get_epic_progress(self, epic_id: str) -> dict[str, Any]:
        progress = await self._epic_progress_map({epic_id})
        return progress[epic_id]

    async def get_project_summary(
        self,
        project_id: str,
        *,
        actionable_limit: int = 5,
        critical_limit: int = 3,
        epic_limit: int = 3,
    ) -> dict[str, Any]:
        tasks: list[Entity] = []
        offset = 0
        page_size = 1000
        while True:
            page = await self.list_by_type(
                EntityType.TASK,
                project_id=project_id,
                limit=page_size,
                offset=offset,
                include_archived=True,
            )
            if not page:
                break
            tasks.extend(page)
            if len(page) < page_size:
                break
            offset += len(page)

        status_counts: dict[str, int] = {}
        doing_tasks: list[dict[str, Any]] = []
        blocked_tasks: list[dict[str, Any]] = []
        review_tasks: list[dict[str, Any]] = []
        recent_tasks: list[dict[str, Any]] = []
        critical_tasks: list[dict[str, Any]] = []
        epic_progress: dict[str, dict[str, int]] = {}

        for task in tasks:
            metadata = task.metadata or {}
            status_value = str(metadata.get("status") or "todo")
            priority = str(metadata.get("priority") or "")
            epic_ref = metadata.get("epic_id")

            status_counts[status_value] = status_counts.get(status_value, 0) + 1
            if epic_ref:
                counters = epic_progress.setdefault(
                    str(epic_ref),
                    {"total_tasks": 0, "completed_tasks": 0},
                )
                counters["total_tasks"] += 1
                if status_value == "done":
                    counters["completed_tasks"] += 1

            task_info = {
                "id": task.id,
                "name": task.name,
                "status": status_value,
                "priority": priority,
            }
            is_critical = (
                priority.lower() in ("critical", "high") or "CRITICAL" in task.name.upper()
            ) and status_value not in ("done", "archived")
            if is_critical and len(critical_tasks) < critical_limit:
                critical_tasks.append(task_info)
            if status_value == "doing" and len(doing_tasks) < actionable_limit:
                doing_tasks.append(task_info)
            elif status_value == "blocked" and len(blocked_tasks) < actionable_limit:
                blocked_tasks.append(task_info)
            elif status_value == "review" and len(review_tasks) < actionable_limit:
                review_tasks.append(task_info)
            elif len(recent_tasks) < actionable_limit:
                recent_tasks.append(task_info)

        actionable: list[dict[str, Any]] = []
        for pool in (doing_tasks, blocked_tasks, review_tasks, recent_tasks):
            for task_info in pool:
                if len(actionable) >= actionable_limit:
                    break
                if task_info["id"] not in {task["id"] for task in actionable}:
                    actionable.append(task_info)
            if len(actionable) >= actionable_limit:
                break

        epics: list[dict[str, Any]] = []
        for epic in await self.list_epics_for_project(
            project_id,
            limit=epic_limit,
            enrich_progress=False,
        ):
            progress = epic_progress.get(epic.id, {})
            total_tasks = progress.get("total_tasks", 0)
            completed_tasks = progress.get("completed_tasks", 0)
            epics.append(
                {
                    "id": epic.id,
                    "name": epic.name,
                    "status": (epic.metadata or {}).get("status") or "planning",
                    "progress_pct": round(
                        (completed_tasks / total_tasks * 100) if total_tasks > 0 else 0,
                        1,
                    ),
                    "total_tasks": total_tasks,
                }
            )

        total = sum(status_counts.values())
        done = status_counts.get("done", 0)
        return {
            "status_counts": status_counts,
            "total_tasks": total,
            "progress_pct": round((done / total * 100) if total > 0 else 0, 1),
            "actionable_tasks": actionable,
            "critical_tasks": critical_tasks,
            "epics": epics,
        }

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
        enrich_epic_progress: bool = False,
        include_content: bool = True,
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
        where_clauses = [
            "group_id = $group_id",
            "entity_type = $entity_type",
        ]
        query_params: dict[str, object] = {
            "group_id": self._group_id,
            "entity_type": entity_type.value,
        }

        if project_id is not None:
            where_clauses.append(_surreal_indexed_field_equals_or_missing("project_id"))
            query_params["project_id"] = project_id
        if epic_id is not None:
            where_clauses.append(_surreal_indexed_field_equals_or_missing("epic_id"))
            query_params["epic_id"] = epic_id
        if no_epic:
            where_clauses.append(_surreal_indexed_field_missing("epic_id"))
        if status_values:
            where_clauses.append(_surreal_indexed_field_in_or_missing("status", "status_values"))
            query_params["status_values"] = status_values
        if priority_values:
            where_clauses.append(
                _surreal_indexed_field_in_or_missing("priority", "priority_values")
            )
            query_params["priority_values"] = priority_values
        if complexity_values:
            where_clauses.append(
                _surreal_indexed_field_in_or_missing("complexity", "complexity_values")
            )
            query_params["complexity_values"] = complexity_values
        if feature:
            where_clauses.append(_surreal_indexed_field_equals_or_missing("feature"))
            query_params["feature"] = feature.lower()
        if not include_archived:
            where_clauses.append("(status IS NONE OR status = '' OR status != 'archived')")
        select_fields = _entity_select_fields(include_content)

        while len(entities) < target_count:
            rows = normalize_records(
                await self._client.execute_query(
                    f"""
                    SELECT {select_fields}
                    FROM entity
                    WHERE {" AND ".join(where_clauses)}
                    ORDER BY updated_at DESC, created_at DESC, uuid DESC
                    LIMIT $limit START $offset;
                    """,
                    **query_params,
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
            entities = entities[start : start + max(int(limit), 1)]
        else:
            entities = entities[: max(int(limit), 1)]

        if entity_type == EntityType.EPIC and enrich_epic_progress:
            return await self._with_epic_progress(entities, project_id=project_id)
        return entities

    async def list_all(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        include_archived: bool = False,
        include_content: bool = True,
    ) -> list[Entity]:
        if limit <= 0:
            return []
        target_count = max(int(offset), 0) + max(int(limit), 1) if not include_archived else limit
        query_offset = 0 if not include_archived else max(int(offset), 0)
        page_size = min(max(target_count, 1), 1000)
        entities: list[Entity] = []
        seen_entity_ids: set[str] = set()
        seen_pages: set[tuple[str | None, ...]] = set()
        where_clauses = ["group_id = $group_id"]
        if not include_archived:
            where_clauses.append(
                "string::lowercase(status ?? attributes.status ?? '') != 'archived'"
            )
        select_fields = _entity_select_fields(include_content)

        while len(entities) < target_count:
            rows = normalize_records(
                await self._client.execute_query(
                    f"""
                    SELECT {select_fields}
                    FROM entity
                    WHERE {" AND ".join(where_clauses)}
                    ORDER BY updated_at DESC, created_at DESC, uuid DESC
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

    async def count_by_type(self, *, include_archived: bool = False) -> dict[str, int]:
        where_clauses = ["group_id = $group_id"]
        if not include_archived:
            where_clauses.append("(status IS NONE OR status = '' OR status != 'archived')")
        rows = normalize_records(
            await self._client.execute_query(
                """
                SELECT entity_type, count() AS entity_count
                FROM entity
                WHERE """
                + " AND ".join(where_clauses)
                + """
                GROUP BY entity_type;
                """,
                group_id=self._group_id,
            )
        )
        counts = {entity_type.value: 0 for entity_type in EntityType}
        for row in rows:
            entity_type = row.get("entity_type")
            if isinstance(entity_type, str) and entity_type:
                counts[entity_type] = _int_value(row.get("entity_count"))
        return counts

    async def _with_epic_progress(
        self, epics: list[Entity], *, project_id: str | None = None
    ) -> list[Entity]:
        progress_by_epic = await self._epic_progress_map(
            {epic.id for epic in epics},
            project_id=project_id,
        )
        enriched: list[Entity] = []
        for epic in epics:
            progress = progress_by_epic.get(epic.id, _finalize_task_progress(_new_task_progress()))
            enriched.append(
                epic.model_copy(
                    update={
                        "metadata": {
                            **(epic.metadata or {}),
                            "total_tasks": progress.get("total_tasks", 0),
                            "completed_tasks": progress.get("completed_tasks", 0),
                            "in_progress_tasks": progress.get("in_progress_tasks", 0),
                            "blocked_tasks": progress.get("blocked_tasks", 0),
                            "in_review_tasks": progress.get("in_review_tasks", 0),
                            "completion_pct": progress.get("completion_pct", 0.0),
                        }
                    }
                )
            )
        return enriched

    async def _epic_progress_map(
        self,
        epic_ids: set[str],
        *,
        project_id: str | None = None,
    ) -> dict[str, dict[str, Any]]:
        progress = {epic_id: _new_task_progress() for epic_id in epic_ids}
        if not progress:
            return {}

        epic_id_list = sorted(epic_ids)
        where_clauses = [
            "group_id = $group_id",
            "entity_type = 'task'",
            "epic_id IN $epic_ids",
        ]
        params: dict[str, Any] = {
            "group_id": self._group_id,
            "epic_ids": epic_id_list,
        }
        if project_id is not None:
            where_clauses.append("project_id = $project_id")
            params["project_id"] = project_id

        rows = normalize_records(
            await self._client.execute_query(
                """
                SELECT epic_id, status, count() AS task_count
                FROM entity
                WHERE """
                + " AND ".join(where_clauses)
                + """
                GROUP BY epic_id, status;
                """,
                **params,
            )
        )
        legacy_where_clauses = [
            "group_id = $group_id",
            "entity_type = 'task'",
            _surreal_indexed_field_missing("epic_id"),
            "attributes.epic_id IN $epic_ids",
        ]
        if project_id is not None:
            legacy_where_clauses.append(
                "(project_id = $project_id OR attributes.project_id = $project_id)"
            )
        rows.extend(
            normalize_records(
                await self._client.execute_query(
                    """
                    SELECT attributes.epic_id AS epic_id,
                           attributes.status AS status,
                           count() AS task_count
                    FROM entity
                    WHERE """
                    + " AND ".join(legacy_where_clauses)
                    + """
                    GROUP BY attributes.epic_id, attributes.status;
                    """,
                    **params,
                )
            )
        )

        for row in rows:
            epic_ref = row.get("epic_id")
            if epic_ref is None:
                continue
            counters = progress.get(str(epic_ref))
            if counters is None:
                continue
            _count_task_status(counters, row.get("status"), count=_int_value(row.get("task_count")))

        return {
            epic_id: _finalize_task_progress(counters) for epic_id, counters in progress.items()
        }


class NativeRelationshipManager:
    def __init__(
        self,
        client: NativeSurrealGraphClient,
        *,
        group_id: str,
        embedding_provider: NativeEmbeddingProvider | None = None,
    ) -> None:
        self._client = client
        self._group_id = group_id
        self._embedding_provider = embedding_provider

    async def create_bulk(self, relationships: Sequence[Relationship]) -> tuple[int, int]:
        created = 0
        failed = 0
        for relationship in relationships:
            try:
                await self.create(relationship)
                created += 1
            except Exception:
                failed += 1
        return created, failed

    async def create(self, relationship: Relationship) -> str:
        relationship = await _relationship_with_native_embedding(
            relationship,
            self._embedding_provider,
        )
        await _replace_relationship(self._client, relationship, group_id=self._group_id)
        return relationship.id

    async def delete(self, relationship_id: str) -> bool:
        rows = normalize_records(
            await self._client.execute_query(
                """
                DELETE FROM relates_to
                WHERE group_id = $group_id AND uuid = $uuid
                RETURN BEFORE;
                DELETE FROM mentions
                WHERE group_id = $group_id AND uuid = $uuid
                RETURN BEFORE;
                """,
                group_id=self._group_id,
                uuid=relationship_id,
            )
        )
        return any(row.get("uuid") == relationship_id for row in rows)

    async def get(self, relationship_id: str) -> Relationship:
        row = await _select_one(
            self._client,
            """
            SELECT id AS record_id,
                   uuid,
                   name,
                   fact,
                   group_id,
                   episodes,
                   attributes,
                   created_at,
                   expired_at,
                   valid_at,
                   invalid_at,
                   source_id ?? in.uuid AS source_uuid,
                   target_id ?? out.uuid AS target_uuid
            FROM relates_to
            WHERE group_id = $group_id AND uuid = $uuid
            LIMIT 1;
            """,
            group_id=self._group_id,
            uuid=relationship_id,
        )
        if row is None:
            raise KeyError(relationship_id)
        return _relationship_from_row(row)

    async def get_for_entity(
        self,
        entity_id: str,
        relationship_types: Sequence[RelationshipType] | None = None,
        direction: str = "both",
    ) -> list[Relationship]:
        type_values = [rel_type.value for rel_type in relationship_types or ()]
        type_clause = " AND name IN $relationship_types" if type_values else ""
        if direction == "outgoing":
            direction_clause = (
                " AND (source_id = $entity_id OR (source_id IS NONE AND in.uuid = $entity_id))"
            )
        elif direction == "incoming":
            direction_clause = (
                " AND (target_id = $entity_id OR (target_id IS NONE AND out.uuid = $entity_id))"
            )
        else:
            direction_clause = """
                AND (
                    source_id = $entity_id
                    OR target_id = $entity_id
                    OR (
                        (source_id IS NONE OR target_id IS NONE)
                        AND (in.uuid = $entity_id OR out.uuid = $entity_id)
                    )
                )
            """

        rows = normalize_records(
            await self._client.execute_query(
                """
                SELECT id AS record_id,
                       uuid,
                       name,
                       fact,
                       group_id,
                       episodes,
                       attributes,
                       created_at,
                       expired_at,
                       valid_at,
                       invalid_at,
                       source_id ?? in.uuid AS source_uuid,
                       target_id ?? out.uuid AS target_uuid
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
        for row in rows:
            if row.get("source_uuid") == entity_id:
                row["direction"] = "outgoing"
            elif row.get("target_uuid") == entity_id:
                row["direction"] = "incoming"
        return [_relationship_from_row(row) for row in rows]

    async def get_related_entities(
        self,
        entity_id: str,
        relationship_types: Sequence[RelationshipType] | None = None,
        max_depth: int = 1,
        limit: int = 50,
    ) -> list[tuple[Entity, Relationship]]:
        del max_depth
        related_by_seed = await self.get_related_entities_batch(
            [entity_id],
            relationship_types=relationship_types,
            limit_per_entity=limit,
        )
        return related_by_seed.get(entity_id, [])

    async def get_related_entities_batch(
        self,
        entity_ids: Sequence[str],
        relationship_types: Sequence[RelationshipType] | None = None,
        limit_per_entity: int = 50,
    ) -> dict[str, list[tuple[Entity, Relationship]]]:
        seed_ids = list(dict.fromkeys(str(entity_id) for entity_id in entity_ids if entity_id))
        if not seed_ids:
            return {}

        type_values = [rel_type.value for rel_type in relationship_types or ()]
        type_clause = "AND name IN $relationship_types" if type_values else ""
        per_seed_limit = max(int(limit_per_entity), 1)
        query_limit = min(per_seed_limit * len(seed_ids), 5000)
        edge_rows = normalize_records(
            await self._client.execute_query(
                """
                SELECT id AS record_id,
                       uuid,
                       name,
                       fact,
                       group_id,
                       episodes,
                       attributes,
                       created_at,
                       expired_at,
                       valid_at,
                       invalid_at,
                       source_id ?? in.uuid AS source_uuid,
                       target_id ?? out.uuid AS target_uuid
                FROM relates_to
                WHERE group_id = $group_id
                  AND (
                    source_id IN $entity_ids
                    OR target_id IN $entity_ids
                    OR (
                        (source_id IS NONE OR target_id IS NONE)
                        AND (in.uuid IN $entity_ids OR out.uuid IN $entity_ids)
                    )
                  )
                """
                + type_clause
                + """
                ORDER BY created_at DESC, uuid DESC
                LIMIT $limit;
                """,
                group_id=self._group_id,
                entity_ids=seed_ids,
                relationship_types=type_values,
                limit=query_limit,
            )
        )

        seed_id_set = set(seed_ids)
        edge_pairs_by_seed: dict[str, list[tuple[SurrealRecord, str]]] = {
            seed_id: [] for seed_id in seed_ids
        }
        other_ids: list[str] = []
        for row in edge_rows:
            source_id = row.get("source_uuid")
            target_id = row.get("target_uuid")
            if isinstance(source_id, str) and isinstance(target_id, str):
                if source_id in seed_id_set:
                    outgoing_row = {**row, "direction": "outgoing"}
                    edge_pairs_by_seed[source_id].append((outgoing_row, target_id))
                    other_ids.append(target_id)
                if target_id in seed_id_set:
                    incoming_row = {**row, "direction": "incoming"}
                    edge_pairs_by_seed[target_id].append((incoming_row, source_id))
                    other_ids.append(source_id)

        if not other_ids:
            return {seed_id: [] for seed_id in seed_ids}

        entity_rows = normalize_records(
            await self._client.execute_query(
                """
                SELECT *
                FROM entity
                WHERE group_id = $group_id AND uuid IN $entity_ids;
                """,
                group_id=self._group_id,
                entity_ids=list(dict.fromkeys(other_ids)),
            )
        )
        entities_by_id = {str(row.get("uuid")): _entity_from_row(row) for row in entity_rows}
        results: dict[str, list[tuple[Entity, Relationship]]] = {}
        for seed_id, edge_pairs in edge_pairs_by_seed.items():
            seed_results: list[tuple[Entity, Relationship]] = []
            for row, other_id in edge_pairs:
                if len(seed_results) >= per_seed_limit:
                    break
                entity = entities_by_id.get(other_id)
                if entity is None:
                    continue
                seed_results.append((entity, _relationship_from_row(row)))
            results[seed_id] = seed_results
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
                SELECT id AS record_id,
                       uuid,
                       name,
                       fact,
                       group_id,
                       episodes,
                       attributes,
                       created_at,
                       expired_at,
                       valid_at,
                       invalid_at,
                       source_id ?? in.uuid AS source_uuid,
                       target_id ?? out.uuid AS target_uuid
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

    async def find_between(
        self,
        source_id: str,
        target_id: str,
        *,
        relationship_type: RelationshipType | None = None,
    ) -> list[Relationship]:
        type_clause = "AND name = $relationship_type" if relationship_type else ""
        rows = normalize_records(
            await self._client.execute_query(
                """
                SELECT id AS record_id,
                       uuid,
                       name,
                       fact,
                       group_id,
                       episodes,
                       attributes,
                       created_at,
                       expired_at,
                       valid_at,
                       invalid_at,
                       source_id ?? in.uuid AS source_uuid,
                       target_id ?? out.uuid AS target_uuid
                FROM relates_to
                WHERE group_id = $group_id
                  AND (
                    (source_id = $source_id AND target_id = $target_id)
                    OR (source_id = $target_id AND target_id = $source_id)
                    OR (
                        (source_id IS NONE OR target_id IS NONE)
                        AND (
                            (in.uuid = $source_id AND out.uuid = $target_id)
                            OR (in.uuid = $target_id AND out.uuid = $source_id)
                        )
                    )
                  )
                """
                + type_clause
                + """
                ORDER BY created_at DESC, uuid DESC;
                """,
                group_id=self._group_id,
                source_id=source_id,
                target_id=target_id,
                relationship_type=relationship_type.value if relationship_type else None,
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
                  AND (
                    (source_id = $source_id AND target_id = $target_id)
                    OR (
                        (source_id IS NONE OR target_id IS NONE)
                        AND in.uuid = $source_id
                        AND out.uuid = $target_id
                    )
                  )
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


async def get_native_graph_runtime(
    group_id: str,
    *,
    embedding_provider: NativeEmbeddingProvider | None = None,
) -> NativeGraphRuntime:
    client = await get_native_graph_client(group_id)
    await prepare_native_graph_schema(client)
    _validate_native_embedding_dimensions(embedding_provider)
    return NativeGraphRuntime(
        client=client,
        entity_manager=NativeEntityManager(
            client,
            group_id=group_id,
            embedding_provider=embedding_provider,
        ),
        relationship_manager=NativeRelationshipManager(
            client,
            group_id=group_id,
            embedding_provider=embedding_provider,
        ),
    )


def _validate_native_embedding_dimensions(
    embedding_provider: NativeEmbeddingProvider | None,
) -> None:
    if embedding_provider is None:
        return
    dimensions = embedding_provider.metadata.dimensions
    if dimensions != EMBEDDING_DIM:
        raise ValueError(
            "native embedding provider dimensions "
            f"({dimensions}) must match Surreal graph schema ({EMBEDDING_DIM})"
        )


async def get_native_graph_client(group_id: str) -> NativeSurrealGraphClient:
    evicted: list[NativeSurrealGraphClient] = []
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
            while len(_clients) > settings.surreal_native_graph_client_cache_size:
                evicted_group_id, evicted_client = _clients.popitem(last=False)
                _prepared_groups.discard(evicted_group_id)
                evicted.append(evicted_client)
        else:
            _clients.move_to_end(group_id)
    if evicted:
        await asyncio.gather(*(client.close() for client in evicted), return_exceptions=True)
        return client
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


def entity_from_surreal_row(row: Mapping[str, object]) -> Entity:
    normalized_row = {str(key): value for key, value in row.items()}
    attributes = _row_attributes(normalized_row)
    metadata = dict(attributes)
    raw_metadata = metadata.get("metadata", normalized_row.get("metadata"))
    if isinstance(raw_metadata, str):
        try:
            parsed = json.loads(raw_metadata)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            metadata.pop("metadata", None)
            metadata = {str(key): value for key, value in parsed.items()} | metadata
    elif isinstance(raw_metadata, dict):
        metadata.pop("metadata", None)
        metadata = {str(key): value for key, value in raw_metadata.items()} | metadata

    if metadata.get("category") is None:
        metadata.pop("category", None)

    for key in (
        "project_id",
        "epic_id",
        "task_id",
        "status",
        "priority",
        "complexity",
        "feature",
        "source_id",
        "source_ids",
        "confidence",
        "valid_at",
        "valid_from",
        "valid_to",
        "invalid_at",
        "created_by",
        "modified_by",
    ):
        value = normalized_row.get(key)
        if value is not None and metadata.get(key) is None:
            metadata[key] = value
    row_tags = normalized_row.get("tags")
    if row_tags is not None and metadata.get("tags") is None:
        metadata["tags"] = row_tags

    entity_id = _entity_id_from_row(normalized_row)
    record_id = _row_record_id(normalized_row)
    if record_id and record_id != entity_id and metadata.get("record_id") is None:
        metadata["record_id"] = record_id

    entity = Entity(
        id=entity_id,
        entity_type=_entity_type_from_row(normalized_row, attributes=attributes),
        name=_first_text(normalized_row.get("name"), normalized_row.get("title"), entity_id),
        description=_first_text(
            normalized_row.get("description"),
            normalized_row.get("summary"),
            metadata.get("description"),
        ),
        content=_first_text(
            normalized_row.get("content"),
            metadata.get("content"),
            normalized_row.get("summary"),
        ),
        organization_id=_first_text(
            normalized_row.get("group_id"),
            metadata.get("group_id"),
            normalized_row.get("organization_id"),
            metadata.get("organization_id"),
        )
        or None,
        created_by=_first_text(normalized_row.get("created_by"), metadata.get("created_by"))
        or None,
        modified_by=_first_text(normalized_row.get("modified_by"), metadata.get("modified_by"))
        or None,
        metadata=metadata,
        created_at=_row_datetime(normalized_row.get("created_at") or metadata.get("created_at"))
        or datetime.now(UTC),
        updated_at=_row_datetime(normalized_row.get("updated_at") or metadata.get("updated_at"))
        or datetime.now(UTC),
        source_file=_first_text(normalized_row.get("source_file"), metadata.get("source_file"))
        or None,
        embedding=_row_embedding(
            normalized_row.get("name_embedding") or normalized_row.get("embedding")
        ),
    )
    return _coerce_native_entity(entity)


def _entity_from_row(row: SurrealRecord) -> Entity:
    return entity_from_surreal_row(row)


def _entity_select_fields(include_content: bool) -> str:
    return "*" if include_content else _ENTITY_LIST_FIELDS


def _entity_type_from_row(
    row: Mapping[str, object],
    *,
    attributes: Mapping[str, object] | None = None,
) -> EntityType:
    row_attributes = attributes if attributes is not None else _row_attributes(row)
    candidates: list[object] = [
        row.get("entity_type"),
        row_attributes.get("entity_type"),
    ]
    labels = row.get("labels")
    if isinstance(labels, list | tuple):
        candidates.extend(label for label in labels if str(label).lower() != "entity")
    for candidate in candidates:
        value = str(candidate or "").lower()
        if not value:
            continue
        try:
            return EntityType(value)
        except ValueError:
            continue
    return EntityType.ARTIFACT


def _entity_id_from_row(row: Mapping[str, object]) -> str:
    for key in ("uuid", "entity_id"):
        if text := _first_text(row.get(key)):
            return text
    raw_id = row.get("id")
    if raw_id is None:
        raw_id = row.get("record_id")
    if text := _first_text(raw_id):
        return _entity_id_from_record_text(text)
    return ""


def _entity_id_from_record_text(value: str) -> str:
    if ":" not in value:
        return value
    _table, record_key = value.split(":", 1)
    return record_key.strip("`'\"⟨⟩<>") or value


def _row_record_id(row: Mapping[str, object]) -> str | None:
    return _first_text(row.get("record_id"), row.get("id")) or None


def _row_attributes(row: Mapping[str, object]) -> dict[str, object]:
    attributes = row.get("attributes")
    if not isinstance(attributes, Mapping):
        return {}
    return {str(key): value for key, value in attributes.items()}


def _first_text(*values: object) -> str:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _coerce_native_entity(entity: Entity) -> Entity:
    if entity.entity_type == EntityType.TASK:
        return _entity_to_task(entity)
    if entity.entity_type == EntityType.PROCEDURE:
        return _entity_to_procedure(entity)
    return entity


def _coerce_enum(enum_type: type[Enum], value: object, default: Enum) -> Enum:
    if value is None:
        return default
    if isinstance(value, enum_type):
        return value
    try:
        return enum_type(value)
    except (TypeError, ValueError):
        return default


def _entity_to_task(entity: Entity) -> Task:
    meta = entity.metadata or {}
    return Task(
        id=entity.id,
        entity_type=EntityType.TASK,
        name=entity.name,
        title=str(meta.get("title") or entity.name),
        description=entity.description or str(meta.get("description") or ""),
        content=entity.content or str(meta.get("content") or ""),
        organization_id=entity.organization_id,
        created_by=entity.created_by,
        modified_by=entity.modified_by,
        metadata=meta,
        created_at=entity.created_at,
        updated_at=entity.updated_at,
        source_file=entity.source_file,
        embedding=entity.embedding,
        status=cast("TaskStatus", _coerce_enum(TaskStatus, meta.get("status"), TaskStatus.TODO)),
        priority=cast(
            "TaskPriority",
            _coerce_enum(TaskPriority, meta.get("priority"), TaskPriority.MEDIUM),
        ),
        task_order=_int_value(meta.get("task_order")),
        project_id=_optional_text(meta.get("project_id")),
        epic_id=_optional_text(meta.get("epic_id")),
        feature=_optional_text(meta.get("feature")),
        sprint=_optional_text(meta.get("sprint")),
        assignees=_metadata_str_list(meta.get("assignees")) or [],
        due_date=_row_datetime(meta.get("due_date")),
        estimated_hours=_float_value(meta.get("estimated_hours")),
        actual_hours=_float_value(meta.get("actual_hours")),
        domain=_optional_text(meta.get("domain")),
        technologies=_metadata_str_list(meta.get("technologies")) or [],
        complexity=cast(
            "TaskComplexity",
            _coerce_enum(TaskComplexity, meta.get("complexity"), TaskComplexity.MEDIUM),
        ),
        tags=_metadata_str_list(meta.get("tags")) or [],
        branch_name=_optional_text(meta.get("branch_name")),
        commit_shas=_metadata_str_list(meta.get("commit_shas")) or [],
        pr_url=_optional_text(meta.get("pr_url")),
        learnings=str(meta.get("learnings") or ""),
        blockers_encountered=_metadata_str_list(meta.get("blockers_encountered")) or [],
        started_at=_row_datetime(meta.get("started_at")),
        completed_at=_row_datetime(meta.get("completed_at")),
        reviewed_at=_row_datetime(meta.get("reviewed_at")),
    )


def _entity_to_procedure(entity: Entity) -> Procedure:
    meta = entity.metadata or {}
    steps: list[ProcedureStep] = []
    for raw_step in meta.get("steps") or []:
        if isinstance(raw_step, ProcedureStep):
            steps.append(raw_step)
        elif isinstance(raw_step, Mapping):
            steps.append(ProcedureStep.model_validate(raw_step))

    return Procedure(
        id=entity.id,
        entity_type=EntityType.PROCEDURE,
        name=entity.name,
        description=entity.description or str(meta.get("description") or ""),
        content=entity.content or str(meta.get("content") or ""),
        organization_id=entity.organization_id,
        created_by=entity.created_by,
        modified_by=entity.modified_by,
        metadata=meta,
        created_at=entity.created_at,
        updated_at=entity.updated_at,
        source_file=entity.source_file,
        embedding=entity.embedding,
        steps=steps,
        required_tools=_metadata_str_list(meta.get("required_tools")) or [],
        category=str(meta.get("category") or ""),
        estimated_minutes=_optional_int(meta.get("estimated_minutes")),
        automation_level=str(meta.get("automation_level") or "manual"),
    )


def _optional_text(value: object) -> str | None:
    return _first_text(value) or None


def _int_value(value: object) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return 0
    return 0


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    return _int_value(value)


def _float_value(value: object) -> float | None:
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


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


def _merge_ranked_entity_results(
    ranked_lists: Sequence[tuple[Sequence[tuple[Entity, float]], float]],
    *,
    limit: int,
    rrf_k: float = 60.0,
) -> list[tuple[Entity, float]]:
    scores: dict[str, float] = {}
    entities: dict[str, Entity] = {}
    original_scores: dict[str, float] = {}

    for results, weight in ranked_lists:
        for rank, (entity, score) in enumerate(results, start=1):
            key = entity.id
            scores[key] = scores.get(key, 0.0) + (weight / (rrf_k + rank))
            original_scores[key] = max(original_scores.get(key, 0.0), score)
            entities.setdefault(key, entity)

    ordered = sorted(
        scores,
        key=lambda key: (scores[key], original_scores[key], entities[key].created_at, key),
        reverse=True,
    )
    return [(entities[key], scores[key]) for key in ordered[: max(int(limit), 1)]]


def _surreal_indexed_field_missing(field: str) -> str:
    return f"({field} IS NONE OR {field} = '')"


def _surreal_indexed_field_equals_or_missing(field: str) -> str:
    return f"({field} = ${field} OR {_surreal_indexed_field_missing(field)})"


def _surreal_indexed_field_in_or_missing(field: str, param: str) -> str:
    return f"({field} IN ${param} OR {_surreal_indexed_field_missing(field)})"


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


def _new_task_progress() -> dict[str, int]:
    return {
        "total_tasks": 0,
        "completed_tasks": 0,
        "in_progress_tasks": 0,
        "blocked_tasks": 0,
        "in_review_tasks": 0,
    }


def _count_task_progress(counters: dict[str, int], task: Entity) -> None:
    _count_task_status(counters, _metadata_scalar(task, "status"))


def _count_task_status(
    counters: dict[str, int],
    status: object | None,
    *,
    count: int = 1,
) -> None:
    if count <= 0:
        return
    counters["total_tasks"] += count
    status_value = str(status or "").lower()
    if status_value == "done":
        counters["completed_tasks"] += count
    elif status_value == "doing":
        counters["in_progress_tasks"] += count
    elif status_value == "blocked":
        counters["blocked_tasks"] += count
    elif status_value == "review":
        counters["in_review_tasks"] += count


def _finalize_task_progress(counters: dict[str, int]) -> dict[str, Any]:
    total = counters["total_tasks"]
    completed = counters["completed_tasks"]
    return {
        **counters,
        "completion_pct": round((completed / total * 100) if total > 0 else 0, 1),
    }


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


def relationship_from_surreal_row(row: Mapping[str, object]) -> Relationship:
    normalized_row = {str(key): value for key, value in row.items()}
    attributes = _row_attributes(normalized_row)
    metadata = dict(attributes)
    raw_metadata = metadata.get("metadata", normalized_row.get("metadata"))
    if isinstance(raw_metadata, str):
        try:
            parsed = json.loads(raw_metadata)
        except json.JSONDecodeError:
            metadata.pop("metadata", None)
        else:
            metadata.pop("metadata", None)
            if isinstance(parsed, dict):
                metadata = {str(key): value for key, value in parsed.items()} | metadata
    elif isinstance(raw_metadata, Mapping):
        metadata.pop("metadata", None)
        metadata = {str(key): value for key, value in raw_metadata.items()} | metadata

    source_id, source_key = _relationship_endpoint(normalized_row, "source")
    target_id, _target_key = _relationship_endpoint(normalized_row, "target")
    for key in (
        "fact",
        "fact_embedding",
        "group_id",
        "project_id",
        "source_id",
        "source_ids",
        "confidence",
        "valid_at",
        "valid_from",
        "valid_to",
        "invalid_at",
        "expired_at",
        "created_by",
        "modified_by",
        "direction",
        "episodes",
    ):
        if key == "source_id" and source_key == "source_id":
            continue
        value = normalized_row.get(key)
        if value is not None and metadata.get(key) is None:
            if key == "fact_embedding":
                if vector := _metadata_float_list(value):
                    metadata[key] = vector
            else:
                metadata[key] = value

    relationship_id = _relationship_id_from_row(normalized_row)
    record_id = _row_record_id(normalized_row)
    if record_id and record_id != relationship_id and metadata.get("record_id") is None:
        metadata["record_id"] = record_id

    return Relationship(
        id=relationship_id,
        relationship_type=_relationship_type_from_row(normalized_row, metadata=metadata),
        source_id=source_id,
        target_id=target_id,
        weight=_metadata_weight(metadata),
        metadata=metadata,
        created_at=_row_datetime(normalized_row.get("created_at")) or datetime.now(UTC),
    )


def _relationship_from_row(row: SurrealRecord) -> Relationship:
    return relationship_from_surreal_row(row)


def _relationship_id_from_row(row: Mapping[str, object]) -> str:
    for key in ("uuid", "relationship_id"):
        if text := _first_text(row.get(key)):
            return text
    raw_id = row.get("id")
    if raw_id is None:
        raw_id = row.get("record_id")
    if text := _first_text(raw_id):
        return _entity_id_from_record_text(text)
    return ""


def _relationship_endpoint(row: Mapping[str, object], side: str) -> tuple[str, str | None]:
    for key in (
        f"{side}_uuid",
        f"{side}_node_uuid",
        f"{side}_id",
    ):
        if text := _first_text(row.get(key)):
            return text, key
    return "", None


def _relationship_type_from_row(
    row: Mapping[str, object],
    *,
    metadata: Mapping[str, object] | None = None,
) -> RelationshipType:
    relationship_metadata = metadata or {}
    value = str(
        row.get("name")
        or row.get("relationship_type")
        or row.get("rel_type")
        or relationship_metadata.get("relationship_type")
        or RelationshipType.RELATED_TO.value
    )
    try:
        return RelationshipType(value)
    except ValueError:
        return RelationshipType.RELATED_TO


def _metadata_weight(metadata: Mapping[str, object]) -> float:
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
    raw_id = payload.pop("id", None)
    if raw_id is not None and payload.get("record_id") is None:
        payload["record_id"] = raw_id
    if (
        raw_id is not None
        and payload.get("uuid") is None
        and payload.get("entity_id") is None
        and (text_id := _first_text(raw_id))
        and ":" not in text_id
    ):
        payload["uuid"] = text_id
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


async def _entity_with_native_embedding(
    entity: Entity,
    provider: NativeEmbeddingProvider | None,
) -> Entity:
    if provider is None or entity.embedding:
        return entity
    embeddings = await provider.embed_texts(
        [native_entity_embedding_text(entity)],
        input_kind="document",
    )
    embedding = _embedding_vector_from_batch(embeddings, provider.metadata.dimensions)
    metadata = {
        **dict(entity.metadata or {}),
        "embedding_metadata": provider.metadata.to_dict(),
    }
    return entity.model_copy(update={"embedding": embedding, "metadata": metadata})


async def _relationship_with_native_embedding(
    relationship: Relationship,
    provider: NativeEmbeddingProvider | None,
) -> Relationship:
    metadata = dict(relationship.metadata or {})
    if provider is None or _metadata_float_list(metadata.get("fact_embedding")):
        return relationship
    embeddings = await provider.embed_texts(
        [native_relationship_embedding_text(relationship)],
        input_kind="document",
    )
    metadata["fact_embedding"] = _embedding_vector_from_batch(
        embeddings,
        provider.metadata.dimensions,
    )
    metadata["embedding_metadata"] = provider.metadata.to_dict()
    return relationship.model_copy(update={"metadata": metadata})


def _embedding_vector_from_batch(
    embeddings: Sequence[Sequence[float]],
    dimensions: int,
) -> list[float]:
    if not embeddings:
        raise ValueError("embedding provider returned no vectors")
    embedding = [float(value) for value in embeddings[0]]
    if len(embedding) != dimensions:
        raise ValueError(
            f"embedding provider returned {len(embedding)} dimensions, expected {dimensions}"
        )
    return embedding


async def _replace_entity(
    client: NativeSurrealGraphClient,
    entity: Entity,
    *,
    group_id: str,
) -> SurrealRecord:
    record = _entity_record(entity, group_id=group_id)
    try:
        result = await _execute_replace_entity_query(client, record)
    except Exception as exc:
        if not _is_transient_connection_error(exc):
            raise
        _prepared_groups.discard(client.group_id)
        await prepare_native_graph_schema(client)
        result = await _execute_replace_entity_query(client, record)
    rows = normalize_records(
        result
    )
    if rows:
        return rows[0]
    stored = await _select_one(
        client, "SELECT * FROM entity WHERE uuid = $uuid LIMIT 1;", uuid=entity.id
    )
    if stored is None:
        raise RuntimeError(f"failed to persist entity {entity.id}")
    return stored


async def _execute_replace_entity_query(
    client: NativeSurrealGraphClient,
    record: SurrealRecord,
) -> object:
    return await client.execute_query(
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
            source_id = $source_id,
            target_id = $target_id,
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
                source_id = $source_id,
                target_id = $target_id,
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
    fact_embedding = _metadata_float_list(
        metadata.get("fact_embedding") or metadata.get("embedding")
    )
    attributes = {
        key: value for key, value in metadata.items() if key not in {"fact_embedding", "embedding"}
    }
    return {
        "uuid": relationship.id,
        "name": relationship.relationship_type.value,
        "fact": fact,
        "fact_embedding": fact_embedding,
        "group_id": group_id,
        "source_id": relationship.source_id,
        "target_id": relationship.target_id,
        "episodes": _metadata_str_list(metadata.get("episodes")),
        "attributes": attributes,
        "created_at": relationship.created_at,
        "expired_at": _metadata_datetime(metadata.get("expired_at")),
        "valid_at": _metadata_datetime(metadata.get("valid_at") or metadata.get("valid_from")),
        "invalid_at": _metadata_datetime(metadata.get("invalid_at") or metadata.get("valid_to")),
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


def _metadata_float_list(value: object) -> list[float] | None:
    if not isinstance(value, Iterable) or isinstance(value, str | bytes | dict):
        return None
    vector: list[float] = []
    for item in value:
        if isinstance(item, int | float | str):
            try:
                vector.append(float(item))
            except ValueError:
                return None
        else:
            return None
    return vector


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
    "entity_from_surreal_row",
    "get_native_graph_client",
    "get_native_graph_runtime",
    "normalize_records",
    "prepare_native_graph_schema",
    "relationship_from_surreal_row",
]
