"""Actual native additive link writes and exact replay behavior."""

from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from surrealdb.connections.async_ws import AsyncWsSurrealConnection

from sibyl_core.models.entities import Entity, EntityType, Relationship, RelationshipType
from sibyl_core.services.graph_client import SurrealGraphClient, prepare_graph_schema
from sibyl_core.services.graph_entities import EntityManager
from sibyl_core.services.graph_link_writes import (
    EntityLinkConflictError,
    add_entity_links_if_revision,
    load_entity_link_snapshot,
)


@pytest_asyncio.fixture
async def native_links(tmp_path):
    url = os.environ.get("SIBYL_ARCHIVE_TEST_SURREAL_URL")
    if not url:
        pytest.skip("native link conflict proof requires independent native sockets")
    username = os.environ["SIBYL_ARCHIVE_TEST_SURREAL_USERNAME"]
    password = os.environ["SIBYL_ARCHIVE_TEST_SURREAL_PASSWORD"]
    org = str(uuid4())
    prefix = "native_migration_links_author_"
    namespace = prefix + org.replace("-", "")
    registry = Path(os.environ.get("SIBYL_LINKS_TEST_REGISTRY", str(tmp_path / "registry.jsonl")))
    registry.parent.mkdir(parents=True, exist_ok=True)

    def record(phase):
        with registry.open("a") as stream:
            stream.write(json.dumps({"phase": phase, "namespaces": [namespace]}) + "\n")

    record("registered")
    root = AsyncWsSurrealConnection(url)
    client = SurrealGraphClient(
        group_id=org,
        url=url,
        username=username,
        password=password,
        namespace_prefix=prefix,
        pool_size=4,
    )
    try:
        await root.connect()
        await root.signin({"username": username, "password": password})
        before = await root.query("INFO FOR ROOT;")
        assert namespace not in before["namespaces"]
        await root.query("DEFINE NAMESPACE " + namespace + ";")
        await prepare_graph_schema(client)
        manager = EntityManager(client, group_id=org, embedding_provider=None)
        for entity_id, kind in (
            ("source", EntityType.TASK),
            ("target", EntityType.TASK),
            ("epic", EntityType.EPIC),
        ):
            await manager.create_direct(
                Entity(
                    id=entity_id,
                    entity_type=kind,
                    name=entity_id,
                    description="original description",
                    content="original body",
                    organization_id=org,
                    created_by="owner",
                    metadata={
                        "memory_scope": "private",
                        "principal_id": "owner",
                        "status": "in_progress",
                        "custom": "preserve",
                    },
                ),
                generate_embedding=False,
            )
        yield client, manager, org
    finally:
        await client.close()
        if root.socket is not None:
            catalog = await root.query("INFO FOR ROOT;")
            if namespace in catalog["namespaces"]:
                await root.query("REMOVE NAMESPACE " + namespace + ";")
            after = await root.query("INFO FOR ROOT;")
            assert namespace not in after["namespaces"]
            record("absent")
        await root.close()


async def _snapshots(client, org, *ids):
    from sibyl_core.services.graph_link_writes import _LOAD_LINK_SNAPSHOT

    rows = []
    for entity_id in ids:
        try:
            rows.append(await load_entity_link_snapshot(client, entity_id, group_id=org))
        except RuntimeError:
            print(
                "native-snapshot-diagnostic",
                await client.execute_query_raw(_LOAD_LINK_SNAPSHOT, uuid=entity_id, group_id=org),
            )
            raise
    assert all(row is not None for row in rows)
    return rows


def _edge(source="source", target="target"):
    return Relationship(
        id=f"rel_{source}_depends_on_{target}",
        source_id=source,
        target_id=target,
        relationship_type=RelationshipType.DEPENDS_ON,
    )


@pytest.mark.asyncio
async def test_native_links_add_topology_preserves_body_and_exact_replay(native_links):
    client, manager, org = native_links
    source, target, epic = await _snapshots(client, org, "source", "target", "epic")
    before = source.entity
    result = await add_entity_links_if_revision(
        client,
        group_id=org,
        source=source,
        targets=[target, epic],
        expected_revision=before.revision,
        relationships=[_edge()],
        epic_id="epic",
        parent_task_id="target",
        depends_on=["target"],
    )
    assert not result.replayed and result.added_relationship_ids == [_edge().id]
    assert result.revision == before.revision + 1
    stored = await manager.get("source")
    assert (stored.name, stored.description, stored.content) == (
        before.name,
        before.description,
        before.content,
    )
    assert stored.metadata["status"] == "in_progress"
    assert stored.metadata["custom"] == "preserve"
    assert stored.metadata["epic_id"] == "epic"
    assert stored.metadata["parent_task_id"] == "target"
    assert stored.metadata["depends_on"] == ["target"]
    await manager.update("source", {"content": "teammate body", "metadata": {"status": "done"}})
    current, target, epic = await _snapshots(client, org, "source", "target", "epic")
    replay = await add_entity_links_if_revision(
        client,
        group_id=org,
        source=current,
        targets=[target, epic],
        expected_revision=before.revision,
        relationships=[_edge()],
        epic_id="epic",
        parent_task_id="target",
        depends_on=["target"],
    )
    assert replay.replayed and replay.added_relationship_ids == []
    assert replay.existing_relationship_ids == [_edge().id]
    after = await manager.get("source")
    assert after.content == "teammate body" and after.metadata["status"] == "done"
    assert after.revision == current.entity.revision


@pytest.mark.asyncio
async def test_native_links_stale_revision_has_no_partial_writes(native_links):
    client, manager, org = native_links
    source, target = await _snapshots(client, org, "source", "target")
    expected = source.entity.revision
    await manager.update("source", {"content": "new body", "metadata": {"status": "done"}})
    current, target = await _snapshots(client, org, "source", "target")
    with pytest.raises(EntityLinkConflictError, match="revision"):
        await add_entity_links_if_revision(
            client,
            group_id=org,
            source=current,
            targets=[target],
            expected_revision=expected,
            relationships=[_edge()],
            depends_on=["target"],
        )
    stored = await manager.get("source")
    assert stored.content == "new body" and stored.metadata["status"] == "done"
    assert "depends_on" not in stored.metadata
    assert not await client.execute_query(
        "SELECT * FROM relates_to WHERE uuid = $uuid;", uuid=_edge().id
    )


@pytest.mark.asyncio
async def test_native_links_changed_authorized_target_is_rejected(native_links):
    client, manager, org = native_links
    source, target = await _snapshots(client, org, "source", "target")
    await manager.update("target", {"content": "target changed"})
    with pytest.raises(EntityLinkConflictError, match="changed"):
        await add_entity_links_if_revision(
            client,
            group_id=org,
            source=source,
            targets=[target],
            expected_revision=source.entity.revision,
            relationships=[_edge()],
        )
    assert (await manager.get("source")).revision == source.entity.revision
    assert not await client.execute_query(
        "SELECT * FROM relates_to WHERE uuid = $uuid;", uuid=_edge().id
    )


@pytest.mark.asyncio
async def test_native_links_conflicting_existing_topology_is_not_replaced(native_links):
    client, manager, org = native_links
    await manager.update("source", {"metadata": {"parent_task_id": "other"}})
    source, target = await _snapshots(client, org, "source", "target")
    with pytest.raises(EntityLinkConflictError, match="topology"):
        await add_entity_links_if_revision(
            client,
            group_id=org,
            source=source,
            targets=[target],
            expected_revision=source.entity.revision,
            parent_task_id="target",
        )
    assert (await manager.get("source")).metadata["parent_task_id"] == "other"


@pytest.mark.asyncio
async def test_native_links_conflicting_existing_edge_is_not_repointed(native_links):
    from sibyl_core.services.graph_relationships import RelationshipManager

    client, manager, org = native_links
    await RelationshipManager(client, group_id=org, embedding_provider=None).create_direct_bulk(
        [
            Relationship(
                id=_edge().id,
                source_id="target",
                target_id="source",
                relationship_type=RelationshipType.RELATED_TO,
            )
        ]
    )
    source, target = await _snapshots(client, org, "source", "target")
    with pytest.raises(EntityLinkConflictError, match="identity"):
        await add_entity_links_if_revision(
            client,
            group_id=org,
            source=source,
            targets=[target],
            expected_revision=source.entity.revision,
            relationships=[_edge()],
        )
    assert (await manager.get("source")).revision == source.entity.revision
    rows = await client.execute_query(
        "SELECT source_id, target_id, name FROM relates_to WHERE uuid = $uuid;", uuid=_edge().id
    )
    assert rows[0]["source_id"] == "target" and rows[0]["target_id"] == "source"


@pytest.mark.asyncio
@pytest.mark.parametrize("revision", [True, 0, -1, "1", None])
async def test_links_invalid_revision_is_denied_before_io(revision):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    client = SimpleNamespace(execute_query_raw=AsyncMock())
    with pytest.raises(ValueError, match="positive integer"):
        await add_entity_links_if_revision(
            client,
            group_id="org",
            source=None,
            targets=[],
            expected_revision=revision,
        )
    client.execute_query_raw.assert_not_awaited()


class _DeferredNativeCommit:
    """Test-only barrier around the unchanged native transaction body."""

    def __init__(self, client, connection, transaction, *, omit_state=None):
        import asyncio

        self.client = client
        self.connection = connection
        self.transaction = transaction
        self.omit_state = omit_state
        self.staged = asyncio.Event()
        self.release = asyncio.Event()

    def __getattr__(self, name):
        return getattr(self.client, name)

    async def execute_query_raw(self, query, **params):
        from sibyl_core.backends.surreal.records import raise_on_error
        from sibyl_core.backends.surreal.schema_source_witness import SOURCE_STATE_WRITE_WITNESS

        sql = query.strip()
        assert sql.startswith("BEGIN TRANSACTION;")
        assert sql.endswith("COMMIT TRANSACTION;")
        body = sql.removeprefix("BEGIN TRANSACTION;").removesuffix("COMMIT TRANSACTION;")
        if self.omit_state is not None:
            # Remove only the endpoint witness, retaining the full native cut check.
            assert body.count(SOURCE_STATE_WRITE_WITNESS) == 1
            body = body.replace(
                SOURCE_STATE_WRITE_WITNESS,
                SOURCE_STATE_WRITE_WITNESS.replace(
                    "UPDATE $source_state.id",
                    "IF $source_state.id != $omitted_state { UPDATE $source_state.id",
                ).replace("type::string(rand::uuid());", "type::string(rand::uuid()); };"),
            )
            params["omitted_state"] = self.omit_state
        response = await self.connection.query_raw(
            f"USE NS {self.client.namespace} DB graph;\n" + body,
            params=params,
            txn_id=self.transaction,
        )
        if response.get("error"):
            raise RuntimeError("Native staged query failed", response["error"])
        raise_on_error(response, query=body)
        self.staged.set()
        await self.release.wait()
        await self.connection.commit(self.transaction)
        evidence = Path(os.environ["SIBYL_LINKS_TEST_REGISTRY"]).with_suffix(".responses.jsonl")
        with evidence.open("a") as stream:
            stream.write(json.dumps({"query": body, "response": response}, default=str) + "\n")
        frames = response["result"]
        assert frames[0]["status"] == "OK"
        assert frames[0]["result"] == {"database": "graph", "namespace": self.client.namespace}
        # The wrapper adds USE; production's scoped socket adds no result frame.
        return {**response, "result": frames[1:]}


async def _deferred(client, *, omit_state=None):
    connection = AsyncWsSurrealConnection(os.environ["SIBYL_ARCHIVE_TEST_SURREAL_URL"])
    await connection.connect()
    await connection.signin(
        {
            "username": os.environ["SIBYL_ARCHIVE_TEST_SURREAL_USERNAME"],
            "password": os.environ["SIBYL_ARCHIVE_TEST_SURREAL_PASSWORD"],
        }
    )
    await connection.use(client.namespace, "graph")
    transaction = await connection.begin()
    return _DeferredNativeCommit(client, connection, transaction, omit_state=omit_state)


@pytest.mark.asyncio
@pytest.mark.parametrize("winner", ["links", "edit"])
@pytest.mark.parametrize("edited", ["source", "target"])
async def test_native_links_real_conflicts_in_both_commit_orders(native_links, winner, edited):
    import asyncio

    from surrealdb.errors import ServerError

    client, manager, org = native_links
    source, target = await _snapshots(client, org, "source", "target")
    links = await _deferred(client)
    edit = await _deferred(client)
    tasks = {}
    try:
        tasks["links"] = asyncio.create_task(
            add_entity_links_if_revision(
                links,
                group_id=org,
                source=source,
                targets=[target],
                expected_revision=source.entity.revision,
                relationships=[_edge()],
            )
        )
        tasks["edit"] = asyncio.create_task(
            EntityManager(edit, group_id=org, embedding_provider=None).update(
                edited, {"content": "concurrent edit", "metadata": {"status": "done"}}
            )
        )
        await asyncio.wait_for(asyncio.gather(links.staged.wait(), edit.staged.wait()), 15)
        first, second = (links, edit) if winner == "links" else (edit, links)
        first.release.set()
        await asyncio.wait_for(tasks[winner], 10)
        second.release.set()
        loser = "edit" if winner == "links" else "links"
        with pytest.raises(ServerError) as caught:
            await asyncio.wait_for(tasks[loser], 10)
        assert caught.value.kind == "Query"
        assert caught.value.code == -32009
        assert caught.value.details == {"kind": "TransactionConflict"}
        print(
            "native-link-conflict",
            winner,
            edited,
            caught.value.kind,
            caught.value.code,
            caught.value.details,
        )
        stored = await manager.get(edited)
        edges = await client.execute_query(
            "SELECT * FROM relates_to WHERE uuid = $uuid;", uuid=_edge().id
        )
        if winner == "edit":
            assert stored.content == "concurrent edit"
            assert stored.metadata["status"] == "done"
            assert edges == []
        else:
            assert stored.content == "original body"
            assert stored.metadata["status"] == "in_progress"
            assert len(edges) == 1
    finally:
        for wrapper in (links, edit):
            wrapper.release.set()
            await wrapper.connection.close()
        await asyncio.gather(*tasks.values(), return_exceptions=True)


@pytest.mark.asyncio
async def test_native_links_omitted_only_target_state_witness_allows_both_commits(native_links):
    import asyncio

    client, manager, org = native_links
    source, target = await _snapshots(client, org, "source", "target")
    links = await _deferred(client, omit_state=target.state_id)
    edit = await _deferred(client)
    tasks = {}
    try:
        tasks["links"] = asyncio.create_task(
            add_entity_links_if_revision(
                links,
                group_id=org,
                source=source,
                targets=[target],
                expected_revision=source.entity.revision,
                relationships=[_edge()],
            )
        )
        tasks["edit"] = asyncio.create_task(
            EntityManager(edit, group_id=org, embedding_provider=None).update(
                "target", {"content": "concurrent edit"}
            )
        )
        await asyncio.wait_for(asyncio.gather(links.staged.wait(), edit.staged.wait()), 15)
        edit.release.set()
        await asyncio.wait_for(tasks["edit"], 10)
        links.release.set()
        result = await asyncio.wait_for(tasks["links"], 10)
        assert not result.replayed
        assert (await manager.get("target")).content == "concurrent edit"
        assert (
            len(
                await client.execute_query(
                    "SELECT * FROM relates_to WHERE uuid = $uuid;", uuid=_edge().id
                )
            )
            == 1
        )
        print("omitted-target-witness-both-acknowledged")
    finally:
        for wrapper in (links, edit):
            wrapper.release.set()
            await wrapper.connection.close()
        await asyncio.gather(*tasks.values(), return_exceptions=True)


@pytest.mark.asyncio
async def test_native_links_unrelated_edit_and_links_both_commit(native_links):
    import asyncio

    client, manager, org = native_links
    await manager.create_direct(
        Entity(
            id="unrelated",
            entity_type=EntityType.TASK,
            name="unrelated",
            content="independent body",
            organization_id=org,
        ),
        generate_embedding=False,
    )
    source, target = await _snapshots(client, org, "source", "target")
    links = await _deferred(client)
    edit = await _deferred(client)
    tasks = {}
    try:
        tasks["links"] = asyncio.create_task(
            add_entity_links_if_revision(
                links,
                group_id=org,
                source=source,
                targets=[target],
                expected_revision=source.entity.revision,
                relationships=[_edge()],
            )
        )
        tasks["edit"] = asyncio.create_task(
            EntityManager(edit, group_id=org, embedding_provider=None).update(
                "unrelated", {"content": "independent edit"}
            )
        )
        await asyncio.wait_for(asyncio.gather(links.staged.wait(), edit.staged.wait()), 15)
        links.release.set()
        await asyncio.wait_for(tasks["links"], 10)
        edit.release.set()
        await asyncio.wait_for(tasks["edit"], 10)
        assert (await manager.get("unrelated")).content == "independent edit"
        assert (await manager.get("source")).content == "original body"
    finally:
        for wrapper in (links, edit):
            wrapper.release.set()
            await wrapper.connection.close()
        await asyncio.gather(*tasks.values(), return_exceptions=True)
