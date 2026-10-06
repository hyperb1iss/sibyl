"""One retrieval request proves each row's availability once.

The supersession gate used to re-derive every verdict from scratch on each of
its passes, and the pack's admission check and related-item batch asked the
same questions again. These tests pin the request-scoped memo: a (kind, id)
is loaded once, concurrent askers share the load in flight, the gate's
independent proofs run together, and a pack does not re-prove a row it
already admitted when that row comes back as somebody's neighbour.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

import sibyl_core.tools.context as context_module
from sibyl_core.models.context import ContextFacet
from sibyl_core.models.entities import Entity, EntityType
from sibyl_core.retrieval import _search_candidates as candidate_module
from sibyl_core.retrieval import _search_lifecycle as lifecycle_module
from sibyl_core.retrieval.candidates import CandidateKind, RetrievalCandidate
from sibyl_core.retrieval.search import RetrievalSignal, build_context_retrieval_plan
from sibyl_core.services import graph_read_availability as availability
from sibyl_core.services.graph_read_availability import GraphReadMemo
from sibyl_core.services.graph_records import relationship_from_surreal_row
from sibyl_core.tools.context import compile_context
from sibyl_core.tools.responses import SearchResponse, SearchResult

ORG = "org-123"
PROJECT = "project_123"
EDGE_ROW: dict[str, Any] = {
    "uuid": "edge-1",
    "name": "SUPPORTS",
    "fact": "node-a supports node-c",
    "group_id": ORG,
    "source_id": "node-a",
    "target_id": "node-c",
    "source_uuid": "node-a",
    "target_uuid": "node-c",
    "source_node_uuid": "node-a",
    "target_node_uuid": "node-c",
    "attributes": {"project_id": PROJECT},
    "episodes": [],
    "created_at": "2026-01-01T00:00:00+00:00",
}


def _plan() -> Any:
    return build_context_retrieval_plan(
        query="deployment target",
        organization_id=ORG,
        facets=[ContextFacet.DECISIONS],
        facet_types={ContextFacet.DECISIONS: ["decision"]},
        principal_id="user-123",
        project=PROJECT,
        accessible_projects={PROJECT},
        limit=12,
    )


def _node(identifier: str) -> RetrievalCandidate:
    return RetrievalCandidate(
        id=identifier,
        type="decision",
        name=identifier,
        content="body",
        score=1.0,
        source=None,
        metadata={"project_id": PROJECT},
        project_id=PROJECT,
        kind=CandidateKind.NODE,
    )


def _edge() -> RetrievalCandidate:
    return candidate_module._candidate_from_edge_record(
        EDGE_ROW, signal=RetrievalSignal.EDGE_FULLTEXT, score=1.0
    )


class _EdgeRowClient:
    """Serves the edge-row hydrate and records which ids it was asked for."""

    def __init__(self) -> None:
        self.edge_row_batches: list[list[str]] = []

    async def execute_query(self, query: str, **params: Any) -> list[dict[str, object]]:
        if "in.uuid AS source_uuid" in query and "uuid IN $ids" in query:
            ids = [str(value) for value in params["ids"]]
            self.edge_row_batches.append(ids)
            return [dict(EDGE_ROW) for identifier in ids if identifier == EDGE_ROW["uuid"]]
        return []


@pytest.mark.asyncio
async def test_memo_loads_each_key_once_and_shares_the_load_in_flight() -> None:
    memo = GraphReadMemo(ORG)
    batches: list[list[str]] = []
    release = asyncio.Event()

    async def load(keys: list[str]) -> dict[str, str]:
        batches.append(list(keys))
        await release.wait()
        return {key: key.upper() for key in keys if key != "gone"}

    first = asyncio.create_task(memo.once("kind", ["a", "b"], load, missing=None))
    await asyncio.sleep(0)
    second = asyncio.create_task(memo.once("kind", ["b", "c", "gone"], load, missing=None))
    await asyncio.sleep(0)
    release.set()

    assert await first == {"a": "A", "b": "B"}
    assert await second == {"b": "B", "c": "C", "gone": None}
    assert batches == [["a", "b"], ["c", "gone"]]
    assert await memo.once("kind", ["gone", "c", "a"], load, missing=None) == {
        "gone": None,
        "c": "C",
        "a": "A",
    }
    assert len(batches) == 2


@pytest.mark.asyncio
async def test_memo_reloads_keys_whose_load_failed() -> None:
    memo = GraphReadMemo(ORG)
    attempts = 0

    async def load(keys: list[str]) -> dict[str, bool]:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("surreal is unhappy")
        return dict.fromkeys(keys, True)

    with pytest.raises(RuntimeError):
        await memo.once("kind", ["a"], load, missing=False)
    assert await memo.once("kind", ["a"], load, missing=False) == {"a": True}
    assert attempts == 2


@pytest.mark.asyncio
async def test_supersession_gate_proves_each_id_once_across_its_passes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two gate passes over overlapping candidates load every (kind, id) once."""

    loads: dict[str, list[list[str]]] = {
        "capture_rows": [],
        "publication": [],
        "relationship": [],
        "entity": [],
        "supersession": [],
    }

    async def capture_rows(org: str, rows: dict[str, Any], **_kwargs: object) -> dict[str, Any]:
        assert org == ORG
        loads["capture_rows"].append(sorted(rows))
        return dict(rows)

    async def publications(org: str, rows: dict[str, Any], **_kwargs: object) -> set[str]:
        assert org == ORG
        loads["publication"].append(sorted(rows))
        return set()

    async def relationships(org: str, ids: list[str], **_kwargs: object) -> dict[str, Any]:
        assert org == ORG
        loads["relationship"].append(sorted(ids))
        return {
            identifier: relationship_from_surreal_row(EDGE_ROW)
            for identifier in ids
            if identifier == EDGE_ROW["uuid"]
        }

    async def entities(org: str, ids: list[str], **_kwargs: object) -> dict[str, Entity]:
        assert org == ORG
        loads["entity"].append(sorted(ids))
        return {
            identifier: Entity(
                id=identifier,
                name=identifier,
                entity_type=EntityType.DECISION,
                organization_id=ORG,
                metadata={"project_id": PROJECT},
            )
            for identifier in ids
        }

    async def supersession_rows(
        _client: object, *, group_id: str, uuids: list[str]
    ) -> list[dict[str, object]]:
        assert group_id == ORG
        loads["supersession"].append(sorted(uuids))
        return []

    monkeypatch.setattr(lifecycle_module, "available_capture_projection_rows", capture_rows)
    monkeypatch.setattr(lifecycle_module, "unavailable_publication_ids", publications)
    monkeypatch.setattr(availability, "_load_available_graph_relationships", relationships)
    monkeypatch.setattr(availability, "_load_available_graph_entities", entities)
    monkeypatch.setattr(lifecycle_module, "_supersession_edge_rows", supersession_rows)

    client = _EdgeRowClient()
    memo = GraphReadMemo(ORG)
    plan = _plan()

    first, _receipt = await lifecycle_module._apply_supersession_gate(
        client=client,
        group_id=ORG,
        plan=plan,
        source_lists=[
            (RetrievalSignal.NODE_FULLTEXT, [_node("node-a"), _node("node-b")]),
            (RetrievalSignal.EDGE_FULLTEXT, [_edge()]),
        ],
        memo=memo,
    )
    assert [candidate.id for _signal, candidates in first for candidate in candidates] == [
        "node-a",
        "node-b",
        "edge-1",
    ]
    assert loads == {
        "capture_rows": [["node-a", "node-b"]],
        "publication": [["node-a", "node-b"], ["edge-1"]],
        "relationship": [["edge-1"]],
        "entity": [["node-a", "node-c"]],
        "supersession": [["node-a", "node-b", "node-c"]],
    }
    assert client.edge_row_batches == [["edge-1"]]

    second, _receipt = await lifecycle_module._apply_supersession_gate(
        client=client,
        group_id=ORG,
        plan=plan,
        source_lists=[
            (RetrievalSignal.GRAPH_EXPANSION, [_node("node-b"), _node("node-c"), _edge()]),
        ],
        memo=memo,
    )
    assert [candidate.id for _signal, candidates in second for candidate in candidates] == [
        "node-b",
        "node-c",
        "edge-1",
    ]
    # Only node-c is new to the request; every other verdict was already settled.
    assert loads == {
        "capture_rows": [["node-a", "node-b"], ["node-c"]],
        "publication": [["node-a", "node-b"], ["edge-1"], ["node-c"]],
        "relationship": [["edge-1"]],
        "entity": [["node-a", "node-c"]],
        "supersession": [["node-a", "node-b", "node-c"]],
    }
    assert client.edge_row_batches == [["edge-1"]]


@pytest.mark.asyncio
async def test_supersession_gate_runs_its_independent_proofs_together(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Capture ancestry, the publication ledger and the edge proof overlap."""

    tracker = SimpleNamespace(in_flight=0, peak=0)

    async def overlapping(result: Any) -> Any:
        tracker.in_flight += 1
        tracker.peak = max(tracker.peak, tracker.in_flight)
        try:
            await asyncio.sleep(0.02)
        finally:
            tracker.in_flight -= 1
        return result

    async def capture_rows(_org: str, rows: dict[str, Any], **_kwargs: object) -> dict[str, Any]:
        return await overlapping(dict(rows))

    async def publications(_org: str, _rows: dict[str, Any], **_kwargs: object) -> set[str]:
        return await overlapping(set())

    async def relationships(_org: str, _ids: list[str], **_kwargs: object) -> dict[str, Any]:
        return await overlapping({})

    async def supersession_rows(*_args: object, **_kwargs: object) -> list[dict[str, object]]:
        return []

    monkeypatch.setattr(lifecycle_module, "available_capture_projection_rows", capture_rows)
    monkeypatch.setattr(lifecycle_module, "unavailable_publication_ids", publications)
    monkeypatch.setattr(availability, "_load_available_graph_relationships", relationships)
    monkeypatch.setattr(lifecycle_module, "_supersession_edge_rows", supersession_rows)

    await lifecycle_module._apply_supersession_gate(
        client=_EdgeRowClient(),
        group_id=ORG,
        plan=_plan(),
        source_lists=[
            (RetrievalSignal.NODE_FULLTEXT, [_node("node-a")]),
            (RetrievalSignal.EDGE_FULLTEXT, [_edge()]),
        ],
    )

    assert tracker.peak == 3


def _served(identifier: str, name: str) -> SearchResult:
    return SearchResult(
        id=identifier,
        type="decision",
        name=name,
        content=f"{name} content",
        score=0.9,
        source=None,
        result_origin="graph",
        metadata={"entity_type": "decision", "project_id": "project-1"},
    )


@pytest.mark.asyncio
async def test_pack_does_not_reprove_an_admitted_item_that_returns_as_a_neighbour(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Admission settles a row's ledger verdict; the related batch reuses it."""

    served = [_served("decision-1", "Use context packs"), _served("decision-2", "Batch lookups")]

    async def fake_context_search(**kwargs: Any) -> SearchResponse:
        return SearchResponse(
            results=served,
            total=len(served),
            query=kwargs["plan"].query,
            filters={},
            graph_count=len(served),
            document_count=0,
            limit=len(served),
        )

    publication_loads: list[list[str]] = []

    async def publications(org: str, rows: dict[str, Any], **_kwargs: object) -> set[str]:
        assert org == ORG
        publication_loads.append(sorted(rows))
        return set()

    async def no_supersession(*_args: object, **_kwargs: object) -> tuple[set[str], int]:
        return set(), 0

    neighbour = SimpleNamespace(
        id="decision-2",
        entity_type=SimpleNamespace(value="decision"),
        name="Batch lookups",
        content="Batch lookups content",
        metadata={"project_id": "project-1"},
    )
    relationship = SimpleNamespace(
        source_id="decision-1",
        target_id="decision-2",
        relationship_type=SimpleNamespace(value="RELATES_TO"),
        metadata={"project_id": "project-1"},
    )
    runtime = SimpleNamespace(
        client=SimpleNamespace(execute_query=AsyncMock(return_value=[])),
        relationship_manager=SimpleNamespace(
            get_related_entities=AsyncMock(return_value=[]),
            get_related_entities_batch=AsyncMock(
                return_value={"decision-1": [(neighbour, relationship)], "decision-2": []}
            ),
        ),
    )
    monkeypatch.setattr(context_module, "context_search", fake_context_search)
    monkeypatch.setattr(context_module, "unavailable_publication_ids", publications)
    monkeypatch.setattr(context_module, "_superseded_candidate_uuids", no_supersession)
    monkeypatch.setattr(context_module, "get_graph_runtime", AsyncMock(return_value=runtime))

    pack = await compile_context(
        "ship faster",
        intent="decide",
        organization_id=ORG,
        limit=2,
        include_related=True,
        principal_id="reader-1",
        accessible_projects={"project-1"},
        record_exposure=False,
    )

    assert [item.id for item in pack.items] == ["decision-1", "decision-2"]
    assert pack.items[0].related[0].id == "decision-2"
    assert publication_loads == [["decision-1", "decision-2"]]
