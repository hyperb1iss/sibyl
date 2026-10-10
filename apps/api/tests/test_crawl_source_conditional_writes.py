"""Startup recovery and manual syncs write crawl sources conditionally.

They read a source, ask the job broker whether a crawl still owns it, and only
then write. A crawl on another process can start or end in that window, so
the reset matches only a source still in progress under the job id that was
read, and a sync of a settled source writes its counts and nothing else.

The embedded engine runs every case; with SIBYL_LIVE_SURREAL_TESTS=1 and a
server SIBYL_SURREAL_URL the same cases run against a server.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from sibyl.persistence.content_common import CrawlSourceRecord
from sibyl.persistence.surreal import content as surreal_content
from sibyl_core.backends.surreal import SurrealContentClient, bootstrap_content_schema
from sibyl_core.backends.surreal.url_schemes import is_embedded_surreal_url
from sibyl_core.models import CrawlStatus

CREDENTIALS = {
    "username": os.environ.get("SIBYL_SURREAL_USERNAME", "root"),
    "password": os.environ.get("SIBYL_SURREAL_PASSWORD", "root"),
}


def _store_url(engine: str) -> str:
    if engine == "embedded":
        return "memory://"
    if os.environ.get("SIBYL_LIVE_SURREAL_TESTS") != "1":
        pytest.skip("live SurrealDB tests are disabled")
    url = os.environ.get("SIBYL_SURREAL_URL", "")
    if not url or is_embedded_surreal_url(url):
        pytest.skip("live SurrealDB tests require SIBYL_SURREAL_URL to point at a server")
    return url


@pytest.fixture(params=["embedded", "live"])
async def content_store(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[SurrealContentClient]:
    url = _store_url(request.param)
    namespace = f"verify_crawl_writes_{uuid4().hex}"
    client = SurrealContentClient(url=url, namespace=namespace, **CREDENTIALS)
    await bootstrap_content_schema(client)

    @asynccontextmanager
    async def scope() -> AsyncIterator[SurrealContentClient]:
        yield client

    monkeypatch.setattr(surreal_content, "surreal_content_client", scope)
    try:
        yield client
    finally:
        if request.param == "live":
            with suppress(Exception):
                await client.execute_query(f"REMOVE NAMESPACE IF EXISTS {namespace};")
        await client.close()


async def _source(**fields: object) -> CrawlSourceRecord:
    source = CrawlSourceRecord(
        organization_id=uuid4(), name="Docs", url=f"https://{uuid4().hex}.example.com"
    )
    for name, value in fields.items():
        setattr(source, name, value)
    return await surreal_content.save_crawl_source_record(None, source=source)


async def _stored(source_id) -> CrawlSourceRecord:
    stored = await surreal_content.get_crawl_source_by_id(None, source_id=source_id)
    assert stored is not None
    return stored


async def _reset(source: CrawlSourceRecord, **overrides: object) -> CrawlSourceRecord | None:
    arguments: dict = {
        "source_id": source.id,
        "expected_job_id": source.current_job_id,
        "crawl_status": CrawlStatus.COMPLETED,
        "document_count": 4,
        "chunk_count": 12,
        "crawled_at": datetime(2026, 10, 10, 12, 0, tzinfo=UTC).replace(tzinfo=None),
    }
    arguments.update(overrides)
    return await surreal_content.reset_stuck_crawl_source(None, **arguments)


async def test_a_source_still_stuck_under_the_job_read_is_reset(
    content_store: SurrealContentClient,
) -> None:
    source = await _source(crawl_status=CrawlStatus.IN_PROGRESS, current_job_id="crawl:old")

    reset = await _reset(source)

    assert reset is not None
    stored = await _stored(source.id)
    assert stored.crawl_status == CrawlStatus.COMPLETED
    assert stored.current_job_id is None
    assert (stored.document_count, stored.chunk_count) == (4, 12)
    assert stored.last_crawled_at is not None


async def test_a_source_without_a_recorded_job_is_reset(
    content_store: SurrealContentClient,
) -> None:
    source = await _source(crawl_status=CrawlStatus.IN_PROGRESS, current_job_id=None)

    reset = await _reset(source, crawl_status=CrawlStatus.PENDING, document_count=0, chunk_count=0)

    assert reset is not None
    assert (await _stored(source.id)).crawl_status == CrawlStatus.PENDING


async def test_a_crawl_that_claimed_the_source_since_keeps_it(
    content_store: SurrealContentClient,
) -> None:
    read = await _source(crawl_status=CrawlStatus.IN_PROGRESS, current_job_id="crawl:old")
    # Another process starts a new crawl after the source was read.
    await surreal_content.save_crawl_source_record(
        None, source=await _with(read, current_job_id="crawl:new")
    )

    assert await _reset(read) is None

    stored = await _stored(read.id)
    assert stored.crawl_status == CrawlStatus.IN_PROGRESS
    assert stored.current_job_id == "crawl:new"


async def test_a_crawl_that_ended_since_keeps_its_result(
    content_store: SurrealContentClient,
) -> None:
    read = await _source(crawl_status=CrawlStatus.IN_PROGRESS, current_job_id="crawl:old")
    await surreal_content.save_crawl_source_record(
        None,
        source=await _with(
            read, crawl_status=CrawlStatus.FAILED, current_job_id=None, last_error="fetch failed"
        ),
    )

    assert await _reset(read) is None

    stored = await _stored(read.id)
    assert stored.crawl_status == CrawlStatus.FAILED
    assert stored.last_error == "fetch failed"


async def test_a_count_update_leaves_status_and_job_alone(
    content_store: SurrealContentClient,
) -> None:
    source = await _source(crawl_status=CrawlStatus.IN_PROGRESS, current_job_id="crawl:live")

    updated = await surreal_content.update_crawl_source_counts(
        None, source_id=source.id, document_count=7, chunk_count=21
    )

    assert updated is not None
    stored = await _stored(source.id)
    assert (stored.document_count, stored.chunk_count) == (7, 21)
    assert stored.crawl_status == CrawlStatus.IN_PROGRESS
    assert stored.current_job_id == "crawl:live"


async def _with(source: CrawlSourceRecord, **fields: object) -> CrawlSourceRecord:
    changed = await _stored(source.id)
    for name, value in fields.items():
        setattr(changed, name, value)
    return changed
