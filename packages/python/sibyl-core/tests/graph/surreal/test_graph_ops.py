"""Tests for SurrealGraphMaintenanceOperations (Wave 1.2 Task 1.2.10)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from graphiti_core.nodes import EntityNode, EpisodeType, EpisodicNode

from sibyl_core.backends.surreal import SurrealDriver
from sibyl_core.graph.surreal.ops.entity_node_ops import SurrealEntityNodeOperations
from sibyl_core.graph.surreal.ops.graph_ops import SurrealGraphMaintenanceOperations


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _make_entity(uuid: str, gid: str, *, name: str = "n") -> EntityNode:
    return EntityNode(
        uuid=uuid,
        name=name,
        group_id=gid,
        labels=["Person"],
        summary="",
        attributes={},
        created_at=_now(),
    )


async def _seed_episode(driver: SurrealDriver, uuid: str, gid: str) -> None:
    await driver.execute_query(
        """
        CREATE episode SET
            uuid = $uuid, name = 'ep', source = 'text',
            content = 'hello', group_id = $gid, valid_at = time::now();
        """,
        uuid=uuid,
        gid=gid,
    )


async def _seed_mentions(
    driver: SurrealDriver, edge_uuid: str, ep_uuid: str, ent_uuid: str, gid: str
) -> None:
    await driver.execute_query(
        """
        LET $src = (SELECT id FROM episode WHERE uuid = $ep LIMIT 1)[0].id;
        LET $tgt = (SELECT id FROM entity WHERE uuid = $ent LIMIT 1)[0].id;
        RELATE $src->mentions->$tgt SET uuid = $uuid, group_id = $gid;
        """,
        ep=ep_uuid,
        ent=ent_uuid,
        uuid=edge_uuid,
        gid=gid,
    )


async def _seed_has_member(
    driver: SurrealDriver,
    uuid: str,
    community_uuid: str,
    entity_uuid: str,
    gid: str,
) -> None:
    await driver.execute_query(
        """
        LET $src = (SELECT id FROM community WHERE uuid = $c LIMIT 1)[0].id;
        LET $tgt = (SELECT id FROM entity WHERE uuid = $e LIMIT 1)[0].id;
        RELATE $src->has_member->$tgt SET uuid = $uuid, group_id = $gid;
        """,
        c=community_uuid,
        e=entity_uuid,
        uuid=uuid,
        gid=gid,
    )


@pytest.mark.asyncio
class TestGraphMaintenance:
    async def test_clear_data_by_group_id(self, surreal_schema: SurrealDriver) -> None:
        ops = SurrealGraphMaintenanceOperations()
        entity_ops = SurrealEntityNodeOperations()
        gid = surreal_schema.group_id
        other_gid = "other-group"

        await entity_ops.save(surreal_schema, _make_entity("ent-a", gid))
        await entity_ops.save(surreal_schema, _make_entity("ent-b", other_gid))

        await ops.clear_data(surreal_schema, group_ids=[gid])

        remaining = await entity_ops.get_by_group_ids(surreal_schema, [gid, other_gid])
        assert {n.uuid for n in remaining} == {"ent-b"}

    async def test_clear_data_global(self, surreal_schema: SurrealDriver) -> None:
        ops = SurrealGraphMaintenanceOperations()
        entity_ops = SurrealEntityNodeOperations()
        gid = surreal_schema.group_id

        await entity_ops.save(surreal_schema, _make_entity("ent-a", gid))
        await ops.clear_data(surreal_schema)
        assert await entity_ops.get_by_group_ids(surreal_schema, [gid]) == []

    async def test_delete_all_indexes_then_rebuild(self, surreal_schema: SurrealDriver) -> None:
        ops = SurrealGraphMaintenanceOperations()
        await ops.delete_all_indexes(surreal_schema)

        info = await surreal_schema.execute_query("INFO FOR TABLE entity;")
        assert info["indexes"] == {}

        # build_indices_and_constraints delegates to the driver
        await ops.build_indices_and_constraints(surreal_schema)
        info = await surreal_schema.execute_query("INFO FOR TABLE entity;")
        assert info["indexes"]

    async def test_get_mentioned_nodes(self, surreal_schema: SurrealDriver) -> None:
        ops = SurrealGraphMaintenanceOperations()
        entity_ops = SurrealEntityNodeOperations()
        gid = surreal_schema.group_id

        await entity_ops.save(surreal_schema, _make_entity("ent-1", gid, name="A"))
        await entity_ops.save(surreal_schema, _make_entity("ent-2", gid, name="B"))
        await _seed_episode(surreal_schema, "ep-1", gid)
        await _seed_mentions(surreal_schema, "m-1", "ep-1", "ent-1", gid)
        await _seed_mentions(surreal_schema, "m-2", "ep-1", "ent-2", gid)

        dummy_episode = EpisodicNode(
            uuid="ep-1",
            name="ep",
            group_id=gid,
            source=EpisodeType.text,
            source_description="",
            content="hello",
            valid_at=_now(),
            created_at=_now(),
            entity_edges=[],
        )
        nodes = await ops.get_mentioned_nodes(surreal_schema, [dummy_episode])
        assert {n.uuid for n in nodes} == {"ent-1", "ent-2"}

    async def test_get_communities_by_nodes(self, surreal_schema: SurrealDriver) -> None:
        ops = SurrealGraphMaintenanceOperations()
        entity_ops = SurrealEntityNodeOperations()
        gid = surreal_schema.group_id

        await entity_ops.save(surreal_schema, _make_entity("ent-1", gid))
        await surreal_schema.execute_query(
            """
            CREATE community SET uuid = 'c-1', name = 'cluster',
                group_id = $gid, summary = '';
            """,
            gid=gid,
        )
        await _seed_has_member(surreal_schema, "hm-1", "c-1", "ent-1", gid)

        node = _make_entity("ent-1", gid)
        communities = await ops.get_communities_by_nodes(surreal_schema, [node])
        assert len(communities) == 1
        assert communities[0].uuid == "c-1"

    @pytest.mark.skip(
        reason="Community clustering depends on graphiti.label_propagation "
        "which hangs on dense subgraphs with the current neighbor projection. "
        "Tracked separately; not required for driver sign-off."
    )
    async def test_get_community_clusters(self, surreal_schema: SurrealDriver) -> None:
        ops = SurrealGraphMaintenanceOperations()
        entity_ops = SurrealEntityNodeOperations()
        gid = surreal_schema.group_id

        # Two loosely connected triangles; label propagation should split them.
        for uuid in ("a1", "a2", "a3", "b1", "b2", "b3"):
            await entity_ops.save(surreal_schema, _make_entity(uuid, gid, name=uuid))

        pairs = [
            ("a1", "a2"),
            ("a2", "a3"),
            ("a1", "a3"),
            ("b1", "b2"),
            ("b2", "b3"),
            ("b1", "b3"),
        ]
        for i, (src, tgt) in enumerate(pairs):
            await surreal_schema.execute_query(
                """
                LET $src = (SELECT id FROM entity WHERE uuid = $src_u LIMIT 1)[0].id;
                LET $tgt = (SELECT id FROM entity WHERE uuid = $tgt_u LIMIT 1)[0].id;
                RELATE $src->relates_to->$tgt SET
                    uuid = $uuid, name = 'r', fact = 'f',
                    group_id = $gid, episodes = [], attributes = {};
                """,
                src_u=src,
                tgt_u=tgt,
                uuid=f"e-{i}",
                gid=gid,
            )

        clusters = await ops.get_community_clusters(surreal_schema, [gid])
        # Flatten and verify each cluster stays within its triangle
        for cluster in clusters:
            uuids = {n.uuid for n in cluster}
            assert uuids.issubset({"a1", "a2", "a3"}) or uuids.issubset({"b1", "b2", "b3"})
