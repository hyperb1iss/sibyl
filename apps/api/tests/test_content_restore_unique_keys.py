"""A content restore that would collide on any UNIQUE index is refused before it writes.

The cases come from the indexes a freshly bootstrapped content store defines,
so an index added later joins automatically, and fails here until it has a
source row below.
"""

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from sibyl.persistence import content_archive
from sibyl_core.backends.surreal import SurrealContentClient, bootstrap_content_schema
from sibyl_core.services.source_archive_store import unique_indexes

# The fields each restored row replaces a destination row by. A destination row
# sharing all of them is the row being replaced, so an index over them cannot
# collide. Source captures replace the row found by organization and uuid;
# a ledger state is upserted at its own derived record key.
IDENTITY: dict[str, tuple[str, ...]] = {
    **{
        spec.name: (spec.target_identity_field,)
        for spec in content_archive._CONTENT_ARCHIVE_TABLE_SPECS
    },
    "raw_captures": ("organization_id", "uuid"),
    # A thread's identity is its organization, channel and thread: its record
    # key and thread_key are both the hash of the three.
    "net_threads": ("organization_id", "channel", "thread"),
    "source_states": (),
    "memory_derivations": ("organization_id", "target_kind", "target_id"),
}


def _source_rows(org: str) -> dict[str, Callable[[], dict]]:
    stamp = datetime(2026, 9, 10, 2, tzinfo=UTC)
    return {
        "api_idempotency_records": lambda: {
            "uuid": str(uuid4()),
            "organization_id": org,
            "principal_id": "owner",
            "idempotency_key": "key",
            "method": "POST",
            "path": "/fixture",
            "request_hash": "hash",
            "response_status_code": 200,
        },
        "backup_settings": lambda: {"uuid": str(uuid4()), "organization_id": org},
        "backups": lambda: {"uuid": str(uuid4()), "organization_id": org, "backup_id": "nightly"},
        "content_changefeed_cursors": lambda: {
            "uuid": str(uuid4()),
            "organization_id": org,
            "table_name": "raw_captures",
            "consumer_name": "fixture",
            "versionstamp": 7,
        },
        "crawl_sources": lambda: {
            "uuid": str(uuid4()),
            "organization_id": org,
            "url": "https://docs.example.test",
        },
        "crawled_documents": lambda: {
            "uuid": str(uuid4()),
            "organization_id": org,
            "source_id": "source-1",
            "url": "https://docs.example.test/page",
        },
        "document_chunks": lambda: {
            "uuid": str(uuid4()),
            "organization_id": org,
            "document_id": "document-1",
            "chunk_index": 0,
        },
        "eval_attempts": lambda: {
            "uuid": str(uuid4()),
            "organization_id": org,
            "experiment_id": "experiment",
            "attempt_id": "attempt",
            "assignment_sha256": "a" * 64,
            "assignment_json": "{}",
        },
        "net_threads": lambda: {
            "organization_id": org,
            "channel": "#reviews",
            "thread": "gh:acme/app#42",
            "first_message_at": stamp,
            "last_message_at": stamp,
        },
        "telemetry_rollups": lambda: {
            "uuid": str(uuid4()),
            "bucket_key": "hour:2026-09-10T02",
            "bucket_start": stamp,
        },
    }


def _discover() -> list[tuple[str, str, tuple[str, ...]]]:
    async def run():
        client = SurrealContentClient(url="memory://")
        try:
            await bootstrap_content_schema(client)
            return [
                (table, name, tuple(fields))
                for table in sorted(IDENTITY)
                for name, fields in await unique_indexes(client.execute_query, table)
            ]
        finally:
            await client.close()

    return asyncio.run(run())


def pytest_generate_tests(metafunc):
    if "unique_index" in metafunc.fixturenames:
        indexes = _discover()
        assert indexes, "no UNIQUE index discovered on the tables a content restore writes"
        metafunc.parametrize(
            "unique_index", indexes, ids=[f"{table}.{name}" for table, name, _ in indexes]
        )


async def _fingerprint(client) -> str:
    info = await client.execute_query("INFO FOR DB;")
    tables = sorted(name for name in info["tables"] if not name.startswith("schema_"))
    snapshot = ", ".join(
        f"`{table}`: (SELECT * FROM `{table}` ORDER BY id)"  # noqa: S608
        for table in tables
    )
    return await client.execute_query(f"RETURN crypto::sha256(type::string({{{snapshot}}}));")


async def _capture(client, org, identity):
    await client.execute_query(
        "CREATE raw_captures CONTENT $record;",
        record={
            "uuid": identity,
            "organization_id": org,
            "principal_id": "owner",
            "raw_content": identity,
        },
    )


@pytest.fixture
async def stores(monkeypatch):
    clients = [SurrealContentClient(url="memory://") for _ in range(2)]
    closes = [client.close for client in clients]
    try:
        for client in clients:
            await bootstrap_content_schema(client)
            monkeypatch.setattr(client, "close", AsyncMock())
        yield clients
    finally:
        for close in closes:
            await close()


async def test_merge_refuses_every_unique_index_collision(stores, monkeypatch, unique_index):
    table, name, fields = unique_index
    identity = IDENTITY[table]
    if identity and set(identity) <= set(fields):
        pytest.skip(f"{name} covers the row's identity: a match is the row being replaced")
    source, destination = stores
    org = str(uuid4())
    global_scope = table in content_archive._GLOBAL_CONTENT_ARCHIVE_TABLES
    # Captures land before auxiliary rows, so a late refusal would show here.
    for index in range(3):
        await _capture(source, org, f"incoming-{index}")
    if table in {"raw_captures", "source_states"}:
        row = (await source.execute_query(f"SELECT * OMIT id FROM {table} LIMIT 1;"))[0]  # noqa: S608
    else:
        factory = _source_rows(org).get(table)
        assert factory is not None, f"add a source row for {table} so {name} is covered"
        row = factory()
        created = await source.execute_query(f"CREATE {table} CONTENT $record;", record=row)
        row = {
            key: value
            for key, value in (created[0] if isinstance(created, list) else created).items()
            if key != "id"
        }
    # The same unique key under another identity, at another record key.
    colliding = dict(row)
    for column in identity:
        if column not in fields:
            colliding[column] = "other-" + str(uuid4())
    created = await destination.execute_query(
        f"CREATE {table} CONTENT $record;",
        record=colliding,
    )
    stored = created[0] if isinstance(created, list) else created
    if any(stored.get(column) != row.get(column) for column in fields):
        pytest.skip(f"{name} is computed from the row's identity, so it collides only with it")
    monkeypatch.setattr(content_archive, "build_surreal_content_client", lambda: source)
    payload = await content_archive.export_content_archive_payload(None if global_scope else org)
    monkeypatch.setattr(content_archive, "build_surreal_content_client", lambda: destination)
    before = await _fingerprint(destination)

    result = await content_archive.restore_content_archive_payload(
        payload, global_scope=global_scope
    )

    assert not result.success
    assert any(f"unique index {name}" in error for error in result.errors), result.errors
    assert await _fingerprint(destination) == before
