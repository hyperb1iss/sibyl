"""Nested objects inside arrays of objects, against a live SurrealDB server.

SurrealDB 3.x stored undeclared keys of an object inside a SCHEMAFULL table's
`array<object>` field; 4.0 refuses the whole write instead ("Found field
'metrics[58].avg', but no such field exists"). Each test bootstraps the content
schema in a namespace of its own and writes a record the way the product does,
so it fails on either server version the moment a nested shape stops fitting.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from sibyl.jobs.source_imports import SourceImportRun, _run_record
from sibyl.persistence.surreal.source_import_runs import SurrealSourceImportRunRepository
from sibyl.services import telemetry as telemetry_service
from sibyl_core.backends.surreal import SurrealContentClient, bootstrap_content_schema
from sibyl_core.backends.surreal.content_schema import (
    CONTENT_SCHEMA_CURRENT_VERSION,
    CONTENT_SCHEMA_NAME,
    _content_schema_migrations,
)
from sibyl_core.backends.surreal.index_engine import index_engine_of
from sibyl_core.backends.surreal.records import normalize_records
from sibyl_core.backends.surreal.schema_version import (
    apply_schema_migrations,
    get_schema_version,
    pin_schema_compatibility_level,
)
from sibyl_core.backends.surreal.url_schemes import is_embedded_surreal_url
from sibyl_core.models.sources import SourceSkippedRecord
from sibyl_core.observability import telemetry_registry

pytestmark = pytest.mark.skipif(
    os.environ.get("SIBYL_LIVE_SURREAL_TESTS") != "1",
    reason="live SurrealDB runtime smoke tests are disabled",
)

_ROLLUP_UPSERT = "UPSERT telemetry_rollups CONTENT $record WHERE bucket_key = $bucket_key;"


def _live_surreal_url() -> str:
    url = os.environ.get("SIBYL_SURREAL_URL", "")
    if not url or is_embedded_surreal_url(url):
        pytest.skip("live SurrealDB tests require SIBYL_SURREAL_URL to point at a server")
    return url


def _client(namespace: str) -> SurrealContentClient:
    return SurrealContentClient(
        url=_live_surreal_url(),
        namespace=namespace,
        database="content",
        username=os.environ.get("SIBYL_SURREAL_USERNAME", "root"),
        password=os.environ.get("SIBYL_SURREAL_PASSWORD", "root"),
    )


async def _drop_namespace(namespace: str) -> None:
    from surrealdb import AsyncSurreal

    client = AsyncSurreal(_live_surreal_url())
    try:
        await client.signin(
            {
                "username": os.environ.get("SIBYL_SURREAL_USERNAME", "root"),
                "password": os.environ.get("SIBYL_SURREAL_PASSWORD", "root"),
            }
        )
        await client.query(f"REMOVE NAMESPACE IF EXISTS {namespace};")
    finally:
        await client.close()


@asynccontextmanager
async def _content_store(*, bootstrap: bool = True) -> AsyncIterator[SurrealContentClient]:
    namespace = f"nested_fields_{uuid4().hex}"
    client = _client(namespace)
    try:
        if bootstrap:
            await bootstrap_content_schema(client)
        yield client
    finally:
        await client.close()
        await _drop_namespace(namespace)


def _runtime_snapshot() -> dict[str, object]:
    """What the running API would roll up: requests, queries and their labels."""
    registry = telemetry_registry()
    registry.record_api_request(
        method="get", route="/api/health/ready", status_code=200, duration_ms=12.5
    )
    registry.record_api_request(method="post", route="/api/search", status_code=500, duration_ms=88)
    registry.record_surreal_query(
        client="content",
        database="content",
        elapsed_ms=3.2,
        statement="select",
        status="ok",
        retry_count=0,
        query_hash="abc123",
    )
    return telemetry_service.runtime_summary()


@pytest.mark.asyncio
async def test_live_runtime_rollup_keeps_metric_event_and_trend_entries(monkeypatch) -> None:
    async with _content_store() as client:

        async def shared_client() -> SurrealContentClient:
            return client

        monkeypatch.setattr(telemetry_service, "get_shared_surreal_content_client", shared_client)
        monkeypatch.setattr(telemetry_service, "_last_persisted_bucket", None)
        snapshot = _runtime_snapshot()
        monkeypatch.setattr(telemetry_service, "runtime_summary", lambda **_: snapshot)

        bucket = telemetry_service._current_bucket()
        record = await telemetry_service.persist_runtime_rollup(bucket=bucket)

        assert record is not None, "the rollup write was refused"
        rows = normalize_records(
            await client.execute_query(
                "SELECT metrics, recent_events, trends FROM telemetry_rollups "
                "WHERE bucket_key = $bucket_key;",
                bucket_key=record["bucket_key"],
            )
        )
        assert len(rows) == 1
        histogram = next(
            metric
            for metric in rows[0]["metrics"]
            if metric.get("name") == "sibyl_api_request_duration_ms"
        )
        assert {"avg", "p95", "labels"} <= set(histogram)
        assert histogram["labels"]["route"] in {"/api/health/ready", "/api/search"}
        assert any(event["labels"].get("route") for event in rows[0]["recent_events"])
        assert rows[0]["trends"]
        assert "api_p95_ms" in rows[0]["trends"][0]


@pytest.mark.asyncio
async def test_live_source_import_keeps_skipped_and_error_entries() -> None:
    async with _content_store() as client:
        run = SourceImportRun(
            import_id=str(uuid4()),
            organization_id=str(uuid4()),
            principal_id=str(uuid4()),
            source_uri="file:///exports/notes.jsonl",
            adapter_name="jsonl",
            options={"target_memory_scope": "private"},
            policy_context={"actor": "test"},
            batch_size=50,
            promotion_preview_approved=False,
        )
        run.skipped_records.append(
            SourceSkippedRecord(
                adapter_record_id="line-7",
                source_uri="file:///exports/notes.jsonl#7",
                reason="duplicate_dedupe_key",
                metadata={"dedupe_key": "sha256:abc", "raw_memory_id": str(uuid4())},
            ).model_dump(mode="json")
        )
        run.errors.append(
            {
                "message": "adapter stopped",
                "type": "RuntimeError",
                "timestamp": datetime.now(UTC).isoformat(),
            }
        )

        await SurrealSourceImportRunRepository(client).save(
            _run_record(run), expected_revision=None
        )

        rows = normalize_records(
            await client.execute_query(
                "SELECT skipped_records, errors FROM source_imports WHERE uuid = $uuid;",
                uuid=run.import_id,
            )
        )
        assert rows[0]["skipped_records"][0]["metadata"]["dedupe_key"] == "sha256:abc"
        assert rows[0]["errors"][0]["type"] == "RuntimeError"


@pytest.mark.asyncio
async def test_live_schema_version_keeps_every_migration_entry() -> None:
    async with _content_store() as client:
        rows = normalize_records(
            await client.execute_query("SELECT migrations FROM schema_version:content;")
        )
        migrations = rows[0]["migrations"]
        assert [entry["version"] for entry in migrations] == list(
            range(1, CONTENT_SCHEMA_CURRENT_VERSION + 1)
        )
        assert {
            "version": 59,
            "name": "content_telemetry_rollup_flexible_entries",
        } in migrations


@pytest.mark.asyncio
async def test_live_migration_59_opens_rollup_entries_on_an_upgraded_database() -> None:
    async with _content_store(bootstrap=False) as client:
        url = _live_surreal_url()
        info = await pin_schema_compatibility_level(client.execute_query, client.execute_query)
        before = tuple(m for m in _content_schema_migrations(url=url) if m.version <= 58)
        await apply_schema_migrations(
            client.execute_query,
            before,
            name=CONTENT_SCHEMA_NAME,
            scope="content_schema_migration",
        )
        assert await get_schema_version(client.execute_query, name=CONTENT_SCHEMA_NAME) == 58

        snapshot = _runtime_snapshot()
        record = {
            "uuid": str(uuid4()),
            "bucket_key": "209901010000",
            "bucket_start": datetime(2099, 1, 1, tzinfo=UTC),
            "uptime_seconds": snapshot["uptime_seconds"],
            "summaries": snapshot["summaries"],
            "trends": snapshot["trends"],
            "metrics": snapshot["metrics"],
            "recent_events": snapshot["recent_events"],
        }
        if index_engine_of(info) is not None:
            # The failure this migration exists for: 4.0 refuses nested keys
            # inside the version 58 element definitions.
            with pytest.raises(Exception, match="no such field exists"):
                await client.execute_query(
                    _ROLLUP_UPSERT, record=record, bucket_key=record["bucket_key"]
                )

        await bootstrap_content_schema(client)

        assert await get_schema_version(client.execute_query, name=CONTENT_SCHEMA_NAME) >= 59
        await client.execute_query(_ROLLUP_UPSERT, record=record, bucket_key=record["bucket_key"])
        rows = normalize_records(
            await client.execute_query(
                "SELECT metrics FROM telemetry_rollups WHERE bucket_key = $bucket_key;",
                bucket_key=record["bucket_key"],
            )
        )
        assert any("avg" in metric for metric in rows[0]["metrics"])
