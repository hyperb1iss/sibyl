"""The API backup producer feeds public CLI restore and existing source fences."""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from typer.testing import CliRunner

from sibyl.api.routes import backups as backup_routes
from sibyl.auth.dependencies import get_current_organization
from sibyl.cli import migrate
from sibyl.jobs import backup
from sibyl.persistence import auth_archive, content_archive
from sibyl.persistence.backups_runtime import create_backup_record
from sibyl_core.backends.surreal import (
    SurrealAuthClient,
    SurrealContentClient,
    bootstrap_auth_schema,
    bootstrap_content_schema,
)
from sibyl_core.backends.surreal.schema import bootstrap_schema
from sibyl_core.migrate.archive import load_archive, validate_archive
from sibyl_core.services import content_client
from sibyl_core.services.content_raw_persistence import remember_raw_memory
from sibyl_core.services.graph_client import SurrealGraphClient
from sibyl_core.services.graph_entities import EntityManager
from sibyl_core.services.graph_relationships import RelationshipManager
from sibyl_core.services.graph_runtime import GraphRuntime
from sibyl_core.services.memory_correction import apply_memory_correction


async def _seed_graph(graph, org):
    from sibyl_core.models.entities import Entity, EntityType, Relationship, RelationshipType

    manager = EntityManager(graph, group_id=org)
    for identity in ("graph-a", "graph-b"):
        await manager.create_direct(
            Entity(
                id=identity,
                entity_type=EntityType.SESSION,
                name=identity,
                content="Original graph evidence",
            )
        )
    await RelationshipManager(graph, group_id=org).create(
        Relationship(
            id="graph-edge",
            source_id="graph-a",
            target_id="graph-b",
            relationship_type=RelationshipType.RELATED_TO,
            metadata={"nested": {"literal": "2026-09-10T00:00:00.123456789Z"}},
        )
    )


async def _assert_graph(graph):
    entities = await graph.execute_query("SELECT uuid, content FROM entity ORDER BY uuid;")
    assert entities == [
        {"uuid": key, "content": "Original graph evidence"} for key in ("graph-a", "graph-b")
    ]
    edges = await graph.execute_query(
        "SELECT uuid, source_id, target_id, attributes FROM relates_to;"
    )
    assert len(edges) == 1
    edge = edges[0]
    assert (edge["uuid"], edge["source_id"], edge["target_id"]) == (
        "graph-edge",
        "graph-a",
        "graph-b",
    )
    assert edge["attributes"]["nested"] == {"literal": "2026-09-10T00:00:00.123456789Z"}


def _configure_stores(monkeypatch, stores, active, org, tmp_path):
    closes = [client.close for group in stores.values() for client in group]
    for group in stores.values():
        for client in group:
            monkeypatch.setattr(client, "close", AsyncMock())

    @asynccontextmanager
    async def session():
        yield stores[active()][1]

    async def graph_runtime(_organization):
        assert _organization == org
        graph = stores[active()][2]
        return GraphRuntime(
            client=graph,
            entity_manager=EntityManager(graph, group_id=org),
            relationship_manager=RelationshipManager(graph, group_id=org),
        )

    async def graph_client(_organization):
        return (await graph_runtime(_organization)).client

    monkeypatch.setattr(content_client, "surreal_content_client", session)
    for module in (auth_archive,):
        monkeypatch.setattr(module, "build_surreal_auth_client", lambda: stores[active()][0])
    monkeypatch.setattr(
        content_archive, "build_surreal_content_client", lambda: stores[active()][1]
    )
    monkeypatch.setattr(
        "sibyl.persistence.surreal.auth.build_surreal_auth_client", lambda: stores[active()][0]
    )
    monkeypatch.setattr(
        "sibyl.persistence.surreal.content.build_surreal_content_client",
        lambda: stores[active()][1],
    )
    monkeypatch.setattr("sibyl_core.tools.admin.get_graph_runtime", graph_runtime)
    monkeypatch.setattr("sibyl_core.services.graph.get_surreal_graph_client", graph_client)
    monkeypatch.setattr(
        "sibyl_core.services.memory_lifecycle.get_surreal_graph_runtime", graph_runtime
    )
    monkeypatch.setattr(backup.settings, "store", "surreal")
    monkeypatch.setattr(backup.settings, "auth_store", "surreal")
    monkeypatch.setattr(backup.settings, "backup_dir", tmp_path)
    monkeypatch.setattr("sibyl.persistence.surreal.backups.surreal_content_client", session)
    monkeypatch.setattr(backup, "_safe_broadcast", AsyncMock())

    return closes


async def _seed_and_backup(stores, org, tmp_path):
    for auth, content, graph in stores.values():
        await bootstrap_auth_schema(auth)
        await bootstrap_content_schema(content)
        await bootstrap_schema(graph)
    auth, content, graph = stores["source"]
    await _seed_graph(graph, org)
    await auth.execute_query(
        "CREATE organizations CONTENT $row;", row={"uuid": org, "name": "Owned", "slug": org}
    )
    memory = await remember_raw_memory(
        organization_id=org,
        principal_id="owner",
        source_id="original",
        raw_content="Amethyst original evidence",
        embedding_provider=None,
    )
    await content.execute_query(
        "CREATE eval_consolidations CONTENT $row;",
        row={
            "uuid": "retained-operation",
            "organization_id": org,
            "principal_id": "owner",
            "request_sha256": "a" * 64,
            "result_kind": "abstained",
            "build_receipt_json": "{}",
        },
    )
    await create_backup_record(
        org_id=UUID(org), backup_id="owned_bundle", include_graph=True, created_by_user_id=None
    )
    result = await backup.run_backup({}, org, backup_id="owned_bundle")
    assert result["success"], result
    app = FastAPI()
    app.include_router(backup_routes.router)
    app.dependency_overrides[get_current_organization] = lambda: SimpleNamespace(id=UUID(org))
    app.dependency_overrides[backup_routes.router.dependencies[0].dependency] = lambda: None
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://owned"
    ) as api:
        response = await api.get("/backups/owned_bundle/download")
        assert response.status_code == 200
    path = tmp_path / "downloaded.tar.gz"
    path.write_bytes(response.content)
    assert path.read_bytes() == (tmp_path / "sibyl_owned_bundle.tar.gz").read_bytes()
    loaded = load_archive(path)
    assert validate_archive(loaded) == []
    assert set(loaded.files) == {"auth.json", "content.json", "graph.json"}
    return memory, path


@pytest.mark.parametrize("content_version", ["2.0", "2.1", "2.2"])
def test_actual_api_backup_public_import_retains_auth_and_source_revocation(
    monkeypatch, tmp_path, content_version
):
    org = str(uuid4())
    stores = {
        name: (
            SurrealAuthClient(url="memory://"),
            SurrealContentClient(url="memory://"),
            SurrealGraphClient(group_id=org, url="memory://"),
        )
        for name in ("source", "destination")
    }
    active = "source"
    closes = _configure_stores(monkeypatch, stores, lambda: active, org, tmp_path)
    if content_version != "2.2":

        async def historical_content(organization_id):
            payload = await content_archive.export_content_archive_payload(organization_id)
            omitted = {"memory_validation_executions", "memory_validation_attempts"}
            if content_version == "2.0":
                omitted |= {"dream_source_checkpoints", "dream_source_cursors"}
            for table in omitted:
                assert payload["tables"].pop(table) == []
                payload["row_counts"].pop(table)
            payload["version"] = content_version
            return payload

        monkeypatch.setattr(backup, "export_content_archive_payload", historical_content)

    with asyncio.Runner() as loop:
        # Keep embedded stores in one loop; the production CLI still calls its real restore owners.
        monkeypatch.setattr(
            migrate, "run_async", lambda function: lambda *a, **kw: loop.run(function(*a, **kw))
        )
        try:
            memory, path = loop.run(_seed_and_backup(stores, org, tmp_path))
            active = "destination"
            args = [
                "import",
                str(path),
                "--source-type",
                "surreal-archive",
                "--target-mode",
                "surreal",
                "--org-id",
                org,
                "--clean",
                "--yes",
            ]
            wrong_org_args = list(args)
            wrong_org_args[wrong_org_args.index("--org-id") + 1] = str(uuid4())
            rejected = CliRunner().invoke(migrate.app, wrong_org_args)
            assert rejected.exit_code == 1
            assert "cannot override its organization" in rejected.output
            assert (
                loop.run(stores["destination"][0].execute_query("SELECT * FROM organizations;"))
                == []
            )
            result = CliRunner().invoke(migrate.app, args)
            assert result.exit_code == 0, result.output

            async def assert_restored_and_hide():
                auth, content, graph = stores["destination"]
                await _assert_graph(graph)
                rows = await auth.execute_query("SELECT uuid FROM organizations;")
                assert [r["uuid"] for r in rows] == [org]
                rows = await content.execute_query(
                    "SELECT * FROM raw_captures WHERE uuid=$id;", id=memory.id
                )
                assert rows[0]["raw_content"] == memory.raw_content
                history = await content.execute_query("SELECT * FROM eval_consolidations;")
                assert len(history) == 1
                assert history[0]["uuid"] == "retained-operation"
                await apply_memory_correction(
                    organization_id=org, principal_id="owner", source_id=memory.id, action="hide"
                )
                return await content.execute_query("SELECT * OMIT id FROM source_states;")

            retained = loop.run(assert_restored_and_hide())
            result = CliRunner().invoke(migrate.app, args)
            assert result.exit_code == 0, result.output
            current = loop.run(
                stores["destination"][1].execute_query("SELECT * OMIT id FROM source_states;")
            )
            assert current == retained
            destination = stores["destination"][1]
            loop.run(destination.execute_query("DELETE raw_captures WHERE uuid=$id;", id=memory.id))
            tombstone = loop.run(destination.execute_query("SELECT * OMIT id FROM source_states;"))
            result = CliRunner().invoke(migrate.app, args)
            assert result.exit_code == 0, result.output
            assert loop.run(destination.execute_query("SELECT * FROM raw_captures;")) == []
            assert (
                loop.run(destination.execute_query("SELECT * OMIT id FROM source_states;"))
                == tombstone
            )
            assert (
                len(loop.run(destination.execute_query("SELECT * FROM eval_consolidations;"))) == 1
            )
            loop.run(_assert_graph(stores["destination"][2]))
        finally:
            for close in closes:
                loop.run(close())
