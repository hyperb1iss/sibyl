"""Archive restore behaviour once a populated table has been converted to SCHEMAFULL."""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
import pytest_asyncio
from surrealdb import AsyncSurreal

from sibyl.persistence import content_archive
from sibyl.persistence.auth_archive import restore_auth_archive_payload
from sibyl.persistence.content_archive import restore_content_archive_payload
from sibyl.persistence.surreal import auth as surreal_auth, content as surreal_content
from sibyl_core.backends.surreal import (
    SurrealAuthClient,
    SurrealContentClient,
    bootstrap_auth_schema,
    bootstrap_content_schema,
)
from sibyl_core.backends.surreal.records import normalize_records


@pytest_asyncio.fixture
async def content_client():
    await surreal_content.close_shared_surreal_content_client()
    client = SurrealContentClient(url="memory://")
    await bootstrap_content_schema(client, reset=True)
    try:
        yield client
    finally:
        await surreal_content.close_shared_surreal_content_client()
        await client.close()


@pytest_asyncio.fixture
async def auth_client():
    await surreal_auth.close_shared_surreal_auth_client()
    client = SurrealAuthClient(url="memory://")
    await bootstrap_auth_schema(client, reset=True)
    try:
        yield client
    finally:
        await surreal_auth.close_shared_surreal_auth_client()
        await client.close()


@pytest.mark.skipif(
    os.environ.get("SIBYL_LIVE_SURREAL_TESTS") != "1",
    reason="undeclared-field rejection only reproduces on a real SurrealDB 3.x server",
)
@pytest.mark.asyncio
async def test_schemafull_conversion_blocks_writes_to_rows_carrying_a_drifted_field() -> None:
    """The cost the SCHEMAFULL repair sweep imposes, pinned.

    Converting a populated table revalidates the stored row as a whole, so an undeclared
    key left over from an older build blocks every later write to that row -- including
    an update that only sets declared fields.

    The SDK-embedded engine is lenient here and accepts all three writes, so this can
    only be pinned against a server.
    """
    url = os.environ.get("SIBYL_SURREAL_URL", "")
    if not url or url.startswith(("memory://", "surrealkv://", "rocksdb://", "file://")):
        pytest.skip("requires SIBYL_SURREAL_URL pointing at a SurrealDB server")

    connection = AsyncSurreal(url)
    await connection.signin(
        {
            "username": os.environ.get("SIBYL_SURREAL_USERNAME", "root"),
            "password": os.environ.get("SIBYL_SURREAL_PASSWORD", "root"),
        }
    )
    await connection.use("drifted", "content")
    try:
        await connection.query("REMOVE TABLE IF EXISTS raw_captures;")
        await connection.query(
            "CREATE raw_captures CONTENT { uuid: 'cap-1', legacy_extra: 'from-an-older-build' };"
        )
        await connection.query("ALTER TABLE IF EXISTS raw_captures SCHEMAFULL;")
        await connection.query("DEFINE FIELD IF NOT EXISTS uuid ON raw_captures TYPE string;")

        errors: dict[str, str] = {}
        for label, statement in (
            ("create", "CREATE raw_captures CONTENT { uuid: 'cap-2', legacy_extra: 'x' };"),
            ("update_declared_only", "UPDATE raw_captures SET uuid = 'cap-1b';"),
            ("merge", "UPDATE raw_captures MERGE { legacy_extra: 'y' };"),
        ):
            try:
                await connection.query(statement)
            except Exception as exc:
                errors[label] = str(exc)
    finally:
        await connection.close()

    assert set(errors) == {"create", "update_declared_only", "merge"}
    for message in errors.values():
        assert "legacy_extra" in message


@pytest.mark.asyncio
async def test_content_restore_drops_undeclared_fields_and_reports_them(
    content_client: SurrealContentClient,
) -> None:
    """A drifted archive key must cost one field, not the whole row.

    Rejecting the row would lose strictly more on the disaster-recovery path than
    shedding a key the schema no longer declares, so the restore filters and reports.
    """
    capture_id = uuid4()
    payload = {
        "version": content_archive.CONTENT_ARCHIVE_VERSION,
        "organization_id": "org-123",
        "tables": {
            "raw_captures": [
                {
                    "id": str(capture_id),
                    "organization_id": "org-123",
                    "source_id": "source:docs:1",
                    "principal_id": "user-123",
                    "memory_scope": "private",
                    "scope_key": "user-123",
                    "title": "Capture",
                    "raw_content": "captured",
                    "entity_type": "note",
                    "tags": ["capture"],
                    "metadata": {"source": "manual"},
                    "provenance": {},
                    "capture_surface": "source_import",
                    "created_at": "2026-04-20T00:00:00+00:00",
                    "legacy_extra": "from-an-older-build",
                }
            ],
        },
    }

    with (
        patch.object(content_client, "close", AsyncMock()),
        patch(
            "sibyl.persistence.content_archive.build_surreal_content_client",
            return_value=content_client,
        ),
    ):
        result = await restore_content_archive_payload(payload)

    stored = await content_client.execute_query(
        "SELECT uuid, title FROM raw_captures WHERE uuid = $uuid;",
        uuid=str(capture_id),
    )

    assert result.success, result.errors
    assert result.rows_restored == 1
    assert result.dropped_fields == {"raw_captures": ["legacy_extra"]}
    assert normalize_records(stored) == [{"uuid": str(capture_id), "title": "Capture"}]


@pytest.mark.asyncio
async def test_auth_restore_drops_undeclared_fields_and_reports_them(
    auth_client: SurrealAuthClient,
) -> None:
    user_id = uuid4()
    payload = {
        "version": "1.0",
        "organization_id": "org-123",
        "tables": {
            "users": [
                {
                    "id": str(user_id),
                    "email": "drift@example.com",
                    "name": "Drift",
                    "created_at": "2026-04-20T00:00:00+00:00",
                    "updated_at": "2026-04-20T00:00:00+00:00",
                    "legacy_extra": "from-an-older-build",
                }
            ],
        },
    }

    with (
        patch.object(auth_client, "close", AsyncMock()),
        patch(
            "sibyl.persistence.auth_archive.build_surreal_auth_client",
            return_value=auth_client,
        ),
    ):
        result = await restore_auth_archive_payload(payload)

    stored = await auth_client.execute_query(
        "SELECT uuid, email FROM users WHERE uuid = $uuid;",
        uuid=str(user_id),
    )

    assert result.success, result.errors
    assert result.rows_restored == 1
    assert result.dropped_fields == {"users": ["legacy_extra"]}
    assert normalize_records(stored) == [{"uuid": str(user_id), "email": "drift@example.com"}]
