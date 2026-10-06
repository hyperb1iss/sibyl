"""The lifecycle tick learns whether an organization owes anything from one round trip per side."""

from __future__ import annotations

from typing import Any

import pytest

from sibyl_core.backends.surreal.schema_embedding_states import embedding_state_key
from sibyl_core.services.lifecycle_probe import (
    ContentWork,
    probe_content_work,
    probe_graph_work,
)

STAMP = {
    "provider": "deterministic",
    "model": "graph-v1",
    "dimensions": 3,
    "text_version": None,
    "input_kind_sensitive": False,
    "stamp_version": 2,
}
OTHER = {**STAMP, "model": "graph-v2"}


class FakeClient:
    def __init__(self, group_id: str, results: list[object]) -> None:
        self.group_id = group_id
        self.results = results
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def execute_query_batch(self, query: str, **params: object) -> object:
        self.calls.append((query, dict(params)))
        return self.results


def _current(stamp: dict[str, Any], age: float = 10.0) -> dict[str, Any]:
    return {"complete_metadata": dict(stamp), "complete_age_seconds": age}


async def test_graph_probe_is_one_indexed_round_trip_on_the_organization_namespace() -> None:
    client = FakeClient("org-a", [[_current(STAMP)], [], []])

    work = await probe_graph_work(client, graph_stamp=STAMP, verify_interval=3600.0)

    assert work.idle
    assert len(client.calls) == 1, "an idle organization costs the tick one query"
    query, params = client.calls[0]
    assert query.count(";") == 3
    assert "WITH INDEX idx_entity_lifecycle_repair_key" in query
    assert "WITH INDEX idx_entity_reflection_candidate_uuid" in query
    assert "LIMIT $limit" in query and "LIMIT 1;" in query
    assert params["limit"] == 1
    assert params["cursor"] == ""
    assert params["group_id"] == "org-a"
    assert params["stamp"] == STAMP
    assert params["state_key"] == embedding_state_key("org-a", "graph")


@pytest.mark.parametrize(
    ("results", "reason"),
    [
        ([[_current(STAMP)], [{"uuid": "row"}], []], "a pending lifecycle row"),
        ([[_current(STAMP)], [], [{"uuid": "candidate"}]], "a candidate owed a vector"),
        ([[_current(OTHER)], [], []], "a sweep finished for another model"),
        ([[_current(STAMP, age=7200.0)], [], []], "a sweep older than the verify interval"),
        ([[], [], []], "no sweep state at all"),
    ],
)
async def test_graph_probe_sends_an_organization_with_work_through_the_full_pass(
    results: list[object], reason: str
) -> None:
    client = FakeClient("org-b", results)

    work = await probe_graph_work(client, graph_stamp=STAMP, verify_interval=3600.0)

    assert not work.idle, reason


async def test_graph_probe_without_a_provider_asks_only_for_lifecycle_rows() -> None:
    client = FakeClient("org-c", [[]])

    work = await probe_graph_work(client, graph_stamp=None, verify_interval=3600.0)

    assert work.idle
    query, params = client.calls[0]
    assert query.count(";") == 1
    assert "state_key" not in params and "stamp" not in params

    client = FakeClient("org-c", [[{"uuid": "row"}]])
    assert not (await probe_graph_work(client, graph_stamp=None, verify_interval=3600.0)).idle


async def test_content_probe_answers_every_organization_in_one_round_trip() -> None:
    chunk = {**STAMP, "model": "chunks-v1"}
    raw = {**STAMP, "model": "raw-v1", "text_version": "raw-capture-v1"}
    rows = [
        {"organization_id": "a", "plane": "document_chunks", **_current(chunk)},
        {"organization_id": "a", "plane": "raw_captures", **_current(raw)},
        {"organization_id": "b", "plane": "document_chunks", **_current(chunk)},
        {"organization_id": "b", "plane": "raw_captures", **_current(OTHER)},
        {"organization_id": "d", "plane": "document_chunks", **_current(chunk)},
        {"organization_id": "d", "plane": "raw_captures", **_current(raw)},
    ]
    client = FakeClient("content", [rows, [{"organization_id": "d"}]])

    work = await probe_content_work(
        client,
        stamps={"document_chunks": chunk, "raw_captures": raw},
        verify_interval=3600.0,
    )

    assert len(client.calls) == 1
    query, params = client.calls[0]
    assert query.count(";") == 2
    assert "WITH INDEX idx_raw_captures_validation_pending" in query
    assert params == {"planes": ["document_chunks", "raw_captures"]}
    planes = ["document_chunks", "raw_captures"]
    assert work.idle("a", planes=planes)
    assert not work.idle("b", planes=planes), "the raw plane finished for another model"
    assert work.idle("b", planes=["document_chunks"])
    assert not work.idle("c", planes=planes), "an organization with no state owes a pass"
    assert work.idle("c", planes=[])
    assert not work.idle("d", planes=planes), "source validation is pending"


async def test_content_probe_without_embedders_asks_only_about_source_validation() -> None:
    client = FakeClient("content", [[{"organization_id": "x"}]])

    work = await probe_content_work(client, stamps={}, verify_interval=3600.0)

    query, params = client.calls[0]
    assert query.count(";") == 1 and params == {}
    assert work == ContentWork(current_planes=frozenset(), validation_pending=frozenset({"x"}))


async def test_probes_refuse_a_response_with_the_wrong_statement_count() -> None:
    client = FakeClient("org-e", [[]])
    with pytest.raises(RuntimeError):
        await probe_graph_work(client, graph_stamp=STAMP, verify_interval=3600.0)
