"""Native first-write ownership races and transaction-bound revisions."""

import asyncio
import json
import os
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from surrealdb.connections.async_ws import AsyncWsSurrealConnection

from sibyl_core.models.entities import Entity, EntityType
from sibyl_core.services import graph_write_authority as authority
from sibyl_core.services.graph_client import SurrealGraphClient, prepare_graph_schema
from sibyl_core.services.graph_entities import EntityManager


@pytest_asyncio.fixture
async def native_authority(tmp_path):
    url = os.environ.get("SIBYL_ARCHIVE_TEST_SURREAL_URL")
    if not url:
        pytest.skip("first-write races require independent native sockets")
    user = os.environ["SIBYL_ARCHIVE_TEST_SURREAL_USERNAME"]
    password = os.environ["SIBYL_ARCHIVE_TEST_SURREAL_PASSWORD"]
    org = str(uuid4())
    prefix = "native_migration_authority_root_"
    namespace = prefix + org.replace("-", "")
    registry = Path(
        os.environ.get("SIBYL_AUTHORITY_TEST_REGISTRY", str(tmp_path / "registry.jsonl"))
    )
    registry.parent.mkdir(parents=True, exist_ok=True)

    def record(phase):
        with registry.open("a") as stream:
            stream.write(json.dumps({"phase": phase, "namespace": namespace}) + "\n")

    record("registered")
    root = AsyncWsSurrealConnection(url)
    client = SurrealGraphClient(
        group_id=org,
        url=url,
        username=user,
        password=password,
        namespace_prefix=prefix,
        pool_size=4,
    )
    try:
        await root.connect()
        await root.signin({"username": user, "password": password})
        assert namespace not in (await root.query("INFO FOR ROOT;"))["namespaces"]
        await root.query("DEFINE NAMESPACE " + namespace + ";")
        await prepare_graph_schema(client)
        yield client, EntityManager(client, group_id=org), org
    finally:
        await client.close()
        if root.socket is not None:
            if namespace in (await root.query("INFO FOR ROOT;"))["namespaces"]:
                await root.query("REMOVE NAMESPACE " + namespace + ";")
            assert namespace not in (await root.query("INFO FOR ROOT;"))["namespaces"]
            record("absent")
        await root.close()


def entity(org, owner="alice", project="project", **metadata):
    return Entity(
        id="same-title",
        name="Same title",
        entity_type=EntityType.NOTE,
        content=owner + " body",
        description=owner + " description",
        organization_id=org,
        created_by=owner,
        metadata={"principal_id": owner, "project_id": project, **metadata},
    )


@pytest.mark.asyncio
async def test_authorized_create_returns_exact_revision_and_allows_own_rewrite(native_authority):
    _, manager, org = native_authority
    first = await manager.create_direct_authorized(
        entity(org), principal_id="alice", project_id="project"
    )
    second = await manager.create_direct_authorized(
        entity(org, custom="kept"), principal_id="alice", project_id="project"
    )
    assert first.revision == 1 and second.revision == 2
    assert second.metadata["custom"] == "kept"
    assert (await manager.get(first.id)).revision == second.revision


@pytest.mark.asyncio
@pytest.mark.parametrize("owner,project", [("bob", "project"), ("alice", "elsewhere")])
async def test_authorized_create_refuses_foreign_owner_or_project(native_authority, owner, project):
    _, manager, org = native_authority
    await manager.create_direct(entity(org), generate_embedding=False)
    with pytest.raises(ValueError, match="another owner or project"):
        await manager.create_direct_authorized(
            entity(org, owner, project), principal_id=owner, project_id=project
        )
    stored = await manager.get("same-title")
    assert stored.content == "alice body" and stored.revision == 1


@pytest.mark.asyncio
async def test_authorized_create_preserves_legacy_private_owner_before_any_heal(native_authority):
    client, manager, org = native_authority
    legacy = entity(org, "service", memory_scope="private", scope_key="alice")
    legacy.metadata.pop("principal_id")
    await manager.create_direct(legacy, generate_embedding=False)
    await client.execute_query(
        "UPDATE entity SET attributes.metadata = $snapshot WHERE uuid=$uuid;",
        uuid=legacy.id,
        snapshot=json.dumps({"custom": "legacy"}),
    )
    before = await client.execute_query("SELECT * FROM entity WHERE uuid=$uuid;", uuid=legacy.id)
    with pytest.raises(ValueError, match="another owner"):
        await manager.create_direct_authorized(
            entity(org, "bob"), principal_id="bob", project_id="project"
        )
    after = await client.execute_query("SELECT * FROM entity WHERE uuid=$uuid;", uuid=legacy.id)
    assert before == after
    written = await manager.create_direct_authorized(
        entity(org, "alice"), principal_id="alice", project_id="project"
    )
    assert written.metadata["custom"] == "legacy"
    assert "metadata" not in written.metadata


@pytest.mark.asyncio
async def test_two_first_authors_cannot_overwrite_each_other(native_authority, monkeypatch):
    _, manager, org = native_authority
    execute = authority.execute_graph_transaction
    ready = asyncio.Event()
    count = 0

    async def barrier(client, query, **params):
        nonlocal count
        result = await execute(client, query, **params)
        if "RETURN {rows:" in query:
            count += 1
            if count == 2:
                ready.set()
            await asyncio.wait_for(ready.wait(), timeout=10)
        return result

    monkeypatch.setattr(authority, "execute_graph_transaction", barrier)
    results = await asyncio.gather(
        *(
            manager.create_direct_authorized(
                entity(org, owner), principal_id=owner, project_id="project"
            )
            for owner in ("alice", "bob")
        ),
        return_exceptions=True,
    )
    winners = [result for result in results if isinstance(result, Entity)]
    assert len(winners) == 1, results
    stored = await manager.get("same-title")
    assert stored.content == winners[0].content and stored.revision == 1
    assert stored.metadata["principal_id"] == winners[0].metadata["principal_id"]


@pytest.mark.asyncio
async def test_authorized_snapshot_refuses_intervening_body_edit(native_authority, monkeypatch):
    _, manager, org = native_authority
    await manager.create_direct(entity(org), generate_embedding=False)
    execute = authority.execute_graph_transaction

    async def edit_after_read(client, query, **params):
        result = await execute(client, query, **params)
        if "RETURN {rows:" in query:
            await manager.update("same-title", {"content": "later teammate edit"})
        return result

    monkeypatch.setattr(authority, "execute_graph_transaction", edit_after_read)
    with pytest.raises(Exception, match="changed write authority"):
        await manager.create_direct_authorized(
            entity(org), principal_id="alice", project_id="project"
        )
    assert (await manager.get("same-title")).content == "later teammate edit"
