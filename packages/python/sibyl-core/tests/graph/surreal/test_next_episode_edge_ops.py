"""Tests for SurrealNextEpisodeEdgeOperations (Wave 1.2 edge ops)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from sibyl_core.backends.surreal import SurrealDriver
from sibyl_core.graph.surreal.compat.models import EdgeNotFoundError, NextEpisodeEdge
from sibyl_core.graph.surreal.compat.ops.next_episode_edge_ops import (
    SurrealNextEpisodeEdgeOperations,
)


async def _seed_episode(driver: SurrealDriver, uuid: str, name: str = "ep") -> None:
    await driver.execute_query(
        "CREATE episode SET uuid = $uuid, name = $name, source = 'text', "
        "content = 'x', group_id = $gid;",
        uuid=uuid,
        name=name,
        gid=driver.group_id,
    )


def _make_edge(uuid: str, group_id: str, src: str, tgt: str) -> NextEpisodeEdge:
    return NextEpisodeEdge(
        uuid=uuid,
        group_id=group_id,
        source_node_uuid=src,
        target_node_uuid=tgt,
        created_at=datetime.now(UTC).replace(tzinfo=None),
    )


@pytest.mark.asyncio
class TestNextEpisodeEdgeOps:
    async def test_save_and_get_by_uuid(self, surreal_schema: SurrealDriver) -> None:
        ops = SurrealNextEpisodeEdgeOperations()
        gid = surreal_schema.group_id
        await _seed_episode(surreal_schema, "ep-1")
        await _seed_episode(surreal_schema, "ep-2")

        await ops.save(surreal_schema, _make_edge("ne-1", gid, "ep-1", "ep-2"))
        fetched = await ops.get_by_uuid(surreal_schema, "ne-1")

        assert fetched.uuid == "ne-1"
        assert fetched.group_id == gid
        assert fetched.source_node_uuid == "ep-1"
        assert fetched.target_node_uuid == "ep-2"

    async def test_get_by_uuid_missing_raises(self, surreal_schema: SurrealDriver) -> None:
        ops = SurrealNextEpisodeEdgeOperations()
        with pytest.raises(EdgeNotFoundError):
            await ops.get_by_uuid(surreal_schema, "nope")

    async def test_save_missing_endpoint_raises(self, surreal_schema: SurrealDriver) -> None:
        ops = SurrealNextEpisodeEdgeOperations()
        gid = surreal_schema.group_id
        with pytest.raises(ValueError, match="not found"):
            await ops.save(surreal_schema, _make_edge("ne-x", gid, "missing-a", "missing-b"))

    async def test_save_bulk_chains_episodes(self, surreal_schema: SurrealDriver) -> None:
        ops = SurrealNextEpisodeEdgeOperations()
        gid = surreal_schema.group_id
        for i in range(5):
            await _seed_episode(surreal_schema, f"ep-{i}")
        edges = [_make_edge(f"ne-{i}", gid, f"ep-{i}", f"ep-{i + 1}") for i in range(4)]

        await ops.save_bulk(surreal_schema, edges)
        listed = await ops.get_by_group_ids(surreal_schema, [gid])
        assert {e.uuid for e in listed} == {f"ne-{i}" for i in range(4)}

    async def test_save_overwrites_on_same_uuid(self, surreal_schema: SurrealDriver) -> None:
        ops = SurrealNextEpisodeEdgeOperations()
        gid = surreal_schema.group_id
        await _seed_episode(surreal_schema, "ep-1")
        await _seed_episode(surreal_schema, "ep-2")
        await _seed_episode(surreal_schema, "ep-3")

        await ops.save(surreal_schema, _make_edge("ne-1", gid, "ep-1", "ep-2"))
        await ops.save(surreal_schema, _make_edge("ne-1", gid, "ep-1", "ep-3"))

        fetched = await ops.get_by_uuid(surreal_schema, "ne-1")
        assert fetched.target_node_uuid == "ep-3"
        all_rows = await ops.get_by_uuids(surreal_schema, ["ne-1"])
        assert len(all_rows) == 1

    async def test_delete_single(self, surreal_schema: SurrealDriver) -> None:
        ops = SurrealNextEpisodeEdgeOperations()
        gid = surreal_schema.group_id
        await _seed_episode(surreal_schema, "ep-1")
        await _seed_episode(surreal_schema, "ep-2")

        edge = _make_edge("ne-1", gid, "ep-1", "ep-2")
        await ops.save(surreal_schema, edge)
        await ops.delete(surreal_schema, edge)

        with pytest.raises(EdgeNotFoundError):
            await ops.get_by_uuid(surreal_schema, "ne-1")

    async def test_delete_by_uuids(self, surreal_schema: SurrealDriver) -> None:
        ops = SurrealNextEpisodeEdgeOperations()
        gid = surreal_schema.group_id
        for i in range(3):
            await _seed_episode(surreal_schema, f"ep-{i}")

        await ops.save(surreal_schema, _make_edge("ne-a", gid, "ep-0", "ep-1"))
        await ops.save(surreal_schema, _make_edge("ne-b", gid, "ep-1", "ep-2"))

        await ops.delete_by_uuids(surreal_schema, ["ne-a"])
        remaining = await ops.get_by_uuids(surreal_schema, ["ne-a", "ne-b"])
        assert {r.uuid for r in remaining} == {"ne-b"}

    async def test_get_by_group_ids_respects_limit_and_cursor(
        self, surreal_schema: SurrealDriver
    ) -> None:
        ops = SurrealNextEpisodeEdgeOperations()
        gid = surreal_schema.group_id
        for i in range(7):
            await _seed_episode(surreal_schema, f"ep-{i:02d}")
        for i in range(6):
            await ops.save(
                surreal_schema,
                _make_edge(f"ne-{i:02d}", gid, f"ep-{i:02d}", f"ep-{i + 1:02d}"),
            )

        first_page = await ops.get_by_group_ids(surreal_schema, [gid], limit=3)
        assert len(first_page) == 3
        assert first_page[0].uuid == "ne-05"
        cursor = first_page[-1].uuid
        second_page = await ops.get_by_group_ids(surreal_schema, [gid], limit=3, uuid_cursor=cursor)
        assert [e.uuid for e in second_page] == ["ne-02", "ne-01", "ne-00"]
