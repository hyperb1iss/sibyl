"""Logical content restores preserve completed ciphertext independently of the old volume."""

import copy
import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest

from sibyl.persistence import content_archive
from sibyl_core.ai.llm.extractor import ExtractionUsage
from sibyl_core.backends.surreal import SurrealContentClient, bootstrap_content_schema
from sibyl_core.config import settings
from sibyl_core.migrate import validation_receipt_archive
from sibyl_core.services import content_client, validation_receipts
from sibyl_core.services.validation_execution import ValidationExecution
from sibyl_core.services.validation_stages import run_validation_stage
from sibyl_core.tasks.memory_validation import MemoryValidationResult
from tests.test_validation_execution_archive import history as history  # noqa: PLC0414


@pytest.fixture(autouse=True)
def private_volume(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "validation_receipt_dir", str(tmp_path / "old-volume"))


def completed_result():
    return MemoryValidationResult(
        status="no_findings",
        submission=None,
        reason=None,
        input_sha256="a" * 64,
        schema_sha256="b" * 64,
        configured_policy_json="{}",
        usage=ExtractionUsage(requests=1, input_tokens=31, output_tokens=7, total_tokens=38),
    )


async def retain_completed(execution, monkeypatch):
    monkeypatch.setattr(execution, "_finish", AsyncMock(side_effect=OSError("result outage")))
    with pytest.raises(OSError, match="result outage"):
        await execution.record_result(completed_result())


async def test_content_archive_recovers_without_original_volume(history, monkeypatch, tmp_path):
    _source, execution = history
    await retain_completed(execution, monkeypatch)
    archive = await content_archive.export_content_archive_payload("org")
    section = archive["validation_receipts"]
    assert section["executions"][0]["status"] == "journal"
    ciphertext = next((tmp_path / "old-volume").glob("*.receipt")).read_bytes()
    (tmp_path / "old-volume").rename(tmp_path / "unavailable-old-volume")
    monkeypatch.setattr(settings, "validation_receipt_dir", str(tmp_path / "new-volume"))
    destination = SurrealContentClient(url="memory://")
    await bootstrap_content_schema(destination)
    close = destination.close
    monkeypatch.setattr(destination, "close", AsyncMock())
    monkeypatch.setattr(content_archive, "build_surreal_content_client", lambda: destination)

    @asynccontextmanager
    async def session():
        yield destination

    monkeypatch.setattr(content_client, "surreal_content_client", session)
    try:
        restored = await content_archive.restore_content_archive_payload(archive, clean=True)
        assert restored.success, restored.errors
        assert next((tmp_path / "new-volume").glob("*.receipt")).read_bytes() == ciphertext
        fresh = ValidationExecution(execution.id, "org", "owner")
        row = await fresh.load()
        forbidden = AsyncMock(side_effect=AssertionError("provider dispatch forbidden"))
        result = await run_validation_stage(
            execution=fresh,
            parent_id=row["parent_id"],
            source_ids=row["source_ids"],
            request=json.loads(row["request_json"]),
            policy=row["policy_json"],
            check_current=AsyncMock(),
            run=forbidden,
        )
        assert result["usage"] == completed_result().usage.model_dump(mode="json")
        assert result["status"] == "no_findings"
        forbidden.assert_not_awaited()
        assert not list((tmp_path / "new-volume").glob("*.receipt"))
    finally:
        await close()


async def test_capture_reconciliation_race_is_explicit_failure(history, monkeypatch):
    client, execution = history
    await retain_completed(execution, monkeypatch)
    original = validation_receipt_archive.capture
    snapshot = original(
        (await content_archive.export_content_archive_payload("org"))["tables"][
            "memory_validation_executions"
        ]
    )

    def reconcile_during_capture(rows):
        # Simulate a disappearing file; the corresponding DB update is performed
        # by the query hook before the exporter validates its snapshot.
        validation_receipts.discard(rows[0]["request_json"])
        return snapshot

    original_query = client.execute_query
    calls = 0

    async def query(statement, **params):
        nonlocal calls
        if "FROM memory_validation_executions" in statement:
            calls += 1
            if calls == 2:
                await original_query("UPDATE memory_validation_executions SET state='failed';")
        return await original_query(statement, **params)

    monkeypatch.setattr(validation_receipt_archive, "capture", reconcile_during_capture)
    monkeypatch.setattr(client, "execute_query", query)
    with pytest.raises(ValueError, match="changed during receipt"):
        await content_archive.export_content_archive_payload("org")


@pytest.mark.parametrize(
    "mutation", ["missing", "checksum", "scope", "duplicate", "status", "ciphertext"]
)
async def test_receipt_tamper_rejected_before_writes(history, monkeypatch, mutation):
    client, execution = history
    await retain_completed(execution, monkeypatch)
    archive = await content_archive.export_content_archive_payload("org")
    before = await client.execute_query("SELECT * FROM memory_validation_executions;")
    entry = archive["validation_receipts"]["executions"][0]
    if mutation == "missing":
        archive["validation_receipts"]["executions"] = []
    elif mutation == "checksum":
        entry["sha256"] = "0" * 64
    elif mutation == "scope":
        entry["execution_id"] = "foreign"
    elif mutation == "ciphertext":
        import base64
        import hashlib

        entry["ciphertext"] = base64.b64encode(b"unauthenticated").decode()
        entry["sha256"] = hashlib.sha256(b"unauthenticated").hexdigest()
    elif mutation == "duplicate":
        archive["validation_receipts"]["executions"].append(copy.deepcopy(entry))
    else:
        entry["status"] = "database"
    with pytest.raises(ValueError):
        await content_archive.restore_content_archive_payload(archive, clean=True)
    assert await client.execute_query("SELECT * FROM memory_validation_executions;") == before


async def test_interrupted_publication_can_resume_exact_import(history, monkeypatch, tmp_path):
    _client, execution = history
    await retain_completed(execution, monkeypatch)
    archive = await content_archive.export_content_archive_payload("org")
    monkeypatch.setattr(settings, "validation_receipt_dir", str(tmp_path / "interrupted-volume"))
    publish = validation_receipt_archive.publish

    def interrupted(pending):
        publish(pending)
        raise OSError("after durable publication")

    monkeypatch.setattr(validation_receipt_archive, "publish", interrupted)
    failed = await content_archive.restore_content_archive_payload(archive, clean=True)
    assert not failed.success
    retained = next((tmp_path / "interrupted-volume").glob("*.receipt")).read_bytes()
    monkeypatch.setattr(validation_receipt_archive, "publish", publish)
    restored = await content_archive.restore_content_archive_payload(archive, clean=True)
    assert restored.success, restored.errors
    assert next((tmp_path / "interrupted-volume").glob("*.receipt")).read_bytes() == retained


async def test_purged_destination_never_recovers_archived_receipt(history, monkeypatch, tmp_path):
    client, execution = history
    await retain_completed(execution, monkeypatch)
    archive = await content_archive.export_content_archive_payload("org")
    await client.execute_query("DELETE raw_captures;")
    monkeypatch.setattr(settings, "validation_receipt_dir", str(tmp_path / "purged-volume"))
    restored = await content_archive.restore_content_archive_payload(archive, clean=True)
    assert not restored.success
    fresh = ValidationExecution(execution.id, "org", "owner")
    assert not await fresh.recover_completed_receipt()
    row = await fresh.load()
    assert row["purged"] is True
    assert row.get("recovery_key") is None
    assert row.get("result_json") is None
    assert not await client.execute_query("SELECT * FROM raw_captures;")


async def test_unresolved_execution_is_explicit_and_legacy_v22_stays_supported(history):
    _client, _execution = history
    archive = await content_archive.export_content_archive_payload("org")
    assert archive["validation_receipts"]["executions"][0]["status"] == "unresolved"
    archive["version"] = "2.2"
    archive.pop("validation_receipts")
    restored = await content_archive.restore_content_archive_payload(archive, clean=True)
    assert restored.success, restored.errors


_RECOVER_PROCESS = r"""
import asyncio,json,sys
from contextlib import asynccontextmanager
from sibyl_core.backends.surreal import SurrealContentClient
from sibyl_core.services import content_client
from sibyl_core.services.validation_execution import ValidationExecution
from sibyl_core.services.validation_stages import run_validation_stage
payload=json.load(sys.stdin)
async def main():
 client=SurrealContentClient(url=payload['url'],namespace=payload['namespace'],database=payload['database'],username='',password='')
 @asynccontextmanager
 async def session():yield client
 content_client.surreal_content_client=session
 execution=ValidationExecution(payload['execution_id'],payload['org'],'owner')
 row=await execution.load()
 async def forbidden():raise AssertionError('provider dispatch forbidden on restored host')
 async def current():
  states=await client.execute_query('SELECT * FROM source_states WHERE organization_id=$org AND source_id=$source;',org=payload['org'],source=row['parent_id'])
  assert len(states)==1 and states[0]['deleted'] is False
 try:
  result=await run_validation_stage(execution=execution,parent_id=row['parent_id'],source_ids=row['source_ids'],request=json.loads(row['request_json']),policy=row['policy_json'],check_current=current,run=forbidden)
  assert result['usage']==payload['usage'] and result['status']=='no_findings'
  print('FRESH_HOST_RECOVERY_PASS')
 finally:await client.close()
asyncio.run(main())
"""


def test_public_backup_import_fresh_process_recovery(monkeypatch, tmp_path):
    import asyncio
    import os
    import subprocess
    import sys
    from uuid import uuid4

    from typer.testing import CliRunner

    from sibyl.cli import migrate
    from sibyl.jobs import backup
    from sibyl_core.backends.surreal import SurrealAuthClient
    from sibyl_core.services.graph_client import SurrealGraphClient
    from sibyl_core.tasks.procedure_review import review_digest
    from tests.test_backup_archive_restore import _configure_stores, _seed_and_backup

    org = str(uuid4())
    stores = {
        name: (
            SurrealAuthClient(url="memory://"),
            SurrealContentClient(url="memory://"),
            SurrealGraphClient(group_id=org, url="memory://"),
        )
        for name in ("source", "destination")
    }
    if stores["source"][1]._url.startswith("memory:"):
        pytest.skip("Fresh-process public restore requires the isolated native database")
    active = "source"
    closes = _configure_stores(monkeypatch, stores, lambda: active, org, tmp_path)
    export = backup.export_content_archive_payload
    retained = {}

    async def complete_before_export(organization_id):
        client = stores["source"][1]
        states = await client.execute_query("SELECT * FROM source_states WHERE deleted=false;")
        state = states[0]
        request = {
            "org": org,
            "principal": "owner",
            "parent": state["source_id"],
            "policy": "{}",
            "source_bindings": [
                {key: state[key] for key in ("source_id", "incarnation", "generation")}
            ],
        }
        execution = ValidationExecution(review_digest(request), org, "owner")
        await execution.begin(
            parent_id=state["source_id"],
            source_ids=[state["source_id"]],
            request=request,
            policy="{}",
        )
        await retain_completed(execution, monkeypatch)
        retained["execution_id"] = execution.id
        return await export(organization_id)

    monkeypatch.setattr(backup, "export_content_archive_payload", complete_before_export)
    with asyncio.Runner() as loop:
        monkeypatch.setattr(
            migrate, "run_async", lambda function: lambda *a, **kw: loop.run(function(*a, **kw))
        )
        try:
            _memory, path = loop.run(_seed_and_backup(stores, org, tmp_path))
            (tmp_path / "old-volume").rename(tmp_path / "unavailable-old-volume")
            monkeypatch.setattr(
                settings, "validation_receipt_dir", str(tmp_path / "restored-volume")
            )
            active = "destination"
            result = CliRunner().invoke(
                migrate.app,
                [
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
                ],
            )
            assert result.exit_code == 0, result.output
            destination = stores["destination"][1]
            child = subprocess.run(  # noqa: S603 - fixed fixture code, JSON on stdin
                [sys.executable, "-B", "-c", _RECOVER_PROCESS],
                input=json.dumps(
                    {
                        **retained,
                        "org": org,
                        "url": destination._url,
                        "namespace": destination.namespace,
                        "database": destination.database,
                        "usage": completed_result().usage.model_dump(mode="json"),
                    }
                ),
                text=True,
                capture_output=True,
                check=False,
                env={
                    **os.environ,
                    "SIBYL_VALIDATION_RECEIPT_DIR": str(tmp_path / "restored-volume"),
                },
            )
            assert child.returncode == 0, child.stderr
            assert "FRESH_HOST_RECOVERY_PASS" in child.stdout
            assert not list((tmp_path / "restored-volume").glob("*.receipt"))
        finally:
            for close in closes:
                loop.run(close())


async def test_scoped_capture_ignores_foreign_journal_files(history, monkeypatch):
    from cryptography.fernet import Fernet

    from sibyl_core.tasks._evidence_json import canonical

    _client, execution = history
    await retain_completed(execution, monkeypatch)
    foreign = canonical({"org": "foreign", "principal": "other"})
    validation_receipts.retain(foreign, Fernet.generate_key().decode(), {"private": "foreign"})
    archive = await content_archive.export_content_archive_payload("org")
    entries = archive["validation_receipts"]["executions"]
    assert [entry["execution_id"] for entry in entries] == [execution.id]


async def test_conflicting_destination_ciphertext_is_not_replaced(history, monkeypatch, tmp_path):
    _client, execution = history
    await retain_completed(execution, monkeypatch)
    archive = await content_archive.export_content_archive_payload("org")
    row = await execution.load()
    monkeypatch.setattr(settings, "validation_receipt_dir", str(tmp_path / "conflicting-volume"))
    # A separately encrypted receipt is not the exact immutable archived bytes.
    validation_receipts.retain(row["request_json"], row["recovery_key"], {"private": "conflict"})
    path = next((tmp_path / "conflicting-volume").glob("*.receipt"))
    before = path.read_bytes()
    restored = await content_archive.restore_content_archive_payload(archive, clean=True)
    assert not restored.success
    assert path.read_bytes() == before
    assert (await execution.load())["state"] == "running"


async def test_current_receipt_format_preserves_structured_row_rejection(history):
    client, _execution = history
    archive = await content_archive.export_content_archive_payload("org")
    before = await client.execute_query("SELECT * FROM memory_validation_executions;")
    del archive["tables"]["memory_validation_executions"][0]["request_json"]
    result = await content_archive.restore_content_archive_payload(archive, clean=True)
    assert not result.success
    assert result.rows_restored == 0
    assert result.errors == ["memory_validation_executions invalid archive row (ValueError)"]
    assert await client.execute_query("SELECT * FROM memory_validation_executions;") == before
