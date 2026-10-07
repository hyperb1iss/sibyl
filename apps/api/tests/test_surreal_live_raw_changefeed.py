"""The raw capture changefeed poller against a live SurrealDB 3.x server.

A throwaway namespace carries a ``raw_captures`` table with a changefeed, so
the poll's statement, the reply's shape and the cursor arithmetic are all
proven against the server rather than a fake.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from sibyl.jobs import raw_changefeed
from sibyl_core.backends.surreal import SurrealContentClient
from tests.test_surreal_live_runtime import (
    _drop_surreal_namespace,
    _live_surreal_url,
    _surreal_password,
    _surreal_username,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("SIBYL_LIVE_SURREAL_TESTS") != "1",
    reason="live SurrealDB runtime smoke tests are disabled",
)


@pytest.fixture
async def live_changefeed(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[SurrealContentClient]:
    namespace = f"raw_changefeed_live_{uuid4().hex}"
    client = SurrealContentClient(
        url=_live_surreal_url(),
        username=_surreal_username(),
        password=_surreal_password(),
        namespace=namespace,
        database="content",
    )
    try:
        await client.execute_query_batch(
            "DEFINE TABLE raw_captures SCHEMALESS CHANGEFEED 1h; "
            "DEFINE TABLE content_changefeed_cursors SCHEMALESS;"
        )

        @asynccontextmanager
        async def session():
            yield client

        monkeypatch.setattr(raw_changefeed, "surreal_content_client", session)
        yield client
    finally:
        await client.close()
        await _drop_surreal_namespace(namespace)


async def _capture(client: SurrealContentClient, org: str, uuid: str) -> None:
    await client.execute_query(
        "CREATE raw_captures CONTENT $record;",
        record={"uuid": uuid, "organization_id": org, "raw_content": f"captured {uuid}"},
    )


@pytest.mark.asyncio
async def test_live_a_poll_reads_versionstamped_changes_and_never_replays_its_cursor(
    live_changefeed: SurrealContentClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    org = str(uuid4())
    for index in range(3):
        await _capture(live_changefeed, org, f"raw-{index}")
    enqueue = AsyncMock(return_value="raw_promotion:queued")
    monkeypatch.setattr(raw_changefeed.job_queue, "enqueue_raw_promotion", enqueue)
    monkeypatch.setattr("sibyl.api.pubsub.publish_event", AsyncMock())

    first = await raw_changefeed.poll_raw_capture_changefeed({}, org, limit=100)

    assert first["status"] == "queued"
    assert first["rows_seen"] >= 1
    assert first["next_versionstamp"] > 0
    assert sorted(first["changed_raw_memory_ids"]) == ["raw-0", "raw-1", "raw-2"]
    enqueue.assert_awaited_once()

    # SINCE is inclusive on the server: the change at the cursor comes back,
    # and a second poll must neither count it nor enqueue it again.
    second = await raw_changefeed.poll_raw_capture_changefeed({}, org, limit=100)

    assert (second["status"], second["rows_seen"]) == ("idle", 0)
    assert second["previous_versionstamp"] == first["next_versionstamp"]
    assert second["next_versionstamp"] == first["next_versionstamp"]
    enqueue.assert_awaited_once()

    # The tick finds nobody behind the feed, then exactly the organization
    # that wrote.
    quiet = await raw_changefeed.poll_all_raw_capture_changefeeds({}, limit=100)
    assert quiet == {"status": "ok", "organizations": 0, "results": []}

    await _capture(live_changefeed, org, "raw-3")
    moved = await raw_changefeed.poll_all_raw_capture_changefeeds({}, limit=100)

    assert moved["organizations"] == 1
    assert moved["results"][0]["changed_raw_memory_ids"] == ["raw-3"]
    assert moved["results"][0]["next_versionstamp"] > first["next_versionstamp"]
    assert enqueue.await_count == 2
