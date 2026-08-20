"""Work-item projections and aggregate reads for graph entities."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sibyl_core.models.entities import Entity, EntityType
from sibyl_core.models.tasks import Epic
from sibyl_core.services.graph_common import normalize_graph_records as normalize_records
from sibyl_core.services.graph_entity_search import _EntitySearchManager
from sibyl_core.services.graph_records import (
    _entity_from_row,
    _entity_select_fields,
    _entity_to_task,
    _int_value,
    _surreal_indexed_field_equals_or_missing,
    _surreal_indexed_field_in_or_missing,
    _surreal_indexed_field_missing,
)
from sibyl_core.services.graph_search import count_task_status as _count_task_status
from sibyl_core.services.graph_search import (
    entity_matches_list_filters as _entity_matches_list_filters,
)
from sibyl_core.services.graph_search import finalize_task_progress as _finalize_task_progress
from sibyl_core.services.graph_search import lower_filter_values as _lower_filter_values
from sibyl_core.services.graph_search import lower_sequence_values as _lower_sequence_values
from sibyl_core.services.graph_search import metadata_scalar as _metadata_scalar
from sibyl_core.services.graph_search import new_task_progress as _new_task_progress
from sibyl_core.services.graph_search import task_priority_rank as _task_priority_rank


class _EntityWorkItemManager(_EntitySearchManager):
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
