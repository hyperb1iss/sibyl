"""Malformed execution history returns a failed restore without changing rows."""

import json

import pytest

from sibyl.persistence import content_archive
from sibyl_core.tasks._evidence_json import canonical
from sibyl_core.tasks.procedure_review import review_digest
from tests.test_validation_execution_archive import history as history  # noqa: PLC0414


@pytest.mark.parametrize(
    "malformation", ["missing_request", "missing_bindings", "nonobject_binding"]
)
async def test_malformed_history_returns_failed_restore_without_writes(history, malformation):
    client, _execution = history
    archive = await content_archive.export_content_archive_payload("org")
    # Exercise the existing 2.2 owner independently of later receipt formats.
    archive["version"] = "2.2"
    archive.pop("validation_receipts", None)
    before = {
        table: await client.execute_query(f"SELECT * FROM {table};")  # noqa: S608
        for table in (
            "raw_captures",
            "source_states",
            "memory_validation_executions",
            "memory_validation_attempts",
        )
    }
    row = archive["tables"]["memory_validation_executions"][0]
    if malformation == "missing_request":
        del row["request_json"]
    else:
        request = json.loads(row["request_json"])
        if malformation == "missing_bindings":
            del request["source_bindings"]
        else:
            request["source_bindings"] = [None]
        row["request_json"] = canonical(request)
        row["uuid"] = row["id"] = row["request_sha256"] = review_digest(request)
    result = await content_archive.restore_content_archive_payload(archive, clean=True)
    assert not result.success
    assert result.rows_restored == 0
    assert result.errors == ["memory_validation_executions invalid archive row (ValueError)"]
    for table, rows in before.items():
        assert await client.execute_query(f"SELECT * FROM {table};") == rows  # noqa: S608
