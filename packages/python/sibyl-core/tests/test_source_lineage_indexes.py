"""Raw source lineage lookup uses its element index."""

from uuid import uuid4

from sibyl_core.backends.surreal import SurrealContentClient
from sibyl_core.backends.surreal.content_schema import bootstrap_content_schema


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
