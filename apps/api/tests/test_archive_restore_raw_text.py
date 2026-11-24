"""A restore brings text back as text, whatever it reads like.

An archive stores every datetime as ISO text, beside text that merely reads
like one. Only fields the schema declares as datetimes become instants again;
a title, a chunk, a name or a setting that reads like a timestamp stays the
text it was. Before this, any such string came back as a datetime: SurrealDB
4 refused the row ("Couldn't coerce value ... found d'...'") and stopped the
restore partway, after a clean restore had already wiped the organization.
Runs on the embedded engine and, with SIBYL_LIVE_SURREAL_TESTS=1, on the
server SIBYL_SURREAL_URL names.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from datetime import datetime
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from sibyl.persistence import auth_archive, content_archive
from sibyl_core.backends.surreal import (
    SurrealAuthClient,
    SurrealContentClient,
    bootstrap_auth_schema,
    bootstrap_content_schema,
)
from sibyl_core.backends.surreal.schema_invariants import fetch_restore_fields
from sibyl_core.backends.surreal.url_schemes import is_embedded_surreal_url

LOOKALIKES = ("2026-11-05T10:00:00Z", "2026-11-05T10:00", "2026-11-05T10:00:00.123456789+02:00")
TEXT = LOOKALIKES[0]


def engine_url(engine: str) -> str:
    if engine == "embedded":
        return "memory://"
    if os.environ.get("SIBYL_LIVE_SURREAL_TESTS") != "1":
        pytest.skip("live SurrealDB tests are disabled")
    url = os.environ.get("SIBYL_SURREAL_URL", "")
    if not url or is_embedded_surreal_url(url):
        pytest.skip("live SurrealDB tests require SIBYL_SURREAL_URL to point at a server")
    return url


def _credentials(url: str) -> dict[str, str]:
    if is_embedded_surreal_url(url):
        return {}
    return {
        "username": os.environ.get("SIBYL_SURREAL_USERNAME", "root"),
        "password": os.environ.get("SIBYL_SURREAL_PASSWORD", "root"),
    }


@pytest.fixture(params=["embedded", "live"])
def url(request: pytest.FixtureRequest) -> str:
    return engine_url(request.param)


async def _stores(url: str, kind: type, bootstrap) -> AsyncIterator[list]:
    stores = [
        kind(url=url, namespace=f"verify_raw_text_{uuid4().hex}", **_credentials(url))
        for _ in range(2)
    ]
    closes = [store.close for store in stores]
    try:
        for store in stores:
            await bootstrap(store)
            store.close = AsyncMock()
        yield stores
    finally:
        for store, close in zip(stores, closes, strict=True):
            if not is_embedded_surreal_url(url):
                await store.execute_query(f"REMOVE NAMESPACE IF EXISTS {store.namespace};")
            await close()


@pytest.fixture
async def content_stores(url: str) -> AsyncIterator[list[SurrealContentClient]]:
    async for stores in _stores(url, SurrealContentClient, bootstrap_content_schema):
        yield stores


@pytest.fixture
async def auth_stores(url: str) -> AsyncIterator[list[SurrealAuthClient]]:
    async for stores in _stores(url, SurrealAuthClient, bootstrap_auth_schema):
        yield stores


def _content_rows(org: str) -> dict[str, dict[str, object]]:
    first, second, third = LOOKALIKES
    return {
        "crawl_sources": {"name": first, "url": "https://docs.example.test"},
        "crawled_documents": {
            "source_id": "source-1",
            "url": "https://docs.example.test/page",
            "title": second,
            "content": third,
        },
        "document_chunks": {"document_id": "document-1", "chunk_index": 0, "content": first},
        "content_changefeed_cursors": {
            "table_name": "raw_captures",
            "consumer_name": second,
            "versionstamp": 7,
            "metadata": {"note": third, "nested": [first]},
        },
        "backups": {"backup_id": first},
    }


async def _text_fields(store, table: str, row: dict[str, object], org: str) -> list[dict]:
    rows = await store.execute_query(
        f"SELECT {', '.join(row)} FROM {table} WHERE organization_id = $org;",  # noqa: S608
        org=org,
    )
    return rows if isinstance(rows, list) else []


@pytest.mark.parametrize("clean", [False, True], ids=["merge", "clean"])
async def test_content_text_that_reads_like_a_timestamp_restores_as_text(
    content_stores: list[SurrealContentClient],
    monkeypatch: pytest.MonkeyPatch,
    clean: bool,
) -> None:
    source, fresh = content_stores
    org = str(uuid4())
    rows = _content_rows(org)
    for table, row in rows.items():
        await source.execute_query(
            f"CREATE {table} CONTENT $record;",
            record={"uuid": str(uuid4()), "organization_id": org, **row},
        )
    monkeypatch.setattr(content_archive, "build_surreal_content_client", lambda: source)
    archive = json.loads(json.dumps(await content_archive.export_content_archive_payload(org)))

    # A merge lands in an empty store; a clean restore wipes and refills the source.
    destination = source if clean else fresh
    monkeypatch.setattr(content_archive, "build_surreal_content_client", lambda: destination)
    result = await content_archive.restore_content_archive_payload(archive, clean=clean)

    assert result.success, result.errors
    for table, row in rows.items():
        assert await _text_fields(destination, table, row, org) == [row], table


@pytest.mark.parametrize("clean", [False, True], ids=["merge", "clean"])
async def test_auth_text_that_reads_like_a_timestamp_restores_as_text(
    auth_stores: list[SurrealAuthClient],
    monkeypatch: pytest.MonkeyPatch,
    clean: bool,
) -> None:
    source, fresh = auth_stores
    org, user = str(uuid4()), str(uuid4())
    first, second, third = LOOKALIKES
    rows = {
        "users": ({"uuid": user, "email": f"{user}@example.test", "name": first}, "uuid", user),
        "organizations": ({"uuid": org, "name": second, "slug": f"o-{org[:8]}"}, "uuid", org),
        "organization_members": (
            {"uuid": str(uuid4()), "organization_id": org, "user_id": user, "role": "owner"},
            "organization_id",
            org,
        ),
        "teams": (
            {"uuid": str(uuid4()), "organization_id": org, "name": third, "slug": "t"},
            "organization_id",
            org,
        ),
    }
    for table, (row, _, _) in rows.items():
        await source.execute_query(f"CREATE {table} CONTENT $record;", record=row)
    monkeypatch.setattr(auth_archive, "build_surreal_auth_client", lambda: source)
    archive = json.loads(json.dumps(await auth_archive.export_auth_archive_payload(org)))

    destination = source if clean else fresh
    monkeypatch.setattr(auth_archive, "build_surreal_auth_client", lambda: destination)
    result = await auth_archive.restore_auth_archive_payload(archive, clean=clean)

    assert result.success, result.errors
    for table, (row, field, value) in rows.items():
        text = {key: item for key, item in row.items() if item in LOOKALIKES}
        if not text:
            continue
        restored = await destination.execute_query(
            f"SELECT {', '.join(text)} FROM {table} WHERE {field} = $value;",  # noqa: S608
            value=value,
        )
        assert restored == [text], table


async def _every_field_reads_like_a_timestamp(
    client, table: str
) -> tuple[dict, frozenset, frozenset]:
    declared, datetimes = await fetch_restore_fields(client.execute_query, table)
    return dict.fromkeys(declared, TEXT), declared, datetimes


def _assert_only_datetimes_converted(
    table: str, record: dict, archived: dict, datetimes: frozenset
) -> None:
    for name, value in archived.items():
        if name not in record:
            continue
        if name in datetimes:
            assert isinstance(record[name], datetime), (table, name)
        else:
            assert record[name] == value, (table, name, record[name])


async def test_every_archived_content_table_converts_only_its_datetime_fields() -> None:
    """Every table a content archive restores, read from the schema, so a new one is covered."""
    client = SurrealContentClient(url="memory://")
    try:
        await bootstrap_content_schema(client)
        for spec in content_archive._CONTENT_ARCHIVE_TABLE_SPECS:
            # Raw captures carry their own typed datetime paths; retained and
            # history tables keep archived values and convert in SQL.
            if spec.name == "raw_captures" or spec.name in (
                content_archive._RETAINED_OPERATION_TABLES | content_archive._DREAM_ARCHIVE_TABLES
            ):
                continue
            row, _, datetimes = await _every_field_reads_like_a_timestamp(client, spec.name)
            row[spec.target_identity_field] = str(uuid4())
            _, _, errors, _, restores = await content_archive._prepare_auxiliary_content_restore(
                client, {spec.name: [row]}, None
            )
            assert errors == [], (spec.name, errors)
            archived = content_archive._normalize_content_archive_restore_row(spec, row)
            if spec.name == "document_chunks":
                # The vector and its provenance stamps have their own restore.
                archived = {k: v for k, v in archived.items() if not k.startswith("embedding")}
            _assert_only_datetimes_converted(spec.name, restores[0].record, archived, datetimes)
    finally:
        await client.close()


async def test_every_archived_auth_table_converts_only_its_datetime_fields() -> None:
    client = SurrealAuthClient(url="memory://")
    try:
        await bootstrap_auth_schema(client)
        for table in auth_archive.AUTH_ARCHIVE_TABLES:
            row, declared, datetimes = await _every_field_reads_like_a_timestamp(client, table)
            row["uuid"] = str(uuid4())
            record, _, _ = auth_archive._auth_archive_record(row, declared, datetimes)
            _assert_only_datetimes_converted(table, record, row, datetimes)
    finally:
        await client.close()
