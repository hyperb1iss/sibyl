from __future__ import annotations

import asyncio
import json
import os
import threading
from uuid import uuid4

import anyio
import pytest

from sibyl.persistence.surreal import archive_import_artifacts as artifacts
from sibyl.persistence.surreal.archive_import_runs import (
    CheckedArchiveArtifact,
    SurrealArchiveImportRunRepository,
)
from sibyl_core.backends.surreal import SurrealContentClient
from sibyl_core.backends.surreal.schema_archive_imports import ARCHIVE_IMPORT_DEFINITIONS
from sibyl_core.migrate.archive import build_manifest, write_archive
from sibyl_core.migrate.personal_archive_artifact import ArchiveArtifactIntegrityError
from sibyl_core.migrate.personal_archive_intake import ArchiveIntakeBudget, parse_personal_archive
from sibyl_core.migrate.personal_archive_plan import (
    ArchiveAudience,
    ArchiveCredentialCeiling,
    ArchiveMappings,
    CheckedArchivePlan,
)


@pytest.fixture
async def staged(tmp_path):
    namespace = "archive_artifact_loader_" + uuid4().hex
    client = SurrealContentClient(
        url=os.environ.get("SIBYL_ARCHIVE_TEST_SURREAL_URL", "memory://"),
        username=os.environ.get("SIBYL_ARCHIVE_TEST_SURREAL_USERNAME", ""),
        password=os.environ.get("SIBYL_ARCHIVE_TEST_SURREAL_PASSWORD", ""),
        namespace=namespace,
    )
    await client.execute_query(ARCHIVE_IMPORT_DEFINITIONS)
    org, actor, foreign = str(uuid4()), str(uuid4()), str(uuid4())
    graph = {
        "version": "2.0",
        "organization_id": foreign,
        "entities": [{"id": "foreign-entity", "entity_type": "topic", "name": "Inert fixture"}],
        "relationships": [],
    }
    files = {"graph.json": json.dumps(graph).encode()}
    path = tmp_path / "owned.spool"
    write_archive(
        path,
        manifest=build_manifest(organization_id=foreign, source_store="surreal", files=files),
        files=files,
    )
    budget = ArchiveIntakeBudget(
        compressed_bytes=1_000_000,
        inflated_bytes=1_000_000,
        member_bytes=500_000,
        members=16,
        json_depth=30,
        json_scalar_bytes=100_000,
        json_nodes=50_000,
        parsed_rows=10_000,
        encoded_artifact_bytes=1_000_000,
        encoded_plan_bytes=1_000_000,
        metadata_transaction_bytes=2_000_000,
    )
    parsed = parse_personal_archive(path, budget)
    plan = CheckedArchivePlan(
        organization_id=org,
        actor_id=actor,
        archive_sha256=parsed.archive_sha256,
        artifact_sha256=parsed.artifact_sha256,
        origin=parsed.origin,
        mappings=ArchiveMappings(
            source_private_owner_id="untrusted-owner",
            quarantine=ArchiveAudience(memory_scope="private", scope_key=actor),
        ),
        credential=ArchiveCredentialCeiling(
            credential_kind="api_key",
            api_key_id=str(uuid4()),
            rest_scopes=("api:write",),
            project_restricted=True,
            project_ids=(),
            memory_restricted=True,
            memory_space_ids=(),
            memory_scope_keys=(),
        ),
        rows=(),
        counts={},
    )
    saved = await SurrealArchiveImportRunRepository(client).create_checked(
        plan=plan,
        artifact=CheckedArchiveArtifact(
            parsed.archive_sha256,
            parsed.artifact_sha256,
            parsed.member_inventory_json,
            parsed.staged_payload_json,
            parsed.measured_sizes_json,
        ),
        intake_identity="loader-control",
        request_sha256="c" * 64,
    )
    try:
        yield client, saved, plan, parsed
    finally:
        try:
            await client.execute_query(f"REMOVE NAMESPACE {namespace};")
        finally:
            await client.close()


async def _load(staged):
    client, saved, plan, _ = staged
    return await artifacts.SurrealArchiveImportArtifactRepository(client).load(
        str(saved.record["uuid"]), organization_id=plan.organization_id, actor_id=plan.actor_id
    )


async def test_native_saved_artifact_is_scoped_inert_and_preserves_original_empty_ceiling(staged):
    client, saved, plan, parsed = staged
    repository = artifacts.SurrealArchiveImportArtifactRepository(client)
    loaded = await _load(staged)
    assert loaded.run_id == saved.record["uuid"]
    assert loaded.artifact_id == saved.record["artifact_id"]
    assert loaded.plan == plan
    assert loaded.plan.credential.project_restricted
    assert loaded.plan.credential.project_ids == ()
    assert loaded.plan.credential.memory_restricted
    assert loaded.archive.materialize().graph == parsed.graph
    loaded.plan.mappings.projects["foreign"] = "tampered snapshot"
    assert loaded.plan.mappings.projects == {}
    for org, actor in ((str(uuid4()), plan.actor_id), (plan.organization_id, str(uuid4()))):
        assert await repository.load(loaded.run_id, organization_id=org, actor_id=actor) is None
    assert len(await client.execute_query("SELECT * FROM archive_import_runs;")) == 1
    assert len(await client.execute_query("SELECT * FROM archive_import_artifacts;")) == 1
    information = await client.execute_query("INFO FOR DB;")
    assert "entity" not in information["tables"]
    assert "raw_captures" not in information["tables"]


@pytest.mark.parametrize(
    ("table", "field", "value"),
    [
        ("archive_import_runs", "artifact_id", "not-a-uuid"),
        ("archive_import_runs", "contract_version", 2),
        ("archive_import_artifacts", "contract_version", 2),
        ("archive_import_runs", "archive_sha256", "d" * 64),
        ("archive_import_runs", "artifact_sha256", "d" * 64),
        ("archive_import_runs", "origin_json", "{}"),
        ("archive_import_runs", "mappings_json", "{}"),
        ("archive_import_runs", "mappings_sha256", "d" * 64),
        ("archive_import_runs", "original_ceiling_json", "{}"),
        ("archive_import_runs", "original_api_key_id", str(uuid4())),
        ("archive_import_runs", "credential_kind", "session"),
        ("archive_import_runs", "preview_counts_json", '{"invented":{"created":1}}'),
        ("archive_import_runs", "checked_plan_sha256", "d" * 64),
        ("archive_import_runs", "checked_plan_json", "{}"),
        ("archive_import_artifacts", "archive_sha256", "d" * 64),
        ("archive_import_artifacts", "artifact_sha256", "d" * 64),
        ("archive_import_artifacts", "member_inventory_json", "{}"),
        ("archive_import_artifacts", "staged_payload_json", "{}"),
        ("archive_import_artifacts", "measured_sizes_json", "{}"),
    ],
)
async def test_native_privileged_corruption_rejects_saved_bindings(staged, table, field, value):
    client, _, _, _ = staged
    # Root-only alteration in this owned synthetic namespace simulates corrupt
    # storage. Normal event immutability is deliberately bypassed for this control.
    await client.execute_query(
        "REMOVE EVENT archive_import_run_bindings_immutable ON archive_import_runs; "
        "REMOVE EVENT archive_import_artifact_immutable ON archive_import_artifacts;"
    )
    if field == "contract_version":
        await client.execute_query(f"DEFINE FIELD OVERWRITE contract_version ON {table} TYPE int;")
    await client.execute_query(f"UPDATE {table} SET {field}=$value;", value=value)  # noqa: S608
    with pytest.raises(ArchiveArtifactIntegrityError):
        await _load(staged)
    assert len(await client.execute_query("SELECT * FROM archive_import_runs;")) == 1
    assert len(await client.execute_query("SELECT * FROM archive_import_artifacts;")) == 1


@pytest.mark.parametrize("binding", ["uuid", "run_id", "organization_id", "actor_id"])
async def test_native_missing_or_cross_bound_artifact_is_never_substituted(staged, binding):
    client, _, _, _ = staged
    await client.execute_query(
        "REMOVE EVENT archive_import_artifact_immutable ON archive_import_artifacts;"
    )
    await client.execute_query(
        f"UPDATE archive_import_artifacts SET {binding}=$other;",  # noqa: S608
        other=str(uuid4()),
    )
    with pytest.raises(ArchiveArtifactIntegrityError, match="unavailable"):
        await _load(staged)


@pytest.mark.parametrize("identity", ["run_id", "organization_id", "actor_id"])
@pytest.mark.parametrize("mutation", ["uppercase", "malformed"])
async def test_noncanonical_identity_is_rejected_before_native_io(identity, mutation):
    calls = 0

    class NoQueries:
        async def execute_query(self, query, **params):
            nonlocal calls
            calls += 1
            raise AssertionError("invalid identity reached native lookup")

    values = {name: str(uuid4()) for name in ("run_id", "organization_id", "actor_id")}
    values[identity] = values[identity].upper() if mutation == "uppercase" else "not-a-uuid"
    with pytest.raises(ArchiveArtifactIntegrityError):
        await artifacts.SurrealArchiveImportArtifactRepository(NoQueries()).load(**values)
    assert calls == 0


@pytest.mark.parametrize("cancellation", ["asyncio", "anyio"])
async def test_native_artifact_worker_is_joined_before_cancelled_load_returns(
    staged, monkeypatch, cancellation
):
    original = artifacts._validated_pair
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    loop_thread = threading.get_ident()
    worker_threads = []

    def parked(*args):
        worker_threads.append(threading.get_ident())
        entered.set()
        assert release.wait(10)
        try:
            return original(*args)
        finally:
            finished.set()

    monkeypatch.setattr(artifacts, "_validated_pair", parked)
    if cancellation == "asyncio":
        task = asyncio.create_task(_load(staged))
        await asyncio.to_thread(entered.wait)
        task.cancel()
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        with anyio.CancelScope() as scope:
            async with anyio.create_task_group() as group:
                group.start_soon(_load, staged)
                await asyncio.to_thread(entered.wait)
                scope.cancel()
                with anyio.CancelScope(shield=True):
                    await anyio.lowlevel.checkpoint()
                    assert not finished.is_set()
                    release.set()
    assert finished.is_set()
    assert worker_threads
    assert all(thread != loop_thread for thread in worker_threads)
