"""Indexed relationship updates preserve vectors and transaction identity."""

import asyncio
import json

import pytest

from sibyl_core.backends.surreal.schema import EMBEDDING_DIM
from sibyl_core.models.entities import Entity, EntityType, Relationship, RelationshipType
from tests.test_reflection_identity import runtime as runtime


def edge(vector, *, target="project_b", label="first"):
    return Relationship(
        id="indexed-edge",
        source_id="project_a",
        target_id=target,
        relationship_type=RelationshipType.RELATED_TO,
        metadata={"fact": label, "embedding": vector, "retained": label},
    )


def vector(index):
    return [1.0 if position == index else 0.0 for position in range(EMBEDDING_DIM)]


async def test_indexed_relationship_embedding_update_replay_and_retarget(runtime):
    manager = runtime.relationship_manager
    first = edge(None)
    await manager.create_direct_bulk([first], generate_embeddings=False)
    original = await runtime.client.execute_query(
        "SELECT * FROM relates_to WHERE uuid='indexed-edge';"
    )
    for index in (0, 1):
        changed = first.model_copy(
            update={"metadata": edge(vector(index), label=str(index)).metadata}
        )
        await manager.create_direct_bulk([changed], generate_embeddings=False)
        rows = await runtime.client.execute_query(
            "SELECT * FROM relates_to WHERE uuid='indexed-edge';"
        )
        assert rows[0]["id"] == original[0]["id"]
        assert rows[0]["fact_embedding"] == vector(index)
        assert rows[0]["attributes"]["retained"] == str(index)
        await manager.create_direct_bulk([changed], generate_embeddings=False)
        assert rows == await runtime.client.execute_query(
            "SELECT * FROM relates_to WHERE uuid='indexed-edge';"
        )
    await runtime.entity_manager.create_direct(
        Entity(id="project_c", name="Third", entity_type=EntityType.PROJECT)
    )
    moved = changed.model_copy(update={"target_id": "project_c"})
    await manager.create_direct_bulk([moved], generate_embeddings=False)
    rows = await runtime.client.execute_query(
        "SELECT *,out.uuid AS endpoint FROM relates_to WHERE uuid='indexed-edge';"
    )
    assert len(rows) == 1 and rows[0]["endpoint"] == "project_c"
    assert rows[0]["id"] != original[0]["id"]
    assert rows[0]["fact_embedding"] == vector(1)
    plan = await runtime.client.execute_query(
        "SELECT id,in,out FROM relates_to WHERE uuid='indexed-edge' LIMIT 1 EXPLAIN;"
    )
    assert "idx_relates_uuid" in json.dumps(plan)


async def test_concurrent_indexed_relationship_creation_has_one_complete_row(runtime, monkeypatch):
    from sibyl_core.backends.surreal import dedicated_client

    original = runtime.client.execute_query
    arrivals = []
    conflicts = []
    original_delay = dedicated_client._transaction_conflict_retry_delay

    def retry_delay(count):
        conflicts.append(count)
        return original_delay(count)

    monkeypatch.setattr(dedicated_client, "_transaction_conflict_retry_delay", retry_delay)

    async def overlapping(statement, **params):
        if "LET $existing = $edges.map" in statement:
            arrivals.append(True)
            if runtime.client.pool_size > 1:
                statement = statement.replace("LET $matching", "SLEEP 200ms; LET $matching")
        return await original(statement, **params)

    monkeypatch.setattr(runtime.client, "execute_query", overlapping)
    await asyncio.gather(
        *(
            runtime.relationship_manager.create_direct_bulk(
                [edge(vector(i), label=str(i))], generate_embeddings=False
            )
            for i in (0, 1)
        )
    )
    assert len(arrivals) == 2
    if runtime.client.pool_size > 1:
        assert conflicts
    rows = await original("SELECT * FROM relates_to WHERE uuid='indexed-edge';")
    assert len(rows) == 1
    index = int(rows[0]["fact"])
    assert rows[0]["attributes"]["retained"] == str(index)
    assert rows[0]["fact_embedding"] == vector(index)


@pytest.mark.parametrize("existing", [False, True])
async def test_indexed_duplicate_rows_preserve_last_body(runtime, existing):
    first = edge(vector(0))
    last = first.model_copy(update={"metadata": edge(vector(1), label="last").metadata})
    if existing:
        await runtime.relationship_manager.create_direct_bulk([first], generate_embeddings=False)
    await runtime.relationship_manager.create_direct_bulk([first, last], generate_embeddings=False)
    rows = await runtime.client.execute_query("SELECT * FROM relates_to WHERE uuid='indexed-edge';")
    assert len(rows) == 1 and rows[0]["fact"] == "last"
    assert rows[0]["fact_embedding"] == vector(1)


async def test_indexed_contradictory_duplicates_refuse_whole_batch(runtime):
    first = edge(None)
    await runtime.relationship_manager.create_direct_bulk([first], generate_embeddings=False)
    await runtime.entity_manager.create_direct(
        Entity(id="project_c", name="Third", entity_type=EntityType.PROJECT)
    )
    before = await runtime.client.execute_query("SELECT * FROM relates_to ORDER BY uuid;")
    changed = first.model_copy(update={"target_id": "project_c"})
    unrelated = first.model_copy(update={"id": "unrelated"})
    with pytest.raises(ValueError, match="conflicting relationship endpoints"):
        await runtime.relationship_manager.create_direct_bulk(
            [unrelated, first, changed], generate_embeddings=False
        )
    assert before == await runtime.client.execute_query("SELECT * FROM relates_to ORDER BY uuid;")


@pytest.mark.parametrize("protected", [False, True])
async def test_indexed_foreign_group_collision_refuses(runtime, protected):
    first = edge(None)
    await runtime.relationship_manager.create_direct_bulk([first], generate_embeddings=False)
    await runtime.client.execute_query(
        "UPDATE relates_to SET group_id='foreign', operational_derivation_required=$protected WHERE uuid='indexed-edge';",
        protected=protected,
    )
    before = await runtime.client.execute_query("SELECT * FROM relates_to ORDER BY uuid;")
    with pytest.raises(Exception, match="relationship identity conflicts with current scope"):
        await runtime.relationship_manager.create_direct_bulk([first], generate_embeddings=False)
    assert before == await runtime.client.execute_query("SELECT * FROM relates_to ORDER BY uuid;")


async def test_indexed_foreign_creation_during_transaction_refuses(runtime, monkeypatch):
    if runtime.client.pool_size < 2:
        pytest.skip("native independent transaction required")
    execute = runtime.client.execute_query
    rival = None

    async def interleave(statement, **params):
        nonlocal rival
        if "LET $existing = $edges.map" in statement and rival is None:
            row = {**params["rows"][0], "group_id": "foreign"}

            async def insert_foreign():
                await asyncio.sleep(0.05)
                await execute("INSERT RELATION INTO relates_to $row;", row=row)

            rival = asyncio.create_task(insert_foreign())
            statement = statement.replace("LET $matching", "SLEEP 200ms; LET $matching")
        return await execute(statement, **params)

    monkeypatch.setattr(runtime.client, "execute_query", interleave)
    with pytest.raises(Exception, match="relationship identity conflicts with current scope"):
        await runtime.relationship_manager.create_direct_bulk(
            [edge(None)], generate_embeddings=False
        )
    assert rival is not None
    await rival
    rows = await execute("SELECT * FROM relates_to WHERE uuid='indexed-edge';")
    assert len(rows) == 1 and rows[0]["group_id"] == "foreign"


async def test_indexed_update_replaces_nested_metadata_and_clears_vector(runtime):
    first = edge(vector(0)).model_copy(
        update={
            "metadata": {"fact": "before", "nested": {"obsolete": True}, "embedding": vector(0)}
        }
    )
    await runtime.relationship_manager.create_direct_bulk([first], generate_embeddings=False)
    changed = first.model_copy(update={"metadata": {"fact": "after", "nested": {"current": True}}})
    await runtime.relationship_manager.create_direct_bulk([changed], generate_embeddings=False)
    rows = await runtime.client.execute_query("SELECT * FROM relates_to WHERE uuid='indexed-edge';")
    assert rows[0]["attributes"] == {"fact": "after", "nested": {"current": True}}
    assert rows[0].get("fact_embedding") is None
