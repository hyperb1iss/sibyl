"""Entity management for the knowledge graph.

This module provides entity CRUD operations using Graphiti's native node APIs.
All graph operations go through EntityNode/EpisodicNode rather than raw Cypher.
"""

import asyncio
import contextlib
import json
import random
import re
from collections import defaultdict
from datetime import UTC, datetime
from enum import Enum
from typing import Any, TypeVar
from uuid import uuid4

import structlog
from graphiti_core.nodes import EntityNode, EpisodeType, EpisodicNode
from graphiti_core.search.search_config_recipes import NODE_HYBRID_SEARCH_RRF
from pydantic import BaseModel

from sibyl_core.errors import EntityNotFoundError, SearchError
from sibyl_core.graph.client import GraphClient
from sibyl_core.models.entities import Entity, EntityType, Procedure, ProcedureStep
from sibyl_core.models.sources import Community, Document, Source
from sibyl_core.models.tasks import (
    Epic,
    ErrorPattern,
    Milestone,
    Note,
    Project,
    Task,
    TaskComplexity,
    TaskPriority,
    TaskStatus,
    Team,
)
from sibyl_core.utils.log_safety import query_log_fields
from sibyl_core.utils.resilience import GRAPHITI_RETRY

log = structlog.get_logger()

# Generic enum type for coercion
TEnum = TypeVar("TEnum", bound=Enum)
# RediSearch special characters that need escaping in fulltext queries
# Includes / which appears in paths like "create/cleanup" or local file paths
_REDISEARCH_SPECIAL_CHARS = re.compile(r"[|&\-@()~$:*\\/]")
_NON_EPISODIC_ID_PREFIXES = tuple(
    f"{entity_type.value}_"
    for entity_type in EntityType
    if entity_type not in {EntityType.EPISODE}
)
_SEARCH_TERM_RE = re.compile(r"[a-z0-9_]{2,}")
_SEARCH_STOP_WORDS = {
    "a",
    "an",
    "and",
    "are",
    "for",
    "from",
    "in",
    "is",
    "of",
    "on",
    "or",
    "the",
    "this",
    "to",
    "with",
}


def sanitize_search_query(query: str) -> str:
    """Escape RediSearch special characters in a query string.

    RediSearch treats |, &, -, @, (), ~, $, :, * as special operators.
    When these appear in document titles or content, they cause syntax errors.
    """
    return _REDISEARCH_SPECIAL_CHARS.sub(r" ", query)


def _should_try_episodic_lookup(entity_id: str) -> bool:
    return not entity_id.startswith(_NON_EPISODIC_ID_PREFIXES)


def _search_terms(query: str) -> list[str]:
    seen: set[str] = set()
    terms: list[str] = []
    for term in _SEARCH_TERM_RE.findall(query.lower()):
        if term in _SEARCH_STOP_WORDS or term in seen:
            continue
        seen.add(term)
        terms.append(term)
    return terms


def _token_recall_score(
    *,
    terms: list[str],
    name: str,
    description: str,
    content: str,
) -> float | None:
    if not terms:
        return None

    text = f"{name} {description} {content}"
    matched = [term for term in terms if term in text]
    if not matched:
        return None

    coverage = len(matched) / len(terms)
    required_matches = len(terms) if len(terms) <= 2 else min(3, len(terms))
    if len(matched) < required_matches and coverage < 0.6:
        return None

    score = 0.45 + (coverage * 0.25)
    if any(term in name for term in matched):
        score += 0.1
    if any(term in description for term in matched):
        score += 0.05
    return min(score, 0.82)


def _metadata_json_contains_params(
    prefix: str, field: str, value: str
) -> tuple[dict[str, str], str]:
    """Build CONTAINS params for legacy JSON-string metadata matching."""
    compact_key = f"{prefix}_compact"
    spaced_key = f"{prefix}_spaced"
    normalized_value = value.lower()
    return (
        {
            compact_key: f'"{field}":"{normalized_value}"',
            spaced_key: f'"{field}": "{normalized_value}"',
        },
        (
            f"toLower(toString(n.metadata)) CONTAINS ${compact_key} "
            f"OR toLower(toString(n.metadata)) CONTAINS ${spaced_key}"
        ),
    )


def _metadata_json_contains_any_params(
    prefix: str,
    field: str,
    values: list[str],
) -> tuple[dict[str, str], str]:
    """Build ORed CONTAINS params for legacy JSON-string metadata matching."""
    params: dict[str, str] = {}
    clauses: list[str] = []
    for index, value in enumerate(values):
        value_params, value_clause = _metadata_json_contains_params(
            f"{prefix}_{index}",
            field,
            value,
        )
        params.update(value_params)
        clauses.append(f"({value_clause})")
    if not clauses:
        return {}, "FALSE"
    return params, " OR ".join(clauses)


class EntityManager:
    """Manages entity CRUD operations in the knowledge graph."""

    def __init__(self, client: "GraphClient", *, group_id: str) -> None:
        """Initialize entity manager with graph client.

        Creates a cloned driver targeting the org-specific graph for multi-tenancy.
        FalkorDB supports multiple isolated graphs within a single database instance.

        Args:
            client: The GraphClient instance.
            group_id: Organization ID (required). No default - callers must provide org context.

        Raises:
            ValueError: If group_id is empty.
        """
        if not group_id:
            raise ValueError("group_id is required - cannot access graph without org context")
        self._client = client
        self._group_id = group_id
        self._driver = client.get_org_driver(group_id)

    def _surreal_entity_node_ops(self):
        try:
            from sibyl_core.backends.surreal import SurrealDriver
        except ImportError:
            return None

        if isinstance(self._driver, SurrealDriver):
            return self._driver.entity_node_ops
        return None

    def _surreal_episode_node_ops(self):
        try:
            from sibyl_core.backends.surreal import SurrealDriver
        except ImportError:
            return None

        if isinstance(self._driver, SurrealDriver):
            return self._driver.episode_node_ops
        return None

    def _is_surreal_driver(self) -> bool:
        try:
            from sibyl_core.backends.surreal import SurrealDriver
        except ImportError:
            return False

        return isinstance(self._driver, SurrealDriver)

    def _assert_legacy_fallback_allowed(self, operation: str) -> None:
        if self._is_surreal_driver():
            raise RuntimeError(f"SurrealDB entity {operation} requires native node operations")

    def _raise_if_surreal_fallback_guard(self, error: Exception) -> None:
        if (
            self._is_surreal_driver()
            and isinstance(error, RuntimeError)
            and "requires native node operations" in str(error)
        ):
            raise error

    def _build_entity_node_attributes(
        self,
        entity: Entity,
        *,
        marker_key: str,
    ) -> dict[str, Any]:
        metadata = self._entity_to_metadata(entity)
        attributes = self._collect_properties(entity)
        attributes.update(
            {
                "description": entity.description or "",
                "content": entity.content or "",
                "source_file": entity.source_file or "",
                "updated_at": datetime.now(UTC).isoformat(),
                marker_key: True,
                "metadata": json.dumps(metadata),
            }
        )
        return {
            "entity_type": entity.entity_type.value,
            **attributes,
        }

    def _build_entity_node(
        self,
        entity: Entity,
        *,
        marker_key: str,
    ) -> EntityNode:
        node = EntityNode(
            uuid=entity.id,
            name=entity.name,
            group_id=self._group_id,
            labels=[entity.entity_type.value],
            created_at=entity.created_at or datetime.now(UTC),
            summary=entity.description[:500] if entity.description else entity.name,
            attributes=self._build_entity_node_attributes(entity, marker_key=marker_key),
        )
        if isinstance(entity.embedding, list) and entity.embedding:
            node.name_embedding = entity.embedding
        return node

    async def _surreal_entity_nodes_for_group(
        self,
        *,
        page_size: int = 1000,
    ) -> list[EntityNode]:
        ops = self._surreal_entity_node_ops()
        if ops is None:
            return []

        nodes: list[EntityNode] = []
        uuid_cursor: str | None = None
        seen_cursors: set[str] = set()

        while True:
            batch = await ops.get_by_group_ids(
                self._driver,
                [self._group_id],
                limit=page_size,
                uuid_cursor=uuid_cursor,
            )
            if not batch:
                break
            nodes.extend(batch)
            if len(batch) < page_size:
                break
            next_cursor = batch[-1].uuid
            if next_cursor in seen_cursors:
                break
            seen_cursors.add(next_cursor)
            uuid_cursor = next_cursor

        return nodes

    async def _surreal_entities_for_group(
        self,
        *,
        include_archived: bool,
    ) -> list[Entity]:
        entities: list[Entity] = []
        for node in await self._surreal_entity_nodes_for_group():
            try:
                entity = self.node_to_entity(node)
            except Exception as exc:
                log.debug("Failed to convert Surreal node to entity", error=str(exc))
                continue
            if not include_archived and self._entity_is_archived(entity):
                continue
            entities.append(entity)

        entities.sort(key=self._entity_sort_key, reverse=True)
        return entities

    async def _surreal_select_entity_records(
        self,
        *,
        entity_type: EntityType | None = None,
        limit: int,
        offset: int,
        project_id: str | None = None,
        epic_id: str | None = None,
        no_epic: bool = False,
        task_id: str | None = None,
        status_values: list[str] | None = None,
        priority_values: list[str] | None = None,
        complexity_values: list[str] | None = None,
        feature: str | None = None,
        include_archived: bool = False,
    ) -> list[dict[str, Any]]:
        if limit <= 0:
            return []

        where_clauses = ["group_id = $group_id"]
        params: dict[str, Any] = {
            "group_id": self._group_id,
            "query_limit": max(limit, 1),
            "query_offset": max(offset, 0),
        }

        if entity_type is not None:
            where_clauses.append("entity_type = $entity_type")
            params["entity_type"] = entity_type.value

        if project_id is not None:
            where_clauses.append("project_id = $project_id")
            params["project_id"] = project_id
        if epic_id is not None:
            where_clauses.append("epic_id = $epic_id")
            params["epic_id"] = epic_id
        if no_epic:
            where_clauses.append("(epic_id IS NONE OR epic_id = '')")
        if task_id is not None:
            where_clauses.append("task_id = $task_id")
            params["task_id"] = task_id
        if status_values:
            where_clauses.append("string::lowercase(status ?? '') IN $status_values")
            params["status_values"] = status_values
        if priority_values:
            where_clauses.append("string::lowercase(priority ?? '') IN $priority_values")
            params["priority_values"] = priority_values
        if complexity_values:
            where_clauses.append("string::lowercase(complexity ?? '') IN $complexity_values")
            params["complexity_values"] = complexity_values
        if feature is not None:
            where_clauses.append("feature = $feature")
            params["feature"] = feature
        if not include_archived:
            where_clauses.append("string::lowercase(status ?? '') != 'archived'")

        query = f"""
            SELECT uuid,
                   name,
                   entity_type,
                   group_id,
                   summary,
                   project_id,
                   epic_id,
                   task_id,
                   status,
                   priority,
                   complexity,
                   feature,
                   tags,
                   attributes.metadata AS metadata,
                   attributes.description AS description,
                   attributes.content AS content,
                   attributes.source_file AS source_file,
                   updated_at ?? attributes.updated_at AS updated_at,
                   created_at
            FROM entity
            WHERE {" AND ".join(where_clauses)}
            ORDER BY updated_at DESC, created_at DESC, uuid DESC
            LIMIT $query_limit START $query_offset;
        """

        return GraphClient.normalize_result(await self._driver.execute_query(query, **params))

    async def _surreal_search_entity_records(
        self,
        *,
        query_lower: str,
        entity_types: list[EntityType] | None,
        limit: int,
        exact_name_only: bool = False,
    ) -> list[dict[str, Any]]:
        if limit <= 0 or not query_lower:
            return []

        fulltext_query = query_lower
        if not exact_name_only:
            fulltext_query = self._driver.build_fulltext_query(query_lower)
            if not fulltext_query:
                return []

        params: dict[str, Any] = {
            "group_id": self._group_id,
            "query_lower": query_lower,
            "search_query": fulltext_query,
            "query_limit": max(limit, 1),
        }
        where_clauses = ["group_id = $group_id"]
        if entity_types:
            where_clauses.append("entity_type IN $entity_types")
            params["entity_types"] = [entity_type.value for entity_type in entity_types]

        name_expr = "string::lowercase(name ?? '')"
        score_expr = ""
        order_by = "updated_at DESC, created_at DESC, uuid DESC"
        if exact_name_only:
            where_clauses.append(f"{name_expr} = $query_lower")
        else:
            where_clauses.append(
                """(
                    name @0@ $search_query
                    OR summary @1@ $search_query
                    OR attributes.description @2@ $search_query
                    OR attributes.content @3@ $search_query
                )"""
            )
            score_expr = (
                "math::max([search::score(0), search::score(1), "
                "search::score(2), search::score(3)]) AS search_score,"
            )
            order_by = (
                "search_score DESC, updated_at DESC, created_at DESC, uuid DESC"
            )

        search_query = f"""
            SELECT uuid,
                   name,
                   entity_type,
                   group_id,
                   summary,
                   project_id,
                   epic_id,
                   task_id,
                   status,
                   priority,
                   complexity,
                   feature,
                   tags,
                   attributes.metadata AS metadata,
                   attributes.description AS description,
                   attributes.content AS content,
                   attributes.source_file AS source_file,
                   updated_at ?? attributes.updated_at AS updated_at,
                   {score_expr}
                   created_at
            FROM entity
            WHERE {" AND ".join(where_clauses)}
            ORDER BY {order_by}
            LIMIT $query_limit;
        """
        return await self._execute_surreal_schema_aware_query(search_query, params)

    async def _execute_surreal_schema_aware_query(
        self,
        query: str,
        params: dict[str, Any],
    ) -> list[dict[str, Any]]:
        try:
            return GraphClient.normalize_result(await self._driver.execute_query(query, **params))
        except Exception as exc:
            if not self._is_missing_surreal_fulltext_index(exc):
                raise
            log.warning(
                "surreal_graph_schema_missing_fulltext_index",
                group_id=self._group_id,
                error_type=type(exc).__name__,
            )
            await self._driver.build_indices_and_constraints()
            return GraphClient.normalize_result(await self._driver.execute_query(query, **params))

    def _is_missing_surreal_fulltext_index(self, error: Exception) -> bool:
        return "no suitable index supporting the expression" in str(error).lower()

    def _surreal_should_search_entities(
        self,
        entity_types: list[EntityType] | None,
    ) -> bool:
        return entity_types is None or any(
            entity_type != EntityType.EPISODE for entity_type in entity_types
        )

    def _surreal_should_search_episodes(
        self,
        entity_types: list[EntityType] | None,
    ) -> bool:
        return entity_types is None or EntityType.EPISODE in entity_types

    async def _surreal_search_episode_records(
        self,
        *,
        query_lower: str,
        limit: int,
        exact_name_only: bool = False,
    ) -> list[dict[str, Any]]:
        if limit <= 0 or not query_lower:
            return []

        params: dict[str, Any] = {
            "group_id": self._group_id,
            "query_lower": query_lower,
            "prefixed_query_lower": f"{EntityType.EPISODE.value}:{query_lower}",
            "query_limit": max(limit, 1),
        }
        name_expr = "string::lowercase(name ?? '')"

        if exact_name_only:
            search_query = f"""
                SELECT uuid,
                       name,
                       group_id,
                       content,
                       source_description,
                       created_at,
                       valid_at,
                       2.0 AS search_score
                FROM episode
                WHERE group_id = $group_id
                  AND ({name_expr} = $query_lower OR {name_expr} = $prefixed_query_lower)
                ORDER BY created_at DESC, uuid DESC
                LIMIT $query_limit;
            """
            return GraphClient.normalize_result(
                await self._driver.execute_query(search_query, **params)
            )

        fulltext_query = self._driver.build_fulltext_query(query_lower)
        if not fulltext_query:
            return []

        params["search_query"] = fulltext_query
        search_query = f"""
            SELECT uuid,
                   name,
                   group_id,
                   content,
                   source_description,
                   created_at,
                   valid_at,
                   search::score(0) AS search_score
            FROM episode
            WHERE group_id = $group_id
              AND (
                  content @0@ $search_query
                  OR {name_expr} = $query_lower
                  OR {name_expr} = $prefixed_query_lower
              )
            ORDER BY search_score DESC, created_at DESC, uuid DESC
            LIMIT $query_limit;
        """
        return await self._execute_surreal_schema_aware_query(search_query, params)

    async def _surreal_scan_recent_entity_records(
        self,
        *,
        entity_types: list[EntityType] | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        if limit <= 0:
            return []

        if not entity_types:
            return await self._surreal_select_entity_records(
                entity_type=None,
                limit=limit,
                offset=0,
            )

        records: list[dict[str, Any]] = []
        seen: set[str] = set()
        for entity_type in entity_types:
            if entity_type == EntityType.EPISODE:
                continue
            for record in await self._surreal_select_entity_records(
                entity_type=entity_type,
                limit=limit,
                offset=0,
            ):
                record_uuid = record.get("uuid")
                if not isinstance(record_uuid, str) or record_uuid in seen:
                    continue
                seen.add(record_uuid)
                records.append(record)
                if len(records) >= limit:
                    return records
        return records

    async def _surreal_scan_recent_episode_records(
        self,
        *,
        limit: int,
    ) -> list[dict[str, Any]]:
        if limit <= 0:
            return []
        query = """
            SELECT uuid,
                   name,
                   group_id,
                   content,
                   source_description,
                   created_at,
                   valid_at
            FROM episode
            WHERE group_id = $group_id
            ORDER BY created_at DESC, uuid DESC
            LIMIT $query_limit;
        """
        return GraphClient.normalize_result(
            await self._driver.execute_query(
                query,
                group_id=self._group_id,
                query_limit=limit,
            )
        )

    async def _surreal_list_entities_direct(
        self,
        *,
        entity_type: EntityType | None = None,
        limit: int,
        offset: int,
        project_id: str | None = None,
        epic_id: str | None = None,
        no_epic: bool = False,
        status_values: list[str] | None = None,
        priority_values: list[str] | None = None,
        complexity_values: list[str] | None = None,
        feature: str | None = None,
        tags: list[str] | None = None,
        include_archived: bool = False,
    ) -> list[Entity]:
        if limit <= 0:
            return []

        requires_python_rechecks = any(
            [
                project_id is not None,
                epic_id is not None,
                no_epic,
                bool(status_values),
                bool(priority_values),
                bool(complexity_values),
                feature is not None,
                bool(tags),
                not include_archived,
            ]
        )
        target_count = offset + limit if requires_python_rechecks else limit
        query_offset = 0 if requires_python_rechecks else offset
        page_size = (
            min(max(target_count, 100), 1000)
            if requires_python_rechecks
            else min(max(limit, 1), 1000)
        )
        entities: list[Entity] = []
        seen_entity_ids: set[str] = set()
        seen_pages: set[tuple[str | None, ...]] = set()
        pushed_filter_to_surreal = any(
            [
                project_id is not None,
                epic_id is not None,
                no_epic,
                bool(status_values),
                bool(priority_values),
                bool(complexity_values),
                feature is not None,
            ]
        )

        while len(entities) < target_count:
            records = await self._surreal_select_entity_records(
                entity_type=entity_type,
                limit=page_size,
                offset=query_offset,
                project_id=project_id,
                epic_id=epic_id,
                no_epic=no_epic,
                status_values=status_values,
                priority_values=priority_values,
                complexity_values=complexity_values,
                feature=feature,
                include_archived=include_archived,
            )
            if not records:
                break

            page_signature = tuple(
                record_uuid if isinstance(record_uuid := record.get("uuid"), str) else None
                for record in records
            )
            if page_signature in seen_pages:
                log.warning(
                    "Surreal entity page repeated, stopping pagination",
                    entity_type=entity_type,
                    query_offset=query_offset,
                    query_limit=page_size,
                )
                break
            seen_pages.add(page_signature)

            for record in records:
                try:
                    entity = self._coerce_entity(self._record_to_entity(record))
                except Exception as exc:
                    log.debug("Failed to hydrate Surreal entity record", error=str(exc))
                    continue

                if entity.id in seen_entity_ids:
                    continue
                if entity_type is not None and entity.entity_type != entity_type:
                    continue
                if not self._entity_matches_filters(
                    entity,
                    project_id=project_id,
                    epic_id=epic_id,
                    no_epic=no_epic,
                    status_values=status_values,
                    priority_values=priority_values,
                    complexity_values=complexity_values,
                    feature=feature,
                    tags=tags,
                    include_archived=include_archived,
                ):
                    continue

                seen_entity_ids.add(entity.id)
                entities.append(entity)

            query_offset += len(records)
            if len(records) < page_size:
                break

        if not entities and pushed_filter_to_surreal:
            legacy_offset = 0
            legacy_seen_pages: set[tuple[str | None, ...]] = set()

            while len(entities) < target_count:
                records = await self._surreal_select_entity_records(
                    entity_type=entity_type,
                    limit=page_size,
                    offset=legacy_offset,
                    include_archived=True,
                )
                if not records:
                    break

                page_signature = tuple(
                    record_uuid if isinstance(record_uuid := record.get("uuid"), str) else None
                    for record in records
                )
                if page_signature in legacy_seen_pages:
                    log.warning(
                        "Surreal legacy entity page repeated, stopping pagination",
                        entity_type=entity_type,
                        query_offset=legacy_offset,
                        query_limit=page_size,
                    )
                    break
                legacy_seen_pages.add(page_signature)

                for record in records:
                    try:
                        entity = self._coerce_entity(self._record_to_entity(record))
                    except Exception as exc:
                        log.debug("Failed to hydrate Surreal entity record", error=str(exc))
                        continue

                    if entity.id in seen_entity_ids:
                        continue
                    if entity_type is not None and entity.entity_type != entity_type:
                        continue
                    if not self._entity_matches_filters(
                        entity,
                        project_id=project_id,
                        epic_id=epic_id,
                        no_epic=no_epic,
                        status_values=status_values,
                        priority_values=priority_values,
                        complexity_values=complexity_values,
                        feature=feature,
                        tags=tags,
                        include_archived=include_archived,
                    ):
                        continue

                    seen_entity_ids.add(entity.id)
                    entities.append(entity)
                    if len(entities) >= target_count:
                        break

                legacy_offset += len(records)
                if len(records) < page_size:
                    break

        if requires_python_rechecks:
            return entities[offset : offset + limit]
        return entities[:limit]

    async def _list_all_via_type_scans(
        self,
        *,
        include_archived: bool,
    ) -> list[Entity]:
        entities_by_id: dict[str, Entity] = {}

        for entity_type in EntityType:
            offset = 0
            while True:
                batch = await self.list_by_type(
                    entity_type,
                    limit=100,
                    offset=offset,
                    include_archived=include_archived,
                )
                if not batch:
                    break
                for entity in batch:
                    entities_by_id[entity.id] = entity
                if len(batch) < 100:
                    break
                offset += 100

        entities = list(entities_by_id.values())
        entities.sort(key=self._entity_sort_key, reverse=True)
        return entities

    def _entity_sort_key(self, entity: Entity) -> tuple[datetime, str]:
        timestamp = entity.updated_at or entity.created_at or datetime.min.replace(tzinfo=UTC)
        return (timestamp, entity.id)

    def _entity_is_archived(self, entity: Entity) -> bool:
        return str((entity.metadata or {}).get("status") or "").lower() == "archived"

    def _entity_matches_filters(
        self,
        entity: Entity,
        *,
        project_id: str | None = None,
        epic_id: str | None = None,
        no_epic: bool = False,
        status_values: list[str] | None = None,
        priority_values: list[str] | None = None,
        complexity_values: list[str] | None = None,
        feature: str | None = None,
        tags: list[str] | None = None,
        include_archived: bool = False,
    ) -> bool:
        metadata = entity.metadata or {}

        if project_id and metadata.get("project_id") != project_id:
            return False
        if epic_id and metadata.get("epic_id") != epic_id:
            return False
        if no_epic and metadata.get("epic_id"):
            return False
        if status_values and str(metadata.get("status") or "").lower() not in status_values:
            return False
        if priority_values and str(metadata.get("priority") or "").lower() not in priority_values:
            return False
        if (
            complexity_values
            and str(metadata.get("complexity") or "").lower() not in complexity_values
        ):
            return False
        if feature and metadata.get("feature") != feature:
            return False
        if tags:
            entity_tags = metadata.get("tags") or []
            if not any(tag in entity_tags for tag in tags):
                return False
        return include_archived or not self._entity_is_archived(entity)

    async def _add_episode_with_retry(
        self,
        name: str,
        episode_body: str,
        source_description: str,
        reference_time: datetime,
        entity_types: dict[str, type[BaseModel]],
    ) -> Any:
        """Call Graphiti add_episode with retry on transient failures.

        This is separated out to apply retry logic - add_episode can take 60-90s
        under load and may fail with Redis timeouts during edge_fulltext_search.
        """
        # Import here to get RedisTimeoutError for type annotation
        try:
            from redis.exceptions import TimeoutError as RedisTimeoutError
        except ImportError:
            RedisTimeoutError = TimeoutError  # type: ignore[misc,assignment]

        # Retry wrapper - applied inline since @retry decorator doesn't work well with methods
        last_error: Exception | None = None
        for attempt in range(GRAPHITI_RETRY.max_attempts):
            try:
                return await self._client.client.add_episode(
                    name=name,
                    episode_body=episode_body,
                    source_description=source_description,
                    reference_time=reference_time,
                    group_id=self._group_id,
                    entity_types=entity_types,
                )
            except (ConnectionError, TimeoutError, OSError, RedisTimeoutError) as e:
                last_error = e
                if attempt < GRAPHITI_RETRY.max_attempts - 1:
                    delay = min(
                        GRAPHITI_RETRY.base_delay * (2**attempt),
                        GRAPHITI_RETRY.max_delay,
                    )
                    if GRAPHITI_RETRY.jitter:
                        delay += random.uniform(-delay * 0.25, delay * 0.25)
                    log.warning(
                        "add_episode failed, retrying",
                        attempt=attempt + 1,
                        max_attempts=GRAPHITI_RETRY.max_attempts,
                        delay=f"{delay:.1f}s",
                        error=str(e),
                    )
                    await asyncio.sleep(delay)
                else:
                    log.error(
                        "add_episode exhausted retries",
                        attempts=GRAPHITI_RETRY.max_attempts,
                        error=str(e),
                    )
        if last_error:
            raise last_error
        raise RuntimeError("Retry logic error in add_episode")

    async def _create_surreal_episode_direct(self, entity: Entity) -> str:
        episode_ops = self._surreal_episode_node_ops()
        if episode_ops is None:
            msg = "Surreal episode ops are unavailable"
            raise RuntimeError(msg)

        episode_id = entity.id or str(uuid4())
        node = EpisodicNode(
            uuid=episode_id,
            name=f"{entity.entity_type.value}:{entity.name}",
            group_id=self._group_id,
            labels=[EntityType.EPISODE.value],
            source=EpisodeType.text,
            source_description=entity.description or entity.source_file or "Sibyl episode",
            content=entity.content or entity.description or entity.name,
            valid_at=entity.created_at or datetime.now(UTC),
            entity_edges=[],
        )
        await episode_ops.save(self._driver, node)
        return episode_id

    async def create(self, entity: Entity) -> str:
        """Create a new entity in the graph.

        Args:
            entity: The entity to create.

        Returns:
            The ID of the created entity.
        """
        log.info("Creating entity", entity_type=entity.entity_type, name=entity.name)

        try:
            if (
                self._surreal_episode_node_ops() is not None
                and entity.entity_type == EntityType.EPISODE
            ):
                return await self._create_surreal_episode_direct(entity)

            if (
                self._surreal_entity_node_ops() is not None
                and entity.entity_type != EntityType.EPISODE
            ):
                return await self.create_direct(entity)

            self._assert_legacy_fallback_allowed("create")

            # Use add_episode to store the entity in Graphiti
            # Graphiti extracts entities from episode content, so we format it as natural language
            episode_body = self._format_entity_as_episode(entity)

            # Store the entity metadata in custom entity_types for extraction
            # Cast to dict[str, type[BaseModel]] for type safety
            entity_types: dict[str, type[BaseModel]] = {entity.entity_type.value: BaseModel}

            # Sanitize the episode name for RediSearch compatibility
            # First: remove markdown formatting (bold/italic)
            safe_name = re.sub(r"\*{1,3}", "", entity.name)
            safe_name = re.sub(r"_{1,3}", "", safe_name)
            # Second: remove special characters that break RediSearch
            safe_name = re.sub(r"[`\[\]{}()|@#$%^&+=<>/:\"']", "", safe_name)
            safe_name = re.sub(r"\s+", " ", safe_name).strip()

            # Call add_episode with retry logic for transient failures
            result = await self._add_episode_with_retry(
                name=f"{entity.entity_type}:{safe_name}",
                episode_body=episode_body,
                source_description=f"MCP Entity: {entity.entity_type}",
                reference_time=entity.created_at or datetime.now(UTC),
                entity_types=entity_types,
            )

            created_uuid = result.episode.uuid
            desired_id = entity.id or created_uuid

            if self._surreal_episode_node_ops() is not None:
                if created_uuid != desired_id:
                    await self._driver.execute_query(
                        "UPDATE episode SET uuid = $desired_id WHERE uuid = $created_uuid;",
                        created_uuid=created_uuid,
                        desired_id=desired_id,
                    )
            else:
                # Force deterministic UUID when caller provides one
                await self._driver.execute_query(
                    """
                    MATCH (n {uuid: $created_uuid})
                    SET n.uuid = $desired_id
                    RETURN n.uuid
                    """,
                    created_uuid=created_uuid,
                    desired_id=desired_id,
                )

                # Persist attributes and metadata on the created node so downstream filters work
                await self._persist_entity_attributes(desired_id, entity)

            log.info(
                "Entity created successfully",
                entity_id=desired_id,
                episode_uuid=created_uuid,
            )
            return desired_id

        except Exception as e:
            log.exception("Failed to create entity", entity_id=entity.id, error=str(e))
            raise

    async def create_direct(self, entity: Entity, *, generate_embedding: bool = True) -> str:
        """Create an entity directly using Graphiti's EntityNode, bypassing LLM.

        This is faster than create() as it skips LLM-based entity extraction.
        Use this for structured entities (tasks, projects) where LLM extraction
        isn't needed. Generates embeddings inline for semantic search support.

        Uses EntityNode.save() which handles idempotent creation (MERGE pattern).

        Args:
            entity: The entity to create.
            generate_embedding: If True (default), generate and store a name_embedding
                for semantic search. Set to False for bulk inserts where embeddings
                will be generated separately.

        Returns:
            The ID of the created entity.

        Raises:
            EntityCreationError: If creation fails.
        """
        import time as _time

        from sibyl_core.errors import EntityCreationError

        log.info(
            "Creating entity directly via EntityNode",
            entity_type=entity.entity_type,
            name=entity.name,
        )

        try:
            _t0 = _time.perf_counter()
            surreal_entity_ops = self._surreal_entity_node_ops()
            node = self._build_entity_node(entity, marker_key="_direct_insert")

            _t1 = _time.perf_counter()
            log.debug("create_direct_timing", step="build_node", ms=round((_t1 - _t0) * 1000))

            if surreal_entity_ops is not None:
                await surreal_entity_ops.save(self._driver, node)
            else:
                self._assert_legacy_fallback_allowed("create_direct")
                await node.save(self._driver)

            _t2 = _time.perf_counter()
            log.debug("create_direct_timing", step="node_save", ms=round((_t2 - _t1) * 1000))

            if surreal_entity_ops is None:
                # Persist structured properties (project_id, status, etc.) for graph filtering.
                # SurrealDB keeps the normalized payload in node.attributes until the
                # store-specific query layer is ported.
                await self._persist_entity_attributes(entity.id, entity)

            _t3 = _time.perf_counter()
            log.debug("create_direct_timing", step="persist_attrs", ms=round((_t3 - _t2) * 1000))

            # Generate embedding for semantic search (name + summary combined)
            if generate_embedding:
                try:
                    embed_text = f"{entity.name}. {entity.description or ''}"[:2000]
                    embedding = await self._client.client.embedder.create(embed_text)

                    _t4 = _time.perf_counter()
                    log.debug(
                        "create_direct_timing", step="embedding_api", ms=round((_t4 - _t3) * 1000)
                    )

                    if surreal_entity_ops is not None:
                        node.name_embedding = embedding
                        await surreal_entity_ops.save(self._driver, node)
                    else:
                        # Store embedding on node using vecf32() for FalkorDB vector ops
                        await self._driver.execute_query(
                            "MATCH (n {uuid: $entity_id}) SET n.name_embedding = vecf32($embedding)",
                            entity_id=entity.id,
                            embedding=embedding,
                        )

                    _t5 = _time.perf_counter()
                    log.debug(
                        "create_direct_timing",
                        step="store_embedding",
                        ms=round((_t5 - _t4) * 1000),
                        total_ms=round((_t5 - _t0) * 1000),
                    )
                    log.debug("Generated embedding for entity", entity_id=entity.id)
                except Exception as e:
                    # Don't fail entity creation if embedding fails - search will still work via BM25
                    log.warning(
                        "Failed to generate embedding, entity still created",
                        entity_id=entity.id,
                        error=str(e),
                    )

            log.info(
                "Entity created via EntityNode.save",
                entity_id=entity.id,
                entity_type=entity.entity_type,
            )
            return entity.id

        except Exception as e:
            log.exception(
                "Failed to create entity directly",
                entity_id=entity.id,
                error=str(e),
            )
            raise EntityCreationError(
                f"Failed to create entity: {e}",
                entity_id=entity.id,
            ) from e

    async def get(self, entity_id: str) -> Entity:
        """Get an entity by ID using Graphiti's node APIs.

        Tries EntityNode first, then EpisodicNode, since nodes can be either type.

        Args:
            entity_id: The entity's unique identifier.

        Returns:
            The requested entity.

        Raises:
            EntityNotFoundError: If entity doesn't exist.
        """
        import time as _time

        log.debug("Fetching entity", entity_id=entity_id)
        _t0 = _time.perf_counter()

        try:
            surreal_entity_ops = self._surreal_entity_node_ops()
            surreal_episode_ops = self._surreal_episode_node_ops()
            # Try EntityNode first (nodes created via create_direct or extracted)
            try:
                if surreal_entity_ops is not None:
                    node = await surreal_entity_ops.get_by_uuid(self._driver, entity_id)
                else:
                    node = await EntityNode.get_by_uuid(self._driver, entity_id)
                _t1 = _time.perf_counter()
                log.debug("get_timing", step="entity_node_query", ms=round((_t1 - _t0) * 1000))

                if node and node.group_id == self._group_id:
                    entity = self.node_to_entity(node)
                    log.debug(
                        "Entity retrieved via EntityNode",
                        entity_id=entity_id,
                        entity_type=entity.entity_type,
                    )
                    return self._coerce_entity(entity)
            except Exception as e:
                _t1 = _time.perf_counter()
                log.debug(
                    "EntityNode lookup failed",
                    entity_id=entity_id,
                    error=str(e),
                    ms=round((_t1 - _t0) * 1000),
                )

            if not _should_try_episodic_lookup(entity_id):
                raise EntityNotFoundError("Entity", entity_id)

            # Try EpisodicNode (nodes created via add_episode)
            try:
                if surreal_episode_ops is not None:
                    episodic = await surreal_episode_ops.get_by_uuid(self._driver, entity_id)
                else:
                    episodic = await EpisodicNode.get_by_uuid(self._driver, entity_id)
                if episodic and episodic.group_id == self._group_id:
                    # Query for entity_type property (not hydrated by Graphiti's dataclass)
                    entity_type_override = (
                        None
                        if surreal_episode_ops is not None
                        else await self._get_node_entity_type(entity_id)
                    )
                    entity = self._episodic_to_entity(episodic, entity_type_override)
                    log.debug(
                        "Entity retrieved via EpisodicNode",
                        entity_id=entity_id,
                        entity_type=entity.entity_type,
                    )
                    return self._coerce_entity(entity)
            except Exception as e:
                log.debug("EpisodicNode lookup failed", entity_id=entity_id, error=str(e))

            raise EntityNotFoundError("Entity", entity_id)

        except EntityNotFoundError:
            raise
        except Exception as e:
            log.exception("Failed to retrieve entity", entity_id=entity_id, error=str(e))
            raise EntityNotFoundError("Entity", entity_id) from e

    async def search(
        self,
        query: str,
        entity_types: list[EntityType] | None = None,
        limit: int = 10,
    ) -> list[tuple[Entity, float]]:
        """Semantic search for entities using Graphiti's node-based hybrid search.

        Uses NODE_HYBRID_SEARCH which combines:
        - BM25 keyword search on node text
        - Cosine similarity on name_embedding vectors
        - RRF (Reciprocal Rank Fusion) for combining results

        Args:
            query: Natural language search query.
            entity_types: Optional filter by entity types.
            limit: Maximum results to return.

        Returns:
            List of (entity, score) tuples ordered by relevance.
        """
        import time as _time

        # Sanitize query to escape RediSearch special characters
        safe_query = sanitize_search_query(query)
        log.info("Searching entities", **query_log_fields(query), types=entity_types, limit=limit)

        try:
            _t0 = _time.perf_counter()
            results: list[tuple[Entity, float]] = []
            if self._surreal_entity_node_ops() is not None:
                exact_name_results = await self._exact_name_search(
                    query=query,
                    entity_types=entity_types,
                    limit=limit,
                )
                if len(exact_name_results) >= limit:
                    log.info(
                        "Search completed",
                        **query_log_fields(query),
                        results_count=len(exact_name_results),
                        mode="surreal_direct_exact",
                    )
                    return exact_name_results[:limit]

                fallback_results = await self._fallback_text_search(
                    query=query,
                    entity_types=entity_types,
                    limit=limit,
                )
                results = self._merge_ranked_results(exact_name_results, fallback_results, limit)
                log.info(
                    "Search completed",
                    **query_log_fields(query),
                    results_count=len(results),
                    mode="surreal_direct",
                )
                return results

            try:
                # Use search_() with NODE_HYBRID_SEARCH for direct node search
                # This searches node embeddings directly instead of going through edges
                # CRITICAL: Pass self._driver (org-specific driver) - otherwise Graphiti
                # uses the default driver which points to "default" graph, not our org graph
                search_results = await self._client.client.search_(
                    query=safe_query,
                    config=NODE_HYBRID_SEARCH_RRF,
                    group_ids=[self._group_id],
                    driver=self._driver,
                )

                _t1 = _time.perf_counter()
                log.debug(
                    "search_timing",
                    step="graphiti_search",
                    ms=round((_t1 - _t0) * 1000),
                    nodes=len(search_results.nodes),
                    episodes=len(search_results.episodes),
                )

                # Process EntityNodes with their reranker scores
                for i, node in enumerate(search_results.nodes):
                    try:
                        # Filter by group_id (multi-tenancy)
                        if node.group_id != self._group_id:
                            continue

                        entity = self.node_to_entity(node)

                        # Filter by entity types if specified
                        if entity_types and entity.entity_type not in entity_types:
                            continue

                        # Use reranker score if available, otherwise position-based
                        if i < len(search_results.node_reranker_scores):
                            score = search_results.node_reranker_scores[i]
                        else:
                            score = 1.0 / (i + 1)

                        results.append((entity, score))
                    except Exception as e:
                        log.debug("Failed to convert EntityNode", error=str(e), node=node.uuid)

                # Also check episodes (for nodes created via add_episode)
                for i, node in enumerate(search_results.episodes):
                    try:
                        if node.group_id != self._group_id:
                            continue

                        entity = self._episodic_to_entity(node)

                        if entity_types and entity.entity_type not in entity_types:
                            continue

                        if i < len(search_results.episode_reranker_scores):
                            score = search_results.episode_reranker_scores[i]
                        else:
                            score = 1.0 / (i + 1)

                        results.append((entity, score))
                    except Exception as e:
                        log.debug("Failed to convert EpisodicNode", error=str(e))
            except Exception:
                raise

            # Sort by score and limit results
            results.sort(key=lambda x: x[1], reverse=True)

            if not self._results_contain_exact_name_match(results, query):
                exact_name_results = await self._exact_name_search(
                    query=query,
                    entity_types=entity_types,
                    limit=limit,
                )
                if exact_name_results:
                    results = self._merge_ranked_results(exact_name_results, results, limit)
                else:
                    results = results[:limit]
            else:
                results = results[:limit]

            if not results:
                results = await self._fallback_text_search(
                    query=query,
                    entity_types=entity_types,
                    limit=limit,
                )

            log.info("Search completed", **query_log_fields(query), results_count=len(results))
            return results

        except Exception as e:
            error_type = type(e).__name__
            log.warning("Search failed", **query_log_fields(query), error_type=error_type)
            raise SearchError("Search failed", details={"error_type": error_type}) from None

    def _results_contain_exact_name_match(
        self,
        results: list[tuple[Entity, float]],
        query: str,
    ) -> bool:
        normalized_query = query.strip().lower()
        if not normalized_query:
            return False

        return any(entity.name.strip().lower() == normalized_query for entity, _ in results)

    def _merge_ranked_results(
        self,
        prioritized_results: list[tuple[Entity, float]],
        secondary_results: list[tuple[Entity, float]],
        limit: int,
    ) -> list[tuple[Entity, float]]:
        seen_ids: set[str] = set()
        merged: list[tuple[Entity, float]] = []

        for entity, score in prioritized_results + secondary_results:
            if entity.id in seen_ids:
                continue
            seen_ids.add(entity.id)
            merged.append((entity, score))
            if len(merged) >= limit:
                break

        return merged

    async def search_exact_name(
        self,
        query: str,
        entity_types: list[EntityType] | None = None,
        limit: int = 10,
    ) -> list[tuple[Entity, float]]:
        """Return exact title matches scoped to the current org graph."""
        return await self._exact_name_search(
            query=query,
            entity_types=entity_types,
            limit=limit,
        )

    async def _exact_name_search(
        self,
        *,
        query: str,
        entity_types: list[EntityType] | None,
        limit: int,
    ) -> list[tuple[Entity, float]]:
        normalized_query = query.strip().lower()
        if not normalized_query:
            return []

        if self._surreal_entity_node_ops() is not None:
            exact_results: list[tuple[Entity, float]] = []
            if self._surreal_should_search_entities(entity_types):
                records = await self._surreal_search_entity_records(
                    query_lower=normalized_query,
                    entity_types=entity_types,
                    limit=limit,
                    exact_name_only=True,
                )
                for record in records:
                    try:
                        exact_results.append(
                            (self._coerce_entity(self._record_to_entity(record)), 2.0)
                        )
                    except Exception as exc:
                        log.debug("surreal_exact_name_record_failed", error=str(exc))

            if (
                len(exact_results) < limit
                and self._surreal_should_search_episodes(entity_types)
            ):
                records = await self._surreal_search_episode_records(
                    query_lower=normalized_query,
                    limit=limit - len(exact_results),
                    exact_name_only=True,
                )
                for record in records:
                    try:
                        entity = self._coerce_entity(self._record_to_episode_entity(record))
                        if entity_types and entity.entity_type not in entity_types:
                            continue
                        exact_results.append((entity, 2.0))
                    except Exception as exc:
                        log.debug("surreal_exact_episode_record_failed", error=str(exc))

            return exact_results[:limit]

        self._assert_legacy_fallback_allowed("search_exact_name")

        params: dict[str, Any] = {
            "group_id": self._group_id,
            "query_lower": normalized_query,
            "limit": limit,
        }
        where_clauses = [
            "n.group_id = $group_id",
            "toLower(coalesce(n.name, '')) = $query_lower",
        ]

        if entity_types:
            params["entity_types"] = [entity_type.value for entity_type in entity_types]
            where_clauses.append("n.entity_type IN $entity_types")

        query_text = f"""
            MATCH (n)
            WHERE {" AND ".join(where_clauses)}
            RETURN n.uuid AS uuid,
                   n.name AS name,
                   n.entity_type AS entity_type,
                   n.group_id AS group_id,
                   n.content AS content,
                   n.description AS description,
                   n.summary AS summary,
                   n.metadata AS metadata,
                   n.created_at AS created_at,
                   n.updated_at AS updated_at,
                   2.0 AS score
            ORDER BY n.created_at DESC, n.uuid DESC
            LIMIT $limit
        """

        result = await self._driver.execute_query(query_text, **params)
        records = GraphClient.normalize_result(result)

        exact_results: list[tuple[Entity, float]] = []
        for record in records:
            try:
                entity = self._coerce_entity(self._record_to_entity(record))
                exact_results.append((entity, float(record.get("score") or 0.0)))
            except Exception as exc:
                log.debug("exact_name_search_record_failed", error=str(exc))

        if exact_results:
            log.info(
                "exact_name_search_used",
                **query_log_fields(query),
                results_count=len(exact_results),
            )

        return exact_results

    async def _fallback_text_search(
        self,
        *,
        query: str,
        entity_types: list[EntityType] | None,
        limit: int,
    ) -> list[tuple[Entity, float]]:
        """Fallback search path using direct graph scans when hybrid indexes miss."""
        normalized_query = query.strip().lower()
        if not normalized_query:
            return []

        if self._surreal_entity_node_ops() is not None:
            records: list[dict[str, Any]] = []
            candidate_limit = min(max(limit * 4, 20), 200)
            if self._surreal_should_search_entities(entity_types):
                records.extend(
                    await self._surreal_search_entity_records(
                        query_lower=normalized_query,
                        entity_types=entity_types,
                        limit=candidate_limit,
                    )
                )
            if self._surreal_should_search_episodes(entity_types):
                records.extend(
                    await self._surreal_search_episode_records(
                        query_lower=normalized_query,
                        limit=candidate_limit,
                    )
                )

            found_fulltext_records = bool(records)
            if not found_fulltext_records and self._surreal_should_search_entities(entity_types):
                records.extend(
                    await self._surreal_scan_recent_entity_records(
                        entity_types=entity_types,
                        limit=candidate_limit,
                    )
                )
            if not found_fulltext_records and self._surreal_should_search_episodes(entity_types):
                records.extend(
                    await self._surreal_scan_recent_episode_records(limit=candidate_limit)
                )

            fallback_results: list[tuple[Entity, float]] = []
            query_terms = _search_terms(normalized_query)
            for record in records:
                try:
                    if "entity_type" in record:
                        entity = self._coerce_entity(self._record_to_entity(record))
                    else:
                        entity = self._coerce_entity(self._record_to_episode_entity(record))
                    if entity_types and entity.entity_type not in entity_types:
                        continue
                except Exception as exc:
                    log.debug("surreal_fallback_text_record_failed", error=str(exc))
                    continue

                name = entity.name.strip().lower()
                description = (entity.description or "").lower()
                content = (entity.content or "").lower()

                if name == normalized_query:
                    score = 1.0
                elif name.startswith(normalized_query):
                    score = 0.95
                elif normalized_query in name:
                    score = 0.9
                elif normalized_query in description:
                    score = 0.75
                elif normalized_query in content:
                    score = 0.6
                elif "search_score" in record:
                    score = 0.65
                else:
                    recall_score = _token_recall_score(
                        terms=query_terms,
                        name=name,
                        description=description,
                        content=content,
                    )
                    if recall_score is None:
                        continue
                    score = recall_score
                fallback_results.append((entity, score))

            fallback_results.sort(
                key=lambda item: (item[1], *self._entity_sort_key(item[0])), reverse=True
            )
            return fallback_results[:limit]

        self._assert_legacy_fallback_allowed("text_search")

        params: dict[str, Any] = {
            "group_id": self._group_id,
            "query_lower": normalized_query,
            "limit": limit,
        }
        where_clauses = [
            "n.group_id = $group_id",
            """(
                toLower(coalesce(n.name, '')) CONTAINS $query_lower
                OR toLower(coalesce(n.description, '')) CONTAINS $query_lower
                OR toLower(coalesce(n.content, '')) CONTAINS $query_lower
            )""",
        ]

        if entity_types:
            params["entity_types"] = [entity_type.value for entity_type in entity_types]
            where_clauses.append("n.entity_type IN $entity_types")

        query_text = f"""
            MATCH (n)
            WHERE {" AND ".join(where_clauses)}
            WITH n,
                 CASE
                     WHEN toLower(coalesce(n.name, '')) = $query_lower THEN 1.0
                     WHEN toLower(coalesce(n.name, '')) STARTS WITH $query_lower THEN 0.95
                     WHEN toLower(coalesce(n.name, '')) CONTAINS $query_lower THEN 0.9
                     WHEN toLower(coalesce(n.description, '')) CONTAINS $query_lower THEN 0.75
                     ELSE 0.6
                 END AS score
            RETURN n.uuid AS uuid,
                   n.name AS name,
                   n.entity_type AS entity_type,
                   n.group_id AS group_id,
                   n.content AS content,
                   n.description AS description,
                   n.summary AS summary,
                   n.metadata AS metadata,
                   n.created_at AS created_at,
                   n.updated_at AS updated_at,
                   score AS score
            ORDER BY score DESC, n.created_at DESC, n.uuid DESC
            LIMIT $limit
        """

        result = await self._driver.execute_query(query_text, **params)
        records = GraphClient.normalize_result(result)

        fallback_results: list[tuple[Entity, float]] = []
        for record in records:
            try:
                entity = self._coerce_entity(self._record_to_entity(record))
                fallback_results.append((entity, float(record.get("score") or 0.0)))
            except Exception as exc:
                log.debug("fallback_text_search_record_failed", error=str(exc))

        if fallback_results:
            log.info(
                "fallback_text_search_used",
                **query_log_fields(query),
                results_count=len(fallback_results),
            )

        return fallback_results

    async def update(self, entity_id: str, updates: dict[str, Any]) -> Entity | None:
        """Update an existing entity with partial updates.

        Args:
            entity_id: The entity's unique identifier.
            updates: Dictionary of fields to update.

        Returns:
            The updated entity, or None if update failed.

        Raises:
            EntityNotFoundError: If entity doesn't exist.
        """
        log.info("Updating entity", entity_id=entity_id, fields=list(updates.keys()))

        try:
            surreal_entity_ops = self._surreal_entity_node_ops()
            surreal_node: EntityNode | None = None
            if surreal_entity_ops is not None:
                surreal_node = await surreal_entity_ops.get_by_uuid(self._driver, entity_id)
                if surreal_node.group_id != self._group_id:
                    raise EntityNotFoundError("Entity", entity_id)
                existing = self.node_to_entity(surreal_node)
            else:
                self._assert_legacy_fallback_allowed("update")
                # Retrieve the existing entity
                existing = await self.get(entity_id)
                if not existing:
                    raise EntityNotFoundError("Entity", entity_id)

            merged_metadata = {**(existing.metadata or {}), **(updates.get("metadata") or {})}

            # Any non-core fields should be preserved in metadata so filters can read them
            # Exclude embedding - it's stored as a direct node property, not in metadata
            # (embeddings in metadata bloat Graphiti's LLM context ~30KB per entity)
            excluded_keys = {
                "name",
                "description",
                "content",
                "metadata",
                "source_file",
                "embedding",
            }
            merged_metadata.update(
                {key: value for key, value in updates.items() if key not in excluded_keys}
            )

            # Collect all properties, preserving existing values when not updated
            updated_entity = Entity(
                id=existing.id,
                entity_type=existing.entity_type,
                name=updates.get("name", existing.name),
                description=updates.get("description", existing.description),
                content=updates.get("content", existing.content),
                metadata=merged_metadata,
                created_at=existing.created_at,
                updated_at=datetime.now(UTC),
                source_file=updates.get("source_file", existing.source_file),
            )

            if surreal_entity_ops is not None:
                if surreal_node is None:
                    raise EntityNotFoundError("Entity", entity_id)

                marker_key = (
                    "_generated" if surreal_node.attributes.get("_generated") else "_direct_insert"
                )
                surreal_node.name = updated_entity.name
                surreal_node.labels = [updated_entity.entity_type.value]
                surreal_node.summary = (
                    updated_entity.description[:500]
                    if updated_entity.description
                    else updated_entity.name
                )
                surreal_node.attributes = self._build_entity_node_attributes(
                    updated_entity,
                    marker_key=marker_key,
                )

                if "embedding" in updates:
                    embedding = updates.get("embedding")
                    surreal_node.name_embedding = embedding if isinstance(embedding, list) else None

                await surreal_entity_ops.save(self._driver, surreal_node)
            else:
                self._assert_legacy_fallback_allowed("update")
                # Persist updates in-place to avoid changing UUIDs
                await self._persist_entity_attributes(entity_id, updated_entity)

                # Store embedding as direct node property (not in metadata to avoid bloating LLM context)
                if "embedding" in updates:
                    embedding = updates.get("embedding")

                    # FalkorDB expects Vectorf32 for vector ops. Casting via vecf32() avoids
                    # "expected Null or Vectorf32 but was List" type mismatches.
                    if embedding and isinstance(embedding, list):
                        await self._driver.execute_query(
                            "MATCH (n {uuid: $entity_id}) SET n.name_embedding = vecf32($embedding)",
                            entity_id=entity_id,
                            embedding=embedding,
                        )
                        log.debug("Stored embedding on node", entity_id=entity_id)
                    else:
                        # Allow clearing embeddings by passing null/empty.
                        await self._driver.execute_query(
                            "MATCH (n {uuid: $entity_id}) SET n.name_embedding = NULL",
                            entity_id=entity_id,
                        )
                        log.debug("Cleared embedding on node", entity_id=entity_id)

            log.info("Entity updated successfully", entity_id=entity_id)
            return updated_entity

        except EntityNotFoundError:
            raise
        except Exception as e:
            log.exception("Failed to update entity", entity_id=entity_id, error=str(e))
            raise

    async def delete(self, entity_id: str) -> bool:
        """Delete an entity from the graph using Graphiti's node APIs.

        Tries EntityNode first, then EpisodicNode deletion.

        Args:
            entity_id: The entity's unique identifier.

        Returns:
            True if deletion succeeded, False otherwise.
        """
        log.info("Deleting entity", entity_id=entity_id)

        try:
            surreal_entity_ops = self._surreal_entity_node_ops()
            surreal_episode_ops = self._surreal_episode_node_ops()
            # Try to delete as EntityNode first
            try:
                if surreal_entity_ops is not None:
                    node = await surreal_entity_ops.get_by_uuid(self._driver, entity_id)
                else:
                    self._assert_legacy_fallback_allowed("delete")
                    node = await EntityNode.get_by_uuid(self._driver, entity_id)
                if node and node.group_id == self._group_id:
                    if surreal_entity_ops is not None:
                        await surreal_entity_ops.delete(self._driver, node)
                    else:
                        await node.delete(self._driver)
                    log.info("Entity deleted via EntityNode", entity_id=entity_id)
                    return True
            except Exception as e:
                self._raise_if_surreal_fallback_guard(e)
                log.debug(
                    "EntityNode delete failed, trying EpisodicNode",
                    entity_id=entity_id,
                    error=str(e),
                )

            # Try to delete as EpisodicNode
            try:
                if surreal_episode_ops is not None:
                    episodic = await surreal_episode_ops.get_by_uuid(self._driver, entity_id)
                else:
                    self._assert_legacy_fallback_allowed("delete")
                    episodic = await EpisodicNode.get_by_uuid(self._driver, entity_id)
                if episodic and episodic.group_id == self._group_id:
                    if surreal_episode_ops is not None:
                        await surreal_episode_ops.delete(self._driver, episodic)
                    else:
                        await episodic.delete(self._driver)
                    log.info("Entity deleted via EpisodicNode", entity_id=entity_id)
                    return True
            except Exception as e:
                self._raise_if_surreal_fallback_guard(e)
                log.debug("EpisodicNode delete failed", entity_id=entity_id, error=str(e))

            raise EntityNotFoundError("Entity", entity_id)

        except EntityNotFoundError:
            raise
        except Exception as e:
            self._raise_if_surreal_fallback_guard(e)
            log.exception("Failed to delete entity", entity_id=entity_id, error=str(e))
            return False

    async def list_by_type(
        self,
        entity_type: EntityType,
        limit: int = 50,
        offset: int = 0,
        *,
        project_id: str | None = None,
        epic_id: str | None = None,
        no_epic: bool = False,
        status: str | None = None,
        priority: str | None = None,
        complexity: str | None = None,
        feature: str | None = None,
        tags: list[str] | None = None,
        include_archived: bool = False,
    ) -> list[Entity]:
        """List all entities of a specific type using direct Cypher query.

        Structured task and epic fields are persisted as top-level node properties
        for fast filtering. We still re-check metadata in Python so legacy rows
        without normalized properties remain readable.

        Args:
            entity_type: The type of entities to list.
            limit: Maximum results to return.
            offset: Pagination offset.
            project_id: Filter by project ID.
            epic_id: Filter by epic ID (uses BELONGS_TO relationship).
            no_epic: Filter for entities without an epic (mutually exclusive with epic_id).
            status: Filter by status (for tasks, parsed from metadata).
            priority: Filter by priority (for tasks, parsed from metadata).
            complexity: Filter by complexity (for tasks, parsed from metadata).
            feature: Filter by feature area (for tasks, parsed from metadata).
            tags: Filter by tags (matches if ANY tag present, parsed from metadata).
            include_archived: Include archived entities.

        Returns:
            List of entities.
        """
        log.debug(
            "Listing entities",
            entity_type=entity_type,
            limit=limit,
            offset=offset,
            project_id=project_id,
            epic_id=epic_id,
            status=status,
            priority=priority,
        )

        params: dict[str, Any] = {
            "entity_type": entity_type.value,
            "group_id": self._group_id,
        }
        match_clause = "MATCH (n)"
        where_clauses = [
            "n.entity_type = $entity_type",
            "n.group_id = $group_id",
        ]

        status_list = [s.strip().lower() for s in status.split(",")] if status else []
        priority_list = [p.strip().lower() for p in priority.split(",")] if priority else []
        complexity_list = [c.strip().lower() for c in complexity.split(",")] if complexity else []

        if self._surreal_entity_node_ops() is not None:
            try:
                return await self._surreal_list_entities_direct(
                    entity_type=entity_type,
                    limit=limit,
                    offset=offset,
                    project_id=project_id,
                    epic_id=epic_id,
                    no_epic=no_epic,
                    status_values=status_list,
                    priority_values=priority_list,
                    complexity_values=complexity_list,
                    feature=feature,
                    tags=tags,
                    include_archived=include_archived,
                )
            except Exception as e:
                log.exception("Failed to list entities", entity_type=entity_type, error=str(e))
                return []

        self._assert_legacy_fallback_allowed("list_by_type")

        # Use BELONGS_TO relationship for epic filtering (most reliable)
        if epic_id:
            match_clause = "MATCH (n)-[:BELONGS_TO]->(e)"
            params["epic_id"] = epic_id
            where_clauses.append("e.uuid = $epic_id")

        if project_id:
            params["project_id"] = project_id
            legacy_project_params, legacy_project_match = _metadata_json_contains_params(
                "legacy_project",
                "project_id",
                project_id,
            )
            params.update(legacy_project_params)
            where_clauses.append(
                f"""(
                    n.project_id = $project_id
                    OR (
                        (n.project_id IS NULL OR n.project_id = '')
                        AND ({legacy_project_match})
                    )
                )"""
            )

        if status_list:
            params["status_values"] = status_list
            legacy_status_params, legacy_status_match = _metadata_json_contains_any_params(
                "legacy_status",
                "status",
                status_list,
            )
            params.update(legacy_status_params)
            where_clauses.append(
                f"""(
                    toLower(n.status) IN $status_values
                    OR (
                        (n.status IS NULL OR n.status = '')
                        AND ({legacy_status_match})
                    )
                )"""
            )

        if priority_list:
            params["priority_values"] = priority_list
            legacy_priority_params, legacy_priority_match = _metadata_json_contains_any_params(
                "legacy_priority",
                "priority",
                priority_list,
            )
            params.update(legacy_priority_params)
            where_clauses.append(
                f"""(
                    toLower(n.priority) IN $priority_values
                    OR (
                        (n.priority IS NULL OR n.priority = '')
                        AND ({legacy_priority_match})
                    )
                )"""
            )

        if complexity_list:
            params["complexity_values"] = complexity_list
            legacy_complexity_params, legacy_complexity_match = _metadata_json_contains_any_params(
                "legacy_complexity",
                "complexity",
                complexity_list,
            )
            params.update(legacy_complexity_params)
            where_clauses.append(
                f"""(
                    toLower(n.complexity) IN $complexity_values
                    OR (
                        (n.complexity IS NULL OR n.complexity = '')
                        AND ({legacy_complexity_match})
                    )
                )"""
            )

        if feature:
            params["feature"] = feature
            where_clauses.append("(n.feature = $feature OR n.feature IS NULL)")

        if tags:
            params["tags"] = tags
            where_clauses.append(
                "(n.tags IS NULL OR any(tag IN coalesce(n.tags, []) WHERE tag IN $tags))"
            )

        if no_epic:
            where_clauses.append("(n.epic_id IS NULL OR n.epic_id = '')")

        if not include_archived:
            where_clauses.append("(n.status IS NULL OR toLower(n.status) <> 'archived')")

        requires_legacy_rechecks = any(
            [
                project_id is not None,
                bool(status_list),
                bool(priority_list),
                bool(complexity_list),
                feature is not None,
                bool(tags),
                no_epic,
                not include_archived,
            ]
        )
        target_count = offset + limit if requires_legacy_rechecks else limit
        page_size = min(max(target_count, 1), 1000)
        params["query_limit"] = page_size
        params["query_offset"] = 0 if requires_legacy_rechecks else offset

        query = f"""
            {match_clause}
            WHERE {" AND ".join(where_clauses)}
            RETURN n.uuid AS uuid,
                   n.name AS name,
                   n.entity_type AS entity_type,
                   n.group_id AS group_id,
                   n.content AS content,
                   n.description AS description,
                   n.summary AS summary,
                   n.metadata AS metadata,
                   n.created_at AS created_at,
                   n.updated_at AS updated_at,
                   labels(n) AS labels
            ORDER BY n.created_at DESC, n.uuid DESC
            SKIP $query_offset
            LIMIT $query_limit
        """

        try:
            entities: list[Entity] = []
            seen_entity_ids: set[str] = set()
            seen_pages: set[tuple[str | None, ...]] = set()
            while len(entities) < target_count:
                result = await self._driver.execute_query(query, **params)

                # Handle FalkorDB result format using normalize helper
                records = GraphClient.normalize_result(result)
                if not records:
                    break

                page_signature = tuple(record.get("uuid") for record in records)
                if page_signature in seen_pages:
                    log.warning(
                        "list_by_type repeated page, stopping pagination",
                        entity_type=entity_type,
                        query_offset=params["query_offset"],
                        query_limit=params["query_limit"],
                    )
                    break
                seen_pages.add(page_signature)

                for record in records:
                    record_uuid = record.get("uuid")
                    if isinstance(record_uuid, str) and record_uuid in seen_entity_ids:
                        continue
                    try:
                        entity = self._record_to_entity(record)
                        entity = self._coerce_entity(entity)

                        # Parse metadata for filtering (stored as JSON string)
                        metadata = entity.metadata or {}

                        # Filter by project_id from metadata
                        if project_id and metadata.get("project_id") != project_id:
                            continue

                        # Filter by status from metadata (supports comma-separated)
                        if status:
                            entity_status = metadata.get("status", "").lower()
                            if entity_status not in status_list:
                                continue

                        # Filter by priority from metadata (supports comma-separated)
                        if priority:
                            entity_priority = metadata.get("priority", "").lower()
                            if entity_priority not in priority_list:
                                continue

                        # Filter by complexity from metadata (supports comma-separated)
                        if complexity:
                            entity_complexity = metadata.get("complexity", "").lower()
                            if entity_complexity not in complexity_list:
                                continue

                        # Filter by feature from metadata
                        if feature:
                            entity_feature = metadata.get("feature")
                            if entity_feature != feature:
                                continue

                        # Filter by tags from metadata (match if ANY tag present)
                        if tags:
                            entity_tags = metadata.get("tags", [])
                            if not any(t in entity_tags for t in tags):
                                continue

                        # Filter for entities without an epic
                        if no_epic:
                            entity_epic = metadata.get("epic_id")
                            if entity_epic:  # Has an epic, skip it
                                continue

                        # Filter archived unless include_archived is True
                        if not include_archived:
                            entity_status = metadata.get("status")
                            if entity_status == "archived":
                                continue

                        if entity.id in seen_entity_ids:
                            continue

                        seen_entity_ids.add(entity.id)
                        entities.append(entity)

                    except Exception as e:
                        log.debug("Failed to convert record to entity", error=str(e))

                params["query_offset"] += len(records)
                if len(records) < params["query_limit"]:
                    break

            log.debug(
                "Listed entities",
                entity_type=entity_type,
                returned=min(
                    len(entities[offset : offset + limit])
                    if requires_legacy_rechecks
                    else len(entities[:limit]),
                    limit,
                ),
            )
            if requires_legacy_rechecks:
                return entities[offset : offset + limit]
            return entities[:limit]

        except Exception as e:
            log.exception("Failed to list entities", entity_type=entity_type, error=str(e))
            return []

    async def list_all(
        self,
        limit: int = 1000,
        offset: int = 0,
        *,
        include_archived: bool = False,
    ) -> list[Entity]:
        """List all entities regardless of type using a single query.

        Args:
            limit: Maximum results to return.
            offset: Pagination offset.
            include_archived: Include archived entities.

        Returns:
            List of entities.
        """
        log.debug("Listing all entities", limit=limit, offset=offset)

        try:
            if self._surreal_entity_node_ops() is not None:
                return await self._surreal_list_entities_direct(
                    limit=limit,
                    offset=offset,
                    include_archived=include_archived,
                )
            self._assert_legacy_fallback_allowed("list_all")
            entities = await self._list_all_via_type_scans(
                include_archived=include_archived,
            )
            sliced_entities = entities[offset : offset + limit]
            log.debug("Listed all entities", returned=len(sliced_entities))
            return sliced_entities

        except Exception as e:
            self._raise_if_surreal_fallback_guard(e)
            log.exception("Failed to list all entities", error=str(e))
            return []

    async def get_tasks_for_epic(
        self,
        epic_id: str,
        status: str | None = None,
        limit: int = 100,
    ) -> list[Entity]:
        """Get all tasks belonging to an epic.

        Args:
            epic_id: The epic's unique identifier.
            status: Optional status filter (todo, doing, done, etc.).
            limit: Maximum results to return.

        Returns:
            List of Task entities belonging to the epic.
        """
        log.debug("Fetching tasks for epic", epic_id=epic_id, status=status)

        try:
            if self._surreal_entity_node_ops() is not None:
                return await self.list_by_type(
                    EntityType.TASK,
                    epic_id=epic_id,
                    status=status,
                    limit=limit,
                    include_archived=True,
                )

            # Use BELONGS_TO relationship to find tasks in epic
            # Status is stored in metadata JSON, so we filter in Python if needed
            query = """
                MATCH (n)-[:BELONGS_TO]->(e)
                WHERE n.entity_type = 'task'
                  AND n.group_id = $group_id
                  AND e.uuid = $epic_id
                RETURN n.uuid AS uuid,
                       n.name AS name,
                       n.entity_type AS entity_type,
                       n.group_id AS group_id,
                       n.content AS content,
                       n.description AS description,
                       n.summary AS summary,
                       n.metadata AS metadata,
                       n.created_at AS created_at
                ORDER BY n.created_at DESC
                LIMIT $limit
            """

            params: dict[str, Any] = {
                "group_id": self._group_id,
                "epic_id": epic_id,
                "limit": limit,
            }

            result = await self._driver.execute_query(query, **params)

            entities: list[Entity] = []
            records = GraphClient.normalize_result(result)
            for record in records:
                try:
                    entity = self._record_to_entity(record)
                    # Filter by status in Python since it's in metadata
                    if status:
                        entity_status = entity.metadata.get("status") if entity.metadata else None
                        if entity_status != status:
                            continue
                    entities.append(entity)
                except Exception as e:
                    log.debug("Failed to convert record", error=str(e))

            log.debug("Fetched tasks for epic", epic_id=epic_id, count=len(entities))
            return entities

        except Exception as e:
            log.exception("Failed to get tasks for epic", epic_id=epic_id, error=str(e))
            return []

    async def get_epic_progress(self, epic_id: str) -> dict[str, Any]:
        """Get progress statistics for an epic.

        Args:
            epic_id: The epic's unique identifier.

        Returns:
            Dict with total_tasks, completed_tasks, in_progress_tasks, and completion_pct.
        """
        log.debug("Getting epic progress", epic_id=epic_id)

        try:
            if self._surreal_entity_node_ops() is not None:
                tasks = await self.list_by_type(
                    EntityType.TASK,
                    epic_id=epic_id,
                    limit=10_000,
                    include_archived=True,
                )
                total = len(tasks)
                done = 0
                doing = 0
                blocked = 0
                review = 0
                for task in tasks:
                    status_value = str((task.metadata or {}).get("status") or "").lower()
                    if status_value == "done":
                        done += 1
                    elif status_value == "doing":
                        doing += 1
                    elif status_value == "blocked":
                        blocked += 1
                    elif status_value == "review":
                        review += 1

                return {
                    "total_tasks": total,
                    "completed_tasks": done,
                    "in_progress_tasks": doing,
                    "blocked_tasks": blocked,
                    "in_review_tasks": review,
                    "completion_pct": round((done / total * 100) if total > 0 else 0, 1),
                }

            # Use BELONGS_TO relationship to find tasks, then count by status in Python
            # since status is stored in metadata JSON
            result = await self._driver.execute_query(
                """
                MATCH (n)-[:BELONGS_TO]->(e)
                WHERE n.entity_type = 'task'
                  AND n.group_id = $group_id
                  AND e.uuid = $epic_id
                RETURN n.metadata AS metadata
                """,
                group_id=self._group_id,
                epic_id=epic_id,
            )

            records = GraphClient.normalize_result(result)

            # Count statuses from metadata
            total = len(records)
            done = 0
            doing = 0
            blocked = 0
            review = 0

            for record in records:
                metadata = record.get("metadata")
                if isinstance(metadata, str):
                    try:
                        metadata = json.loads(metadata)
                    except json.JSONDecodeError:
                        metadata = {}
                status = metadata.get("status") if metadata else None
                if status == "done":
                    done += 1
                elif status == "doing":
                    doing += 1
                elif status == "blocked":
                    blocked += 1
                elif status == "review":
                    review += 1

            return {
                "total_tasks": total,
                "completed_tasks": done,
                "in_progress_tasks": doing,
                "blocked_tasks": blocked,
                "in_review_tasks": review,
                "completion_pct": round((done / total * 100) if total > 0 else 0, 1),
            }

        except Exception as e:
            log.exception("Failed to get epic progress", epic_id=epic_id, error=str(e))
            return {"total_tasks": 0, "completed_tasks": 0, "completion_pct": 0.0}

    async def get_project_summary(
        self,
        project_id: str,
        *,
        actionable_limit: int = 5,
        critical_limit: int = 3,
        epic_limit: int = 3,
    ) -> dict[str, Any]:
        """Get a rich summary of a project with actionable task highlights.

        Returns task counts by status and curated lists of tasks that need attention,
        prioritized by urgency: doing > blocked > review > recent.

        Args:
            project_id: The project's unique identifier.
            actionable_limit: Max number of actionable tasks to return.
            critical_limit: Max number of critical tasks to return.
            epic_limit: Max number of epics to return.

        Returns:
            Dict with:
                - status_counts: Dict of status -> count
                - total_tasks: Total task count
                - progress_pct: Completion percentage
                - actionable_tasks: List of tasks needing attention (dicts with id, name, status)
                - critical_tasks: List of critical/high priority tasks
                - epics: List of active epics with progress
        """
        log.debug("Getting project summary", project_id=project_id)

        try:
            if self._surreal_entity_node_ops() is not None:
                tasks: list[Entity] = []
                task_offset = 0
                task_page_size = 1000
                while True:
                    task_page = await self.list_by_type(
                        EntityType.TASK,
                        project_id=project_id,
                        limit=task_page_size,
                        offset=task_offset,
                        include_archived=True,
                    )
                    if not task_page:
                        break
                    tasks.extend(task_page)
                    if len(task_page) < task_page_size:
                        break
                    task_offset += len(task_page)

                status_counts: dict[str, int] = {}
                doing_tasks: list[dict[str, Any]] = []
                blocked_tasks: list[dict[str, Any]] = []
                review_tasks: list[dict[str, Any]] = []
                critical_tasks: list[dict[str, Any]] = []
                recent_tasks: list[dict[str, Any]] = []
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
                for pool in [doing_tasks, blocked_tasks, review_tasks, recent_tasks]:
                    for task_info in pool:
                        if len(actionable) >= actionable_limit:
                            break
                        if task_info["id"] not in [t["id"] for t in actionable]:
                            actionable.append(task_info)
                    if len(actionable) >= actionable_limit:
                        break

                total = sum(status_counts.values())
                done = status_counts.get("done", 0)
                epics: list[dict[str, Any]] = []
                for epic in await self.list_epics_for_project(project_id, limit=epic_limit):
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

                return {
                    "status_counts": status_counts,
                    "total_tasks": total,
                    "progress_pct": round((done / total * 100) if total > 0 else 0, 1),
                    "actionable_tasks": actionable,
                    "critical_tasks": critical_tasks,
                    "epics": epics,
                }

            legacy_project_params, legacy_project_match = _metadata_json_contains_params(
                "legacy_project",
                "project_id",
                project_id,
            )
            # Fetch all tasks for the project
            result = await self._driver.execute_query(
                f"""
                MATCH (n)
                WHERE n.entity_type = 'task'
                  AND n.group_id = $group_id
                  AND (
                      n.project_id = $project_id
                      OR (
                          (n.project_id IS NULL OR n.project_id = '')
                          AND ({legacy_project_match})
                      )
                  )
                RETURN n.uuid AS uuid,
                       n.name AS name,
                       n.project_id AS project_id,
                       n.status AS status,
                       n.priority AS priority,
                       n.epic_id AS epic_id,
                       n.metadata AS metadata,
                       n.updated_at AS updated_at
                ORDER BY n.updated_at DESC
                """,
                group_id=self._group_id,
                project_id=project_id,
                **legacy_project_params,
            )

            records = GraphClient.normalize_result(result)

            # Count by status and collect actionable/critical tasks
            status_counts: dict[str, int] = {}
            doing_tasks: list[dict[str, Any]] = []
            blocked_tasks: list[dict[str, Any]] = []
            review_tasks: list[dict[str, Any]] = []
            critical_tasks: list[dict[str, Any]] = []
            recent_tasks: list[dict[str, Any]] = []
            epic_progress: dict[str, dict[str, int]] = {}

            for record in records:
                metadata = record.get("metadata")
                if isinstance(metadata, str):
                    try:
                        metadata = json.loads(metadata)
                    except json.JSONDecodeError:
                        metadata = {}

                task_project_id = record.get("project_id") or (
                    metadata.get("project_id") if metadata else None
                )
                if task_project_id != project_id:
                    continue

                status = (
                    record.get("status") or (metadata.get("status") if metadata else None) or "todo"
                )
                priority = (
                    record.get("priority") or (metadata.get("priority") if metadata else None) or ""
                )
                name = record.get("name") or ""
                epic_id = record.get("epic_id") or (metadata.get("epic_id") if metadata else None)

                status_counts[status] = status_counts.get(status, 0) + 1

                if epic_id:
                    counters = epic_progress.setdefault(
                        str(epic_id),
                        {"total_tasks": 0, "completed_tasks": 0},
                    )
                    counters["total_tasks"] += 1
                    if status == "done":
                        counters["completed_tasks"] += 1

                task_info = {
                    "id": record.get("uuid"),
                    "name": name,
                    "status": status,
                    "priority": priority,
                }

                # Check if task is critical (not done/archived)
                is_critical = (
                    priority.lower() in ("critical", "high") or "CRITICAL" in name.upper()
                ) and status not in ("done", "archived")

                if is_critical and len(critical_tasks) < critical_limit:
                    critical_tasks.append(task_info)

                # Collect actionable tasks by priority
                if status == "doing" and len(doing_tasks) < actionable_limit:
                    doing_tasks.append(task_info)
                elif status == "blocked" and len(blocked_tasks) < actionable_limit:
                    blocked_tasks.append(task_info)
                elif status == "review" and len(review_tasks) < actionable_limit:
                    review_tasks.append(task_info)
                elif len(recent_tasks) < actionable_limit:
                    recent_tasks.append(task_info)

            # Build prioritized actionable list: doing > blocked > review > recent
            actionable: list[dict[str, Any]] = []
            for pool in [doing_tasks, blocked_tasks, review_tasks, recent_tasks]:
                for task in pool:
                    if len(actionable) >= actionable_limit:
                        break
                    # Dedupe by ID
                    if task["id"] not in [t["id"] for t in actionable]:
                        actionable.append(task)
                if len(actionable) >= actionable_limit:
                    break

            total = sum(status_counts.values())
            done = status_counts.get("done", 0)

            # Fetch epics for project
            epics: list[dict[str, Any]] = []
            try:
                epic_result = await self._driver.execute_query(
                    """
                    MATCH (e)
                    WHERE e.entity_type = 'epic'
                      AND e.group_id = $group_id
                      AND e.project_id = $project_id
                      AND (e.status IS NULL OR e.status <> 'archived')
                    RETURN e.uuid AS uuid,
                           e.name AS name,
                           coalesce(e.status, 'planning') AS status
                    ORDER BY e.priority ASC, e.created_at DESC
                    LIMIT $limit
                    """,
                    group_id=self._group_id,
                    project_id=project_id,
                    limit=epic_limit,
                )
                epic_records = GraphClient.normalize_result(epic_result)
                for rec in epic_records:
                    progress = epic_progress.get(str(rec.get("uuid")), {})
                    total_tasks = progress.get("total_tasks", 0)
                    completed_tasks = progress.get("completed_tasks", 0)
                    epics.append(
                        {
                            "id": rec.get("uuid"),
                            "name": rec.get("name"),
                            "status": rec.get("status") or "planning",
                            "progress_pct": round(
                                (completed_tasks / total_tasks * 100) if total_tasks > 0 else 0,
                                1,
                            ),
                            "total_tasks": total_tasks,
                        }
                    )
            except Exception as epic_err:
                log.debug("Failed to fetch epics", error=str(epic_err))

            return {
                "status_counts": status_counts,
                "total_tasks": total,
                "progress_pct": round((done / total * 100) if total > 0 else 0, 1),
                "actionable_tasks": actionable,
                "critical_tasks": critical_tasks,
                "epics": epics,
            }

        except Exception as e:
            log.exception("Failed to get project summary", project_id=project_id, error=str(e))
            return {
                "status_counts": {},
                "total_tasks": 0,
                "progress_pct": 0.0,
                "actionable_tasks": [],
                "critical_tasks": [],
                "epics": [],
            }

    async def list_epics_for_project(
        self,
        project_id: str,
        status: str | None = None,
        limit: int = 50,
    ) -> list[Entity]:
        """Get all epics belonging to a project.

        Args:
            project_id: The project's unique identifier.
            status: Optional status filter (planning, in_progress, completed, etc.).
            limit: Maximum results to return.

        Returns:
            List of Epic entities belonging to the project.
        """
        log.debug("Fetching epics for project", project_id=project_id, status=status)

        try:
            if self._surreal_entity_node_ops() is not None:
                return await self.list_by_type(
                    EntityType.EPIC,
                    project_id=project_id,
                    status=status,
                    limit=limit,
                )

            status_clause = "AND n.status = $status" if status else ""
            query = f"""
                MATCH (n)
                WHERE n.entity_type = 'epic'
                  AND n.group_id = $group_id
                  AND n.project_id = $project_id
                  {status_clause}
                RETURN n.uuid AS uuid,
                       n.name AS name,
                       n.entity_type AS entity_type,
                       n.group_id AS group_id,
                       n.content AS content,
                       n.description AS description,
                       n.metadata AS metadata,
                       n.created_at AS created_at,
                       n.status AS status,
                       n.priority AS priority,
                       n.project_id AS project_id,
                       n.total_tasks AS total_tasks,
                       n.completed_tasks AS completed_tasks
                ORDER BY n.priority ASC, n.created_at DESC
                LIMIT $limit
            """

            params: dict[str, Any] = {
                "group_id": self._group_id,
                "project_id": project_id,
                "limit": limit,
            }
            if status:
                params["status"] = status

            result = await self._driver.execute_query(query, **params)

            entities: list[Entity] = []
            records = GraphClient.normalize_result(result)
            for record in records:
                try:
                    entity = self._record_to_entity(record)
                    entities.append(entity)
                except Exception as e:
                    log.debug("Failed to convert record", error=str(e))

            log.debug("Fetched epics for project", project_id=project_id, count=len(entities))
            return entities

        except Exception as e:
            log.exception("Failed to list epics for project", project_id=project_id, error=str(e))
            return []

    async def get_notes_for_task(
        self,
        task_id: str,
        limit: int = 50,
    ) -> list[Entity]:
        """Get all notes belonging to a task, ordered by creation time (newest first).

        Args:
            task_id: The task's unique identifier.
            limit: Maximum results to return.

        Returns:
            List of Note entities belonging to the task.
        """
        log.debug("Fetching notes for task", task_id=task_id, limit=limit)

        try:
            if self._surreal_entity_node_ops() is not None:
                records = await self._surreal_select_entity_records(
                    entity_type=EntityType.NOTE,
                    task_id=task_id,
                    limit=limit,
                    offset=0,
                    include_archived=True,
                )
                notes: list[Entity] = []
                for record in records:
                    try:
                        notes.append(self._coerce_entity(self._record_to_entity(record)))
                    except Exception as e:
                        log.debug("Failed to convert note record", error=str(e))

                legacy_offset = 0
                page_size = max(limit, 100)
                seen_note_ids = {note.id for note in notes}
                if notes:
                    return notes[:limit]

                while len(notes) < limit:
                    legacy_records = await self._surreal_select_entity_records(
                        entity_type=EntityType.NOTE,
                        limit=page_size,
                        offset=legacy_offset,
                        include_archived=True,
                    )
                    if not legacy_records:
                        break

                    for record in legacy_records:
                        try:
                            note = self._coerce_entity(self._record_to_entity(record))
                        except Exception as e:
                            log.debug("Failed to convert note record", error=str(e))
                            continue
                        if note.id in seen_note_ids:
                            continue
                        if (note.metadata or {}).get("task_id") != task_id:
                            continue
                        seen_note_ids.add(note.id)
                        notes.append(note)
                        if len(notes) >= limit:
                            break

                    legacy_offset += len(legacy_records)
                    if len(legacy_records) < page_size:
                        break

                return notes[:limit]

            self._assert_legacy_fallback_allowed("get_notes_for_task")

            # Use BELONGS_TO relationship to find notes
            query = """
                MATCH (n)-[:BELONGS_TO]->(t)
                WHERE n.entity_type = 'note'
                  AND n.group_id = $group_id
                  AND t.uuid = $task_id
                RETURN n.uuid AS uuid,
                       n.name AS name,
                       n.entity_type AS entity_type,
                       n.group_id AS group_id,
                       n.content AS content,
                       n.description AS description,
                       n.metadata AS metadata,
                       n.created_at AS created_at
                ORDER BY n.created_at DESC
                LIMIT $limit
            """

            params: dict[str, Any] = {
                "group_id": self._group_id,
                "task_id": task_id,
                "limit": limit,
            }

            result = await self._driver.execute_query(query, **params)

            entities: list[Entity] = []
            records = GraphClient.normalize_result(result)
            for record in records:
                try:
                    entity = self._record_to_entity(record)
                    entities.append(entity)
                except Exception as e:
                    log.debug("Failed to convert note record", error=str(e))

            log.debug("Fetched notes for task", task_id=task_id, count=len(entities))
            return entities

        except RuntimeError:
            raise
        except Exception as e:
            log.exception("Failed to get notes for task", task_id=task_id, error=str(e))
            return []

    def _record_to_entity(self, node_data: dict[str, Any]) -> Entity:
        """Convert a raw database record to an Entity.

        Args:
            node_data: Raw node data from Cypher query.

        Returns:
            Entity instance.
        """
        import json

        # Parse metadata if it's a string
        metadata = node_data.get("metadata", {})
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except (json.JSONDecodeError, TypeError):
                metadata = {}
        elif not isinstance(metadata, dict):
            metadata = {}

        for field in (
            "project_id",
            "epic_id",
            "task_id",
            "status",
            "priority",
            "complexity",
            "feature",
            "tags",
        ):
            value = node_data.get(field)
            if value is not None and field not in metadata:
                metadata[field] = value

        # Get entity type
        entity_type_str = node_data.get("entity_type", "episode")
        try:
            entity_type = EntityType(entity_type_str)
        except ValueError:
            entity_type = EntityType.EPISODE

        # Build entity kwargs, only including datetime fields if present
        # Use `or ""` to convert None to empty string for required string fields
        entity_kwargs: dict[str, Any] = {
            "id": node_data.get("uuid") or "",
            "name": node_data.get("name") or "",
            "entity_type": entity_type,
            "description": node_data.get("description") or node_data.get("summary") or "",
            "content": node_data.get("content") or "",
            "organization_id": node_data.get("group_id") or metadata.get("organization_id"),
            "created_by": metadata.get("created_by"),
            "modified_by": metadata.get("modified_by"),
            "metadata": metadata,
        }
        embedding = node_data.get("name_embedding")
        if isinstance(embedding, list):
            entity_kwargs["embedding"] = embedding
        if created_at := self._parse_datetime(node_data.get("created_at")):
            entity_kwargs["created_at"] = created_at
        if updated_at := self._parse_datetime(node_data.get("updated_at")):
            entity_kwargs["updated_at"] = updated_at

        return Entity(**entity_kwargs)

    def _parse_datetime(self, value: Any) -> datetime | None:
        """Parse datetime from various formats."""
        if value is None:
            return None
        if isinstance(value, datetime):
            return value
        if isinstance(value, str):
            try:
                return datetime.fromisoformat(value)
            except ValueError:
                return None
        return None

    def _record_to_episode_entity(self, node_data: dict[str, Any]) -> Entity:
        name = str(node_data.get("name") or "")
        entity_type = EntityType.EPISODE
        if ":" in name:
            prefix, suffix = name.split(":", 1)
            with contextlib.suppress(ValueError):
                entity_type = EntityType(prefix.strip().lower())
                name = suffix.strip()

        entity_kwargs: dict[str, Any] = {
            "id": node_data.get("uuid") or "",
            "name": name,
            "entity_type": entity_type,
            "description": node_data.get("source_description") or "",
            "content": node_data.get("content") or "",
            "organization_id": node_data.get("group_id"),
            "metadata": {},
        }
        if created_at := self._parse_datetime(node_data.get("created_at")):
            entity_kwargs["created_at"] = created_at
            entity_kwargs["updated_at"] = created_at

        return Entity(**entity_kwargs)

    async def _persist_entity_attributes(self, entity_id: str, entity: Entity) -> None:
        """Persist normalized attributes/metadata on a node for reliable querying."""
        props = self._collect_properties(entity)
        # Use _entity_to_metadata to include model-specific fields (Task.status, etc.)
        metadata = self._entity_to_metadata(entity)

        # Remove None values to appease FalkorDB property constraints
        props = {k: v for k, v in props.items() if v is not None}

        props["updated_at"] = datetime.now(UTC).isoformat()
        if entity.created_at:
            props["created_at"] = entity.created_at.isoformat()

        import json

        metadata_json = json.dumps(metadata) if metadata else "{}"

        await self._driver.execute_query(
            """
            MATCH (n {uuid: $entity_id})
            SET n += $props,
                n.metadata = $metadata
            """,
            entity_id=entity_id,
            props=props,
            metadata=metadata_json,
        )

    def _collect_properties(self, entity: Entity) -> dict[str, Any]:
        """Collect structured properties for storage and filtering."""
        props: dict[str, Any] = {
            "uuid": entity.id,
            "entity_type": entity.entity_type.value,
            "name": entity.name,
            "description": entity.description,
            "content": entity.content,
            "source_file": entity.source_file,
        }

        def add_fields(fields: tuple[str, ...]) -> None:
            for field in fields:
                value = getattr(entity, field, None)
                if value is None:
                    value = entity.metadata.get(field)
                if value is not None:
                    props[field] = self._serialize_metadata_value(value)

        # Common optional fields
        for field in (
            "category",
            "languages",
            "tags",
            "organization_id",
            "created_by",
            "modified_by",
            "severity",
            "template_type",
            "file_extension",
            "steps",
            "required_tools",
            "estimated_minutes",
            "automation_level",
        ):
            value = getattr(entity, field, None)
            if value is None:
                value = entity.metadata.get(field)
            if value is not None:
                props[field] = self._serialize_metadata_value(value)

        # Task/Epic-specific fields (if present)
        task_fields = (
            "status",
            "priority",
            "task_order",
            "project_id",
            "epic_id",
            "feature",
            "sprint",
            "assignees",
            "due_date",
            "estimated_hours",
            "actual_hours",
            "domain",
            "technologies",
            "complexity",
            "branch_name",
            "commit_shas",
            "pr_url",
            "learnings",
            "blockers_encountered",
            "started_at",
            "completed_at",
            "reviewed_at",
        )
        add_fields(task_fields)

        procedure_fields = (
            "steps",
            "required_tools",
            "estimated_minutes",
            "automation_level",
        )
        add_fields(procedure_fields)

        note_fields = (
            "task_id",
            "author_type",
            "author_name",
        )
        add_fields(note_fields)

        return props

    def _serialize_metadata(self, metadata: dict[str, Any]) -> dict[str, Any]:
        """Convert metadata values to JSON-serializable forms."""
        serialized: dict[str, Any] = {}
        for key, value in metadata.items():
            serialized_value = self._serialize_metadata_value(value)
            if serialized_value is not None:
                serialized[key] = serialized_value
        return serialized

    def _serialize_metadata_value(self, value: Any) -> Any:
        """Normalize nested metadata values into JSON-safe primitives."""
        if value is None:
            return None
        if isinstance(value, datetime):
            return value.isoformat()
        if hasattr(value, "value"):
            return value.value
        if isinstance(value, BaseModel):
            return {
                key: serialized
                for key, nested in value.model_dump(mode="python").items()
                if (serialized := self._serialize_metadata_value(nested)) is not None
            }
        if isinstance(value, dict):
            return {
                key: serialized
                for key, nested in value.items()
                if (serialized := self._serialize_metadata_value(nested)) is not None
            }
        if isinstance(value, (list, tuple, set)):
            return [
                serialized
                for nested in value
                if (serialized := self._serialize_metadata_value(nested)) is not None
            ]
        return value

    def _entity_to_metadata(self, entity: Entity) -> dict[str, Any]:
        """Extract all entity fields as metadata for storage.

        This ensures model-specific fields (Task.status, Project.tech_stack, etc.)
        are persisted in the metadata JSON, not just the generic metadata dict.
        """
        # Start with explicit metadata
        metadata = dict(entity.metadata or {})

        # Add Task-specific fields
        if isinstance(entity, Task):
            metadata["status"] = entity.status.value if entity.status else "todo"
            metadata["priority"] = entity.priority.value if entity.priority else "medium"
            metadata["project_id"] = entity.project_id
            metadata["epic_id"] = entity.epic_id
            metadata["task_order"] = entity.task_order
            if entity.assignees:
                metadata["assignees"] = entity.assignees
            if entity.technologies:
                metadata["technologies"] = entity.technologies
            if entity.feature:
                metadata["feature"] = entity.feature
            if entity.domain:
                metadata["domain"] = entity.domain
            if entity.due_date:
                metadata["due_date"] = entity.due_date.isoformat()
            if entity.estimated_hours:
                metadata["estimated_hours"] = entity.estimated_hours
            if entity.branch_name:
                metadata["branch_name"] = entity.branch_name
            if entity.pr_url:
                metadata["pr_url"] = entity.pr_url

        # Add Project-specific fields
        elif isinstance(entity, Project):
            metadata["status"] = entity.status.value if entity.status else "active"
            if entity.tech_stack:
                metadata["tech_stack"] = entity.tech_stack
            if entity.repository_url:
                metadata["repository_url"] = entity.repository_url

        # Add Epic-specific fields
        elif isinstance(entity, Epic):
            metadata["status"] = entity.status.value if entity.status else "planning"
            metadata["priority"] = entity.priority.value if entity.priority else "medium"
            metadata["project_id"] = entity.project_id
            if entity.assignees:
                metadata["assignees"] = entity.assignees
            if entity.target_date:
                metadata["target_date"] = entity.target_date.isoformat()
            if entity.learnings:
                metadata["learnings"] = entity.learnings

        # Add Note-specific fields
        elif isinstance(entity, Note):
            metadata["task_id"] = entity.task_id
            metadata["author_type"] = entity.author_type.value if entity.author_type else "user"
            metadata["author_name"] = entity.author_name

        # Add Procedure-specific fields
        elif isinstance(entity, Procedure):
            if entity.steps:
                metadata["steps"] = entity.steps
            if entity.required_tools:
                metadata["required_tools"] = entity.required_tools
            if entity.estimated_minutes is not None:
                metadata["estimated_minutes"] = entity.estimated_minutes
            if entity.automation_level:
                metadata["automation_level"] = entity.automation_level

        # Common fields (use getattr since not all entity types have these)
        if languages := getattr(entity, "languages", None):
            metadata["languages"] = languages
        if tags := getattr(entity, "tags", None):
            metadata["tags"] = tags
        if category := getattr(entity, "category", None):
            metadata["category"] = category

        return self._serialize_metadata(metadata)

    def _coerce_enum(self, enum_type: type[TEnum], value: Any, default: TEnum) -> TEnum:
        """Coerce a value into an enum, falling back to default on mismatch."""
        if value is None:
            return default
        if isinstance(value, enum_type):
            return value
        try:
            return enum_type(value)
        except (ValueError, TypeError):
            return default

    def _coerce_entity(self, entity: Entity) -> Entity:
        """Convert generic entities into their typed models when possible."""
        if isinstance(entity, (Task, Procedure)):
            return entity

        try:
            if entity.entity_type == EntityType.TASK:
                return self._entity_to_task(entity)
            if entity.entity_type == EntityType.PROCEDURE:
                return self._entity_to_procedure(entity)
        except Exception as exc:
            log.debug(
                "Failed to coerce entity",
                entity_id=entity.id,
                entity_type=entity.entity_type,
                error=str(exc),
            )

        return entity

    def _entity_to_task(self, entity: Entity) -> Task:
        """Hydrate a Task from a generic entity + metadata."""
        meta = entity.metadata or {}
        return Task(
            id=entity.id,
            entity_type=EntityType.TASK,
            name=entity.name,
            title=meta.get("title") or entity.name,
            description=entity.description or meta.get("description") or "",
            content=entity.content or meta.get("content") or "",
            organization_id=entity.organization_id,
            created_by=entity.created_by,
            modified_by=entity.modified_by,
            metadata=meta,
            created_at=entity.created_at,
            updated_at=entity.updated_at,
            source_file=entity.source_file,
            embedding=entity.embedding,
            status=self._coerce_enum(TaskStatus, meta.get("status"), TaskStatus.TODO),
            priority=self._coerce_enum(TaskPriority, meta.get("priority"), TaskPriority.MEDIUM),
            task_order=meta.get("task_order", 0),
            project_id=meta.get("project_id"),
            epic_id=meta.get("epic_id"),
            feature=meta.get("feature"),
            sprint=meta.get("sprint"),
            assignees=meta.get("assignees", []),
            due_date=meta.get("due_date"),
            estimated_hours=meta.get("estimated_hours"),
            actual_hours=meta.get("actual_hours"),
            domain=meta.get("domain"),
            technologies=meta.get("technologies", []),
            complexity=self._coerce_enum(
                TaskComplexity,
                meta.get("complexity"),
                TaskComplexity.MEDIUM,
            ),
            tags=meta.get("tags", []),
            branch_name=meta.get("branch_name"),
            commit_shas=meta.get("commit_shas", []),
            pr_url=meta.get("pr_url"),
            learnings=meta.get("learnings", ""),
            blockers_encountered=meta.get("blockers_encountered", []),
            started_at=meta.get("started_at"),
            completed_at=meta.get("completed_at"),
            reviewed_at=meta.get("reviewed_at"),
        )

    def _entity_to_procedure(self, entity: Entity) -> Procedure:
        """Hydrate a Procedure from a generic entity + metadata."""
        meta = entity.metadata or {}
        steps: list[ProcedureStep] = []
        for raw_step in meta.get("steps", []):
            if isinstance(raw_step, ProcedureStep):
                steps.append(raw_step)
            elif isinstance(raw_step, dict):
                steps.append(ProcedureStep.model_validate(raw_step))

        return Procedure(
            id=entity.id,
            entity_type=EntityType.PROCEDURE,
            name=entity.name,
            description=entity.description or meta.get("description") or "",
            content=entity.content or meta.get("content") or "",
            organization_id=entity.organization_id,
            created_by=entity.created_by,
            modified_by=entity.modified_by,
            metadata=meta,
            created_at=entity.created_at,
            updated_at=entity.updated_at,
            source_file=entity.source_file,
            embedding=entity.embedding,
            steps=steps,
            required_tools=meta.get("required_tools", []),
            category=meta.get("category", ""),
            estimated_minutes=meta.get("estimated_minutes"),
            automation_level=meta.get("automation_level", "manual"),
        )

    def _build_bulk_direct_row(self, entity: Entity) -> dict[str, Any]:
        """Build the property map used for batched direct entity upserts."""
        props = {k: v for k, v in self._collect_properties(entity).items() if v is not None}
        props["group_id"] = self._group_id
        props["summary"] = entity.description[:500] if entity.description else entity.name
        props["created_at"] = (entity.created_at or datetime.now(UTC)).isoformat()
        props["updated_at"] = datetime.now(UTC).isoformat()
        props["metadata"] = json.dumps(self._entity_to_metadata(entity) or {})
        props["name_embedding"] = (
            entity.embedding if isinstance(entity.embedding, list) and entity.embedding else None
        )
        props["_generated"] = True
        return props

    async def _save_entity_node_direct(self, entity: Entity) -> None:
        """Persist one entity through Graphiti's EntityNode.save fallback path."""
        node = self._build_entity_node(entity, marker_key="_generated")
        surreal_entity_ops = self._surreal_entity_node_ops()
        if surreal_entity_ops is not None:
            await surreal_entity_ops.save(self._driver, node)
            return
        await node.save(self._driver)

    async def bulk_create_direct(
        self,
        entities: list[Entity],
        batch_size: int = 100,
    ) -> tuple[int, int]:
        """Bulk create entities using Graphiti's EntityNode.save(), bypassing LLM.

        This is faster than create() as it skips LLM-based entity extraction.
        Use this for stress testing or bulk imports where LLM processing isn't needed.

        Args:
            entities: List of entities to create.
            batch_size: Number of entities per batch.

        Returns:
            Tuple of (created_count, failed_count).
        """
        created = 0
        failed = 0
        surreal_entity_ops = self._surreal_entity_node_ops()

        if surreal_entity_ops is not None:
            for i in range(0, len(entities), batch_size):
                batch = entities[i : i + batch_size]
                nodes = [
                    self._build_entity_node(entity, marker_key="_generated") for entity in batch
                ]
                try:
                    await surreal_entity_ops.save_bulk(self._driver, nodes, batch_size=batch_size)
                    created += len(batch)
                except Exception as e:
                    log.warning(
                        "bulk direct surreal upsert failed, falling back to per-entity saves",
                        batch_size=len(batch),
                        error=str(e),
                    )
                    for entity in batch:
                        try:
                            await self._save_entity_node_direct(entity)
                            created += 1
                        except Exception as item_error:
                            log.debug(
                                "Failed to create entity",
                                entity_id=entity.id,
                                error=str(item_error),
                            )
                            failed += 1

            log.info("Bulk create complete", created=created, failed=failed)
            return created, failed

        self._assert_legacy_fallback_allowed("bulk_create_direct")

        for i in range(0, len(entities), batch_size):
            batch = entities[i : i + batch_size]
            batch_groups: dict[str, list[Entity]] = defaultdict(list)
            for entity in batch:
                batch_groups[entity.entity_type.value].append(entity)

            for entity_type, typed_batch in batch_groups.items():
                entity_rows = [self._build_bulk_direct_row(entity) for entity in typed_batch]
                batch_query = f"""
                    UNWIND $entity_rows AS entity_data
                    MERGE (n:Entity {{uuid: entity_data.uuid}})
                    SET n:{entity_type}
                    SET n = entity_data
                    SET n.name_embedding = vecf32(entity_data.name_embedding)
                    RETURN count(n) AS upserted
                """
                try:
                    await self._driver.execute_query(batch_query, entity_rows=entity_rows)
                    created += len(typed_batch)
                except Exception as e:
                    log.warning(
                        "bulk direct upsert failed, falling back to per-entity saves",
                        entity_type=entity_type,
                        batch_size=len(typed_batch),
                        error=str(e),
                    )
                    for entity in typed_batch:
                        try:
                            await self._save_entity_node_direct(entity)
                            created += 1
                        except Exception as item_error:
                            log.debug(
                                "Failed to create entity",
                                entity_id=entity.id,
                                error=str(item_error),
                            )
                            failed += 1

        log.info("Bulk create complete", created=created, failed=failed)
        return created, failed

    def _format_entity_as_episode(self, entity: Entity) -> str:
        """Format an entity as natural language for episode storage.

        Args:
            entity: The entity to format.

        Returns:
            Formatted episode body.
        """

        # Sanitize text for RediSearch compatibility
        def sanitize(text: str) -> str:
            # Remove markdown formatting (bold/italic markers)
            result = re.sub(r"\*{1,3}", "", text)
            result = re.sub(r"_{1,3}", "", result)
            # Remove special characters that break RediSearch
            result = re.sub(r"[`\[\]{}()|@#$%^&+=<>\"']", "", result)
            result = result.replace(":", " ").replace("/", " ")
            return re.sub(r"\s+", " ", result).strip()

        parts = [
            f"Entity: {sanitize(entity.name)}",
            f"Type: {entity.entity_type}",
        ]

        if entity.description:
            parts.append(f"Description: {sanitize(entity.description)}")

        if entity.content:
            # Truncate content to avoid excessive episode size
            content = entity.content[:500] if len(entity.content) > 500 else entity.content
            parts.append(f"Content: {sanitize(content)}")

        # Add type-specific fields
        parts.extend(self._format_specialized_fields(entity, sanitize))

        return "\n".join(parts)

    def _format_specialized_fields(
        self,
        entity: Entity,
        sanitize: Any,
    ) -> list[str]:
        """Format specialized fields for different entity types.

        Args:
            entity: The entity to format.
            sanitize: Function to sanitize text.

        Returns:
            List of formatted field strings.
        """
        parts: list[str] = []

        if isinstance(entity, Task):
            if entity.status:
                parts.append(f"Status: {entity.status}")
            if entity.priority:
                parts.append(f"Priority: {entity.priority}")
            if entity.domain:
                parts.append(f"Domain: {sanitize(entity.domain)}")
            if entity.technologies:
                parts.append(f"Technologies: {', '.join(entity.technologies)}")
            if entity.feature:
                parts.append(f"Feature: {sanitize(entity.feature)}")

        elif isinstance(entity, Project):
            if entity.status:
                parts.append(f"Status: {entity.status}")
            if entity.tech_stack:
                parts.append(f"Tech Stack: {', '.join(entity.tech_stack)}")
            if entity.features:
                parts.append(f"Features: {', '.join(entity.features[:5])}")

        elif isinstance(entity, Epic):
            if entity.status:
                parts.append(f"Status: {entity.status}")
            if entity.priority:
                parts.append(f"Priority: {entity.priority}")
            if entity.project_id:
                parts.append(f"Project ID: {entity.project_id}")
            if entity.assignees:
                parts.append(f"Assignees: {', '.join(entity.assignees[:5])}")

        elif isinstance(entity, Source):
            parts.append(f"URL: {sanitize(entity.url)}")
            parts.append(f"Source Type: {entity.source_type}")
            if entity.crawl_status:
                parts.append(f"Crawl Status: {entity.crawl_status}")
            if entity.document_count:
                parts.append(f"Documents: {entity.document_count}")

        elif isinstance(entity, Document):
            parts.append(f"URL: {sanitize(entity.url)}")
            if entity.title:
                parts.append(f"Title: {sanitize(entity.title)}")
            if entity.headings:
                parts.append(f"Headings: {', '.join(entity.headings[:5])}")
            if entity.has_code:
                parts.append("Has Code: yes")
            if entity.language:
                parts.append(f"Language: {entity.language}")

        elif isinstance(entity, Community):
            if entity.key_concepts:
                parts.append(f"Concepts: {', '.join(entity.key_concepts)}")
            if entity.member_count:
                parts.append(f"Members: {entity.member_count}")
            if entity.level is not None:
                parts.append(f"Level: {entity.level}")

        elif isinstance(entity, ErrorPattern):
            parts.append(f"Error: {sanitize(entity.error_message)}")
            parts.append(f"Root Cause: {sanitize(entity.root_cause)}")
            parts.append(f"Solution: {sanitize(entity.solution)}")
            if entity.technologies:
                parts.append(f"Technologies: {', '.join(entity.technologies)}")

        elif isinstance(entity, Team):
            if entity.members:
                parts.append(f"Members: {', '.join(entity.members[:5])}")
            if entity.focus_areas:
                parts.append(f"Focus Areas: {', '.join(entity.focus_areas)}")

        elif isinstance(entity, Milestone):
            if entity.total_tasks:
                parts.append(f"Tasks: {entity.completed_tasks}/{entity.total_tasks}")

        elif isinstance(entity, Note):
            if entity.task_id:
                parts.append(f"Task ID: {entity.task_id}")
            if entity.author_type:
                parts.append(f"Author Type: {entity.author_type}")
            if entity.author_name:
                parts.append(f"Author: {sanitize(entity.author_name)}")

        return parts

    def node_to_entity(self, node: EntityNode) -> Entity:
        """Convert a Graphiti EntityNode to our Entity model.

        Args:
            node: The EntityNode to convert.

        Returns:
            Converted Entity.
        """
        # Extract entity type from attributes first, then fall back to node labels
        entity_type_str = node.attributes.get("entity_type") or ""

        # If no entity_type attribute, check node labels (e.g., ["Entity", "task"])
        if not entity_type_str and node.labels:
            for label in node.labels:
                label_lower = label.lower()
                if label_lower != "entity":  # Skip the generic "Entity" label
                    try:
                        EntityType(label_lower)
                        entity_type_str = label_lower
                        break
                    except ValueError:
                        continue

        # Default to topic if still not found
        entity_type_str = entity_type_str or "topic"

        try:
            entity_type = EntityType(entity_type_str)
        except ValueError:
            # Default to TOPIC if unknown type
            entity_type = EntityType.TOPIC
            log.warning(
                "Unknown entity type, defaulting to TOPIC",
                node_uuid=node.uuid,
                entity_type_str=entity_type_str,
            )

        # Extract other attributes
        description = node.attributes.get("description", node.summary or "")
        content = node.attributes.get("content", "")
        source_file = node.attributes.get("source_file")
        updated_at = self._parse_datetime(node.attributes.get("updated_at")) or node.created_at

        # Remove known fields from attributes to get clean metadata
        metadata = {
            k: v
            for k, v in node.attributes.items()
            if k not in {"entity_type", "description", "content", "source_file", "metadata"}
        }

        # Parse metadata - may be JSON string (from create_direct) or dict
        raw_metadata = node.attributes.get("metadata")
        if raw_metadata:
            if isinstance(raw_metadata, str):
                import json

                try:
                    parsed = json.loads(raw_metadata)
                    if isinstance(parsed, dict):
                        metadata.update(parsed)
                except json.JSONDecodeError:
                    pass  # Not valid JSON, skip
            elif isinstance(raw_metadata, dict):
                metadata.update(raw_metadata)

        return self._coerce_entity(
            Entity(
                id=node.uuid,
                entity_type=entity_type,
                name=node.name,
                description=description,
                content=content,
                organization_id=node.group_id,
                created_by=metadata.get("created_by"),
                modified_by=metadata.get("modified_by"),
                metadata=metadata,
                created_at=node.created_at,
                updated_at=updated_at,
                source_file=source_file,
                embedding=node.name_embedding if node.name_embedding else None,
            )
        )

    async def _get_node_entity_type(self, entity_id: str) -> EntityType | None:
        """Query for entity_type property directly from graph node.

        Graphiti's dataclass hydration doesn't include custom properties like
        entity_type that we persist via _persist_entity_attributes. This method
        directly queries the graph to retrieve it.

        Args:
            entity_id: The node's UUID.

        Returns:
            EntityType if found and valid, None otherwise.
        """
        try:
            self._assert_legacy_fallback_allowed("entity_type_lookup")
            result = await self._driver.execute_query(
                "MATCH (n {uuid: $id}) RETURN n.entity_type AS entity_type",
                id=entity_id,
            )
            # FalkorDB returns (rows, columns, stats) where rows is list of dicts
            if result and result[0]:
                rows = result[0]
                if rows and isinstance(rows[0], dict):
                    raw_type = rows[0].get("entity_type")
                    if raw_type:
                        return EntityType(raw_type)
        except (ValueError, IndexError, TypeError, KeyError):
            pass
        return None

    def _episodic_to_entity(
        self, node: EpisodicNode, entity_type_override: EntityType | None = None
    ) -> Entity:
        """Convert a Graphiti EpisodicNode to our Entity model.

        EpisodicNodes are created via add_episode() and have different structure
        than EntityNodes.

        Args:
            node: The EpisodicNode to convert.
            entity_type_override: Optional entity type from graph property lookup.
                Used when the node's Python object doesn't have the entity_type
                attribute (Graphiti dataclass doesn't hydrate custom properties).

        Returns:
            Converted Entity.
        """

        # EpisodicNode has: uuid, name, group_id, content, created_at, valid_at, source_description

        # Priority for entity_type:
        # 1. entity_type_override (from direct graph property query)
        # 2. node.entity_type attribute (if Graphiti ever hydrates it)
        # 3. Parse from name prefix (format: "type:name")
        # 4. Default to EPISODE
        entity_type: EntityType = entity_type_override or EntityType.EPISODE
        name = node.name

        # Try node attribute as fallback (may not be hydrated by Graphiti)
        if entity_type == EntityType.EPISODE and (
            node_entity_type := getattr(node, "entity_type", None)
        ):
            with contextlib.suppress(ValueError):
                entity_type = EntityType(node_entity_type)

        # Fallback: try to extract entity_type from the name (format: "type:name")
        if entity_type == EntityType.EPISODE and ":" in name:
            parts = name.split(":", 1)
            potential_type = parts[0].strip().lower()
            # Check if it's a valid entity type
            try:
                entity_type = EntityType(potential_type)
                name = parts[1].strip() if len(parts) > 1 else name
            except ValueError:
                pass  # Not a valid type prefix, use full name

        # Extract content and description from node (use getattr for type safety)
        content = getattr(node, "content", "") or ""
        description = getattr(node, "source_description", "") or ""

        # Build metadata dict
        metadata: dict[str, Any] = {}

        # Build entity kwargs, only including datetime fields if present
        entity_kwargs: dict[str, Any] = {
            "id": node.uuid,
            "entity_type": entity_type,
            "name": name,
            "description": description,
            "content": content,
            "organization_id": getattr(node, "group_id", None),
            "metadata": metadata,
        }
        if created_at := getattr(node, "created_at", None):
            entity_kwargs["created_at"] = created_at
            # Use created_at for updated_at if no explicit updated_at
            entity_kwargs["updated_at"] = created_at

        return self._coerce_entity(Entity(**entity_kwargs))
