from __future__ import annotations

import asyncio
import os
from uuid import uuid4

import pytest
from pydantic import ValidationError

from sibyl.persistence.surreal.archive_import_runs import (
    ArchiveCheckConflictError,
    CheckedArchiveArtifact,
    SurrealArchiveImportRunRepository,
)
from sibyl_core.backends.surreal import SurrealContentClient
from sibyl_core.backends.surreal.schema_archive_imports import ARCHIVE_IMPORT_DEFINITIONS
from sibyl_core.migrate.personal_archive_plan import (
    ArchiveAudience,
    ArchiveCredentialCeiling,
    ArchiveDisposition,
    ArchiveKind,
    ArchiveMappings,
    ArchiveSourceOrigin,
    CheckedArchivePlan,
    PlannedArchiveRow,
    preview_counts,
)


@pytest.fixture
async def metadata_client():
    client = SurrealContentClient(
        url=os.environ.get("SIBYL_ARCHIVE_TEST_SURREAL_URL", "memory://"),
        username=os.environ.get("SIBYL_ARCHIVE_TEST_SURREAL_USERNAME", ""),
        password=os.environ.get("SIBYL_ARCHIVE_TEST_SURREAL_PASSWORD", ""),
        namespace="archive_repository_" + uuid4().hex,
    )
    await client.execute_query(ARCHIVE_IMPORT_DEFINITIONS)
    try:
        yield client
    finally:
        await client.close()


def _intake():
    actor, org = str(uuid4()), str(uuid4())
    plan = CheckedArchivePlan(
        organization_id=org,
        actor_id=actor,
        archive_sha256="a" * 64,
        artifact_sha256="b" * 64,
        origin=ArchiveSourceOrigin(organization_id=str(uuid4()), source_store="surreal"),
        mappings=ArchiveMappings(
            source_private_owner_id="untrusted-source-owner",
            quarantine=ArchiveAudience(memory_scope="private", scope_key=actor),
        ),
        credential=ArchiveCredentialCeiling(credential_kind="session"),
        rows=(),
        counts={},
    )
    artifact = CheckedArchiveArtifact(
        archive_sha256=plan.archive_sha256,
        artifact_sha256=plan.artifact_sha256,
        member_inventory_json="{}",
        staged_payload_json="{}",
        measured_sizes_json="{}",
    )
    return plan, artifact


async def test_archive_repository_native_actor_scoping_and_replay(metadata_client):
    repo = SurrealArchiveImportRunRepository(metadata_client)
    plan, artifact = _intake()
    saved = await repo.create_checked(
        plan=plan, artifact=artifact, intake_identity="operation", request_sha256="c" * 64
    )
    assert saved.plan == plan
    assert not saved.replayed
    repeated = await repo.create_checked(
        plan=plan, artifact=artifact, intake_identity="operation", request_sha256="c" * 64
    )
    assert repeated.replayed
    assert repeated.record["uuid"] == saved.record["uuid"]
    assert (
        await repo.load(
            str(saved.record["uuid"]), organization_id=plan.organization_id, actor_id=str(uuid4())
        )
        is None
    )
    assert (
        await repo.load(
            str(saved.record["uuid"]), organization_id=str(uuid4()), actor_id=plan.actor_id
        )
        is None
    )
    with pytest.raises(ArchiveCheckConflictError, match="another request"):
        await repo.create_checked(
            plan=plan, artifact=artifact, intake_identity="operation", request_sha256="d" * 64
        )
    assert len(await metadata_client.execute_query("SELECT * FROM archive_import_runs;")) == 1
    assert len(await metadata_client.execute_query("SELECT * FROM archive_import_artifacts;")) == 1


async def test_archive_repository_native_concurrent_check_leaves_one_atomic_pair(metadata_client):
    if not os.environ.get("SIBYL_ARCHIVE_TEST_SURREAL_URL"):
        pytest.skip("concurrent transaction conflicts require the native server")

    # Both real native reads finish before either creation transaction starts.
    ready = asyncio.Event()
    arrivals = 0

    class RacingClient:
        async def execute_query(self, query: str, **params: object):
            nonlocal arrivals
            result = await metadata_client.execute_query(query, **params)
            if query.startswith("SELECT") and params.get("value") == "raced-operation":
                arrivals += 1
                if arrivals == 2:
                    ready.set()
                await ready.wait()
            return result

    repo = SurrealArchiveImportRunRepository(RacingClient())
    plan, artifact = _intake()
    first, second = await asyncio.gather(
        repo.create_checked(
            plan=plan, artifact=artifact, intake_identity="raced-operation", request_sha256="c" * 64
        ),
        repo.create_checked(
            plan=plan, artifact=artifact, intake_identity="raced-operation", request_sha256="c" * 64
        ),
    )
    assert first.record["uuid"] == second.record["uuid"]
    assert sorted([first.replayed, second.replayed]) == [False, True]
    assert len(await metadata_client.execute_query("SELECT * FROM archive_import_runs;")) == 1
    assert len(await metadata_client.execute_query("SELECT * FROM archive_import_artifacts;")) == 1


async def test_archive_repository_rejects_artifact_binding_before_metadata(metadata_client):
    repo = SurrealArchiveImportRunRepository(metadata_client)
    plan, artifact = _intake()
    different = CheckedArchiveArtifact(
        archive_sha256="0" * 64,
        artifact_sha256=artifact.artifact_sha256,
        member_inventory_json=artifact.member_inventory_json,
        staged_payload_json=artifact.staged_payload_json,
        measured_sizes_json=artifact.measured_sizes_json,
    )
    with pytest.raises(ValueError, match="binding mismatch"):
        await repo.create_checked(
            plan=plan, artifact=different, intake_identity="operation", request_sha256="c" * 64
        )
    assert not await metadata_client.execute_query("SELECT * FROM archive_import_runs;")
    assert not await metadata_client.execute_query("SELECT * FROM archive_import_artifacts;")


def _nonempty_plan(plan):
    row = PlannedArchiveRow(
        kind=ArchiveKind.SOURCE_STATE,
        original_id="foreign-state",
        audience=plan.mappings.quarantine,
        disposition=ArchiveDisposition.QUARANTINED,
        reason="foreign source authority stays inert",
        semantic_sha256="e" * 64,
        protection="inert",
    )
    payload = plan.model_dump(mode="python")
    payload["mappings"]["projects"] = {"foreign-project": "destination-project"}
    payload["rows"] = (row.model_dump(mode="python"),)
    payload["counts"] = {
        kind: counts.model_dump(mode="python") for kind, counts in preview_counts((row,)).items()
    }
    return CheckedArchivePlan.model_validate(payload)


@pytest.mark.parametrize("mutation", ["counts", "project"])
async def test_archive_repository_revalidates_mutated_plan_before_native_io(
    metadata_client, mutation
):
    valid, artifact = _intake()
    valid = _nonempty_plan(valid)
    mutated = CheckedArchivePlan.model_validate(valid.model_dump(mode="python"))
    if mutation == "counts":
        mutated.counts.clear()
    else:
        mutated.mappings.projects["foreign-project"] = ""
    calls = 0

    class ObservingClient:
        async def execute_query(self, query: str, **params: object):
            nonlocal calls
            calls += 1
            return await metadata_client.execute_query(query, **params)

    repo = SurrealArchiveImportRunRepository(ObservingClient())
    with pytest.raises(ValidationError):
        await repo.create_checked(
            plan=mutated, artifact=artifact, intake_identity="mutation", request_sha256="c" * 64
        )
    assert calls == 0
    assert not await metadata_client.execute_query("SELECT * FROM archive_import_runs;")
    assert not await metadata_client.execute_query("SELECT * FROM archive_import_artifacts;")
    saved = await repo.create_checked(
        plan=valid, artifact=artifact, intake_identity="healthy", request_sha256="c" * 64
    )
    assert saved.plan == valid
    assert len(await metadata_client.execute_query("SELECT * FROM archive_import_runs;")) == 1
    assert len(await metadata_client.execute_query("SELECT * FROM archive_import_artifacts;")) == 1


async def test_archive_repository_serializes_validated_snapshot_across_native_await(
    metadata_client,
):
    plan, artifact = _intake()
    plan = _nonempty_plan(plan)
    expected = CheckedArchivePlan.model_validate(plan.model_dump(mode="python"))

    class MutatingClient:
        async def execute_query(self, query: str, **params: object):
            if query.startswith("SELECT"):
                plan.counts.clear()
                plan.mappings.projects["foreign-project"] = ""
            return await metadata_client.execute_query(query, **params)

    saved = await SurrealArchiveImportRunRepository(MutatingClient()).create_checked(
        plan=plan, artifact=artifact, intake_identity="snapshot", request_sha256="c" * 64
    )
    assert saved.plan == expected
