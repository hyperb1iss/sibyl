"""SurrealDB implementation of Graphiti's GraphDriver interface.

Each organization's knowledge graph lives in its own SurrealDB namespace
(``org_{hex_uuid}``) under database ``graph``. ``clone(group_id)`` returns a
driver scoped to that org's namespace with a fresh client connection.

Connection model:
    A single AsyncSurreal client per driver instance. clone() creates a new
    driver with its own client so per-org namespace switching is safe under
    concurrent use. Embedded connections (``memory://`` / ``surrealkv://``)
    skip authentication; remote connections (``ws://`` / ``http://``) call
    signin() on first use.

Transaction model:
    Embedded SurrealDB has no transaction support. ``execute_write`` runs
    without BEGIN/COMMIT and relies on caller-side idempotency. WebSocket
    connections could wrap in BEGIN/COMMIT in a later iteration; Phase 1
    ships with the permissive embedded behavior.

Provider tag:
    Graphiti's ``GraphProvider`` enum does not yet include a SurrealDB
    variant. We reuse ``NEO4J`` as the closest semantic neighbor (openCypher
    family, property-graph model). Sibyl's custom operations interfaces
    intercept save/delete dispatch before any provider-specific Cypher is
    emitted, so the tag only affects fallback paths we override.
"""

from __future__ import annotations

import asyncio
import copy
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from functools import cached_property
from typing import Concatenate, Literal, ParamSpec, TypeVar, cast

from graphiti_core.driver.driver import (
    GraphDriver,
    GraphDriverSession,
    GraphProvider,
)

from sibyl_core.backends.surreal.connection import _can_retry_query, _is_transient_connection_error
from sibyl_core.backends.surreal.fulltext import build_fulltext_query
from sibyl_core.backends.surreal.observability import elapsed_ms, log_query, query_start
from sibyl_core.backends.surreal.protocols import QueryParams, SurrealClient

logger = logging.getLogger(__name__)

# See module docstring "Provider tag" for rationale.
_SURREAL_PROVIDER_TAG: GraphProvider = GraphProvider.NEO4J
_MAX_CLOSED_CONNECTION_RETRIES = 2
_EDGE_FULLTEXT_MATCH_HEADROOM = 8
_EDGE_FULLTEXT_MIN_MATCH_LIMIT = 32
type CompatibilityResultKind = Literal[
    "duplicate_pair_records",
    "edge_fulltext_records",
    "episode_records",
    "records",
]
type GraphitiCompatQuery = tuple[str, QueryParams]
type GraphitiCompatQueryWithKind = tuple[str, QueryParams, CompatibilityResultKind]
type SurrealRecord = dict[str, object]
_P = ParamSpec("_P")
_R = TypeVar("_R")


def _object_mapping(value: object) -> Mapping[object, object] | None:
    if not isinstance(value, Mapping):
        return None
    return cast(Mapping[object, object], value)


def _raise_if_surreal_error(query: str, result: object) -> None:
    """Raise a SurrealQueryError when the SDK returned an error envelope."""
    if isinstance(result, str):
        if query.lstrip().upper().startswith("RETURN "):
            return
        raise SurrealQueryError(query, result)
    if isinstance(result, list):
        for entry in result:
            entry_map = _object_mapping(entry)
            if entry_map is not None and entry_map.get("status") == "ERR":
                raise SurrealQueryError(query, str(entry_map.get("result", entry)))


def _significant_query_tokens(query: str) -> list[str]:
    tokens: list[str] = []
    index = 0
    length = len(query)

    while index < length:
        char = query[index]
        next_char = query[index + 1] if index + 1 < length else ""

        if char in {"'", '"', "`"}:
            quote = char
            index += 1
            while index < length:
                current = query[index]
                if current == "\\":
                    index += 2
                    continue
                index += 1
                if current == quote:
                    break
            continue

        if char == "/" and next_char == "*":
            index += 2
            while index + 1 < length and not (query[index] == "*" and query[index + 1] == "/"):
                index += 1
            index = min(index + 2, length)
            continue

        if (char == "-" and next_char == "-") or (char == "/" and next_char == "/"):
            index += 2
            while index < length and query[index] not in "\r\n":
                index += 1
            continue

        if char == "$":
            index += 1
            while index < length and (query[index].isalnum() or query[index] == "_"):
                index += 1
            continue

        if char.isalpha() or char == "_":
            start = index
            index += 1
            while index < length and (query[index].isalnum() or query[index] == "_"):
                index += 1
            tokens.append(query[start:index].upper())
            continue

        index += 1

    return tokens


def _unsupported_graphiti_tokens(query: str) -> set[str]:
    tokens = _significant_query_tokens(query)
    unsupported = {token for token in tokens if token in {"MATCH", "UNWIND"}}
    for index, token in enumerate(tokens[:-1]):
        if token == "CALL" and tokens[index + 1] == "DB":
            unsupported.add("CALL")
    return unsupported


def _raise_if_unsupported_graphiti_query(query: str) -> None:
    unsupported_tokens = _unsupported_graphiti_tokens(query)
    if unsupported_tokens:
        tokens = ", ".join(sorted(unsupported_tokens))
        raise SurrealQueryError(
            query,
            f"Unsupported Graphiti/Cypher query for SurrealDB driver ({tokens})",
        )


class SurrealQueryError(RuntimeError):
    """Raised when SurrealDB returns an error envelope instead of result rows."""

    def __init__(self, query: str, message: str) -> None:
        snippet = (query[:120] + "…") if len(query) > 120 else query
        super().__init__(f"SurrealDB query failed: {message} (query: {snippet!r})")
        self.query = query
        self.surreal_message = message


def _namespace_for_group(prefix: str, group_id: str) -> str:
    """Translate a Sibyl group_id (UUID) into a SurrealDB namespace name.

    SurrealDB namespace names must be alphanumeric plus underscores; UUID
    hyphens are stripped. An empty group_id yields a placeholder namespace
    used only for root-level operations (eg. ``INFO FOR KV``).
    """
    sanitized = group_id.replace("-", "").lower() if group_id else "default"
    return f"{prefix}{sanitized}"


_EPISODE_SELECT_FIELDS = (
    "uuid, name, group_id, created_at, source, source_description, content, valid_at, entity_edges"
)


def _graphiti_episode_records(result: object) -> list[SurrealRecord]:
    from sibyl_core.graph.surreal.ops._common import normalize_records

    records = normalize_records(result)
    for record in records:
        record.setdefault("source_description", None)
        record.setdefault("entity_edges", [])
    return records


def _graphiti_records(result: object) -> list[SurrealRecord]:
    from sibyl_core.graph.surreal.ops._common import normalize_records

    return normalize_records(result)


def _graphiti_duplicate_pair_records(
    result: object,
    pairs: list[tuple[str, str]],
) -> list[SurrealRecord]:
    records = _graphiti_records(result)
    pair_set = set(pairs)
    return [
        record
        for record in records
        if (str(record.get("source_uuid")), str(record.get("target_uuid"))) in pair_set
    ]


def _duplicate_pairs_from_param(value: object) -> list[tuple[str, str]]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        return []
    pairs: list[tuple[str, str]] = []
    for item in value:
        source: object | None = None
        target: object | None = None
        item_map = _object_mapping(item)
        if item_map is not None:
            source = item_map.get("source") or item_map.get("src")
            target = item_map.get("target") or item_map.get("dst")
        elif isinstance(item, Sequence) and not isinstance(item, str | bytes) and len(item) >= 2:
            source = item[0]
            target = item[1]
        if source is None or target is None:
            continue
        pairs.append((str(source), str(target)))
    return pairs


def _limit_from_param(value: object, default: int = 100) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int | float | str):
        return max(int(value), 1)
    return default


def _graphiti_retrieve_episodes_query(
    query: str,
    params: QueryParams,
) -> GraphitiCompatQuery | None:
    normalized = " ".join(query.split()).upper()
    if "RETURN E.UUID AS UUID" not in normalized:
        return None
    if "reference_time" not in params or "num_episodes" not in params:
        return None

    source_clause = "AND source = $source" if params.get("source") is not None else ""
    if "MATCH (S:SAGA" in normalized and "HAS_EPISODE" in normalized:
        return (
            "SELECT "
            f"{_EPISODE_SELECT_FIELDS} "
            "FROM episode "
            "WHERE id IN ("
            "SELECT VALUE out FROM has_episode "
            "WHERE in IN ("
            "SELECT id FROM saga WHERE name = $saga_name AND group_id = $group_id"
            ")"
            ") "
            "AND valid_at <= $reference_time "
            f"{source_clause} "
            "ORDER BY valid_at DESC "
            "LIMIT $num_episodes;",
            params,
        )

    if "MATCH (E:EPISODIC)" not in normalized:
        return None

    group_clause = "AND group_id IN $group_ids" if params.get("group_ids") else ""
    return (
        "SELECT "
        f"{_EPISODE_SELECT_FIELDS} "
        "FROM episode "
        "WHERE valid_at <= $reference_time "
        f"{group_clause} "
        f"{source_clause} "
        "ORDER BY valid_at DESC "
        "LIMIT $num_episodes;",
        params,
    )


def _graphiti_saga_query(
    query: str,
    params: QueryParams,
) -> GraphitiCompatQuery | None:
    normalized = " ".join(query.split()).upper()
    if (
        "MATCH (S:SAGA {NAME: $NAME, GROUP_ID: $GROUP_ID})" in normalized
        and "RETURN S.UUID AS UUID" in normalized
        and "S.NAME AS NAME" in normalized
        and "S.GROUP_ID AS GROUP_ID" in normalized
        and "S.CREATED_AT AS CREATED_AT" in normalized
    ):
        return (
            """
            SELECT uuid, name, group_id, created_at
            FROM saga
            WHERE name = $name AND group_id = $group_id
            LIMIT 1;
            """,
            params,
        )

    if (
        "MATCH (S:SAGA {UUID: $SAGA_UUID})-[:HAS_EPISODE]->(E:EPISODIC)" in normalized
        and "RETURN E.UUID AS UUID" in normalized
        and "ORDER BY E.VALID_AT DESC" in normalized
        and "LIMIT 1" in normalized
    ):
        current_episode_clause = (
            "AND out.uuid != $current_episode_uuid"
            if params.get("current_episode_uuid") is not None
            else ""
        )
        return (
            """
            SELECT out.uuid AS uuid, out.valid_at AS valid_at, out.created_at AS created_at
            FROM has_episode
            WHERE in IN (SELECT VALUE id FROM saga WHERE uuid = $saga_uuid LIMIT 1)
            """
            + current_episode_clause
            + """
            ORDER BY out.valid_at DESC, out.created_at DESC
            LIMIT 1;
            """,
            params,
        )

    return None


def _graphiti_episode_count_query(
    query: str,
    params: QueryParams,
) -> GraphitiCompatQuery | None:
    normalized = " ".join(query.split()).upper()
    if (
        "MATCH (E:EPISODIC)-[:MENTIONS]->(N:ENTITY {UUID: $UUID})" not in normalized
        or "RETURN COUNT(*) AS EPISODE_COUNT" not in normalized
    ):
        return None

    return (
        """
        SELECT out.uuid AS uuid, count() AS episode_count
        FROM mentions
        WHERE out.uuid = $uuid
        GROUP BY out.uuid;
        """,
        params,
    )


def _graphiti_existing_duplicate_edges_query(
    query: str,
    params: QueryParams,
) -> GraphitiCompatQuery | None:
    normalized = " ".join(query.split()).upper()
    if "UNWIND $DUPLICATE_NODE_UUIDS" not in normalized:
        return None
    if "IS_DUPLICATE_OF" not in normalized:
        return None
    if "RETURN DISTINCT" not in normalized:
        return None

    duplicate_pairs = _duplicate_pairs_from_param(params.get("duplicate_node_uuids"))
    source_uuids = [source for source, _ in duplicate_pairs]
    target_uuids = [target for _, target in duplicate_pairs]

    next_params = dict(params)
    next_params["source_uuids"] = source_uuids
    next_params["target_uuids"] = target_uuids
    next_params["duplicate_pairs"] = duplicate_pairs
    if not source_uuids or not target_uuids:
        return "RETURN [];", next_params

    filters = [
        "name = 'IS_DUPLICATE_OF'",
        "in.uuid IN $source_uuids",
        "out.uuid IN $target_uuids",
    ]
    if params.get("group_ids"):
        filters.append("group_id IN $group_ids")

    return (
        """
        SELECT in.uuid AS source_uuid, out.uuid AS target_uuid
        FROM relates_to
        WHERE """
        + " AND ".join(filters)
        + """;
        """,
        next_params,
    )


def _group_filter_clause(params: QueryParams) -> str:
    return "group_id IN $group_ids" if params.get("group_ids") else "true"


def _graphiti_fulltext_query(
    query: str,
    params: QueryParams,
) -> GraphitiCompatQueryWithKind | None:
    normalized = " ".join(query.split())
    upper = normalized.upper()
    fulltext_kind: str | None = None
    if 'CALL DB.INDEX.FULLTEXT.QUERYNODES("NODE_NAME_AND_SUMMARY"' in upper:
        fulltext_kind = "node"
    elif 'CALL DB.INDEX.FULLTEXT.QUERYRELATIONSHIPS("EDGE_NAME_AND_FACT"' in upper:
        fulltext_kind = "edge"
    elif 'CALL DB.INDEX.FULLTEXT.QUERYNODES("EPISODE_CONTENT"' in upper:
        fulltext_kind = "episode"
    elif 'CALL DB.INDEX.FULLTEXT.QUERYNODES("COMMUNITY_NAME"' in upper:
        fulltext_kind = "community"
    else:
        return None

    search_query = build_fulltext_query(str(params.get("query") or ""))
    next_params = dict(params)
    next_params["query"] = search_query
    next_params["limit"] = _limit_from_param(params.get("limit"))

    if not search_query:
        return "RETURN [];", next_params, "records"

    if fulltext_kind == "node":
        return (
            """
            SELECT uuid, name, group_id, created_at, summary, labels, attributes,
                   math::max([
                       search::score(0),
                       search::score(1),
                       search::score(2),
                       search::score(3)
                   ]) AS score
            FROM entity
            WHERE """
            + _group_filter_clause(next_params)
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
            next_params,
            "records",
        )

    if fulltext_kind == "edge":
        next_params["match_limit"] = max(
            int(next_params["limit"]) * _EDGE_FULLTEXT_MATCH_HEADROOM,
            _EDGE_FULLTEXT_MIN_MATCH_LIMIT,
        )
        edge_filters = [_group_filter_clause(next_params), "fact @0@ $query"]
        if params.get("edge_types"):
            edge_filters.append("name IN $edge_types")
        if params.get("edge_uuids"):
            edge_filters.append("uuid IN $edge_uuids")
        return (
            """
            SELECT uuid, created_at, search::score(0) AS score
            FROM relates_to
            WHERE """
            + " AND ".join(edge_filters)
            + """
            ORDER BY score DESC, created_at DESC, uuid DESC
            LIMIT $match_limit;
            """,
            next_params,
            "edge_fulltext_records",
        )

    if fulltext_kind == "episode":
        return (
            """
            SELECT uuid, name, group_id, created_at, source, source_description,
                   content, valid_at, entity_edges, search::score(0) AS score
            FROM episode
            WHERE """
            + _group_filter_clause(next_params)
            + """
              AND content @0@ $query
            ORDER BY score DESC, created_at DESC, uuid DESC
            LIMIT $limit;
            """,
            next_params,
            "records",
        )

    if fulltext_kind == "community":
        return (
            """
            SELECT uuid, group_id, name, created_at, summary, name_embedding,
                   search::score(0) AS score
            FROM community
            WHERE """
            + _group_filter_clause(next_params)
            + """
              AND (name @0@ $query OR summary @1@ $query)
            ORDER BY score DESC, created_at DESC, uuid DESC
            LIMIT $limit;
            """,
            next_params,
            "records",
        )

    return None


def _graphiti_compat_query(
    query: str,
    params: QueryParams,
) -> GraphitiCompatQueryWithKind | None:
    episode_query = _graphiti_retrieve_episodes_query(query, params)
    if episode_query is not None:
        return episode_query[0], episode_query[1], "episode_records"

    saga_query = _graphiti_saga_query(query, params)
    if saga_query is not None:
        return saga_query[0], saga_query[1], "records"

    episode_count_query = _graphiti_episode_count_query(query, params)
    if episode_count_query is not None:
        return episode_count_query[0], episode_count_query[1], "records"

    duplicate_edges_query = _graphiti_existing_duplicate_edges_query(query, params)
    if duplicate_edges_query is not None:
        return duplicate_edges_query[0], duplicate_edges_query[1], "duplicate_pair_records"

    fulltext_query = _graphiti_fulltext_query(query, params)
    if fulltext_query is not None:
        return fulltext_query

    return None


async def _execute_graphiti_edge_fulltext_query(
    client: SurrealClient,
    match_query: str,
    params: QueryParams,
) -> list[SurrealRecord]:
    match_result = await client.query(match_query, params if params else None)
    _raise_if_surreal_error(match_query, match_result)
    match_records = _graphiti_records(match_result)
    match_scores: dict[str, float] = {}
    for record in match_records:
        uuid = record.get("uuid")
        if uuid is not None and uuid != "":
            match_scores[str(uuid)] = float(record.get("score") or 0.0)
    match_uuids = list(match_scores)
    if not match_uuids:
        return []

    hydrate_params = dict(params)
    hydrate_params["match_uuids"] = match_uuids
    hydrate_params["limit"] = len(match_uuids)
    hydrate_query = (
        """
        SELECT uuid, name, fact, fact_embedding, group_id,
               episodes, attributes,
               created_at, expired_at, valid_at, invalid_at,
               in.uuid AS source_node_uuid,
               out.uuid AS target_node_uuid
        FROM relates_to
        WHERE """
        + " AND ".join(["uuid IN $match_uuids", _group_filter_clause(hydrate_params)])
        + """
        LIMIT $limit;
        """
    )
    hydrate_result = await client.query(hydrate_query, hydrate_params)
    _raise_if_surreal_error(hydrate_query, hydrate_result)
    rows_by_uuid = {
        str(record["uuid"]): record
        for record in _graphiti_records(hydrate_result)
        if record.get("uuid")
    }
    records: list[SurrealRecord] = []
    for uuid in match_uuids:
        if uuid not in rows_by_uuid:
            continue
        record = dict(rows_by_uuid[uuid])
        record["score"] = match_scores[uuid]
        records.append(record)
    return records[: _limit_from_param(params.get("limit"))]


class SurrealDriverSession(GraphDriverSession):
    """GraphDriverSession wrapper for SurrealDB.

    The session delegates to the owning driver's client and provides the
    async context manager interface Graphiti expects. Closing the session
    does not close the underlying driver connection (that belongs to the
    driver lifecycle).
    """

    provider: GraphProvider = _SURREAL_PROVIDER_TAG

    def __init__(self, driver: SurrealDriver) -> None:
        self._driver = driver

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        await self.close()

    async def run(self, query: str, **kwargs: object) -> object:
        return await self._driver.execute_query(query, **kwargs)

    async def close(self) -> None:
        return None

    async def execute_write(
        self,
        func: Callable[Concatenate[SurrealDriverSession, _P], Awaitable[_R]],
        *args: _P.args,
        **kwargs: _P.kwargs,
    ) -> _R:
        return await func(self, *args, **kwargs)


class SurrealDriver(GraphDriver):
    """Graphiti ``GraphDriver`` backed by SurrealDB."""

    provider: GraphProvider = _SURREAL_PROVIDER_TAG
    fulltext_syntax: str = ""

    def __init__(
        self,
        url: str,
        *,
        username: str | None = None,
        password: str | None = None,
        token: str | None = None,
        namespace_prefix: str = "org_",
        default_database: str = "graph",
    ) -> None:
        self._url = url
        self._username = username
        self._password = password
        self._token = token
        self._namespace_prefix = namespace_prefix
        self._default_database = default_database
        self._database: str = ""
        self._client: SurrealClient | None = None
        self._query_lock = asyncio.Lock()
        from sibyl_core.graph.search_interface import SurrealSearchInterface
        from sibyl_core.graph.surreal.ops.graph_operations_interface import (
            SurrealGraphOperationsInterface,
        )

        self.search_interface = SurrealSearchInterface()
        self.graph_operations_interface = SurrealGraphOperationsInterface()

    @property
    def namespace_prefix(self) -> str:
        return self._namespace_prefix

    @property
    def default_database(self) -> str:
        return self._default_database

    @property
    def group_id(self) -> str:
        return self._database

    @cached_property
    def _entity_node_ops(self):
        from sibyl_core.graph.surreal.ops.entity_node_ops import SurrealEntityNodeOperations

        return SurrealEntityNodeOperations()

    @property
    def entity_node_ops(self):
        return self._entity_node_ops

    @cached_property
    def _episode_node_ops(self):
        from sibyl_core.graph.surreal.ops.episode_node_ops import SurrealEpisodeNodeOperations

        return SurrealEpisodeNodeOperations()

    @property
    def episode_node_ops(self):
        return self._episode_node_ops

    @cached_property
    def _community_node_ops(self):
        from sibyl_core.graph.surreal.ops.community_node_ops import SurrealCommunityNodeOperations

        return SurrealCommunityNodeOperations()

    @property
    def community_node_ops(self):
        return self._community_node_ops

    @cached_property
    def _saga_node_ops(self):
        from sibyl_core.graph.surreal.ops.saga_node_ops import SurrealSagaNodeOperations

        return SurrealSagaNodeOperations()

    @property
    def saga_node_ops(self):
        return self._saga_node_ops

    @cached_property
    def _entity_edge_ops(self):
        from sibyl_core.graph.surreal.ops.entity_edge_ops import SurrealEntityEdgeOperations

        return SurrealEntityEdgeOperations()

    @property
    def entity_edge_ops(self):
        return self._entity_edge_ops

    @cached_property
    def _episodic_edge_ops(self):
        from sibyl_core.graph.surreal.ops.episodic_edge_ops import SurrealEpisodicEdgeOperations

        return SurrealEpisodicEdgeOperations()

    @property
    def episodic_edge_ops(self):
        return self._episodic_edge_ops

    @cached_property
    def _community_edge_ops(self):
        from sibyl_core.graph.surreal.ops.community_edge_ops import SurrealCommunityEdgeOperations

        return SurrealCommunityEdgeOperations()

    @property
    def community_edge_ops(self):
        return self._community_edge_ops

    @cached_property
    def _has_episode_edge_ops(self):
        from sibyl_core.graph.surreal.ops.has_episode_edge_ops import (
            SurrealHasEpisodeEdgeOperations,
        )

        return SurrealHasEpisodeEdgeOperations()

    @property
    def has_episode_edge_ops(self):
        return self._has_episode_edge_ops

    @cached_property
    def _next_episode_edge_ops(self):
        from sibyl_core.graph.surreal.ops.next_episode_edge_ops import (
            SurrealNextEpisodeEdgeOperations,
        )

        return SurrealNextEpisodeEdgeOperations()

    @property
    def next_episode_edge_ops(self):
        return self._next_episode_edge_ops

    @cached_property
    def _graph_ops(self):
        from sibyl_core.graph.surreal.ops.graph_ops import SurrealGraphMaintenanceOperations

        return SurrealGraphMaintenanceOperations()

    @property
    def graph_ops(self):
        return self._graph_ops

    async def _ensure_client(self) -> SurrealClient:
        if self._client is not None:
            return self._client

        from surrealdb import AsyncSurreal

        client = cast(SurrealClient, AsyncSurreal(self._url))
        try:
            if self._requires_auth():
                if self._token:
                    await client.authenticate(self._token)
                elif self._username and self._password:
                    await client.signin({"username": self._username, "password": self._password})

            namespace = _namespace_for_group(self._namespace_prefix, self._database)
            await client.use(namespace, self._default_database)
        except Exception:
            try:
                await client.close()
            except Exception as exc:
                logger.debug("SurrealDB client close after setup failure failed: %s", exc)
            raise

        self._client = client
        return client

    def _requires_auth(self) -> bool:
        return not self._url.startswith(("memory://", "surrealkv://"))

    async def _drop_client(self) -> None:
        client = self._client
        self._client = None
        if client is not None:
            try:
                await client.close()
            except Exception as exc:
                logger.debug("SurrealDB client close after connection failure failed: %s", exc)

    async def execute_query(self, cypher_query_: str, **kwargs: object) -> object:
        started_at = query_start()
        retry_count = 0
        namespace = _namespace_for_group(self._namespace_prefix, self._database)
        compat_query = _graphiti_compat_query(cypher_query_, kwargs)
        if compat_query is None:
            try:
                _raise_if_unsupported_graphiti_query(cypher_query_)
            except Exception as exc:
                log_query(
                    cypher_query_,
                    client_kind="graph",
                    namespace=namespace,
                    database=self._default_database,
                    raw=False,
                    elapsed=elapsed_ms(started_at),
                    retry_count=retry_count,
                    error=exc,
                )
                raise
        query = compat_query[0] if compat_query is not None else cypher_query_
        params = compat_query[1] if compat_query is not None else kwargs
        compat_kind = compat_query[2] if compat_query is not None else None
        result: object = None
        async with self._query_lock:
            try:
                while True:
                    try:
                        client = await self._ensure_client()
                        if compat_kind == "edge_fulltext_records":
                            result = await _execute_graphiti_edge_fulltext_query(
                                client,
                                query,
                                params,
                            )
                        else:
                            result = await client.query(query, params if params else None)
                        break
                    except Exception as exc:
                        if not _is_transient_connection_error(exc):
                            raise
                        await self._drop_client()
                        if (
                            not _can_retry_query(query)
                            or retry_count >= _MAX_CLOSED_CONNECTION_RETRIES
                        ):
                            raise
                        retry_count += 1
                        logger.warning(
                            "SurrealDB connection failed during read; reconnecting and retrying "
                            "attempt=%s error=%s",
                            retry_count,
                            exc,
                        )
            except Exception as exc:
                log_query(
                    query,
                    client_kind="graph",
                    namespace=namespace,
                    database=self._default_database,
                    raw=False,
                    elapsed=elapsed_ms(started_at),
                    retry_count=retry_count,
                    error=exc,
                )
                raise
        # The surrealdb SDK does not raise on per-statement errors — it returns
        # the error message as a string (single statement) or as `{"status":
        # "ERR", "result": "..."}` (multi-statement). Detect both and raise so
        # bulk paths can't falsely report success on a rejected insert.
        try:
            _raise_if_surreal_error(query, result)
        except Exception as exc:
            log_query(
                query,
                client_kind="graph",
                namespace=namespace,
                database=self._default_database,
                raw=False,
                elapsed=elapsed_ms(started_at),
                retry_count=retry_count,
                error=exc,
            )
            raise
        log_query(
            query,
            client_kind="graph",
            namespace=namespace,
            database=self._default_database,
            raw=False,
            elapsed=elapsed_ms(started_at),
            retry_count=retry_count,
        )
        if compat_kind == "episode_records":
            return _graphiti_episode_records(result), None, None
        if compat_kind == "duplicate_pair_records":
            return (
                _graphiti_duplicate_pair_records(
                    result,
                    _duplicate_pairs_from_param(params.get("duplicate_pairs")),
                ),
                None,
                None,
            )
        if compat_kind == "edge_fulltext_records":
            return _graphiti_records(result), None, None
        if compat_kind == "records":
            return _graphiti_records(result), None, None
        return result

    def session(self, database: str | None = None) -> GraphDriverSession:
        if database is not None and database != self._database:
            return self.clone(database).session()
        return SurrealDriverSession(self)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None

    def clone(self, database: str) -> SurrealDriver:
        if database == self._database and self._client is not None:
            return self
        cloned = copy.copy(self)
        cloned._database = database
        cloned._client = None
        cloned._query_lock = asyncio.Lock()
        return cloned

    async def delete_all_indexes(self) -> None:
        from sibyl_core.backends.surreal.schema import drop_all_indexes

        await drop_all_indexes(self)

    async def build_indices_and_constraints(self, delete_existing: bool = False) -> None:
        if not self._database:
            logger.debug(
                "build_indices_and_constraints called without group_id; "
                "use driver.clone(org_id) first",
            )
            return

        from sibyl_core.backends.surreal.schema import bootstrap_schema

        await bootstrap_schema(self, reset=delete_existing)

    def build_fulltext_query(
        self,
        query: str,
        group_ids: list[str] | None = None,
        max_query_length: int = 128,
    ) -> str:
        del group_ids
        return build_fulltext_query(query, max_query_length=max_query_length)


__all__ = ["SurrealDriver", "SurrealDriverSession", "_namespace_for_group"]
