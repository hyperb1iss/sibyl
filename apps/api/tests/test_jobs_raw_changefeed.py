from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock

import pytest

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
    monkeypatch.setattr(raw_changefeed, "surreal_content_client", _client_context(client))
    monkeypatch.setattr(
        raw_changefeed.job_queue,
        "enqueue_raw_promotion",
        enqueue_raw_promotion,
    )

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


@pytest.mark.asyncio
async def test_poll_raw_capture_changefeed_preserves_cursor_when_enqueue_fails(
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

    with pytest.raises(RuntimeError, match="queue down"):
        await raw_changefeed.poll_raw_capture_changefeed({}, "org-1")

    assert "SINCE 3" in client.raw_queries[0][0]
    assert client.created_records == []
    assert client.updated_records == []


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
