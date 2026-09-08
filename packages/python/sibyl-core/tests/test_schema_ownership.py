"""Schema ownership rejects stale mutations without changing query result shape."""

import asyncio
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from sibyl_core.backends.surreal.schema_ownership import (
    SchemaOwnership,
    SchemaOwnershipLost,
    try_acquire_schema_ownership,
)
from sibyl_core.services.graph_client import SurrealGraphClient


@pytest.fixture
async def client():
    connection = SurrealGraphClient(group_id=f"schema-lease-{uuid4().hex}", url="memory://")
    try:
        yield connection
    finally:
        await connection.close()


async def test_first_claim_is_exclusive_and_release_allows_successor(client):
    claims = await asyncio.gather(
        *(try_acquire_schema_ownership(client.execute_query) for _ in range(10))
    )
    owners = [claim for claim in claims if claim is not None]
    assert len(owners) == 1
    owner = owners[0]
    assert await try_acquire_schema_ownership(client.execute_query) is None
    await owner.release()
    successor = await try_acquire_schema_ownership(client.execute_query)
    assert successor is not None
    assert successor.owner != owner.owner
    with pytest.raises(SchemaOwnershipLost):
        await owner.mutate("CREATE protected:stale;")
    await successor.release()


@pytest.mark.parametrize(
    "body", ["BEGIN TRANSACTION; CREATE protected:one; COMMIT;", "CANCEL;", "COMMIT;"]
)
async def test_mutation_rejects_transaction_controls(client, body):
    owner = await try_acquire_schema_ownership(client.execute_query)
    assert owner is not None
    with pytest.raises(ValueError, match="transaction"):
        await owner.mutate(body)
    await owner.release()


async def test_mutation_reserves_guard_parameters(client):
    owner = await try_acquire_schema_ownership(client.execute_query)
    assert owner is not None
    with pytest.raises(ValueError, match="ownership parameters"):
        await owner.mutate("CREATE protected:one;", sibyl_schema_owned=[{}])
    await owner.release()


async def test_mutation_rejects_unchecked_raw_executor(client):
    owner = await try_acquire_schema_ownership(client.execute_query)
    assert owner is not None
    await owner.mutate("DEFINE TABLE protected SCHEMALESS;")
    unchecked = SchemaOwnership(client.execute_query_raw, owner.owner)
    await client.execute_query("UPDATE schema_lease:graph SET deadline = time::now() - 1s;")
    with pytest.raises(TypeError, match="checked query"):
        await unchecked.mutate("CREATE protected:stale;")
    assert await client.execute_query("SELECT * FROM protected;") == []


async def test_comment_terminated_mutation_commits(client):
    owner = await try_acquire_schema_ownership(client.execute_query)
    assert owner is not None
    assert await owner.mutate("CREATE protected:one SET value = 42; -- trailing comment") is None
    assert await owner.read("SELECT VALUE value FROM protected;") == [42]
    await owner.release()


async def test_index_wait_renews_before_reading_status(client, monkeypatch):
    from sibyl_core.backends.surreal import schema_version

    owner = await try_acquire_schema_ownership(client.execute_query)
    assert owner is not None
    await client.execute_query("UPDATE schema_lease:graph SET deadline = time::now() + 1s;")

    async def ready(*args, **kwargs):
        assert await owner.read(
            "SELECT VALUE deadline > time::now() + 30s FROM schema_lease:graph;"
        ) == [True]
        return schema_version.IndexBuildStatus("ready")

    monkeypatch.setattr(schema_version, "get_index_build_status", ready)
    await schema_version.wait_for_index_ready(
        owner.read, name="example", table="entity", ownership=owner
    )
    await owner.release()


async def test_index_wait_stops_after_ownership_loss(client, monkeypatch):
    from sibyl_core.backends.surreal import schema_version

    owner = await try_acquire_schema_ownership(client.execute_query)
    assert owner is not None
    polls = 0

    async def building(*args, **kwargs):
        nonlocal polls
        polls += 1
        await client.execute_query("UPDATE schema_lease:graph SET deadline = time::now() - 1s;")
        return schema_version.IndexBuildStatus("indexing")

    monkeypatch.setattr(schema_version, "get_index_build_status", building)
    with pytest.raises(SchemaOwnershipLost):
        await schema_version.wait_for_index_ready(
            owner.read, name="example", table="entity", ownership=owner, poll_interval_seconds=0
        )
    assert polls == 1


async def test_configured_lease_duration_applies_to_claim_and_renewal(client):
    owner = await try_acquire_schema_ownership(client.execute_query, lease_seconds=120)
    assert owner is not None
    long_deadline = "SELECT VALUE deadline > time::now() + 100s FROM schema_lease:graph;"
    assert await owner.read(long_deadline) == [True]
    await client.execute_query("UPDATE schema_lease:graph SET deadline = time::now() + 1s;")
    await owner.mutate("CREATE lease_duration_probe:one;")
    assert await owner.read(long_deadline) == [True]
    await client.execute_query("UPDATE schema_lease:graph SET deadline = time::now() + 1s;")
    await owner.heartbeat()
    assert await owner.read(long_deadline) == [True]
    await owner.release()


async def test_invalid_lease_duration_does_not_touch_database(client):
    execute = AsyncMock(wraps=client.execute_query)
    for invalid in (0, -1, True):
        with pytest.raises(ValueError, match="positive integer"):
            await try_acquire_schema_ownership(execute, lease_seconds=invalid)
    execute.assert_not_awaited()


async def test_lease_duration_covers_long_atomic_mutation(client):
    short = await try_acquire_schema_ownership(client.execute_query, lease_seconds=1)
    assert short is not None
    await short.mutate("SLEEP 1500ms; CREATE lease_duration_probe:short;")
    with pytest.raises(SchemaOwnershipLost):
        await short.heartbeat()
    long = await try_acquire_schema_ownership(client.execute_query, lease_seconds=10)
    assert long is not None
    await long.mutate("SLEEP 1500ms; CREATE lease_duration_probe:long;")
    await long.heartbeat()
    assert await long.read("SELECT count() AS count FROM lease_duration_probe GROUP ALL;") == [
        {"count": 2}
    ]
    await long.release()


async def test_index_wait_keeps_heartbeat_within_short_lease(client, monkeypatch):
    from sibyl_core.backends.surreal import schema_version

    execute = AsyncMock(return_value=[{}])
    owner = SchemaOwnership(execute, "synthetic", lease_seconds=3)
    status = AsyncMock(
        side_effect=[
            schema_version.IndexBuildStatus("indexing"),
            schema_version.IndexBuildStatus("ready"),
        ]
    )
    sleep = AsyncMock()
    monkeypatch.setattr(schema_version, "get_index_build_status", status)
    monkeypatch.setattr(schema_version.asyncio, "sleep", sleep)
    await schema_version.wait_for_index_ready(
        owner.read, name="example", table="entity", ownership=owner, poll_interval_seconds=120
    )
    assert sleep.await_count == 1
    assert 0 < sleep.await_args.args[0] < owner.lease_seconds
    assert execute.await_count == 2


async def test_expired_owner_cannot_mutate_or_release_successor(client):
    owner = await try_acquire_schema_ownership(client.execute_query)
    assert owner is not None
    await client.execute_query("UPDATE schema_lease:graph SET deadline = time::now() - 1s;")
    successor = await try_acquire_schema_ownership(client.execute_query)
    assert successor is not None
    await successor.mutate("CREATE protected:current SET value = 1;")
    with pytest.raises(SchemaOwnershipLost):
        await owner.mutate("REMOVE TABLE protected;")
    await owner.release()
    await successor.heartbeat()
    assert await successor.read("SELECT VALUE value FROM protected;") == [1]
    await successor.release()


async def test_expired_heartbeat_stops_without_reviving_lease(client):
    owner = await try_acquire_schema_ownership(client.execute_query)
    assert owner is not None
    await client.execute_query("UPDATE schema_lease:graph SET deadline = time::now() - 1s;")
    with pytest.raises(SchemaOwnershipLost):
        await owner.heartbeat()
    assert await client.execute_query(
        "SELECT VALUE deadline <= time::now() FROM schema_lease:graph;"
    ) == [True]
    with pytest.raises(SchemaOwnershipLost):
        await owner.mutate("CREATE protected:stale;")


async def test_mutation_page_rolls_back_data_and_cursor_together(client):
    owner = await try_acquire_schema_ownership(client.execute_query)
    assert owner is not None
    await owner.mutate("DEFINE TABLE protected SCHEMALESS;")
    await owner.mutate("CREATE checkpoint:one SET cursor = 'before';")
    with pytest.raises(Exception, match="abort page"):
        await owner.mutate(
            "CREATE protected:partial SET value = 1; "
            "UPDATE checkpoint:one SET cursor = 'after'; THROW 'abort page';"
        )
    assert await owner.read("SELECT * FROM protected;") == []
    assert await owner.read("SELECT VALUE cursor FROM checkpoint:one;") == ["before"]
    assert await owner.read("RETURN 7;") == 7
    await owner.mutate(
        "CREATE protected:complete SET value = $value; UPDATE checkpoint:one SET cursor = 'after';",
        value=2,
    )
    assert await owner.read("SELECT VALUE value FROM protected;") == [2]
    assert await owner.read("SELECT VALUE cursor FROM checkpoint:one;") == ["after"]
    await owner.release()


async def test_schema_version_reset_preserves_owner_and_fences_advancement(client):
    owner = await try_acquire_schema_ownership(client.execute_query)
    assert owner is not None
    await owner.mutate("UPSERT schema_version:graph SET name = 'graph', version = 22;")
    await owner.mutate("REMOVE TABLE schema_version;")
    owner.stage = "migration_23"
    await owner.heartbeat()
    assert await owner.read("SELECT VALUE stage FROM schema_lease:graph;") == ["migration_23"]
    await owner.mutate("CREATE schema_version:graph SET name = 'graph', version = 22;")
    await client.execute_query("UPDATE schema_lease:graph SET deadline = time::now() - 1s;")
    successor = await try_acquire_schema_ownership(client.execute_query)
    assert successor is not None
    with pytest.raises(SchemaOwnershipLost):
        await owner.mutate("UPDATE schema_version:graph SET version = 23;")
    assert await successor.read("SELECT VALUE version FROM schema_version:graph;") == [22]
    await successor.mutate("UPDATE schema_version:graph SET version = 23;")
    assert await successor.read("SELECT VALUE version FROM schema_version:graph;") == [23]
    await successor.release()


async def test_embedded_read_reports_takeover_and_preserves_successor(client):
    execute = client.execute_query
    owner = await try_acquire_schema_ownership(execute, renew_after_operation=True)
    assert owner is not None
    successor = None

    async def take_over_before_read(statement, **params):
        nonlocal successor
        if statement.startswith("RETURN 7"):
            await execute("UPDATE schema_lease:graph SET deadline = time::now() - 1s;")
            successor = await try_acquire_schema_ownership(execute, initialize=False)
            assert successor is not None
        return await execute(statement, **params)

    owner.execute = take_over_before_read
    with pytest.raises(SchemaOwnershipLost):
        await owner.read("RETURN 7;")
    assert successor is not None
    await owner.release()
    await successor.heartbeat()
    with pytest.raises(SchemaOwnershipLost):
        await owner.mutate("CREATE protected:stale;")
    await successor.release()
