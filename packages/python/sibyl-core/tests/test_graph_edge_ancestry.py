"""Relationship facts and navigation retain both current endpoint ancestors."""

import importlib
from unittest.mock import AsyncMock

import pytest

from sibyl_core.models.entities import Entity, EntityType, Relationship, RelationshipType
from sibyl_core.retrieval._search_lifecycle import _apply_supersession_gate
from sibyl_core.retrieval._search_plan import RetrievalSignal, build_context_retrieval_plan
from sibyl_core.retrieval.candidates import CandidateKind, RetrievalCandidate
from sibyl_core.tools.helpers import memory_scope_guard
from tests.test_graph_passage_derivations import published_passages
from tests.test_synthesis_source_observations import content_store as content_store
from tests.test_synthesis_source_observations import (
    disable_embeddings_and_bind_runtime as disable_embeddings_and_bind_runtime,
)
from tests.test_synthesis_source_observations import runtime as runtime

explore_module = importlib.import_module("sibyl_core.tools.explore")


def plan(org, principal="user_a"):
    return build_context_retrieval_plan(
        query="approval",
        organization_id=org,
        facets=[],
        facet_types={},
        principal_id=principal,
        project=None,
        accessible_projects=None,
    )


def candidate(edge):
    return RetrievalCandidate(
        id=edge.id,
        type="claim",
        name="RELATED_TO",
        content="Stored relationship fact",
        score=1,
        source=None,
        metadata={
            "stale_annotation": "old candidate bytes",
            "source_node_uuid": "forged-visible",
            "target_node_uuid": "forged-visible",
        },
        kind=CandidateKind.EDGE,
    )


async def edge_results(runtime, edge, principal="user_a"):
    lists, _receipt = await _apply_supersession_gate(
        client=runtime.client,
        group_id=runtime.client.group_id,
        source_lists=[(RetrievalSignal.EDGE_FULLTEXT, [candidate(edge)])],
        plan=plan(runtime.client.group_id, principal),
    )
    return lists[0][1]


async def related(runtime, seed, monkeypatch, principal="user_a"):
    monkeypatch.setattr(explore_module, "get_graph_runtime", AsyncMock(return_value=runtime))
    return await explore_module._explore_related(
        entity_id=seed,
        relationship_types=["RELATED_TO"],
        depth=4,
        limit=50,
        filters={},
        mode="related",
        group_id=runtime.client.group_id,
        scope_guard=memory_scope_guard(
            principal_id=principal, accessible_projects=None, allowed_memory_scope_keys=None
        ),
    )


async def test_graph_edge_protected_endpoints_and_seed_retire_together(
    runtime, content_store, monkeypatch
):
    source, target, _projection, passage = await published_passages(
        runtime, content_store, monkeypatch
    )
    edge = Relationship(
        id="protected-edge",
        relationship_type=RelationshipType.RELATED_TO,
        metadata={"fact": "Stored relationship fact"},
        source_id=target.id,
        target_id=passage.id,
    )
    await runtime.relationship_manager.create(edge)
    assert await edge_results(runtime, edge)
    assert (await related(runtime, target.id, monkeypatch)).total == 1
    assert not await edge_results(runtime, edge, "foreign_reader")
    assert (await related(runtime, target.id, monkeypatch, "foreign_reader")).total == 0
    await runtime.entity_manager.update(source.id, {"content": "Original evidence withdrawn"})
    assert not await edge_results(runtime, edge)
    assert (await related(runtime, target.id, monkeypatch)).total == 0


@pytest.mark.parametrize(
    "mutation",
    ["none", "missing", "retired", "edge_fact", "edge_scope", "edge_retired", "edge_attributes"],
)
async def test_graph_edge_ordinary_endpoints_remain_available(
    runtime, content_store, monkeypatch, mutation
):
    for key in ("ordinary-a", "ordinary-b"):
        await runtime.entity_manager.create_direct(
            Entity(id=key, name=key, entity_type=EntityType.PATTERN)
        )
    edge = Relationship(
        id="ordinary-edge",
        relationship_type=RelationshipType.RELATED_TO,
        metadata={"fact": "Stored relationship fact"},
        source_id="ordinary-a",
        target_id="ordinary-b",
    )
    await runtime.relationship_manager.create(edge)
    if mutation == "missing":
        await runtime.client.execute_query("DELETE entity WHERE uuid='ordinary-b';")
    elif mutation == "retired":
        await runtime.entity_manager.update(
            "ordinary-b", {"metadata": {"excluded_from_recall": True}}
        )
    elif mutation == "edge_fact":
        await runtime.client.execute_query(
            "UPDATE relates_to SET fact='Changed current fact' WHERE uuid=$id;", id=edge.id
        )
    elif mutation == "edge_scope":
        await runtime.client.execute_query(
            "UPDATE relates_to SET attributes.memory_scope='private', attributes.principal_id='other' WHERE uuid=$id;",
            id=edge.id,
        )
    elif mutation == "edge_attributes":
        await runtime.client.execute_query(
            "UPDATE relates_to SET attributes.source_ids=['current-ref'], attributes.audit_note='current metadata' WHERE uuid=$id;",
            id=edge.id,
        )
    elif mutation == "edge_retired":
        await runtime.client.execute_query(
            "UPDATE relates_to SET attributes.excluded_from_recall=true WHERE uuid=$id;", id=edge.id
        )
    actual = await edge_results(runtime, edge)
    assert bool(actual) == (mutation in {"none", "edge_fact", "edge_attributes"})
    if actual:
        assert "stale_annotation" not in actual[0].metadata
    if mutation == "edge_attributes":
        assert actual[0].metadata["source_ids"] == ["current-ref"]
        assert actual[0].metadata["audit_note"] == "current metadata"
    if mutation == "edge_fact":
        assert actual[0].content == "Changed current fact"
        assert actual[0].score == 1
        assert actual[0].metadata["source_node_uuid"] == "ordinary-a"
    assert bool((await related(runtime, "ordinary-a", monkeypatch)).total) == (
        mutation in {"none", "edge_fact", "edge_attributes"}
    )


@pytest.mark.parametrize("protected_side", ["source", "target"])
async def test_graph_edge_checks_each_actual_endpoint_even_with_forged_metadata(
    runtime, content_store, monkeypatch, protected_side
):
    source, target, _projection, _passage = await published_passages(
        runtime, content_store, monkeypatch
    )
    await runtime.entity_manager.create_direct(
        Entity(id="ordinary-neighbor", name="Ordinary", entity_type=EntityType.PATTERN)
    )
    endpoints = (
        (target.id, "ordinary-neighbor")
        if protected_side == "source"
        else ("ordinary-neighbor", target.id)
    )
    edge = Relationship(
        id="mixed-edge",
        relationship_type=RelationshipType.RELATED_TO,
        metadata={"fact": "Stored relationship fact"},
        source_id=endpoints[0],
        target_id=endpoints[1],
    )
    await runtime.relationship_manager.create(edge)
    assert await edge_results(runtime, edge)
    assert (await related(runtime, "ordinary-neighbor", monkeypatch)).total == 1
    await runtime.entity_manager.update(source.id, {"content": "Retired underlying evidence"})
    assert not await edge_results(runtime, edge)
    assert (await related(runtime, "ordinary-neighbor", monkeypatch)).total == 0


async def test_graph_edge_query_error_is_not_an_empty_available_set():
    from sibyl_core.backends.surreal.records import SurrealQueryError
    from sibyl_core.retrieval._search_lifecycle import _available_edge_endpoints

    class Client:
        async def execute_query(self, query, **params):
            return [{"status": "ERR", "result": "owned query denied"}]

    edge = Relationship(
        id="query-error",
        relationship_type=RelationshipType.RELATED_TO,
        source_id="a",
        target_id="b",
    )
    with pytest.raises(SurrealQueryError):
        await _available_edge_endpoints(Client(), "org", [candidate(edge)], plan("org"))


async def test_graph_explore_missing_seed_does_not_expand(runtime, content_store, monkeypatch):
    expansion = AsyncMock(side_effect=AssertionError("missing seed expanded"))
    monkeypatch.setattr(runtime.relationship_manager, "get_related_entities", expansion)
    assert (await related(runtime, "missing-seed", monkeypatch)).total == 0
    expansion.assert_not_awaited()


async def test_graph_edge_actual_context_search_rechecks_endpoint_ancestry(
    runtime, content_store, monkeypatch
):
    from dataclasses import replace

    from sibyl_core.retrieval import _search_database
    from sibyl_core.retrieval.search import context_search

    source, target, _projection, passage = await published_passages(
        runtime, content_store, monkeypatch
    )
    edge = Relationship(
        id="context-edge",
        relationship_type=RelationshipType.RELATED_TO,
        source_id=target.id,
        target_id=passage.id,
        metadata={"fact": "Deployment approval context fact"},
    )
    await runtime.relationship_manager.create(edge)
    monkeypatch.setattr(
        _search_database, "_get_read_only_graph_runtime", AsyncMock(return_value=runtime)
    )
    request = replace(
        plan(runtime.client.group_id),
        signals=(RetrievalSignal.EDGE_FULLTEXT,),
        graph_expansion_depth=0,
    )
    before = await context_search(plan=request, types=["relationship"], embedding_provider=None)
    assert edge.id in {row.id for row in before.results}
    await runtime.entity_manager.update(
        source.id, {"content": "Original approval no longer applies"}
    )
    after = await context_search(plan=request, types=["relationship"], embedding_provider=None)
    assert edge.id not in {row.id for row in after.results}
