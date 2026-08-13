"""Behavioral gates for supersession and correction enforcement.

The write side already knew how to say "B replaced A". Retrieval acted on the
declaration backwards: the SUPERSEDES edge is stored new-row to old-row, and
following it outwards scored the retired row at 0.95. These tests pin the
corrected behavior at each of the three places the declaration now bites --
the traversal query, the candidate gate inside `context_search`, and the last
admission check before a pack is served.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

import pytest

import sibyl_core.retrieval.search as search_module
import sibyl_core.tools.context as context_module
from sibyl_core.memory_pipeline.lifecycle import graph_metadata_recallable
from sibyl_core.models.context import ContextFacet
from sibyl_core.models.entities import RelationshipType
from sibyl_core.retrieval.search import (
    RetrievalCandidate,
    RetrievalSignal,
    build_context_retrieval_plan,
)
from sibyl_core.services import memory as memory_module
from sibyl_core.services.surreal_content import MemoryScope, RawMemory
from sibyl_core.tools.context import compile_context
from sibyl_core.tools.responses import SearchResponse, SearchResult

SUPERSEDED_ID = "memory-old"
SUCCESSOR_ID = "memory-new"


def _plan(query: str = "deployment target") -> Any:
    return build_context_retrieval_plan(
        query=query,
        organization_id="org-123",
        facets=[ContextFacet.DECISIONS],
        facet_types={ContextFacet.DECISIONS: ["decision"]},
        principal_id="user-123",
        project="project_123",
        accessible_projects={"project_123"},
        limit=12,
    )


class _SupersessionGraphClient:
    """A graph holding exactly one supersession: SUCCESSOR replaced SUPERSEDED.

    The edge row is emitted in the direction the write path actually stores
    it (`services/memory.py:_relationships_for_promotion` builds
    `_relationship(entity_id, superseded_id, SUPERSEDES)`), and the relation
    query honors the exclusion parameter so the test observes behavior rather
    than SQL text.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def execute_query(self, query: str, **params: object) -> list[dict[str, object]]:
        self.calls.append((query, params))
        if "FROM mentions" in query:
            return []
        if 'out.entity_type = "community"' in query or "target_id IN $community_uuids" in query:
            return []
        if "FROM relates_to" in query and "target_id IN $uuids" in query:
            targets = set(params.get("uuids") or ())
            return [{"uuid": SUPERSEDED_ID}] if SUPERSEDED_ID in targets else []
        if "FROM relates_to" in query:
            sources = set(params.get("source_uuids") or ())
            if SUCCESSOR_ID not in sources:
                return []
            wanted = params.get("relationship_names")
            excluded = set(params.get("exclude_relationship_names") or ())
            if wanted and "SUPERSEDES" not in set(wanted):
                return []
            if "SUPERSEDES" in excluded:
                return []
            return [{"uuid": SUPERSEDED_ID, "relationship": "SUPERSEDES"}]
        if "FROM entity" in query:
            return [
                {
                    "uuid": SUPERSEDED_ID,
                    "name": "Deploy to Fly",
                    "entity_type": "decision",
                    "content": "we deploy to fly.io",
                    "project_id": "project_123",
                    "attributes": {},
                    "created_at": None,
                }
            ]
        return []


@pytest.mark.asyncio
async def test_outgoing_supersedes_edge_no_longer_expands_into_the_retired_row() -> None:
    """Traversal gate: the walk must not carry back the row it just retired."""

    client = _SupersessionGraphClient()
    candidates = await search_module._graph_expansion_candidates(
        client=client,
        plan=_plan(),
        search_filter=search_module.SearchFilter(
            node_types=("decision",),
            project_ids=("project_123",),
        ),
        seed_candidates=[
            RetrievalCandidate(
                id=SUCCESSOR_ID,
                type="decision",
                name="Deploy to Hetzner",
                content="we deploy to hetzner",
                score=1.0,
                source=None,
                metadata={},
                project_id="project_123",
            )
        ],
        limit=4,
    )

    assert [candidate.id for candidate in candidates] == []
    relation_calls = [
        params
        for query, params in client.calls
        if "FROM relates_to" in query and "source_uuids" in params
    ]
    assert relation_calls
    assert relation_calls[0]["exclude_relationship_names"] == ["SUPERSEDES"]


@pytest.mark.asyncio
async def test_explicit_supersedes_walk_still_reaches_the_retired_row() -> None:
    """A caller naming the predicate is asking for lineage, not for recall."""

    client = _SupersessionGraphClient()
    rows = await search_module._node_bfs_records(
        client=client,
        origin_uuids=[SUCCESSOR_ID],
        search_filter=search_module.SearchFilter(node_types=("decision",)),
        group_id="org-123",
        max_depth=1,
        limit=4,
        relationship_names=["SUPERSEDES"],
    )

    assert [row.get("uuid") for row in rows] == [SUPERSEDED_ID]


@pytest.mark.asyncio
async def test_successor_wins_admission_when_both_rows_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recency override: an inbound SUPERSEDES edge retires its own target."""

    client = _SupersessionGraphClient()

    class Runtime:
        pass

    Runtime.client = client  # type: ignore[attr-defined]

    async def fake_runtime(_organization_id: str, **_kwargs: object) -> Runtime:
        return Runtime()

    async def fake_node_fulltext(**_kwargs: object) -> list[RetrievalCandidate]:
        return [
            RetrievalCandidate(
                id=SUPERSEDED_ID,
                type="decision",
                name="Deploy to Fly",
                content="we deploy to fly.io",
                score=1.0,
                source=None,
                metadata={},
                project_id="project_123",
            ),
            RetrievalCandidate(
                id=SUCCESSOR_ID,
                type="decision",
                name="Deploy to Hetzner",
                content="we deploy to hetzner",
                score=0.9,
                source=None,
                metadata={},
                project_id="project_123",
            ),
        ]

    async def no_expansion(**_kwargs: object) -> list[RetrievalCandidate]:
        return []

    monkeypatch.setattr(search_module, "get_surreal_graph_runtime", fake_runtime)
    monkeypatch.setattr(search_module, "_node_fulltext_candidates", fake_node_fulltext)
    monkeypatch.setattr(search_module, "_graph_expansion_candidates", no_expansion)

    response = await search_module.context_search(
        plan=_plan(),
        types=["decision"],
        facet=ContextFacet.DECISIONS,
        limit=5,
        raw_memory_recall_fn=lambda **_kwargs: [],
    )

    assert [result.id for result in response.results] == [SUCCESSOR_ID]
    gate = response.filters["supersession_gate"]
    assert gate["superseded_dropped"] == 1
    assert gate["superseded_uuids"] == [SUPERSEDED_ID]


@pytest.mark.asyncio
async def test_corrected_row_is_refused_admission_even_with_a_winning_score(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A correction stamped onto the graph row takes it out of recall."""

    corrected = RetrievalCandidate(
        id=SUPERSEDED_ID,
        type="decision",
        name="Deploy to Fly",
        content="we deploy to fly.io",
        score=1.0,
        source=None,
        metadata={
            "lifecycle_state": "contested",
            "lifecycle_action": "mark_wrong",
            "excluded_from_recall": True,
        },
        project_id="project_123",
    )

    class Runtime:
        client = _SupersessionGraphClient()

    async def fake_runtime(_organization_id: str, **_kwargs: object) -> Runtime:
        return Runtime()

    async def fake_node_fulltext(**_kwargs: object) -> list[RetrievalCandidate]:
        return [corrected]

    async def no_expansion(**_kwargs: object) -> list[RetrievalCandidate]:
        return []

    monkeypatch.setattr(search_module, "get_surreal_graph_runtime", fake_runtime)
    monkeypatch.setattr(search_module, "_node_fulltext_candidates", fake_node_fulltext)
    monkeypatch.setattr(search_module, "_graph_expansion_candidates", no_expansion)

    response = await search_module.context_search(
        plan=_plan(),
        types=["decision"],
        facet=ContextFacet.DECISIONS,
        limit=5,
        raw_memory_recall_fn=lambda **_kwargs: [],
    )

    assert response.results == []
    assert response.filters["supersession_gate"]["lifecycle_dropped"] == 1


def _search_result(identifier: str, name: str, metadata: dict[str, Any]) -> SearchResult:
    return SearchResult(
        id=identifier,
        type="decision",
        name=name,
        content=f"{name} body",
        score=1.0,
        source=None,
        result_origin="graph",
        metadata={"entity_type": "decision", **metadata},
    )


@pytest.mark.asyncio
async def test_pack_admission_drops_a_corrected_item_it_was_handed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pack re-checks the verdict for rows the native gate never saw."""

    served: list[SearchResult] = [
        _search_result("decision-live", "Deploy to Hetzner", {}),
        _search_result("decision-wrong", "Deploy to Fly", {"lifecycle_state": "contested"}),
        _search_result(
            "decision-replaced",
            "Deploy to Heroku",
            {"superseded_by_source_id": "raw_memory:new"},
        ),
    ]

    async def fake_context_search(**_kwargs: object) -> SearchResponse:
        return SearchResponse(
            results=served,
            total=len(served),
            query="deployment target",
            filters={},
            graph_count=len(served),
            document_count=0,
            limit=len(served),
        )

    monkeypatch.setattr(context_module, "context_search", fake_context_search)

    pack = await compile_context(
        "deployment target",
        intent="decide",
        organization_id="org-123",
        record_exposure=False,
    )

    assert [item.id for item in pack.items] == ["decision-live"]
    assert pack.total_items == 1


def test_supersedes_weight_only_applies_where_the_edge_points_forward() -> None:
    """The 0.95 weight is not the bug; walking the edge outwards was."""

    assert search_module._GRAPH_EXPANSION_RELATIONSHIP_WEIGHTS["SUPERSEDES"] == 0.95
    assert search_module._SUPERSEDES_PREDICATE == "SUPERSEDES"


@pytest.mark.asyncio
async def test_supersession_lookup_failure_degrades_without_failing_the_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A gate that cannot read its edges still applies the metadata verdict."""

    async def exploding_lookup(*_args: object, **_kwargs: object) -> set[str]:
        raise RuntimeError("surreal is unhappy")

    monkeypatch.setattr(search_module, "_superseded_candidate_uuids", exploding_lookup)

    survivor = RetrievalCandidate(
        id=SUCCESSOR_ID,
        type="decision",
        name="Deploy to Hetzner",
        content="we deploy to hetzner",
        score=1.0,
        source=None,
        metadata={},
        project_id="project_123",
    )
    retired = RetrievalCandidate(
        id=SUPERSEDED_ID,
        type="decision",
        name="Deploy to Fly",
        content="we deploy to fly.io",
        score=0.9,
        source=None,
        metadata={"lifecycle_state": "superseded"},
        project_id="project_123",
    )

    surviving, metadata = await search_module._apply_supersession_gate(
        client=_SupersessionGraphClient(),
        group_id="org-123",
        source_lists=[(RetrievalSignal.NODE_FULLTEXT, [survivor, retired])],
    )

    assert [candidate.id for _signal, candidates in surviving for candidate in candidates] == [
        SUCCESSOR_ID
    ]
    assert metadata["supersession_gate"]["lifecycle_dropped"] == 1
    assert metadata["supersession_gate"]["lookup_error_type"] == "RuntimeError"


class _CorrectionGraphRuntime:
    """Minimal graph double: one projected entity per corrected capture."""

    def __init__(self, *, uuid: str = "entity-old") -> None:
        self.uuid = uuid
        self.updates: list[tuple[str, dict[str, Any]]] = []
        self.relationships: list[Any] = []
        self.client = self
        self.entity_manager = self
        self.relationship_manager = self

    async def execute_query(self, _query: str, **_params: object) -> list[dict[str, object]]:
        return [{"uuid": self.uuid}]

    async def update(self, entity_id: str, updates: dict[str, Any]) -> None:
        self.updates.append((entity_id, dict(updates["metadata"])))

    async def create(self, relationship: Any) -> str:
        self.relationships.append(relationship)
        return relationship.id


def _raw_capture(**overrides: Any) -> RawMemory:
    values: dict[str, Any] = {
        "id": "candidate-1",
        "organization_id": "org-1",
        "source_id": "source-1",
        "principal_id": "user-1",
        "memory_scope": MemoryScope.PRIVATE,
        "scope_key": None,
        "review_state": "pending",
        "entity_type": "decision",
        "title": "Decision: deploy to Fly",
        "raw_content": "We decided to deploy to fly.io.",
        "tags": ["decision"],
        "metadata": {"capture_surface": "reflection_candidate", "domain": "sibyl"},
        "provenance": {},
        "capture_surface": "reflection_candidate",
        "captured_at": datetime(2026, 5, 12, 12, 0, 0, tzinfo=UTC),
        "created_at": datetime(2026, 5, 12, 12, 0, 0, tzinfo=UTC),
    }
    values.update(overrides)
    return RawMemory(**values)


@pytest.mark.asyncio
async def test_correction_stamps_the_graph_row_so_recall_stops_serving_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`sibyl correct` reaches the entity, not just `raw_captures`."""

    memory = _raw_capture()
    runtime = _CorrectionGraphRuntime()

    async def fake_runtime(_organization_id: str, **_kwargs: object) -> _CorrectionGraphRuntime:
        return runtime

    monkeypatch.setattr(memory_module, "get_raw_memory", AsyncMock(return_value=memory))
    monkeypatch.setattr(memory_module, "get_raw_memory_by_source_id", AsyncMock())
    monkeypatch.setattr(
        memory_module,
        "save_raw_memory",
        AsyncMock(side_effect=lambda updated, **_kwargs: updated),
    )
    monkeypatch.setattr(memory_module, "get_surreal_graph_runtime", fake_runtime)

    result = await memory_module.apply_memory_correction(
        organization_id="org-1",
        source_id="source-1",
        principal_id="user-1",
        action="mark_wrong",
        reason="we moved off Fly",
    )

    assert result.applied
    assert result.affected_entity_ids == ["entity-old"]
    entity_id, stamped = runtime.updates[0]
    assert entity_id == "entity-old"
    assert stamped["lifecycle_state"] == "contested"
    assert stamped["excluded_from_recall"] is True

    # The stamp is exactly what the retrieval gate reads, so the same query
    # that ranked this row first now refuses it admission.
    assert graph_metadata_recallable(stamped) is False


@pytest.mark.asyncio
async def test_supersede_writes_the_replacement_edge_in_the_direction_retrieval_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Source is the survivor, target is the retired row."""

    memory = _raw_capture(id="source-1")
    replacement = _raw_capture(id="replacement-1", title="Deploy to Hetzner")
    runtime = _CorrectionGraphRuntime()
    seen_uuids = iter(["entity-old", "entity-new"])

    async def execute_query(_query: str, **_params: object) -> list[dict[str, object]]:
        return [{"uuid": next(seen_uuids)}]

    runtime.execute_query = execute_query  # type: ignore[method-assign]

    async def fake_runtime(_organization_id: str, **_kwargs: object) -> _CorrectionGraphRuntime:
        return runtime

    monkeypatch.setattr(
        memory_module,
        "get_raw_memory",
        AsyncMock(side_effect=[memory, replacement, memory]),
    )
    monkeypatch.setattr(
        memory_module,
        "get_raw_memory_by_source_id",
        AsyncMock(return_value=replacement),
    )
    monkeypatch.setattr(
        memory_module,
        "save_raw_memory",
        AsyncMock(side_effect=lambda updated, **_kwargs: updated),
    )
    monkeypatch.setattr(memory_module, "get_surreal_graph_runtime", fake_runtime)

    result = await memory_module.apply_memory_correction(
        organization_id="org-1",
        source_id="source-1",
        principal_id="user-1",
        action="supersede",
        replacement_source_id="replacement-1",
    )

    assert result.applied
    assert result.affected_entity_ids == ["entity-old"]
    assert len(runtime.relationships) == 1
    edge = runtime.relationships[0]
    assert edge.source_id == "entity-new"
    assert edge.target_id == "entity-old"
    assert edge.relationship_type is RelationshipType.SUPERSEDES


@pytest.mark.asyncio
async def test_restore_clears_the_graph_stamp_it_wrote(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Enforcement has to be reversible or `restore` is a lie."""

    memory = _raw_capture(
        review_state="hidden",
        metadata={
            "capture_surface": "reflection_candidate",
            "lifecycle_state": "active",
            "lifecycle_flags": ["hidden"],
        },
    )
    runtime = _CorrectionGraphRuntime()

    async def fake_runtime(_organization_id: str, **_kwargs: object) -> _CorrectionGraphRuntime:
        return runtime

    monkeypatch.setattr(memory_module, "get_raw_memory", AsyncMock(return_value=memory))
    monkeypatch.setattr(memory_module, "get_raw_memory_by_source_id", AsyncMock())
    monkeypatch.setattr(
        memory_module,
        "save_raw_memory",
        AsyncMock(side_effect=lambda updated, **_kwargs: updated),
    )
    monkeypatch.setattr(memory_module, "get_surreal_graph_runtime", fake_runtime)

    result = await memory_module.apply_memory_correction(
        organization_id="org-1",
        source_id="source-1",
        principal_id="user-1",
        action="restore",
    )

    assert result.applied
    _entity_id, stamped = runtime.updates[0]
    assert stamped["excluded_from_recall"] is False
    assert stamped["superseded_by_source_id"] == ""
    assert graph_metadata_recallable(stamped) is True


@pytest.mark.asyncio
async def test_correction_still_applies_when_the_graph_is_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The raw write already landed; a graph failure must not undo the report."""

    memory = _raw_capture()

    async def exploding_runtime(_organization_id: str, **_kwargs: object) -> Any:
        raise RuntimeError("no graph today")

    monkeypatch.setattr(memory_module, "get_raw_memory", AsyncMock(return_value=memory))
    monkeypatch.setattr(memory_module, "get_raw_memory_by_source_id", AsyncMock())
    monkeypatch.setattr(
        memory_module,
        "save_raw_memory",
        AsyncMock(side_effect=lambda updated, **_kwargs: updated),
    )
    monkeypatch.setattr(memory_module, "get_surreal_graph_runtime", exploding_runtime)

    result = await memory_module.apply_memory_correction(
        organization_id="org-1",
        source_id="source-1",
        principal_id="user-1",
        action="mark_stale",
    )

    assert result.applied
    assert result.affected_entity_ids == []
