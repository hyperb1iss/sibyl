"""Tests for SurrealEntityNodeOperations (Wave 1.2 Task 1.2.1)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from graphiti_core.errors import NodeNotFoundError
from graphiti_core.nodes import EntityNode

from sibyl_core.backends.surreal import SurrealDriver
from sibyl_core.backends.surreal.schema import EMBEDDING_DIM
from sibyl_core.graph.surreal.ops.entity_node_ops import SurrealEntityNodeOperations


def _make_entity(
    uuid: str,
    group_id: str,
    *,
    name: str = "Alice",
    summary: str | None = "she knows things",
    labels: list[str] | None = None,
    attributes: dict[str, object] | None = None,
    embedding: list[float] | None = None,
) -> EntityNode:
    return EntityNode(
        uuid=uuid,
        name=name,
        group_id=group_id,
        summary=summary,
        labels=labels or ["Person"],
        attributes=attributes or {},
        name_embedding=embedding,
        created_at=datetime.now(UTC).replace(tzinfo=None),
    )


@pytest.mark.asyncio
class TestEntityNodeOps:
    async def test_save_and_get_by_uuid(self, surreal_schema: SurrealDriver) -> None:
        ops = SurrealEntityNodeOperations()
        ent = _make_entity("ent-1", surreal_schema.group_id, attributes={"role": "dev"})

        await ops.save(surreal_schema, ent)
        fetched = await ops.get_by_uuid(surreal_schema, "ent-1")

        assert fetched.uuid == "ent-1"
        assert fetched.name == "Alice"
        assert fetched.summary == "she knows things"
        assert "Person" in fetched.labels
        assert fetched.attributes == {"role": "dev"}

    async def test_get_by_uuid_missing_raises(self, surreal_schema: SurrealDriver) -> None:
        ops = SurrealEntityNodeOperations()
        with pytest.raises(NodeNotFoundError):
            await ops.get_by_uuid(surreal_schema, "nope")

    async def test_get_by_uuids_returns_ordered(self, surreal_schema: SurrealDriver) -> None:
        ops = SurrealEntityNodeOperations()
        await ops.save(
            surreal_schema,
            _make_entity("ent-a", surreal_schema.group_id, name="A"),
        )
        await ops.save(
            surreal_schema,
            _make_entity("ent-b", surreal_schema.group_id, name="B"),
        )
        results = await ops.get_by_uuids(surreal_schema, ["ent-a", "ent-b", "ent-x"])
        names = {r.uuid: r.name for r in results}
        assert names == {"ent-a": "A", "ent-b": "B"}

    async def test_save_bulk_and_group_query(self, surreal_schema: SurrealDriver) -> None:
        ops = SurrealEntityNodeOperations()
        gid = surreal_schema.group_id
        nodes = [_make_entity(f"ent-{i}", gid, name=f"N{i}") for i in range(5)]
        await ops.save_bulk(surreal_schema, nodes, batch_size=2)

        listed = await ops.get_by_group_ids(surreal_schema, [gid])
        assert len(listed) == 5
        assert {n.uuid for n in listed} == {f"ent-{i}" for i in range(5)}

    async def test_save_overwrites_on_same_uuid(self, surreal_schema: SurrealDriver) -> None:
        ops = SurrealEntityNodeOperations()
        gid = surreal_schema.group_id
        await ops.save(
            surreal_schema,
            _make_entity("ent-1", gid, name="Original", summary="orig"),
        )
        await ops.save(
            surreal_schema,
            _make_entity("ent-1", gid, name="Updated", summary="updated"),
        )
        fetched = await ops.get_by_uuid(surreal_schema, "ent-1")
        assert fetched.name == "Updated"
        assert fetched.summary == "updated"

    async def test_delete_by_uuids(self, surreal_schema: SurrealDriver) -> None:
        ops = SurrealEntityNodeOperations()
        gid = surreal_schema.group_id
        await ops.save(surreal_schema, _make_entity("ent-a", gid))
        await ops.save(surreal_schema, _make_entity("ent-b", gid))
        await ops.delete_by_uuids(surreal_schema, ["ent-a"])

        remaining = await ops.get_by_uuids(surreal_schema, ["ent-a", "ent-b"])
        assert {n.uuid for n in remaining} == {"ent-b"}

    async def test_delete_by_group_id(self, surreal_schema: SurrealDriver) -> None:
        ops = SurrealEntityNodeOperations()
        gid = surreal_schema.group_id
        await ops.save(surreal_schema, _make_entity("ent-a", gid))
        await ops.save(surreal_schema, _make_entity("ent-b", gid))
        await ops.delete_by_group_id(surreal_schema, gid)
        assert await ops.get_by_group_ids(surreal_schema, [gid]) == []

    async def test_load_embeddings_single_and_bulk(self, surreal_schema: SurrealDriver) -> None:
        ops = SurrealEntityNodeOperations()
        gid = surreal_schema.group_id
        embedding = [0.1] * EMBEDDING_DIM
        await ops.save(
            surreal_schema,
            _make_entity("ent-1", gid, embedding=embedding),
        )
        # Single: mutate node whose embedding we dropped
        fresh = _make_entity("ent-1", gid)
        assert fresh.name_embedding is None
        await ops.load_embeddings(surreal_schema, fresh)
        assert fresh.name_embedding is not None
        assert len(fresh.name_embedding) == EMBEDDING_DIM
        # Bulk
        a = _make_entity("ent-1", gid)
        b = _make_entity("ent-missing", gid)
        await ops.load_embeddings_bulk(surreal_schema, [a, b])
        assert a.name_embedding is not None
        assert b.name_embedding is None

    async def test_get_by_group_ids_respects_limit_and_cursor(
        self, surreal_schema: SurrealDriver
    ) -> None:
        ops = SurrealEntityNodeOperations()
        gid = surreal_schema.group_id
        for i in range(6):
            await ops.save(
                surreal_schema,
                _make_entity(f"ent-{i:02d}", gid, name=f"n{i}"),
            )
        first_page = await ops.get_by_group_ids(surreal_schema, [gid], limit=3)
        assert len(first_page) == 3
        # Ordered uuid DESC, so first page starts at ent-05
        assert first_page[0].uuid == "ent-05"
        cursor = first_page[-1].uuid
        second_page = await ops.get_by_group_ids(surreal_schema, [gid], limit=3, uuid_cursor=cursor)
        assert [n.uuid for n in second_page] == ["ent-02", "ent-01", "ent-00"]
