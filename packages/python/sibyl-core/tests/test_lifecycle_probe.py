"""The lifecycle tick learns whether an organization owes anything from one round trip per side."""

from __future__ import annotations

from typing import Any

import pytest

from sibyl_core.backends.surreal.schema_embedding_states import embedding_state_key
from sibyl_core.services.lifecycle_probe import (
    ContentWork,
    PlaneFacts,
    plane_facts,
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
CHUNK = {**STAMP, "model": "chunks-v1"}
RAW = {**STAMP, "model": "raw-v1", "text_version": "raw-capture-v1"}
PLANES = ["document_chunks", "raw_captures"]


class FakeClient:
    def __init__(self, group_id: str, results: list[object]) -> None:
        self.group_id = group_id
        self.results = results
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def execute_query_batch(self, query: str, **params: object) -> object:
        self.calls.append((query, dict(params)))
        return self.results


def _current(stamp: dict[str, Any], age: float = 10.0, **fields: Any) -> dict[str, Any]:
    return {"complete_metadata": dict(stamp), "complete_age_seconds": age, **fields}


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
        (
            [[_current(STAMP, legacy_provisional=True)], [], []],
            "a provisional adoption is weighed again every pass",
        ),
    ],
)
async def test_graph_probe_sends_an_organization_with_work_through_the_full_pass(
    results: list[object], reason: str
) -> None:
    client = FakeClient("org-b", results)

    work = await probe_graph_work(client, graph_stamp=STAMP, verify_interval=3600.0)

    assert not work.idle, reason


async def test_graph_probe_reports_the_verdict_a_current_plane_holds() -> None:
    state = _current(
        STAMP,
        legacy_decision="adopt",
        legacy_warning="adopted_without_evidence",
        legacy_notice=None,
    )
    client = FakeClient("org-w", [[state], [], []])

    work = await probe_graph_work(client, graph_stamp=STAMP, verify_interval=3600.0)

    assert work.idle
    assert work.sweep == PlaneFacts(
        current=True, legacy_decision="adopt", warning="adopted_without_evidence", notice=None
    )


async def test_graph_probe_without_a_provider_asks_only_for_lifecycle_rows() -> None:
    client = FakeClient("org-c", [[]])

    work = await probe_graph_work(client, graph_stamp=None, verify_interval=3600.0)

    assert work.idle and work.sweep is None
    query, params = client.calls[0]
    assert query.count(";") == 1
    assert "state_key" not in params and "stamp" not in params

    client = FakeClient("org-c", [[{"uuid": "row"}]])
    assert not (await probe_graph_work(client, graph_stamp=None, verify_interval=3600.0)).idle


async def test_content_probe_answers_every_organization_in_one_round_trip() -> None:
    rows = [
        {"organization_id": "a", "plane": "document_chunks", **_current(CHUNK)},
        {"organization_id": "a", "plane": "raw_captures", **_current(RAW)},
        {"organization_id": "b", "plane": "document_chunks", **_current(CHUNK)},
        {"organization_id": "b", "plane": "raw_captures", **_current(OTHER)},
        {"organization_id": "d", "plane": "document_chunks", **_current(CHUNK)},
        {"organization_id": "d", "plane": "raw_captures", **_current(RAW)},
    ]
    client = FakeClient("content", [rows, [{"organization_id": "d"}]])

    work = await probe_content_work(
        client, stamps={"document_chunks": CHUNK, "raw_captures": RAW}, verify_interval=3600.0
    )

    assert len(client.calls) == 1
    query, params = client.calls[0]
    assert query.count(";") == 2
    assert "WITH INDEX idx_raw_captures_validation_pending" in query
    assert "last_run.pass_deferred AS pass_deferred" in query
    assert params == {"planes": PLANES}
    assert work.idle("a", planes=PLANES)
    assert not work.idle("b", planes=PLANES), "the raw plane finished for another model"
    assert work.idle("b", planes=["document_chunks"])
    assert not work.idle("c", planes=PLANES), "an organization with no state owes a pass"
    assert work.idle("c", planes=[])
    assert not work.idle("d", planes=PLANES), "source validation is pending"


@pytest.mark.parametrize(
    ("receipt", "reason"),
    [
        ({"pass_deferred": 1, "pass_refused": 0}, "a capture the model keeps failing on"),
        ({"pass_deferred": 0, "pass_refused": 2}, "a capture the provider refused"),
    ],
)
async def test_a_raw_plane_holding_a_set_aside_capture_is_never_idle(
    receipt: dict[str, int], reason: str
) -> None:
    """A deferred or refused capture is counted every pass and retried when its wait ends."""
    rows = [
        {"organization_id": "a", "plane": "raw_captures", **_current(RAW, **receipt)},
        {"organization_id": "a", "plane": "document_chunks", **_current(CHUNK)},
    ]
    client = FakeClient("content", [rows, []])

    work = await probe_content_work(
        client, stamps={"document_chunks": CHUNK, "raw_captures": RAW}, verify_interval=3600.0
    )

    assert not work.idle("a", planes=PLANES), reason
    assert work.idle("a", planes=["document_chunks"])
    facts = work.facts("a", "raw_captures")
    assert facts is not None and not facts.current


async def test_a_provisional_chunk_plane_is_never_idle_and_its_warning_is_reported() -> None:
    rows = [
        {
            "organization_id": "a",
            "plane": "document_chunks",
            **_current(
                CHUNK,
                legacy_decision="adopt",
                legacy_warning="adopted_on_incomplete_evidence",
                legacy_provisional=True,
            ),
        },
        {
            "organization_id": "b",
            "plane": "document_chunks",
            **_current(CHUNK, legacy_decision="adopt", legacy_warning="adopted_without_evidence"),
        },
    ]
    client = FakeClient("content", [rows, []])

    work = await probe_content_work(
        client, stamps={"document_chunks": CHUNK}, verify_interval=3600.0
    )

    assert not work.idle("a", planes=["document_chunks"])
    assert work.idle("b", planes=["document_chunks"])
    assert work.facts("b", "document_chunks") == PlaneFacts(
        current=True,
        legacy_decision="adopt",
        warning="adopted_without_evidence",
        notice=None,
    )


def test_plane_facts_judges_the_raw_plane_by_its_receipt() -> None:
    state = _current(RAW, pass_deferred=0, pass_refused=0)
    assert plane_facts(state, plane="raw_captures", stamp=RAW, verify_interval=3600.0).current
    held = _current(RAW, pass_deferred=1)
    assert not plane_facts(held, plane="raw_captures", stamp=RAW, verify_interval=3600.0).current


async def test_content_probe_without_embedders_asks_only_about_source_validation() -> None:
    client = FakeClient("content", [[{"organization_id": "x"}]])

    work = await probe_content_work(client, stamps={}, verify_interval=3600.0)

    query, params = client.calls[0]
    assert query.count(";") == 1 and params == {}
    assert work == ContentWork(planes={}, validation_pending=frozenset({"x"}))


async def test_probes_refuse_a_response_with_the_wrong_statement_count() -> None:
    client = FakeClient("org-e", [[]])
    with pytest.raises(RuntimeError):
        await probe_graph_work(client, graph_stamp=STAMP, verify_interval=3600.0)
