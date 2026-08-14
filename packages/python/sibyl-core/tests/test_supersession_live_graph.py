"""Supersession enforcement exercised against a real embedded SurrealDB graph.

The unit suite in `test_supersession_enforcement.py` stubs the Surreal query,
which proves the Python branches but not the SQL, the edge direction as
actually persisted, or the admission lanes that rebuild item metadata from an
entity rather than from a search result. These tests write real rows and real
`relates_to` edges through the production managers and then read them back
through `context_search` and `compile_context`.

`compile_context` is the entry an agent actually reaches, and it is not a thin
wrapper: it picks the batch related-items lane, runs the active-work lookup,
asks for passages alongside every facet type, and applies its own admission
filter. Lanes tested one helper down have already shipped a hole the pack lane
still had, so the bypass tests here call the pack.
"""

from __future__ import annotations

import uuid as uuid_module
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

import sibyl_core.retrieval.search as search_module
import sibyl_core.services.memory as memory_module
import sibyl_core.tools.context as context_module
from sibyl_core.auth.memory_policy import stamp_memory_scope_metadata
from sibyl_core.models.context import ContextFacet, ContextPack
from sibyl_core.models.entities import Entity, EntityType, Relationship, RelationshipType
from sibyl_core.projection.memory import project_memory_entity
from sibyl_core.projection.passages import project_entity_passages
from sibyl_core.retrieval.search import build_context_retrieval_plan
from sibyl_core.services.graph import (
    EntityManager,
    RelationshipManager,
    SurrealGraphClient,
    prepare_graph_schema,
)
from sibyl_core.services.surreal_content import MemoryScope, RawMemory

PROJECT_ID = "proj-supersession"
PRINCIPAL = "user-supersession"


class _Runtime:
    def __init__(self, client: Any, entities: Any, relationships: Any, group_id: str) -> None:
        self.client = client
        self.entity_manager = entities
        self.relationship_manager = relationships
        self.group_id = group_id


@pytest_asyncio.fixture
async def graph(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[_Runtime]:
    # Each test gets its own namespace. The embedded store is process-wide, so
    # a shared group id leaks rows and edges between tests and turns a
    # supersession assertion into a function of test ordering.
    group_id = f"supersession-live-{uuid_module.uuid4().hex[:12]}"
    client = SurrealGraphClient(group_id=group_id, url="memory://")
    await client.connect()
    await prepare_graph_schema(client)
    runtime = _Runtime(
        client,
        EntityManager(client, group_id=group_id),
        RelationshipManager(client, group_id=group_id),
        group_id,
    )

    async def runtime_factory(requested_group_id: str, **_kwargs: object) -> _Runtime:
        assert str(requested_group_id) == group_id
        return runtime

    monkeypatch.setattr(search_module, "get_surreal_graph_runtime", runtime_factory)
    monkeypatch.setattr(context_module, "get_surreal_graph_runtime", runtime_factory)
    monkeypatch.setattr(context_module, "get_graph_runtime", runtime_factory, raising=False)
    monkeypatch.setattr(memory_module, "get_surreal_graph_runtime", runtime_factory)
    # No embedding provider in the embedded harness. Left alone, building one
    # can raise inside the native lane, and `compile_context` answers an
    # exception by falling back to a different search path, which would leave
    # every assertion below passing without the lane under test ever running.
    monkeypatch.setattr(context_module, "configured_embedding_provider", lambda: None)
    yield runtime
    await client.close()


async def _no_fallback(**_kwargs: object) -> list[Any]:
    """Make the pack's degraded path loud instead of silently substituting.

    `compile_context` swallows a native retrieval failure and recompiles
    through `search_fn`. A test that let it would assert against a lane it was
    not written for and pass while the real one was broken.
    """

    raise AssertionError("compile_context fell back to the non-native search path")


async def _no_active_work(**_kwargs: object) -> list[Any]:
    return []


async def _no_raw_memories(**_kwargs: object) -> list[Any]:
    """The raw-capture store is not wired up here; the graph lanes are."""

    return []


async def _pack(
    runtime: _Runtime,
    goal: str,
    *,
    include_related: bool = False,
    active_work_fn: Any = _no_active_work,
) -> ContextPack:
    """Build a pack the way the MCP surface does."""

    return await context_module.compile_context(
        goal,
        intent="build",
        project=PROJECT_ID,
        accessible_projects={PROJECT_ID},
        principal_id=PRINCIPAL,
        organization_id=runtime.group_id,
        include_related=include_related,
        related_limit=5,
        search_fn=_no_fallback,
        raw_memory_recall_fn=_no_raw_memories,
        active_work_fn=active_work_fn,
        record_exposure=False,
    )


def _pack_ids(pack: ContextPack) -> set[str]:
    return {item.id for section in pack.sections for item in section.items}


def _related_ids(pack: ContextPack) -> set[str]:
    return {
        related.id
        for section in pack.sections
        for item in section.items
        for related in item.related or ()
    }


def _entity(runtime: _Runtime, uuid: str, name: str, content: str, **metadata: Any) -> Entity:
    return Entity(
        id=uuid,
        name=name,
        entity_type=EntityType.DECISION,
        description=content,
        content=content,
        organization_id=runtime.group_id,
        metadata={
            "memory_scope": "project",
            "scope_key": PROJECT_ID,
            "project_id": PROJECT_ID,
            **metadata,
        },
        project_id=PROJECT_ID,
    )


async def _supersede(runtime: _Runtime, *, survivor: str, retired: str) -> None:
    """Write the edge in the direction the production write path uses."""

    await runtime.relationship_manager.create(
        Relationship(
            id=f"rel_{survivor}_supersedes_{retired}",
            source_id=survivor,
            target_id=retired,
            relationship_type=RelationshipType.SUPERSEDES,
            organization_id=runtime.group_id,
            metadata={"native_write_path": "memory_correction"},
        )
    )


def _plan(runtime: _Runtime, query: str) -> Any:
    return build_context_retrieval_plan(
        query=query,
        organization_id=runtime.group_id,
        facets=[ContextFacet.DECISIONS],
        facet_types={ContextFacet.DECISIONS: ["decision"]},
        principal_id=PRINCIPAL,
        project=PROJECT_ID,
        accessible_projects={PROJECT_ID},
        limit=12,
    )


async def _search(runtime: _Runtime, query: str) -> Any:
    return await search_module.context_search(
        plan=_plan(runtime, query),
        types=["decision"],
        facet=ContextFacet.DECISIONS,
        limit=10,
        include_content=True,
        raw_memory_recall_fn=_no_raw_memories,
    )


@pytest.mark.asyncio
async def test_a_real_supersedes_edge_retires_its_target_and_serves_the_successor(
    graph: _Runtime,
) -> None:
    """End to end through real SQL: the retired row leaves, the survivor stays."""

    await graph.entity_manager.create_direct(
        _entity(graph, "live-old", "Deploy to Fly", "we deploy to fly for hosting")
    )
    await graph.entity_manager.create_direct(
        _entity(graph, "live-new", "Deploy to Hetzner", "we deploy to hetzner for hosting")
    )
    await _supersede(graph, survivor="live-new", retired="live-old")

    response = await _search(graph, "deploy hosting")

    served = [result.id for result in response.results]
    assert "live-new" in served
    assert "live-old" not in served
    assert response.filters["supersession_gate"]["superseded_uuids"] == ["live-old"]


@pytest.mark.asyncio
async def test_a_real_supersession_cycle_does_not_black_out_both_rows(
    graph: _Runtime,
) -> None:
    """Mutual supersession leaves the newest statement standing, not nothing."""

    await graph.entity_manager.create_direct(
        _entity(graph, "cycle-a", "Cycle A", "cycle hosting decision alpha")
    )
    await graph.entity_manager.create_direct(
        _entity(graph, "cycle-b", "Cycle B", "cycle hosting decision beta")
    )
    await _supersede(graph, survivor="cycle-a", retired="cycle-b")
    await _supersede(graph, survivor="cycle-b", retired="cycle-a")

    response = await _search(graph, "cycle hosting decision")

    served = {result.id for result in response.results}
    assert served, "a supersession cycle must not black out every row"
    assert len(served & {"cycle-a", "cycle-b"}) == 1


@pytest.mark.asyncio
async def test_a_real_self_supersession_retires_nothing(graph: _Runtime) -> None:
    """A row that supersedes itself must stay servable."""

    await graph.entity_manager.create_direct(
        _entity(graph, "selfie", "Self Superseding", "self superseding hosting note")
    )
    await _supersede(graph, survivor="selfie", retired="selfie")

    response = await _search(graph, "self superseding hosting")

    assert "selfie" in {result.id for result in response.results}


@pytest.mark.asyncio
async def test_the_outgoing_walk_does_not_expand_into_a_real_retired_row(
    graph: _Runtime,
) -> None:
    """Traversal exclusion, proven against the real relates_to table."""

    await graph.entity_manager.create_direct(
        _entity(graph, "walk-new", "Walk Survivor", "walk survivor hosting body")
    )
    await graph.entity_manager.create_direct(
        _entity(graph, "walk-old", "Walk Retired", "walk retired hosting body")
    )
    await _supersede(graph, survivor="walk-new", retired="walk-old")

    hops = await search_module._node_bfs_records(
        client=graph.client,
        origin_uuids=["walk-new"],
        search_filter=search_module.SearchFilter(node_types=("decision",)),
        group_id=graph.group_id,
        max_depth=1,
        limit=10,
    )
    assert [row.get("uuid") for row in hops] == []

    # Naming the predicate is the documented carve-out for lineage walks.
    lineage = await search_module._node_bfs_records(
        client=graph.client,
        origin_uuids=["walk-new"],
        search_filter=search_module.SearchFilter(node_types=("decision",)),
        group_id=graph.group_id,
        max_depth=1,
        limit=10,
        relationship_names=["SUPERSEDES"],
    )
    assert [row.get("uuid") for row in lineage] == ["walk-old"]


@pytest.mark.asyncio
async def test_deleting_the_edge_restores_the_row_to_recall(graph: _Runtime) -> None:
    """The supersede/restore round trip, which is what makes correction reversible."""

    await graph.entity_manager.create_direct(
        _entity(graph, "round-old", "Round Retired", "round trip hosting body")
    )
    await graph.entity_manager.create_direct(
        _entity(graph, "round-new", "Round Survivor", "round trip hosting successor")
    )
    await _supersede(graph, survivor="round-new", retired="round-old")

    assert "round-old" not in {r.id for r in (await _search(graph, "round trip hosting")).results}

    await graph.client.execute_query(
        """
        DELETE FROM relates_to
        WHERE group_id = $group_id
          AND name = 'SUPERSEDES'
          AND target_id IN $target_ids
          AND attributes.native_write_path = 'memory_correction';
        """,
        group_id=graph.group_id,
        target_ids=["round-old"],
    )

    assert "round-old" in {r.id for r in (await _search(graph, "round trip hosting")).results}


@pytest.mark.asyncio
async def test_the_active_work_lane_cannot_smuggle_a_retired_task_into_a_pack(
    graph: _Runtime,
) -> None:
    """This lane rebuilds item metadata from the entity, so it needs its own carry.

    Driven through `compile_context` with the production active-work lookup, so
    the lookup query, the item builder and the admission filter are all the
    ones a pack really runs.
    """

    live = Entity(
        id="task-live",
        name="Live Task",
        entity_type=EntityType.TASK,
        description="live task body",
        content="live task body",
        organization_id=graph.group_id,
        status="doing",
        project_id=PROJECT_ID,
        metadata={"memory_scope": "project", "scope_key": PROJECT_ID, "project_id": PROJECT_ID},
    )
    retired = Entity(
        id="task-retired",
        name="Retired Task",
        entity_type=EntityType.TASK,
        description="retired task body",
        content="retired task body",
        organization_id=graph.group_id,
        status="doing",
        project_id=PROJECT_ID,
        metadata={
            "memory_scope": "project",
            "scope_key": PROJECT_ID,
            "project_id": PROJECT_ID,
            "lifecycle_state": "contested",
            "excluded_from_recall": True,
        },
    )
    await graph.entity_manager.create_direct(live)
    await graph.entity_manager.create_direct(retired)

    pack = await _pack(graph, "live task body", active_work_fn=None)

    served = _pack_ids(pack)
    assert "task-live" in served, "the live task proves the lane ran at all"
    assert "task-retired" not in served


@pytest.mark.asyncio
async def test_the_related_item_lane_does_not_attach_a_retired_neighbour(
    graph: _Runtime,
) -> None:
    """Related items ride inside an admitted item, so admission never sees them.

    Read through `compile_context`, which is what selects the batch lane. The
    singular helper had the gate while the batch one did not, so a test that
    called the helper directly reported a lane nothing in production reads.
    """

    await graph.entity_manager.create_direct(
        _entity(graph, "seed-row", "Seed Row", "seed row hosting body")
    )
    await graph.entity_manager.create_direct(
        _entity(graph, "neighbour-live", "Live Neighbour", "live neighbour body")
    )
    await graph.entity_manager.create_direct(
        _entity(
            graph,
            "neighbour-retired",
            "Retired Neighbour",
            "retired neighbour body",
            lifecycle_state="contested",
            excluded_from_recall=True,
        )
    )
    for target in ("neighbour-live", "neighbour-retired"):
        await graph.relationship_manager.create(
            Relationship(
                id=f"rel_seed_{target}",
                source_id="seed-row",
                target_id=target,
                relationship_type=RelationshipType.RELATED_TO,
                organization_id=graph.group_id,
                metadata={},
            )
        )

    pack = await _pack(graph, "seed row hosting body", include_related=True)

    assert "seed-row" in _pack_ids(pack)
    attached = _related_ids(pack)
    assert "neighbour-live" in attached, "the live neighbour proves the lane ran at all"
    assert "neighbour-retired" not in attached


def _passage_body(topic: str) -> str:
    """A body long enough that the production cutter really slices it."""

    return "\n\n".join(
        f"## {topic} section {index}\n\n" + f"{topic} passage body line {index} " * 40
        for index in range(4)
    )


async def _correct(
    graph: _Runtime,
    monkeypatch: pytest.MonkeyPatch,
    *,
    raw_memory_id: str,
    action: str = "mark_wrong",
) -> Any:
    """Run the production correction with only the raw-capture store stubbed.

    The graph half is entirely live: target resolution, the lineage cascade and
    the metadata stamp all run against real rows through the real managers,
    which is where the passage hole was.
    """

    memory = RawMemory(
        id=raw_memory_id,
        organization_id=graph.group_id,
        source_id=f"source-{raw_memory_id}",
        principal_id=PRINCIPAL,
        memory_scope=MemoryScope.PRIVATE,
        scope_key=None,
        review_state="pending",
        entity_type="decision",
        title="Deploy to Fly",
        raw_content="We decided to deploy to fly.io.",
        tags=["decision"],
        metadata={},
        provenance={},
        capture_surface="reflection_candidate",
    )
    monkeypatch.setattr(memory_module, "get_raw_memory", AsyncMock(return_value=memory))
    monkeypatch.setattr(memory_module, "get_raw_memory_by_source_id", AsyncMock())
    monkeypatch.setattr(
        memory_module,
        "save_raw_memory",
        AsyncMock(side_effect=lambda updated, **_kwargs: updated),
    )
    return await memory_module.apply_memory_correction(
        organization_id=graph.group_id,
        source_id=memory.source_id,
        principal_id=PRINCIPAL,
        action=action,
        accessible_projects=[PROJECT_ID],
    )


@pytest.mark.asyncio
async def test_correcting_a_memory_takes_its_passages_out_of_the_pack(
    graph: _Runtime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A correction has to reach the spans cut from the corrected body.

    A passage is an independently indexed copy of part of its parent, and it
    inherits scope but neither provenance nor lifecycle. So the correction's
    provenance query cannot see it, and a correction that stopped at the parent
    retired the memory while its own words kept ranking.
    """

    parent = _entity(
        graph,
        "passage-parent",
        "Deploy to Fly",
        _passage_body("hetzner"),
        raw_memory_id="raw-passage-parent",
    )
    await graph.entity_manager.create_direct(parent)
    projection = await project_entity_passages(
        entity_manager=graph.entity_manager,
        relationship_manager=graph.relationship_manager,
        source=parent,
        group_id=graph.group_id,
        generate_embeddings=False,
    )
    assert projection.passages >= 2, "the falsifier needs real spans to survive"
    passage_ids = {entity.id for entity in projection.created_passages}

    before = _pack_ids(await _pack(graph, "hetzner passage body"))
    assert passage_ids & before, "the spans have to be recallable before the correction"

    result = await _correct(graph, monkeypatch, raw_memory_id="raw-passage-parent")

    assert result.applied
    assert "passage-parent" in result.affected_entity_ids
    assert passage_ids <= set(result.affected_entity_ids)

    after = _pack_ids(await _pack(graph, "hetzner passage body"))
    assert "passage-parent" not in after
    assert not (passage_ids & after)


@pytest.mark.asyncio
async def test_spans_cut_after_the_correction_are_born_retired(
    graph: _Runtime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The window between writing a memory and cutting it into spans.

    Projection is two writes, not one. A correction that lands between them
    stamps the parent and finds no spans to cascade to, and the projection then
    cuts spans from the caller's copy of the parent, which predates the
    verdict. Reading the parent back at cut time is what closes it.
    """

    parent = _entity(
        graph,
        "raced-parent",
        "Deploy to Fly",
        _passage_body("interleaved"),
        raw_memory_id="raw-raced-parent",
    )
    await graph.entity_manager.create_direct(parent)

    result = await _correct(graph, monkeypatch, raw_memory_id="raw-raced-parent")
    assert result.applied
    assert result.affected_entity_ids == ["raced-parent"], (
        "no spans exist yet, so the cascade has nothing to reach"
    )

    # The caller's copy is the pre-correction one, exactly as the worker holds
    # it: the correction happened in another process and never touched it.
    projection = await project_entity_passages(
        entity_manager=graph.entity_manager,
        relationship_manager=graph.relationship_manager,
        source=parent,
        group_id=graph.group_id,
        generate_embeddings=False,
    )
    assert projection.passages >= 2, "the spans have to exist for the test to mean anything"
    for span in projection.created_passages:
        assert span.metadata.get("excluded_from_recall") is True
        assert span.metadata.get("lifecycle_state") == "contested"

    served = _pack_ids(await _pack(graph, "interleaved passage body"))
    assert "raced-parent" not in served
    assert not (served & {span.id for span in projection.created_passages})


_PROJECTION_BODY = (
    "We migrated the Hetzner cluster to Talos Linux on 2026-03-01. "
    "Bliss prefers Ratatui for terminal dashboards. "
    "The Grafana dashboard now reads from Prometheus."
)


def _projection_parent(runtime: _Runtime, entity_id: str, raw_memory_id: str) -> Entity:
    return Entity(
        id=entity_id,
        name="Migration session",
        entity_type=EntityType.EPISODE,
        description=_PROJECTION_BODY,
        content=_PROJECTION_BODY,
        organization_id=runtime.group_id,
        project_id=PROJECT_ID,
        metadata={
            "memory_scope": "project",
            "scope_key": PROJECT_ID,
            "project_id": PROJECT_ID,
            "raw_memory_id": raw_memory_id,
        },
    )


async def _project(runtime: _Runtime, parent: Entity) -> tuple[str, ...]:
    result = await project_memory_entity(
        entity_manager=runtime.entity_manager,
        relationship_manager=runtime.relationship_manager,
        source=parent,
        group_id=runtime.group_id,
        generate_embeddings=False,
    )
    rows = (
        *getattr(result, "created_projected_entities", ()),
        *getattr(result, "created_projected_facts", ()),
    )
    return tuple(str(row.id) for row in rows)


@pytest.mark.asyncio
async def test_correcting_a_memory_retires_the_entities_and_facts_projected_from_it(
    graph: _Runtime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spans are not the only rows carrying a memory's text.

    A projected entity copies the parent's candidate context and a projected
    fact copies its span, and both inherit scope but not provenance, so the
    correction's own query cannot see either. The cascade follows lineage
    rather than type for exactly this reason.
    """

    parent = _projection_parent(graph, "projection-parent", "raw-projection-parent")
    await graph.entity_manager.create_direct(parent)
    projected = await _project(graph, parent)
    assert projected, "the projection has to produce rows for this to mean anything"

    before = _pack_ids(await _pack(graph, "Grafana dashboard reads from Prometheus"))
    assert before & set(projected), "the projected rows have to be recallable first"

    result = await _correct(graph, monkeypatch, raw_memory_id="raw-projection-parent")
    assert result.applied
    assert set(projected) <= set(result.affected_entity_ids)

    for row_id in projected:
        stored = await graph.entity_manager.get(row_id)
        assert stored is not None
        assert stored.metadata.get("excluded_from_recall") is True

    after = _pack_ids(await _pack(graph, "Grafana dashboard reads from Prometheus"))
    assert not (after & set(projected))


@pytest.mark.asyncio
async def test_rows_projected_after_the_correction_are_born_retired(
    graph: _Runtime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same window the spans have, for the rows the other projection mints."""

    parent = _projection_parent(graph, "late-projection-parent", "raw-late-projection")
    await graph.entity_manager.create_direct(parent)

    result = await _correct(graph, monkeypatch, raw_memory_id="raw-late-projection")
    assert result.affected_entity_ids == ["late-projection-parent"], (
        "nothing is projected yet, so the cascade has nothing to reach"
    )

    # The caller's copy predates the verdict, exactly as the worker's does.
    projected = await _project(graph, parent)
    assert projected

    for row_id in projected:
        stored = await graph.entity_manager.get(row_id)
        assert stored is not None
        assert stored.metadata.get("excluded_from_recall") is True

    served = _pack_ids(await _pack(graph, "Grafana dashboard reads from Prometheus"))
    assert not (served & set(projected))


@pytest.mark.asyncio
async def test_a_row_written_through_add_is_reachable_by_a_correction(
    graph: _Runtime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The write path and the correction path have to agree on one key.

    Correction resolves its targets by querying `attributes.raw_memory_id`,
    and the graph writer strips that key from caller metadata because a caller
    that could set it could nominate somebody else's row. The capture pipeline
    stamps it and then calls that writer, so for a while every row written
    this way named no capture and no correction could find it. This walks the
    real writer, so a row that reaches the graph unnamed fails here.
    """

    import sibyl_core.tools.add as add_module

    async def runtime_factory(_group_id: str, **_kwargs: object) -> _Runtime:
        return graph

    monkeypatch.setattr(add_module, "get_graph_runtime", runtime_factory)

    response = await add_module.add(
        title="Deploy to Fly",
        content="we deploy to fly for hosting and it is written through the real writer",
        entity_type="decision",
        project=PROJECT_ID,
        memory_scope="project",
        scope_key=PROJECT_ID,
        principal_id=PRINCIPAL,
        check_conflicts=False,
        sync=True,
        generate_embeddings=False,
        metadata={"organization_id": graph.group_id},
        capture_provenance={"raw_memory_id": "raw-through-add"},
    )
    assert response.success and response.id

    stored = await graph.entity_manager.get(response.id)
    assert stored is not None
    assert stored.metadata.get("raw_memory_id") == "raw-through-add", (
        "the row has to name its capture or no correction can ever reach it"
    )

    before = _pack_ids(await _pack(graph, "deploy fly hosting written real writer"))
    assert response.id in before

    result = await _correct(graph, monkeypatch, raw_memory_id="raw-through-add")
    assert result.applied
    assert result.affected_entity_ids == [response.id]

    after = _pack_ids(await _pack(graph, "deploy fly hosting written real writer"))
    assert response.id not in after


@pytest.mark.asyncio
async def test_a_retired_row_cannot_seed_the_graph_walk(
    graph: _Runtime,
) -> None:
    """Dropping a retired row from the answer is not the same as not walking it.

    The neighbour passes its own admission check, so a walk seeded from a
    retired row still routes recall through a memory a writer has retired. The
    live control row is what makes the exclusion mean something: without it a
    walk that never ran would pass this test.
    """

    for prefix, extra in (("live", {}), ("dead", {"excluded_from_recall": True})):
        await graph.entity_manager.create_direct(
            _entity(
                graph,
                f"{prefix}-seed",
                f"{prefix.title()} Seed",
                f"{prefix} seed expansion hosting body",
                lifecycle_state="contested" if extra else "active",
                **extra,
            )
        )
        await graph.entity_manager.create_direct(
            _entity(
                graph,
                f"{prefix}-neighbour",
                f"{prefix.title()} Neighbour",
                f"{prefix} unrelated aardvark subject matter",
            )
        )
        await graph.relationship_manager.create(
            Relationship(
                id=f"rel_{prefix}_seed_neighbour",
                source_id=f"{prefix}-seed",
                target_id=f"{prefix}-neighbour",
                relationship_type=RelationshipType.RELATED_TO,
                organization_id=graph.group_id,
                metadata={},
            )
        )

    served = _pack_ids(await _pack(graph, "seed expansion hosting body"))

    assert "live-seed" in served
    assert "live-neighbour" in served, "the walk has to run for the exclusion to mean anything"
    assert "dead-seed" not in served
    assert "dead-neighbour" not in served


@pytest.mark.asyncio
async def test_an_exact_key_hit_on_a_retired_row_cannot_seed_the_walk(
    graph: _Runtime,
) -> None:
    """The exact-key lane picks its seed from the caller's own query text.

    That makes it the lane where seeding off a retired row is most reachable: a
    caller who knows the key gets the retired row's neighbourhood back without
    the retired row ever being served.
    """

    # Every row's vocabulary is disjoint from every other row's, and from both
    # probe keys. A shared word would let the lexical lane return the neighbour
    # on its own, and the test would then pass while the seed gate did nothing.
    rows = (
        ("live", "quartz-9001", "Quartz Anchor", "Mirrored Neighbour", "mirrored cabinet", {}),
        (
            "dead",
            "velvet-4242",
            "Lantern Anchor",
            "Harbour Neighbour",
            "harbour ferry timetable",
            {"excluded_from_recall": True},
        ),
    )
    for prefix, key, anchor_name, neighbour_name, neighbour_body, extra in rows:
        await graph.entity_manager.create_direct(
            _entity(
                graph,
                f"{prefix}-keyed",
                anchor_name,
                f"{key} anchor row",
                retrieval_keys=[key],
                lifecycle_state="contested" if extra else "active",
                **extra,
            )
        )
        await graph.entity_manager.create_direct(
            _entity(
                graph,
                f"{prefix}-keyed-neighbour",
                neighbour_name,
                neighbour_body,
            )
        )
        await graph.relationship_manager.create(
            Relationship(
                id=f"rel_{prefix}_keyed_neighbour",
                source_id=f"{prefix}-keyed",
                target_id=f"{prefix}-keyed-neighbour",
                relationship_type=RelationshipType.RELATED_TO,
                organization_id=graph.group_id,
                metadata={},
            )
        )

    live_served = _pack_ids(await _pack(graph, "quartz-9001"))
    assert "live-keyed" in live_served, "the probe has to fire for the exclusion to mean anything"
    assert "live-keyed-neighbour" in live_served, "and the walk has to reach the neighbour"

    dead_served = _pack_ids(await _pack(graph, "velvet-4242"))
    assert "dead-keyed" not in dead_served
    assert "dead-keyed-neighbour" not in dead_served


@pytest.mark.asyncio
async def test_a_real_supersession_cycle_resolves_the_same_way_in_either_order(
    graph: _Runtime,
) -> None:
    """Which row survives a mutual supersession is a property of the edges.

    Row order out of Surreal is not contractual, so a resolver that let the
    last row win would retire one row on this run and the other on the next,
    from data that never changed. Both edges are stamped at the same instant
    here, which is the case where only the tie-breaker decides.
    """

    stamped = datetime(2026, 3, 1, tzinfo=UTC)
    for row_id in ("tie-a", "tie-b"):
        await graph.entity_manager.create_direct(
            _entity(graph, row_id, row_id.title(), f"{row_id} tie hosting decision")
        )
    for survivor, retired in (("tie-a", "tie-b"), ("tie-b", "tie-a")):
        await graph.relationship_manager.create(
            Relationship(
                id=f"rel_{survivor}_supersedes_{retired}",
                source_id=survivor,
                target_id=retired,
                relationship_type=RelationshipType.SUPERSEDES,
                organization_id=graph.group_id,
                metadata={"native_write_path": "memory_correction"},
                created_at=stamped,
            )
        )

    rows = await search_module._execute_query_records(
        graph.client,
        """
        SELECT uuid, target_id, source_id, created_at
        FROM relates_to
        WHERE name = 'SUPERSEDES'
          AND target_id IN $uuids
          AND group_id = $group_id
        ORDER BY created_at, uuid;
        """,
        uuids=["tie-a", "tie-b"],
        group_id=graph.group_id,
    )
    assert len(rows) == 2, "both directions of the cycle must be on the table"
    forward = search_module._resolve_superseded(rows)
    reverse = search_module._resolve_superseded(list(reversed(rows)))
    assert forward == reverse
    assert len(forward) == 1

    served = _pack_ids(await _pack(graph, "tie hosting decision"))
    assert served & {"tie-a", "tie-b"} == {"tie-a", "tie-b"} - forward


def test_a_write_payload_cannot_supply_capture_provenance() -> None:
    """Provenance is what the correction write-through resolves its targets by.

    Leaving it caller-settable is what let a planted `raw_memory_id` nominate
    somebody else's row to be retired by a correction on an unrelated capture.
    """

    stamped = stamp_memory_scope_metadata(
        {
            "raw_memory_id": "victim-capture",
            "raw_source_id": "victim-source",
            "principal_id": "somebody-else",
            "note": "kept",
        },
        memory_scope="project",
        scope_key=PROJECT_ID,
        principal_id=PRINCIPAL,
    )

    assert "raw_memory_id" not in stamped
    assert "raw_source_id" not in stamped
    assert stamped["principal_id"] == PRINCIPAL
    assert stamped["note"] == "kept"


@pytest.mark.asyncio
async def test_capture_overwrites_planted_provenance_with_the_real_write() -> None:
    """The strip must neutralize planting without breaking the writer that needs it.

    Provenance is what the correction write-through resolves its targets by,
    so it has to be unforgeable, and it also has to still be there or the
    write-through finds nothing. Capture stamps both keys from the completed
    raw write after the strip runs, which satisfies both at once.
    """

    from sibyl_core.memory_pipeline.capture import MemoryCaptureRequest, MemoryCaptureService
    from sibyl_core.models.memory_scope import MemoryScope

    seen: dict[str, Any] = {}

    async def raw_writer(_request: Any) -> dict[str, Any]:
        return {"id": "raw-real", "source_id": "source-real"}

    async def graph_writer(_request: Any, graph_metadata: Any) -> dict[str, Any]:
        seen.update(graph_metadata)
        return {"id": "entity-1"}

    service = MemoryCaptureService(
        remember_raw_memory=raw_writer,
        create_graph_entity=graph_writer,
    )
    await service.capture(
        MemoryCaptureRequest(
            content="capture provenance body",
            title="Capture provenance",
            entity_type="note",
            memory_scope=MemoryScope.PRIVATE,
            scope_key=None,
            principal_id=PRINCIPAL,
            metadata={
                "raw_memory_id": "planted-victim-capture",
                "raw_source_id": "planted-victim-source",
                "unrelated": "kept",
            },
        )
    )

    assert seen["raw_memory_id"] == "raw-real"
    assert seen["raw_source_id"] == "source-real"
    assert seen["unrelated"] == "kept"


@pytest.mark.asyncio
async def test_a_stale_reconcile_cannot_overwrite_a_newer_correction(
    graph: _Runtime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The post-write pass is itself a read-modify-write, so it needs a fence.

    The schedule that breaks an unfenced pass: a correction retires the memory
    while the row is being written, the pass reads that verdict, and a restore
    lands before the pass gets to write. Last writer wins by default, so the
    pass would put the retirement back on a row somebody had just restored.

    The restore is injected between the pass's read and its write, which is the
    only interval where it can do that.
    """

    from sibyl_core.projection.reconcile import reconcile_with_capture

    retired = {
        "lifecycle_state": "contested",
        "excluded_from_recall": True,
    }
    restored = {
        "lifecycle_state": "active",
        "excluded_from_recall": False,
    }

    row = _entity(graph, "fenced-row", "Deploy to Fly", "fenced hosting body")
    await graph.entity_manager.create_direct(row)

    capture_verdict: dict[str, Any] = dict(retired)
    reads: list[str] = []

    async def verdict_then_restore(**_kwargs: object) -> dict[str, Any]:
        answer = dict(capture_verdict)
        reads.append(str(answer.get("lifecycle_state")))
        if answer.get("lifecycle_state") == "contested":
            # The restore lands between this pass's read and its write, and it
            # updates the capture and the row the way a real restore does.
            stored = await graph.entity_manager.get("fenced-row")
            await graph.entity_manager.update(
                "fenced-row",
                {"metadata": restored},
                expected_revision=stored.revision,
            )
            capture_verdict.clear()
            capture_verdict.update(restored)
        return answer

    monkeypatch.setattr(
        "sibyl_core.services.memory.projected_row_lifecycle_stamp",
        verdict_then_restore,
    )

    await reconcile_with_capture(
        graph.entity_manager,
        organization_id=graph.group_id,
        metadata={"raw_memory_id": "raw-fenced"},
        row_ids=["fenced-row"],
    )

    assert len(reads) > 1, "the refused write has to send the pass back for a fresh verdict"

    settled = await graph.entity_manager.get("fenced-row")
    assert settled.metadata.get("lifecycle_state") == "active", (
        "the newer correction has to survive a pass that started before it"
    )
    assert settled.metadata.get("excluded_from_recall") is False

    served = _pack_ids(await _pack(graph, "fenced hosting body"))
    assert "fenced-row" in served


@pytest.mark.asyncio
async def test_a_readable_verdict_clears_the_pending_marker(
    graph: _Runtime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The marker says nobody could check, so a check has to take it off.

    A marker that outlived the outage would keep a healthy row excluded
    forever, which turns a transient store blip into permanent data loss from
    the reader's point of view. It is asserted gone from the row rather than
    merely falsy, because the graph patch expresses removal as an explicit
    None and a key that merely reads falsy is still there for anything
    querying on presence.
    """

    from sibyl_core.projection.reconcile import RECONCILE_PENDING_KEY, reconcile_with_capture

    row = _entity(
        graph,
        "pending-row",
        "Deploy to Fly",
        "pending marker hosting body",
    )
    await graph.entity_manager.create_direct(row)
    await graph.entity_manager.update(
        "pending-row",
        {"metadata": {"excluded_from_recall": True, RECONCILE_PENDING_KEY: True}},
    )
    marked = await graph.entity_manager.get("pending-row")
    assert marked.metadata.get(RECONCILE_PENDING_KEY) is True
    assert "pending-row" not in _pack_ids(await _pack(graph, "pending marker hosting body"))

    async def healthy_verdict(**_kwargs: object) -> dict[str, Any]:
        return {}

    monkeypatch.setattr(
        "sibyl_core.services.memory.projected_row_lifecycle_stamp",
        healthy_verdict,
    )

    outcome = await reconcile_with_capture(
        graph.entity_manager,
        organization_id=graph.group_id,
        metadata={"raw_memory_id": "raw-pending"},
        row_ids=["pending-row"],
    )

    assert outcome.changed
    settled = await graph.entity_manager.get("pending-row")
    assert RECONCILE_PENDING_KEY not in settled.metadata, "the marker has to be gone, not falsy"
    assert settled.metadata.get("excluded_from_recall") is False
    assert "pending-row" in _pack_ids(await _pack(graph, "pending marker hosting body"))


@pytest.mark.asyncio
async def test_a_correction_clears_the_pending_marker_it_finds(
    graph: _Runtime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A correction is somebody reading the verdict, so it answers the marker."""

    from sibyl_core.projection.reconcile import RECONCILE_PENDING_KEY

    row = _entity(
        graph,
        "marked-parent",
        "Deploy to Fly",
        "marked parent hosting body",
        raw_memory_id="raw-marked-parent",
    )
    await graph.entity_manager.create_direct(row)
    await graph.entity_manager.update(
        "marked-parent",
        {"metadata": {RECONCILE_PENDING_KEY: True}},
    )

    result = await _correct(graph, monkeypatch, raw_memory_id="raw-marked-parent")
    assert result.applied
    assert "marked-parent" in result.affected_entity_ids

    settled = await graph.entity_manager.get("marked-parent")
    assert RECONCILE_PENDING_KEY not in settled.metadata
    assert settled.metadata.get("excluded_from_recall") is True
