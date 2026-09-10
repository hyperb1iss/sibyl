"""Restoration retains forward operation history and restores absent history."""

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from sibyl.persistence import content_archive
from sibyl_core.backends.surreal import SurrealContentClient, bootstrap_content_schema
from tests.test_memory_eval_admission import eval_api as eval_api  # noqa: PLC0414


@pytest.mark.parametrize(
    "table", ["eval_attempts", "eval_consolidations", "api_idempotency_records"]
)
@pytest.mark.parametrize("clean", [False, True])
@pytest.mark.parametrize("global_scope", [False, True])
async def test_archive_retains_operation_history(table, clean, global_scope, monkeypatch):
    store = SurrealContentClient(url="memory://")
    close = store.close
    await bootstrap_content_schema(store)
    monkeypatch.setattr(store, "close", AsyncMock())
    monkeypatch.setattr(content_archive, "build_surreal_content_client", lambda: store)
    org, identity = str(uuid4()), str(uuid4())
    records = {
        "eval_attempts": {
            "experiment_id": "experiment",
            "attempt_id": "attempt",
            "assignment_sha256": "a" * 64,
            "assignment_json": "{}",
        },
        "eval_consolidations": {
            "principal_id": "owner",
            "request_sha256": "b" * 64,
            "result_kind": "abstained",
        },
        "api_idempotency_records": {
            "principal_id": "owner",
            "idempotency_key": "key",
            "method": "POST",
            "path": "/fixture",
            "request_hash": "hash",
            "response_status_code": 200,
            "response_body": {"timestamp_text": "2026-09-10T02:47:11.572211763Z"},
        },
    }
    select = f"SELECT * FROM {table};"  # noqa: S608
    try:
        empty = await content_archive.export_content_archive_payload(None if global_scope else org)
        await store.execute_query(
            f"CREATE {table} CONTENT $record;",
            record={"uuid": identity, "organization_id": org, **records[table]},
        )
        await store.execute_query(
            f"UPDATE {table} SET created_at = <datetime>'2026-09-10T02:47:11.572211763Z';"  # noqa: S608
        )
        before = await store.execute_query(select)
        # An archive predating completion cannot erase the permanent operation.
        result = await content_archive.restore_content_archive_payload(
            empty,
            clean=clean,
            global_scope=global_scope,
        )
        assert result.success, result.errors
        assert await store.execute_query(select) == before

        archived = await content_archive.export_content_archive_payload(
            None if global_scope else org
        )
        assert archived["tables"][table][0]["created_at"] == "2026-09-10T02:47:11.572211763Z"
        result = await content_archive.restore_content_archive_payload(
            archived,
            clean=clean,
            global_scope=global_scope,
        )
        assert result.success, result.errors
        assert await store.execute_query(select) == before

        # An older collision must not replace a newer terminal or admission field.
        mutation = {
            "eval_attempts": "admitted_at = <datetime>'2026-09-10T02:49:11.572211763Z'",
            "eval_consolidations": "promoted_entity_id = 'retained-publication'",
            "api_idempotency_records": "response_body = {current: true}",
        }[table]
        await store.execute_query(f"UPDATE {table} SET {mutation};")  # noqa: S608
        advanced = await store.execute_query(select)
        result = await content_archive.restore_content_archive_payload(
            archived,
            clean=clean,
            global_scope=global_scope,
        )
        assert not result.success
        assert await store.execute_query(select) == advanced

        current = await content_archive.export_content_archive_payload(
            None if global_scope else org
        )
        if table == "eval_attempts":
            assert current["tables"][table][0]["admitted_at"] == "2026-09-10T02:49:11.572211763Z"
        # Absent-row DR remains supported in a genuinely empty destination.
        destination = SurrealContentClient(url="memory://")
        await bootstrap_content_schema(destination)
        destination_close = destination.close
        monkeypatch.setattr(destination, "close", AsyncMock())
        monkeypatch.setattr(content_archive, "build_surreal_content_client", lambda: destination)
        try:
            result = await content_archive.restore_content_archive_payload(
                current,
                clean=clean,
                global_scope=global_scope,
            )
            assert result.success, result.errors
            restored = await destination.execute_query(select)
            assert [{k: v for k, v in row.items() if k != "id"} for row in restored] == [
                {k: v for k, v in row.items() if k != "id"} for row in advanced
            ]
        finally:
            await destination_close()
    finally:
        await close()


async def test_restore_keeps_terminal_consolidation_out_of_extraction(eval_api, monkeypatch):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from sibyl_core.services import eval_consolidation, eval_publication
    from tests.test_memory_eval_admission import _register

    api = eval_api
    assert (await _register(api)).status_code == 200
    assert (await api.client.post(api.path, json=api.body)).status_code == 200
    monkeypatch.setattr(api.store, "close", AsyncMock())
    monkeypatch.setattr(content_archive, "build_surreal_content_client", lambda: api.store)
    org = api.assignment.organization_id
    old = await content_archive.export_content_archive_payload(org)
    raw = await api.store.execute_query("SELECT * FROM raw_captures ORDER BY uuid;")
    operation = eval_publication.ConsolidationOperation(
        organization_id=org,
        principal_id="owner",
        experiment_id="experiment",
        experiment_revision="1",
        arm_id="raw",
        checkpoint=0,
        group_id="contrast",
        attempt_ids=("a", "b"),
        mechanism="check output",
        controller_policy_sha256="c" * 64,
        extractor_revision="frozen-v1",
    )
    await api.store.execute_query(
        "CREATE eval_consolidations CONTENT $record;",
        record={
            "uuid": operation.key,
            "organization_id": org,
            "principal_id": "owner",
            "request_sha256": operation.request_sha256,
            "result_kind": "abstained",
        },
    )
    result = await content_archive.restore_content_archive_payload(old, clean=True)
    assert result.success, result.errors
    assert await api.store.execute_query("SELECT * FROM raw_captures ORDER BY uuid;") == raw
    extractor = AsyncMock(side_effect=AssertionError("terminal operation reached extraction"))
    monkeypatch.setattr(eval_consolidation, "propose_admitted_procedure", extractor)
    retained = await eval_publication.consolidate_admitted_procedure(
        operation,
        trusted_issuer_id="oracle",
        trusted_public_key=Ed25519PrivateKey.generate().public_key(),
        model_override="unused",
    )
    assert retained.status == "abstained"
    extractor.assert_not_awaited()
