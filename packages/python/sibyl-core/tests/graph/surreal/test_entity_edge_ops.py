"""Tests for SurrealEntityEdgeOperations (Wave 1.2 Task 1.2.2)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from graphiti_core.edges import EntityEdge
from graphiti_core.errors import EdgeNotFoundError
from graphiti_core.nodes import EntityNode
from surrealdb import RecordID

from sibyl_core.backends.surreal import SurrealDriver
from sibyl_core.backends.surreal.schema import EMBEDDING_DIM
from sibyl_core.graph.surreal.ops.entity_edge_ops import SurrealEntityEdgeOperations
from sibyl_core.graph.surreal.ops.entity_node_ops import SurrealEntityNodeOperations


def _naive_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _utc_now() -> datetime:
    # SurrealDB stores datetimes as UTC-aware; writing tz-aware values keeps
    # round-trip equality cheap (naive-in -> aware-out mismatches otherwise).
    return datetime.now(UTC)


def _make_entity(
    uuid: str,
    group_id: str,
    *,
    name: str = "Alice",
    labels: list[str] | None = None,
) -> EntityNode:
    return EntityNode(
        uuid=uuid,
        name=name,
        group_id=group_id,
        summary="",
        labels=labels or ["Person"],
        attributes={},
        name_embedding=None,
        created_at=_naive_now(),
    )


def _make_edge(
    uuid: str,
    group_id: str,
    *,
    source_uuid: str = "ent-a",
    target_uuid: str = "ent-b",
    name: str = "KNOWS",
    fact: str = "Alice knows Bob",
    fact_embedding: list[float] | None = None,
    episodes: list[str] | None = None,
    attributes: dict[str, object] | None = None,
    created_at: datetime | None = None,
    expired_at: datetime | None = None,
    valid_at: datetime | None = None,
    invalid_at: datetime | None = None,
) -> EntityEdge:
    return EntityEdge(
        uuid=uuid,
        source_node_uuid=source_uuid,
        target_node_uuid=target_uuid,
        name=name,
        fact=fact,
        fact_embedding=fact_embedding,
        group_id=group_id,
        episodes=episodes or [],
        attributes=attributes or {},
        created_at=created_at if created_at is not None else _naive_now(),
        expired_at=expired_at,
        valid_at=valid_at,
        invalid_at=invalid_at,
    )


async def _seed_entities(driver: SurrealDriver, uuids: list[str]) -> None:
    node_ops = SurrealEntityNodeOperations()
    for uuid in uuids:
        await node_ops.save(driver, _make_entity(uuid, driver.group_id, name=uuid))


class _RecordingExecutor:
    def __init__(self, records_by_uuid: dict[str, RecordID] | None = None) -> None:
        self.queries: list[tuple[str, dict[str, Any]]] = []
        self.records_by_uuid = records_by_uuid or {}

    async def execute_query(self, query: str, **params: Any) -> list[Any]:
        self.queries.append((query, params))
        if "SELECT id FROM entity WHERE uuid = $uuid LIMIT 1" in query:
            record = self.records_by_uuid.get(params["uuid"])
            if record is not None:
                return [{"id": record}]
        return []


@pytest.mark.asyncio
class TestEntityEdgeOps:
    async def test_save_uses_single_update_or_relate_statement(self) -> None:
        ops = SurrealEntityEdgeOperations()
        executor = _RecordingExecutor(
            {
                "ent-a": RecordID("entity", "ent-a"),
                "ent-b": RecordID("entity", "ent-b"),
            }
        )

        await ops.save(executor, _make_edge("edge-1", "group-1"))

        assert len(executor.queries) == 3
        query, params = executor.queries[-1]
        assert "type::" not in query
        assert "DELETE FROM relates_to WHERE uuid = $uuid AND (in != $src OR out != $tgt)" in query
        assert "LET $updated = (UPDATE relates_to SET" in query
        assert "RELATE $src->$rel->$tgt SET" in query
        assert "DELETE FROM relates_to WHERE uuid = $uuid;" not in query
        assert isinstance(params["rel"], RecordID)
        assert params["rel"].table_name == "relates_to"
        assert params["rel"].id == "edge-1"
        assert params["uuid"] == "edge-1"
        assert isinstance(params["src"], RecordID)
        assert params["src"].table_name == "entity"
        assert params["src"].id == "ent-a"
        assert isinstance(params["tgt"], RecordID)
        assert params["tgt"].table_name == "entity"
        assert params["tgt"].id == "ent-b"

    async def test_save_full_temporal_payload_round_trips(
        self, surreal_schema: SurrealDriver
    ) -> None:
        """Critical: every bi-temporal field must survive the round trip.

        Asserts fact, fact_embedding, episodes, attributes, and all four
        datetime fields (created_at, expired_at, valid_at, invalid_at) come
        back from get_by_uuid exactly as they went in.
        """
        ops = SurrealEntityEdgeOperations()
        gid = surreal_schema.group_id
        await _seed_entities(surreal_schema, ["ent-a", "ent-b"])

        created = _utc_now()
        valid = created - timedelta(days=10)
        invalid = created + timedelta(days=5)
        expired = created + timedelta(days=30)

        edge = _make_edge(
            "edge-1",
            gid,
            source_uuid="ent-a",
            target_uuid="ent-b",
            name="KNOWS",
            fact="Alice has known Bob since 2015",
            fact_embedding=[0.25] * EMBEDDING_DIM,
            episodes=["ep-1", "ep-2"],
            attributes={"confidence": 0.9, "source": "interview"},
            created_at=created,
            expired_at=expired,
            valid_at=valid,
            invalid_at=invalid,
        )

        await ops.save(surreal_schema, edge)
        fetched = await ops.get_by_uuid(surreal_schema, "edge-1")

        assert fetched.uuid == "edge-1"
        assert fetched.source_node_uuid == "ent-a"
        assert fetched.target_node_uuid == "ent-b"
        assert fetched.name == "KNOWS"
        assert fetched.fact == "Alice has known Bob since 2015"
        assert fetched.group_id == gid
        assert fetched.episodes == ["ep-1", "ep-2"]
        assert fetched.attributes == {"confidence": 0.9, "source": "interview"}
        # Temporal fields: load_embeddings isn't called by get_by_uuid in the
        # FalkorDB impl, so fact_embedding may be absent here — verify via
        # load_embeddings explicitly below.
        assert fetched.created_at == created
        assert fetched.expired_at == expired
        assert fetched.valid_at == valid
        assert fetched.invalid_at == invalid

        # fact_embedding is returned by the standard SELECT * shape — verify it.
        assert fetched.fact_embedding is not None
        assert len(fetched.fact_embedding) == EMBEDDING_DIM
        assert fetched.fact_embedding[0] == pytest.approx(0.25)

    async def test_get_by_uuid_missing_raises(self, surreal_schema: SurrealDriver) -> None:
        ops = SurrealEntityEdgeOperations()
        with pytest.raises(EdgeNotFoundError):
            await ops.get_by_uuid(surreal_schema, "nope")

    async def test_save_missing_endpoint_raises(self, surreal_schema: SurrealDriver) -> None:
        ops = SurrealEntityEdgeOperations()
        gid = surreal_schema.group_id
        with pytest.raises(ValueError, match="not found"):
            await ops.save(
                surreal_schema,
                _make_edge(
                    "edge-missing",
                    gid,
                    source_uuid="missing-source",
                    target_uuid="missing-target",
                ),
            )

    async def test_get_by_uuids_returns_subset(self, surreal_schema: SurrealDriver) -> None:
        ops = SurrealEntityEdgeOperations()
        gid = surreal_schema.group_id
        await _seed_entities(surreal_schema, ["ent-a", "ent-b"])

        await ops.save(surreal_schema, _make_edge("edge-a", gid))
        await ops.save(surreal_schema, _make_edge("edge-b", gid, name="LIKES"))

        results = await ops.get_by_uuids(surreal_schema, ["edge-a", "edge-b", "edge-missing"])
        assert {r.uuid for r in results} == {"edge-a", "edge-b"}

    async def test_save_overwrites_on_same_uuid(self, surreal_schema: SurrealDriver) -> None:
        ops = SurrealEntityEdgeOperations()
        gid = surreal_schema.group_id
        await _seed_entities(surreal_schema, ["ent-a", "ent-b"])

        await ops.save(
            surreal_schema,
            _make_edge("edge-1", gid, name="ORIGINAL", fact="orig fact"),
        )
        await ops.save(
            surreal_schema,
            _make_edge("edge-1", gid, name="UPDATED", fact="new fact"),
        )
        fetched = await ops.get_by_uuid(surreal_schema, "edge-1")
        assert fetched.name == "UPDATED"
        assert fetched.fact == "new fact"

    async def test_save_updates_legacy_random_id_row_without_duplicate(
        self, surreal_schema: SurrealDriver
    ) -> None:
        ops = SurrealEntityEdgeOperations()
        gid = surreal_schema.group_id
        await _seed_entities(surreal_schema, ["ent-a", "ent-b", "ent-c"])
        await surreal_schema.execute_query(
            """
            LET $src = (SELECT VALUE id FROM entity WHERE uuid = 'ent-a' LIMIT 1)[0];
            LET $tgt = (SELECT VALUE id FROM entity WHERE uuid = 'ent-b' LIMIT 1)[0];
            RELATE $src->relates_to->$tgt SET
                uuid = 'edge-legacy',
                name = 'ORIGINAL',
                fact = 'old fact',
                group_id = $gid,
                episodes = [],
                attributes = {},
                created_at = time::now();
            """,
            gid=gid,
        )

        await ops.save(
            surreal_schema,
            _make_edge(
                "edge-legacy",
                gid,
                source_uuid="ent-a",
                target_uuid="ent-c",
                name="UPDATED",
                fact="new fact",
            ),
        )

        rows = await surreal_schema.execute_query(
            "SELECT uuid, name, fact, in.uuid AS src, out.uuid AS tgt "
            "FROM relates_to WHERE uuid = 'edge-legacy';"
        )
        assert len(rows) == 1
        assert rows[0]["name"] == "UPDATED"
        assert rows[0]["fact"] == "new fact"
        assert rows[0]["src"] == "ent-a"
        assert rows[0]["tgt"] == "ent-c"

    async def test_save_bulk_and_group_query(self, surreal_schema: SurrealDriver) -> None:
        ops = SurrealEntityEdgeOperations()
        gid = surreal_schema.group_id
        await _seed_entities(surreal_schema, ["ent-a", "ent-b"])

        edges = [_make_edge(f"edge-{i:02d}", gid, name=f"R{i}") for i in range(5)]
        await ops.save_bulk(surreal_schema, edges, batch_size=2)

        listed = await ops.get_by_group_ids(surreal_schema, [gid])
        assert len(listed) == 5
        assert {e.uuid for e in listed} == {f"edge-{i:02d}" for i in range(5)}

    async def test_get_by_group_ids_respects_limit_and_cursor(
        self, surreal_schema: SurrealDriver
    ) -> None:
        ops = SurrealEntityEdgeOperations()
        gid = surreal_schema.group_id
        await _seed_entities(surreal_schema, ["ent-a", "ent-b"])

        for i in range(6):
            await ops.save(surreal_schema, _make_edge(f"edge-{i:02d}", gid))

        first_page = await ops.get_by_group_ids(surreal_schema, [gid], limit=3)
        assert len(first_page) == 3
        # Ordered uuid DESC, first entry is edge-05
        assert first_page[0].uuid == "edge-05"
        cursor = first_page[-1].uuid
        second_page = await ops.get_by_group_ids(surreal_schema, [gid], limit=3, uuid_cursor=cursor)
        assert [e.uuid for e in second_page] == ["edge-02", "edge-01", "edge-00"]

    async def test_get_by_group_ids_respects_offset(self, surreal_schema: SurrealDriver) -> None:
        ops = SurrealEntityEdgeOperations()
        gid = surreal_schema.group_id
        await _seed_entities(surreal_schema, ["ent-a", "ent-b"])

        for i in range(6):
            await ops.save(surreal_schema, _make_edge(f"edge-{i:02d}", gid))

        page = await ops.get_by_group_ids(surreal_schema, [gid], limit=2, offset=2)
        assert [e.uuid for e in page] == ["edge-03", "edge-02"]

    async def test_delete_and_delete_by_uuids(self, surreal_schema: SurrealDriver) -> None:
        ops = SurrealEntityEdgeOperations()
        gid = surreal_schema.group_id
        await _seed_entities(surreal_schema, ["ent-a", "ent-b"])

        edge_a = _make_edge("edge-a", gid)
        edge_b = _make_edge("edge-b", gid)
        edge_c = _make_edge("edge-c", gid)
        await ops.save(surreal_schema, edge_a)
        await ops.save(surreal_schema, edge_b)
        await ops.save(surreal_schema, edge_c)

        await ops.delete(surreal_schema, edge_a)
        await ops.delete_by_uuids(surreal_schema, ["edge-b"])

        remaining = await ops.get_by_uuids(surreal_schema, ["edge-a", "edge-b", "edge-c"])
        assert {e.uuid for e in remaining} == {"edge-c"}

    async def test_get_between_nodes(self, surreal_schema: SurrealDriver) -> None:
        ops = SurrealEntityEdgeOperations()
        gid = surreal_schema.group_id
        await _seed_entities(surreal_schema, ["ent-a", "ent-b", "ent-c"])

        await ops.save(
            surreal_schema,
            _make_edge("edge-ab1", gid, source_uuid="ent-a", target_uuid="ent-b"),
        )
        await ops.save(
            surreal_schema,
            _make_edge("edge-ab2", gid, source_uuid="ent-a", target_uuid="ent-b"),
        )
        await ops.save(
            surreal_schema,
            _make_edge("edge-ac", gid, source_uuid="ent-a", target_uuid="ent-c"),
        )

        between = await ops.get_between_nodes(surreal_schema, "ent-a", "ent-b")
        assert {e.uuid for e in between} == {"edge-ab1", "edge-ab2"}
        # Direction matters for get_between_nodes (source -> target).
        reverse = await ops.get_between_nodes(surreal_schema, "ent-b", "ent-a")
        assert reverse == []

        await ops.save(
            surreal_schema,
            _make_edge("edge-other-group", "other-group", source_uuid="ent-a", target_uuid="ent-b"),
        )
        scoped = await ops.get_between_nodes(
            surreal_schema,
            "ent-a",
            "ent-b",
            group_ids=[gid],
            limit=10,
        )
        assert {e.uuid for e in scoped} == {"edge-ab1", "edge-ab2"}

    async def test_get_by_node_uuid_matches_either_endpoint(
        self, surreal_schema: SurrealDriver
    ) -> None:
        ops = SurrealEntityEdgeOperations()
        gid = surreal_schema.group_id
        await _seed_entities(surreal_schema, ["ent-a", "ent-b", "ent-c"])

        await ops.save(
            surreal_schema,
            _make_edge("edge-ab", gid, source_uuid="ent-a", target_uuid="ent-b"),
        )
        await ops.save(
            surreal_schema,
            _make_edge("edge-ca", gid, source_uuid="ent-c", target_uuid="ent-a"),
        )
        await ops.save(
            surreal_schema,
            _make_edge("edge-bc", gid, source_uuid="ent-b", target_uuid="ent-c"),
        )

        touching_a = await ops.get_by_node_uuid(surreal_schema, "ent-a")
        assert {e.uuid for e in touching_a} == {"edge-ab", "edge-ca"}

        await ops.save(
            surreal_schema,
            _make_edge("edge-other-group", "other-group", source_uuid="ent-a", target_uuid="ent-c"),
        )
        scoped = await ops.get_by_node_uuid(surreal_schema, "ent-a", group_ids=[gid], limit=10)
        assert {e.uuid for e in scoped} == {"edge-ab", "edge-ca"}

    async def test_get_by_node_uuids_matches_any_endpoint(
        self, surreal_schema: SurrealDriver
    ) -> None:
        ops = SurrealEntityEdgeOperations()
        gid = surreal_schema.group_id
        await _seed_entities(surreal_schema, ["ent-a", "ent-b", "ent-c", "ent-d"])

        await ops.save(
            surreal_schema,
            _make_edge("edge-ab", gid, source_uuid="ent-a", target_uuid="ent-b"),
        )
        await ops.save(
            surreal_schema,
            _make_edge("edge-cd", gid, source_uuid="ent-c", target_uuid="ent-d"),
        )
        await ops.save(
            surreal_schema,
            _make_edge("edge-bd", gid, source_uuid="ent-b", target_uuid="ent-d"),
        )

        touching_seeds = await ops.get_by_node_uuids(surreal_schema, ["ent-a", "ent-c"])
        assert {e.uuid for e in touching_seeds} == {"edge-ab", "edge-cd"}

        await ops.save(
            surreal_schema,
            _make_edge("edge-other-group", "other-group", source_uuid="ent-a", target_uuid="ent-d"),
        )
        scoped = await ops.get_by_node_uuids(
            surreal_schema,
            ["ent-a", "ent-c"],
            group_ids=[gid],
            limit=10,
        )
        assert {e.uuid for e in scoped} == {"edge-ab", "edge-cd"}

    async def test_load_embeddings_single_and_bulk(self, surreal_schema: SurrealDriver) -> None:
        ops = SurrealEntityEdgeOperations()
        gid = surreal_schema.group_id
        await _seed_entities(surreal_schema, ["ent-a", "ent-b"])

        embedding = [0.1] * EMBEDDING_DIM
        await ops.save(
            surreal_schema,
            _make_edge("edge-1", gid, fact_embedding=embedding),
        )

        fresh = _make_edge("edge-1", gid)
        assert fresh.fact_embedding is None
        await ops.load_embeddings(surreal_schema, fresh)
        assert fresh.fact_embedding is not None
        assert len(fresh.fact_embedding) == EMBEDDING_DIM

        a = _make_edge("edge-1", gid)
        b = _make_edge("edge-missing", gid)
        await ops.load_embeddings_bulk(surreal_schema, [a, b])
        assert a.fact_embedding is not None
        assert b.fact_embedding is None

    async def test_load_embeddings_missing_raises(self, surreal_schema: SurrealDriver) -> None:
        ops = SurrealEntityEdgeOperations()
        gid = surreal_schema.group_id
        edge = _make_edge("edge-missing", gid)
        with pytest.raises(EdgeNotFoundError):
            await ops.load_embeddings(surreal_schema, edge)
