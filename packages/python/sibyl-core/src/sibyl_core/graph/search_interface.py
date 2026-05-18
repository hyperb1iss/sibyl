"""Search interface for the SurrealDB-backed compatibility runtime."""

from dataclasses import dataclass
from dataclasses import field as dataclass_field
from typing import Any

EDGE_FULLTEXT_MATCH_HEADROOM = 8
EDGE_FULLTEXT_MIN_MATCH_LIMIT = 32
type SurrealRecord = dict[str, object]

_ENTITY_EDGE_SELECT = """
SELECT
    uuid, name, fact, fact_embedding, group_id,
    episodes, attributes,
    created_at, expired_at, valid_at, invalid_at,
    in.uuid AS source_node_uuid,
    out.uuid AS target_node_uuid
FROM relates_to
"""


@dataclass(slots=True)
class _EntitySearchNode:
    uuid: str
    name: str
    group_id: str
    labels: list[str]
    summary: str
    attributes: dict[str, Any] = dataclass_field(default_factory=dict)
    name_embedding: list[float] | None = None
    created_at: Any = None


@dataclass(slots=True)
class _EntitySearchEdge:
    uuid: str
    source_node_uuid: str
    target_node_uuid: str
    fact: str
    name: str
    group_id: str
    episodes: list[str] = dataclass_field(default_factory=list)
    attributes: dict[str, Any] = dataclass_field(default_factory=dict)
    fact_embedding: list[float] | None = None
    created_at: Any = None
    expired_at: Any = None
    valid_at: Any = None
    invalid_at: Any = None
    reference_time: Any = None


@dataclass(slots=True)
class _EpisodeSearchNode:
    uuid: str
    group_id: str
    name: str
    content: str
    source: str
    source_description: str
    entity_edges: list[Any] = dataclass_field(default_factory=list)
    created_at: Any = None
    valid_at: Any = None


@dataclass(slots=True)
class _CommunitySearchNode:
    uuid: str
    name: str
    group_id: str
    summary: str
    name_embedding: list[float] | None = None
    created_at: Any = None


def _group_filter_clause(group_ids: list[str] | None) -> str:
    return "group_id IN $group_ids" if group_ids is not None else "true"


def _node_filter_clause(search_filter: Any) -> tuple[list[str], dict[str, Any]]:
    clauses: list[str] = []
    params: dict[str, Any] = {}
    node_labels = getattr(search_filter, "node_labels", None)
    if node_labels:
        clauses.append("labels CONTAINS $node_label")
        params["node_label"] = node_labels[0]
    project_ids = getattr(search_filter, "project_ids", None)
    if project_ids:
        clauses.append("(project_id IN $project_ids OR attributes.project_id IN $project_ids)")
        params["project_ids"] = list(project_ids)
    return clauses, params


def _edge_filter_clause(
    search_filter: Any,
    *,
    source_node_uuid: str | None = None,
    target_node_uuid: str | None = None,
) -> tuple[list[str], dict[str, Any]]:
    clauses: list[str] = []
    params: dict[str, Any] = {}

    edge_uuids = getattr(search_filter, "edge_uuids", None)
    if edge_uuids:
        clauses.append("uuid IN $edge_uuids")
        params["edge_uuids"] = edge_uuids

    edge_types = getattr(search_filter, "edge_types", None)
    if edge_types:
        clauses.append("name IN $edge_types")
        params["edge_types"] = edge_types

    node_labels = getattr(search_filter, "node_labels", None)
    if node_labels:
        clauses.append("in.labels CONTAINS $node_label AND out.labels CONTAINS $node_label")
        params["node_label"] = node_labels[0]

    project_ids = getattr(search_filter, "project_ids", None)
    if project_ids:
        clauses.append(
            "("
            "attributes.project_id IN $project_ids "
            "OR in.project_id IN $project_ids "
            "OR in.attributes.project_id IN $project_ids "
            "OR out.project_id IN $project_ids "
            "OR out.attributes.project_id IN $project_ids"
            ")"
        )
        params["project_ids"] = list(project_ids)

    if source_node_uuid is not None:
        clauses.append("in.uuid = $source_node_uuid")
        params["source_node_uuid"] = source_node_uuid

    if target_node_uuid is not None:
        clauses.append("out.uuid = $target_node_uuid")
        params["target_node_uuid"] = target_node_uuid

    for field in ("valid_at", "invalid_at", "created_at", "expired_at"):
        if temporal_clause := _temporal_filter_clause(search_filter, field, params):
            clauses.append(temporal_clause)

    return clauses, params


def _edge_match_filter_clause(search_filter: Any) -> tuple[list[str], dict[str, Any]]:
    clauses: list[str] = []
    params: dict[str, Any] = {}

    edge_uuids = getattr(search_filter, "edge_uuids", None)
    if edge_uuids:
        clauses.append("uuid IN $edge_uuids")
        params["edge_uuids"] = edge_uuids

    edge_types = getattr(search_filter, "edge_types", None)
    if edge_types:
        clauses.append("name IN $edge_types")
        params["edge_types"] = edge_types

    for field in ("valid_at", "invalid_at", "created_at", "expired_at"):
        if temporal_clause := _temporal_filter_clause(search_filter, field, params):
            clauses.append(temporal_clause)

    return clauses, params


def _where_clause(clauses: list[str]) -> str:
    return " AND ".join(clauses) if clauses else "true"


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def _record_uuid(record: dict[str, object]) -> str | None:
    uuid = record.get("uuid")
    if uuid is None or uuid == "":
        return None
    return str(uuid)


def _normalize_record(record: object) -> SurrealRecord | None:
    if record is None or not isinstance(record, dict):
        return None
    out = {str(key): value for key, value in record.items()}
    out.pop("id", None)
    if "attributes" not in out or out["attributes"] is None:
        out["attributes"] = {}
    if "labels" not in out or out["labels"] is None:
        out["labels"] = []
    return out


def _normalize_records(result: object) -> list[SurrealRecord]:
    if result is None:
        return []
    if isinstance(result, dict):
        single = _normalize_record(result)
        return [single] if single is not None else []
    if isinstance(result, list):
        records: list[SurrealRecord] = []
        for item in result:
            if isinstance(item, list):
                records.extend(
                    normalized
                    for nested in item
                    if (normalized := _normalize_record(nested)) is not None
                )
            elif (normalized := _normalize_record(item)) is not None:
                records.append(normalized)
        return records
    return []


def _normalize_embedding(value: object) -> list[float] | None:
    if not isinstance(value, list):
        return None
    embedding: list[float] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, int | float):
            return None
        embedding.append(float(item))
    return embedding


def _attributes_from_record(record: dict[str, Any], excluded: set[str]) -> dict[str, Any]:
    attributes = dict(record.get("attributes") or {})
    for key in excluded:
        attributes.pop(key, None)
    return attributes


def _entity_node_from_record(record: dict[str, Any]) -> _EntitySearchNode:
    labels = list(record.get("labels") or [])
    group_id = str(record.get("group_id") or "")
    dynamic_label = "Entity_" + group_id.replace("-", "")
    if dynamic_label in labels:
        labels.remove(dynamic_label)

    return _EntitySearchNode(
        uuid=str(record["uuid"]),
        name=str(record["name"]),
        name_embedding=_normalize_embedding(record.get("name_embedding")),
        group_id=group_id,
        labels=labels,
        created_at=record.get("created_at"),
        summary=str(record.get("summary") or ""),
        attributes=_attributes_from_record(
            record,
            {
                "uuid",
                "name",
                "group_id",
                "name_embedding",
                "summary",
                "created_at",
                "labels",
            },
        ),
    )


def _entity_edge_from_record(record: dict[str, Any]) -> _EntitySearchEdge:
    return _EntitySearchEdge(
        uuid=str(record["uuid"]),
        source_node_uuid=str(record["source_node_uuid"]),
        target_node_uuid=str(record["target_node_uuid"]),
        fact=str(record.get("fact") or ""),
        fact_embedding=_normalize_embedding(record.get("fact_embedding")),
        name=str(record["name"]),
        group_id=str(record["group_id"]),
        episodes=list(record.get("episodes") or []),
        created_at=record.get("created_at"),
        expired_at=record.get("expired_at"),
        valid_at=record.get("valid_at"),
        invalid_at=record.get("invalid_at"),
        reference_time=record.get("reference_time"),
        attributes=_attributes_from_record(
            record,
            {
                "uuid",
                "source_node_uuid",
                "target_node_uuid",
                "fact",
                "fact_embedding",
                "name",
                "group_id",
                "episodes",
                "created_at",
                "expired_at",
                "valid_at",
                "invalid_at",
                "reference_time",
            },
        ),
    )


def _rrf_uuids(results: list[list[str]], rank_const: int = 1) -> list[str]:
    scores: dict[str, float] = {}
    for result in results:
        for index, uuid in enumerate(result):
            scores[uuid] = scores.get(uuid, 0.0) + 1 / (index + rank_const)
    return [
        uuid
        for uuid, _score in sorted(
            scores.items(),
            key=lambda item: item[1],
            reverse=True,
        )
    ]


def _temporal_filter_clause(
    search_filter: Any,
    field: str,
    params: dict[str, Any],
) -> str:
    filters = getattr(search_filter, field, None)
    if not filters:
        return ""

    or_clauses: list[str] = []
    for or_index, and_filters in enumerate(filters):
        and_clauses: list[str] = []
        for and_index, date_filter in enumerate(and_filters):
            operator = date_filter.comparison_operator.value
            if operator == "IS NULL":
                and_clauses.append(f"{field} IS NONE")
                continue
            if operator == "IS NOT NULL":
                and_clauses.append(f"{field} IS NOT NONE")
                continue

            param_name = f"{field}_{or_index}_{and_index}"
            params[param_name] = date_filter.date
            and_clauses.append(f"{field} {operator} ${param_name}")

        if and_clauses:
            or_clauses.append("(" + " AND ".join(and_clauses) + ")")

    if not or_clauses:
        return ""
    return "(" + " OR ".join(or_clauses) + ")"


def _episode_from_record(record: dict[str, Any]) -> _EpisodeSearchNode:
    record["source_description"] = record.get("source_description") or ""
    record.setdefault("entity_edges", [])
    return _EpisodeSearchNode(
        uuid=str(record["uuid"]),
        group_id=str(record["group_id"]),
        name=str(record.get("name") or ""),
        content=str(record.get("content") or ""),
        source=str(record.get("source") or ""),
        source_description=str(record["source_description"]),
        entity_edges=list(record["entity_edges"]),
        created_at=record.get("created_at"),
        valid_at=record.get("valid_at"),
    )


def _community_from_record(record: dict[str, Any]) -> _CommunitySearchNode:
    record.setdefault("summary", "")
    record.setdefault("name_embedding", None)
    return _CommunitySearchNode(
        uuid=str(record["uuid"]),
        name=str(record["name"]),
        group_id=str(record["group_id"]),
        name_embedding=_normalize_embedding(record.get("name_embedding")),
        created_at=record.get("created_at"),
        summary=str(record["summary"]),
    )


class SurrealSearchInterface:
    """Native search adapter for SurrealDB compatibility callers."""

    async def _mentioned_entity_uuids(
        self,
        driver: Any,
        episode_uuids: list[str],
        group_ids: list[str] | None,
    ) -> list[str]:
        if not episode_uuids:
            return []
        group_clause = (
            "AND group_id IN $group_ids AND out.group_id IN $group_ids" if group_ids else ""
        )
        records = _normalize_records(
            await driver.execute_query(
                """
                SELECT out.uuid AS uuid
                FROM mentions
                WHERE in.uuid IN $episode_uuids """
                + group_clause
                + ";",
                episode_uuids=episode_uuids,
                group_ids=group_ids,
            )
        )
        return _dedupe([uuid for record in records if (uuid := _record_uuid(record))])

    async def _relation_target_uuids(
        self,
        driver: Any,
        source_uuids: list[str],
        group_ids: list[str] | None,
    ) -> list[str]:
        if not source_uuids:
            return []
        group_clause = (
            "AND group_id IN $group_ids AND out.group_id IN $group_ids" if group_ids else ""
        )
        records = _normalize_records(
            await driver.execute_query(
                """
                SELECT out.uuid AS uuid
                FROM relates_to
                WHERE in.uuid IN $source_uuids """
                + group_clause
                + ";",
                source_uuids=source_uuids,
                group_ids=group_ids,
            )
        )
        return _dedupe([uuid for record in records if (uuid := _record_uuid(record))])

    async def _hydrate_nodes(
        self,
        driver: Any,
        uuids: list[str],
        search_filter: Any,
        group_ids: list[str] | None,
        limit: int,
    ) -> list[Any]:
        if not uuids:
            return []
        filter_clauses, filter_params = _node_filter_clause(search_filter)
        records = _normalize_records(
            await driver.execute_query(
                "SELECT * FROM entity WHERE "
                + _where_clause(
                    ["uuid IN $uuids", _group_filter_clause(group_ids), *filter_clauses]
                )
                + " LIMIT $limit;",
                uuids=uuids,
                group_ids=group_ids,
                limit=max(int(limit), 1),
                **filter_params,
            )
        )
        nodes_by_uuid = {record["uuid"]: _entity_node_from_record(record) for record in records}
        return [nodes_by_uuid[uuid] for uuid in uuids if uuid in nodes_by_uuid]

    async def node_fulltext_search(
        self,
        driver: Any,
        query: str,
        search_filter: Any,
        group_ids: list[str] | None = None,
        limit: int = 100,
    ) -> list[Any]:
        search_query = driver.build_fulltext_query(query)
        if not search_query:
            return []

        filter_clauses, filter_params = _node_filter_clause(search_filter)
        records = _normalize_records(
            await driver.execute_query(
                """
                SELECT *,
                       math::max([
                           search::score(0),
                           search::score(1),
                           search::score(2),
                           search::score(3)
                       ]) AS score
                FROM entity
                WHERE """
                + _where_clause([_group_filter_clause(group_ids), *filter_clauses])
                + """
                  AND (
                      name @0@ $query
                      OR summary @1@ $query
                      OR description @2@ $query
                      OR content @3@ $query
                  )
                ORDER BY score DESC, created_at DESC, uuid DESC
                LIMIT $limit;
                """,
                query=search_query,
                group_ids=group_ids,
                limit=max(int(limit), 1),
                **filter_params,
            )
        )
        return [_entity_node_from_record(record) for record in records]

    async def node_similarity_search(
        self,
        driver: Any,
        search_vector: list[float],
        search_filter: Any,
        group_ids: list[str] | None = None,
        limit: int = 100,
        min_score: float = 0.7,
    ) -> list[Any]:
        if not search_vector:
            return []

        filter_clauses, filter_params = _node_filter_clause(search_filter)
        candidate_limit = max(int(limit) * 4, int(limit), 1)
        records = _normalize_records(
            await driver.execute_query(
                "SELECT * FROM ("
                "SELECT *, (1 - vector::distance::knn()) AS score FROM entity "
                "WHERE "
                + _where_clause([_group_filter_clause(group_ids), *filter_clauses])
                + " AND name_embedding IS NOT NONE "
                f"AND name_embedding <|{candidate_limit}, 40|> $search_vector"
                ") WHERE score > $min_score "
                "ORDER BY score DESC, created_at DESC, uuid DESC LIMIT $limit;",
                search_vector=search_vector,
                min_score=min_score,
                group_ids=group_ids,
                limit=max(int(limit), 1),
                **filter_params,
            )
        )
        return [_entity_node_from_record(record) for record in records]

    async def edge_fulltext_search(
        self,
        driver: Any,
        query: str,
        search_filter: Any,
        group_ids: list[str] | None = None,
        limit: int = 100,
    ) -> list[Any]:
        search_query = driver.build_fulltext_query(query)
        if not search_query:
            return []

        result_limit = max(int(limit), 1)
        match_limit = max(
            result_limit * EDGE_FULLTEXT_MATCH_HEADROOM, EDGE_FULLTEXT_MIN_MATCH_LIMIT
        )
        match_clauses, match_params = _edge_match_filter_clause(search_filter)
        match_records = _normalize_records(
            await driver.execute_query(
                """
                SELECT uuid, created_at, search::score(0) AS score
                FROM relates_to
                WHERE """
                + _where_clause([_group_filter_clause(group_ids), *match_clauses])
                + """
                  AND fact @0@ $query
                ORDER BY score DESC, created_at DESC, uuid DESC
                LIMIT $match_limit;
                """,
                query=search_query,
                group_ids=group_ids,
                match_limit=match_limit,
                **match_params,
            )
        )
        match_scores: dict[str, float] = {}
        for record in match_records:
            uuid = _record_uuid(record)
            if uuid is not None:
                match_scores[uuid] = float(record.get("score") or 0.0)
        match_uuids = list(match_scores)
        if not match_uuids:
            return []

        filter_clauses, filter_params = _edge_filter_clause(search_filter)
        records = _normalize_records(
            await driver.execute_query(
                _ENTITY_EDGE_SELECT
                + " WHERE "
                + _where_clause(
                    ["uuid IN $match_uuids", _group_filter_clause(group_ids), *filter_clauses]
                )
                + " LIMIT $limit;",
                match_uuids=match_uuids,
                group_ids=group_ids,
                limit=len(match_uuids),
                **filter_params,
            )
        )
        rows_by_uuid = {str(record["uuid"]): record for record in records if record.get("uuid")}
        ordered_records: list[dict[str, object]] = []
        for uuid in match_uuids:
            if uuid not in rows_by_uuid:
                continue
            record = dict(rows_by_uuid[uuid])
            record["score"] = match_scores[uuid]
            ordered_records.append(record)
        return [_entity_edge_from_record(record) for record in ordered_records[:result_limit]]

    async def edge_similarity_search(
        self,
        driver: Any,
        search_vector: list[float],
        source_node_uuid: str | None,
        target_node_uuid: str | None,
        search_filter: Any,
        group_ids: list[str] | None = None,
        limit: int = 100,
        min_score: float = 0.7,
    ) -> list[Any]:
        if not search_vector:
            return []

        filter_clauses, filter_params = _edge_filter_clause(
            search_filter,
            source_node_uuid=source_node_uuid,
            target_node_uuid=target_node_uuid,
        )
        candidate_limit = max(int(limit) * 4, int(limit), 1)
        vector_select = _ENTITY_EDGE_SELECT.replace(
            "FROM relates_to",
            ", (1 - vector::distance::knn()) AS score\nFROM relates_to",
        )
        records = _normalize_records(
            await driver.execute_query(
                "SELECT * FROM ("
                + vector_select
                + "WHERE "
                + _where_clause([_group_filter_clause(group_ids), *filter_clauses])
                + " AND fact_embedding IS NOT NONE "
                f"AND fact_embedding <|{candidate_limit}, 40|> $search_vector"
                ") WHERE score > $min_score "
                "ORDER BY score DESC, created_at DESC, uuid DESC LIMIT $limit;",
                search_vector=search_vector,
                min_score=min_score,
                group_ids=group_ids,
                limit=max(int(limit), 1),
                **filter_params,
            )
        )
        return [_entity_edge_from_record(record) for record in records]

    async def episode_fulltext_search(
        self,
        driver: Any,
        query: str,
        search_filter: Any,
        group_ids: list[str] | None = None,
        limit: int = 100,
    ) -> list[Any]:
        if getattr(search_filter, "project_ids", None):
            return []
        search_query = driver.build_fulltext_query(query)
        if not search_query:
            return []

        records = _normalize_records(
            await driver.execute_query(
                """
                SELECT *, search::score(0) AS score
                FROM episode
                WHERE """
                + _group_filter_clause(group_ids)
                + """
                  AND content @0@ $query
                ORDER BY score DESC, created_at DESC, uuid DESC
                LIMIT $limit;
                """,
                query=search_query,
                group_ids=group_ids,
                limit=max(int(limit), 1),
            )
        )
        return [_episode_from_record(record) for record in records]

    async def edge_bfs_search(
        self,
        driver: Any,
        bfs_origin_node_uuids: list[str] | None,
        bfs_max_depth: int,
        search_filter: Any,
        group_ids: list[str] | None = None,
        limit: int = 100,
    ) -> list[Any]:
        if not bfs_origin_node_uuids or bfs_max_depth < 1:
            return []

        filter_clauses, filter_params = _edge_filter_clause(search_filter)
        result_records: list[dict[str, Any]] = []
        seen_edges: set[str] = set()
        visited_entities = set(bfs_origin_node_uuids)
        entity_frontier = _dedupe(list(bfs_origin_node_uuids))
        episode_frontier = _dedupe(list(bfs_origin_node_uuids))

        for depth in range(1, bfs_max_depth + 1):
            next_entities: list[str] = []
            if depth == 1:
                next_entities.extend(
                    await self._mentioned_entity_uuids(driver, episode_frontier, group_ids)
                )

            if entity_frontier:
                traversal_targets = await self._relation_target_uuids(
                    driver,
                    entity_frontier,
                    group_ids,
                )
                next_entities.extend(traversal_targets)

                records = _normalize_records(
                    await driver.execute_query(
                        _ENTITY_EDGE_SELECT
                        + " WHERE "
                        + _where_clause(
                            [
                                "in.uuid IN $source_uuids",
                                *(
                                    ["group_id IN $group_ids", "out.group_id IN $group_ids"]
                                    if group_ids
                                    else []
                                ),
                                *filter_clauses,
                            ]
                        )
                        + " LIMIT $limit;",
                        source_uuids=entity_frontier,
                        group_ids=group_ids,
                        limit=max(int(limit), 1),
                        **filter_params,
                    )
                )
                for record in records:
                    uuid = _record_uuid(record)
                    if not uuid or uuid in seen_edges:
                        continue
                    seen_edges.add(uuid)
                    result_records.append(record)
                    if len(result_records) >= limit:
                        return [_entity_edge_from_record(r) for r in result_records]

            entity_frontier = [
                uuid for uuid in _dedupe(next_entities) if uuid not in visited_entities
            ]
            visited_entities.update(entity_frontier)
            if not entity_frontier:
                break

        return [_entity_edge_from_record(record) for record in result_records]

    async def node_bfs_search(
        self,
        driver: Any,
        bfs_origin_node_uuids: list[str] | None,
        search_filter: Any,
        bfs_max_depth: int,
        group_ids: list[str] | None = None,
        limit: int = 100,
    ) -> list[Any]:
        if not bfs_origin_node_uuids or bfs_max_depth < 1:
            return []

        discovered: list[str] = []
        seen_discovered: set[str] = set()
        visited_entities = set(bfs_origin_node_uuids)
        entity_frontier = _dedupe(list(bfs_origin_node_uuids))
        episode_frontier = _dedupe(list(bfs_origin_node_uuids))

        for depth in range(1, bfs_max_depth + 1):
            next_entities: list[str] = []
            if depth == 1:
                next_entities.extend(
                    await self._mentioned_entity_uuids(driver, episode_frontier, group_ids)
                )
            next_entities.extend(
                await self._relation_target_uuids(driver, entity_frontier, group_ids)
            )

            for uuid in _dedupe(next_entities):
                if uuid in seen_discovered:
                    continue
                seen_discovered.add(uuid)
                discovered.append(uuid)
                if len(discovered) >= limit:
                    return await self._hydrate_nodes(
                        driver,
                        discovered,
                        search_filter,
                        group_ids,
                        limit,
                    )

            entity_frontier = [
                uuid for uuid in _dedupe(next_entities) if uuid not in visited_entities
            ]
            visited_entities.update(entity_frontier)
            if not entity_frontier:
                break

        return await self._hydrate_nodes(driver, discovered, search_filter, group_ids, limit)

    async def community_fulltext_search(
        self,
        driver: Any,
        query: str,
        group_ids: list[str] | None = None,
        limit: int = 100,
    ) -> list[Any]:
        search_query = driver.build_fulltext_query(query)
        if not search_query:
            return []

        records = _normalize_records(
            await driver.execute_query(
                """
                SELECT *,
                       math::max([search::score(0), search::score(1)]) AS score
                FROM community
                WHERE """
                + _group_filter_clause(group_ids)
                + """
                  AND (
                      name @0@ $query
                      OR summary @1@ $query
                  )
                ORDER BY score DESC, created_at DESC, uuid DESC
                LIMIT $limit;
                """,
                query=search_query,
                group_ids=group_ids,
                limit=max(int(limit), 1),
            )
        )
        return [_community_from_record(record) for record in records]

    async def community_similarity_search(
        self,
        driver: Any,
        search_vector: list[float],
        group_ids: list[str] | None = None,
        limit: int = 100,
        min_score: float = 0.6,
    ) -> list[Any]:
        if not search_vector:
            return []

        candidate_limit = max(int(limit) * 4, int(limit), 1)
        records = _normalize_records(
            await driver.execute_query(
                "SELECT * FROM ("
                "SELECT *, (1 - vector::distance::knn()) AS score FROM community "
                "WHERE " + _group_filter_clause(group_ids) + " AND name_embedding IS NOT NONE "
                f"AND name_embedding <|{candidate_limit}, 40|> $search_vector"
                ") WHERE score > $min_score "
                "ORDER BY score DESC, created_at DESC, uuid DESC LIMIT $limit;",
                search_vector=search_vector,
                min_score=min_score,
                group_ids=group_ids,
                limit=max(int(limit), 1),
            )
        )
        return [_community_from_record(record) for record in records]

    async def get_embeddings_for_communities(
        self,
        driver: Any,
        communities: list[Any],
    ) -> dict[str, list[float]]:
        uuids = [community.uuid for community in communities]
        if not uuids:
            return {}
        records = _normalize_records(
            await driver.execute_query(
                "SELECT uuid, name_embedding FROM community WHERE uuid IN $uuids;",
                uuids=uuids,
            )
        )
        embeddings: dict[str, list[float]] = {}
        for record in records:
            uuid = _record_uuid(record)
            embedding = _normalize_embedding(record.get("name_embedding"))
            if uuid is not None and embedding is not None:
                embeddings[uuid] = embedding
        return embeddings

    async def node_distance_reranker(
        self,
        driver: Any,
        node_uuids: list[str],
        center_node_uuid: str,
        min_score: float = 0,
    ) -> tuple[list[str], list[float]]:
        filtered_uuids = [uuid for uuid in node_uuids if uuid != center_node_uuid]
        scores: dict[str, float] = {uuid: 0.0 for uuid in filtered_uuids}
        if filtered_uuids:
            records = _normalize_records(
                await driver.execute_query(
                    """
                    SELECT
                        IF in.uuid = $center_uuid THEN out.uuid ELSE in.uuid END AS uuid
                    FROM relates_to
                    WHERE ((
                        in.uuid = $center_uuid AND out.uuid IN $node_uuids
                    ) OR (
                        out.uuid = $center_uuid AND in.uuid IN $node_uuids
                    ))
                    AND group_id = in.group_id
                    AND group_id = out.group_id;
                    """,
                    center_uuid=center_node_uuid,
                    node_uuids=filtered_uuids,
                )
            )
            for record in records:
                uuid = _record_uuid(record)
                if uuid in scores:
                    scores[uuid] = 1.0

        ordered = sorted(filtered_uuids, key=lambda uuid: scores[uuid], reverse=True)
        if center_node_uuid in node_uuids:
            ordered = [center_node_uuid, *ordered]
            scores[center_node_uuid] = 0.1

        return [uuid for uuid in ordered if scores[uuid] >= min_score], [
            scores[uuid] for uuid in ordered if scores[uuid] >= min_score
        ]

    async def episode_mentions_reranker(
        self,
        driver: Any,
        node_uuids: list[list[str]],
        min_score: float = 0,
    ) -> tuple[list[str], list[float]]:
        sorted_uuids = _rrf_uuids(node_uuids)
        if not sorted_uuids:
            return [], []

        records = _normalize_records(
            await driver.execute_query(
                """
                SELECT out.uuid AS uuid
                FROM mentions
                WHERE out.uuid IN $node_uuids
                  AND group_id = in.group_id
                  AND group_id = out.group_id;
                """,
                node_uuids=sorted_uuids,
            )
        )
        scores = {uuid: 0.0 for uuid in sorted_uuids}
        for record in records:
            uuid = record.get("uuid")
            if uuid in scores:
                scores[uuid] += 1.0

        sorted_uuids.sort(key=lambda uuid: scores[uuid], reverse=True)
        return [uuid for uuid in sorted_uuids if scores[uuid] >= min_score], [
            scores[uuid] for uuid in sorted_uuids if scores[uuid] >= min_score
        ]
