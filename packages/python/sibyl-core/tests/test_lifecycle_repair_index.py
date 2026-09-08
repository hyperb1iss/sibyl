"""Pending repair discovery and schema upgrade against an isolated graph."""

from sibyl_core.backends.surreal.schema import bootstrap_schema
from sibyl_core.models.entities import Entity, EntityType
from sibyl_core.projection.repair import PENDING_REPAIR_QUERY as QUERY
from sibyl_core.services.graph_common import normalize_graph_records
from tests.test_reflection_identity import runtime as runtime


async def test_repair_index_upgrades_existing_pending_rows_and_tracks_recovery(runtime):
    await runtime.client.execute_query("REMOVE INDEX idx_entity_lifecycle_repair_key ON entity;")
    await runtime.client.execute_query("REMOVE FIELD lifecycle_repair_key ON entity;")
    await runtime.client.execute_query(
        "UPDATE schema_version SET version = 21 WHERE name = 'graph';"
    )
    for row_id, metadata in (
        ("repair-a", {"lifecycle_reconciliation_pending": {"parent:a": True}}),
        ("repair-b", {"source_validation_pending": True}),
        ("healthy", {}),
    ):
        await runtime.entity_manager.create_direct(
            Entity(id=row_id, name=row_id, entity_type=EntityType.EPISODE, metadata=metadata),
            generate_embedding=False,
        )
    await bootstrap_schema(runtime.client)
    params = {"cursor": "", "limit": 10, "group_id": runtime.client.group_id}
    rows = normalize_graph_records(await runtime.client.execute_query(QUERY + ";", **params))
    assert [row["uuid"] for row in rows] == ["repair-a", "repair-b"]
    assert all(row["uuid"] == row["lifecycle_repair_key"] for row in rows)
    plan = normalize_graph_records(
        await runtime.client.execute_query(QUERY + " EXPLAIN;", **params)
    )
    _assert_repair_index(plan)
    page_params = {**params, "cursor": "repair-a", "limit": 1}
    page = normalize_graph_records(await runtime.client.execute_query(QUERY + ";", **page_params))
    assert [row["uuid"] for row in page] == ["repair-b"]
    bounded_plan = normalize_graph_records(
        await runtime.client.execute_query(QUERY + " EXPLAIN;", **page_params)
    )
    _assert_repair_index(bounded_plan, cursor="repair-a")
    # This checks predicate evaluation within one namespace, not tenant isolation.
    assert not normalize_graph_records(
        await runtime.client.execute_query(QUERY + ";", **{**params, "group_id": "other"})
    )
    row = await runtime.entity_manager.get("repair-a")
    await runtime.entity_manager.update(
        row.id,
        {"metadata": {"lifecycle_reconciliation_pending": None}},
        expected_revision=row.observed_revision,
        replace_metadata_keys=("lifecycle_reconciliation_pending",),
    )
    rows = normalize_graph_records(await runtime.client.execute_query(QUERY + ";", **params))
    assert [row["uuid"] for row in rows] == ["repair-b"]
    assert (await runtime.entity_manager.get("repair-b")).metadata["source_validation_pending"]
    await bootstrap_schema(runtime.client, force=True)
    rows = normalize_graph_records(await runtime.client.execute_query(QUERY + ";", **params))
    assert [row["uuid"] for row in rows] == ["repair-b"]


def _assert_repair_index(plan, *, cursor=None):
    """Check range discovery in both supported planner representations.

    Index use does not establish how many records an ordered collector reads.
    Scale qualification must measure that separately on populated databases.
    The server branch is exercised by the external isolated-server qualification;
    this module's default runtime fixture uses the embedded backend.
    """
    nodes = []

    def visit(node):
        nodes.append(node)
        for child in node.get("children", []):
            visit(child)

    for node in plan:
        visit(node)
    assert not any(
        str(node.get("operation", "")).startswith("Iterate Table")
        or node.get("operator") == "TableScan"
        for node in nodes
    ), plan
    indexes = [
        node
        for node in nodes
        if node.get("operation") == "Iterate Index" or node.get("operator") == "IndexScan"
    ]
    assert indexes, plan
    for node in indexes:
        if node.get("operation") == "Iterate Index":
            access = node["detail"]["plan"]
            assert access["index"] == "idx_entity_lifecycle_repair_key", plan
            assert access["from"]["inclusive"] is False, plan
            assert access["from"]["value"] == (cursor or ""), plan
        else:
            access = node["attributes"]
            assert access["index"] == "idx_entity_lifecycle_repair_key", plan
            assert access["access"] == f">{cursor or ''!r}", plan


async def test_repair_key_tracks_updates_and_allows_multiple_healthy_rows(runtime):
    for row_id in ("healthy-a", "healthy-b", "healthy-c"):
        await runtime.entity_manager.create_direct(
            Entity(id=row_id, name=row_id, entity_type=EntityType.EPISODE),
            generate_embedding=False,
        )
    params = {"cursor": "", "limit": 10, "group_id": runtime.client.group_id}
    assert not normalize_graph_records(await runtime.client.execute_query(QUERY + ";", **params))

    row = await runtime.entity_manager.get("healthy-b")
    await runtime.entity_manager.update(
        row.id,
        {"metadata": {"lifecycle_reconciliation_pending": {"parent:absent": True}}},
        expected_revision=row.observed_revision,
        replace_metadata_keys=("lifecycle_reconciliation_pending",),
    )
    rows = normalize_graph_records(await runtime.client.execute_query(QUERY + ";", **params))
    assert rows == [{"uuid": "healthy-b", "lifecycle_repair_key": "healthy-b"}]

    row = await runtime.entity_manager.get("healthy-b")
    await runtime.entity_manager.update(
        row.id,
        {"metadata": {"lifecycle_reconciliation_pending": None}},
        expected_revision=row.observed_revision,
        replace_metadata_keys=("lifecycle_reconciliation_pending",),
    )
    assert not normalize_graph_records(await runtime.client.execute_query(QUERY + ";", **params))
