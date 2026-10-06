"""Exposure annotation pays one write on the request path and stamps later."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from sibyl_core.services.usage import MemoryUsageItemKind
from sibyl_core.tools import usage_exposure
from sibyl_core.tools.responses import SearchResult
from sibyl_core.tools.usage_exposure import (
    annotate_search_result_exposures,
    drain_pending_exposure_stamps,
)


class _RecordingContentClient:
    """Answers the usage statements and records every query it receives."""

    def __init__(self, *, fail_stamps: bool = False, embedded: bool = False) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.fail_stamps = fail_stamps
        self.is_embedded = embedded

    async def execute_query(self, query: str, **params: Any) -> Any:
        self.calls.append((query, params))
        statement = query.lstrip().split(None, 1)[0]
        if statement == "INSERT":
            return list(params["rows"])
        if self.fail_stamps:
            raise RuntimeError("content store unavailable")
        if statement == "RETURN":
            return [
                {
                    "item_kind": target["item_kind"],
                    "item_id": target["item_id"],
                    "retrieval_count": 1,
                    "citation_count": 0,
                    "misled_count": 0,
                    "last_recalled_at": None,
                    "last_used_at": None,
                }
                for target in params["targets"]
            ]
        if statement == "BEGIN":
            return [
                {
                    "uuid": stamp["item_id"],
                    "organization_id": params["organization_id"],
                    **stamp,
                }
                for stamp in params["stamps"]
            ]
        raise AssertionError(f"unexpected content query: {query}")

    @property
    def statements(self) -> list[str]:
        return [query.lstrip().split(None, 1)[0] for query, _ in self.calls]


def _raw_result(index: int) -> SearchResult:
    return SearchResult(
        id=f"raw_memory:raw-{index}",
        type="raw_memory",
        name=f"Raw {index}",
        content="",
        score=1.0,
        result_origin="raw_memory",
    )


@pytest.mark.asyncio
async def test_recall_pays_one_write_and_the_stamp_runs_after_the_response() -> None:
    content_client = _RecordingContentClient()
    results = [_raw_result(index) for index in range(5)]

    with (
        patch.object(
            usage_exposure,
            "get_shared_surreal_content_client",
            AsyncMock(return_value=content_client),
        ),
        patch.object(usage_exposure, "get_surreal_graph_client", AsyncMock()),
    ):
        summary = await annotate_search_result_exposures(
            results,
            organization_id="org-a",
            principal_id="user-a",
            project_id=None,
            source_surface="search",
        )
        assert content_client.statements == ["INSERT"], "the request path writes once"
        assert len(content_client.calls[0][1]["rows"]) == 5
        assert summary["stamped_count"] == 5
        assert summary["coverage_complete"] is True
        assert all(result.metadata["usage_exposure"]["status"] == "stamped" for result in results)

        assert await drain_pending_exposure_stamps() == 1

    assert content_client.statements == ["INSERT", "RETURN", "BEGIN"]
    aggregate_targets = content_client.calls[1][1]["targets"]
    assert [target["item_id"] for target in aggregate_targets] == [f"raw-{i}" for i in range(5)]
    stamp_query, stamp_params = content_client.calls[2]
    assert [stamp["item_id"] for stamp in stamp_params["stamps"]] == [f"raw-{i}" for i in range(5)]
    assert "revision" not in stamp_query
    assert "raw_captures" in stamp_query


@pytest.mark.asyncio
async def test_a_failed_stamp_never_fails_the_recall() -> None:
    content_client = _RecordingContentClient(fail_stamps=True)
    results = [_raw_result(index) for index in range(2)]

    with (
        patch.object(
            usage_exposure,
            "get_shared_surreal_content_client",
            AsyncMock(return_value=content_client),
        ),
        patch.object(usage_exposure, "get_surreal_graph_client", AsyncMock()),
    ):
        summary = await annotate_search_result_exposures(
            results,
            organization_id="org-a",
            principal_id="user-a",
            project_id=None,
            source_surface="search",
        )
        assert summary["stamped_count"] == 2
        assert summary["excluded_count"] == 0

        assert await drain_pending_exposure_stamps() == 1

    assert content_client.statements == ["INSERT", "RETURN"], "the stamp failed after the insert"
    assert all(result.metadata["usage_exposure"]["status"] == "stamped" for result in results)


@pytest.mark.asyncio
async def test_a_failed_event_insert_excludes_the_items_and_schedules_nothing() -> None:
    content_client = _RecordingContentClient()

    async def refuse(query: str, **params: Any) -> Any:
        raise RuntimeError("content store unavailable")

    content_client.execute_query = refuse  # type: ignore[method-assign]
    results = [_raw_result(0)]

    with (
        patch.object(
            usage_exposure,
            "get_shared_surreal_content_client",
            AsyncMock(return_value=content_client),
        ),
        patch.object(usage_exposure, "get_surreal_graph_client", AsyncMock()),
    ):
        summary = await annotate_search_result_exposures(
            results,
            organization_id="org-a",
            principal_id="user-a",
            project_id=None,
            source_surface="search",
        )
        assert await drain_pending_exposure_stamps() == 0

    assert summary["stamped_count"] == 0
    assert summary["exclusions"] == [
        {
            "response_id": "raw_memory:raw-0",
            "reason": "recording_failed",
            "detail": "RuntimeError",
        }
    ]
    assert results[0].metadata["usage_exposure"]["status"] == "excluded"


@pytest.mark.asyncio
async def test_graph_targets_share_the_single_event_insert() -> None:
    content_client = _RecordingContentClient()
    graph_client = AsyncMock()
    graph_client.execute_query = AsyncMock(
        side_effect=lambda query, **params: [
            {"uuid": stamp["item_id"], "group_id": params["organization_id"], **stamp}
            for stamp in params["stamps"]
        ]
    )
    results = [
        _raw_result(0),
        SearchResult(
            id="entity-1",
            type="decision",
            name="Decision",
            content="",
            score=1.0,
            result_origin="graph",
        ),
    ]

    with (
        patch.object(
            usage_exposure,
            "get_shared_surreal_content_client",
            AsyncMock(return_value=content_client),
        ),
        patch.object(
            usage_exposure,
            "get_surreal_graph_client",
            AsyncMock(return_value=graph_client),
        ),
    ):
        summary = await annotate_search_result_exposures(
            results,
            organization_id="org-a",
            principal_id="user-a",
            project_id=None,
            source_surface="search",
        )
        assert content_client.statements == ["INSERT"]
        kinds = [row["item_kind"] for row in content_client.calls[0][1]["rows"]]
        assert kinds == [
            MemoryUsageItemKind.RAW_CAPTURE.value,
            MemoryUsageItemKind.GRAPH_ENTITY.value,
        ]
        assert summary["stamped_count"] == 2
        assert await drain_pending_exposure_stamps() == 1

    assert content_client.statements == ["INSERT", "RETURN", "BEGIN"]
    graph_client.execute_query.assert_awaited_once()
    graph_query = graph_client.execute_query.await_args.args[0]
    assert "UPDATE entity SET" in graph_query
    assert "revision" not in graph_query


@pytest.mark.asyncio
async def test_an_embedded_store_stamps_inline_so_nothing_is_pending_at_exit() -> None:
    """A query left in flight on the embedded engine aborts the interpreter at exit."""
    content_client = _RecordingContentClient(embedded=True)
    results = [_raw_result(index) for index in range(3)]

    with (
        patch.object(
            usage_exposure,
            "get_shared_surreal_content_client",
            AsyncMock(return_value=content_client),
        ),
        patch.object(usage_exposure, "get_surreal_graph_client", AsyncMock()),
    ):
        summary = await annotate_search_result_exposures(
            results,
            organization_id="org-a",
            principal_id="user-a",
            project_id=None,
            source_surface="search",
        )
        assert content_client.statements == ["INSERT", "RETURN", "BEGIN"]
        assert await drain_pending_exposure_stamps() == 0

    assert summary["stamped_count"] == 3
