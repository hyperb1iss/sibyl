"""Differential native coverage for adjacency query batching."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from datetime import UTC, datetime

import pytest

from sibyl_core.backends.surreal.records import SurrealRecord
from sibyl_core.models.entities import Entity, EntityType, Relationship, RelationshipType
from sibyl_core.services.graph import SurrealGraphClient, normalize_records, prepare_graph_schema
from sibyl_core.services.graph_entities import EntityManager
from sibyl_core.services.graph_records import _related_entity_projection
from sibyl_core.services.graph_relationships import RelationshipManager


class LegacyRelationshipManager(RelationshipManager):
    """The pre-batching per-seed query, retained as the differential oracle."""

    async def _get_native_related_entity_direction_rows(
        self,
        seed_ids: Sequence[str],
        *,
        endpoint_field: str,
        endpoint_alias: str,
        related_side: str,
        direction: str,
        type_clause: str,
        type_values: Sequence[str],
        limit: int,
    ) -> list[SurrealRecord]:
        async def get_seed_rows(seed_id: str) -> list[SurrealRecord]:
            return normalize_records(
                await self._client.execute_query(
                    f"""
                SELECT id AS record_id,
                       uuid,
                       name,
                       fact,
                       group_id,
                       episodes,
                       attributes,
                       created_at,
                       expired_at,
                       valid_at,
                       invalid_at,
                       source_id AS source_uuid,
                       target_id AS target_uuid,
                       {endpoint_field} AS seed_uuid,
                       {_related_entity_projection(related_side)}
                FROM relates_to
                WHERE group_id = $group_id
                  AND {endpoint_field} = $entity_id
                  AND {related_side}.group_id = $group_id
                """
                    + type_clause
                    + """
                ORDER BY created_at DESC, uuid DESC
                LIMIT $limit;
                """,
                    group_id=self._group_id,
                    entity_id=seed_id,
                    relationship_types=type_values,
                    limit=limit,
                )
            )

        rows = [
            row
            for seed_rows in await asyncio.gather(*(get_seed_rows(seed_id) for seed_id in seed_ids))
            for row in seed_rows
        ]
        for row in rows:
            row["direction"] = direction
            row.setdefault("seed_uuid", row.get(endpoint_alias))
        return rows


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [1, 2, 5])
@pytest.mark.parametrize("filtered", [False, True])
async def test_batched_adjacency_matches_native_per_seed_queries(
    limit: int, filtered: bool
) -> None:
    client = SurrealGraphClient(
        group_id=f"org-adjacency-differential-{limit}-{filtered}", url="memory://"
    )
    try:
        await prepare_graph_schema(client)
        entities = EntityManager(client, group_id=client.group_id)
        manager = RelationshipManager(client, group_id=client.group_id)
        legacy = LegacyRelationshipManager(client, group_id=client.group_id)
        for identity in ("a", "b", "c", "d", "empty"):
            await entities.create_direct(
                Entity(
                    id=identity,
                    entity_type=EntityType.TOPIC,
                    name=identity,
                    organization_id=client.group_id,
                    metadata={"source_ids": ["raw-source"], "confidence": 0.75},
                )
            )
        now = datetime(2026, 1, 1, tzinfo=UTC)
        edges = [
            Relationship(
                id=f"edge-{index}",
                source_id=source,
                target_id=target,
                relationship_type=kind,
                created_at=now,
                metadata={"confidence": 0.8, "source_ids": ["raw-source"]},
            )
            for index, (source, target, kind) in enumerate(
                [
                    ("a", "b", RelationshipType.RELATED_TO),
                    ("a", "c", RelationshipType.RELATED_TO),
                    ("a", "d", RelationshipType.DEPENDS_ON),
                    ("b", "a", RelationshipType.RELATED_TO),
                    ("c", "a", RelationshipType.RELATED_TO),
                    ("b", "d", RelationshipType.RELATED_TO),
                    ("a", "a", RelationshipType.RELATED_TO),
                ]
            )
        ]
        assert await manager.create_bulk(edges) == (len(edges), 0)
        await client.execute_query(
            "UPDATE relates_to SET expired_at=d'2026-01-02T00:00:00Z' WHERE uuid='edge-1';"
        )
        types = [RelationshipType.RELATED_TO] if filtered else None
        seeds = ["b", "a", "empty", "absent", "b"]
        expected = await legacy.get_related_entities_batch(
            seeds,
            relationship_types=types,
            limit_per_entity=limit,
        )
        actual = await manager.get_related_entities_batch(
            seeds,
            relationship_types=types,
            limit_per_entity=limit,
        )
        assert actual == expected
        assert list(actual) == ["b", "a", "empty", "absent"]
        assert actual["empty"] == actual["absent"] == []
        assert all(len(rows) <= limit for rows in actual.values())
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["source_id", "target_id"])
async def test_adjacency_batch_retains_native_index_access(
    endpoint: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = SurrealGraphClient(
        group_id=f"org-adjacency-plan-{endpoint}",
        url="memory://",
        pool_size=1,
    )
    try:
        await prepare_graph_schema(client)
        original_query = client.execute_query
        original_raw = client.execute_query_raw
        original_batch = client.execute_query_batch
        legacy_calls: list[tuple[str, dict[str, object]]] = []
        batch_calls: list[tuple[str, dict[str, object]]] = []

        async def record_legacy(query: str, **params: object) -> object:
            legacy_calls.append((query, params))
            return await original_query(query, **params)

        async def record_batch(query: str, **params: object) -> object:
            batch_calls.append((query, params))
            return await original_batch(query, **params)

        monkeypatch.setattr(client, "execute_query", record_legacy)
        monkeypatch.setattr(client, "execute_query_batch", record_batch)
        legacy = LegacyRelationshipManager(client, group_id=client.group_id)
        manager = RelationshipManager(client, group_id=client.group_id)
        seeds = ["seed-a", "seed-b"]
        assert await legacy.get_related_entities_batch(
            seeds
        ) == await manager.get_related_entities_batch(seeds)
        expected = [
            await original_query(query.rstrip().removesuffix(";") + " EXPLAIN;", **params)
            for query, params in legacy_calls
            if f"AND {endpoint} =" in query
        ]
        [(query, params)] = [call for call in batch_calls if f"AND {endpoint} =" in call[0]]
        explained = (
            ";".join(statement + " EXPLAIN" for statement in query.split(";") if statement.strip())
            + ";"
        )
        result = await original_raw(explained, **params)
        assert isinstance(result, dict)
        statements = result["result"]
        assert isinstance(statements, list) and len(statements) == len(expected) == 2
        assert all(statement["status"] == "OK" for statement in statements)
        assert [statement["result"] for statement in statements] == expected
        for plan_result in expected:
            plan = json.dumps(plan_result, default=str)
            assert "Iterate Index" in plan or "IndexScan" in plan
            assert "Iterate Table" not in plan and "TableScan" not in plan
            if isinstance(plan_result, dict):
                assert f"idx_relates_{endpoint.removesuffix('_id')}_created" in plan
                assert "MemoryOrderedLimit" not in plan
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_adjacency_partitions_keep_existing_pool_capacity_active() -> None:
    class Client:
        pool_size = 3

        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []
            self.started = asyncio.Event()

        async def execute_query_batch(self, query: str, **params: object) -> object:
            self.calls.append(params)
            assert query.count("FROM relates_to") == 2
            if len(self.calls) == 6:
                self.started.set()
            await asyncio.wait_for(self.started.wait(), timeout=1)
            return [[], []]

    client = Client()
    manager = RelationshipManager(client, group_id="org-capacity")
    seeds = [f"seed-{index}" for index in range(6)]
    assert await manager.get_related_entities_batch(seeds) == {seed: [] for seed in seeds}
    assert len(client.calls) == 6
    assert [(params["seed_0"], params["seed_1"]) for params in client.calls] == [
        ("seed-0", "seed-1"),
        ("seed-2", "seed-3"),
        ("seed-4", "seed-5"),
    ] * 2


@pytest.mark.asyncio
async def test_query_capacity_reports_embedded_clamp() -> None:
    client = SurrealGraphClient(group_id="org-capacity-property", url="memory://", pool_size=8)
    try:
        assert client.pool_size == 1
    finally:
        await client.close()
