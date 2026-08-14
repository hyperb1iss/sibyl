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
from types import SimpleNamespace
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
            if SUPERSEDED_ID not in targets:
                return []
            return [
                {
                    "uuid": f"rel_{SUCCESSOR_ID}_supersedes_{SUPERSEDED_ID}",
                    "target_id": SUPERSEDED_ID,
                    "source_id": SUCCESSOR_ID,
                    "created_at": "2026-01-01T00:00:00+00:00",
                }
            ]
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


@pytest.mark.asyncio
async def test_a_retired_row_does_not_spend_a_pack_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Gating ahead of selection is the difference between a full pack and a short one."""

    served: list[SearchResult] = [
        _search_result("decision-wrong", "Deploy to Fly", {"lifecycle_state": "contested"}),
        _search_result("decision-live", "Deploy to Hetzner", {}),
        _search_result("decision-also-live", "Deploy to Vultr", {}),
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
        limit=2,
        record_exposure=False,
    )

    # Both surviving rows are served. Dropping the retired row after selection
    # would have spent one of the two slots on it and returned a pack of one.
    assert sorted(item.id for item in pack.items) == ["decision-also-live", "decision-live"]
    assert pack.total_items == 2


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

    def __init__(
        self,
        *,
        uuid: str = "entity-old",
        missing: frozenset[str] = frozenset(),
        owner_id: str = "user-1",
        foreign: frozenset[str] = frozenset(),
    ) -> None:
        self.uuid = uuid
        self.missing = missing
        self.owner_id = owner_id
        self.foreign = foreign
        self.updates: list[tuple[str, dict[str, Any]]] = []
        self.relationships: list[Any] = []
        self.client = self
        self.entity_manager = self
        self.relationship_manager = self

    async def execute_query(self, _query: str, **_params: object) -> list[dict[str, object]]:
        return [{"uuid": self.uuid}]

    async def get(self, entity_id: str) -> Any:
        # A private row owned by somebody else is what the write check has to
        # refuse; everything else belongs to the correcting principal.
        owner = "user-intruder" if entity_id in self.foreign else self.owner_id
        return SimpleNamespace(
            id=entity_id,
            created_by=owner,
            metadata={"memory_scope": "private", "principal_id": owner},
        )

    async def update(self, entity_id: str, updates: dict[str, Any]) -> object | None:
        if entity_id in self.missing:
            return None
        self.updates.append((entity_id, dict(updates["metadata"])))
        return object()

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

    async def execute_query(query: str, **_params: object) -> list[dict[str, object]]:
        if "parent_entity_id" in query:
            # This capture projected no passages, so the lineage cascade finds
            # nothing. Answering it with a provenance row would hand the test a
            # span that was never cut.
            return []
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
async def test_a_span_takes_the_stamp_but_never_the_supersession_edge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The two halves of a correction reach different rows.

    A span has to leave recall with its parent, so it takes the lifecycle
    stamp. It must not take an edge: "this row replaced that one" is a claim a
    writer made about a memory, and minting it once per span would assert a
    replacement nobody declared, on up to `MAX_PASSAGES_PER_SOURCE` rows.
    """

    memory = _raw_capture(id="source-1")
    replacement = _raw_capture(id="replacement-1", title="Deploy to Hetzner")
    runtime = _CorrectionGraphRuntime()
    seen_uuids = iter(["entity-old", "entity-new"])

    async def execute_query(query: str, **_params: object) -> list[dict[str, object]]:
        if "parent_entity_id" in query:
            return [{"uuid": "passage-of-entity-old"}]
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
    assert result.affected_entity_ids == ["entity-old", "passage-of-entity-old"]
    stamped = {entity_id for entity_id, _updates in runtime.updates}
    assert stamped == {"entity-old", "passage-of-entity-old"}
    assert [edge.target_id for edge in runtime.relationships] == ["entity-old"]


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


@pytest.mark.asyncio
async def test_correction_resolves_only_its_own_capture_not_its_source_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A correction names one capture, so it must not reach sibling captures.

    `raw_captures.source_id` groups memories rather than identifying one
    (`idx_raw_captures_source` is not UNIQUE), so resolving graph rows through
    it would stamp projections the correction never declared as affected.
    """

    memory = _raw_capture(id="candidate-1", source_id="shared-source")
    runtime = _CorrectionGraphRuntime()
    queries: list[tuple[str, dict[str, Any]]] = []

    async def execute_query(query: str, **params: object) -> list[dict[str, object]]:
        queries.append((query, dict(params)))
        return [{"uuid": "entity-old"}]

    runtime.execute_query = execute_query  # type: ignore[method-assign]

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
        source_id="candidate-1",
        principal_id="user-1",
        action="mark_wrong",
    )

    assert result.applied
    query, params = queries[0]
    assert "raw_source_id" not in query
    assert params["raw_memory_id"] == "candidate-1"
    assert params["group_id"] == "org-1"
    assert params["limit"] == memory_module._GRAPH_CORRECTION_LOOKUP_LIMIT


@pytest.mark.asyncio
async def test_the_edge_lookup_is_not_fed_ids_that_cannot_be_edge_endpoints() -> None:
    """Raw memories and episodes are not entity uuids, so they stay out of the IN list."""

    seen_uuids: list[list[str]] = []

    class RecordingClient:
        async def execute_query(self, _query: str, **params: object) -> list[dict[str, object]]:
            seen_uuids.append(list(params.get("uuids") or ()))
            return []

    def candidate(identifier: str, entity_type: str) -> RetrievalCandidate:
        return RetrievalCandidate(
            id=identifier,
            type=entity_type,
            name=identifier,
            content="body",
            score=1.0,
            source=None,
            metadata={},
            project_id="project_123",
        )

    await search_module._apply_supersession_gate(
        client=RecordingClient(),
        group_id="org-123",
        source_lists=[
            (
                RetrievalSignal.NODE_FULLTEXT,
                [
                    candidate("entity-real", "decision"),
                    candidate("raw_memory:abc", "raw_memory"),
                    candidate("episode-1", "episode"),
                    candidate("rel-1", "relationship"),
                ],
            )
        ],
    )

    assert seen_uuids == [["entity-real"]]


@pytest.mark.asyncio
async def test_the_receipt_names_only_rows_that_were_really_stamped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`_correction_derived_ids` reports relationship ids too, and those are not entities.

    An id that matches no entity row must not reach the mutation receipt, or
    the receipt claims a write that never happened.
    """

    memory = _raw_capture(
        id="source-1",
        metadata={
            "capture_surface": "reflection_candidate",
            "promoted_entity_id": "entity-old",
            "relationship_ids": ["rel_entity-old_supersedes_entity-gone"],
        },
    )
    runtime = _CorrectionGraphRuntime(missing=frozenset({"rel_entity-old_supersedes_entity-gone"}))

    async def execute_query(_query: str, **_params: object) -> list[dict[str, object]]:
        return []

    runtime.execute_query = execute_query  # type: ignore[method-assign]

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
    )

    assert result.applied
    assert result.affected_entity_ids == ["entity-old"]


@pytest.mark.asyncio
async def test_a_correction_cannot_retire_an_entity_the_caller_cannot_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Capture metadata is caller pass-through, so a named target is not a trusted target.

    Without a per-target write check, a caller could plant another
    principal's entity id in their own capture's `promoted_entity_id`,
    correct that capture, and retire a row they have no write access to.
    """

    memory = _raw_capture(
        id="source-1",
        metadata={
            "capture_surface": "reflection_candidate",
            "promoted_entity_id": "entity-victim",
        },
    )
    runtime = _CorrectionGraphRuntime(foreign=frozenset({"entity-victim"}))

    async def execute_query(_query: str, **_params: object) -> list[dict[str, object]]:
        return [{"uuid": "entity-own"}]

    runtime.execute_query = execute_query  # type: ignore[method-assign]

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
    )

    assert result.applied
    # The caller's own projected row is stamped; the planted one is refused.
    assert result.affected_entity_ids == ["entity-own"]
    assert [entity_id for entity_id, _stamp in runtime.updates] == ["entity-own"]


def test_a_promoted_near_duplicate_survivor_stays_recallable() -> None:
    """Reflection's duplicate marker outlives the verdict that set it.

    `services/reflection.py` stamps `duplicate_of_source_id` on a candidate it
    thinks near-duplicates a prior memory, and `_promotion_lifecycle_metadata`
    resets the promoted row to ACTIVE by adding keys without removing that
    one. The survivor has no SUPERSEDES edge and is the only graph row for its
    content, so treating the residue as a verdict would make it invisible from
    birth.
    """

    promoted = {
        "lifecycle_state": "active",
        "lifecycle_action": "promote",
        "duplicate_of_source_id": "raw_memory:prior",
        "duplicate_reason": "near_normalized_text_duplicate",
    }
    assert graph_metadata_recallable(promoted) is True

    # The carve-out is scoped to that one key and to the ACTIVE state, so a
    # genuine mark_duplicate correction (which lands CONTESTED) still retires,
    # and no other marker is softened.
    assert graph_metadata_recallable({**promoted, "lifecycle_state": "contested"}) is False
    assert graph_metadata_recallable({**promoted, "excluded_from_recall": True}) is False
    assert (
        graph_metadata_recallable({**promoted, "superseded_by_source_id": "raw_memory:x"}) is False
    )


def test_dict_shaped_lifecycle_flags_are_not_read_as_set_flags() -> None:
    """Iterating a Mapping yields keys, so a dict flag bag would retire on a False value."""

    assert graph_metadata_recallable({"lifecycle_flags": {"hidden": False}}) is True
    assert graph_metadata_recallable({"lifecycle_flags": ["hidden"]}) is False
    assert graph_metadata_recallable({"lifecycle_flags": ("redacted",)}) is False
    assert graph_metadata_recallable({"lifecycle_flags": "sensitive"}) is False


@pytest.mark.asyncio
async def test_a_truncated_supersession_check_says_so_in_the_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cap fails open, so it must never fail silently."""

    monkeypatch.setattr(search_module, "_SUPERSESSION_LOOKUP_LIMIT", 2)

    async def empty_lookup(*_args: object, **_kwargs: object) -> tuple[set[str], int]:
        return set(), 0

    monkeypatch.setattr(search_module, "_superseded_candidate_uuids", empty_lookup)

    def candidate(identifier: str) -> RetrievalCandidate:
        return RetrievalCandidate(
            id=identifier,
            type="decision",
            name=identifier,
            content="body",
            score=1.0,
            source=None,
            metadata={},
            project_id="project_123",
        )

    _surviving, metadata = await search_module._apply_supersession_gate(
        client=_SupersessionGraphClient(),
        group_id="org-123",
        source_lists=[
            (RetrievalSignal.NODE_FULLTEXT, [candidate(f"entity-{index}") for index in range(5)])
        ],
    )

    gate = metadata["supersession_gate"]
    assert gate["truncated"] is True
    assert gate["checked_candidates"] == 2
    assert gate["total_candidates"] == 5


@pytest.mark.asyncio
async def test_an_untruncated_check_carries_no_truncation_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def empty_lookup(*_args: object, **_kwargs: object) -> tuple[set[str], int]:
        return set(), 0

    monkeypatch.setattr(search_module, "_superseded_candidate_uuids", empty_lookup)

    _surviving, metadata = await search_module._apply_supersession_gate(
        client=_SupersessionGraphClient(),
        group_id="org-123",
        source_lists=[
            (
                RetrievalSignal.NODE_FULLTEXT,
                [
                    RetrievalCandidate(
                        id="entity-1",
                        type="decision",
                        name="one",
                        content="body",
                        score=1.0,
                        source=None,
                        metadata={},
                        project_id="project_123",
                    )
                ],
            )
        ],
    )

    assert "truncated" not in metadata["supersession_gate"]


def test_an_active_state_never_rescues_a_flagged_or_genuinely_duplicate_row() -> None:
    """The ACTIVE carve-out has to be narrow, because ACTIVE is a common state.

    `_LEGACY_LIFECYCLE_STATES` maps hidden, redacted, and sensitive to ACTIVE
    and carries the verdict in the flags instead, while a real mark_duplicate
    correction maps to CONTESTED. So the carve-out can only ever reach a row
    that no correction touched.
    """

    for flag in ("hidden", "redacted", "sensitive"):
        assert (
            graph_metadata_recallable(
                {
                    "lifecycle_state": "active",
                    "lifecycle_flags": [flag],
                    "duplicate_of_source_id": "raw_memory:prior",
                }
            )
            is False
        )

    assert (
        graph_metadata_recallable(
            {
                "lifecycle_state": "contested",
                "lifecycle_action": "mark_duplicate",
                "duplicate_of_source_id": "raw_memory:prior",
            }
        )
        is False
    )

    # And a correction always stamps the explicit flag alongside the state, so
    # even an ACTIVE row it excluded stays excluded.
    assert (
        graph_metadata_recallable(
            {
                "lifecycle_state": "active",
                "excluded_from_recall": True,
                "duplicate_of_source_id": "raw_memory:prior",
            }
        )
        is False
    )


@pytest.mark.parametrize(
    ("scope_metadata", "reachable"),
    [
        pytest.param({}, True, id="legacy-no-scope"),
        pytest.param({"memory_scope": "private", "principal_id": "user-1"}, True, id="private-own"),
        pytest.param({"memory_scope": "project", "scope_key": "proj-1"}, True, id="project-member"),
        pytest.param(
            {"memory_scope": "private", "principal_id": "user-2"}, False, id="private-other"
        ),
        pytest.param(
            {"memory_scope": "project", "scope_key": "proj-9"}, False, id="project-outsider"
        ),
    ],
)
@pytest.mark.asyncio
async def test_a_correction_retires_only_projected_rows_it_can_still_see(
    monkeypatch: pytest.MonkeyPatch,
    scope_metadata: dict[str, Any],
    reachable: bool,
) -> None:
    """Provenance is server-owned now, but rows written before that are not.

    `raw_memory_id` was patchable until this branch, so a row can carry a
    planted capture id. The attack is to plant it on a row you can write, lose
    access, then correct your own capture to retire it. Requiring that the
    projected row still be readable by the correcting principal closes that
    without refusing anything legitimate, because a genuine projection
    inherits its capture's audience.

    The reachable cases are the ones that can actually be served: read
    authorization admits private, project, and the legacy fail-open, while
    organization, shared, and public all reach `scope_not_enabled`
    (`migrate/scope_backfill.py:70-79`), so no servable row carries them.
    """

    memory = _raw_capture(id="source-1")
    runtime = _CorrectionGraphRuntime()

    async def execute_query(_query: str, **_params: object) -> list[dict[str, object]]:
        return [{"uuid": "entity-projected"}]

    async def get(entity_id: str) -> Any:
        return SimpleNamespace(id=entity_id, created_by=None, metadata=dict(scope_metadata))

    runtime.execute_query = execute_query  # type: ignore[method-assign]
    runtime.get = get  # type: ignore[method-assign]

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
        accessible_projects={"proj-1"},
        action="mark_wrong",
    )

    assert result.applied
    if reachable:
        assert result.affected_entity_ids == ["entity-projected"]
        _entity_id, stamped = runtime.updates[0]
        assert graph_metadata_recallable(stamped) is False
    else:
        assert result.affected_entity_ids == []
        assert runtime.updates == []


@pytest.mark.asyncio
async def test_a_refused_target_is_reported_rather_than_silently_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A partial write must not answer like a complete one."""

    memory = _raw_capture(
        id="source-1",
        metadata={
            "capture_surface": "reflection_candidate",
            "promoted_entity_id": "entity-victim",
        },
    )
    runtime = _CorrectionGraphRuntime(foreign=frozenset({"entity-victim"}))

    async def execute_query(_query: str, **_params: object) -> list[dict[str, object]]:
        return [{"uuid": "entity-own"}]

    runtime.execute_query = execute_query  # type: ignore[method-assign]

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
    )

    assert result.affected_entity_ids == ["entity-own"]
    assert result.refused_entity_ids == ["entity-victim"]


@pytest.mark.asyncio
async def test_the_row_cap_is_detected_even_when_dedup_hides_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One retired row can carry many inbound edges, so rows bind before uuids do."""

    monkeypatch.setattr(search_module, "_SUPERSESSION_LOOKUP_LIMIT", 4)

    async def saturated_lookup(*_args: object, **_kwargs: object) -> tuple[set[str], int]:
        # Four rows read, all pointing at the same retired row.
        return {"entity-retired"}, 4

    monkeypatch.setattr(search_module, "_superseded_candidate_uuids", saturated_lookup)

    _surviving, metadata = await search_module._apply_supersession_gate(
        client=_SupersessionGraphClient(),
        group_id="org-123",
        source_lists=[
            (
                RetrievalSignal.NODE_FULLTEXT,
                [
                    RetrievalCandidate(
                        id="entity-retired",
                        type="decision",
                        name="retired",
                        content="body",
                        score=1.0,
                        source=None,
                        metadata={},
                        project_id="project_123",
                    )
                ],
            )
        ],
    )

    gate = metadata["supersession_gate"]
    assert gate["truncated"] is True
    assert gate["edge_rows_read"] == 4
    assert gate["total_candidates"] == 1


def test_a_self_supersession_retires_nothing() -> None:
    """A row replacing itself says nothing, and must not black itself out."""

    rows = [{"uuid": "edge-1", "target_id": "a", "source_id": "a"}]
    assert search_module._resolve_superseded(rows) == set()


def test_a_supersession_cycle_retires_only_the_newest_edges_target() -> None:
    """Mutual supersession must not black out both rows.

    A supersedes B, then B supersedes A. Retiring every inbound target would
    leave the reader with neither row, which is strictly worse than the
    pre-branch behavior of serving both.
    """

    retired = search_module._resolve_superseded(
        [
            {
                "uuid": "edge-a-supersedes-b",
                "target_id": "B",
                "source_id": "A",
                "created_at": "2026-01-01T00:00:00+00:00",
            },
            {
                "uuid": "edge-b-supersedes-a",
                "target_id": "A",
                "source_id": "B",
                "created_at": "2026-02-01T00:00:00+00:00",
            },
        ]
    )
    assert retired == {"A"}


def test_cycle_resolution_does_not_depend_on_the_order_rows_arrive_in() -> None:
    """The winner is a property of the edges, not of the query planner.

    Row order out of Surreal is not contractual, so a resolver that let the
    last row win would retire A on one run and B on the next from the same two
    edges. Equal timestamps are the case that exposes it, because then only
    the tie-breaker decides.
    """

    stamped = "2026-03-01T00:00:00+00:00"
    rows = [
        {
            "uuid": "edge-0001",
            "target_id": "B",
            "source_id": "A",
            "created_at": stamped,
        },
        {
            "uuid": "edge-0002",
            "target_id": "A",
            "source_id": "B",
            "created_at": stamped,
        },
    ]

    forward = search_module._resolve_superseded(rows)
    reverse = search_module._resolve_superseded(list(reversed(rows)))

    assert forward == reverse == {"A"}


def test_an_edge_with_no_recorded_source_still_retires_its_target() -> None:
    """Cycle resolution is a refinement, not a way to escape the gate."""

    assert search_module._resolve_superseded([{"target_id": "old"}]) == {"old"}


@pytest.mark.asyncio
async def test_restore_deletes_the_supersession_edge_the_correction_minted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Clearing the stamp is not enough while the gate reads the edge.

    The admission gate retires any row carrying an inbound SUPERSEDES edge, so
    a restore that left the edge behind would leave the row excluded forever
    and make the correction irreversible in practice.
    """

    memory = _raw_capture(
        id="source-1",
        review_state="superseded",
        metadata={
            "capture_surface": "reflection_candidate",
            "lifecycle_state": "superseded",
            "superseded_by_source_id": "replacement-1",
        },
    )
    runtime = _CorrectionGraphRuntime()
    deletes: list[tuple[str, dict[str, Any]]] = []

    async def execute_query(query: str, **params: object) -> list[dict[str, object]]:
        if query.strip().startswith("DELETE"):
            deletes.append((query, dict(params)))
            return []
        return [{"uuid": "entity-old"}]

    runtime.execute_query = execute_query  # type: ignore[method-assign]

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
    assert result.affected_entity_ids == ["entity-old"]
    assert len(deletes) == 1
    query, params = deletes[0]
    assert "relates_to" in query
    assert params["target_ids"] == ["entity-old"]
    assert params["predicate"] == "SUPERSEDES"
    # Only edges this path wrote are removed; a reflection-promoted
    # supersession is a different claim that restore has no opinion about.
    assert params["write_path"] == memory_module._CORRECTION_NATIVE_WRITE_PATH
    assert params["group_id"] == "org-1"
    _entity_id, stamped = runtime.updates[0]
    assert graph_metadata_recallable(stamped) is True


@pytest.mark.asyncio
async def test_a_missing_id_and_a_denied_id_are_reported_identically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`refused_entity_ids` echoes caller-chosen input, so it must not disclose existence.

    Capture metadata is caller-writable, so a caller can name any id. If a
    nonexistent id were dropped while an existing-but-denied id came back in
    the refused list, the difference would answer "does this row exist" for
    rows outside the caller's scope, one guess at a time.
    """

    async def run(named_id: str, *, exists: bool) -> list[str]:
        memory = _raw_capture(
            id="source-1",
            metadata={
                "capture_surface": "reflection_candidate",
                "promoted_entity_id": named_id,
            },
        )
        runtime = _CorrectionGraphRuntime(foreign=frozenset({named_id}))

        async def execute_query(_query: str, **_params: object) -> list[dict[str, object]]:
            return []

        async def get(entity_id: str) -> Any:
            if not exists:
                return None
            return SimpleNamespace(
                id=entity_id,
                created_by="user-intruder",
                metadata={"memory_scope": "private", "principal_id": "user-intruder"},
            )

        runtime.execute_query = execute_query  # type: ignore[method-assign]
        runtime.get = get  # type: ignore[method-assign]

        async def fake_runtime(_org: str, **_kwargs: object) -> _CorrectionGraphRuntime:
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
        )
        assert result.applied
        assert result.affected_entity_ids == []
        return result.refused_entity_ids

    denied = await run("entity-exists-denied", exists=True)
    missing = await run("entity-does-not-exist", exists=False)

    assert denied == ["entity-exists-denied"]
    assert missing == ["entity-does-not-exist"]


@pytest.mark.asyncio
async def test_a_synchronous_create_reconciles_the_capture_before_writing_the_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The queued path is not the only projection boundary.

    Sync creation runs after the capture is already durable, so a correction
    can land in between there too. The window is narrower than the worker's,
    not absent, and an unstamped row does not heal itself: the correction that
    would have stamped it has already run.
    """

    import sibyl_core.tools.add as add_module
    from sibyl_core.models.entities import Entity, EntityType

    monkeypatch.setattr(
        memory_module,
        "get_raw_memory",
        AsyncMock(
            return_value=_raw_capture(
                id="raw-corrected",
                metadata={"lifecycle_state": "contested", "lifecycle_action": "mark_wrong"},
            )
        ),
    )

    written: list[Entity] = []

    class _Manager:
        async def create_direct(self, entity: Entity, *, generate_embedding: bool = True) -> str:
            written.append(entity)
            return entity.id

    entity = Entity(
        id="sync-row",
        name="Deploy to Fly",
        entity_type=EntityType.DECISION,
        description="we deploy to fly",
        content="we deploy to fly",
        organization_id="org-1",
        metadata={"raw_memory_id": "raw-corrected"},
    )

    created_id = await add_module._create_entity_record(
        _Manager(),
        entity,
        generate_embeddings=False,
        organization_id="org-1",
    )

    assert created_id == "sync-row"
    assert written[0].metadata["excluded_from_recall"] is True
    assert written[0].metadata["lifecycle_state"] == "contested"
    assert graph_metadata_recallable(written[0].metadata) is False


@pytest.mark.asyncio
async def test_a_synchronous_create_reads_nothing_for_a_row_with_no_provenance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Most rows this path writes were never projected from a capture.

    No correction can ever name them, so charging every such write a capture
    read would buy nothing.
    """

    import sibyl_core.tools.add as add_module
    from sibyl_core.models.entities import Entity, EntityType

    lookup = AsyncMock(side_effect=AssertionError("no capture read should happen"))
    monkeypatch.setattr(memory_module, "get_raw_memory", lookup)

    class _Manager:
        async def create_direct(self, entity: Entity, *, generate_embedding: bool = True) -> str:
            return entity.id

    entity = Entity(
        id="plain-row",
        name="Plain",
        entity_type=EntityType.DECISION,
        description="plain body",
        content="plain body",
        organization_id="org-1",
        metadata={},
    )

    assert (
        await add_module._create_entity_record(
            _Manager(),
            entity,
            generate_embeddings=False,
            organization_id="org-1",
        )
        == "plain-row"
    )
    lookup.assert_not_awaited()


@pytest.mark.asyncio
async def test_only_the_server_channel_can_put_provenance_on_a_written_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Provenance has to be unforgeable and it has to arrive, which pull apart.

    A caller that could set `raw_memory_id` could nominate any row it can write
    to be retired by a correction on an unrelated capture, so the graph writer
    strips it from caller metadata. The capture pipeline stamps it from the
    completed raw write and then calls that same writer, so without a channel
    that survives the strip the authoritative id never reaches the row and both
    the correction write-through and the projection boundary lose their only
    link back to the capture.
    """

    import sibyl_core.tools.add as add_module

    written: list[dict[str, Any]] = []

    class _Queue:
        async def enqueue_create_entity(
            self, *, entity_data: dict[str, Any], **_kwargs: object
        ) -> str:
            written.append(dict(entity_data))
            return "job-1"

    monkeypatch.setattr(add_module, "get_queue_port", lambda: _Queue())

    async def write(**kwargs: Any) -> Any:
        return await add_module.add(
            title="Deploy",
            content="we deploy to fly",
            entity_type="note",
            check_conflicts=False,
            metadata={"organization_id": "org-1", "raw_memory_id": "victim-capture"},
            **kwargs,
        )

    assert (await write()).success
    planted = written[-1]["metadata"]
    assert "raw_memory_id" not in planted, "caller metadata cannot name a capture"

    assert (await write(capture_provenance={"raw_memory_id": "real-capture"})).success
    stamped = written[-1]["metadata"]
    assert stamped["raw_memory_id"] == "real-capture", "the server channel has to survive"


@pytest.mark.asyncio
async def test_the_lineage_walk_pages_past_a_single_query_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One parent can carry more projected rows than one page holds.

    Spans alone go to `MAX_PASSAGES_PER_SOURCE` per parent, and projected
    entities and facts land beside them, so a fixed multiplier is a guess that
    goes stale the moment any projection widens. A walk that stopped at one
    page would leave the overflow servable, which is the whole defect being
    fixed, just quieter.
    """

    page_size = memory_module._GRAPH_CORRECTION_PROJECTION_PAGE_SIZE
    total = page_size + 7
    all_ids = [f"projected-{index:05d}" for index in range(total)]
    cursors: list[str] = []

    class _Runtime:
        def __init__(self) -> None:
            self.client = self
            self.entity_manager = self

        async def execute_query(self, query: str, **params: object) -> list[dict[str, object]]:
            if "parent_entity_id" not in query:
                return [{"uuid": "parent-1"}]
            cursor = str(params.get("cursor") or "")
            cursors.append(cursor)
            after = [row_id for row_id in all_ids if row_id > cursor]
            return [{"uuid": row_id} for row_id in after[:page_size]]

        async def get(self, entity_id: str) -> Any:
            return SimpleNamespace(
                id=entity_id,
                created_by="user-1",
                metadata={"memory_scope": "private", "principal_id": "user-1"},
            )

    runtime = _Runtime()
    memory = _raw_capture()

    targets = await memory_module._correction_graph_entity_ids(
        runtime,
        organization_id="org-1",
        memory=memory,
        principal_id="user-1",
        accessible_projects=None,
    )

    assert len(cursors) > 1, "one page cannot cover a parent's whole projection"
    assert len(targets.projections) == total
    assert targets.projections[-1] == all_ids[-1]


@pytest.mark.asyncio
async def test_an_unreadable_verdict_retires_the_row_instead_of_poisoning_the_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A store that cannot be reached must not decide by default.

    Raising looks like the safe answer and is not: the local broker records a
    failed job as COMPLETE with an error and then suppresses the same
    deterministic job id, so an exception is a poison pill rather than a retry.
    Leaving the row servable is worse still, because nobody comes back for it.
    The row is excluded and flagged instead, which is visible and reversible.
    """

    from sibyl_core.projection.reconcile import (
        RECONCILE_MAX_ATTEMPTS,
        RECONCILE_PENDING_KEY,
        reconcile_with_capture,
    )

    attempts: list[int] = []
    stamped: list[tuple[str, dict[str, Any]]] = []

    async def exploding_lookup(**_kwargs: object) -> Any:
        attempts.append(1)
        msg = "content store unreachable"
        raise ConnectionError(msg)

    monkeypatch.setattr(memory_module, "get_raw_memory", exploding_lookup)
    monkeypatch.setattr("asyncio.sleep", AsyncMock())

    class _Manager:
        async def get(self, entity_id: str) -> Any:
            return SimpleNamespace(id=entity_id, metadata={}, revision=1)

        async def update(
            self,
            entity_id: str,
            updates: dict[str, Any],
            *,
            expected_revision: int | None = None,
        ) -> object:
            stamped.append((entity_id, dict(updates["metadata"])))
            return object()

    outcome = await reconcile_with_capture(
        _Manager(),
        organization_id="org-1",
        metadata={"raw_memory_id": "raw-1"},
        row_ids=["row-1"],
    )

    assert len(attempts) == RECONCILE_MAX_ATTEMPTS, "transient failures are retried, bounded"
    assert outcome.unverified == 1
    assert stamped == [("row-1", {"excluded_from_recall": True, RECONCILE_PENDING_KEY: True})]
    assert graph_metadata_recallable(stamped[0][1]) is False


@pytest.mark.asyncio
async def test_an_absent_parent_is_not_treated_as_an_unreadable_one() -> None:
    """The real manager raises KeyError for a row that is not there.

    Nothing was stamped on a row that does not exist, so there is no verdict to
    inherit and nothing to fail over. Treating absence as failure would retire
    every projection whose parent had not been written yet.
    """

    from sibyl_core.models.entities import Entity, EntityType
    from sibyl_core.projection.inheritance import parent_lifecycle_as_stored

    class _Manager:
        async def get(self, entity_id: str) -> Any:
            raise KeyError(entity_id)

    source = Entity(
        id="child-1",
        name="Child",
        entity_type=EntityType.NOTE,
        description="body",
        content="body",
        organization_id="org-1",
        metadata={"memory_scope": "project"},
    )

    resolved = await parent_lifecycle_as_stored(_Manager(), source, source_id="missing-parent")

    assert resolved is source


@pytest.mark.asyncio
async def test_a_walk_that_hits_its_ceiling_reports_a_partial_correction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A correction that could not reach every projected row must say so.

    The page ceiling is a stop, not a guarantee. A caller told "applied" with
    no qualifier would have no way to learn that rows projected from this
    capture are still servable, which is the same silence the truncation
    receipt on the retrieval gate exists to prevent.
    """

    page_size = memory_module._GRAPH_CORRECTION_PROJECTION_PAGE_SIZE
    max_pages = memory_module._GRAPH_CORRECTION_PROJECTION_MAX_PAGES

    class _Runtime:
        def __init__(self) -> None:
            self.client = self
            self.entity_manager = self

        async def execute_query(self, query: str, **params: object) -> list[dict[str, object]]:
            if "parent_entity_id" not in query:
                return [{"uuid": "parent-1"}]
            # Always a full page, so the walk can only stop at its ceiling.
            cursor = str(params.get("cursor") or "")
            start = int(cursor.rsplit("-", 1)[-1]) + 1 if cursor else 0
            return [{"uuid": f"projected-{index:07d}"} for index in range(start, start + page_size)]

        async def get(self, entity_id: str) -> Any:
            return SimpleNamespace(
                id=entity_id,
                created_by="user-1",
                metadata={"memory_scope": "private", "principal_id": "user-1"},
            )

    targets = await memory_module._correction_graph_entity_ids(
        _Runtime(),
        organization_id="org-1",
        memory=_raw_capture(),
        principal_id="user-1",
        accessible_projects=None,
    )

    assert targets.truncated is True
    assert len(targets.projections) == page_size * max_pages
