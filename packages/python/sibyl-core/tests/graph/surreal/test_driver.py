"""Tests for the SurrealDB GraphDriver foundation (Wave 1.1)."""

from __future__ import annotations

import pytest

from sibyl_core.backends.surreal import SurrealDriver, SurrealDriverSession
from sibyl_core.backends.surreal.driver import _namespace_for_group
from sibyl_core.backends.surreal.schema import GRAPH_EDGES, GRAPH_TABLES


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

    def test_session_returns_surreal_session(self) -> None:
        d = SurrealDriver("memory://")
        session = d.session()
        assert isinstance(session, SurrealDriverSession)


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
    async def test_execute_query_roundtrip(self, surreal_driver: SurrealDriver) -> None:
        info = await surreal_driver.execute_query("INFO FOR DB;")
        assert isinstance(info, dict)
        assert "tables" in info

    async def test_parameters_bind(self, surreal_driver: SurrealDriver) -> None:
        # Must not fail on bound params; exact shape varies by SurrealDB version.
        result = await surreal_driver.execute_query("RETURN $value;", value="hello")
        assert result == "hello" or (isinstance(result, list) and result and result[0] == "hello")


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
