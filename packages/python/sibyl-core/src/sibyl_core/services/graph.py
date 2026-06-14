"""SurrealDB graph helpers for native memory paths."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import Any, cast

import structlog
from surrealdb import RecordID

from sibyl_core.backends.surreal.connection import _is_transient_connection_error
from sibyl_core.backends.surreal.fulltext import build_fulltext_query
from sibyl_core.backends.surreal.records import raise_on_error
from sibyl_core.backends.surreal.schema import bootstrap_schema
from sibyl_core.config import settings
from sibyl_core.embeddings.providers import (
    EmbeddingInputKind,
    EmbeddingProvider,
    entity_embedding_text,
    relationship_embedding_text,
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
    Epic,
    Task,
    TaskComplexity,
    TaskPriority,
    TaskStatus,
)
from sibyl_core.services import graph_client as _graph_client
from sibyl_core.services.graph_search import (
    bounded_similarity_score as _bounded_similarity_score,
)
from sibyl_core.services.graph_search import (
    count_task_status as _count_task_status,
)
from sibyl_core.services.graph_search import (
    entity_matches_list_filters as _entity_matches_list_filters,
)
from sibyl_core.services.graph_search import (
    finalize_task_progress as _finalize_task_progress,
)
from sibyl_core.services.graph_search import (
    lower_filter_values as _lower_filter_values,
)
from sibyl_core.services.graph_search import (
    lower_sequence_values as _lower_sequence_values,
)
from sibyl_core.services.graph_search import (
    merge_ranked_entity_results as _merge_ranked_entity_results,
)
from sibyl_core.services.graph_search import (
    metadata_scalar as _metadata_scalar,
)
from sibyl_core.services.graph_search import (
    new_task_progress as _new_task_progress,
)
from sibyl_core.services.graph_search import (
    normalize_search_text as _normalize_search_text,
)
from sibyl_core.services.graph_search import (
    row_score as _row_score,
)
from sibyl_core.services.graph_search import (
    task_priority_rank as _task_priority_rank,
)

SurrealGraphClient = _graph_client.SurrealGraphClient
_clients = _graph_client._clients
_prepared_groups = _graph_client._prepared_groups

type SurrealRecord = dict[str, object]

_ENTITY_LIST_FIELDS = "* OMIT content, embedding, name_embedding, attributes.content"
_RELATED_ENTITY_PROJECTION_FIELDS = (
    ("id", "record_id"),
    ("uuid", "uuid"),
    ("name", "name"),
    ("entity_type", "entity_type"),
    ("summary", "summary"),
    ("description", "description"),
    ("labels", "labels"),
    ("attributes", "attributes"),
    ("group_id", "group_id"),
    ("created_at", "created_at"),
    ("updated_at", "updated_at"),
    ("project_id", "project_id"),
    ("epic_id", "epic_id"),
    ("parent_task_id", "parent_task_id"),
    ("task_id", "task_id"),
    ("status", "status"),
    ("priority", "priority"),
    ("complexity", "complexity"),
    ("feature", "feature"),
    ("tags", "tags"),
    ("source_id", "source_id"),
    ("source_ids", "source_ids"),
    ("confidence", "confidence"),
    ("valid_at", "valid_at"),
    ("valid_from", "valid_from"),
    ("valid_to", "valid_to"),
    ("invalid_at", "invalid_at"),
    ("created_by", "created_by"),
    ("modified_by", "modified_by"),
    ("source_file", "source_file"),
)
_ENTITY_SEARCH_PROJECTION_FIELDS = (
    *_RELATED_ENTITY_PROJECTION_FIELDS,
    ("content", "content"),
)
_ENTITY_SEARCH_FIELDS = ",\n                       ".join(
    f"{field_name} AS {alias}" if field_name != alias else field_name
    for field_name, alias in _ENTITY_SEARCH_PROJECTION_FIELDS
)
_ENTITY_BULK_UPSERT_QUERY = """
INSERT INTO entity $rows ON DUPLICATE KEY UPDATE
    uuid = $input.uuid,
    name = $input.name,
    entity_type = $input.entity_type,
    summary = $input.summary,
    description = $input.description,
    content = $input.content,
    labels = $input.labels,
    attributes = $input.attributes,
    group_id = $input.group_id,
    created_at = $input.created_at,
    updated_at = $input.updated_at,
    project_id = $input.project_id,
    epic_id = $input.epic_id,
    parent_task_id = $input.parent_task_id,
    task_id = $input.task_id,
    status = $input.status,
    priority = $input.priority,
    complexity = $input.complexity,
    feature = $input.feature,
    tags = $input.tags,
    name_embedding = $input.name_embedding;
"""
_RELATIONSHIP_BULK_UPSERT_QUERY = """
FOR $row IN $rows {
    LET $src = $row.src;
    LET $tgt = $row.tgt;
    DELETE FROM relates_to WHERE uuid = $row.uuid AND (in != $src OR out != $tgt);
    LET $updated = (UPDATE relates_to SET
        in = $src,
        out = $tgt,
        uuid = $row.uuid,
        name = $row.name,
        fact = $row.fact,
        fact_embedding = $row.fact_embedding,
        group_id = $row.group_id,
        source_id = $row.source_id,
        target_id = $row.target_id,
        episodes = $row.episodes ?? [],
        attributes = $row.attributes ?? {},
        created_at = $row.created_at,
        expired_at = $row.expired_at,
        valid_at = $row.valid_at,
        invalid_at = $row.invalid_at
    WHERE uuid = $row.uuid RETURN id);
    IF array::len($updated) = 0 THEN
        RELATE $src->relates_to->$tgt SET
            uuid = $row.uuid,
            name = $row.name,
            fact = $row.fact,
            fact_embedding = $row.fact_embedding,
            group_id = $row.group_id,
            source_id = $row.source_id,
            target_id = $row.target_id,
            episodes = $row.episodes ?? [],
            attributes = $row.attributes ?? {},
            created_at = $row.created_at,
            expired_at = $row.expired_at,
            valid_at = $row.valid_at,
            invalid_at = $row.invalid_at
    END;
};
"""
log = structlog.get_logger()


@dataclass(frozen=True, slots=True)
class GraphRuntime:
    client: SurrealGraphClient
    entity_manager: EntityManager
    relationship_manager: RelationshipManager


class EntityManager:
    supports_bounded_entity_list = True
    supports_lightweight_entity_list = True

    def __init__(
        self,
        client: SurrealGraphClient,
        *,
        group_id: str,
        embedding_provider: EmbeddingProvider | None = None,
    ) -> None:
        self._client = client
        self._group_id = group_id
        self._embedding_provider = embedding_provider

    async def create_direct(self, entity: Entity, *, generate_embedding: bool = False) -> str:
        if generate_embedding:
            entity = await _entity_with_native_embedding(entity, self._embedding_provider)
        await _replace_entity(self._client, entity, group_id=self._group_id)
        return entity.id

    async def create_direct_bulk(
        self,
        entities: Sequence[Entity],
        *,
        generate_embeddings: bool = False,
        embedding_batch_size: int = 64,
        write_batch_size: int = 128,
    ) -> list[str]:
        prepared_entities = await self.prepare_entities_for_write(
            entities,
            generate_embeddings=generate_embeddings,
            embedding_batch_size=embedding_batch_size,
        )
        if not prepared_entities:
            return []

        created_ids: list[str] = []
        batch_size = max(int(write_batch_size), 1)
        for index in range(0, len(prepared_entities), batch_size):
            batch = prepared_entities[index : index + batch_size]
            await _replace_entities_bulk(self._client, batch, group_id=self._group_id)
            created_ids.extend(entity.id for entity in batch)
        return created_ids

    async def prepare_entities_for_write(
        self,
        entities: Sequence[Entity],
        *,
        generate_embeddings: bool = False,
        embedding_batch_size: int = 64,
    ) -> list[Entity]:
        prepared_entities = list(entities)
        if generate_embeddings:
            prepared_entities = await _entities_with_native_embeddings(
                prepared_entities,
                self._embedding_provider,
                batch_size=embedding_batch_size,
            )
        return prepared_entities

    async def create(self, entity: Entity) -> str:
        return await self.create_direct(entity, generate_embedding=True)

    async def delete(self, entity_id: str) -> bool:
        rows = await _execute_graph_transaction(
            self._client,
            """
            BEGIN TRANSACTION;
            DELETE FROM relates_to
            WHERE group_id = $group_id
              AND (source_id = $uuid OR target_id = $uuid)
            RETURN BEFORE;
            DELETE FROM mentions
            WHERE group_id = $group_id
              AND (source_id = $uuid OR target_id = $uuid)
            RETURN BEFORE;
            DELETE FROM entity
            WHERE group_id = $group_id AND uuid = $uuid
            RETURN BEFORE;
            COMMIT TRANSACTION;
            """,
            group_id=self._group_id,
            uuid=entity_id,
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

        patch = _entity_update_patch(updates, updated_at=datetime.now(UTC))
        rows = await _execute_graph_transaction(
            self._client,
            """
            BEGIN TRANSACTION;
            UPDATE entity MERGE $patch
            WHERE group_id = $group_id AND uuid = $uuid
            RETURN NONE;
            UPDATE entity SET
                summary = IF description != NONE AND description != '' THEN
                    string::slice(description, 0, 500)
                ELSE
                    name
                END
            WHERE group_id = $group_id AND uuid = $uuid
            RETURN NONE;
            SELECT *
            FROM entity
            WHERE group_id = $group_id AND uuid = $uuid
            LIMIT 1;
            COMMIT TRANSACTION;
            """,
            group_id=self._group_id,
            uuid=entity_id,
            patch=patch,
        )
        if not rows:
            return None
        return _entity_from_row(rows[0])

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
        result_limit = max(int(limit), 1)

        fulltext_results, vector_results = await asyncio.gather(
            self._fulltext_search(
                query=query,
                search_query=search_query,
                entity_types=entity_types,
                limit=result_limit,
            ),
            self._vector_search(
                query=query,
                entity_types=entity_types,
                limit=result_limit,
            ),
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

    async def _fulltext_search(
        self,
        *,
        query: str,
        search_query: str,
        entity_types: Sequence[EntityType] | None,
        limit: int,
    ) -> list[tuple[Entity, float]]:
        type_values = [entity_type.value for entity_type in entity_types or ()]
        type_clause = "AND entity_type IN $entity_types" if type_values else ""
        rows = normalize_records(
            await self._client.execute_query(
                "SELECT "
                + _ENTITY_SEARCH_FIELDS
                + """,
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
                limit=limit,
                _query_label="entity.search.fulltext",
            )
        )
        fulltext_results: list[tuple[Entity, float]] = []
        for row in rows:
            entity = _entity_from_row(row)
            fulltext_results.append((entity, _bounded_similarity_score(query, entity)))
        return fulltext_results

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
        knn_effort = max(1, int(settings.graph_knn_ef))
        try:
            embeddings = await _embed_texts_with_timeout(
                self._embedding_provider,
                [query],
                input_kind="query",
                operation="entity_vector_search",
                timeout_seconds=settings.graph_search_embedding_timeout_seconds,
            )
            query_embedding = _embedding_vector_from_batch(
                embeddings,
                self._embedding_provider.metadata.dimensions,
            )
            rows = normalize_records(
                await self._client.execute_query(
                    "SELECT *"
                    " FROM ("
                    "SELECT "
                    + _ENTITY_SEARCH_FIELDS
                    + """,
                               (1 - vector::distance::knn()) AS score
                        FROM entity
                        WHERE group_id = $group_id
                    """
                    + type_clause
                    + f"""
                          AND name_embedding <|{candidate_limit}, {knn_effort}|> $query_embedding
                    )
                    ORDER BY score DESC, created_at DESC, uuid DESC
                    LIMIT $limit;
                    """,
                    group_id=self._group_id,
                    query_embedding=query_embedding,
                    entity_types=type_values,
                    limit=candidate_limit,
                    _query_label="entity.search.vector",
                )
            )
        except Exception as exc:
            log.warning(
                "entity_vector_search_failed",
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
                "SELECT "
                + _ENTITY_SEARCH_FIELDS
                + """
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
                "SELECT "
                + _ENTITY_SEARCH_FIELDS
                + """
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

    async def list_subtasks(
        self,
        parent_task_id: str,
        *,
        status: str | None = None,
        limit: int = 100,
        include_archived: bool = True,
    ) -> list[Entity]:
        """List the child tasks of a parent task (a task with children is an epic)."""
        return await self.list_by_type(
            EntityType.TASK,
            parent_task_id=parent_task_id,
            status=status,
            limit=limit,
            include_archived=include_archived,
        )

    async def derive_epic_from_task(self, parent_task_id: str) -> Epic | None:
        """View a task-with-children as an epic, status derived from its subtasks.

        Returns ``None`` when the parent task does not exist. This is a read-only
        projection (W14): it never writes, leaves the stored Epic entity and
        ``epic_id`` untouched, and reuses the U1 subtask query for the children.
        """
        try:
            parent = await self.get(parent_task_id)
        except KeyError:
            return None
        children = await self.list_subtasks(parent_task_id)
        return Epic.derived_from_task(
            _entity_to_task(parent),
            [_entity_to_task(child) for child in children],
        )

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
            epic_ref = metadata.get("parent_task_id") or metadata.get("epic_id")

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
            if is_critical:
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
        critical_tasks = sorted(critical_tasks, key=_task_priority_rank)[:critical_limit]
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
        parent_task_id: str | None = None,
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
                parent_task_id is not None,
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
            where_clauses.append(
                "("
                + _surreal_indexed_field_equals_or_missing("parent_task_id")
                + " OR "
                + _surreal_indexed_field_equals_or_missing("epic_id")
                + ")"
            )
            query_params["epic_id"] = epic_id
            query_params["parent_task_id"] = epic_id
        if no_epic:
            where_clauses.append(
                "("
                + _surreal_indexed_field_missing("parent_task_id")
                + " AND "
                + _surreal_indexed_field_missing("epic_id")
                + ")"
            )
        if parent_task_id is not None:
            where_clauses.append(_surreal_indexed_field_equals_or_missing("parent_task_id"))
            query_params["parent_task_id"] = parent_task_id
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
                    parent_task_id=parent_task_id,
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
            "parent_task_id IN $epic_ids",
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
                SELECT parent_task_id AS epic_id, status, count() AS task_count
                FROM entity
                WHERE """
                + " AND ".join(where_clauses)
                + """
                GROUP BY parent_task_id, status;
                """,
                **params,
            )
        )
        legacy_where_clauses = [
            "group_id = $group_id",
            "entity_type = 'task'",
            _surreal_indexed_field_missing("parent_task_id"),
            "(attributes.parent_task_id IN $epic_ids OR attributes.epic_id IN $epic_ids)",
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


class RelationshipManager:
    def __init__(
        self,
        client: SurrealGraphClient,
        *,
        group_id: str,
        embedding_provider: EmbeddingProvider | None = None,
    ) -> None:
        self._client = client
        self._group_id = group_id
        self._embedding_provider = embedding_provider

    async def create_bulk(self, relationships: Sequence[Relationship]) -> tuple[int, int]:
        prepared = list(relationships)
        if not prepared:
            return 0, 0
        try:
            created_ids = await self.create_direct_bulk(prepared, generate_embeddings=True)
        except Exception:
            return 0, len(prepared)
        created = len(created_ids)
        return created, len(prepared) - created

    async def create_direct_bulk(
        self,
        relationships: Sequence[Relationship],
        *,
        generate_embeddings: bool = False,
        embedding_batch_size: int = 64,
        write_batch_size: int = 128,
    ) -> list[str]:
        prepared = list(relationships)
        if not prepared:
            return []
        if generate_embeddings:
            prepared = await _relationships_with_native_embeddings(
                prepared,
                self._embedding_provider,
                batch_size=embedding_batch_size,
            )

        created_ids: list[str] = []
        batch_size = max(int(write_batch_size), 1)
        for index in range(0, len(prepared), batch_size):
            batch = prepared[index : index + batch_size]
            written = await _replace_relationships_bulk(
                self._client, batch, group_id=self._group_id
            )
            created_ids.extend(written)
        return created_ids

    async def create(self, relationship: Relationship) -> str:
        relationship = await _relationship_with_native_embedding(
            relationship,
            self._embedding_provider,
        )
        await _replace_relationship(self._client, relationship, group_id=self._group_id)
        return relationship.id

    async def delete(self, relationship_id: str) -> bool:
        rows = await _execute_graph_transaction(
            self._client,
            """
            BEGIN TRANSACTION;
            DELETE FROM relates_to
            WHERE group_id = $group_id AND uuid = $uuid
            RETURN BEFORE;
            DELETE FROM mentions
            WHERE group_id = $group_id AND uuid = $uuid
            RETURN BEFORE;
            COMMIT TRANSACTION;
            """,
            group_id=self._group_id,
            uuid=relationship_id,
        )
        return any(row.get("uuid") == relationship_id for row in rows)

    async def delete_bulk(self, relationship_ids: Sequence[str]) -> int:
        unique_ids = list(
            dict.fromkeys(
                relationship_id for relationship_id in relationship_ids if relationship_id
            )
        )
        if not unique_ids:
            return 0
        rows = await _execute_graph_transaction(
            self._client,
            """
            BEGIN TRANSACTION;
            DELETE FROM relates_to
            WHERE group_id = $group_id AND uuid IN $uuids
            RETURN BEFORE;
            DELETE FROM mentions
            WHERE group_id = $group_id AND uuid IN $uuids
            RETURN BEFORE;
            COMMIT TRANSACTION;
            """,
            group_id=self._group_id,
            uuids=unique_ids,
        )
        deleted = {str(row.get("uuid")) for row in rows if row.get("uuid") is not None}
        return len(deleted & set(unique_ids))

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
                   source_id AS source_uuid,
                   target_id AS target_uuid
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
            direction_clause = " AND source_id = $entity_id"
        elif direction == "incoming":
            direction_clause = " AND target_id = $entity_id"
        else:
            direction_clause = """
                AND (
                    source_id = $entity_id
                    OR target_id = $entity_id
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
                       source_id AS source_uuid,
                       target_id AS target_uuid
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
        related_rows = await self._get_native_related_entity_rows(
            seed_ids,
            type_clause=type_clause,
            type_values=type_values,
            limit=per_seed_limit,
        )

        results: dict[str, list[tuple[Entity, Relationship]]] = {
            seed_id: [] for seed_id in seed_ids
        }
        seen_by_seed: dict[str, set[tuple[str, object]]] = {seed_id: set() for seed_id in seed_ids}
        for row in related_rows:
            seed_id = row.get("seed_uuid")
            if not isinstance(seed_id, str) or seed_id not in results:
                continue
            seed_results = results[seed_id]
            if len(seed_results) >= per_seed_limit:
                continue
            entity = _related_entity_from_row(row)
            if entity is None:
                continue
            relationship = _relationship_from_row(row)
            key = (relationship.id, row.get("direction"))
            if key in seen_by_seed[seed_id]:
                continue
            seen_by_seed[seed_id].add(key)
            seed_results.append((entity, relationship))
        return results

    async def _get_native_related_entity_rows(
        self,
        seed_ids: Sequence[str],
        *,
        type_clause: str,
        type_values: Sequence[str],
        limit: int,
    ) -> list[SurrealRecord]:
        outgoing_rows = await self._get_native_related_entity_direction_rows(
            seed_ids,
            endpoint_field="source_id",
            endpoint_alias="source_uuid",
            related_side="out",
            direction="outgoing",
            type_clause=type_clause,
            type_values=type_values,
            limit=limit,
        )
        incoming_rows = await self._get_native_related_entity_direction_rows(
            seed_ids,
            endpoint_field="target_id",
            endpoint_alias="target_uuid",
            related_side="in",
            direction="incoming",
            type_clause=type_clause,
            type_values=type_values,
            limit=limit,
        )

        rows: list[SurrealRecord] = []
        seen: set[str] = set()
        for row in [*outgoing_rows, *incoming_rows]:
            key = ":".join(
                str(value)
                for value in (
                    row.get("seed_uuid"),
                    row.get("direction"),
                    row.get("uuid") or row.get("record_id") or id(row),
                )
            )
            if key in seen:
                continue
            seen.add(key)
            rows.append(row)
        return rows

    async def _get_native_related_entity_direction_rows(
        self,
        seed_ids: Sequence[str],
        *,
        endpoint_field: str,
        endpoint_alias: str,
        related_side: str,
        direction: str,
        type_clause: str,
        type_values: Sequence[str],
        limit: int,
    ) -> list[SurrealRecord]:
        batch_limit = min(max(limit * len(seed_ids), limit), 5000)
        rows = normalize_records(
            await self._client.execute_query(
                f"""
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
                       source_id AS source_uuid,
                       target_id AS target_uuid,
                       {endpoint_field} AS seed_uuid,
                       {_related_entity_projection(related_side)}
                FROM relates_to
                WHERE group_id = $group_id
                  AND {endpoint_field} IN $entity_ids
                  AND {related_side}.group_id = $group_id
                """
                + type_clause
                + """
                ORDER BY created_at DESC, uuid DESC
                LIMIT $limit;
                """,
                group_id=self._group_id,
                entity_ids=list(seed_ids),
                relationship_types=type_values,
                limit=batch_limit,
            )
        )
        for row in rows:
            row["direction"] = direction
            row.setdefault("seed_uuid", row.get(endpoint_alias))
        if len(rows) < batch_limit:
            return rows

        counts: dict[str, int] = {}
        for row in rows:
            endpoint_id = row.get(endpoint_alias)
            if isinstance(endpoint_id, str):
                counts[endpoint_id] = counts.get(endpoint_id, 0) + 1

        for seed_id in seed_ids:
            if counts.get(seed_id, 0) >= limit:
                continue
            rows.extend(
                normalize_records(
                    await self._client.execute_query(
                        f"""
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
                               source_id AS source_uuid,
                               target_id AS target_uuid,
                               {endpoint_field} AS seed_uuid,
                               {_related_entity_projection(related_side)}
                        FROM relates_to
                        WHERE group_id = $group_id
                          AND {endpoint_field} = $entity_id
                          AND {related_side}.group_id = $group_id
                        """
                        + type_clause
                        + """
                        ORDER BY created_at DESC, uuid DESC
                        LIMIT $limit;
                        """,
                        group_id=self._group_id,
                        entity_id=seed_id,
                        relationship_types=type_values,
                        limit=limit,
                    )
                )
            )
        for row in rows:
            row["direction"] = direction
            row.setdefault("seed_uuid", row.get(endpoint_alias))
        return rows

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
                       source_id AS source_uuid,
                       target_id AS target_uuid
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
                       source_id AS source_uuid,
                       target_id AS target_uuid
                FROM relates_to
                WHERE group_id = $group_id
                  AND (
                    (source_id = $source_id AND target_id = $target_id)
                    OR (source_id = $target_id AND target_id = $source_id)
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


def _validate_native_embedding_dimensions(
    embedding_provider: EmbeddingProvider | None,
) -> None:
    _graph_client.validate_native_embedding_dimensions(embedding_provider)


async def get_surreal_graph_client(group_id: str) -> SurrealGraphClient:
    # Compatibility callers patch graph.SurrealGraphClient, not graph_client.
    original_client_type = _graph_client.SurrealGraphClient
    _graph_client.SurrealGraphClient = SurrealGraphClient
    try:
        return await _graph_client.get_surreal_graph_client(group_id)
    finally:
        _graph_client.SurrealGraphClient = original_client_type


async def close_graph_clients() -> None:
    await _graph_client.close_graph_clients()


async def prepare_graph_schema(client: SurrealGraphClient) -> None:
    # Compatibility callers patch graph.bootstrap_schema, not graph_client.
    original_bootstrap_schema = _graph_client.bootstrap_schema
    _graph_client.bootstrap_schema = bootstrap_schema
    try:
        await _graph_client.prepare_graph_schema(client)
    finally:
        _graph_client.bootstrap_schema = original_bootstrap_schema


def mark_graph_schema_dirty(group_id: str) -> None:
    _graph_client.mark_graph_schema_dirty(group_id)


async def get_surreal_graph_runtime(
    group_id: str,
    *,
    embedding_provider: EmbeddingProvider | None = None,
    ensure_schema: bool = True,
) -> GraphRuntime:
    client = await get_surreal_graph_client(group_id)
    if ensure_schema:
        await prepare_graph_schema(client)
    _validate_native_embedding_dimensions(embedding_provider)
    return GraphRuntime(
        client=client,
        entity_manager=EntityManager(
            client,
            group_id=group_id,
            embedding_provider=embedding_provider,
        ),
        relationship_manager=RelationshipManager(
            client,
            group_id=group_id,
            embedding_provider=embedding_provider,
        ),
    )


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
        "parent_task_id",
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


def _related_entity_projection(side: str) -> str:
    return ",\n                       ".join(
        f"{side}.{field_name} AS related_{alias}"
        for field_name, alias in _RELATED_ENTITY_PROJECTION_FIELDS
    )


def _related_entity_from_row(row: Mapping[str, object]) -> Entity | None:
    related_row = {
        key.removeprefix("related_"): value
        for key, value in row.items()
        if key.startswith("related_")
    }
    if not _first_text(related_row.get("uuid")):
        return None
    return entity_from_surreal_row(related_row)


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
        parent_task_id=_optional_text(meta.get("parent_task_id")),
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


def _surreal_indexed_field_missing(field: str) -> str:
    return f"({field} IS NONE OR {field} = '')"


def _surreal_indexed_field_equals_or_missing(field: str) -> str:
    return f"({field} = ${field} OR {_surreal_indexed_field_missing(field)})"


def _surreal_indexed_field_in_or_missing(field: str, param: str) -> str:
    return f"({field} IN ${param} OR {_surreal_indexed_field_missing(field)})"


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
        if (
            "result" in payload
            and "status" not in payload
            and isinstance(payload.get("result"), list)
        ):
            return normalize_records(payload["result"])
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
    client: SurrealGraphClient,
    query: str,
    **params: object,
) -> SurrealRecord | None:
    rows = normalize_records(await client.execute_query(query, **params))
    return rows[0] if rows else None


async def _execute_graph_transaction(
    client: SurrealGraphClient,
    query: str,
    **params: object,
) -> list[SurrealRecord]:
    execute_query_raw = getattr(client, "execute_query_raw", None)
    if callable(execute_query_raw):
        result = await cast("Any", execute_query_raw)(query, **params)
    else:
        result = await client.execute_query(query, **params)
    raise_on_error(result, query=query)
    return normalize_records(result)


async def _entity_with_native_embedding(
    entity: Entity,
    provider: EmbeddingProvider | None,
) -> Entity:
    if provider is None or entity.embedding:
        return entity
    embeddings = await _embed_texts_for_write(
        provider,
        [entity_embedding_text(entity)],
        operation="entity_create",
    )
    if embeddings is None:
        return entity
    embedding = _embedding_vector_from_batch(embeddings, provider.metadata.dimensions)
    metadata = {
        **dict(entity.metadata or {}),
        "embedding_metadata": provider.metadata.to_dict(),
    }
    return entity.model_copy(update={"embedding": embedding, "metadata": metadata})


async def _entities_with_native_embeddings(
    entities: Sequence[Entity],
    provider: EmbeddingProvider | None,
    *,
    batch_size: int,
) -> list[Entity]:
    if provider is None:
        return list(entities)

    updated_entities = list(entities)
    pending_indexes = [
        index for index, entity in enumerate(updated_entities) if not entity.embedding
    ]
    if not pending_indexes:
        return updated_entities

    dimensions = provider.metadata.dimensions
    for start in range(0, len(pending_indexes), max(int(batch_size), 1)):
        batch_indexes = pending_indexes[start : start + max(int(batch_size), 1)]
        embeddings = await _embed_texts_for_write(
            provider,
            [entity_embedding_text(updated_entities[index]) for index in batch_indexes],
            operation="entity_bulk_create",
        )
        if embeddings is None:
            continue
        if len(embeddings) != len(batch_indexes):
            raise ValueError(
                "embedding provider returned "
                f"{len(embeddings)} vectors for {len(batch_indexes)} entities"
            )
        for index, embedding_values in zip(batch_indexes, embeddings, strict=True):
            embedding = _embedding_vector_from_batch([embedding_values], dimensions)
            entity = updated_entities[index]
            metadata = {
                **dict(entity.metadata or {}),
                "embedding_metadata": provider.metadata.to_dict(),
            }
            updated_entities[index] = entity.model_copy(
                update={"embedding": embedding, "metadata": metadata}
            )

    return updated_entities


async def _relationship_with_native_embedding(
    relationship: Relationship,
    provider: EmbeddingProvider | None,
) -> Relationship:
    metadata = dict(relationship.metadata or {})
    if provider is None or _metadata_float_list(metadata.get("fact_embedding")):
        return relationship
    embeddings = await _embed_texts_for_write(
        provider,
        [relationship_embedding_text(relationship)],
        operation="relationship_create",
    )
    if embeddings is None:
        return relationship
    metadata["fact_embedding"] = _embedding_vector_from_batch(
        embeddings,
        provider.metadata.dimensions,
    )
    metadata["embedding_metadata"] = provider.metadata.to_dict()
    return relationship.model_copy(update={"metadata": metadata})


async def _relationships_with_native_embeddings(
    relationships: Sequence[Relationship],
    provider: EmbeddingProvider | None,
    *,
    batch_size: int,
) -> list[Relationship]:
    if provider is None:
        return list(relationships)

    updated = list(relationships)
    pending_indexes = [
        index
        for index, relationship in enumerate(updated)
        if not _metadata_float_list(dict(relationship.metadata or {}).get("fact_embedding"))
    ]
    if not pending_indexes:
        return updated

    dimensions = provider.metadata.dimensions
    for start in range(0, len(pending_indexes), max(int(batch_size), 1)):
        batch_indexes = pending_indexes[start : start + max(int(batch_size), 1)]
        embeddings = await _embed_texts_for_write(
            provider,
            [relationship_embedding_text(updated[index]) for index in batch_indexes],
            operation="relationship_bulk_create",
        )
        if embeddings is None:
            continue
        if len(embeddings) != len(batch_indexes):
            raise ValueError(
                "embedding provider returned "
                f"{len(embeddings)} vectors for {len(batch_indexes)} relationships"
            )
        for index, embedding_values in zip(batch_indexes, embeddings, strict=True):
            relationship = updated[index]
            metadata = dict(relationship.metadata or {})
            metadata["fact_embedding"] = _embedding_vector_from_batch(
                [embedding_values], dimensions
            )
            metadata["embedding_metadata"] = provider.metadata.to_dict()
            updated[index] = relationship.model_copy(update={"metadata": metadata})

    return updated


async def _embed_texts_for_write(
    provider: EmbeddingProvider,
    texts: Sequence[str],
    *,
    operation: str,
) -> list[list[float]] | None:
    started = time.perf_counter()
    try:
        return await _embed_texts_with_timeout(
            provider,
            texts,
            input_kind="document",
            operation=operation,
        )
    except Exception as exc:
        log.warning(
            "graph_embedding_failed",
            operation=operation,
            provider=provider.metadata.provider,
            model=provider.metadata.model,
            items=len(texts),
            timeout_seconds=settings.graph_embedding_timeout_seconds,
            elapsed_ms=round((time.perf_counter() - started) * 1000, 2),
            error_type=type(exc).__name__,
        )
        return None


async def _embed_texts_with_timeout(
    provider: EmbeddingProvider,
    texts: Sequence[str],
    *,
    input_kind: EmbeddingInputKind,
    operation: str,
    timeout_seconds: float | None = None,
) -> list[list[float]]:
    timeout_seconds = (
        settings.graph_embedding_timeout_seconds if timeout_seconds is None else timeout_seconds
    )
    started = time.perf_counter()
    if timeout_seconds > 0:
        embeddings = await asyncio.wait_for(
            provider.embed_texts(texts, input_kind=input_kind),
            timeout=timeout_seconds,
        )
    else:
        embeddings = await provider.embed_texts(texts, input_kind=input_kind)

    log.info(
        "graph_embedding_complete",
        operation=operation,
        provider=provider.metadata.provider,
        model=provider.metadata.model,
        items=len(texts),
        timeout_seconds=timeout_seconds,
        elapsed_ms=round((time.perf_counter() - started) * 1000, 2),
    )
    return embeddings


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
    client: SurrealGraphClient,
    entity: Entity,
    *,
    group_id: str,
) -> SurrealRecord:
    record = _entity_record(entity, group_id=group_id)
    try:
        result = await _execute_replace_entities_with_schema_retry(client, [record])
    except Exception as exc:
        if not _is_transient_connection_error(exc):
            raise
        mark_graph_schema_dirty(client.group_id)
        await prepare_graph_schema(client)
        result = await _execute_replace_entities_with_schema_retry(client, [record])
    rows = normalize_records(result)
    if rows:
        return rows[0]
    stored = await _select_one(
        client, "SELECT * FROM entity WHERE uuid = $uuid LIMIT 1;", uuid=entity.id
    )
    if stored is None:
        raise RuntimeError(f"failed to persist entity {entity.id}")
    return stored


async def _replace_entities_bulk(
    client: SurrealGraphClient,
    entities: Sequence[Entity],
    *,
    group_id: str,
) -> list[SurrealRecord]:
    records = [_entity_record(entity, group_id=group_id) for entity in entities]
    if not records:
        return []
    try:
        result = await _execute_replace_entities_with_schema_retry(client, records)
    except Exception as exc:
        if not _is_transient_connection_error(exc):
            raise
        mark_graph_schema_dirty(client.group_id)
        await prepare_graph_schema(client)
        result = await _execute_replace_entities_with_schema_retry(client, records)
    return normalize_records(result)


async def _execute_replace_entity_query(
    client: SurrealGraphClient,
    record: SurrealRecord,
) -> object:
    return await _execute_replace_entities_bulk_query(client, [record])


async def _execute_replace_entities_bulk_query(
    client: SurrealGraphClient,
    records: Sequence[SurrealRecord],
) -> object:
    return await client.execute_query(_ENTITY_BULK_UPSERT_QUERY, rows=list(records))


async def _execute_replace_entities_with_schema_retry(
    client: SurrealGraphClient,
    records: Sequence[SurrealRecord],
) -> object:
    try:
        return await _execute_replace_entities_bulk_query(client, records)
    except Exception as exc:
        if not _is_legacy_updated_at_string_schema_error(exc):
            raise
        legacy_records = _records_with_legacy_updated_at_strings(records)
        return await _execute_replace_entities_bulk_query(client, legacy_records)


def _is_legacy_updated_at_string_schema_error(exc: BaseException) -> bool:
    message = str(exc).lower()
    return "coerce value for field `updated_at`" in message and (
        "expected `none | string`" in message or "expected none | string" in message
    )


def _records_with_legacy_updated_at_strings(
    records: Sequence[SurrealRecord],
) -> list[SurrealRecord]:
    converted: list[SurrealRecord] = []
    for record in records:
        patched = dict(record)
        patched["updated_at"] = _legacy_updated_at_value(patched.get("updated_at"))
        attributes = patched.get("attributes")
        if isinstance(attributes, dict):
            patched_attributes = dict(attributes)
            patched_attributes["updated_at"] = _legacy_updated_at_value(
                patched_attributes.get("updated_at")
            )
            patched["attributes"] = patched_attributes
        converted.append(patched)
    return converted


def _legacy_updated_at_value(value: object) -> object:
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _entity_record(
    entity: Entity,
    *,
    group_id: str,
    canonicalize_parent_task_id: bool = True,
) -> SurrealRecord:
    metadata = _entity_metadata(entity)
    now = datetime.now(UTC)
    updated_at = _metadata_datetime(metadata.get("updated_at")) or entity.updated_at or now
    created_at = entity.created_at or now
    project_id = _metadata_str(metadata, "project_id")
    epic_id = _metadata_str(metadata, "epic_id")
    parent_task_id = _metadata_str(metadata, "parent_task_id")
    if canonicalize_parent_task_id and not parent_task_id and entity.entity_type == EntityType.TASK:
        parent_task_id = epic_id
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
        "parent_task_id": parent_task_id,
        "task_id": task_id,
        "status": status,
        "priority": priority,
        "complexity": complexity,
        "feature": feature,
        "tags": tags,
        "name_embedding": entity.embedding,
    }


def _entity_update_patch(updates: Mapping[str, Any], *, updated_at: datetime) -> SurrealRecord:
    metadata_patch = _entity_update_metadata_patch(updates)
    attributes_patch: dict[str, object] = {
        **metadata_patch,
        "updated_at": updated_at,
        "_direct_insert": True,
    }
    patch: SurrealRecord = {
        "updated_at": updated_at,
        "attributes": attributes_patch,
    }

    name = updates.get("name") or updates.get("title")
    if name:
        patch["name"] = str(name)
    if "description" in updates:
        description = str(updates.get("description") or "")
        patch["description"] = description
        attributes_patch["description"] = description
    if "content" in updates:
        patch["content"] = str(updates.get("content") or "")
    if source_file := updates.get("source_file"):
        source_file_text = str(source_file)
        patch["source_file"] = source_file_text
        attributes_patch["source_file"] = source_file_text
    elif "source_file" in updates:
        patch["source_file"] = None
        attributes_patch["source_file"] = ""
    if modified_by := updates.get("modified_by"):
        patch["modified_by"] = str(modified_by)
    if "embedding" in updates:
        embedding = updates.get("embedding")
        patch["name_embedding"] = embedding if isinstance(embedding, list) else None

    for key in (
        "project_id",
        "epic_id",
        "parent_task_id",
        "task_id",
        "status",
        "priority",
        "complexity",
        "feature",
    ):
        if key in metadata_patch:
            patch[key] = _metadata_str(metadata_patch, key)
    if "tags" in metadata_patch:
        patch["tags"] = _metadata_str_list(metadata_patch.get("tags")) or []
    return patch


def _entity_update_metadata_patch(updates: Mapping[str, Any]) -> dict[str, object]:
    metadata: dict[str, object] = {}
    update_metadata = updates.get("metadata")
    if isinstance(update_metadata, Mapping):
        metadata.update({str(key): _jsonable(value) for key, value in update_metadata.items()})

    excluded_keys = {
        "content",
        "description",
        "embedding",
        "metadata",
        "name",
        "source_file",
        "title",
    }
    metadata.update(
        {str(key): _jsonable(value) for key, value in updates.items() if key not in excluded_keys}
    )
    return metadata


async def _replace_relationship(
    client: SurrealGraphClient,
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


async def _record_id(client: SurrealGraphClient, uuid: str) -> object | None:
    row = await _select_one(
        client,
        "SELECT id AS record_id FROM entity WHERE uuid = $uuid LIMIT 1;",
        uuid=uuid,
    )
    return row.get("record_id") if row else None


async def _record_ids(
    client: SurrealGraphClient,
    uuids: Sequence[str],
    *,
    group_id: str,
) -> dict[str, object]:
    unique = list(dict.fromkeys(uuid for uuid in uuids if uuid))
    if not unique:
        return {}
    rows = normalize_records(
        await client.execute_query(
            """
            SELECT uuid, id AS record_id
            FROM entity
            WHERE group_id = $group_id AND uuid IN $uuids;
            """,
            group_id=group_id,
            uuids=unique,
        )
    )
    resolved: dict[str, object] = {}
    for row in rows:
        uuid = row.get("uuid")
        record_id = row.get("record_id")
        if isinstance(uuid, str) and record_id is not None:
            resolved[uuid] = record_id
    return resolved


async def _replace_relationships_bulk(
    client: SurrealGraphClient,
    relationships: Sequence[Relationship],
    *,
    group_id: str,
) -> list[str]:
    if not relationships:
        return []
    endpoint_uuids = [
        endpoint
        for relationship in relationships
        for endpoint in (relationship.source_id, relationship.target_id)
    ]
    record_ids = await _record_ids(client, endpoint_uuids, group_id=group_id)

    rows: list[SurrealRecord] = []
    written_ids: list[str] = []
    for relationship in relationships:
        src = record_ids.get(relationship.source_id)
        tgt = record_ids.get(relationship.target_id)
        if src is None or tgt is None:
            continue
        payload = _relationship_record(relationship, group_id=group_id)
        payload["src"] = src
        payload["tgt"] = tgt
        rows.append(payload)
        written_ids.append(relationship.id)

    if not rows:
        return []

    try:
        await client.execute_query(_RELATIONSHIP_BULK_UPSERT_QUERY, rows=rows)
    except Exception as exc:
        if not _is_transient_connection_error(exc):
            raise
        mark_graph_schema_dirty(client.group_id)
        await prepare_graph_schema(client)
        await client.execute_query(_RELATIONSHIP_BULK_UPSERT_QUERY, rows=rows)
    return written_ids


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
    "EntityManager",
    "GraphRuntime",
    "RelationshipManager",
    "SurrealGraphClient",
    "close_graph_clients",
    "entity_from_surreal_row",
    "get_surreal_graph_client",
    "get_surreal_graph_runtime",
    "normalize_records",
    "prepare_graph_schema",
    "relationship_from_surreal_row",
]
