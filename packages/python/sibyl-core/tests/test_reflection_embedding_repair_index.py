"""Candidate-index scans skip unrelated graph rows and retain keyset progress."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from sibyl_core.backends.surreal.schema import GRAPH_SCHEMA_MIGRATIONS
from sibyl_core.backends.surreal.schema_version import (
    apply_schema_migrations,
    get_schema_version,
    record_schema_version,
)
from sibyl_core.embeddings.providers import DeterministicEmbeddingProvider, EmbeddingMetadata
from sibyl_core.models.entities import Entity, EntityType
from sibyl_core.services import memory_embedding
from sibyl_core.services.graph_client import SurrealGraphClient, prepare_graph_schema


@pytest.fixture
async def indexed_client():
    client = SurrealGraphClient(group_id="repair-index-" + uuid4().hex, url="memory://")
    try:
        await prepare_graph_schema(client)
        yield client
    finally:
        await client.close()


def provider():
    return DeterministicEmbeddingProvider(
        EmbeddingMetadata(
            provider="deterministic",
            model="repair-index",
            dimensions=1024,
            cache_namespace="repair-index",
            tokenizer_estimate_method="utf8-byte-length",
        )
    )


async def test_reflection_repair_index_advances_past_current_pages(indexed_client, monkeypatch):
    client = indexed_client
    configured = provider()
    # This fixture measures candidate traversal, not HNSW construction.
    await client.execute_query("REMOVE INDEX idx_entity_embedding ON entity;")
    rows = [
        dict(
            uuid=f"candidate-{i:04d}",
            group_id=client.group_id,
            entity_type="pattern",
            name="Synthetic candidate",
            derivation_required=True,
            attributes={
                "reflection_identity": {"purpose": "candidate"},
                "embedding_metadata": configured.metadata.to_dict(),
            },
            name_embedding=[0.01] * 1024,
        )
        for i in range(520)
    ]
    rows[-1].pop("name_embedding")
    rows.extend(
        [
            {
                **rows[-1],
                "uuid": "excluded-operational",
                "attributes": {
                    "reflection_identity": {"purpose": "candidate"},
                    "operational_source_id": "other-owner",
                },
            },
            {**rows[-1], "uuid": "excluded-unprotected", "derivation_required": False},
            {
                **rows[-1],
                "uuid": "excluded-critic",
                "attributes": {"reflection_identity": {"purpose": "critic"}},
            },
        ]
    )
    await client.execute_query("INSERT INTO entity $rows RETURN NONE;", rows=rows)
    monkeypatch.setattr(memory_embedding, "configured_embedding_provider", lambda: configured)
    wanted = Entity(id="candidate-0519", name="Synthetic candidate", entity_type=EntityType.PATTERN)
    available = AsyncMock(return_value={wanted.id: wanted})
    monkeypatch.setattr(memory_embedding, "available_graph_entities", available)
    queue = AsyncMock()
    monkeypatch.setattr(memory_embedding, "get_queue_port", lambda: queue)
    execute = client.execute_query
    scans = []

    async def observe(query, **params):
        if "FROM entity WITH INDEX idx_entity_reflection_candidate_uuid" in query:
            plan = await execute(query.removesuffix(";") + " EXPLAIN FULL;", **params)
            scans.append((params["cursor"], plan))
        return await execute(query, **params)

    monkeypatch.setattr(client, "execute_query", observe)
    result = await memory_embedding.repair_promoted_embeddings(SimpleNamespace(client=client))
    assert result.checked == result.pending == 1
    assert result.failed == 0
    assert [cursor for cursor, _plan in scans] == ["", "candidate-0511"]
    available.assert_awaited_once_with(
        client.group_id, [wanted.id], runtime=SimpleNamespace(client=client)
    )
    queue.enqueue_entity_embedding_backfill.assert_awaited_once()
    for _cursor, plan in scans:
        serialized = json.dumps(plan)
        assert "idx_entity_reflection_candidate_uuid" in serialized
        assert "Iterate Table" not in serialized and "Fallback" not in serialized
        assert "candidate" in serialized and ("prefix" in serialized or "access" in serialized)


async def test_reflection_repair_index_upgrade_is_idempotent(indexed_client):
    client = indexed_client
    await client.execute_query(
        'CREATE entity:retained SET uuid="retained", group_id=$org, entity_type="pattern", name="Retained", attributes={reflection_identity:{purpose:"candidate"}}, derivation_required=true;',
        org=client.group_id,
    )
    await client.execute_query("REMOVE INDEX idx_entity_reflection_candidate_uuid ON entity;")
    older = tuple(m for m in GRAPH_SCHEMA_MIGRATIONS if m.version <= 29)
    await record_schema_version(client.execute_query, version=29, migrations=older)
    before = await client.execute_query("SELECT * FROM entity;")
    applied = await apply_schema_migrations(
        client.execute_query, GRAPH_SCHEMA_MIGRATIONS, group_id=client.group_id
    )
    assert [m.version for m in applied] == [30]
    assert await get_schema_version(client.execute_query) == 30
    info = await client.execute_query("INFO FOR TABLE entity;")
    assert "idx_entity_reflection_candidate_uuid" in str(info)
    assert await client.execute_query("SELECT * FROM entity;") == before
    executed = []

    async def observe(query, **params):
        executed.append(query)
        return await client.execute_query(query, **params)

    assert (
        await apply_schema_migrations(observe, GRAPH_SCHEMA_MIGRATIONS, group_id=client.group_id)
        == []
    )
    assert not any("idx_entity_reflection_candidate_uuid" in query for query in executed)
    assert await client.execute_query("SELECT * FROM entity;") == before
