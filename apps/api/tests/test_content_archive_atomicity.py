"""Failed source-local restore cannot commit cleanup or earlier auxiliary writes."""

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from sibyl.persistence import content_archive
from sibyl_core.backends.surreal import SurrealContentClient, bootstrap_content_schema


@pytest.fixture
async def archive_stores(monkeypatch):
    source = SurrealContentClient(url="memory://")
    destination = SurrealContentClient(url="memory://")
    closes = (source.close, destination.close)
    try:
        for client in (source, destination):
            await bootstrap_content_schema(client)
            monkeypatch.setattr(client, "close", AsyncMock())
        yield source, destination
    finally:
        for close in closes:
            await close()


async def fingerprint(client):
    return await client.execute_query(
        "RETURN crypto::sha256(type::string({"
        "raw: (SELECT * FROM raw_captures ORDER BY id),"
        "states: (SELECT * FROM source_states ORDER BY id),"
        "derivations: (SELECT * FROM memory_derivations ORDER BY id),"
        "attempts: (SELECT * FROM eval_attempts ORDER BY id),"
        "cursors: (SELECT * FROM content_changefeed_cursors ORDER BY id) }));"
    )


async def raw(client, org, identity):
    await client.execute_query(
        "CREATE raw_captures CONTENT $record;",
        record={
            "uuid": identity,
            "organization_id": org,
            "principal_id": "owner",
            "raw_content": identity,
        },
    )


@pytest.mark.parametrize("clean", [False, True])
async def test_auxiliary_conflict_rolls_back_sources_cleanup_and_prior_writes(
    archive_stores, monkeypatch, clean
):
    source, destination = archive_stores
    org, foreign, collision = str(uuid4()), str(uuid4()), str(uuid4())
    await raw(source, org, "incoming")
    await raw(destination, org, "must-survive")
    await source.execute_query(
        "CREATE eval_attempts CONTENT $record;",
        record={
            "uuid": str(uuid4()),
            "organization_id": org,
            "experiment_id": "experiment",
            "attempt_id": "attempt",
            "assignment_sha256": "a" * 64,
            "assignment_json": "{}",
        },
    )
    for client, organization in ((source, org), (destination, foreign)):
        await client.execute_query(
            "CREATE content_changefeed_cursors CONTENT $record;",
            record={
                "uuid": collision,
                "organization_id": organization,
                "table_name": "raw_captures",
                "consumer_name": "fixture",
                "versionstamp": 7,
            },
        )
    monkeypatch.setattr(content_archive, "build_surreal_content_client", lambda: source)
    payload = await content_archive.export_content_archive_payload(org)
    monkeypatch.setattr(content_archive, "build_surreal_content_client", lambda: destination)
    before = await fingerprint(destination)
    result = await content_archive.restore_content_archive_payload(payload, clean=clean)
    assert not result.success
    assert any("another organization" in error for error in result.errors)
    assert result.rows_restored == 0
    assert await fingerprint(destination) == before


async def test_auxiliary_snapshot_change_denies_source_commit(archive_stores, monkeypatch):
    source, destination = archive_stores
    org, cursor = str(uuid4()), str(uuid4())
    await raw(source, org, "incoming")
    await raw(destination, org, "must-survive")
    await destination.execute_query(
        "CREATE content_changefeed_cursors CONTENT $record;",
        record={
            "uuid": cursor,
            "organization_id": org,
            "table_name": "raw_captures",
            "consumer_name": "fixture",
            "versionstamp": 7,
        },
    )
    monkeypatch.setattr(content_archive, "build_surreal_content_client", lambda: source)
    payload = await content_archive.export_content_archive_payload(org)
    monkeypatch.setattr(content_archive, "build_surreal_content_client", lambda: destination)
    execute = destination.execute_query
    changed = False
    preserved = None

    async def race(query, **parameters):
        nonlocal changed, preserved
        if (
            "BEGIN TRANSACTION" in query
            and "archive auxiliary destination changed" in query
            and not changed
        ):
            changed = True
            await execute(
                "UPDATE content_changefeed_cursors SET versionstamp=8 WHERE uuid=$uuid;",
                uuid=cursor,
            )
            preserved = await fingerprint(destination)
        return await execute(query, **parameters)

    monkeypatch.setattr(destination, "execute_query", race)
    result = await content_archive.restore_content_archive_payload(payload, clean=True)
    assert changed
    assert not result.success
    assert any("auxiliary destination changed" in error for error in result.errors)
    assert await fingerprint(destination) == preserved


async def test_invalid_auxiliary_payload_denies_before_clean(archive_stores, monkeypatch):
    source, destination = archive_stores
    org = str(uuid4())
    await raw(source, org, "incoming")
    await raw(destination, org, "must-survive")
    monkeypatch.setattr(content_archive, "build_surreal_content_client", lambda: source)
    payload = await content_archive.export_content_archive_payload(org)
    payload["tables"]["eval_attempts"] = [{"organization_id": org}]
    monkeypatch.setattr(content_archive, "build_surreal_content_client", lambda: destination)
    before = await fingerprint(destination)
    result = await content_archive.restore_content_archive_payload(payload, clean=True)
    assert not result.success
    assert any("missing" in error for error in result.errors)
    assert await fingerprint(destination) == before


@pytest.mark.parametrize("null_field", ["created_at", "captured_at"])
async def test_legacy_null_required_date_uses_import_default(
    archive_stores, monkeypatch, null_field
):
    _, destination = archive_stores
    org = str(uuid4())
    supplied = "2020-01-02T03:04:05Z"
    record = {
        "uuid": "legacy-null-date",
        "organization_id": org,
        "raw_content": "legacy evidence",
        "created_at": supplied,
        "captured_at": supplied,
        null_field: None,
    }
    monkeypatch.setattr(content_archive, "build_surreal_content_client", lambda: destination)
    payload = {"organization_id": org, "tables": {"raw_captures": [record]}}
    for _ in range(2):
        result = await content_archive.restore_content_archive_payload(payload)
        assert result.success, result.errors
        rows = content_archive._normalize_records(
            await destination.execute_query("SELECT * FROM raw_captures;")
        )
        assert len(rows) == 1
        assert rows[0][null_field] is not None
        preserved = "captured_at" if null_field == "created_at" else "created_at"
        assert rows[0][preserved].isoformat().startswith("2020-01-02T03:04:05")
        assert rows[0]["raw_content"] == "legacy evidence"
        assert rows[0]["derivation_required"] is True


@pytest.mark.parametrize(
    "table", ["eval_attempts", "eval_consolidations", "api_idempotency_records"]
)
async def test_legacy_operation_without_generated_date_repeats_without_rewrite(
    archive_stores, monkeypatch, table
):
    _, destination = archive_stores
    org = str(uuid4())
    record = {
        "uuid": str(uuid4()),
        "organization_id": org,
        "experiment_id": "experiment",
        "attempt_id": "attempt",
        "assignment_sha256": "a" * 64,
        "assignment_json": "{}",
    }
    if table == "eval_consolidations":
        record = {
            "uuid": record["uuid"],
            "organization_id": org,
            "principal_id": "owner",
            "request_sha256": "b" * 64,
            "result_kind": "abstained",
        }
    elif table == "api_idempotency_records":
        record = {
            "uuid": record["uuid"],
            "organization_id": org,
            "principal_id": "owner",
            "idempotency_key": "key",
            "method": "POST",
            "path": "/fixture",
            "request_hash": "hash",
            "response_status_code": 200,
        }
    payload = {"organization_id": org, "tables": {table: [record]}}
    monkeypatch.setattr(content_archive, "build_surreal_content_client", lambda: destination)
    first = await content_archive.restore_content_archive_payload(payload)
    assert first.success, first.errors
    before = await fingerprint(destination)
    repeated = await content_archive.restore_content_archive_payload(payload, clean=True)
    assert repeated.success, repeated.errors
    assert await fingerprint(destination) == before
    changed_field = {
        "eval_attempts": "assignment_json",
        "eval_consolidations": "request_sha256",
        "api_idempotency_records": "request_hash",
    }[table]
    record[changed_field] = "changed"
    conflicting = await content_archive.restore_content_archive_payload(payload)
    assert not conflicting.success
    assert any("retained operation history" in error for error in conflicting.errors)
    assert await fingerprint(destination) == before
