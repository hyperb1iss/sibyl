"""Tests for SurrealSagaNodeOperations (Wave 1.2)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from graphiti_core.errors import NodeNotFoundError
from graphiti_core.nodes import SagaNode

from sibyl_core.backends.surreal import SurrealDriver
from sibyl_core.graph.surreal.ops.saga_node_ops import SurrealSagaNodeOperations


def _make_saga(
    uuid: str,
    group_id: str,
    *,
    name: str = "Main Arc",
) -> SagaNode:
    return SagaNode(
        uuid=uuid,
        name=name,
        group_id=group_id,
        created_at=datetime.now(UTC).replace(tzinfo=None),
    )


@pytest.mark.asyncio
class TestSagaNodeOps:
    async def test_save_and_get_by_uuid(self, surreal_schema: SurrealDriver) -> None:
        ops = SurrealSagaNodeOperations()
        saga = _make_saga("saga-1", surreal_schema.group_id)

        await ops.save(surreal_schema, saga)
        fetched = await ops.get_by_uuid(surreal_schema, "saga-1")

        assert fetched.uuid == "saga-1"
        assert fetched.name == "Main Arc"
        assert fetched.group_id == surreal_schema.group_id

    async def test_get_by_uuid_missing_raises(self, surreal_schema: SurrealDriver) -> None:
        ops = SurrealSagaNodeOperations()
        with pytest.raises(NodeNotFoundError):
            await ops.get_by_uuid(surreal_schema, "nope")

    async def test_save_bulk_and_group_query(self, surreal_schema: SurrealDriver) -> None:
        ops = SurrealSagaNodeOperations()
        gid = surreal_schema.group_id
        nodes = [_make_saga(f"saga-{i}", gid, name=f"S{i}") for i in range(5)]
        await ops.save_bulk(surreal_schema, nodes, batch_size=2)

        listed = await ops.get_by_group_ids(surreal_schema, [gid])
        assert len(listed) == 5
        assert {n.uuid for n in listed} == {f"saga-{i}" for i in range(5)}

    async def test_save_overwrites_on_same_uuid(self, surreal_schema: SurrealDriver) -> None:
        ops = SurrealSagaNodeOperations()
        gid = surreal_schema.group_id
        await ops.save(surreal_schema, _make_saga("saga-1", gid, name="Original"))
        await ops.save(surreal_schema, _make_saga("saga-1", gid, name="Updated"))
        fetched = await ops.get_by_uuid(surreal_schema, "saga-1")
        assert fetched.name == "Updated"

    async def test_delete_by_uuids(self, surreal_schema: SurrealDriver) -> None:
        ops = SurrealSagaNodeOperations()
        gid = surreal_schema.group_id
        await ops.save(surreal_schema, _make_saga("saga-a", gid))
        await ops.save(surreal_schema, _make_saga("saga-b", gid))
        await ops.delete_by_uuids(surreal_schema, ["saga-a"])

        remaining = await ops.get_by_uuids(surreal_schema, ["saga-a", "saga-b"])
        assert {n.uuid for n in remaining} == {"saga-b"}

    async def test_delete_by_group_id(self, surreal_schema: SurrealDriver) -> None:
        ops = SurrealSagaNodeOperations()
        gid = surreal_schema.group_id
        await ops.save(surreal_schema, _make_saga("saga-a", gid))
        await ops.save(surreal_schema, _make_saga("saga-b", gid))
        await ops.delete_by_group_id(surreal_schema, gid)
        assert await ops.get_by_group_ids(surreal_schema, [gid]) == []

    async def test_get_by_group_ids_respects_limit_and_cursor(
        self, surreal_schema: SurrealDriver
    ) -> None:
        ops = SurrealSagaNodeOperations()
        gid = surreal_schema.group_id
        for i in range(6):
            await ops.save(
                surreal_schema,
                _make_saga(f"saga-{i:02d}", gid, name=f"n{i}"),
            )
        first_page = await ops.get_by_group_ids(surreal_schema, [gid], limit=3)
        assert len(first_page) == 3
        assert first_page[0].uuid == "saga-05"
        cursor = first_page[-1].uuid
        second_page = await ops.get_by_group_ids(surreal_schema, [gid], limit=3, uuid_cursor=cursor)
        assert [n.uuid for n in second_page] == ["saga-02", "saga-01", "saga-00"]
