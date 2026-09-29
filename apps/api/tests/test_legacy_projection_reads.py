"""Legacy projection reads require current source evidence and its audience."""

import os
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from sibyl.api.routes import entity_contracts
from sibyl.api.routes.entity_reads import get_entity, list_entities
from sibyl.persistence.graph_runtime import GraphQueryAdapter, GraphReadServiceAdapter
from sibyl_core.backends.surreal import SurrealContentClient
from sibyl_core.backends.surreal.content_schema import bootstrap_content_schema
from sibyl_core.models.entities import Entity, EntityType, Relationship, RelationshipType
from sibyl_core.services import content_client
from sibyl_core.services.graph_client import SurrealGraphClient, prepare_graph_schema
from sibyl_core.services.graph_entities import EntityManager
from sibyl_core.services.graph_relationships import RelationshipManager
from sibyl_core.services.graph_runtime import GraphRuntime
from sibyl_core.services.surreal_content import remember_raw_memory
from tests.harness.auth import stub_auth_context


@pytest.fixture
async def legacy_store(monkeypatch):
    owner = stub_auth_context(organization_id=uuid4())
    outsider = stub_auth_context(organization_id=owner.organization_id, user_id=uuid4())
    url = os.environ.get("SIBYL_LEGACY_SURREAL_URL", "memory://")
    credentials = {"username": "root", "password": "root"} if url != "memory://" else {}
    graph = SurrealGraphClient(group_id=owner.organization_id, url=url, **credentials)
    content = SurrealContentClient(url=url, namespace="sibyl_legacy_test", **credentials)
    try:
        await prepare_graph_schema(graph)
        await bootstrap_content_schema(content, reset=True)
        runtime = GraphRuntime(
            graph,
            EntityManager(graph, group_id=owner.organization_id),
            RelationshipManager(graph, group_id=owner.organization_id),
        )

        @asynccontextmanager
        async def session():
            yield content

        monkeypatch.setattr(content_client, "surreal_content_client", session)
        for path in (
            "sibyl_core.services.graph_runtime.get_surreal_graph_runtime",
            "sibyl_core.services.memory_lifecycle.get_surreal_graph_runtime",
            "sibyl.api.routes.entity_policy.get_entity_graph_runtime",
        ):
            monkeypatch.setattr(path, AsyncMock(return_value=runtime))
        monkeypatch.setattr(
            "sibyl.api.routes.entity_policy.list_accessible_project_graph_ids",
            AsyncMock(return_value={"project-a"}),
        )
        monkeypatch.setattr(
            "sibyl.api.routes.entity_policy.verify_entity_project_access", AsyncMock()
        )
        root = await remember_raw_memory(
            organization_id=owner.organization_id,
            principal_id=owner.user_id,
            source_id="nonunique-source-group",
            raw_content="Private source words",
            embedding_provider=None,
        )
        for entity in (
            Entity(
                id="private-parent",
                entity_type=EntityType.NOTE,
                name="Private source",
                content="Private source words",
                metadata={
                    "raw_memory_id": root.id,
                    "memory_scope": "private",
                    "principal_id": owner.user_id,
                },
            ),
            Entity(
                id="legacy-projection",
                entity_type=EntityType.PASSAGE,
                name="Derived private words",
                content="Private source words",
                metadata={
                    "projection_kind": "passage",
                    "parent_entity_id": "private-parent",
                    "source_entity_id": "private-parent",
                    "memory_scope": "project",
                    "scope_key": "project-a",
                    "project_id": "project-a",
                },
            ),
        ):
            await runtime.entity_manager.create_direct(entity, generate_embedding=False)
        await runtime.entity_manager.create_direct(
            Entity(id="ordinary-org-resource", entity_type=EntityType.NOTE, name="Shared resource"),
            generate_embedding=False,
        )
        await runtime.relationship_manager.create_direct_bulk(
            [
                Relationship(
                    id="visible-related",
                    source_id="ordinary-org-resource",
                    target_id="legacy-projection",
                    relationship_type=RelationshipType.RELATED_TO,
                )
            ]
        )
        yield SimpleNamespace(
            owner=owner,
            outsider=outsider,
            org=owner.organization,
            runtime=runtime,
            root=root,
            service=GraphReadServiceAdapter.from_runtime(runtime, owner.organization_id),
        )
    finally:
        await content.close()
        await graph.close()


async def test_legacy_projection_detail_denies_private_parent_to_project_outsider(legacy_store):
    store = legacy_store
    with pytest.raises(HTTPException) as denied:
        await get_entity(
            "legacy-projection", org=store.org, ctx=store.outsider, service=store.service
        )
    assert denied.value.status_code == 404


async def test_legacy_projection_detail_allows_private_owner_with_full_grants(legacy_store):
    store = legacy_store
    result = await get_entity(
        "legacy-projection", org=store.org, ctx=store.owner, service=store.service
    )
    assert result.content == "Private source words"


@pytest.mark.parametrize("reader", ["owner", "outsider"])
async def test_legacy_projection_list_preserves_private_parent_audience(legacy_store, reader):
    store = legacy_store
    result = await list_entities(
        org=store.org,
        ctx=getattr(store, reader),
        entity_type=None,
        language=None,
        category=None,
        search=None,
        project_ids=None,
        page=1,
        page_size=50,
        sort_by=entity_contracts.SortField.UPDATED_AT,
        sort_order=entity_contracts.SortOrder.DESC,
    )
    ids = {row.id for row in result.entities}
    assert "ordinary-org-resource" in ids
    assert ("legacy-projection" in ids) is (reader == "owner")


@pytest.mark.parametrize("reader", ["owner", "outsider"])
async def test_legacy_related_summary_preserves_private_parent_audience(legacy_store, reader):
    store = legacy_store
    result = await get_entity(
        "ordinary-org-resource",
        org=store.org,
        ctx=getattr(store, reader),
        service=store.service,
        related_limit=10,
    )
    ids = {row.id for row in result.related or []}
    assert ("legacy-projection" in ids) is (reader == "owner")


async def test_private_owner_without_private_credential_grant_cannot_read_projection(legacy_store):
    store = legacy_store
    restricted = replace(store.owner, api_key_memory_scope_keys=frozenset({"project:project-a"}))
    with pytest.raises(HTTPException) as denied:
        await get_entity("legacy-projection", org=store.org, ctx=restricted, service=store.service)
    assert denied.value.status_code == 404


@pytest.mark.parametrize("reader", ["owner", "outsider"])
async def test_native_search_gate_preserves_private_parent_audience(legacy_store, reader):
    from sibyl_core.models.context import ContextFacet
    from sibyl_core.retrieval._search_lifecycle import _apply_supersession_gate
    from sibyl_core.retrieval._search_plan import RetrievalSignal, build_context_retrieval_plan
    from sibyl_core.retrieval.candidates import RetrievalCandidate

    store = legacy_store
    ctx = getattr(store, reader)
    entity = await store.runtime.entity_manager.get("legacy-projection")
    candidate = RetrievalCandidate(
        id=entity.id,
        type=entity.entity_type.value,
        name=entity.name,
        content=entity.content,
        score=1.0,
        source=None,
        metadata=entity.metadata,
        project_id="project-a",
    )
    plan = build_context_retrieval_plan(
        query="Private source",
        organization_id=str(store.org.id),
        facets=(ContextFacet.PRIOR_ART,),
        facet_types={},
        principal_id=str(ctx.user.id),
        project=None,
        accessible_projects={"project-a"},
    )
    result, _receipt = await _apply_supersession_gate(
        client=store.runtime.client,
        group_id=str(store.org.id),
        plan=plan,
        source_lists=[(RetrievalSignal.NODE_FULLTEXT, [candidate])],
    )
    assert bool(result[0][1]) is (reader == "owner")


async def test_canonical_capture_scope_overrules_inaccurate_graph_parent_scope(legacy_store):
    store = legacy_store
    await store.runtime.entity_manager.update(
        "private-parent",
        {
            "metadata": {
                "raw_memory_id": store.root.id,
                "memory_scope": "project",
                "scope_key": "project-a",
                "project_id": "project-a",
            }
        },
    )
    with pytest.raises(HTTPException) as denied:
        await get_entity(
            "legacy-projection", org=store.org, ctx=store.outsider, service=store.service
        )
    assert denied.value.status_code == 404


@pytest.mark.parametrize("action", ["hide", "revise"])
async def test_rest_legacy_projection_uses_capture_history_after_failed_graph_stamps(
    legacy_store, monkeypatch, action
):
    from sibyl_core.services.memory_correction import apply_memory_correction

    store = legacy_store
    before = await store.runtime.entity_manager.get("legacy-projection")
    monkeypatch.setattr(
        store.runtime.entity_manager,
        "update",
        AsyncMock(side_effect=RuntimeError("graph stamps unavailable")),
    )
    for correction in (action, "restore"):
        result = await apply_memory_correction(
            organization_id=str(store.org.id),
            source_id=store.root.id,
            principal_id=str(store.owner.user.id),
            action=correction,
            **({"revised_content": "Revised source words"} if correction == "revise" else {}),
        )
        assert result.applied
        assert not result.propagation_complete
        if correction == "restore" and action == "hide":
            readable = await get_entity(
                "legacy-projection", org=store.org, ctx=store.owner, service=store.service
            )
            assert readable.content == "Private source words"
        else:
            with pytest.raises(HTTPException) as denied:
                await get_entity(
                    "legacy-projection", org=store.org, ctx=store.owner, service=store.service
                )
            assert denied.value.status_code == 404
    after = await store.runtime.entity_manager.get("legacy-projection")
    assert before.model_dump() == after.model_dump()


@pytest.mark.parametrize("reader", ["owner", "outsider"])
async def test_visible_graph_snapshot_preserves_private_parent_audience(legacy_store, reader):
    from sibyl_core.services.graph_community_snapshot import _get_visible_graph_snapshot

    store = legacy_store
    snapshot = await _get_visible_graph_snapshot(
        store.runtime.client,
        str(store.org.id),
        principal_id=str(getattr(store, reader).user.id),
        accessible_projects={"project-a"},
    )
    assert "ordinary-org-resource" in snapshot.entity_by_id
    assert ("legacy-projection" in snapshot.entity_by_id) is (reader == "owner")


@pytest.mark.parametrize("reader", ["owner", "outsider"])
@pytest.mark.parametrize("mode", ["list", "related"])
async def test_mcp_explore_preserves_private_parent_audience(legacy_store, reader, mode):
    from sibyl_core.tools.explore import explore

    store = legacy_store
    result = await explore(
        mode=mode,
        types=["passage"] if mode == "list" else None,
        entity_id="ordinary-org-resource" if mode == "related" else None,
        organization_id=str(store.org.id),
        principal_id=str(getattr(store, reader).user.id),
        accessible_projects={"project-a"},
    )
    ids = {row.id for row in result.entities}
    assert ("legacy-projection" in ids) is (reader == "owner")


@pytest.mark.parametrize("reader", ["owner", "outsider"])
async def test_graph_connection_counts_preserve_private_parent_audience(legacy_store, reader):
    from functools import partial

    from sibyl.api.routes.entity_policy import entity_visible_to_reader

    store = legacy_store
    counts = await GraphQueryAdapter(store.runtime, str(store.org.id)).get_connection_counts(
        ["ordinary-org-resource"],
        entity_visible=partial(
            entity_visible_to_reader,
            reader_user_id=str(getattr(store, reader).user.id),
            accessible_projects={"project-a"},
            allowed_memory_scope_keys=None,
        ),
    )
    assert counts == {"ordinary-org-resource": 1 if reader == "owner" else 0}
