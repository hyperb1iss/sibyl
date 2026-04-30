"""Tests for the SurrealDB GraphDriver foundation (Wave 1.1)."""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from sibyl_core.backends.surreal import SurrealDriver, SurrealDriverSession
from sibyl_core.backends.surreal.driver import (
    SurrealQueryError,
    _can_retry_query,
    _is_connection_closed_error,
    _namespace_for_group,
)
from sibyl_core.backends.surreal.schema import GRAPH_EDGES, GRAPH_TABLES, bootstrap_schema
from sibyl_core.graph.surreal.ops.community_edge_ops import SurrealCommunityEdgeOperations
from sibyl_core.graph.surreal.ops.community_node_ops import SurrealCommunityNodeOperations
from sibyl_core.graph.surreal.ops.entity_edge_ops import SurrealEntityEdgeOperations
from sibyl_core.graph.surreal.ops.entity_node_ops import SurrealEntityNodeOperations
from sibyl_core.graph.surreal.ops.episode_node_ops import SurrealEpisodeNodeOperations
from sibyl_core.graph.surreal.ops.episodic_edge_ops import SurrealEpisodicEdgeOperations
from sibyl_core.graph.surreal.ops.graph_operations_interface import (
    SurrealGraphOperationsInterface,
)
from sibyl_core.graph.surreal.ops.graph_ops import SurrealGraphMaintenanceOperations
from sibyl_core.graph.surreal.ops.has_episode_edge_ops import SurrealHasEpisodeEdgeOperations
from sibyl_core.graph.surreal.ops.next_episode_edge_ops import SurrealNextEpisodeEdgeOperations
from sibyl_core.graph.surreal.ops.saga_node_ops import SurrealSagaNodeOperations


class TestNamespaceNaming:
    def test_hyphens_stripped_from_uuid(self) -> None:
        assert _namespace_for_group("org_", "abc-123-def") == "org_abc123def"

    def test_uppercase_lowered(self) -> None:
        assert _namespace_for_group("org_", "ABC-DEF") == "org_abcdef"

    def test_empty_group_uses_default(self) -> None:
        assert _namespace_for_group("org_", "") == "org_default"

    def test_custom_prefix(self) -> None:
        assert _namespace_for_group("tenant_", "abc-def") == "tenant_abcdef"


class TestDriverConstruction:
    def test_defaults(self) -> None:
        d = SurrealDriver("memory://")
        assert d.namespace_prefix == "org_"
        assert d.default_database == "graph"
        assert d.group_id == ""

    def test_clone_sets_group_id(self) -> None:
        d = SurrealDriver("memory://")
        scoped = d.clone("org-abc")
        assert scoped.group_id == "org-abc"
        assert d.group_id == ""  # original untouched

    def test_clone_same_group_without_client_returns_new(self) -> None:
        d = SurrealDriver("memory://")
        first = d.clone("org-abc")
        second = first.clone("org-abc")
        # With no client established, repeated clone is safe
        assert second.group_id == "org-abc"

    def test_clone_gets_independent_query_lock(self) -> None:
        d = SurrealDriver("memory://")
        first = d.clone("org-a")
        second = d.clone("org-b")

        assert first._query_lock is not d._query_lock
        assert second._query_lock is not d._query_lock
        assert first._query_lock is not second._query_lock

    def test_session_returns_surreal_session(self) -> None:
        d = SurrealDriver("memory://")
        session = d.session()
        assert isinstance(session, SurrealDriverSession)

    def test_driver_exposes_graphiti_ops(self) -> None:
        d = SurrealDriver("memory://")

        assert isinstance(d.entity_node_ops, SurrealEntityNodeOperations)
        assert isinstance(d.episode_node_ops, SurrealEpisodeNodeOperations)
        assert isinstance(d.community_node_ops, SurrealCommunityNodeOperations)
        assert isinstance(d.saga_node_ops, SurrealSagaNodeOperations)
        assert isinstance(d.entity_edge_ops, SurrealEntityEdgeOperations)
        assert isinstance(d.episodic_edge_ops, SurrealEpisodicEdgeOperations)
        assert isinstance(d.community_edge_ops, SurrealCommunityEdgeOperations)
        assert isinstance(d.has_episode_edge_ops, SurrealHasEpisodeEdgeOperations)
        assert isinstance(d.next_episode_edge_ops, SurrealNextEpisodeEdgeOperations)
        assert isinstance(d.graph_ops, SurrealGraphMaintenanceOperations)
        assert isinstance(d.graph_operations_interface, SurrealGraphOperationsInterface)


class TestBuildFulltextQuery:
    def test_strips_quotes_and_control_chars(self) -> None:
        d = SurrealDriver("memory://")
        out = d.build_fulltext_query('hello "world"\x00')
        assert '"' not in out
        assert "\x00" not in out
        assert "hello" in out

    def test_truncates_long_input(self) -> None:
        d = SurrealDriver("memory://")
        long_query = "x" * 500
        out = d.build_fulltext_query(long_query, max_query_length=64)
        assert len(out) == 64

    def test_empty_input_returns_empty(self) -> None:
        d = SurrealDriver("memory://")
        assert d.build_fulltext_query("   ") == ""


class TestConnectionRetry:
    def test_detects_websocket_connection_closed_errors(self) -> None:
        ConnectionClosedError = type(
            "ConnectionClosedError",
            (RuntimeError,),
            {"__module__": "websockets.exceptions"},
        )

        assert _is_connection_closed_error(
            ConnectionClosedError("sent 1011 keepalive ping timeout")
        )

    def test_retryable_queries_are_read_only(self) -> None:
        assert _can_retry_query("SELECT * FROM entity;")
        assert _can_retry_query(" RETURN true;")
        assert not _can_retry_query("SELECT * FROM entity; CREATE entity CONTENT $record;")
        assert not _can_retry_query("CREATE entity CONTENT $record;")
        assert not _can_retry_query("DELETE FROM entity;")


@pytest.mark.asyncio
class TestDriverConnection:
    async def test_execute_query_serializes_shared_client_calls(self, monkeypatch) -> None:
        driver = SurrealDriver("memory://").clone("org-abc")
        active_queries = 0
        max_active_queries = 0

        async def fake_query(query: str, params: object | None = None) -> list[dict[str, object]]:
            nonlocal active_queries, max_active_queries
            active_queries += 1
            max_active_queries = max(max_active_queries, active_queries)
            await asyncio.sleep(0)
            active_queries -= 1
            return [{"query": query, "params": params}]

        async def fake_ensure_client() -> SimpleNamespace:
            return SimpleNamespace(query=fake_query)

        monkeypatch.setattr(
            driver,
            "_ensure_client",
            fake_ensure_client,
        )

        await asyncio.gather(
            driver.execute_query("RETURN $value;", value="a"),
            driver.execute_query("RETURN $value;", value="b"),
        )

        assert max_active_queries == 1

    async def test_execute_query_roundtrip(self, surreal_driver: SurrealDriver) -> None:
        info = await surreal_driver.execute_query("INFO FOR DB;")
        assert isinstance(info, dict)
        assert "tables" in info

    async def test_parameters_bind(self, surreal_driver: SurrealDriver) -> None:
        # Must not fail on bound params; exact shape varies by SurrealDB version.
        result = await surreal_driver.execute_query("RETURN $value;", value="hello")
        assert result == "hello" or (isinstance(result, list) and result and result[0] == "hello")

    async def test_execute_query_still_raises_string_errors_for_writes(self, monkeypatch) -> None:
        driver = SurrealDriver("memory://").clone("org-abc")

        async def fake_query(query: str, params: object | None = None) -> str:
            return "Expected a array<float, 1024> but the array had 1536 items"

        async def fake_ensure_client() -> SimpleNamespace:
            return SimpleNamespace(query=fake_query)

        monkeypatch.setattr(
            driver,
            "_ensure_client",
            fake_ensure_client,
        )

        with pytest.raises(SurrealQueryError, match="array<float"):
            await driver.execute_query("CREATE entity SET uuid = $uuid;", uuid="ent-1")

    async def test_execute_query_reconnects_and_retries_closed_read_socket(
        self, monkeypatch
    ) -> None:
        class ConnectionClosedError(RuntimeError):
            pass

        ConnectionClosedError.__module__ = "websockets.exceptions"
        clients: list[FakeAsyncSurreal] = []

        class FakeAsyncSurreal:
            def __init__(self, url: str) -> None:
                self.url = url
                self.closed = False
                clients.append(self)

            async def signin(self, credentials: dict[str, str]) -> None:
                self.credentials = credentials

            async def use(self, namespace: str, database: str) -> None:
                self.namespace = namespace
                self.database = database

            async def query(self, query: str, params: object | None = None) -> list[dict[str, str]]:
                if len(clients) == 1:
                    raise ConnectionClosedError("sent 1011 keepalive ping timeout")
                return [{"ok": "yes"}]

            async def close(self) -> None:
                self.closed = True

        monkeypatch.setitem(sys.modules, "surrealdb", SimpleNamespace(AsyncSurreal=FakeAsyncSurreal))
        driver = SurrealDriver("ws://localhost:8000/rpc", username="root", password="root").clone(
            "org-abc"
        )

        result = await driver.execute_query("SELECT * FROM entity;")

        assert result == [{"ok": "yes"}]
        assert len(clients) == 2
        assert clients[0].closed is True
        assert clients[1].namespace == "org_orgabc"

    async def test_execute_query_translates_graphiti_episode_retrieval(self, monkeypatch) -> None:
        driver = SurrealDriver("memory://").clone("org-abc")
        calls: list[tuple[str, object | None]] = []

        async def fake_query(query: str, params: object | None = None) -> list[dict[str, object]]:
            calls.append((query, params))
            return [
                {
                    "uuid": "episode-1",
                    "name": "Episode",
                    "group_id": "org-abc",
                    "created_at": "2026-01-01T00:00:00Z",
                    "source": "message",
                    "content": "hello",
                    "valid_at": "2026-01-01T00:00:00Z",
                }
            ]

        async def fake_ensure_client() -> SimpleNamespace:
            return SimpleNamespace(query=fake_query)

        monkeypatch.setattr(driver, "_ensure_client", fake_ensure_client)

        records, _, _ = await driver.execute_query(
            """
            MATCH (e:Episodic)
            WHERE e.valid_at <= $reference_time
            AND e.group_id IN $group_ids
            AND e.source = $source
            RETURN
                e.uuid AS uuid
            ORDER BY e.valid_at DESC
            LIMIT $num_episodes
            """,
            reference_time="2026-01-01T00:00:00Z",
            num_episodes=3,
            group_ids=["org-abc"],
            source="message",
        )

        query, params = calls[0]
        assert "MATCH" not in query
        assert "FROM episode" in query
        assert "group_id IN $group_ids" in query
        assert "source = $source" in query
        assert params is not None
        assert records[0]["source_description"] is None
        assert records[0]["entity_edges"] == []

    async def test_execute_query_translates_graphiti_saga_episode_retrieval(
        self, monkeypatch
    ) -> None:
        driver = SurrealDriver("memory://").clone("org-abc")
        calls: list[tuple[str, object | None]] = []

        async def fake_query(query: str, params: object | None = None) -> list[dict[str, object]]:
            calls.append((query, params))
            return []

        async def fake_ensure_client() -> SimpleNamespace:
            return SimpleNamespace(query=fake_query)

        monkeypatch.setattr(driver, "_ensure_client", fake_ensure_client)

        records, _, _ = await driver.execute_query(
            """
            MATCH (s:Saga {name: $saga_name, group_id: $group_id})-[:HAS_EPISODE]->(e:Episodic)
            WHERE e.valid_at <= $reference_time
            RETURN
                e.uuid AS uuid
            ORDER BY e.valid_at DESC
            LIMIT $num_episodes
            """,
            saga_name="daily",
            group_id="org-abc",
            reference_time="2026-01-01T00:00:00Z",
            num_episodes=3,
        )

        query, _params = calls[0]
        assert records == []
        assert "MATCH" not in query
        assert "FROM has_episode" in query
        assert "FROM saga" in query

    async def test_execute_query_translates_graphiti_saga_lookup(self, monkeypatch) -> None:
        driver = SurrealDriver("memory://").clone("org-abc")
        calls: list[tuple[str, object | None]] = []

        async def fake_query(query: str, params: object | None = None) -> list[dict[str, object]]:
            calls.append((query, params))
            return [
                {
                    "uuid": "saga-1",
                    "name": "daily",
                    "group_id": "org-abc",
                    "created_at": "2026-01-01T00:00:00Z",
                }
            ]

        async def fake_ensure_client() -> SimpleNamespace:
            return SimpleNamespace(query=fake_query)

        monkeypatch.setattr(driver, "_ensure_client", fake_ensure_client)

        records, _, _ = await driver.execute_query(
            """
            MATCH (s:Saga {name: $name, group_id: $group_id})
            RETURN s.uuid AS uuid, s.name AS name, s.group_id AS group_id, s.created_at AS created_at
            """,
            name="daily",
            group_id="org-abc",
            routing_="r",
        )

        query, params = calls[0]
        assert "MATCH" not in query
        assert "FROM saga" in query
        assert "WHERE name = $name AND group_id = $group_id" in query
        assert isinstance(params, dict)
        assert params["name"] == "daily"
        assert records[0]["uuid"] == "saga-1"

    async def test_execute_query_translates_graphiti_previous_saga_episode_lookup(
        self, monkeypatch
    ) -> None:
        driver = SurrealDriver("memory://").clone("org-abc")
        calls: list[tuple[str, object | None]] = []

        async def fake_query(query: str, params: object | None = None) -> list[dict[str, object]]:
            calls.append((query, params))
            return [{"uuid": "episode-prev"}]

        async def fake_ensure_client() -> SimpleNamespace:
            return SimpleNamespace(query=fake_query)

        monkeypatch.setattr(driver, "_ensure_client", fake_ensure_client)

        records, _, _ = await driver.execute_query(
            """
            MATCH (s:Saga {uuid: $saga_uuid})-[:HAS_EPISODE]->(e:Episodic)
            WHERE e.uuid <> $current_episode_uuid
            RETURN e.uuid AS uuid
            ORDER BY e.valid_at DESC, e.created_at DESC
            LIMIT 1
            """,
            saga_uuid="saga-1",
            current_episode_uuid="episode-current",
            routing_="r",
        )

        query, params = calls[0]
        assert "MATCH" not in query
        assert "FROM has_episode" in query
        assert "FROM saga" in query
        assert "out.uuid != $current_episode_uuid" in query
        assert isinstance(params, dict)
        assert params["saga_uuid"] == "saga-1"
        assert records[0]["uuid"] == "episode-prev"

    async def test_execute_query_graphiti_previous_saga_episode_lookup_roundtrips(
        self, surreal_schema: SurrealDriver
    ) -> None:
        created = datetime(2026, 1, 1, tzinfo=UTC)
        older = datetime(2026, 1, 2, tzinfo=UTC)
        newer = datetime(2026, 1, 3, tzinfo=UTC)

        await surreal_schema.execute_query(
            """
            UPSERT saga:saga_live SET
                uuid = 'saga-live',
                name = 'daily',
                group_id = $group_id,
                created_at = $created;
            UPSERT episode:episode_old SET
                uuid = 'episode-old',
                name = 'Old episode',
                source = 'message',
                content = 'old',
                group_id = $group_id,
                created_at = $created,
                valid_at = $older;
            UPSERT episode:episode_new SET
                uuid = 'episode-new',
                name = 'New episode',
                source = 'message',
                content = 'new',
                group_id = $group_id,
                created_at = $created,
                valid_at = $newer;
            RELATE saga:saga_live->has_episode->episode:episode_old SET
                uuid = 'has-old',
                group_id = $group_id,
                created_at = $created;
            RELATE saga:saga_live->has_episode->episode:episode_new SET
                uuid = 'has-new',
                group_id = $group_id,
                created_at = $created;
            """,
            group_id=surreal_schema.group_id,
            created=created,
            older=older,
            newer=newer,
        )

        records, _, _ = await surreal_schema.execute_query(
            """
            MATCH (s:Saga {uuid: $saga_uuid})-[:HAS_EPISODE]->(e:Episodic)
            WHERE e.uuid <> $current_episode_uuid
            RETURN e.uuid AS uuid
            ORDER BY e.valid_at DESC, e.created_at DESC
            LIMIT 1
            """,
            saga_uuid="saga-live",
            current_episode_uuid="episode-new",
        )

        assert records[0]["uuid"] == "episode-old"

    async def test_execute_query_does_not_translate_other_saga_queries(self, monkeypatch) -> None:
        driver = SurrealDriver("memory://").clone("org-abc")
        calls: list[tuple[str, object | None]] = []

        async def fake_query(query: str, params: object | None = None) -> list[dict[str, object]]:
            calls.append((query, params))
            return [{"count": 1}]

        async def fake_ensure_client() -> SimpleNamespace:
            return SimpleNamespace(query=fake_query)

        monkeypatch.setattr(driver, "_ensure_client", fake_ensure_client)

        await driver.execute_query(
            """
            MATCH (s:Saga {name: $name, group_id: $group_id})
            RETURN count(s) AS count
            """,
            name="daily",
            group_id="org-abc",
        )

        query, _params = calls[0]
        assert "MATCH (s:Saga" in query

    async def test_execute_query_does_not_translate_other_saga_episode_queries(
        self, monkeypatch
    ) -> None:
        driver = SurrealDriver("memory://").clone("org-abc")
        calls: list[tuple[str, object | None]] = []

        async def fake_query(query: str, params: object | None = None) -> list[dict[str, object]]:
            calls.append((query, params))
            return [{"content": "episode content"}]

        async def fake_ensure_client() -> SimpleNamespace:
            return SimpleNamespace(query=fake_query)

        monkeypatch.setattr(driver, "_ensure_client", fake_ensure_client)

        await driver.execute_query(
            """
            MATCH (s:Saga {uuid: $saga_uuid})-[:HAS_EPISODE]->(e:Episodic)
            RETURN e.content AS content
            """,
            saga_uuid="saga-1",
        )

        query, _params = calls[0]
        assert "MATCH (s:Saga" in query

    async def test_execute_query_translates_graphiti_episode_count(self, monkeypatch) -> None:
        driver = SurrealDriver("memory://").clone("org-abc")
        calls: list[tuple[str, object | None]] = []

        async def fake_query(query: str, params: object | None = None) -> list[dict[str, object]]:
            calls.append((query, params))
            return [{"episode_count": 2}]

        async def fake_ensure_client() -> SimpleNamespace:
            return SimpleNamespace(query=fake_query)

        monkeypatch.setattr(driver, "_ensure_client", fake_ensure_client)

        records, _, _ = await driver.execute_query(
            """
            MATCH (e:Episodic)-[:MENTIONS]->(n:Entity {uuid: $uuid})
            RETURN count(*) AS episode_count
            """,
            uuid="entity-1",
            routing_="r",
        )

        query, params = calls[0]
        assert "MATCH" not in query
        assert "FROM mentions" in query
        assert "GROUP BY out.uuid" in query
        assert isinstance(params, dict)
        assert params["uuid"] == "entity-1"
        assert records[0]["episode_count"] == 2

    async def test_execute_query_graphiti_episode_count_roundtrips(
        self, surreal_schema: SurrealDriver
    ) -> None:
        created = datetime(2026, 1, 1, tzinfo=UTC)

        await surreal_schema.execute_query(
            """
            UPSERT entity:entity_count SET
                uuid = 'entity-count',
                name = 'Counted entity',
                entity_type = 'concept',
                group_id = $group_id,
                created_at = $created;
            UPSERT episode:episode_count_one SET
                uuid = 'episode-count-one',
                name = 'First episode',
                source = 'message',
                content = 'first',
                group_id = $group_id,
                created_at = $created,
                valid_at = $created;
            UPSERT episode:episode_count_two SET
                uuid = 'episode-count-two',
                name = 'Second episode',
                source = 'message',
                content = 'second',
                group_id = $group_id,
                created_at = $created,
                valid_at = $created;
            RELATE episode:episode_count_one->mentions->entity:entity_count SET
                uuid = 'mention-count-one',
                group_id = $group_id,
                created_at = $created;
            RELATE episode:episode_count_two->mentions->entity:entity_count SET
                uuid = 'mention-count-two',
                group_id = $group_id,
                created_at = $created;
            """,
            group_id=surreal_schema.group_id,
            created=created,
        )

        records, _, _ = await surreal_schema.execute_query(
            """
            MATCH (e:Episodic)-[:MENTIONS]->(n:Entity {uuid: $uuid})
            RETURN count(*) AS episode_count
            """,
            uuid="entity-count",
        )

        assert records[0]["episode_count"] == 2

    async def test_execute_query_translates_graphiti_existing_duplicate_edge_lookup(
        self, monkeypatch
    ) -> None:
        driver = SurrealDriver("memory://").clone("org-abc")
        calls: list[tuple[str, object | None]] = []

        async def fake_query(query: str, params: object | None = None) -> list[dict[str, object]]:
            calls.append((query, params))
            return [
                {"source_uuid": "entity-a", "target_uuid": "entity-b"},
                {"source_uuid": "entity-a", "target_uuid": "entity-d"},
            ]

        async def fake_ensure_client() -> SimpleNamespace:
            return SimpleNamespace(query=fake_query)

        monkeypatch.setattr(driver, "_ensure_client", fake_ensure_client)

        records, _, _ = await driver.execute_query(
            """
            UNWIND $duplicate_node_uuids AS duplicate_tuple
            MATCH (n:Entity {uuid: duplicate_tuple[0]})-[r:RELATES_TO {name: 'IS_DUPLICATE_OF'}]->(m:Entity {uuid: duplicate_tuple[1]})
            RETURN DISTINCT
                n.uuid AS source_uuid,
                m.uuid AS target_uuid
            """,
            duplicate_node_uuids=[("entity-a", "entity-b"), ("entity-c", "entity-d")],
            group_ids=["org-abc"],
            routing_="r",
        )

        query, params = calls[0]
        assert "UNWIND" not in query
        assert "MATCH" not in query
        assert "FROM relates_to" in query
        assert "name = 'IS_DUPLICATE_OF'" in query
        assert "in.uuid IN $source_uuids" in query
        assert "out.uuid IN $target_uuids" in query
        assert "group_id IN $group_ids" in query
        assert isinstance(params, dict)
        assert params["source_uuids"] == ["entity-a", "entity-c"]
        assert params["target_uuids"] == ["entity-b", "entity-d"]
        assert params["duplicate_pairs"] == [("entity-a", "entity-b"), ("entity-c", "entity-d")]
        assert len(records) == 1
        assert records[0]["source_uuid"] == "entity-a"
        assert records[0]["target_uuid"] == "entity-b"

    async def test_execute_query_graphiti_existing_duplicate_edge_lookup_roundtrips(
        self, surreal_schema: SurrealDriver
    ) -> None:
        created = datetime(2026, 1, 1, tzinfo=UTC)

        await surreal_schema.execute_query(
            """
            UPSERT entity:duplicate_source SET
                uuid = 'duplicate-source',
                name = 'Duplicate source',
                entity_type = 'concept',
                group_id = $group_id,
                created_at = $created;
            UPSERT entity:duplicate_target SET
                uuid = 'duplicate-target',
                name = 'Duplicate target',
                entity_type = 'concept',
                group_id = $group_id,
                created_at = $created;
            UPSERT entity:duplicate_missing SET
                uuid = 'duplicate-missing',
                name = 'Duplicate missing',
                entity_type = 'concept',
                group_id = $group_id,
                created_at = $created;
            UPSERT entity:duplicate_cross_target SET
                uuid = 'duplicate-cross-target',
                name = 'Duplicate cross target',
                entity_type = 'concept',
                group_id = $group_id,
                created_at = $created;
            RELATE entity:duplicate_source->relates_to:duplicate_edge->entity:duplicate_target SET
                uuid = 'duplicate-edge',
                name = 'IS_DUPLICATE_OF',
                fact = 'source duplicates target',
                group_id = $group_id,
                episodes = [],
                attributes = {},
                created_at = $created,
                valid_at = $created;
            RELATE entity:duplicate_source->relates_to:duplicate_cross_edge->entity:duplicate_cross_target SET
                uuid = 'duplicate-cross-edge',
                name = 'IS_DUPLICATE_OF',
                fact = 'source duplicates cross target',
                group_id = $group_id,
                episodes = [],
                attributes = {},
                created_at = $created,
                valid_at = $created;
            """,
            group_id=surreal_schema.group_id,
            created=created,
        )

        records, _, _ = await surreal_schema.execute_query(
            """
            UNWIND $duplicate_node_uuids AS duplicate_tuple
            MATCH (n:Entity {uuid: duplicate_tuple[0]})-[r:RELATES_TO {name: 'IS_DUPLICATE_OF'}]->(m:Entity {uuid: duplicate_tuple[1]})
            RETURN DISTINCT
                n.uuid AS source_uuid,
                m.uuid AS target_uuid
            """,
            duplicate_node_uuids=[
                ("duplicate-source", "duplicate-target"),
                ("duplicate-missing", "duplicate-cross-target"),
            ],
        )

        assert len(records) == 1
        assert records[0]["source_uuid"] == "duplicate-source"
        assert records[0]["target_uuid"] == "duplicate-target"

    async def test_execute_query_translates_graphiti_node_fulltext_call(self, monkeypatch) -> None:
        driver = SurrealDriver("memory://").clone("org-abc")
        calls: list[tuple[str, object | None]] = []

        async def fake_query(query: str, params: object | None = None) -> list[dict[str, object]]:
            calls.append((query, params))
            return [
                {
                    "uuid": "node-1",
                    "name": "Surreal memory",
                    "group_id": "org-abc",
                    "created_at": "2026-01-01T00:00:00Z",
                    "summary": "native fulltext",
                    "labels": ["Entity"],
                    "attributes": {},
                }
            ]

        async def fake_ensure_client() -> SimpleNamespace:
            return SimpleNamespace(query=fake_query)

        monkeypatch.setattr(driver, "_ensure_client", fake_ensure_client)

        records, _, _ = await driver.execute_query(
            """
            CALL db.index.fulltext.queryNodes("node_name_and_summary", $query, {limit: $limit})
            YIELD node AS n, score
            RETURN n.uuid AS uuid
            """,
            query="surreal memory",
            group_ids=["org-abc"],
            limit=5,
        )

        query, params = calls[0]
        assert "CALL" not in query
        assert "MATCH" not in query
        assert "FROM entity" in query
        assert "name @0@ $query" in query
        assert isinstance(params, dict)
        assert params["query"] == "surreal memory"
        assert params["group_ids"] == ["org-abc"]
        assert records[0]["uuid"] == "node-1"

    async def test_execute_query_translates_graphiti_relationship_fulltext_call(
        self, monkeypatch
    ) -> None:
        driver = SurrealDriver("memory://").clone("org-abc")
        calls: list[tuple[str, object | None]] = []

        async def fake_query(query: str, params: object | None = None) -> list[dict[str, object]]:
            calls.append((query, params))
            return [
                {
                    "uuid": "edge-1",
                    "name": "RELATES_TO",
                    "fact": "Surreal search",
                    "group_id": "org-abc",
                    "episodes": [],
                    "attributes": {},
                    "created_at": "2026-01-01T00:00:00Z",
                    "expired_at": None,
                    "valid_at": "2026-01-01T00:00:00Z",
                    "invalid_at": None,
                    "source_node_uuid": "source-1",
                    "target_node_uuid": "target-1",
                }
            ]

        async def fake_ensure_client() -> SimpleNamespace:
            return SimpleNamespace(query=fake_query)

        monkeypatch.setattr(driver, "_ensure_client", fake_ensure_client)

        records, _, _ = await driver.execute_query(
            """
            CALL db.index.fulltext.queryRelationships("edge_name_and_fact", $query, {limit: $limit})
            YIELD relationship AS rel, score
            RETURN rel.uuid AS uuid
            """,
            query="surreal search",
            group_ids=["org-abc"],
            edge_uuids=["edge-1"],
            limit=5,
        )

        query, params = calls[0]
        assert "CALL" not in query
        assert "MATCH" not in query
        assert "FROM relates_to" in query
        assert "fact @0@ $query" in query
        assert "uuid IN $edge_uuids" in query
        assert isinstance(params, dict)
        assert params["edge_uuids"] == ["edge-1"]
        assert records[0]["uuid"] == "edge-1"

    async def test_execute_query_translates_graphiti_episode_fulltext_call(
        self, monkeypatch
    ) -> None:
        driver = SurrealDriver("memory://").clone("org-abc")
        calls: list[tuple[str, object | None]] = []

        async def fake_query(query: str, params: object | None = None) -> list[dict[str, object]]:
            calls.append((query, params))
            return [{"uuid": "episode-1", "content": "Surreal raw memory"}]

        async def fake_ensure_client() -> SimpleNamespace:
            return SimpleNamespace(query=fake_query)

        monkeypatch.setattr(driver, "_ensure_client", fake_ensure_client)

        records, _, _ = await driver.execute_query(
            """
            CALL db.index.fulltext.queryNodes("episode_content", $query, {limit: $limit})
            YIELD node AS episode, score
            RETURN episode.uuid AS uuid
            """,
            query="raw memory",
            group_ids=["org-abc"],
            limit=5,
        )

        query, _params = calls[0]
        assert "CALL" not in query
        assert "MATCH" not in query
        assert "FROM episode" in query
        assert "content @0@ $query" in query
        assert records[0]["uuid"] == "episode-1"

    async def test_execute_query_translates_graphiti_community_fulltext_call(
        self, monkeypatch
    ) -> None:
        driver = SurrealDriver("memory://").clone("org-abc")
        calls: list[tuple[str, object | None]] = []

        async def fake_query(query: str, params: object | None = None) -> list[dict[str, object]]:
            calls.append((query, params))
            return [{"uuid": "community-1", "name": "Surreal community"}]

        async def fake_ensure_client() -> SimpleNamespace:
            return SimpleNamespace(query=fake_query)

        monkeypatch.setattr(driver, "_ensure_client", fake_ensure_client)

        records, _, _ = await driver.execute_query(
            """
            CALL db.index.fulltext.queryNodes("community_name", $query, {limit: $limit})
            YIELD node AS c, score
            RETURN c.uuid AS uuid
            """,
            query="community",
            group_ids=["org-abc"],
            limit=0,
        )

        query, params = calls[0]
        assert "CALL" not in query
        assert "MATCH" not in query
        assert "FROM community" in query
        assert "name @0@ $query" in query
        assert isinstance(params, dict)
        assert params["limit"] == 1
        assert records[0]["uuid"] == "community-1"

    async def test_execute_query_does_not_retry_closed_write_socket(self, monkeypatch) -> None:
        class ConnectionClosedError(RuntimeError):
            pass

        ConnectionClosedError.__module__ = "websockets.exceptions"
        clients: list[FakeAsyncSurreal] = []

        class FakeAsyncSurreal:
            def __init__(self, url: str) -> None:
                self.url = url
                self.closed = False
                clients.append(self)

            async def signin(self, credentials: dict[str, str]) -> None:
                self.credentials = credentials

            async def use(self, namespace: str, database: str) -> None:
                self.namespace = namespace
                self.database = database

            async def query(self, query: str, params: object | None = None) -> list[dict[str, str]]:
                raise ConnectionClosedError("sent 1011 keepalive ping timeout")

            async def close(self) -> None:
                self.closed = True

        monkeypatch.setitem(sys.modules, "surrealdb", SimpleNamespace(AsyncSurreal=FakeAsyncSurreal))
        driver = SurrealDriver("ws://localhost:8000/rpc", username="root", password="root").clone(
            "org-abc"
        )

        with pytest.raises(ConnectionClosedError):
            await driver.execute_query("CREATE entity SET uuid = $uuid;", uuid="ent-1")

        assert len(clients) == 1
        assert clients[0].closed is True

    async def test_execute_query_retries_closed_socket_during_connect(self, monkeypatch) -> None:
        class ConnectionClosedError(RuntimeError):
            pass

        ConnectionClosedError.__module__ = "websockets.exceptions"
        clients: list[FakeAsyncSurreal] = []

        class FakeAsyncSurreal:
            def __init__(self, url: str) -> None:
                self.url = url
                self.closed = False
                clients.append(self)

            async def signin(self, credentials: dict[str, str]) -> None:
                self.credentials = credentials

            async def use(self, namespace: str, database: str) -> None:
                self.namespace = namespace
                self.database = database
                if len(clients) == 1:
                    raise ConnectionClosedError("sent 1011 keepalive ping timeout")

            async def query(self, query: str, params: object | None = None) -> list[dict[str, str]]:
                return [{"ok": "yes"}]

            async def close(self) -> None:
                self.closed = True

        monkeypatch.setitem(sys.modules, "surrealdb", SimpleNamespace(AsyncSurreal=FakeAsyncSurreal))
        driver = SurrealDriver("ws://localhost:8000/rpc", username="root", password="root").clone(
            "org-abc"
        )

        result = await driver.execute_query("SELECT * FROM entity;")

        assert result == [{"ok": "yes"}]
        assert len(clients) == 2
        assert clients[0].closed is True
        assert clients[1].namespace == "org_orgabc"

    async def test_execute_query_allows_two_closed_read_retries(self, monkeypatch) -> None:
        class ConnectionClosedError(RuntimeError):
            pass

        ConnectionClosedError.__module__ = "websockets.exceptions"
        clients: list[FakeAsyncSurreal] = []

        class FakeAsyncSurreal:
            def __init__(self, url: str) -> None:
                self.url = url
                self.closed = False
                clients.append(self)

            async def signin(self, credentials: dict[str, str]) -> None:
                self.credentials = credentials

            async def use(self, namespace: str, database: str) -> None:
                self.namespace = namespace
                self.database = database

            async def query(self, query: str, params: object | None = None) -> list[dict[str, str]]:
                if len(clients) <= 2:
                    raise ConnectionClosedError("sent 1011 keepalive ping timeout")
                return [{"ok": "yes"}]

            async def close(self) -> None:
                self.closed = True

        monkeypatch.setitem(sys.modules, "surrealdb", SimpleNamespace(AsyncSurreal=FakeAsyncSurreal))
        driver = SurrealDriver("ws://localhost:8000/rpc", username="root", password="root").clone(
            "org-abc"
        )

        result = await driver.execute_query("SELECT * FROM entity;")

        assert result == [{"ok": "yes"}]
        assert len(clients) == 3
        assert clients[0].closed is True
        assert clients[1].closed is True


@pytest.mark.asyncio
class TestSchemaBootstrap:
    async def test_bootstrap_creates_all_tables(self, surreal_schema: SurrealDriver) -> None:
        info = await surreal_schema.execute_query("INFO FOR DB;")
        tables = info.get("tables", {}) if isinstance(info, dict) else {}
        for name in (*GRAPH_TABLES, *GRAPH_EDGES):
            assert name in tables, f"missing table: {name}"

    async def test_bootstrap_is_idempotent(self, surreal_driver: SurrealDriver) -> None:
        # First call creates, second call must not error.
        await surreal_driver.build_indices_and_constraints()
        await surreal_driver.build_indices_and_constraints()

    async def test_bootstrap_upgrades_existing_schemaless_tables(
        self, surreal_driver: SurrealDriver
    ) -> None:
        await surreal_driver.execute_query("DEFINE TABLE entity SCHEMALESS;")
        await surreal_driver.build_indices_and_constraints()
        await surreal_driver.build_indices_and_constraints()

    async def test_bootstrap_continues_when_unique_index_hits_dirty_duplicates(
        self,
    ) -> None:
        class FakeDriver:
            group_id = "org-dirty"
            _url = "ws://127.0.0.1:8000/rpc"

            def __init__(self) -> None:
                self.statements: list[str] = []

            async def execute_query(self, statement: str) -> None:
                self.statements.append(statement)
                if "idx_episode_uuid" in statement:
                    raise RuntimeError(
                        "Database index `idx_episode_uuid` already contains 'episode-1'"
                    )

        driver = FakeDriver()
        await bootstrap_schema(driver)  # type: ignore[arg-type]

        assert any("idx_episode_group" in statement for statement in driver.statements)

    async def test_bootstrap_without_group_id_raises(self) -> None:
        d = SurrealDriver("memory://")

        with pytest.raises(ValueError, match="group_id"):
            await bootstrap_schema(d)

    async def test_entity_crud_roundtrip(self, surreal_schema: SurrealDriver) -> None:
        # Create
        await surreal_schema.execute_query(
            "CREATE entity SET uuid = $uuid, name = $name, entity_type = $etype, group_id = $gid;",
            uuid="ent-1",
            name="Alice",
            etype="person",
            gid=surreal_schema.group_id,
        )
        # Read
        rows = await surreal_schema.execute_query(
            "SELECT * FROM entity WHERE uuid = $uuid;", uuid="ent-1"
        )
        assert isinstance(rows, list) and len(rows) == 1
        assert rows[0]["name"] == "Alice"
        assert rows[0]["entity_type"] == "person"
        assert rows[0]["labels"] == []
        assert rows[0]["attributes"] == {}

    async def test_entity_fulltext_indexes_cover_search_fields(
        self, surreal_schema: SurrealDriver
    ) -> None:
        gid = surreal_schema.group_id
        await surreal_schema.execute_query(
            """
            CREATE entity SET
                uuid = "ent-fts",
                name = "namelight",
                entity_type = "pattern",
                group_id = $gid,
                summary = "summaryglow",
                attributes = {
                    description: "descriptionglow",
                    content: "contentglow",
                    metadata: {}
                };
            """,
            gid=gid,
        )

        query = """
            SELECT uuid,
                   math::max([
                       search::score(0),
                       search::score(1),
                       search::score(2),
                       search::score(3)
                   ]) AS search_score
            FROM entity
            WHERE group_id = $gid
              AND (
                  name @0@ $term
                  OR summary @1@ $term
                  OR attributes.description @2@ $term
                  OR attributes.content @3@ $term
              )
            ORDER BY search_score DESC
            LIMIT 10;
        """
        for term in ("namelight", "summaryglow", "descriptionglow", "contentglow"):
            rows = await surreal_schema.execute_query(query, gid=gid, term=term)
            assert isinstance(rows, list)
            assert [row["uuid"] for row in rows] == ["ent-fts"]
            assert "search_score" in rows[0]

    async def test_relates_to_edge_roundtrip(self, surreal_schema: SurrealDriver) -> None:
        gid = surreal_schema.group_id
        # Create two entities
        for uuid_ in ("ent-a", "ent-b"):
            await surreal_schema.execute_query(
                "CREATE entity SET uuid = $uuid, name = $name, "
                "entity_type = 'person', group_id = $gid;",
                uuid=uuid_,
                name=uuid_,
                gid=gid,
            )
        # Create a relates_to edge with full temporal payload
        await surreal_schema.execute_query(
            """
            LET $src = (SELECT id FROM entity WHERE uuid = $src_uuid LIMIT 1)[0].id;
            LET $tgt = (SELECT id FROM entity WHERE uuid = $tgt_uuid LIMIT 1)[0].id;
            RELATE $src->relates_to->$tgt SET
                uuid = $edge_uuid,
                name = 'KNOWS',
                fact = 'Alice knows Bob',
                group_id = $gid,
                episodes = [],
                attributes = { confidence: 0.9 };
            """,
            src_uuid="ent-a",
            tgt_uuid="ent-b",
            edge_uuid="edge-1",
            gid=gid,
        )
        rows = await surreal_schema.execute_query(
            "SELECT uuid, name, fact, attributes FROM relates_to WHERE uuid = $edge_uuid;",
            edge_uuid="edge-1",
        )
        assert isinstance(rows, list) and len(rows) == 1
        assert rows[0]["fact"] == "Alice knows Bob"
        assert rows[0]["attributes"]["confidence"] == 0.9


def test_legacy_graph_surreal_path_reexports_backend_driver() -> None:
    from sibyl_core.graph.surreal import SurrealDriver as LegacySurrealDriver

    assert LegacySurrealDriver is SurrealDriver
