"""The API backup producer feeds public CLI restore and existing source fences."""

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from typer.testing import CliRunner

from sibyl.api.routes import backups as backup_routes
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

    async def auth_client():
        return stores[active()][0]

    monkeypatch.setattr(
        "sibyl.persistence.surreal.auth.get_shared_surreal_auth_client", auth_client
    )

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


CREDENTIAL_TABLES = (
    "user_sessions",
    "api_keys",
    "device_authorization_requests",
    "api_key_project_scopes",
    "api_key_memory_space_scopes",
)


async def _seed_credentials(auth, org, prefix):
    rows = {
        "user_sessions": {
            "user_id": "owner",
            "organization_id": org,
            "token_hash": prefix + "-session",
            "refresh_token_hash": prefix + "-refresh",
            "expires_at": datetime.now(UTC) + timedelta(hours=1),
        },
        "api_keys": {
            "organization_id": org,
            "user_id": "owner",
            "key_prefix": prefix,
            "key_salt": "salt",
            "key_hash": prefix + "-hash",
        },
        "device_authorization_requests": {
            "organization_id": org,
            "user_id": "owner",
            "device_code_hash": prefix + "-device",
            "user_code": prefix + "-code",
            "expires_at": datetime.now(UTC) + timedelta(hours=1),
        },
        "api_key_project_scopes": {"api_key_id": prefix + "-api_keys", "project_id": "project"},
        "api_key_memory_space_scopes": {
            "api_key_id": prefix + "-api_keys",
            "memory_space_id": "space",
        },
        "organization_invitations": {
            "organization_id": org,
            "invited_email": "owned@example.test",
            "created_by_user_id": "owner",
            "token": prefix + "-invite",
            "token_hash": prefix + "-invite-hash",
        },
    }
    for table, row in rows.items():
        await auth.execute_query(
            f"CREATE {table} CONTENT $row;", row={"uuid": prefix + "-" + table, **row}
        )


async def _credential_rows(auth, table):
    assert table in CREDENTIAL_TABLES
    return await auth.execute_query(f"SELECT * FROM {table};")  # noqa: S608 - fixed table inventory


async def _seed_destination_credentials(auth, org):
    await _seed_credentials(auth, str(uuid4()), "unrelated")
    before = {table: await _credential_rows(auth, table) for table in CREDENTIAL_TABLES}
    await _seed_credentials(auth, org, "old-target")
    return before


async def _assert_scoped_credentials(auth, org, before, output):
    for table in CREDENTIAL_TABLES:
        assert f"Skipped 1 {table} rows" in output
        assert await _credential_rows(auth, table) == before[table]
    invitations = await auth.execute_query(
        "SELECT * FROM organization_invitations WHERE organization_id=$org;", org=org
    )
    assert len(invitations) == 1
    assert invitations[0].get("token") is None
    assert invitations[0].get("token_hash") is None


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
    await _seed_credentials(auth, org, "source")
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
    response = await _authenticated_download(auth, org)
    path = tmp_path / "downloaded.tar.gz"
    path.write_bytes(response.content)
    assert path.read_bytes() == (tmp_path / "sibyl_owned_bundle.tar.gz").read_bytes()
    loaded = load_archive(path)
    assert validate_archive(loaded) == []
    assert set(loaded.files) == {"auth.json", "content.json", "graph.json"}
    return memory, path


async def _authenticated_download(auth, org):
    from sibyl.persistence.auth_runtime import create_api_key_for_user

    assert backup.settings.disable_auth is False
    user_id = uuid4()
    await auth.execute_query(
        "CREATE users CONTENT $row;",
        row={"uuid": str(user_id), "email": f"{user_id}@archive.invalid", "name": "Backup owner"},
    )
    await auth.execute_query(
        "CREATE organization_members CONTENT $row;",
        row={
            "uuid": str(uuid4()),
            "organization_id": org,
            "user_id": str(user_id),
            "role": "owner",
        },
    )
    _key, bearer = await create_api_key_for_user(
        organization_id=UUID(org),
        user_id=user_id,
        name="Owned archive download",
        live=False,
        scopes=["api:read"],
        expires_at=None,
        request=None,
    )
    app = FastAPI()
    app.include_router(backup_routes.router, prefix="/api")
    assert not app.dependency_overrides
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://owned"
    ) as api:
        route = "/api/backups/owned_bundle/download"
        assert (await api.get(route)).status_code == 401
        assert (
            await api.get(route, headers={"Authorization": "Bearer sk_invalid"})
        ).status_code == 401
        response = await api.get(route, headers={"Authorization": f"Bearer {bearer}"})
        assert response.status_code == 200, response.text
        await auth.execute_query(
            "UPDATE organization_members SET role='viewer' WHERE user_id=$user;", user=str(user_id)
        )
        assert (
            await api.get(route, headers={"Authorization": f"Bearer {bearer}"})
        ).status_code == 403
        return response


@pytest.mark.parametrize("content_version", ["2.0", "2.1", "2.2", "2.3"])
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
    if content_version != "2.3":

        async def historical_content(organization_id):
            payload = await content_archive.export_content_archive_payload(organization_id)
            payload.pop("validation_receipts")
            omitted = (
                set()
                if content_version == "2.2"
                else {"memory_validation_executions", "memory_validation_attempts"}
            )
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
            unrelated_before = loop.run(
                _seed_destination_credentials(stores["destination"][0], org)
            )
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
            loop.run(
                _assert_scoped_credentials(
                    stores["destination"][0], org, unrelated_before, result.output
                )
            )

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


@pytest.mark.parametrize("scoped", [False, True])
@pytest.mark.parametrize("clean", [False, True])
def test_auth_restore_separates_global_credentials_from_scoped_metadata(monkeypatch, scoped, clean):
    org = str(uuid4())
    source = SurrealAuthClient(url="memory://")
    destination = SurrealAuthClient(url="memory://")
    closes = [source.close, destination.close]
    active = source
    monkeypatch.setattr(auth_archive, "build_surreal_auth_client", lambda: active)
    monkeypatch.setattr(source, "close", AsyncMock())
    monkeypatch.setattr(destination, "close", AsyncMock())
    with asyncio.Runner() as loop:
        try:
            loop.run(bootstrap_auth_schema(source))
            loop.run(bootstrap_auth_schema(destination))
            loop.run(_seed_credentials(source, org, "source"))
            # A fully credential-bearing payload must not confer authority merely
            # because an importer labels it as organization scoped.
            payload = loop.run(auth_archive.export_auth_archive_payload())
            if scoped:
                payload["organization_id"] = org
            active = destination
            result = loop.run(auth_archive.restore_auth_archive_payload(payload, clean=clean))
            assert result.success, result.errors
            for table in CREDENTIAL_TABLES:
                rows = loop.run(_credential_rows(destination, table))
                assert len(rows) == (0 if scoped else 1)
                if scoped:
                    assert result.skipped_credential_rows[table] == 1
                else:
                    original = loop.run(_credential_rows(source, table))[0]
                    for field in (
                        "token_hash",
                        "refresh_token_hash",
                        "key_salt",
                        "key_hash",
                        "device_code_hash",
                        "user_code",
                        "api_key_id",
                    ):
                        assert rows[0].get(field) == original.get(field)
            invitations = loop.run(
                destination.execute_query("SELECT * FROM organization_invitations;")
            )
            assert len(invitations) == 1
            assert invitations[0].get("token_hash") == (None if scoped else "source-invite-hash")
            if not scoped:
                assert result.skipped_credential_rows == {}
        finally:
            for close in closes:
                loop.run(close())


@pytest.mark.parametrize("table", CREDENTIAL_TABLES)
def test_scoped_malformed_credential_table_never_imports_valid_grants(monkeypatch, table):
    org = str(uuid4())
    store = SurrealAuthClient(url="memory://")
    close = store.close
    monkeypatch.setattr(store, "close", AsyncMock())
    monkeypatch.setattr(auth_archive, "build_surreal_auth_client", lambda: store)
    with asyncio.Runner() as loop:
        try:
            loop.run(bootstrap_auth_schema(store))
            loop.run(_seed_credentials(store, org, "source"))
            payload = loop.run(auth_archive.export_auth_archive_payload())
            payload["organization_id"] = org
            payload["tables"][table].append(None)
            result = loop.run(auth_archive.restore_auth_archive_payload(payload, clean=True))
            assert not result.success
            assert f"{table} row payload must be an object" in result.errors
            for credential_table in CREDENTIAL_TABLES:
                assert loop.run(_credential_rows(store, credential_table)) == []
                assert result.skipped_credential_rows[credential_table] == 1
        finally:
            loop.run(close())
