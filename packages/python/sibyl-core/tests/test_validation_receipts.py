"""Completed receipts survive content outages without new provider dispatch."""

import json
from unittest.mock import AsyncMock

import pytest
from cryptography.fernet import Fernet, InvalidToken

from sibyl_core.config import settings
from sibyl_core.services import validation_execution as execution_module
from sibyl_core.services import validation_receipts as receipts
from sibyl_core.services.validation_execution import ValidationExecution
from sibyl_core.services.validation_stages import run_validation_stage
from sibyl_core.tasks._evidence_json import canonical
from sibyl_core.tasks.memory_validation import run_memory_validation
from sibyl_core.tasks.procedure_review import review_digest
from tests.test_eval_publication import content_store as content_store
from tests.test_memory_validation import candidate as candidate
from tests.test_memory_validation import citations as citations
from tests.test_memory_validation import extractor
from tests.test_memory_validation import prepared as prepared
from tests.test_memory_validation import sources as sources


@pytest.fixture(autouse=True)
def private_journal(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "validation_receipt_dir", str(tmp_path / "receipts"))
    return tmp_path / "receipts"


def test_journal_is_encrypted_immutable_and_detects_torn_records(private_journal):
    request = canonical({"org": "org", "principal": "owner"})
    key = Fernet.generate_key().decode()
    receipts.ready()
    receipts.retain(request, key, {"private": "known completed evidence"})
    path = next(private_journal.glob("*.receipt"))
    assert b"known completed evidence" not in path.read_bytes()
    assert path.stat().st_mode & 0o077 == 0
    assert receipts.read(request, key) == {"private": "known completed evidence"}
    receipts.retain(request, key, {"private": "known completed evidence"})
    with pytest.raises(ValueError, match="differs"):
        receipts.retain(request, key, {"private": "different"})
    path.write_bytes(path.read_bytes()[:20])
    with pytest.raises(InvalidToken):
        receipts.read(request, key)


async def test_database_outage_then_new_execution_recovers_completed_receipt(
    content_store, prepared, monkeypatch, private_journal
):
    request = {"org": "org", "principal": "owner", "parent": "parent", "policy": "{}"}
    identity = review_digest(request)
    execution = ValidationExecution(identity, "org", "owner")
    original_query = execution_module._query
    result = await run_memory_validation(prepared, extractor({"findings": []}))

    async def completed_before_outage():
        monkeypatch.setattr(
            execution_module, "_query", AsyncMock(side_effect=OSError("database unavailable"))
        )
        return result

    args = dict(
        execution=execution,
        parent_id="parent",
        source_ids=[],
        request=request,
        policy="{}",
        check_current=AsyncMock(),
        run=AsyncMock(side_effect=completed_before_outage),
    )
    with pytest.raises(OSError) as caught:
        await run_validation_stage(**args)
    assert caught.value.extraction_usage == result.usage.model_dump(mode="json")
    assert len(list(private_journal.glob("*.receipt"))) == 1
    monkeypatch.setattr(execution_module, "_query", original_query)
    row = await execution.load()
    assert row["state"] == "running" and row.get("usage_json") is None
    args["execution"] = ValidationExecution(identity, "org", "owner")
    args["run"] = AsyncMock(side_effect=AssertionError("duplicate provider call"))
    resumed = await run_validation_stage(**args)
    assert resumed["usage"] == result.usage.model_dump(mode="json")
    assert resumed["status"] == "no_findings"
    args["run"].assert_not_awaited()
    assert not list(private_journal.glob("*.receipt"))


def test_receipt_survives_writer_process_exit(private_journal):
    import os
    import subprocess
    import sys

    request = canonical({"org": "org", "principal": "owner"})
    key = Fernet.generate_key().decode()
    code = (
        "import json,sys; from sibyl_core.services import validation_receipts as r; "
        "v=json.load(sys.stdin); r.retain(v['request'],v['key'],v['result'])"
    )
    value = {"request": request, "key": key, "result": {"usage": {"requests": 3}}}
    completed = subprocess.run(
        [sys.executable, "-c", code],
        input=json.dumps(value),
        text=True,
        capture_output=True,
        env={**os.environ, "SIBYL_VALIDATION_RECEIPT_DIR": str(private_journal)},
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert receipts.read(request, key) == value["result"]


async def test_journal_unavailable_before_dispatch_never_runs_provider(content_store, monkeypatch):
    monkeypatch.setattr(receipts, "ready", lambda: (_ for _ in ()).throw(OSError("read only")))
    request = {"org": "org", "principal": "owner"}
    execution = ValidationExecution(review_digest(request), "org", "owner")
    run = AsyncMock(side_effect=AssertionError("must not dispatch"))
    with pytest.raises(OSError, match="read only"):
        await run_validation_stage(
            execution=execution,
            parent_id="parent",
            source_ids=[],
            request=request,
            policy="{}",
            check_current=AsyncMock(),
            run=run,
        )
    assert await execution.load() is None
    run.assert_not_awaited()


async def test_journal_failure_after_return_keeps_database_result(
    content_store, prepared, monkeypatch
):
    request = {"org": "org", "principal": "owner"}
    execution = ValidationExecution(review_digest(request), "org", "owner")
    result = await run_memory_validation(prepared, extractor({"findings": []}))
    monkeypatch.setattr(
        receipts, "retain", lambda *args: (_ for _ in ()).throw(OSError("disk full"))
    )
    stored = await run_validation_stage(
        execution=execution,
        parent_id="parent",
        source_ids=[],
        request=request,
        policy="{}",
        check_current=AsyncMock(),
        run=AsyncMock(return_value=result),
    )
    assert stored["usage"] == result.usage.model_dump(mode="json")
    assert (await execution.load())["state"] == "returned"


@pytest.mark.parametrize("deny", ["purge", "request", "principal", "other_volume"])
async def test_retained_receipt_cannot_override_database_identity(
    content_store, prepared, monkeypatch, tmp_path, deny
):
    request = {"org": "org", "principal": "owner"}
    execution = ValidationExecution(review_digest(request), "org", "owner")
    await execution.begin(parent_id="parent", source_ids=[], policy="{}", request=request)
    result = await run_memory_validation(prepared, extractor({"findings": []}))
    row = await execution.load()
    receipts.retain(row["request_json"], row["recovery_key"], execution._result_value(result))
    if deny == "purge":
        await execution_module._query(
            "UPDATE memory_validation_executions SET purged=true, recovery_key=NONE WHERE uuid=$uuid;",
            uuid=execution.id,
        )
    elif deny == "request":
        await execution_module._query(
            "UPDATE memory_validation_executions SET request_json='{}' WHERE uuid=$uuid;",
            uuid=execution.id,
        )
    elif deny == "principal":
        execution = ValidationExecution(execution.id, "org", "someone-else")
    else:
        monkeypatch.setattr(settings, "validation_receipt_dir", str(tmp_path / "other-replica"))
    assert not await execution.recover_completed_receipt()
    rows = await execution_module._query("SELECT * FROM memory_validation_executions;")
    assert rows[0]["state"] == "running" and rows[0].get("result_json") is None


async def test_actual_source_purge_erases_pending_receipt_key(content_store, prepared):
    await execution_module._query(
        "CREATE raw_captures CONTENT {uuid:'source',organization_id:'org',raw_content:'evidence'};"
    )
    request = {"org": "org", "principal": "owner"}
    execution = ValidationExecution(review_digest(request), "org", "owner")
    await execution.begin(parent_id="source", source_ids=["source"], policy="{}", request=request)
    result = await run_memory_validation(prepared, extractor({"findings": []}))
    row = await execution.load()
    receipts.retain(row["request_json"], row["recovery_key"], execution._result_value(result))
    await execution_module._query("DELETE raw_captures WHERE uuid='source';")
    retained = await execution.load()
    assert retained["purged"] is True and retained.get("recovery_key") is None
    assert not await execution.recover_completed_receipt()
    assert retained.get("result_json") is None


async def test_repeated_cancel_waits_for_journal_thread(content_store, prepared, monkeypatch):
    import asyncio
    import threading

    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    original = receipts.retain

    def blocked(*args):
        entered.set()
        assert release.wait(10)
        original(*args)
        finished.set()

    monkeypatch.setattr(receipts, "retain", blocked)
    request = {"org": "org", "principal": "owner"}
    execution = ValidationExecution(review_digest(request), "org", "owner")
    result = await run_memory_validation(prepared, extractor({"findings": []}))
    task = asyncio.create_task(
        run_validation_stage(
            execution=execution,
            parent_id="parent",
            source_ids=[],
            request=request,
            policy="{}",
            check_current=AsyncMock(),
            run=AsyncMock(return_value=result),
        )
    )
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done() and not finished.is_set()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError) as caught:
        await task
    assert finished.is_set()
    assert caught.value.extraction_usage == result.usage.model_dump(mode="json")
    row = await execution.load()
    assert row["state"] == "recorded" and json.loads(row["usage_json"])["requests"] == 1


async def test_request_change_before_result_write_preserves_sole_receipt(
    content_store, prepared, monkeypatch, private_journal
):
    request = {"org": "org", "principal": "owner"}
    execution = ValidationExecution(review_digest(request), "org", "owner")
    await execution.begin(parent_id="parent", source_ids=[], policy="{}", request=request)
    result = await run_memory_validation(prepared, extractor({"findings": []}))
    await execution_module._query(
        "UPDATE memory_validation_executions SET request_json='{}' WHERE uuid=$uuid;",
        uuid=execution.id,
    )
    with pytest.raises(ValueError, match="outcome was not committed"):
        await execution.record_result(result)
    row = await execution.load()
    assert row["request_json"] == "{}" and row["state"] == "running"
    assert row.get("result_json") is None
    assert len(list(private_journal.glob("*.receipt"))) == 1


_RECOVERY_PROCESS = r"""
import asyncio,json,sys
from contextlib import asynccontextmanager
from pydantic import TypeAdapter
from sibyl_core.backends.surreal.content_client import SurrealContentClient
from sibyl_core.services import content_client,validation_execution as module
from sibyl_core.services.validation_execution import ValidationExecution,ValidationStageResult
from sibyl_core.services.validation_stages import run_validation_stage
from sibyl_core.tasks.procedure_review import review_digest
payload=json.load(sys.stdin)
async def main():
 client=SurrealContentClient(url=payload['url'],username='',password='',namespace=payload['namespace'],database=payload['database'])
 @asynccontextmanager
 async def session():yield client
 content_client.surreal_content_client=session
 request=payload['request']
 execution=ValidationExecution(review_digest(request),'org','owner')
 async def check():pass
 async def provider():
  if payload['mode']!='write':raise AssertionError('provider redispatched after restart')
  async def outage(*args,**kwargs):raise OSError('injected database result outage')
  module._query=outage
  return TypeAdapter(ValidationStageResult).validate_python(payload['result'])
 try:
  try:
   value=await run_validation_stage(execution=execution,parent_id='parent',source_ids=[],request=request,policy='{}',check_current=check,run=provider)
  except OSError as error:
   assert payload['mode']=='write'
   assert error.extraction_usage==payload['result']['usage']
   print('PROCESS_RECEIPT:'+json.dumps({'phase':'write','known_usage_retained':True}))
  else:
   assert payload['mode']=='read'
   assert value['usage']==payload['result']['usage']
   assert value['status']==payload['result']['status']
   print('PROCESS_RECEIPT:'+json.dumps({'phase':'read','recovered':True}))
 finally:await client.close()
asyncio.run(main())
"""


async def test_outage_then_two_process_stage_recovery(content_store, prepared, tmp_path):
    import asyncio
    import os
    import sys

    from pydantic import TypeAdapter

    from sibyl_core.services import content_client
    from sibyl_core.services.validation_execution import ValidationStageResult

    async with content_client.surreal_content_client() as client:
        if client._url.startswith("memory:"):
            pytest.skip("Requires the isolated native SDK database")
        connection = {
            "url": client._url,
            "namespace": client.namespace,
            "database": client.database,
        }
    result = await run_memory_validation(prepared, extractor({"findings": []}))
    payload = {
        **connection,
        "request": {"org": "org", "principal": "owner", "parent": "parent", "policy": "{}"},
        "result": TypeAdapter(ValidationStageResult).dump_python(result, mode="json"),
    }
    journal = tmp_path / "shared-journal"
    env = {**os.environ, "SIBYL_VALIDATION_RECEIPT_DIR": str(journal)}
    for mode in ("write", "read"):
        child = await asyncio.create_subprocess_exec(
            sys.executable,
            "-B",
            "-c",
            _RECOVERY_PROCESS,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        out, err = await child.communicate(json.dumps({**payload, "mode": mode}).encode())
        assert child.returncode == 0, err.decode()
        assert (
            json.loads(
                next(
                    line.removeprefix("PROCESS_RECEIPT:")
                    for line in out.decode().splitlines()
                    if line.startswith("PROCESS_RECEIPT:")
                )
            )["phase"]
            == mode
        )
        async with content_client.surreal_content_client() as client:
            rows = await client.execute_query("SELECT * FROM memory_validation_executions;")
        assert len(rows) == 1
        if mode == "write":
            assert rows[0]["state"] == "running" and rows[0].get("usage_json") is None
            assert len(list(journal.glob("*.receipt"))) == 1
        else:
            assert rows[0]["state"] == "returned"
            assert json.loads(rows[0]["usage_json"]) == payload["result"]["usage"]
            assert not list(journal.glob("*.receipt"))
