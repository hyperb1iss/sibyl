"""Tests for SurrealEpisodeNodeOperations (Wave 1.2)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from graphiti_core.errors import NodeNotFoundError
from graphiti_core.nodes import EpisodeType, EpisodicNode

from sibyl_core.backends.surreal import SurrealDriver
from sibyl_core.graph.surreal.ops.episode_node_ops import SurrealEpisodeNodeOperations


def _make_episode(
    uuid: str,
    group_id: str,
    *,
    name: str = "conversation",
    source: EpisodeType = EpisodeType.message,
    source_description: str = "chat log",
    content: str = "user: hi\nassistant: hello",
    entity_edges: list[str] | None = None,
    valid_at: datetime | None = None,
) -> EpisodicNode:
    now = datetime.now(UTC).replace(tzinfo=None)
    return EpisodicNode(
        uuid=uuid,
        name=name,
        group_id=group_id,
        source=source,
        source_description=source_description,
        content=content,
        entity_edges=entity_edges or [],
        created_at=now,
        valid_at=valid_at or now,
    )


class _RecordingExecutor:
    def __init__(self) -> None:
        self.queries: list[tuple[str, dict[str, Any]]] = []

    async def execute_query(self, query: str, **params: Any) -> list[Any]:
        self.queries.append((query, params))
        return []


@pytest.mark.asyncio
class TestEpisodeNodeOps:
    async def test_save_uses_single_upsert_statement(self) -> None:
        ops = SurrealEpisodeNodeOperations()
        executor = _RecordingExecutor()

        await ops.save(executor, _make_episode("ep-1", "group-1"))

        assert len(executor.queries) == 1
        query, params = executor.queries[0]
        assert "UPSERT episode SET" in query
        assert "DELETE FROM episode" not in query
        assert params["uuid"] == "ep-1"

    async def test_save_and_get_by_uuid(self, surreal_schema: SurrealDriver) -> None:
        ops = SurrealEpisodeNodeOperations()
        ep = _make_episode(
            "ep-1",
            surreal_schema.group_id,
            entity_edges=["edge-a", "edge-b"],
        )

        await ops.save(surreal_schema, ep)
        fetched = await ops.get_by_uuid(surreal_schema, "ep-1")

        assert fetched.uuid == "ep-1"
        assert fetched.name == "conversation"
        assert fetched.source == EpisodeType.message
        assert fetched.source_description == "chat log"
        assert fetched.content == "user: hi\nassistant: hello"
        assert fetched.entity_edges == ["edge-a", "edge-b"]

    async def test_get_by_uuid_missing_raises(self, surreal_schema: SurrealDriver) -> None:
        ops = SurrealEpisodeNodeOperations()
        with pytest.raises(NodeNotFoundError):
            await ops.get_by_uuid(surreal_schema, "nope")

    async def test_save_bulk_and_group_query(self, surreal_schema: SurrealDriver) -> None:
        ops = SurrealEpisodeNodeOperations()
        gid = surreal_schema.group_id
        nodes = [_make_episode(f"ep-{i}", gid, name=f"E{i}") for i in range(5)]
        await ops.save_bulk(surreal_schema, nodes, batch_size=2)

        listed = await ops.get_by_group_ids(surreal_schema, [gid])
        assert len(listed) == 5
        assert {n.uuid for n in listed} == {f"ep-{i}" for i in range(5)}

    async def test_save_overwrites_on_same_uuid(self, surreal_schema: SurrealDriver) -> None:
        ops = SurrealEpisodeNodeOperations()
        gid = surreal_schema.group_id
        await ops.save(
            surreal_schema,
            _make_episode("ep-1", gid, name="Original", content="orig"),
        )
        await ops.save(
            surreal_schema,
            _make_episode("ep-1", gid, name="Updated", content="updated"),
        )
        fetched = await ops.get_by_uuid(surreal_schema, "ep-1")
        assert fetched.name == "Updated"
        assert fetched.content == "updated"

    async def test_delete_by_uuids(self, surreal_schema: SurrealDriver) -> None:
        ops = SurrealEpisodeNodeOperations()
        gid = surreal_schema.group_id
        await ops.save(surreal_schema, _make_episode("ep-a", gid))
        await ops.save(surreal_schema, _make_episode("ep-b", gid))
        await ops.delete_by_uuids(surreal_schema, ["ep-a"])

        remaining = await ops.get_by_uuids(surreal_schema, ["ep-a", "ep-b"])
        assert {n.uuid for n in remaining} == {"ep-b"}

    async def test_delete_by_group_id(self, surreal_schema: SurrealDriver) -> None:
        ops = SurrealEpisodeNodeOperations()
        gid = surreal_schema.group_id
        await ops.save(surreal_schema, _make_episode("ep-a", gid))
        await ops.save(surreal_schema, _make_episode("ep-b", gid))
        await ops.delete_by_group_id(surreal_schema, gid)
        assert await ops.get_by_group_ids(surreal_schema, [gid]) == []

    async def test_get_by_group_ids_respects_limit_and_cursor(
        self, surreal_schema: SurrealDriver
    ) -> None:
        ops = SurrealEpisodeNodeOperations()
        gid = surreal_schema.group_id
        for i in range(6):
            await ops.save(
                surreal_schema,
                _make_episode(f"ep-{i:02d}", gid, name=f"n{i}"),
            )
        first_page = await ops.get_by_group_ids(surreal_schema, [gid], limit=3)
        assert len(first_page) == 3
        # Ordered uuid DESC, so first page starts at ep-05
        assert first_page[0].uuid == "ep-05"
        cursor = first_page[-1].uuid
        second_page = await ops.get_by_group_ids(surreal_schema, [gid], limit=3, uuid_cursor=cursor)
        assert [n.uuid for n in second_page] == ["ep-02", "ep-01", "ep-00"]
