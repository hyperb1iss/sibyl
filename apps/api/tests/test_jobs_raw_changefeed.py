from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock

import pytest

from sibyl.api.event_types import WSEvent
from sibyl.jobs import raw_changefeed


class FakeChangefeedClient:
    def __init__(
        self,
        *,
        cursor_rows: list[dict[str, object]] | None = None,
        change_rows: list[dict[str, object]] | None = None,
        organization_rows: list[dict[str, object]] | None = None,
        update_returns_row: bool = False,
    ) -> None:
        self.cursor_rows = cursor_rows or []
        self.change_rows = change_rows or []
        self.organization_rows = organization_rows or []
        self.update_returns_row = update_returns_row
        self.raw_queries: list[tuple[str, dict[str, object]]] = []
        self.queries: list[tuple[str, dict[str, object]]] = []
        self.created_records: list[dict[str, object]] = []
        self.updated_records: list[dict[str, object]] = []

    async def execute_query_raw(self, query: str, **params: object) -> object:
        self.raw_queries.append((query, dict(params)))
        return list(self.change_rows)

    async def execute_query(self, query: str, **params: object) -> object:
        self.queries.append((query, dict(params)))
        stripped = query.strip()
        if stripped.startswith("SELECT versionstamp FROM content_changefeed_cursors"):
            return list(self.cursor_rows)
        if stripped.startswith("SELECT organization_id FROM raw_captures"):
            return list(self.organization_rows)
        if stripped.startswith("UPDATE content_changefeed_cursors"):
            self.updated_records.append(dict(params))
            return [{"uuid": "cursor-existing"}] if self.update_returns_row else []
        if stripped.startswith("CREATE content_changefeed_cursors"):
            record = dict(params["record"])  # type: ignore[index]
            self.created_records.append(record)
            return [record]
        raise AssertionError(f"unexpected query: {query}")


def _client_context(client: FakeChangefeedClient):
    @asynccontextmanager
    async def context():
        yield client

    return context


@pytest.mark.asyncio
async def test_poll_raw_capture_changefeed_queues_changes_and_saves_cursor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeChangefeedClient(
        change_rows=[
            {
                "versionstamp": 7,
                "changes": [
                    {
                        "update": {
                            "uuid": "raw-a",
                            "organization_id": "org-1",
                        }
                    },
                    {
                        "update": {
                            "uuid": "raw-other",
                            "organization_id": "org-2",
                        }
                    },
                ],
            },
            {
                "versionstamp": 9,
                "changes": [
                    {
                        "update": {
                            "uuid": "raw-b",
                            "organization_id": "org-1",
                        }
                    }
                ],
            },
        ]
    )
    enqueue_raw_promotion = AsyncMock(return_value="raw_promotion:queued")
    publish_event = AsyncMock()
    monkeypatch.setattr(raw_changefeed, "surreal_content_client", _client_context(client))
    monkeypatch.setattr(
        raw_changefeed.job_queue,
        "enqueue_raw_promotion",
        enqueue_raw_promotion,
    )
    monkeypatch.setattr("sibyl.api.pubsub.publish_event", publish_event)

    result = await raw_changefeed.poll_raw_capture_changefeed(
        {},
        "org-1",
        limit=25,
    )

    assert result["status"] == "queued"
    assert result["changed_raw_memory_ids"] == ["raw-a", "raw-b"]
    assert result["previous_versionstamp"] == 0
    assert result["next_versionstamp"] == 9
    assert "SINCE 0" in client.raw_queries[0][0]
    assert client.raw_queries[0][1] == {"limit": 25}
    enqueue_raw_promotion.assert_awaited_once_with(
        "org-1",
        raw_memory_ids=["raw-a", "raw-b"],
        limit=2,
    )
    assert client.created_records[0]["versionstamp"] == 9
    assert client.created_records[0]["metadata"] == {
        "rows_seen": 2,
        "raw_memory_count": 2,
        "promotion_job_id": "raw_promotion:queued",
    }
    publish_event.assert_awaited_once_with(
        WSEvent.RAW_CAPTURE_CHANGED,
        {
            "organization_id": "org-1",
            "raw_memory_ids": ["raw-a", "raw-b"],
            "promotion_job_id": "raw_promotion:queued",
            "rows_seen": 2,
            "previous_versionstamp": 0,
            "next_versionstamp": 9,
        },
        org_id="org-1",
    )


@pytest.mark.asyncio
async def test_raw_capture_changefeed_broadcast_payload_uses_org_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publish_event = AsyncMock()
    monkeypatch.setattr("sibyl.api.pubsub.publish_event", publish_event)

    await raw_changefeed._safe_broadcast_raw_capture_changed(
        {
            "organization_id": "org-1",
            "changed_raw_memory_ids": ["raw-a", "raw-b"],
            "promotion_job_id": "raw_promotion:queued",
            "rows_seen": 2,
            "previous_versionstamp": 3,
            "next_versionstamp": 9,
        }
    )

    publish_event.assert_awaited_once_with(
        WSEvent.RAW_CAPTURE_CHANGED,
        {
            "organization_id": "org-1",
            "raw_memory_ids": ["raw-a", "raw-b"],
            "promotion_job_id": "raw_promotion:queued",
            "rows_seen": 2,
            "previous_versionstamp": 3,
            "next_versionstamp": 9,
        },
        org_id="org-1",
    )


@pytest.mark.asyncio
async def test_queue_raw_capture_changes_enqueues_promotion_and_broadcasts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    enqueue_raw_promotion = AsyncMock(return_value="raw_promotion:queued")
    publish_event = AsyncMock()
    monkeypatch.setattr(
        raw_changefeed.job_queue,
        "enqueue_raw_promotion",
        enqueue_raw_promotion,
    )
    monkeypatch.setattr("sibyl.api.pubsub.publish_event", publish_event)

    job_id = await raw_changefeed.queue_raw_capture_changes(
        "org-1",
        raw_memory_ids=["raw-a", "raw-b"],
        rows_seen=2,
    )

    assert job_id == "raw_promotion:queued"
    enqueue_raw_promotion.assert_awaited_once_with(
        "org-1",
        raw_memory_ids=["raw-a", "raw-b"],
        limit=2,
    )
    publish_event.assert_awaited_once_with(
        WSEvent.RAW_CAPTURE_CHANGED,
        {
            "organization_id": "org-1",
            "raw_memory_ids": ["raw-a", "raw-b"],
            "promotion_job_id": "raw_promotion:queued",
            "rows_seen": 2,
        },
        org_id="org-1",
    )


@pytest.mark.asyncio
async def test_poll_raw_capture_changefeed_advances_cursor_when_enqueue_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeChangefeedClient(
        cursor_rows=[{"versionstamp": 3}],
        change_rows=[
            {
                "versionstamp": 5,
                "changes": [
                    {
                        "update": {
                            "uuid": "raw-a",
                            "organization_id": "org-1",
                        }
                    }
                ],
            }
        ],
    )
    monkeypatch.setattr(raw_changefeed, "surreal_content_client", _client_context(client))
    monkeypatch.setattr(
        raw_changefeed.job_queue,
        "enqueue_raw_promotion",
        AsyncMock(side_effect=RuntimeError("queue down")),
    )

    result = await raw_changefeed.poll_raw_capture_changefeed({}, "org-1")

    # A dead queue used to strand the cursor, so every later poll replayed the
    # same oldest changes and the feed never moved.
    assert result["status"] == "enqueue_failed"
    assert result["enqueue_error"] == "RuntimeError"
    assert result["next_versionstamp"] == 5
    assert client.created_records != [] or client.updated_records != []
    saved = (client.created_records or client.updated_records)[0]
    assert saved["versionstamp"] == 5
    metadata = saved["metadata"]
    assert metadata["enqueue_error"] == "RuntimeError"
    assert metadata["enqueue_failed_raw_memory_ids"] == ["raw-a"]


@pytest.mark.asyncio
async def test_poll_raw_capture_changefeed_never_hauls_embeddings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    embedding = [0.125] * 1536
    client = FakeChangefeedClient(
        change_rows=[
            {
                "versionstamp": 11,
                "changes": [
                    {
                        "update": {
                            "uuid": "raw-a",
                            "organization_id": "org-1",
                            "content": "hello",
                            "embedding": embedding,
                            "title_embedding": embedding,
                            "nested": {"vector": embedding, "keep": 1},
                        }
                    }
                ],
            }
        ],
    )
    monkeypatch.setattr(raw_changefeed, "surreal_content_client", _client_context(client))
    enqueue = AsyncMock(return_value="job-1")
    monkeypatch.setattr(raw_changefeed.job_queue, "enqueue_raw_promotion", enqueue)
    monkeypatch.setattr(raw_changefeed, "publish_raw_capture_changed", AsyncMock())

    result = await raw_changefeed.poll_raw_capture_changefeed({}, "org-1")

    assert result["changed_raw_memory_ids"] == ["raw-a"]
    assert "0.125" not in repr(enqueue.await_args)
    assert "0.125" not in repr(result)

    # The vectors are gone at the point the rows enter the process, so nothing
    # downstream can hold or re-encode 1536 floats per change.
    rows = await raw_changefeed._show_raw_capture_changes(client, since=0, limit=100)
    payload = rows[0]["changes"][0]["update"]
    assert payload == {
        "uuid": "raw-a",
        "organization_id": "org-1",
        "content": "hello",
        "nested": {"keep": 1},
    }
    assert "0.125" not in repr(rows)


def test_strip_vector_fields_drops_vectors_and_keeps_everything_else() -> None:
    payload = {
        "uuid": "raw-a",
        "embedding": [0.1, 0.2],
        "Embeddings": [0.1],
        "summary_embedding": [0.3],
        "state_vector": [0.4],
        "changes": [{"current": {"vector": [0.5], "organization_id": "org-1"}}],
    }

    stripped = raw_changefeed.strip_vector_fields(payload)

    assert stripped == {
        "uuid": "raw-a",
        "changes": [{"current": {"organization_id": "org-1"}}],
    }


@pytest.mark.asyncio
async def test_poll_all_raw_capture_changefeeds_polls_each_raw_capture_org(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeChangefeedClient(
        organization_rows=[
            {"organization_id": "org-a"},
            {"organization_id": "org-b"},
        ]
    )
    poll_one = AsyncMock(
        side_effect=[
            {"organization_id": "org-a", "status": "idle"},
            {"organization_id": "org-b", "status": "queued"},
        ]
    )
    monkeypatch.setattr(raw_changefeed, "surreal_content_client", _client_context(client))
    monkeypatch.setattr(raw_changefeed, "poll_raw_capture_changefeed", poll_one)

    result = await raw_changefeed.poll_all_raw_capture_changefeeds(
        {"worker": "test"},
        limit=10,
        organization_limit=20,
    )

    assert result == {
        "status": "ok",
        "organizations": 2,
        "results": [
            {"organization_id": "org-a", "status": "idle"},
            {"organization_id": "org-b", "status": "queued"},
        ],
    }
    assert client.queries[0][1] == {"limit": 20}
    assert poll_one.await_args_list[0].args == ({"worker": "test"}, "org-a")
    assert poll_one.await_args_list[0].kwargs == {"limit": 10}
    assert poll_one.await_args_list[1].args == ({"worker": "test"}, "org-b")


def test_changefeed_parser_dedupes_current_raw_capture_payloads() -> None:
    rows: list[dict[str, Any]] = [
        {
            "changes": [
                {
                    "update": {
                        "id": "raw_captures:record-a",
                        "uuid": "raw-a",
                        "organization_id": "org-1",
                    }
                },
                {
                    "current": {
                        "id": "raw_captures:record-a",
                        "uuid": "raw-a",
                        "organization_id": "org-1",
                    }
                },
                {
                    "delete": {
                        "id": "raw_captures:record-deleted",
                        "organization_id": "org-1",
                    }
                },
            ]
        }
    ]

    refs = raw_changefeed._raw_capture_refs_for_org(rows, organization_id="org-1")

    assert refs == [raw_changefeed.RawCaptureChangeRef("raw-a", "org-1")]
