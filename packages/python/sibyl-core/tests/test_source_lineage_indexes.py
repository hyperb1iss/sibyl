"""Exercise reverse provenance queries against populated embedded indexes."""

from types import SimpleNamespace
from uuid import uuid4

import pytest

from sibyl_core.backends.surreal import SurrealContentClient
from sibyl_core.backends.surreal.content_schema import bootstrap_content_schema
from sibyl_core.services.graph_client import SurrealGraphClient, prepare_graph_schema
from sibyl_core.services.memory_lineage import _graph_descendant_ids


@pytest.mark.asyncio
async def test_graph_lineage_queries_use_each_index_and_cross_page_boundary(monkeypatch):
    organization_id = str(uuid4())
    client = SurrealGraphClient(group_id=organization_id, url="memory://")
    try:
        await prepare_graph_schema(client)
        rows = [
            {
                "uuid": f"derived-{index:04}",
                "group_id": organization_id,
                "name": "Derived memory",
                "entity_type": "note",
                "labels": [],
                "attributes": {"raw_source_ids": ["root"]},
            }
            for index in range(513)
        ]
        for field, source in (
            ("review_capture_id", "root"),
            ("raw_memory_id", "root"),
            ("parent_entity_id", "parent"),
            ("source_entity_id", "parent"),
        ):
            rows.append(
                {
                    "uuid": field,
                    "group_id": organization_id,
                    "name": "Linked memory",
                    "entity_type": "note",
                    "labels": [],
                    "attributes": {field: source, "projection_kind": "passage"},
                }
            )
        rows.append(
            {
                "uuid": "unrelated",
                "group_id": organization_id,
                "name": "Unrelated memory",
                "entity_type": "note",
                "labels": [],
                "attributes": {"raw_source_ids": ["different-root"]},
            }
        )
        await client.execute_query("INSERT INTO entity $rows;", rows=rows)
        execute = client.execute_query
        plans = []

        async def inspect_query(query, **params):
            result = await execute(query, **params)
            plans.append(await execute(query.rstrip(";") + " EXPLAIN;", **params))
            return result

        monkeypatch.setattr(client, "execute_query", inspect_query)
        found = [
            entity_id
            async for entity_id in _graph_descendant_ids(
                SimpleNamespace(client=client),
                organization_id=organization_id,
                raw_ids=["root"],
                graph_ids=["parent"],
            )
        ]
        assert set(found) == {row["uuid"] for row in rows[:-1]}
        assert len(found) == 517
        assert len(plans) == 6  # Two source pages, then four other indexed arms.
        for plan in plans:
            assert any(step["operation"] == "Iterate Index" for step in plan)
            assert all(step["operation"] != "Iterate Table" for step in plan)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_raw_lineage_element_index_returns_sources_without_table_scan():
    organization_id = str(uuid4())
    client = SurrealContentClient(url="memory://")
    try:
        await bootstrap_content_schema(client, reset=True)
        await client.execute_query(
            "INSERT INTO raw_captures $rows;",
            rows=[
                {
                    "uuid": f"raw-{index:04}",
                    "organization_id": organization_id,
                    "title": "Raw source",
                    "raw_content": "Evidence",
                    "metadata": {"raw_source_ids": ["root" if index == 3 else "other"]},
                }
                for index in range(40)
            ],
        )
        query = (
            "SELECT uuid FROM raw_captures WITH INDEX idx_raw_captures_source_lineage "
            "WHERE organization_id = $organization_id AND uuid > $cursor "
            "AND metadata.raw_source_ids.* CONTAINSANY $source_ids "
            "ORDER BY uuid LIMIT $limit"
        )
        params = dict(organization_id=organization_id, cursor="", source_ids=["root"], limit=512)
        assert await client.execute_query(query + ";", **params) == [{"uuid": "raw-0003"}]
        plan = await client.execute_query(query + " EXPLAIN;", **params)
        assert any(step["operation"] == "Iterate Index" for step in plan)
        assert all(step["operation"] != "Iterate Table" for step in plan)
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("missing_name_field", [False, True])
async def test_graph_lineage_upgrade_preserves_existing_entity_fields(missing_name_field):
    from sibyl_core.backends.surreal.schema import bootstrap_schema

    org = str(uuid4())
    client = SurrealGraphClient(group_id=org, url="memory://")
    try:
        await prepare_graph_schema(client)
        for suffix in (
            "raw_sources",
            "raw_memory",
            "review_capture",
            "projection_parent",
            "projection_source",
        ):
            await client.execute_query(f"REMOVE INDEX idx_entity_{suffix} ON entity;")
        await client.execute_query("UPDATE schema_version SET version = 20 WHERE name = 'graph';")
        await client.execute_query(
            "CREATE entity:retained CONTENT $row;",
            row={
                "uuid": "retained",
                "group_id": org,
                "name": "Original title",
                "entity_type": "note",
                "labels": ["retained"],
                "attributes": {
                    "raw_source_ids": ["root"],
                    "content": "Original evidence",
                    "nested": {"kept": True},
                },
            },
        )
        before = await client.execute_query("SELECT * FROM entity:retained;")
        assert before[0]["name"] == "Original title"
        assert before[0]["attributes"]["content"] == "Original evidence"
        if missing_name_field:
            await client.execute_query("REMOVE FIELD name ON entity;")
            assert await client.execute_query("SELECT * FROM entity:retained;") == before
        await bootstrap_schema(client)
        assert await client.execute_query("SELECT * FROM entity:retained;") == before
        found = [
            entity_id
            async for entity_id in _graph_descendant_ids(
                SimpleNamespace(client=client),
                organization_id=org,
                raw_ids=["root"],
                graph_ids=[],
            )
        ]
        assert found == ["retained"]
    finally:
        await client.close()
