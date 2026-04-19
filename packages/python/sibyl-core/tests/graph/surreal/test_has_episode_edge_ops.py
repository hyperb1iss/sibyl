"""Tests for SurrealHasEpisodeEdgeOperations (Wave 1.2 edge ops)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from graphiti_core.edges import HasEpisodeEdge
from graphiti_core.errors import EdgeNotFoundError

from sibyl_core.backends.surreal import SurrealDriver
from sibyl_core.graph.surreal.ops.has_episode_edge_ops import SurrealHasEpisodeEdgeOperations


async def _seed_saga(driver: SurrealDriver, uuid: str, name: str = "s") -> None:
    await driver.execute_query(
        "CREATE saga SET uuid = $uuid, name = $name, group_id = $gid;",
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


def _make_edge(uuid: str, group_id: str, src: str, tgt: str) -> HasEpisodeEdge:
    return HasEpisodeEdge(
        uuid=uuid,
        group_id=group_id,
        source_node_uuid=src,
        target_node_uuid=tgt,
        created_at=datetime.now(UTC).replace(tzinfo=None),
    )


@pytest.mark.asyncio
class TestHasEpisodeEdgeOps:
    async def test_save_and_get_by_uuid(self, surreal_schema: SurrealDriver) -> None:
        ops = SurrealHasEpisodeEdgeOperations()
        gid = surreal_schema.group_id
        await _seed_saga(surreal_schema, "saga-1")
        await _seed_episode(surreal_schema, "ep-1")

        await ops.save(surreal_schema, _make_edge("he-1", gid, "saga-1", "ep-1"))
        fetched = await ops.get_by_uuid(surreal_schema, "he-1")

        assert fetched.uuid == "he-1"
        assert fetched.group_id == gid
        assert fetched.source_node_uuid == "saga-1"
        assert fetched.target_node_uuid == "ep-1"

    async def test_get_by_uuid_missing_raises(self, surreal_schema: SurrealDriver) -> None:
        ops = SurrealHasEpisodeEdgeOperations()
        with pytest.raises(EdgeNotFoundError):
            await ops.get_by_uuid(surreal_schema, "nope")

    async def test_save_missing_endpoint_raises(self, surreal_schema: SurrealDriver) -> None:
        ops = SurrealHasEpisodeEdgeOperations()
        gid = surreal_schema.group_id
        with pytest.raises(ValueError, match="not found"):
            await ops.save(surreal_schema, _make_edge("he-x", gid, "missing-saga", "missing-ep"))

    async def test_save_bulk_and_group_query(self, surreal_schema: SurrealDriver) -> None:
        ops = SurrealHasEpisodeEdgeOperations()
        gid = surreal_schema.group_id
        await _seed_saga(surreal_schema, "saga-1")
        for i in range(5):
            await _seed_episode(surreal_schema, f"ep-{i}")
        edges = [_make_edge(f"he-{i}", gid, "saga-1", f"ep-{i}") for i in range(5)]

        await ops.save_bulk(surreal_schema, edges)
        listed = await ops.get_by_group_ids(surreal_schema, [gid])
        assert {e.uuid for e in listed} == {f"he-{i}" for i in range(5)}

    async def test_save_overwrites_on_same_uuid(self, surreal_schema: SurrealDriver) -> None:
        ops = SurrealHasEpisodeEdgeOperations()
        gid = surreal_schema.group_id
        await _seed_saga(surreal_schema, "saga-1")
        await _seed_episode(surreal_schema, "ep-1")
        await _seed_episode(surreal_schema, "ep-2")

        await ops.save(surreal_schema, _make_edge("he-1", gid, "saga-1", "ep-1"))
        await ops.save(surreal_schema, _make_edge("he-1", gid, "saga-1", "ep-2"))

        fetched = await ops.get_by_uuid(surreal_schema, "he-1")
        assert fetched.target_node_uuid == "ep-2"
        all_rows = await ops.get_by_uuids(surreal_schema, ["he-1"])
        assert len(all_rows) == 1

    async def test_delete_single(self, surreal_schema: SurrealDriver) -> None:
        ops = SurrealHasEpisodeEdgeOperations()
        gid = surreal_schema.group_id
        await _seed_saga(surreal_schema, "saga-1")
        await _seed_episode(surreal_schema, "ep-1")

        edge = _make_edge("he-1", gid, "saga-1", "ep-1")
        await ops.save(surreal_schema, edge)
        await ops.delete(surreal_schema, edge)

        with pytest.raises(EdgeNotFoundError):
            await ops.get_by_uuid(surreal_schema, "he-1")

    async def test_delete_by_uuids(self, surreal_schema: SurrealDriver) -> None:
        ops = SurrealHasEpisodeEdgeOperations()
        gid = surreal_schema.group_id
        await _seed_saga(surreal_schema, "saga-1")
        await _seed_episode(surreal_schema, "ep-1")
        await _seed_episode(surreal_schema, "ep-2")

        await ops.save(surreal_schema, _make_edge("he-a", gid, "saga-1", "ep-1"))
        await ops.save(surreal_schema, _make_edge("he-b", gid, "saga-1", "ep-2"))

        await ops.delete_by_uuids(surreal_schema, ["he-a"])
        remaining = await ops.get_by_uuids(surreal_schema, ["he-a", "he-b"])
        assert {r.uuid for r in remaining} == {"he-b"}

    async def test_get_by_group_ids_respects_limit_and_cursor(
        self, surreal_schema: SurrealDriver
    ) -> None:
        ops = SurrealHasEpisodeEdgeOperations()
        gid = surreal_schema.group_id
        await _seed_saga(surreal_schema, "saga-1")
        for i in range(6):
            await _seed_episode(surreal_schema, f"ep-{i:02d}")
            await ops.save(
                surreal_schema,
                _make_edge(f"he-{i:02d}", gid, "saga-1", f"ep-{i:02d}"),
            )

        first_page = await ops.get_by_group_ids(surreal_schema, [gid], limit=3)
        assert len(first_page) == 3
        assert first_page[0].uuid == "he-05"
        cursor = first_page[-1].uuid
        second_page = await ops.get_by_group_ids(surreal_schema, [gid], limit=3, uuid_cursor=cursor)
        assert [e.uuid for e in second_page] == ["he-02", "he-01", "he-00"]
