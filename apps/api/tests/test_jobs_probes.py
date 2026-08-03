"""The probe replay job, against rows shaped the way the graph actually returns them.

Every assertion here exists because a hand-shaped double let the job look
correct while addressing columns that do not exist. The rows below carry the
real column names: the model's metadata is the `attributes` column, the logical
id is `uuid`, and `normalize_records` moves the Surreal record id out of `id`
into `record_id`, so a job reading `id` gets nothing.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from sibyl.jobs.probes import replay_memory_probes, replay_memory_probes_all_orgs
from sibyl_core.projection.passages import passage_entity_id

_GROUP = "org-probe-replay"
_PARENT = "episode_probe_parent"


def _candidate_row(*, probes: list[str], uuid: str = _PARENT) -> dict[str, Any]:
    return {
        "record_id": f"entity:{uuid}",
        "uuid": uuid,
        "created_at": datetime.now(UTC),
        "attributes": {
            "memory_probes": probes,
            "memory_scope": "private",
            "principal_id": "user-1",
        },
    }


def _passage_row(*, index: int, parent: str = _PARENT) -> dict[str, Any]:
    return {
        "record_id": f"entity:passage-{index}",
        "uuid": passage_entity_id(parent, index),
        "parent_entity_id": parent,
    }


class _RecordingClient:
    def __init__(self, candidates: list[dict[str, Any]], passages: list[dict[str, Any]]) -> None:
        self._candidates = candidates
        self._passages = passages
        self.queries: list[tuple[str, dict[str, Any]]] = []

    async def execute_query(self, query: str, **params: Any) -> Any:
        # Named for the method the graph runtime's client actually exposes. The
        # low-level protocol also has `query`, and a double offering that name
        # would let a job calling it pass here and fail live.
        self.queries.append((query, dict(params)))
        return self._passages if "parent_entity_id IN" in query else self._candidates


class _RecordingEntityManager:
    def __init__(self, *, matches: bool = True) -> None:
        self.updates: list[tuple[str, dict[str, Any]]] = []
        self._matches = matches

    async def update(self, entity_id: str, updates: dict[str, Any]) -> object | None:
        self.updates.append((entity_id, updates))
        return object() if self._matches else None


def _runtime(client: Any, entity_manager: Any) -> Any:
    from types import SimpleNamespace

    return SimpleNamespace(client=client, entity_manager=entity_manager)


def _rehearsal(*, retrievable: int, total: int) -> dict[str, Any]:
    return {"total": total, "retrievable": retrievable, "probes": [], "truncated": False}


@pytest.mark.asyncio
async def test_replay_reads_the_columns_the_graph_actually_has() -> None:
    client = _RecordingClient([_candidate_row(probes=["why"])], [_passage_row(index=0)])
    entity_manager = _RecordingEntityManager()
    rehearse = AsyncMock(return_value=_rehearsal(retrievable=1, total=1))

    with (
        patch(
            "sibyl.jobs.probes._get_graph_runtime",
            AsyncMock(return_value=_runtime(client, entity_manager)),
        ),
        patch("sibyl.jobs.probes.rehearse_memory_probes", rehearse),
    ):
        summary = await replay_memory_probes({}, group_id=_GROUP)

    assert summary["memories"] == 1
    assert summary["probes_total"] == 1
    assert summary["probes_retrievable"] == 1
    assert summary["failures"] == 0

    candidate_query = client.queries[0][0]
    assert "attributes.memory_probes" in candidate_query
    assert "metadata.memory_probes" not in candidate_query
    # 3.x rejects ORDER BY on a field absent from a non-star projection.
    assert "created_at" in candidate_query.split("FROM")[0]
    # updated_at is stored as text rather than a datetime, so the window cannot use it.
    assert "updated_at" not in candidate_query


@pytest.mark.asyncio
async def test_replay_addresses_the_memory_by_its_logical_id() -> None:
    """The record id is not what the receipt write or a search result speaks."""
    client = _RecordingClient([_candidate_row(probes=["why"])], [_passage_row(index=0)])
    entity_manager = _RecordingEntityManager()
    rehearse = AsyncMock(return_value=_rehearsal(retrievable=1, total=1))

    with (
        patch(
            "sibyl.jobs.probes._get_graph_runtime",
            AsyncMock(return_value=_runtime(client, entity_manager)),
        ),
        patch("sibyl.jobs.probes.rehearse_memory_probes", rehearse),
    ):
        await replay_memory_probes({}, group_id=_GROUP)

    assert rehearse.await_args.kwargs["entity_id"] == _PARENT
    assert rehearse.await_args.kwargs["passage_ids"] == (passage_entity_id(_PARENT, 0),)
    assert rehearse.await_args.kwargs["surface"] == "replay"
    written_id, updates = entity_manager.updates[0]
    assert written_id == _PARENT
    assert updates["metadata"]["probe_last_replay"]["retrievable"] == 1
    # The probes themselves survive the write that records their verdict.
    assert updates["metadata"]["memory_probes"] == ["why"]


@pytest.mark.asyncio
async def test_a_receipt_that_matched_no_row_is_counted_as_a_failure() -> None:
    """update() returns None rather than raising, so silence has to be caught."""
    client = _RecordingClient([_candidate_row(probes=["why"])], [])
    entity_manager = _RecordingEntityManager(matches=False)

    with (
        patch(
            "sibyl.jobs.probes._get_graph_runtime",
            AsyncMock(return_value=_runtime(client, entity_manager)),
        ),
        patch(
            "sibyl.jobs.probes.rehearse_memory_probes",
            AsyncMock(return_value=_rehearsal(retrievable=0, total=1)),
        ),
    ):
        summary = await replay_memory_probes({}, group_id=_GROUP)

    assert summary["failures"] == 1


@pytest.mark.asyncio
async def test_a_memory_without_probes_is_not_replayed() -> None:
    client = _RecordingClient([_candidate_row(probes=[])], [])
    entity_manager = _RecordingEntityManager()

    with (
        patch(
            "sibyl.jobs.probes._get_graph_runtime",
            AsyncMock(return_value=_runtime(client, entity_manager)),
        ),
        patch("sibyl.jobs.probes.rehearse_memory_probes", AsyncMock()) as rehearse,
    ):
        summary = await replay_memory_probes({}, group_id=_GROUP)

    assert summary["memories"] == 0
    rehearse.assert_not_awaited()
    assert entity_manager.updates == []


@pytest.mark.asyncio
async def test_a_failing_passage_lookup_still_replays_against_the_parent() -> None:
    class _HalfBrokenClient(_RecordingClient):
        async def execute_query(self, query: str, **params: Any) -> Any:
            if "parent_entity_id IN" in query:
                msg = "index unavailable"
                raise RuntimeError(msg)
            return await super().execute_query(query, **params)

    client = _HalfBrokenClient([_candidate_row(probes=["why"])], [])
    entity_manager = _RecordingEntityManager()
    rehearse = AsyncMock(return_value=_rehearsal(retrievable=1, total=1))

    with (
        patch(
            "sibyl.jobs.probes._get_graph_runtime",
            AsyncMock(return_value=_runtime(client, entity_manager)),
        ),
        patch("sibyl.jobs.probes.rehearse_memory_probes", rehearse),
    ):
        summary = await replay_memory_probes({}, group_id=_GROUP)

    assert summary["memories"] == 1
    assert rehearse.await_args.kwargs["passage_ids"] == ()


@pytest.mark.asyncio
async def test_all_orgs_reports_a_self_retrievable_percentage() -> None:
    with (
        patch("sibyl.jobs.probes._list_organization_ids", AsyncMock(return_value=["a", "b"])),
        patch(
            "sibyl.jobs.probes.replay_memory_probes",
            AsyncMock(
                side_effect=[
                    {"probes_total": 4, "probes_retrievable": 3},
                    {"probes_total": 4, "probes_retrievable": 1},
                ]
            ),
        ),
    ):
        summary = await replay_memory_probes_all_orgs({})

    assert summary["orgs_succeeded"] == 2
    assert summary["probes_total"] == 8
    assert summary["self_retrievable_pct"] == 50.0


@pytest.mark.asyncio
async def test_one_orgs_failure_does_not_stop_the_walk() -> None:
    with (
        patch("sibyl.jobs.probes._list_organization_ids", AsyncMock(return_value=["a", "b"])),
        patch(
            "sibyl.jobs.probes.replay_memory_probes",
            AsyncMock(
                side_effect=[
                    RuntimeError("graph down"),
                    {"probes_total": 1, "probes_retrievable": 1},
                ]
            ),
        ),
    ):
        summary = await replay_memory_probes_all_orgs({})

    assert summary["orgs_failed"] == 1
    assert summary["orgs_succeeded"] == 1
    assert summary["self_retrievable_pct"] == 100.0
