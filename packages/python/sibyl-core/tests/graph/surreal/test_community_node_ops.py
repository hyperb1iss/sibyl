"""Tests for SurrealCommunityNodeOperations (Wave 1.2)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from graphiti_core.errors import NodeNotFoundError
from graphiti_core.nodes import CommunityNode

from sibyl_core.backends.surreal import SurrealDriver
from sibyl_core.backends.surreal.schema import EMBEDDING_DIM
from sibyl_core.graph.surreal.ops.community_node_ops import SurrealCommunityNodeOperations


def _make_community(
    uuid: str,
    group_id: str,
    *,
    name: str = "Cluster A",
    summary: str = "a tight-knit cluster",
    embedding: list[float] | None = None,
) -> CommunityNode:
    return CommunityNode(
        uuid=uuid,
        name=name,
        group_id=group_id,
        summary=summary,
        name_embedding=embedding,
        created_at=datetime.now(UTC).replace(tzinfo=None),
    )


class _RecordingExecutor:
    def __init__(self) -> None:
        self.queries: list[tuple[str, dict[str, Any]]] = []

    async def execute_query(self, query: str, **params: Any) -> list[Any]:
        self.queries.append((query, params))
        return []


@pytest.mark.asyncio
class TestCommunityNodeOps:
    async def test_save_uses_single_upsert_statement(self) -> None:
        ops = SurrealCommunityNodeOperations()
        executor = _RecordingExecutor()

        await ops.save(executor, _make_community("com-1", "group-1"))

        assert len(executor.queries) == 1
        query, params = executor.queries[0]
        assert "UPSERT community SET" in query
        assert "DELETE FROM community" not in query
        assert "CREATE community" not in query
        assert params["uuid"] == "com-1"

    async def test_save_bulk_uses_duplicate_key_upsert_statement(self) -> None:
        ops = SurrealCommunityNodeOperations()
        executor = _RecordingExecutor()

        await ops.save_bulk(
            executor,
            [_make_community("com-a", "group-1"), _make_community("com-b", "group-1")],
            batch_size=10,
        )

        assert len(executor.queries) == 1
        query, params = executor.queries[0]
        assert "INSERT INTO community $rows ON DUPLICATE KEY UPDATE" in query
        assert "DELETE FROM community" not in query
        assert [row["uuid"] for row in params["rows"]] == ["com-a", "com-b"]

    async def test_save_and_get_by_uuid(self, surreal_schema: SurrealDriver) -> None:
        ops = SurrealCommunityNodeOperations()
        com = _make_community("com-1", surreal_schema.group_id)

        await ops.save(surreal_schema, com)
        fetched = await ops.get_by_uuid(surreal_schema, "com-1")

        assert fetched.uuid == "com-1"
        assert fetched.name == "Cluster A"
        assert fetched.summary == "a tight-knit cluster"

    async def test_get_by_uuid_missing_raises(self, surreal_schema: SurrealDriver) -> None:
        ops = SurrealCommunityNodeOperations()
        with pytest.raises(NodeNotFoundError):
            await ops.get_by_uuid(surreal_schema, "nope")

    async def test_save_bulk_and_group_query(self, surreal_schema: SurrealDriver) -> None:
        ops = SurrealCommunityNodeOperations()
        gid = surreal_schema.group_id
        nodes = [_make_community(f"com-{i}", gid, name=f"C{i}") for i in range(5)]
        await ops.save_bulk(surreal_schema, nodes, batch_size=2)

        listed = await ops.get_by_group_ids(surreal_schema, [gid])
        assert len(listed) == 5
        assert {n.uuid for n in listed} == {f"com-{i}" for i in range(5)}

    async def test_save_overwrites_on_same_uuid(self, surreal_schema: SurrealDriver) -> None:
        ops = SurrealCommunityNodeOperations()
        gid = surreal_schema.group_id
        await ops.save(
            surreal_schema,
            _make_community("com-1", gid, name="Original", summary="orig"),
        )
        await ops.save(
            surreal_schema,
            _make_community("com-1", gid, name="Updated", summary="updated"),
        )
        fetched = await ops.get_by_uuid(surreal_schema, "com-1")
        assert fetched.name == "Updated"
        assert fetched.summary == "updated"

    async def test_delete_by_uuids(self, surreal_schema: SurrealDriver) -> None:
        ops = SurrealCommunityNodeOperations()
        gid = surreal_schema.group_id
        await ops.save(surreal_schema, _make_community("com-a", gid))
        await ops.save(surreal_schema, _make_community("com-b", gid))
        await ops.delete_by_uuids(surreal_schema, ["com-a"])

        remaining = await ops.get_by_uuids(surreal_schema, ["com-a", "com-b"])
        assert {n.uuid for n in remaining} == {"com-b"}

    async def test_delete_by_group_id(self, surreal_schema: SurrealDriver) -> None:
        ops = SurrealCommunityNodeOperations()
        gid = surreal_schema.group_id
        await ops.save(surreal_schema, _make_community("com-a", gid))
        await ops.save(surreal_schema, _make_community("com-b", gid))
        await ops.delete_by_group_id(surreal_schema, gid)
        assert await ops.get_by_group_ids(surreal_schema, [gid]) == []

    async def test_load_name_embedding_single_and_bulk(self, surreal_schema: SurrealDriver) -> None:
        ops = SurrealCommunityNodeOperations()
        gid = surreal_schema.group_id
        embedding = [0.2] * EMBEDDING_DIM
        await ops.save(
            surreal_schema,
            _make_community("com-1", gid, embedding=embedding),
        )
        # Graphiti's CommunityNodeOperations exposes only load_name_embedding;
        # exercise it on one node then a "bulk" sequence of independent calls.
        fresh = _make_community("com-1", gid)
        assert fresh.name_embedding is None
        await ops.load_name_embedding(surreal_schema, fresh)
        assert fresh.name_embedding is not None
        assert len(fresh.name_embedding) == EMBEDDING_DIM

        # Multi-node: loop manually — mirrors how callers would bulk-hydrate.
        await ops.save(surreal_schema, _make_community("com-2", gid, embedding=embedding))
        a = _make_community("com-1", gid)
        b = _make_community("com-2", gid)
        for node in (a, b):
            await ops.load_name_embedding(surreal_schema, node)
        assert a.name_embedding is not None
        assert b.name_embedding is not None

    async def test_load_name_embedding_missing_raises(self, surreal_schema: SurrealDriver) -> None:
        ops = SurrealCommunityNodeOperations()
        ghost = _make_community("ghost", surreal_schema.group_id)
        with pytest.raises(NodeNotFoundError):
            await ops.load_name_embedding(surreal_schema, ghost)

    async def test_get_by_group_ids_respects_limit_and_cursor(
        self, surreal_schema: SurrealDriver
    ) -> None:
        ops = SurrealCommunityNodeOperations()
        gid = surreal_schema.group_id
        for i in range(6):
            await ops.save(
                surreal_schema,
                _make_community(f"com-{i:02d}", gid, name=f"n{i}"),
            )
        first_page = await ops.get_by_group_ids(surreal_schema, [gid], limit=3)
        assert len(first_page) == 3
        assert first_page[0].uuid == "com-05"
        cursor = first_page[-1].uuid
        second_page = await ops.get_by_group_ids(surreal_schema, [gid], limit=3, uuid_cursor=cursor)
        assert [n.uuid for n in second_page] == ["com-02", "com-01", "com-00"]
