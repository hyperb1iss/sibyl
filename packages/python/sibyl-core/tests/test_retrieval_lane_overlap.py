"""The vector lanes run beside the lexical lanes, not after them.

Nothing the vector lanes need comes out of the lexical gather: the query text
is known up front, the embedding is cached per process and the HNSW walks read
the same client. Sequencing them after the gather parked the embedding call
and both walks behind the slowest raw-memory scope on every pack.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

import sibyl_core.retrieval.search as search_module
from sibyl_core.models.context import ContextFacet
from sibyl_core.retrieval import _search_database as database_module
from sibyl_core.retrieval import _search_expansion as expansion_module
from sibyl_core.retrieval import _search_sources as source_module
from sibyl_core.retrieval.candidates import RetrievalCandidate, VectorCandidateFetch
from sibyl_core.retrieval.search import build_context_retrieval_plan


def _plan() -> Any:
    return build_context_retrieval_plan(
        query="deployment target",
        organization_id="org-123",
        facets=[ContextFacet.DECISIONS],
        facet_types={ContextFacet.DECISIONS: ["decision"]},
        principal_id="user-123",
        project="project_123",
        accessible_projects={"project_123"},
        limit=12,
    )


class _IdleClient:
    async def execute_query(self, _query: str, **_params: object) -> list[dict[str, object]]:
        return []


@pytest.mark.asyncio
async def test_vector_lanes_are_in_flight_while_the_lexical_lanes_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tracker = SimpleNamespace(in_flight=0, peak=0)

    async def overlapping(result: Any) -> Any:
        tracker.in_flight += 1
        tracker.peak = max(tracker.peak, tracker.in_flight)
        try:
            await asyncio.sleep(0.02)
        finally:
            tracker.in_flight -= 1
        return result

    async def lexical_lane(**_kwargs: object) -> list[RetrievalCandidate]:
        return await overlapping([])

    async def vector_lanes(**_kwargs: object) -> VectorCandidateFetch:
        return await overlapping(
            VectorCandidateFetch(
                node_candidates=[], edge_candidates=[], requested=True, attempted=True
            )
        )

    async def no_expansion(**_kwargs: object) -> list[RetrievalCandidate]:
        return []

    async def no_raw_memory(**_kwargs: object) -> list[Any]:
        return []

    async def fake_runtime(_organization_id: str, **_kwargs: object) -> Any:
        return SimpleNamespace(client=_IdleClient())

    # Only the lane fakes read the provider, so any object stands in for it.
    provider: Any = SimpleNamespace(metadata=None)

    monkeypatch.setattr(database_module, "get_surreal_graph_runtime", fake_runtime)
    monkeypatch.setattr(source_module, "_node_fulltext_candidates", lexical_lane)
    monkeypatch.setattr(source_module, "_vector_candidate_sources_detailed", vector_lanes)
    monkeypatch.setattr(expansion_module, "_graph_expansion_candidates", no_expansion)

    response = await search_module.context_search(
        plan=_plan(),
        types=["decision"],
        facet=ContextFacet.DECISIONS,
        limit=5,
        embedding_provider=provider,
        raw_memory_recall_fn=no_raw_memory,
    )

    assert tracker.peak == 2
    timings = response.filters["stage_timings_ms"]
    assert timings["lexical_candidates"] >= 20
    assert timings["vector_candidates"] >= 20
