"""Tests for SurrealCommunityEdgeOperations (Wave 1.2 Task 1.2.3)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from sibyl_core.backends.surreal import SurrealDriver
from sibyl_core.graph.surreal.compat.models import (
    CommunityEdge,
    CommunityNode,
    EdgeNotFoundError,
    EntityNode,
)
from sibyl_core.graph.surreal.compat.ops.community_edge_ops import SurrealCommunityEdgeOperations
from sibyl_core.graph.surreal.compat.ops.entity_node_ops import SurrealEntityNodeOperations


def _naive_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _utc_now() -> datetime:
    # SurrealDB stores datetimes as UTC-aware; writing tz-aware values keeps
    # round-trip equality cheap (naive-in -> aware-out mismatches otherwise).
    return datetime.now(UTC)


def _make_entity(uuid: str, group_id: str, *, name: str = "Alice") -> EntityNode:
    return EntityNode(
        uuid=uuid,
        name=name,
        group_id=group_id,
        summary="",
        labels=["Person"],
        attributes={},
        name_embedding=None,
        created_at=_naive_now(),
    )


async def _seed_community(
    driver: SurrealDriver, uuid: str, *, name: str = "Cohort"
) -> CommunityNode:
    """Insert a community row directly; no community ops module exists yet."""
    await driver.execute_query(
        """
        CREATE community SET
            uuid = $uuid,
            name = $name,
            summary = '',
            labels = [],
            group_id = $gid,
            created_at = $created_at;
        """,
        uuid=uuid,
        name=name,
        gid=driver.group_id,
        created_at=_naive_now(),
    )
    return CommunityNode(
        uuid=uuid,
        name=name,
        group_id=driver.group_id,
        name_embedding=None,
        summary="",
        created_at=_naive_now(),
    )


def _make_edge(
    uuid: str,
    group_id: str,
    *,
    source_uuid: str,
    target_uuid: str,
    created_at: datetime | None = None,
) -> CommunityEdge:
    return CommunityEdge(
        uuid=uuid,
        source_node_uuid=source_uuid,
        target_node_uuid=target_uuid,
        group_id=group_id,
        created_at=created_at if created_at is not None else _naive_now(),
    )


@pytest.mark.asyncio
class TestCommunityEdgeOps:
    async def test_save_community_to_entity_round_trips(
        self, surreal_schema: SurrealDriver
    ) -> None:
        ops = SurrealCommunityEdgeOperations()
        gid = surreal_schema.group_id
        await _seed_community(surreal_schema, "com-1", name="Developers")
        node_ops = SurrealEntityNodeOperations()
        await node_ops.save(surreal_schema, _make_entity("ent-a", gid))

        created = _utc_now()
        edge = _make_edge(
            "edge-1",
            gid,
            source_uuid="com-1",
            target_uuid="ent-a",
            created_at=created,
        )
        await ops.save(surreal_schema, edge)

        fetched = await ops.get_by_uuid(surreal_schema, "edge-1")
        assert fetched.uuid == "edge-1"
        assert fetched.source_node_uuid == "com-1"
        assert fetched.target_node_uuid == "ent-a"
        assert fetched.group_id == gid
        assert fetched.created_at == created

    async def test_save_community_to_community_round_trips(
        self, surreal_schema: SurrealDriver
    ) -> None:
        """has_member allows community -> community nesting."""
        ops = SurrealCommunityEdgeOperations()
        gid = surreal_schema.group_id
        await _seed_community(surreal_schema, "com-parent", name="Engineering")
        await _seed_community(surreal_schema, "com-child", name="Backend")

        edge = _make_edge(
            "edge-nest",
            gid,
            source_uuid="com-parent",
            target_uuid="com-child",
        )
        await ops.save(surreal_schema, edge)

        fetched = await ops.get_by_uuid(surreal_schema, "edge-nest")
        assert fetched.source_node_uuid == "com-parent"
        assert fetched.target_node_uuid == "com-child"

    async def test_get_by_uuid_missing_raises(self, surreal_schema: SurrealDriver) -> None:
        ops = SurrealCommunityEdgeOperations()
        with pytest.raises(EdgeNotFoundError):
            await ops.get_by_uuid(surreal_schema, "nope")

    async def test_get_by_uuids_returns_subset(self, surreal_schema: SurrealDriver) -> None:
        ops = SurrealCommunityEdgeOperations()
        gid = surreal_schema.group_id
        await _seed_community(surreal_schema, "com-1")
        node_ops = SurrealEntityNodeOperations()
        await node_ops.save(surreal_schema, _make_entity("ent-a", gid))
        await node_ops.save(surreal_schema, _make_entity("ent-b", gid))

        await ops.save(
            surreal_schema,
            _make_edge("edge-a", gid, source_uuid="com-1", target_uuid="ent-a"),
        )
        await ops.save(
            surreal_schema,
            _make_edge("edge-b", gid, source_uuid="com-1", target_uuid="ent-b"),
        )

        results = await ops.get_by_uuids(surreal_schema, ["edge-a", "edge-b", "edge-missing"])
        assert {r.uuid for r in results} == {"edge-a", "edge-b"}

    async def test_save_overwrites_on_same_uuid(self, surreal_schema: SurrealDriver) -> None:
        ops = SurrealCommunityEdgeOperations()
        gid = surreal_schema.group_id
        await _seed_community(surreal_schema, "com-1")
        node_ops = SurrealEntityNodeOperations()
        await node_ops.save(surreal_schema, _make_entity("ent-a", gid))
        await node_ops.save(surreal_schema, _make_entity("ent-b", gid))

        await ops.save(
            surreal_schema,
            _make_edge("edge-1", gid, source_uuid="com-1", target_uuid="ent-a"),
        )
        await ops.save(
            surreal_schema,
            _make_edge("edge-1", gid, source_uuid="com-1", target_uuid="ent-b"),
        )
        fetched = await ops.get_by_uuid(surreal_schema, "edge-1")
        assert fetched.target_node_uuid == "ent-b"

    async def test_delete_and_delete_by_uuids(self, surreal_schema: SurrealDriver) -> None:
        ops = SurrealCommunityEdgeOperations()
        gid = surreal_schema.group_id
        await _seed_community(surreal_schema, "com-1")
        node_ops = SurrealEntityNodeOperations()
        for uuid in ("ent-a", "ent-b", "ent-c"):
            await node_ops.save(surreal_schema, _make_entity(uuid, gid))

        edge_a = _make_edge("edge-a", gid, source_uuid="com-1", target_uuid="ent-a")
        edge_b = _make_edge("edge-b", gid, source_uuid="com-1", target_uuid="ent-b")
        edge_c = _make_edge("edge-c", gid, source_uuid="com-1", target_uuid="ent-c")
        await ops.save(surreal_schema, edge_a)
        await ops.save(surreal_schema, edge_b)
        await ops.save(surreal_schema, edge_c)

        await ops.delete(surreal_schema, edge_a)
        await ops.delete_by_uuids(surreal_schema, ["edge-b"])

        remaining = await ops.get_by_uuids(surreal_schema, ["edge-a", "edge-b", "edge-c"])
        assert {e.uuid for e in remaining} == {"edge-c"}

    async def test_get_by_group_ids_respects_limit_and_cursor(
        self, surreal_schema: SurrealDriver
    ) -> None:
        ops = SurrealCommunityEdgeOperations()
        gid = surreal_schema.group_id
        await _seed_community(surreal_schema, "com-1")
        node_ops = SurrealEntityNodeOperations()
        for i in range(6):
            await node_ops.save(surreal_schema, _make_entity(f"ent-{i:02d}", gid))
            await ops.save(
                surreal_schema,
                _make_edge(
                    f"edge-{i:02d}",
                    gid,
                    source_uuid="com-1",
                    target_uuid=f"ent-{i:02d}",
                ),
            )

        first_page = await ops.get_by_group_ids(surreal_schema, [gid], limit=3)
        assert len(first_page) == 3
        assert first_page[0].uuid == "edge-05"
        cursor = first_page[-1].uuid
        second_page = await ops.get_by_group_ids(surreal_schema, [gid], limit=3, uuid_cursor=cursor)
        assert [e.uuid for e in second_page] == ["edge-02", "edge-01", "edge-00"]
