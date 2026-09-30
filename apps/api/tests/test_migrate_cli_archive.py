"""Migration archive exports preserve tenant scope and JSON transport."""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from surrealdb import RecordID
from typer.testing import CliRunner

from sibyl.cli import migrate as migrate_cli
from sibyl.cli.migrate import _archive_json_default
from sibyl.persistence import auth_archive, content_archive
from sibyl_core.backends.surreal import (
    SurrealAuthClient,
    SurrealContentClient,
    bootstrap_auth_schema,
    bootstrap_content_schema,
)
from sibyl_core.memory_pipeline.observations import SourceKind
from sibyl_core.migrate.archive import (
    AUTH_FILENAME,
    CONTENT_FILENAME,
    load_archive,
    validate_archive,
)
from sibyl_core.migrate.source_integrity import validate_integrity_archive
from sibyl_core.services import content_client
from sibyl_core.services.surreal_content import remember_raw_memory


def test_archive_default_renders_record_ids_canonically() -> None:
    payload = {"link": RecordID("raw_captures", "abc123")}
    encoded = json.dumps(payload, default=_archive_json_default)
    assert json.loads(encoded) == {"link": "raw_captures:abc123"}


def test_archive_default_renders_datetimes_iso() -> None:
    moment = datetime(2026, 9, 3, 1, 2, 3, tzinfo=UTC)
    encoded = json.dumps({"at": moment}, default=_archive_json_default)
    assert json.loads(encoded) == {"at": "2026-09-03T01:02:03+00:00"}


def test_archive_default_falls_back_to_str() -> None:
    class Odd:
        def __str__(self) -> str:
            return "odd-value"

    encoded = json.dumps({"v": Odd()}, default=_archive_json_default)
    assert json.loads(encoded) == {"v": "odd-value"}


@pytest.mark.parametrize("include_auth", [False, True])
async def test_archive_export_keeps_selected_content_org_native(
    monkeypatch, tmp_path, include_auth
):
    url = os.getenv("SIBYL_ARCHIVE_PREREQUISITE_TEST_URL")
    if not url:
        pytest.skip("native archive prerequisite database is not configured")
    credentials = {
        "url": url,
        "username": os.getenv("SIBYL_ARCHIVE_PREREQUISITE_TEST_USERNAME", "root"),
        "password": os.getenv("SIBYL_ARCHIVE_PREREQUISITE_TEST_PASSWORD", "root"),
    }
    namespace = "archive_export_" + uuid4().hex

    def content_factory():
        return SurrealContentClient(**credentials, namespace=namespace)

    def auth_factory():
        return SurrealAuthClient(**credentials, namespace=namespace + "_auth")

    content = content_factory()
    auth = auth_factory()
    selected_org, foreign_org = str(uuid4()), str(uuid4())
    try:
        await bootstrap_content_schema(content)
        await bootstrap_auth_schema(auth)

        @asynccontextmanager
        async def session():
            yield content

        monkeypatch.setattr(content_client, "surreal_content_client", session)
        monkeypatch.setattr(content_archive, "build_surreal_content_client", content_factory)
        monkeypatch.setattr(auth_archive, "build_surreal_auth_client", auth_factory)
        memories = {}
        for organization in (selected_org, foreign_org):
            memories[organization] = await remember_raw_memory(
                organization_id=organization,
                principal_id="archive-owner",
                source_id="export-scope-control",
                raw_content="Synthetic archive evidence for " + organization,
                embedding_provider=None,
            )
            await content.execute_query(
                "CREATE content_changefeed_cursors CONTENT $record;",
                record={
                    "uuid": uuid4().hex,
                    "organization_id": organization,
                    "table_name": "raw_captures",
                    "consumer_name": "archive-test",
                    "versionstamp": 1,
                },
            )
        output = tmp_path / "scoped-content.tar.gz"
        args = ["export", "--org-id", selected_org, "--skip-graph", "-o", str(output)]
        if not include_auth:
            args.append("--skip-auth")
        result = await asyncio.to_thread(CliRunner().invoke, migrate_cli.app, args)
        assert result.exit_code == 0, result.output
        archive = load_archive(output)
        assert validate_archive(archive) == []
        assert archive.manifest.organization_id == selected_org
        assert (AUTH_FILENAME in archive.files) is include_auth
        payload = json.loads(archive.files[CONTENT_FILENAME])
        assert payload["organization_id"] == selected_org
        assert {row["uuid"] for row in payload["tables"]["raw_captures"]} == {
            memories[selected_org].id
        }
        assert all(
            row["organization_id"] == selected_org
            for rows in payload["tables"].values()
            for row in rows
        )
        sources, states, _ = validate_integrity_archive(
            payload["source_integrity"],
            kind=SourceKind.RAW_CAPTURE,
            organizations=[selected_org],
        )
        assert {row["uuid"] for row in sources} == {memories[selected_org].id}
        assert {row["source_id"] for row in states} == {memories[selected_org].id}
        assert {row["organization_id"] for row in states} == {selected_org}
        retained = await content.execute_query(
            "SELECT uuid, organization_id FROM raw_captures ORDER BY uuid;"
        )
        assert {row["uuid"] for row in retained} == {row.id for row in memories.values()}
    finally:
        await content.close()
        await auth.close()
