from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import threading
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from importlib.util import find_spec
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import anyio
import httpx
import pytest
from fastapi import FastAPI, Request

from sibyl.api.routes import archive_import_preview as preview, archive_imports as routes
from sibyl.auth.context import AuthContext
from sibyl.auth.dependencies import get_auth_context
from sibyl.persistence.surreal.archive_import_runs import SavedArchiveCheck
from sibyl.persistence.surreal.auth_runtime import api_keys
from sibyl_core.backends.surreal import SurrealContentClient
from sibyl_core.backends.surreal.content_schema import bootstrap_content_schema
from sibyl_core.backends.surreal.schema import bootstrap_schema
from sibyl_core.memory_pipeline.observations import SourceKind
from sibyl_core.migrate.archive import build_manifest, write_archive
from sibyl_core.migrate.personal_archive_intake import ArchiveIntakeBudget
from sibyl_core.migrate.personal_archive_plan import ArchiveKind, canonical_json
from sibyl_core.migrate.source_integrity import build_integrity_archive
from sibyl_core.models.memory_scope import MemoryScope
from sibyl_core.services.content_models import RawMemory, raw_memory_record
from sibyl_core.services.graph_client import SurrealGraphClient
from tests import test_archive_import_authority as archive_auth_fixtures


@pytest.mark.parametrize(
    "module_name",
    [
        "sibyl.persistence.auth_archive",
        "sibyl.persistence.content_archive",
    ],
)
def test_storage_neutral_imports_do_not_load_relational_stack(module_name: str) -> None:
    script = (
        "import importlib, json, sys; "
        f"importlib.import_module({module_name!r}); "
        "print(json.dumps({"
        "'db': 'sibyl.db.connection' in sys.modules, "
        "'sqlalchemy': 'sqlalchemy' in sys.modules"
        "}))"
    )

    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", script],
        capture_output=True,
        check=True,
        text=True,
    )

    assert json.loads(result.stdout) == {"db": False, "sqlalchemy": False}


@pytest.mark.parametrize("module_name", ["sibyl.auth.rls", "sibyl.cache"])
def test_retired_runtime_modules_are_absent(module_name: str) -> None:
    assert find_spec(module_name) is None


archive_auth = archive_auth_fixtures.archive_auth


@dataclass
class ArchiveHttpFixture:
    http: httpx.AsyncClient
    content: SurrealContentClient
    graph: SurrealGraphClient
    context: AuthContext
    token: str
    temporary_directories: list[Path]
    metadata_queries: list[str]

    async def post(self, payload, options, *, operation="checked-operation", **kwargs):
        headers = {"Authorization": "Bearer " + self.token}
        if operation is not None:
            headers["Idempotency-Key"] = operation
        return await self.http.post(
            "/api/archive-imports/check",
            headers=headers,
            files={
                "archive": ("../../outside.tar.gz", payload, "application/gzip"),
                "options": (None, options, "application/json"),
            },
            **kwargs,
        )

    async def status(self, run_id):
        return await self.http.get(
            "/api/archive-imports/" + run_id,
            headers={"Authorization": "Bearer " + self.token},
        )

    async def counts(self):
        return {
            table: len(await self.content.execute_query(query))
            for table, query in {
                "archive_import_runs": "SELECT * FROM archive_import_runs;",
                "archive_import_artifacts": "SELECT * FROM archive_import_artifacts;",
                "raw_captures": "SELECT * FROM raw_captures;",
            }.items()
        }

    def assert_clean(self):
        assert self.temporary_directories
        assert all(not directory.exists() for directory in self.temporary_directories)


def _fixture_budgets():
    from sibyl.api.routes.archive_import_upload import ArchiveUploadBudget

    intake = ArchiveIntakeBudget(
        compressed_bytes=1_000_000,
        inflated_bytes=1_000_000,
        member_bytes=500_000,
        members=16,
        json_depth=32,
        json_scalar_bytes=100_000,
        json_nodes=50_000,
        parsed_rows=10_000,
        encoded_artifact_bytes=1_000_000,
        encoded_plan_bytes=1_000_000,
        metadata_transaction_bytes=2_000_000,
    )
    return intake, ArchiveUploadBudget(
        request_bytes=1_100_000, options_bytes=10_000, header_bytes=2048
    )


@pytest.fixture
async def archive_http(archive_auth, monkeypatch, tmp_path):
    _, token = await archive_auth.key(spaces=[archive_auth.private_space_id])
    context = await archive_auth.key_context(token)
    namespace = "archive_http_" + uuid4().hex
    client = SurrealContentClient(
        url=os.environ.get("SIBYL_ARCHIVE_AUTH_TEST_URL", "memory://"),
        username=os.environ.get("SIBYL_ARCHIVE_AUTH_TEST_USERNAME", ""),
        password=os.environ.get("SIBYL_ARCHIVE_AUTH_TEST_PASSWORD", ""),
        namespace=namespace,
    )
    graph = SurrealGraphClient(
        group_id=str(archive_auth.organization_id),
        url=os.environ.get("SIBYL_ARCHIVE_AUTH_TEST_URL", "memory://"),
        username=os.environ.get("SIBYL_ARCHIVE_AUTH_TEST_USERNAME", ""),
        password=os.environ.get("SIBYL_ARCHIVE_AUTH_TEST_PASSWORD", ""),
        namespace_prefix=namespace + "_graph_",
    )
    queries, directories = [], []

    class ObservingClient:
        async def execute_query(self, query: str, **params: object):
            queries.append(query)
            return await client.execute_query(query, **params)

    @asynccontextmanager
    async def content_scope():
        yield ObservingClient()

    async def graph_client(group_id):
        assert group_id == str(archive_auth.organization_id)
        return graph

    monkeypatch.setattr(routes, "surreal_content_client", content_scope)
    monkeypatch.setattr(preview, "surreal_content_client", content_scope)
    monkeypatch.setattr(preview, "get_surreal_graph_client", graph_client)
    monkeypatch.setattr(routes, "archive_import_budgets", _fixture_budgets)

    def temporary_directory(**kwargs):
        directory = TemporaryDirectory(dir=tmp_path, **kwargs)
        directories.append(Path(directory.name))
        return directory

    monkeypatch.setattr(routes, "TemporaryDirectory", temporary_directory)
    app = FastAPI()
    app.include_router(routes.router, prefix="/api")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://archive.test",
    ) as http:
        fixture = ArchiveHttpFixture(http, client, graph, context, token, directories, queries)

        async def cached_request_context(request: Request):
            request.state.auth_context = fixture.context
            return fixture.context

        app.dependency_overrides[get_auth_context] = cached_request_context
        try:
            await bootstrap_content_schema(client)
            await bootstrap_schema(graph)
            yield fixture
        finally:
            try:
                await client.execute_query(f"REMOVE NAMESPACE `{namespace}`;")
                await graph.execute_query(f"REMOVE NAMESPACE `{graph.namespace}`;")
            finally:
                await client.close()
                await graph.close()


def _personal_archive(
    tmp_path, actor, *, protected=False, body="Synthetic private archive evidence"
):
    organization, owner, identity = str(uuid4()), str(uuid4()), str(uuid4())
    now = datetime(2026, 9, 30, tzinfo=UTC)
    raw = RawMemory(
        id=identity,
        organization_id=organization,
        source_id="untrusted-foreign-source",
        principal_id=owner,
        memory_scope=MemoryScope.PRIVATE,
        title="Synthetic foreign title",
        raw_content=body,
        captured_at=now,
        created_at=now,
        metadata={"label": "untrusted foreign metadata"},
    )
    record = raw_memory_record(raw)
    record["derivation_required"] = protected
    payload = {
        "version": "2.0",
        "organization_id": organization,
        "tables": {"raw_captures": [record]},
        "row_counts": {"raw_captures": 1},
        "total_rows": 1,
        "source_integrity": build_integrity_archive(
            kind=SourceKind.RAW_CAPTURE,
            organizations=[organization],
            source_rows=[record],
            source_states=[
                {
                    "organization_id": organization,
                    "source_kind": "raw_capture",
                    "source_id": identity,
                    "generation": 1,
                    "revision": 1,
                    "deleted": False,
                    "incarnation": str(uuid4()),
                }
            ],
            derivations=[],
        ),
    }
    encoded = json.dumps(payload, default=lambda value: value.isoformat()).encode()
    path = tmp_path / ("source-" + uuid4().hex + ".tar.gz")
    write_archive(
        path,
        manifest=build_manifest(
            organization_id=organization, source_store="surreal", files={"content.json": encoded}
        ),
        files={"content.json": encoded},
    )
    options = canonical_json(
        {
            "mappings": {
                "source_private_owner_id": owner,
                "quarantine": {"memory_scope": "private", "scope_key": actor},
            },
            "conflict_policy": "additive",
        }
    )
    return path.read_bytes(), options


async def test_archive_http_native_checked_status_and_replay(archive_http, tmp_path):
    fixture = archive_http
    payload, options = _personal_archive(tmp_path, fixture.context.user_id)
    response = await fixture.post(payload, options)
    assert response.status_code == 200, response.text
    original = response.json()
    assert original["status"] == "checked"
    assert original["revision"] == 0
    assert not original["replayed"]
    assert original["preview_counts"]["raw_capture"]["created"] == 1
    assert original["preview_counts"]["raw_capture"]["coalesced"] == 1
    assert await fixture.counts() == {
        "archive_import_runs": 1,
        "archive_import_artifacts": 1,
        "raw_captures": 0,
    }
    repeated = await fixture.post(payload, options)
    assert repeated.status_code == 200, repeated.text
    assert repeated.json() == {**original, "replayed": True}
    status = await fixture.status(original["run_id"])
    assert status.status_code == 200, status.text
    assert status.json() == original
    encoded = status.text
    for forbidden in (
        "Synthetic private archive evidence",
        "Synthetic foreign title",
        "untrusted foreign metadata",
        "scope_key",
        "endpoint_ids",
        "witnesses",
        "checked_plan_json",
        "member_inventory_json",
        "staged_payload_json",
    ):
        assert forbidden not in encoded
    assert set(status.json()) == {
        "run_id",
        "status",
        "contract_version",
        "archive_sha256",
        "artifact_sha256",
        "checked_plan_sha256",
        "mappings_sha256",
        "revision",
        "created_at",
        "preview_counts",
        "replayed",
    }
    assert not await fixture.graph.execute_query("SELECT * FROM entity;")
    fixture.assert_clean()


async def test_archive_http_native_protected_rows_are_only_quarantined(archive_http, tmp_path):
    fixture = archive_http
    payload, options = _personal_archive(tmp_path, fixture.context.user_id, protected=True)
    response = await fixture.post(payload, options)
    assert response.status_code == 200, response.text
    counts = response.json()["preview_counts"]["raw_capture"]
    assert counts["created"] == 0
    assert counts["quarantined"] == 1
    assert (await fixture.counts())["raw_captures"] == 0
    fixture.assert_clean()


async def test_archive_http_native_operation_conflict_and_independent_checks(
    archive_http, tmp_path
):
    fixture = archive_http
    first = _personal_archive(tmp_path, fixture.context.user_id)
    other = _personal_archive(tmp_path, fixture.context.user_id, body="Different archive evidence")
    response = await fixture.post(*first)
    assert response.status_code == 200, response.text
    denied = await fixture.post(*other)
    assert denied.status_code == 409
    assert denied.json() == {"detail": "Archive operation conflicts"}
    assert await fixture.counts() == {
        "archive_import_runs": 1,
        "archive_import_artifacts": 1,
        "raw_captures": 0,
    }
    for _ in range(2):
        healthy = await fixture.post(*other, operation=None)
        assert healthy.status_code == 200, healthy.text
        assert not healthy.json()["replayed"]
    assert (await fixture.counts())["archive_import_runs"] == 3
    fixture.assert_clean()


@pytest.mark.parametrize("change", ["revoked", "viewer", "removed"])
async def test_archive_http_native_authority_change_during_parse_denies_before_metadata(
    archive_http, archive_auth, monkeypatch, tmp_path, change
):
    fixture = archive_http
    payload, options = _personal_archive(tmp_path, fixture.context.user_id)
    actual_parse = routes.parse_personal_archive
    loop = asyncio.get_running_loop()

    async def change_authority():
        if change == "revoked":
            await archive_auth.client.execute_query(
                "UPDATE api_keys SET revoked_at=time::now() WHERE uuid=$id;",
                id=fixture.context.api_key_id,
            )
        else:
            await archive_auth.client.execute_query(
                "UPDATE organization_members SET role='viewer' WHERE user_id=$id;"
                if change == "viewer"
                else "DELETE organization_members WHERE user_id=$id;",
                id=fixture.context.user_id,
            )

    def parsing_with_actual_mutation(path, budget):
        parsed = actual_parse(path, budget)
        asyncio.run_coroutine_threadsafe(change_authority(), loop).result(timeout=30)
        return parsed

    monkeypatch.setattr(routes, "parse_personal_archive", parsing_with_actual_mutation)
    response = await fixture.post(payload, options)
    assert response.status_code == (401 if change == "revoked" else 403), response.text
    assert not fixture.metadata_queries
    assert await fixture.counts() == {
        "archive_import_runs": 0,
        "archive_import_artifacts": 0,
        "raw_captures": 0,
    }
    fixture.assert_clean()


@pytest.mark.parametrize("failure", ["malformed", "request_capacity", "metadata_capacity"])
async def test_archive_http_native_failures_cleanup_without_partial_metadata(
    archive_http, monkeypatch, tmp_path, failure
):
    fixture = archive_http
    payload, options = _personal_archive(tmp_path, fixture.context.user_id)
    intake, upload = _fixture_budgets()
    if failure == "malformed":
        payload = b"untrusted malformed archive secret"
    elif failure == "request_capacity":
        upload = replace(upload, request_bytes=1)
    else:
        intake = replace(intake, metadata_transaction_bytes=1)
    monkeypatch.setattr(routes, "archive_import_budgets", lambda: (intake, upload))
    response = await fixture.post(payload, options)
    assert response.status_code == (422 if failure == "malformed" else 413), response.text
    assert "secret" not in response.text
    assert await fixture.counts() == {
        "archive_import_runs": 0,
        "archive_import_artifacts": 0,
        "raw_captures": 0,
    }
    fixture.assert_clean()


async def test_archive_http_native_viewer_and_read_only_key_can_read_own_status(
    archive_http, archive_auth, tmp_path
):
    fixture = archive_http
    payload, options = _personal_archive(tmp_path, fixture.context.user_id)
    created = await fixture.post(payload, options)
    assert created.status_code == 200, created.text
    await archive_auth.client.execute_query(
        "UPDATE organization_members SET role='viewer' WHERE user_id=$user; "
        "UPDATE api_keys SET scopes=['api:read'] WHERE uuid=$key;",
        user=fixture.context.user_id,
        key=fixture.context.api_key_id,
    )
    status = await fixture.status(created.json()["run_id"])
    assert status.status_code == 200, status.text
    assert status.json() == created.json()
    denied = await fixture.post(payload, options)
    assert denied.status_code == 403
    assert (await fixture.counts())["archive_import_runs"] == 1
    fixture.assert_clean()


async def test_archive_http_native_revoked_key_cannot_read_saved_status(
    archive_http, archive_auth, tmp_path
):
    fixture = archive_http
    created = await fixture.post(*_personal_archive(tmp_path, fixture.context.user_id))
    assert created.status_code == 200, created.text
    await archive_auth.client.execute_query(
        "UPDATE api_keys SET revoked_at=time::now() WHERE uuid=$id;", id=fixture.context.api_key_id
    )
    denied = await fixture.status(created.json()["run_id"])
    assert denied.status_code == 401
    assert "preview_counts" not in denied.text


async def test_archive_http_native_lost_transaction_reply_replays_original_pair(
    archive_http, monkeypatch, tmp_path
):
    fixture = archive_http
    payload, options = _personal_archive(tmp_path, fixture.context.user_id)
    lost = False

    class LostReplyClient:
        async def execute_query(self, query: str, **params: object):
            nonlocal lost
            result = await fixture.content.execute_query(query, **params)
            if "BEGIN TRANSACTION" in query and not lost:
                lost = True
                raise ConnectionError("synthetic lost committed response")
            return result

    @asynccontextmanager
    async def scope():
        yield LostReplyClient()

    monkeypatch.setattr(routes, "surreal_content_client", scope)
    interrupted = await fixture.post(payload, options)
    assert interrupted.status_code == 500, interrupted.text
    assert lost
    assert await fixture.counts() == {
        "archive_import_runs": 1,
        "archive_import_artifacts": 1,
        "raw_captures": 0,
    }
    retry = await fixture.post(payload, options)
    assert retry.status_code == 200, retry.text
    assert retry.json()["replayed"]
    assert await fixture.counts() == {
        "archive_import_runs": 1,
        "archive_import_artifacts": 1,
        "raw_captures": 0,
    }
    fixture.assert_clean()


async def test_archive_http_native_concurrent_retries_keep_one_checked_pair(
    archive_http, monkeypatch, tmp_path
):
    if not os.environ.get("SIBYL_ARCHIVE_AUTH_TEST_URL"):
        pytest.skip("native concurrent transactions require the synthetic server")
    fixture = archive_http
    payload, options = _personal_archive(tmp_path, fixture.context.user_id)
    ready, arrivals = asyncio.Event(), 0

    class RacingClient:
        async def execute_query(self, query: str, **params: object):
            nonlocal arrivals
            result = await fixture.content.execute_query(query, **params)
            if "intake_identity=$value" in query:
                arrivals += 1
                if arrivals <= 2:
                    if arrivals == 2:
                        ready.set()
                    await asyncio.wait_for(ready.wait(), timeout=30)
            return result

    @asynccontextmanager
    async def scope():
        yield RacingClient()

    monkeypatch.setattr(routes, "surreal_content_client", scope)
    responses = await asyncio.gather(fixture.post(payload, options), fixture.post(payload, options))
    assert [response.status_code for response in responses] == [200, 200], [
        r.text for r in responses
    ]
    bodies = [response.json() for response in responses]
    assert bodies[0]["run_id"] == bodies[1]["run_id"]
    assert sorted(body["replayed"] for body in bodies) == [False, True]
    assert await fixture.counts() == {
        "archive_import_runs": 1,
        "archive_import_artifacts": 1,
        "raw_captures": 0,
    }
    fixture.assert_clean()


@pytest.mark.parametrize("isolation", ["actor", "organization"])
async def test_archive_http_native_saved_status_is_actor_and_organization_scoped(
    archive_http, archive_auth, tmp_path, isolation
):
    fixture = archive_http
    created = await fixture.post(*_personal_archive(tmp_path, fixture.context.user_id))
    assert created.status_code == 200, created.text
    actor = archive_auth.user_id if isolation == "organization" else uuid4()
    organization = archive_auth.organization_id if isolation == "actor" else uuid4()
    if isolation == "actor":
        await archive_auth.client.execute_query(
            "CREATE users CONTENT $row;",
            row={"uuid": str(actor), "email": "other-actor@example.test", "name": "Other actor"},
        )
    else:
        await archive_auth.client.execute_query(
            "CREATE organizations CONTENT $row;",
            row={"uuid": str(organization), "name": "Other org", "slug": "other-org"},
        )
    await archive_auth.client.execute_query(
        "CREATE organization_members CONTENT $row;",
        row={
            "uuid": str(uuid4()),
            "organization_id": str(organization),
            "user_id": str(actor),
            "role": "member",
        },
    )
    _, token = await api_keys.create_api_key_for_user(
        organization_id=organization,
        user_id=actor,
        name="isolation key",
        live=False,
        scopes=["api:write"],
        project_ids=None,
        memory_space_ids=None,
        expires_at=None,
        request=None,
    )
    fixture.token = token
    fixture.context = await archive_auth.key_context(token)
    denied = await fixture.status(created.json()["run_id"])
    absent = await fixture.status(str(uuid4()))
    assert denied.status_code == absent.status_code == 404
    assert denied.json() == absent.json() == {"detail": "Archive check not found"}
    assert "preview_counts" not in denied.text


async def test_archive_http_native_replay_retains_original_restricted_ceiling(
    archive_http, archive_auth, tmp_path
):
    fixture = archive_http
    payload, options = _personal_archive(tmp_path, fixture.context.user_id)
    created = await fixture.post(payload, options)
    assert created.status_code == 200, created.text
    await archive_auth.client.execute_query(
        "DELETE api_key_memory_space_scopes WHERE api_key_id=$key; "
        "UPDATE api_keys SET memory_scope_restricted=false WHERE uuid=$key;",
        key=fixture.context.api_key_id,
    )
    fixture.context = await archive_auth.key_context(fixture.token)
    assert fixture.context.api_key_memory_space_ids is None
    replay = await fixture.post(payload, options)
    assert replay.status_code == 200, replay.text
    assert replay.json() == {**created.json(), "replayed": True}
    records = await fixture.content.execute_query("SELECT * FROM archive_import_runs;")
    original = json.loads(records[0]["original_ceiling_json"])
    assert original["memory_restricted"]
    assert original["memory_space_ids"] == [str(archive_auth.private_space_id)]
    fixture.assert_clean()


async def test_archive_http_native_replay_cannot_exchange_credential_identity(
    archive_http, archive_auth, tmp_path
):
    fixture = archive_http
    payload, options = _personal_archive(tmp_path, fixture.context.user_id)
    created = await fixture.post(payload, options)
    assert created.status_code == 200, created.text
    _, token = await archive_auth.key(spaces=[archive_auth.private_space_id])
    fixture.token = token
    fixture.context = await archive_auth.key_context(token)
    denied = await fixture.post(payload, options)
    assert denied.status_code == 409, denied.text
    status = await fixture.status(created.json()["run_id"])
    assert status.status_code == 200, status.text
    assert status.json() == created.json()
    assert (await fixture.counts())["archive_import_runs"] == 1
    fixture.assert_clean()


@pytest.mark.parametrize("credential", ["read_key", "write_key", "session"])
async def test_archive_http_native_other_current_credential_reads_after_origin_revocation(
    archive_http, archive_auth, tmp_path, credential
):
    fixture = archive_http
    fixture.http._transport.app.dependency_overrides.clear()
    payload, options = _personal_archive(tmp_path, fixture.context.user_id)
    created = await fixture.post(payload, options)
    assert created.status_code == 200, created.text
    original_key_id = fixture.context.api_key_id
    if credential == "session":
        _, fixture.token, fixture.context = await archive_auth.session()
    else:
        scopes = ["api:read"] if credential == "read_key" else ["api:write"]
        _, fixture.token = await archive_auth.key(
            spaces=[archive_auth.private_space_id], scopes=scopes
        )
        fixture.context = await archive_auth.key_context(fixture.token)
    await archive_auth.client.execute_query(
        "UPDATE api_keys SET revoked_at=time::now() WHERE uuid=$key;",
        key=original_key_id,
    )
    status = await fixture.status(created.json()["run_id"])
    assert status.status_code == 200, status.text
    assert status.json() == created.json()
    denied = await fixture.post(payload, options)
    assert denied.status_code == (403 if credential == "read_key" else 409), denied.text
    assert await fixture.counts() == {
        "archive_import_runs": 1,
        "archive_import_artifacts": 1,
        "raw_captures": 0,
    }
    fixture.assert_clean()


async def test_archive_http_native_current_reader_revoked_during_status_load(
    archive_http, archive_auth, tmp_path, monkeypatch
):
    fixture = archive_http
    fixture.http._transport.app.dependency_overrides.clear()
    payload, options = _personal_archive(tmp_path, fixture.context.user_id)
    created = await fixture.post(payload, options)
    assert created.status_code == 200, created.text
    _, fixture.token = await archive_auth.key(
        spaces=[archive_auth.private_space_id], scopes=["api:read"]
    )
    fixture.context = await archive_auth.key_context(fixture.token)
    original_load = routes.SurrealArchiveImportRunRepository.load
    completed = []

    async def load_then_revoke(repository, run_id, **kwargs):
        record = await original_load(repository, run_id, **kwargs)
        assert record is not None
        await archive_auth.client.execute_query(
            "UPDATE api_keys SET revoked_at=time::now() WHERE uuid=$key;",
            key=fixture.context.api_key_id,
        )
        completed.append(run_id)
        return record

    monkeypatch.setattr(routes.SurrealArchiveImportRunRepository, "load", load_then_revoke)
    denied = await fixture.status(created.json()["run_id"])
    assert completed == [created.json()["run_id"]]
    assert denied.status_code == 401, denied.text
    assert (await fixture.counts())["archive_import_runs"] == 1
    fixture.assert_clean()


async def test_archive_http_native_new_signed_session_can_replay_original_session_plan(
    archive_http, archive_auth, tmp_path
):
    fixture = archive_http
    _, fixture.token, fixture.context = await archive_auth.session()
    payload, options = _personal_archive(tmp_path, fixture.context.user_id)
    created = await fixture.post(payload, options)
    assert created.status_code == 200, created.text
    _, fixture.token, fixture.context = await archive_auth.session()
    replay = await fixture.post(payload, options)
    assert replay.status_code == 200, replay.text
    assert replay.json() == {**created.json(), "replayed": True}
    fixture.assert_clean()


@pytest.mark.parametrize("cancellation_mode", ["asyncio", "scope"])
async def test_archive_http_native_cancellation_waits_for_owned_parser_before_cleanup(
    archive_http, monkeypatch, tmp_path, cancellation_mode
):
    fixture = archive_http
    payload, options = _personal_archive(tmp_path, fixture.context.user_id)
    started, release = asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    actual_parse = routes.parse_personal_archive

    def held_parser(path, budget):
        parsed = actual_parse(path, budget)
        loop.call_soon_threadsafe(started.set)
        assert release.wait(timeout=30), "test did not release owned parser"
        return parsed

    monkeypatch.setattr(routes, "parse_personal_archive", held_parser)
    scopes = []

    async def run_request():
        if cancellation_mode == "asyncio":
            return await fixture.post(payload, options)
        with anyio.CancelScope() as scope:
            scopes.append(scope)
            return await fixture.post(payload, options)
        return "cancelled"

    request = asyncio.create_task(run_request())
    try:
        await asyncio.wait_for(started.wait(), timeout=30)
        if cancellation_mode == "asyncio":
            request.cancel()
        else:
            scopes[0].cancel()
        await asyncio.sleep(0)
        assert not request.done()
        assert fixture.temporary_directories[-1].exists()
        assert not fixture.metadata_queries
    finally:
        release.set()
    if cancellation_mode == "asyncio":
        with pytest.raises(asyncio.CancelledError):
            await request
    else:
        assert await request == "cancelled"
    fixture.assert_clean()
    assert await fixture.counts() == {
        "archive_import_runs": 0,
        "archive_import_artifacts": 0,
        "raw_captures": 0,
    }


def test_archive_http_router_is_registered_in_api_factory():
    from sibyl.api.app import create_api_app

    app = create_api_app()
    paths = app.openapi()["paths"]
    assert "post" in paths["/archive-imports/check"]
    assert "get" in paths["/archive-imports/{run_id}"]


async def _saved_raw_destination(fixture, run_id):
    rows = await fixture.content.execute_query(
        "SELECT * FROM archive_import_runs WHERE uuid=$id;", id=run_id
    )
    plan = SavedArchiveCheck(record=rows[0], replayed=False).plan
    raw = next(row for row in plan.rows if row.kind is ArchiveKind.RAW_CAPTURE)
    assert raw.destination_id is not None
    return raw.destination_id


@pytest.mark.parametrize("hidden", ["foreign_owner", "foreign_organization"])
async def test_archive_http_native_hidden_current_collision_denies_before_new_metadata(
    archive_http, tmp_path, hidden
):
    fixture = archive_http
    payload, options = _personal_archive(tmp_path, fixture.context.user_id)
    created = await fixture.post(payload, options)
    assert created.status_code == 200, created.text
    identity = await _saved_raw_destination(fixture, created.json()["run_id"])
    now = datetime.now(UTC)
    raw = raw_memory_record(
        RawMemory(
            id=identity,
            organization_id=fixture.context.organization_id
            if hidden == "foreign_owner"
            else str(uuid4()),
            source_id=identity,
            principal_id=str(uuid4()) if hidden == "foreign_owner" else fixture.context.user_id,
            memory_scope=MemoryScope.PRIVATE,
            title="Hidden current destination",
            raw_content="Hidden destination body must remain opaque",
            captured_at=now,
            created_at=now,
        )
    )
    await fixture.content.execute_query("CREATE raw_captures CONTENT $row;", row=raw)
    denied = await fixture.post(payload, options, operation="new-hidden-operation")
    assert denied.status_code == 403, denied.text
    assert denied.json() == {"detail": "archive_destination_unavailable"}
    assert "preview_counts" not in denied.text
    assert "Hidden current destination" not in denied.text
    assert await fixture.counts() == {
        "archive_import_runs": 1,
        "archive_import_artifacts": 1,
        "raw_captures": 1,
    }
    unchanged = await fixture.content.execute_query(
        "SELECT * FROM raw_captures WHERE uuid=$id;", id=identity
    )
    assert unchanged[0]["raw_content"] == raw["raw_content"]
    healthy = await fixture.post(
        *_personal_archive(tmp_path, fixture.context.user_id), operation="healthy-neighbor"
    )
    assert healthy.status_code == 200, healthy.text
    assert (await fixture.counts())["archive_import_runs"] == 2
    fixture.assert_clean()


@pytest.mark.parametrize("excluded", ["derivation_required", "deleted"])
async def test_archive_http_native_authorized_excluded_collision_stays_conflicted(
    archive_http, tmp_path, excluded
):
    fixture = archive_http
    payload, options = _personal_archive(tmp_path, fixture.context.user_id)
    created = await fixture.post(payload, options)
    assert created.status_code == 200, created.text
    identity = await _saved_raw_destination(fixture, created.json()["run_id"])
    now = datetime.now(UTC)
    raw = raw_memory_record(
        RawMemory(
            id=identity,
            organization_id=fixture.context.organization_id,
            source_id=identity,
            principal_id=fixture.context.user_id,
            memory_scope=MemoryScope.PRIVATE,
            title="Authorized excluded current row",
            raw_content="Current content retained during an additive check",
            captured_at=now,
            created_at=now,
            deleted_at=now if excluded == "deleted" else None,
        )
    )
    raw["derivation_required"] = excluded == "derivation_required"
    await fixture.content.execute_query("CREATE raw_captures CONTENT $row;", row=raw)
    result = await fixture.post(payload, options, operation="excluded-collision")
    assert result.status_code == 200, result.text
    counts = result.json()["preview_counts"]["raw_capture"]
    assert counts["created"] == 0
    assert counts["conflicted"] == 1
    assert await fixture.counts() == {
        "archive_import_runs": 2,
        "archive_import_artifacts": 2,
        "raw_captures": 1,
    }
    retained = await fixture.content.execute_query(
        "SELECT * FROM raw_captures WHERE uuid=$id;", id=identity
    )
    assert retained[0]["raw_content"] == raw["raw_content"]
    assert retained[0]["derivation_required"] is raw["derivation_required"]
    fixture.assert_clean()


async def test_archive_http_native_empty_current_destination_grants_block_post_replay(
    archive_http, archive_auth, tmp_path
):
    fixture = archive_http
    payload, options = _personal_archive(tmp_path, fixture.context.user_id)
    created = await fixture.post(payload, options)
    assert created.status_code == 200, created.text
    await archive_auth.client.execute_query(
        "DELETE api_key_memory_space_scopes WHERE api_key_id=$key; "
        "UPDATE api_keys SET memory_scope_restricted=true WHERE uuid=$key;",
        key=fixture.context.api_key_id,
    )
    fixture.context = await archive_auth.key_context(fixture.token)
    assert fixture.context.api_key_memory_space_ids == frozenset()
    denied = await fixture.post(payload, options)
    assert denied.status_code == 403, denied.text
    assert "preview_counts" not in denied.text
    own_status = await fixture.status(created.json()["run_id"])
    assert own_status.status_code == 200, own_status.text
    assert own_status.json() == created.json()
    assert await fixture.counts() == {
        "archive_import_runs": 1,
        "archive_import_artifacts": 1,
        "raw_captures": 0,
    }
    fixture.assert_clean()
