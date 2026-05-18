"""Tests for SurrealEpisodicEdgeOperations (Wave 1.2 edge ops)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from sibyl_core.backends.surreal import SurrealDriver
from sibyl_core.graph.surreal.compat.models import EdgeNotFoundError, EpisodicEdge
from sibyl_core.graph.surreal.compat.ops.episodic_edge_ops import SurrealEpisodicEdgeOperations


async def _seed_entity(driver: SurrealDriver, uuid: str, name: str = "E") -> None:
    await driver.execute_query(
        "CREATE entity SET uuid = $uuid, name = $name, entity_type = 'test', group_id = $gid;",
        uuid=uuid,
        name=name,
        gid=driver.group_id,
    )


async def _seed_episode(driver: SurrealDriver, uuid: str, name: str = "ep") -> None:
    await driver.execute_query(
        "CREATE episode SET uuid = $uuid, name = $name, source = 'text', "
        "content = 'x', group_id = $gid;",
        uuid=uuid,
        name=name,
        gid=driver.group_id,
    )


def _make_edge(uuid: str, group_id: str, src: str, tgt: str) -> EpisodicEdge:
    return EpisodicEdge(
        uuid=uuid,
        group_id=group_id,
        source_node_uuid=src,
        target_node_uuid=tgt,
        created_at=datetime.now(UTC).replace(tzinfo=None),
    )


@pytest.mark.asyncio
class TestEpisodicEdgeOps:
    async def test_save_and_get_by_uuid(self, surreal_schema: SurrealDriver) -> None:
        ops = SurrealEpisodicEdgeOperations()
        gid = surreal_schema.group_id
        await _seed_episode(surreal_schema, "ep-1")
        await _seed_entity(surreal_schema, "ent-1")

        await ops.save(surreal_schema, _make_edge("m-1", gid, "ep-1", "ent-1"))
        fetched = await ops.get_by_uuid(surreal_schema, "m-1")

        assert fetched.uuid == "m-1"
        assert fetched.group_id == gid
        assert fetched.source_node_uuid == "ep-1"
        assert fetched.target_node_uuid == "ent-1"

    async def test_get_by_uuid_missing_raises(self, surreal_schema: SurrealDriver) -> None:
        ops = SurrealEpisodicEdgeOperations()
        with pytest.raises(EdgeNotFoundError):
            await ops.get_by_uuid(surreal_schema, "nope")

    async def test_save_missing_endpoint_raises(self, surreal_schema: SurrealDriver) -> None:
        ops = SurrealEpisodicEdgeOperations()
        gid = surreal_schema.group_id
        # no seeding: endpoints don't exist
        with pytest.raises(ValueError, match="not found"):
            await ops.save(surreal_schema, _make_edge("m-x", gid, "missing-ep", "missing-ent"))

    async def test_get_by_uuids_returns_subset(self, surreal_schema: SurrealDriver) -> None:
        ops = SurrealEpisodicEdgeOperations()
        gid = surreal_schema.group_id
        await _seed_episode(surreal_schema, "ep-1")
        await _seed_entity(surreal_schema, "ent-1")
        await _seed_entity(surreal_schema, "ent-2")

        await ops.save(surreal_schema, _make_edge("m-a", gid, "ep-1", "ent-1"))
        await ops.save(surreal_schema, _make_edge("m-b", gid, "ep-1", "ent-2"))

        results = await ops.get_by_uuids(surreal_schema, ["m-a", "m-b", "m-zzz"])
        uuids = {r.uuid for r in results}
        assert uuids == {"m-a", "m-b"}

    async def test_get_between_nodes(self, surreal_schema: SurrealDriver) -> None:
        ops = SurrealEpisodicEdgeOperations()
        gid = surreal_schema.group_id
        await _seed_episode(surreal_schema, "ep-1")
        await _seed_entity(surreal_schema, "ent-1")
        await _seed_entity(surreal_schema, "ent-2")

        await ops.save(surreal_schema, _make_edge("m-a", gid, "ep-1", "ent-1"))
        await ops.save(surreal_schema, _make_edge("m-b", gid, "ep-1", "ent-2"))

        results = await ops.get_between_nodes(
            surreal_schema,
            "ep-1",
            "ent-1",
            group_ids=[gid],
        )
        assert [edge.uuid for edge in results] == ["m-a"]

    async def test_get_by_node_uuid_matches_source_or_target(
        self, surreal_schema: SurrealDriver
    ) -> None:
        ops = SurrealEpisodicEdgeOperations()
        gid = surreal_schema.group_id
        await _seed_episode(surreal_schema, "ep-1")
        await _seed_episode(surreal_schema, "ep-2")
        await _seed_entity(surreal_schema, "ent-1")
        await _seed_entity(surreal_schema, "ent-2")

        await ops.save(surreal_schema, _make_edge("m-a", gid, "ep-1", "ent-1"))
        await ops.save(surreal_schema, _make_edge("m-b", gid, "ep-2", "ent-1"))
        await ops.save(surreal_schema, _make_edge("m-c", gid, "ep-2", "ent-2"))

        by_episode = await ops.get_by_node_uuid(surreal_schema, "ep-2", group_ids=[gid])
        by_entity = await ops.get_by_node_uuid(surreal_schema, "ent-1", group_ids=[gid])

        assert {edge.uuid for edge in by_episode} == {"m-b", "m-c"}
        assert {edge.uuid for edge in by_entity} == {"m-a", "m-b"}

    async def test_save_bulk_and_group_query(self, surreal_schema: SurrealDriver) -> None:
        ops = SurrealEpisodicEdgeOperations()
        gid = surreal_schema.group_id
        await _seed_episode(surreal_schema, "ep-1")
        for i in range(5):
            await _seed_entity(surreal_schema, f"ent-{i}")
        edges = [_make_edge(f"m-{i}", gid, "ep-1", f"ent-{i}") for i in range(5)]

        await ops.save_bulk(surreal_schema, edges, batch_size=2)
        listed = await ops.get_by_group_ids(surreal_schema, [gid])
        assert {e.uuid for e in listed} == {f"m-{i}" for i in range(5)}

    async def test_save_overwrites_on_same_uuid(self, surreal_schema: SurrealDriver) -> None:
        ops = SurrealEpisodicEdgeOperations()
        gid = surreal_schema.group_id
        await _seed_episode(surreal_schema, "ep-1")
        await _seed_entity(surreal_schema, "ent-1")
        await _seed_entity(surreal_schema, "ent-2")

        await ops.save(surreal_schema, _make_edge("m-1", gid, "ep-1", "ent-1"))
        await ops.save(surreal_schema, _make_edge("m-1", gid, "ep-1", "ent-2"))

        fetched = await ops.get_by_uuid(surreal_schema, "m-1")
        assert fetched.target_node_uuid == "ent-2"
        # Ensure only one row exists under this uuid (no duplicate RELATE).
        all_rows = await ops.get_by_uuids(surreal_schema, ["m-1"])
        assert len(all_rows) == 1

    async def test_delete_single(self, surreal_schema: SurrealDriver) -> None:
        ops = SurrealEpisodicEdgeOperations()
        gid = surreal_schema.group_id
        await _seed_episode(surreal_schema, "ep-1")
        await _seed_entity(surreal_schema, "ent-1")

        edge = _make_edge("m-1", gid, "ep-1", "ent-1")
        await ops.save(surreal_schema, edge)
        await ops.delete(surreal_schema, edge)

        with pytest.raises(EdgeNotFoundError):
            await ops.get_by_uuid(surreal_schema, "m-1")

    async def test_delete_by_uuids(self, surreal_schema: SurrealDriver) -> None:
        ops = SurrealEpisodicEdgeOperations()
        gid = surreal_schema.group_id
        await _seed_episode(surreal_schema, "ep-1")
        await _seed_entity(surreal_schema, "ent-1")
        await _seed_entity(surreal_schema, "ent-2")

        await ops.save(surreal_schema, _make_edge("m-a", gid, "ep-1", "ent-1"))
        await ops.save(surreal_schema, _make_edge("m-b", gid, "ep-1", "ent-2"))

        await ops.delete_by_uuids(surreal_schema, ["m-a"])
        remaining = await ops.get_by_uuids(surreal_schema, ["m-a", "m-b"])
        assert {r.uuid for r in remaining} == {"m-b"}

    async def test_get_by_group_ids_respects_limit_and_cursor(
        self, surreal_schema: SurrealDriver
    ) -> None:
        ops = SurrealEpisodicEdgeOperations()
        gid = surreal_schema.group_id
        await _seed_episode(surreal_schema, "ep-1")
        for i in range(6):
            await _seed_entity(surreal_schema, f"ent-{i:02d}")
            await ops.save(
                surreal_schema,
                _make_edge(f"m-{i:02d}", gid, "ep-1", f"ent-{i:02d}"),
            )

        first_page = await ops.get_by_group_ids(surreal_schema, [gid], limit=3)
        assert len(first_page) == 3
        assert first_page[0].uuid == "m-05"
        cursor = first_page[-1].uuid
        second_page = await ops.get_by_group_ids(surreal_schema, [gid], limit=3, uuid_cursor=cursor)
        assert [e.uuid for e in second_page] == ["m-02", "m-01", "m-00"]
