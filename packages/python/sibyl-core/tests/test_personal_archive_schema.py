from __future__ import annotations

import os
from uuid import uuid4

import pytest

from sibyl_core.backends.surreal import SurrealContentClient
from sibyl_core.backends.surreal.schema_archive_imports import ARCHIVE_IMPORT_DEFINITIONS


def _run(org: str, actor: str, identity: str, artifact: str) -> dict[str, object]:
    return {
        "uuid": str(uuid4()),
        "organization_id": org,
        "actor_id": actor,
        "intake_identity": identity,
        "request_sha256": "a" * 64,
        "contract_version": 1,
        "archive_sha256": "b" * 64,
        "artifact_id": artifact,
        "artifact_sha256": "c" * 64,
        "origin_json": "{}",
        "mappings_json": "{}",
        "mappings_sha256": "d" * 64,
        "conflict_policy": "additive",
        "credential_kind": "session",
        "original_ceiling_json": "{}",
        "checked_plan_json": "{}",
        "checked_plan_sha256": "e" * 64,
        "preview_counts_json": "{}",
    }


async def test_archive_schema_native_atomic_identity_and_immutable_bindings() -> None:
    client = SurrealContentClient(
        url=os.environ.get("SIBYL_ARCHIVE_TEST_SURREAL_URL", "memory://"),
        username=os.environ.get("SIBYL_ARCHIVE_TEST_SURREAL_USERNAME", ""),
        password=os.environ.get("SIBYL_ARCHIVE_TEST_SURREAL_PASSWORD", ""),
        namespace="archive_contract_" + uuid4().hex,
    )
    try:
        await client.execute_query(ARCHIVE_IMPORT_DEFINITIONS)
        org, actor, artifact_id = str(uuid4()), str(uuid4()), str(uuid4())
        record = _run(org, actor, "same-operation", artifact_id)
        artifact = {
            "uuid": artifact_id,
            "organization_id": org,
            "actor_id": actor,
            "run_id": record["uuid"],
            "archive_sha256": record["archive_sha256"],
            "artifact_sha256": record["artifact_sha256"],
            "contract_version": 1,
            "member_inventory_json": "{}",
            "staged_payload_json": "{}",
            "measured_sizes_json": "{}",
        }
        create = (
            "BEGIN TRANSACTION; CREATE archive_import_artifacts CONTENT $artifact; "
            "CREATE archive_import_runs CONTENT $run; COMMIT TRANSACTION;"
        )
        await client.execute_query(create, artifact=artifact, run=record)
        second = _run(org, actor, "same-operation", str(uuid4()))
        orphan = {**artifact, "uuid": second["artifact_id"], "run_id": second["uuid"]}
        with pytest.raises(Exception, match="archive_import_runs_intake"):
            await client.execute_query(create, artifact=orphan, run=second)
        assert len(await client.execute_query("SELECT * FROM archive_import_runs;")) == 1
        assert len(await client.execute_query("SELECT * FROM archive_import_artifacts;")) == 1
        for field in (
            "organization_id",
            "actor_id",
            "origin_json",
            "original_ceiling_json",
            "checked_plan_json",
            "checked_plan_sha256",
        ):
            with pytest.raises(Exception, match="immutable"):
                await client.execute_query(
                    f"UPDATE archive_import_runs SET {field}=$changed;",
                    changed=str(uuid4()),
                )
        with pytest.raises(Exception, match="immutable"):
            await client.execute_query(
                "UPDATE archive_import_artifacts SET staged_payload_json=$changed;",
                changed='{"changed":true}',
            )
    finally:
        await client.close()
