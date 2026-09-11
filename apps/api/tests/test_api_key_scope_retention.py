from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from sibyl.api.routes.memory_auth import api_key_memory_scope_allowed
from sibyl.auth.dependencies import _api_key_claims
from sibyl.persistence.surreal.auth_runtime import api_keys, projects
from sibyl_core.backends.surreal import SurrealAuthClient
from sibyl_core.backends.surreal.auth_schema import bootstrap_auth_schema


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["missing_project", "project_cleanup", "missing_memory"])
async def test_deleted_scoped_target_does_not_broaden_key(monkeypatch, case):
    client = SurrealAuthClient(url="memory://", namespace="authority_" + uuid4().hex)
    await client.connect()
    await bootstrap_auth_schema(client)

    @asynccontextmanager
    async def scope():
        yield client

    monkeypatch.setattr(api_keys, "_auth_client_scope", scope)
    monkeypatch.setattr(projects, "_auth_client_scope", scope)
    org, user, project, space = uuid4(), uuid4(), uuid4(), uuid4()
    try:
        await client.execute_query(
            "CREATE projects CONTENT $r;",
            r={
                "uuid": str(project),
                "organization_id": str(org),
                "graph_project_id": "project-a",
                "name": "A",
                "slug": "a",
            },
        )
        await client.execute_query(
            "CREATE memory_spaces CONTENT $r;",
            r={
                "uuid": str(space),
                "organization_id": str(org),
                "memory_scope": "private",
                "scope_key": str(user),
                "created_by_user_id": str(user),
            },
        )
        await client.execute_query(
            "CREATE projects CONTENT $r;",
            r={
                "uuid": str(uuid4()),
                "organization_id": str(org),
                "graph_project_id": "other-project",
            },
        )
        _key, raw = await api_keys.create_api_key_for_user(
            organization_id=org,
            user_id=user,
            name="synthetic",
            live=False,
            scopes=["api:read"],
            project_ids=["project-a"] if case != "missing_memory" else None,
            memory_space_ids=[space] if case == "missing_memory" else None,
            expires_at=None,
            request=None,
        )
        before = await api_keys.authenticate_api_key(raw)
        assert before is not None
        if case == "missing_memory":
            assert before.memory_spaces
            await client.execute_query("DELETE memory_spaces WHERE uuid=$id;", id=str(space))
        elif case == "project_cleanup":
            monkeypatch.setattr(
                "sibyl.persistence.graph_runtime.delete_project_graph_data", AsyncMock()
            )
            assert await projects.delete_project_record(
                organization_id=org, graph_project_id="project-a"
            )
        else:
            assert before.project_ids == ["project-a"]
            await client.execute_query("DELETE projects WHERE uuid=$id;", id=str(project))
        after = await api_keys.authenticate_api_key(raw)
        assert after is not None
        claims = _api_key_claims(after, scopes=after.scopes)
        if case == "missing_memory":
            assert claims.get("api_key_memory_scope_keys") == []
            assert not api_key_memory_scope_allowed(
                SimpleNamespace(
                    api_key_memory_scope_keys=claims["api_key_memory_scope_keys"], user_id=str(user)
                ),
                memory_scope="private",
                scope_key=str(user),
            )
        else:
            assert claims.get("api_key_project_ids") == []
            ctx = SimpleNamespace(
                organization=SimpleNamespace(id=org),
                user=SimpleNamespace(id=user),
                org_role="owner",
                api_key_project_ids=None,
            )
            assert "other-project" in await projects.list_accessible_project_graph_ids(ctx)
            ctx.api_key_project_ids = claims["api_key_project_ids"]
            assert await projects.list_accessible_project_graph_ids(ctx) == set()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_unrestricted_and_remaining_project_grants(monkeypatch):
    client = SurrealAuthClient(url="memory://", namespace="scope_" + uuid4().hex)
    await client.connect()
    await bootstrap_auth_schema(client)

    @asynccontextmanager
    async def scope():
        yield client

    monkeypatch.setattr(api_keys, "_auth_client_scope", scope)
    org, user = uuid4(), uuid4()
    try:
        for graph_id in ["project-a", "project-b"]:
            await client.execute_query(
                "CREATE projects CONTENT $r;",
                r={"uuid": str(uuid4()), "organization_id": str(org), "graph_project_id": graph_id},
            )

        async def create(project_ids):
            return await api_keys.create_api_key_for_user(
                organization_id=org,
                user_id=user,
                name="synthetic",
                live=False,
                scopes=["api:read"],
                project_ids=project_ids,
                expires_at=None,
                request=None,
            )

        _, unrestricted = await create(None)
        key, restricted = await create(["project-a", "project-b"])
        await client.execute_query("DELETE projects WHERE graph_project_id='project-a';")
        assert (await api_keys.authenticate_api_key(unrestricted)).project_ids is None
        assert (await api_keys.authenticate_api_key(restricted)).project_ids == ["project-b"]
        fresh = await api_keys.resolve_api_key_authority(
            api_key_id=key.id, organization_id=org, user_id=user
        )
        assert fresh.project_ids == ["project-b"]
        assert (
            await api_keys.resolve_api_key_authority(
                api_key_id=key.id, organization_id=uuid4(), user_id=user
            )
            is None
        )
        assert (
            await api_keys.resolve_api_key_authority(
                api_key_id=key.id, organization_id=org, user_id=uuid4()
            )
            is None
        )
        await client.execute_query(
            "UPDATE api_keys SET revoked_at=time::now() WHERE uuid=$id;", id=str(key.id)
        )
        assert (
            await api_keys.resolve_api_key_authority(
                api_key_id=key.id, organization_id=org, user_id=user
            )
            is None
        )
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "evidence", ["declaration", "audit", "none", "foreign_audit", "malformed_audit"]
)
async def test_legacy_restriction_backfill_uses_only_retained_evidence(monkeypatch, evidence):
    client = SurrealAuthClient(url="memory://", namespace="migration_" + uuid4().hex)
    await client.connect()
    await bootstrap_auth_schema(client)

    @asynccontextmanager
    async def scope():
        yield client

    monkeypatch.setattr(api_keys, "_auth_client_scope", scope)
    org, user = uuid4(), uuid4()
    try:
        key, raw = await api_keys.create_api_key_for_user(
            organization_id=org,
            user_id=user,
            name="legacy",
            live=False,
            scopes=["api:read"],
            expires_at=None,
            request=None,
        )
        await client.execute_query(
            "UPDATE api_keys UNSET project_scope_restricted,memory_scope_restricted WHERE uuid=$id;",
            id=str(key.id),
        )
        if evidence == "declaration":
            await client.execute_query(
                "CREATE api_key_project_scopes CONTENT $r;",
                r={"uuid": str(uuid4()), "api_key_id": str(key.id), "project_id": str(uuid4())},
            )
        if evidence in {"audit", "foreign_audit", "malformed_audit"}:
            await client.execute_query(
                "UPDATE audit_logs SET details.project_scope_count=1,details.memory_space_scope_count=1,organization_id=$org WHERE details.api_key_id=$id;",
                org=str(uuid4() if evidence == "foreign_audit" else org),
                id=str(key.id),
            )
        if evidence == "malformed_audit":
            await client.execute_query(
                "UPDATE audit_logs SET details.project_scope_count='unknown',"
                "details.memory_space_scope_count=true;"
            )
        await client.execute_query("REMOVE FIELD project_scope_restricted ON api_keys;")
        await client.execute_query("REMOVE FIELD memory_scope_restricted ON api_keys;")
        await client.execute_query("UPDATE schema_version SET version=7 WHERE name='auth';")
        await bootstrap_auth_schema(client)
        # Prove durable interpretation survives deletion of audit evidence.
        await client.execute_query("DELETE audit_logs;")
        result = await api_keys.authenticate_api_key(raw)
        assert result.project_ids == ([] if evidence in {"declaration", "audit"} else None)
        assert result.memory_spaces == ([] if evidence == "audit" else None)
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [False, True])
async def test_global_archive_preserves_empty_restrictions(monkeypatch, legacy):
    from sibyl.persistence import auth_archive

    source = SurrealAuthClient(url="memory://", namespace="source_" + uuid4().hex)
    destination = SurrealAuthClient(url="memory://", namespace="destination_" + uuid4().hex)
    await bootstrap_auth_schema(source)
    await bootstrap_auth_schema(destination)
    closes = [source.close, destination.close]
    active = source
    monkeypatch.setattr(source, "close", AsyncMock())
    monkeypatch.setattr(destination, "close", AsyncMock())
    monkeypatch.setattr(auth_archive, "build_surreal_auth_client", lambda: active)

    @asynccontextmanager
    async def scope():
        yield active

    monkeypatch.setattr(api_keys, "_auth_client_scope", scope)
    org, user = uuid4(), uuid4()
    try:
        key, raw = await api_keys.create_api_key_for_user(
            organization_id=org,
            user_id=user,
            name="archive",
            live=False,
            scopes=["api:read"],
            expires_at=None,
            request=None,
        )
        await source.execute_query(
            "UPDATE api_keys SET project_scope_restricted=true,memory_scope_restricted=true;"
        )
        if legacy:
            await source.execute_query(
                "UPDATE api_keys UNSET project_scope_restricted,memory_scope_restricted;"
            )
            await source.execute_query(
                "UPDATE audit_logs SET details.project_scope_count=1,details.memory_space_scope_count=1;"
            )
        payload = await auth_archive.export_auth_archive_payload()
        active = destination
        result = await auth_archive.restore_auth_archive_payload(payload, clean=True)
        assert result.success, result.errors
        await destination.execute_query("DELETE audit_logs;")
        auth = await api_keys.authenticate_api_key(raw)
        assert auth is not None
        assert auth.project_ids == []
        assert auth.memory_spaces == []
        assert (
            await api_keys.resolve_api_key_authority(
                api_key_id=key.id, organization_id=org, user_id=user
            )
            == auth
        )
    finally:
        for close in closes:
            await close()


@pytest.mark.asyncio
async def test_expired_deleted_and_partially_missing_memory_authority(monkeypatch):
    from datetime import UTC, datetime, timedelta

    client = SurrealAuthClient(url="memory://", namespace="memory_" + uuid4().hex)
    await bootstrap_auth_schema(client)

    @asynccontextmanager
    async def scope():
        yield client

    monkeypatch.setattr(api_keys, "_auth_client_scope", scope)
    org, user, first, second = uuid4(), uuid4(), uuid4(), uuid4()
    try:
        for space in (first, second):
            await client.execute_query(
                "CREATE memory_spaces CONTENT $r;",
                r={
                    "uuid": str(space),
                    "organization_id": str(org),
                    "memory_scope": "project",
                    "scope_key": str(space),
                    "created_by_user_id": str(user),
                },
            )
        key, raw = await api_keys.create_api_key_for_user(
            organization_id=org,
            user_id=user,
            name="memory",
            live=False,
            scopes=["api:read"],
            memory_space_ids=[first, second],
            expires_at=None,
            request=None,
        )
        await client.execute_query("DELETE memory_spaces WHERE uuid=$id;", id=str(first))
        auth = await api_keys.authenticate_api_key(raw)
        assert auth.memory_space_ids == [second]
        await client.execute_query(
            "UPDATE api_keys SET expires_at=$expiry WHERE uuid=$id;",
            id=str(key.id),
            expiry=datetime.now(UTC) - timedelta(seconds=1),
        )
        assert await api_keys.authenticate_api_key(raw) is None
        assert (
            await api_keys.resolve_api_key_authority(
                api_key_id=key.id, organization_id=org, user_id=user
            )
            is None
        )
        await client.execute_query("DELETE api_keys WHERE uuid=$id;", id=str(key.id))
        assert (
            await api_keys.resolve_api_key_authority(
                api_key_id=key.id, organization_id=org, user_id=user
            )
            is None
        )
    finally:
        await client.close()
