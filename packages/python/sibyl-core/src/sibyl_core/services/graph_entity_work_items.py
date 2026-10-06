"""Work-item projections and aggregate reads for graph entities."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
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


def _private_memory_clauses(
    *,
    exclude_private_memory: bool,
    private_memory_owner: str | None,
    params: dict[str, object],
) -> list[str]:
    """Keep a reader's own private rows and drop everyone else's in SurrealQL.

    The row-level rule (``memory_metadata_read_allowed``) still runs on what
    comes back; these predicates only remove rows it would deny anyway, so a
    list window arrives mostly visible instead of mostly filtered. Scope and
    owner coalesce ``attributes`` over the column, the way the entity reader
    does. A private row with no stamped principal is left for the row rule.
    """
    if exclude_private_memory:
        return ["(attributes.memory_scope ?? memory_scope) != 'private'"]
    if private_memory_owner is not None:
        params["private_memory_owner"] = private_memory_owner
        return [
            "NOT ((attributes.memory_scope ?? memory_scope) = 'private'"
            " AND attributes.principal_id != NONE"
            " AND attributes.principal_id != $private_memory_owner)"
        ]
    return []


def _summary_rows(value: object) -> list[Mapping[str, Any]]:
    if not isinstance(value, list):
        return []
    return [row for row in value if isinstance(row, Mapping)]


# Status is read as the entity reader reads it (attributes over the column)
# where it is displayed or grouped, and as the plain column where it selects
# rows, so each bucket stays on idx_entity_type_status_updated or
# idx_entity_type_project_updated instead of filtering the whole project.
_SUMMARY_TASK_FIELDS = (
    "uuid, name, attributes.status ?? status AS status, "
    "attributes.priority ?? priority AS priority, updated_at"
)
_SUMMARY_TASK_SCOPE = "group_id = $group_id AND entity_type = 'task' AND project_id = $project_id"
_SUMMARY_OPEN = "(status IS NONE OR status NOT IN ['done', 'archived'])"
_PROJECT_SUMMARY_STATEMENT = f"""
RETURN {{
    status_counts: (
        SELECT attributes.status ?? status AS status, count() AS n
        FROM entity
        WHERE {_SUMMARY_TASK_SCOPE}
        GROUP BY status
    ),
    doing: (
        SELECT {_SUMMARY_TASK_FIELDS} FROM entity
        WHERE {_SUMMARY_TASK_SCOPE} AND status = 'doing'
        ORDER BY updated_at DESC LIMIT $actionable_limit
    ),
    blocked: (
        SELECT {_SUMMARY_TASK_FIELDS} FROM entity
        WHERE {_SUMMARY_TASK_SCOPE} AND status = 'blocked'
        ORDER BY updated_at DESC LIMIT $actionable_limit
    ),
    review: (
        SELECT {_SUMMARY_TASK_FIELDS} FROM entity
        WHERE {_SUMMARY_TASK_SCOPE} AND status = 'review'
        ORDER BY updated_at DESC LIMIT $actionable_limit
    ),
    recent: (
        SELECT {_SUMMARY_TASK_FIELDS} FROM entity
        WHERE {_SUMMARY_TASK_SCOPE}
          AND (status IS NONE OR status NOT IN ['doing', 'blocked', 'review'])
        ORDER BY updated_at DESC LIMIT $actionable_limit
    ),
    critical: (
        SELECT {_SUMMARY_TASK_FIELDS} FROM entity
        WHERE {_SUMMARY_TASK_SCOPE} AND {_SUMMARY_OPEN} AND priority = 'critical'
        ORDER BY updated_at DESC LIMIT $critical_limit
    ),
    high: (
        SELECT {_SUMMARY_TASK_FIELDS} FROM entity
        WHERE {_SUMMARY_TASK_SCOPE} AND {_SUMMARY_OPEN} AND priority = 'high'
        ORDER BY updated_at DESC LIMIT $critical_limit
    ),
    flagged: (
        SELECT {_SUMMARY_TASK_FIELDS} FROM entity
        WHERE {_SUMMARY_TASK_SCOPE} AND {_SUMMARY_OPEN}
          AND (priority IS NONE OR priority NOT IN ['critical', 'high'])
          AND string::contains(string::uppercase(name), 'CRITICAL')
        ORDER BY updated_at DESC LIMIT $critical_limit
    ),
}};
"""


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
        """Roll a project's tasks up to counts and a few actionable rows.

        One statement answers it: status counts grouped in the database, and
        each actionable bucket (doing, blocked, review, the rest) and each
        critical bucket as its own ordered, limited select on the project
        index. This used to page every task of the project with SELECT *
        and reduce the rows in Python; the hub project paid 1,150 rows over
        three restarted pages to fill five slots.
        """
        rows = normalize_records(
            await self._client.execute_query(
                _PROJECT_SUMMARY_STATEMENT,
                group_id=self._group_id,
                project_id=project_id,
                actionable_limit=max(int(actionable_limit), 1),
                critical_limit=max(int(critical_limit), 1),
            )
        )
        payload: dict[str, Any] = rows[0] if rows else {}

        # Keyed the way count_by_status keys its counts: a status-less task is
        # todo and the key is lowercase, so the summary and the completion
        # counters the write path maintains read one task the same way.
        status_counts: dict[str, int] = {}
        for row in _summary_rows(payload.get("status_counts")):
            status_value = str(row.get("status") or "todo").lower()
            status_counts[status_value] = status_counts.get(status_value, 0) + _int_value(
                row.get("n")
            )

        def task_info(row: Mapping[str, Any]) -> dict[str, Any]:
            return {
                "id": str(row.get("uuid") or ""),
                "name": str(row.get("name") or ""),
                "status": str(row.get("status") or "todo"),
                "priority": str(row.get("priority") or ""),
            }

        actionable: list[dict[str, Any]] = []
        seen: set[str] = set()
        for bucket in ("doing", "blocked", "review", "recent"):
            for row in _summary_rows(payload.get(bucket)):
                if len(actionable) >= actionable_limit:
                    break
                info = task_info(row)
                if info["id"] in seen:
                    continue
                seen.add(info["id"])
                actionable.append(info)
            if len(actionable) >= actionable_limit:
                break

        critical_candidates = [
            task_info(row)
            for bucket in ("critical", "high", "flagged")
            for row in _summary_rows(payload.get(bucket))
        ]
        critical_tasks = sorted(critical_candidates, key=_task_priority_rank)[:critical_limit]

        epics: list[dict[str, Any]] = []
        listed_epics = await self.list_epics_for_project(
            project_id,
            limit=epic_limit,
            enrich_progress=False,
        )
        epic_progress = (
            await self._epic_progress_map({epic.id for epic in listed_epics}, project_id=project_id)
            if listed_epics
            else {}
        )
        for epic in listed_epics:
            progress = epic_progress.get(epic.id, {})
            total_tasks = int(progress.get("total_tasks", 0))
            completed_tasks = int(progress.get("completed_tasks", 0))
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

    async def count_by_status(
        self,
        entity_type: EntityType,
        *,
        project_id: str | None = None,
        epic_id: str | None = None,
    ) -> dict[str, int]:
        """Count rows of one type per status in a single aggregate statement.

        Archived rows are included and a row without a status counts as todo,
        the same reading list_by_type's callers apply to task metadata. The
        scope filters are exact column matches, the same predicates
        list_by_type uses, so the aggregate stays on the compound
        (entity_type, project_id, ...) indexes; rows written before the
        columns existed were promoted by graph migrations 7 and 36.
        """
        where_clauses = [
            "group_id = $group_id",
            "entity_type = $entity_type",
        ]
        query_params: dict[str, object] = {
            "group_id": self._group_id,
            "entity_type": entity_type.value,
        }
        if project_id is not None:
            where_clauses.append("project_id = $project_id")
            query_params["project_id"] = project_id
        if epic_id is not None:
            where_clauses.append("(parent_task_id = $parent_task_id OR epic_id = $epic_id)")
            query_params["epic_id"] = epic_id
            query_params["parent_task_id"] = epic_id
        rows = normalize_records(
            await self._client.execute_query(
                f"""
                SELECT status, count() AS total
                FROM entity
                WHERE {" AND ".join(where_clauses)}
                GROUP BY status;
                """,
                **query_params,
            )
        )
        counts: dict[str, int] = {}
        for row in rows:
            status = str(row.get("status") or "todo").lower()
            total = row.get("total")
            counts[status] = counts.get(status, 0) + (
                int(total) if isinstance(total, int | float) and not isinstance(total, bool) else 0
            )
        return counts

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
        exact_window: bool = False,
        exclude_private_memory: bool = False,
        private_memory_owner: str | None = None,
    ) -> list[Entity]:
        """List one type newest-first, with ``offset`` counting visible rows.

        Every column filter is an exact predicate, so the database page is the
        page; only the Python-side tag filter restarts the walk at ``START 0``
        to keep the visible offset exact. ``exact_window``
        instead addresses the ordered index directly: one statement of
        ``limit`` rows from ``START offset``, no recheck, no fill. A caller
        paging a large type reads O(limit) per page that way and runs the
        recheck on the rows it gets back. ``exclude_private_memory`` and
        ``private_memory_owner`` push the reader's private-scope rule into
        the statement (see ``_private_memory_clauses``).
        """
        if limit <= 0:
            return []

        status_values = _lower_filter_values(status)
        priority_values = _lower_filter_values(priority)
        complexity_values = _lower_filter_values(complexity)
        tag_values = _lower_sequence_values(tags)
        # Every filter below is an exact column predicate, so the database
        # page is the page: only tags (Python-only) force a restart from the
        # first row. The old "or missing" branches admitted rows the recheck
        # then dropped, which also pushed the planner off the compound
        # (entity_type, project_id, ...) index onto a walk of every row of
        # the type.
        requires_recheck = bool(tag_values)
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
            where_clauses.append("project_id = $project_id")
            query_params["project_id"] = project_id
        if epic_id is not None:
            where_clauses.append("(parent_task_id = $parent_task_id OR epic_id = $epic_id)")
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
            where_clauses.append("parent_task_id = $parent_task_id")
            query_params["parent_task_id"] = parent_task_id
        if status_values:
            where_clauses.append("status IN $status_values")
            query_params["status_values"] = status_values
        if priority_values:
            where_clauses.append("priority IN $priority_values")
            query_params["priority_values"] = priority_values
        if complexity_values:
            where_clauses.append("complexity IN $complexity_values")
            query_params["complexity_values"] = complexity_values
        if feature:
            where_clauses.append("feature = $feature")
            query_params["feature"] = feature.lower()
        if not include_archived:
            where_clauses.append("(status IS NONE OR status = '' OR status != 'archived')")
        where_clauses.extend(
            _private_memory_clauses(
                exclude_private_memory=exclude_private_memory,
                private_memory_owner=private_memory_owner,
                params=query_params,
            )
        )
        select_fields = _entity_select_fields(include_content)
        statement = f"""
            SELECT {select_fields}
            FROM entity
            WHERE {" AND ".join(where_clauses)}
            ORDER BY updated_at DESC, created_at DESC, uuid DESC
            LIMIT $limit START $offset;
            """

        if exact_window:
            rows = normalize_records(
                await self._client.execute_query(
                    statement,
                    **query_params,
                    limit=max(int(limit), 1),
                    offset=max(int(offset), 0),
                )
            )
            entities = [_entity_from_row(row) for row in rows]
            if entity_type == EntityType.EPIC and enrich_epic_progress:
                return await self._with_epic_progress(entities, project_id=project_id)
            return entities

        while len(entities) < target_count:
            rows = normalize_records(
                await self._client.execute_query(
                    statement,
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
        exact_window: bool = False,
        exclude_private_memory: bool = False,
        private_memory_owner: str | None = None,
    ) -> list[Entity]:
        """List every type newest-first; the keyword contract matches list_by_type."""
        if limit <= 0:
            return []
        target_count = max(int(offset), 0) + max(int(limit), 1) if not include_archived else limit
        query_offset = 0 if not include_archived else max(int(offset), 0)
        page_size = min(max(target_count, 1), 1000)
        entities: list[Entity] = []
        seen_entity_ids: set[str] = set()
        seen_pages: set[tuple[str | None, ...]] = set()
        where_clauses = ["group_id = $group_id"]
        query_params: dict[str, object] = {"group_id": self._group_id}
        if not include_archived:
            where_clauses.append(
                "string::lowercase(status ?? attributes.status ?? '') != 'archived'"
            )
        where_clauses.extend(
            _private_memory_clauses(
                exclude_private_memory=exclude_private_memory,
                private_memory_owner=private_memory_owner,
                params=query_params,
            )
        )
        select_fields = _entity_select_fields(include_content)
        statement = f"""
            SELECT {select_fields}
            FROM entity
            WHERE {" AND ".join(where_clauses)}
            ORDER BY updated_at DESC, created_at DESC, uuid DESC
            LIMIT $limit START $offset;
            """

        if exact_window:
            rows = normalize_records(
                await self._client.execute_query(
                    statement,
                    **query_params,
                    limit=max(int(limit), 1),
                    offset=max(int(offset), 0),
                )
            )
            return [_entity_from_row(row) for row in rows]

        while len(entities) < target_count:
            rows = normalize_records(
                await self._client.execute_query(
                    statement,
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
        # Every row of an organization namespace shares group_id, so that
        # predicate only disabled the count optimisation: with it, or with a
        # GROUP BY, the 3.x planner scans and decodes the whole table. One
        # single-column equality per type plans as an IndexCountScan, and the
        # archived rows come from one scan of the status index.
        types = list(EntityType)
        statements = [
            f"SELECT count() AS entity_count FROM entity WHERE entity_type = $type_{index} GROUP ALL;"
            for index in range(len(types))
        ]
        if not include_archived:
            statements.append(
                "SELECT entity_type, count() AS entity_count FROM entity "
                "WHERE status = 'archived' GROUP BY entity_type;"
            )
        results = await self._client.execute_query_batch(
            "\n".join(statements),
            **{f"type_{index}": entity_type.value for index, entity_type in enumerate(types)},
        )
        if not isinstance(results, list) or len(results) != len(statements):
            raise RuntimeError("entity type counts returned an unexpected statement set")
        counts: dict[str, int] = {}
        for entity_type, result in zip(types, results[: len(types)], strict=True):
            rows = normalize_records(result)
            counts[entity_type.value] = _int_value(rows[0].get("entity_count")) if rows else 0
        if not include_archived:
            for row in normalize_records(results[-1]):
                entity_type_value = row.get("entity_type")
                if isinstance(entity_type_value, str) and entity_type_value in counts:
                    counts[entity_type_value] = max(
                        counts[entity_type_value] - _int_value(row.get("entity_count")), 0
                    )
        return counts

    async def has_entities_of_types(
        self, entity_types: Sequence[EntityType], *, include_archived: bool = False
    ) -> bool:
        """Whether any row of the given types exists, without counting anything.

        One type per statement: a multi-type `IN` list makes the planner
        materialise every branch before the limit applies, while a single
        equality stops at the first row.
        """
        archived_clause = (
            ""
            if include_archived
            else " AND (status IS NONE OR status = '' OR status != 'archived')"
        )
        for entity_type in entity_types:
            rows = normalize_records(
                await self._client.execute_query(
                    f"SELECT uuid FROM entity WHERE entity_type = $entity_type{archived_clause} "
                    "LIMIT 1;",
                    entity_type=entity_type.value,
                )
            )
            if rows:
                return True
        return False

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
