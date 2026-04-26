"""Tests for the SurrealDB GraphDriver foundation (Wave 1.1)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from sibyl_core.backends.surreal import SurrealDriver, SurrealDriverSession
from sibyl_core.backends.surreal.driver import SurrealQueryError, _namespace_for_group
from sibyl_core.backends.surreal.schema import GRAPH_EDGES, GRAPH_TABLES
from sibyl_core.graph.surreal.ops.community_edge_ops import SurrealCommunityEdgeOperations
from sibyl_core.graph.surreal.ops.community_node_ops import SurrealCommunityNodeOperations
from sibyl_core.graph.surreal.ops.entity_edge_ops import SurrealEntityEdgeOperations
from sibyl_core.graph.surreal.ops.entity_node_ops import SurrealEntityNodeOperations
from sibyl_core.graph.surreal.ops.episode_node_ops import SurrealEpisodeNodeOperations
from sibyl_core.graph.surreal.ops.episodic_edge_ops import SurrealEpisodicEdgeOperations
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

    async def test_bootstrap_without_group_id_raises(self) -> None:
        d = SurrealDriver("memory://")
        from sibyl_core.backends.surreal.schema import bootstrap_schema

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
