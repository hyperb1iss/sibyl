"""Write-time probe rehearsal: what the receipt records, and what it cannot hide."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from sibyl_core.memory_pipeline.capture import MemoryCaptureRequest, MemoryCaptureService
from sibyl_core.memory_pipeline.rehearsal import (
    REHEARSAL_STATUS_ABSENT,
    REHEARSAL_STATUS_ERROR,
    REHEARSAL_STATUS_RETRIEVABLE,
    REHEARSAL_STATUS_SKIPPED,
    rehearse_memory_probes,
)
from sibyl_core.memory_pipeline.structure import MemoryStructureError

_ENTITY = "episode_parent"
_PASSAGE = "passage_child_0"


@dataclass
class _Result:
    id: str


@dataclass
class _Response:
    results: list[_Result]


class _RecordingSearch:
    def __init__(self, *responses: list[str]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> _Response:
        self.calls.append(kwargs)
        ids = self._responses.pop(0) if self._responses else []
        return _Response(results=[_Result(id=value) for value in ids])


async def _rehearse(probes: list[str], search: Any, **overrides: Any) -> dict[str, Any]:
    return await rehearse_memory_probes(
        probes=probes,
        organization_id="org-1",
        entity_id=_ENTITY,
        passage_ids=[_PASSAGE],
        principal_id="user-1",
        search_fn=search,
        **overrides,
    )


@pytest.mark.asyncio
async def test_a_probe_that_finds_the_parent_records_its_rank() -> None:
    search = _RecordingSearch(["other_1", _ENTITY])

    receipt = await _rehearse(["why did it break"], search)

    assert receipt["probes"][0]["status"] == REHEARSAL_STATUS_RETRIEVABLE
    assert receipt["probes"][0]["rank"] == 2
    assert receipt["probes"][0]["matched_kind"] == "parent"
    assert receipt["retrievable"] == 1
    assert receipt["total"] == 1


@pytest.mark.asyncio
async def test_a_probe_that_finds_one_of_the_memorys_spans_counts_as_found() -> None:
    """A reader that lands on a span widens to its parent in one lookup."""
    search = _RecordingSearch([_PASSAGE])

    receipt = await _rehearse(["what fixed it"], search)

    assert receipt["probes"][0]["status"] == REHEARSAL_STATUS_RETRIEVABLE
    assert receipt["probes"][0]["matched_kind"] == "passage"
    assert receipt["probes"][0]["matched_id"] == _PASSAGE


@pytest.mark.asyncio
async def test_a_probe_that_finds_nothing_is_recorded_as_absent() -> None:
    """The honest failure. A rehearsal that cannot fail measures nothing."""
    search = _RecordingSearch(["unrelated_1", "unrelated_2"])

    receipt = await _rehearse(["totally unrelated vocabulary"], search)

    assert receipt["probes"][0]["status"] == REHEARSAL_STATUS_ABSENT
    assert receipt["probes"][0]["rank"] is None
    assert receipt["retrievable"] == 0


@pytest.mark.asyncio
async def test_rehearsal_excludes_raw_recall_and_does_not_record_exposure() -> None:
    """Raw recall would match the text just written and pass every probe."""
    search = _RecordingSearch([_ENTITY])

    await _rehearse(["why"], search)

    call = search.calls[0]
    assert call["include_raw_memory"] is False
    assert call["include_documents"] is False
    assert call["record_exposure"] is False
    assert call["organization_id"] == "org-1"
    assert call["principal_id"] == "user-1"


@pytest.mark.asyncio
async def test_a_failing_search_is_recorded_and_never_raised() -> None:
    async def explode(**_kwargs: Any) -> None:
        msg = "retrieval is down"
        raise RuntimeError(msg)

    receipt = await _rehearse(["why"], explode)

    assert receipt["probes"][0]["status"] == REHEARSAL_STATUS_ERROR
    assert receipt["probes"][0]["error_type"] == "RuntimeError"
    assert receipt["retrievable"] == 0


@pytest.mark.asyncio
async def test_an_exhausted_budget_skips_the_rest_and_says_so() -> None:
    search = _RecordingSearch([_ENTITY], [_ENTITY])

    receipt = await _rehearse(["one", "two"], search, budget_seconds=0.0)

    assert receipt["truncated"] is True
    assert [entry["status"] for entry in receipt["probes"]] == [
        REHEARSAL_STATUS_SKIPPED,
        REHEARSAL_STATUS_SKIPPED,
    ]
    assert search.calls == []


@pytest.mark.asyncio
async def test_every_probe_gets_an_entry_in_order() -> None:
    search = _RecordingSearch([_ENTITY], ["nope"], [_PASSAGE])

    receipt = await _rehearse(["a", "b", "c"], search)

    assert [entry["probe"] for entry in receipt["probes"]] == ["a", "b", "c"]
    assert receipt["retrievable"] == 2
    assert receipt["truncated"] is False


# ---------------------------------------------------------------------------
# Capture spine
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_invalid_plan_is_refused_before_the_verbatim_write() -> None:
    """A raw capture left behind by a rejected write turns a retry into a duplicate."""
    raw_writes: list[str] = []

    async def remember_raw(request: MemoryCaptureRequest) -> dict[str, Any]:
        raw_writes.append(request.content)
        return {"id": "raw_1", "source_id": "src"}

    async def create_graph_entity(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"id": "episode_1"}

    service = MemoryCaptureService(
        remember_raw_memory=remember_raw,
        create_graph_entity=create_graph_entity,
    )

    with pytest.raises(MemoryStructureError):
        await service.capture(
            MemoryCaptureRequest(
                title="t",
                content="alpha beta gamma",
                spans=[{"start": 0, "end": 4}, {"start": 9, "end": 16}],
            )
        )

    assert raw_writes == []


def test_spans_address_the_stored_body_not_the_untrimmed_one() -> None:
    request = MemoryCaptureRequest(
        title="t",
        content="  alpha beta  ",
        spans=[{"start": 0, "end": 5}, {"start": 5, "end": 10}],
    )

    assert request.stored_content == "alpha beta"
    assert len(request.structure().spans) == 2
