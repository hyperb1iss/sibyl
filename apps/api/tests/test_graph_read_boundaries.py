"""Registered graph responses reject sources retired during discovery awaits."""

import httpx
import pytest

from sibyl.api.routes import graph
from sibyl.persistence.graph_runtime import GraphQueryAdapter
from sibyl_core.services import graph_community_clusters
from sibyl_core.services.graph_relationships import RelationshipManager
from sibyl_core.services.graph_runtime import get_surreal_graph_runtime
from sibyl_core.services.memory_correction import apply_memory_correction
from tests.test_entity_membership_reads import (
    membership_entities as membership_entities,  # noqa: PLC0414
    reader_token,
)
from tests.test_public_membership_adapters import (
    public_membership_runtime as public_membership_runtime,  # noqa: PLC0414
)


def discovery_boundary(surface):
    return {
        "nodes": (GraphQueryAdapter, "get_connection_counts"),
        "full": (GraphQueryAdapter, "list_relationships_for_entities"),
        "subgraph": (RelationshipManager, "get_related_entities"),
        "clusters": (graph_community_clusters, "_native_relationship_edges_between_ids"),
        "debug": (RelationshipManager, "list_all"),
    }[surface]


@pytest.mark.parametrize("surface", ["nodes", "full", "subgraph", "clusters", "debug"])
async def test_graph_response_rechecks_source_after_discovery(
    membership_entities, monkeypatch, surface
):
    runtime = membership_entities
    runtime.app.include_router(graph.router, prefix="/api")
    if surface == "debug":
        await runtime.auth.execute_query(
            "UPDATE organization_members SET role='owner' "
            "WHERE organization_id=$org AND user_id=$user;",
            org=runtime.org,
            user=runtime.owner,
        )
    token = await reader_token(runtime, "matching_key")
    target_ids = {runtime.parent_id, runtime.passage_id}
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(base_url=runtime.base, headers=headers) as client:
        path = f"/api/graph/{surface}"
        body = {"entity_id": runtime.parent_id, "depth": 2, "max_nodes": 20}

        async def read():
            if surface == "subgraph":
                return await client.post(path, json=body)
            return await client.get(path)

        if surface == "clusters":
            response = await client.get(path)
            assert response.status_code == 200, response.text
            for cluster in response.json()["clusters"]:
                detail_path = f"/api/graph/clusters/{cluster['id']}"
                response = await client.get(detail_path)
                ids = {row["id"] for row in response.json()["nodes"]}
                if target_ids <= ids:
                    path = detail_path
                    break
            else:
                pytest.fail("Source-backed fixture has no readable cluster")
        baseline = await read()
        assert baseline.status_code == 200, baseline.text
        data = baseline.json()
        if surface == "debug":
            assert data["node_count"] == 3
        else:
            rows = data if surface == "nodes" else data["nodes"]
            assert target_ids <= {row["id"] for row in rows}, baseline.text

        applied = False

        async def retire():
            nonlocal applied
            if applied:
                return
            correction = await apply_memory_correction(
                organization_id=runtime.org,
                source_id=runtime.raw_id,
                principal_id=runtime.owner,
                action="delete",
                accessible_projects={runtime.project},
                writable_projects={runtime.project},
                accessible_teams={runtime.team},
                accessible_delegations={runtime.delegation},
            )
            assert correction.applied
            assert correction.propagation_complete
            applied = True

        owner, method = discovery_boundary(surface)
        discover = getattr(owner, method)

        async def retiring_discovery(*args, **kwargs):
            await retire()
            return await discover(*args, **kwargs)

        monkeypatch.setattr(owner, method, retiring_discovery)
        response = await read()
        assert applied
        if surface == "subgraph":
            assert response.status_code == 404, response.text
            return
        assert response.status_code == 200, response.text
        data = response.json()
        if surface == "debug":
            assert data["node_count"] == 1
            assert data["edge_count"] == 0
            assert data["sample_nodes"] == [runtime.anchor_id]
        else:
            rows = data if surface == "nodes" else data["nodes"]
            ids = {row["id"] for row in rows}
            assert not (target_ids & ids), response.text
            assert runtime.anchor_id in ids, response.text
            if surface != "nodes":
                assert data["edges"] == []


@pytest.mark.parametrize("surface", ["full", "subgraph", "clusters", "edges", "debug"])
async def test_graph_response_drops_edge_removed_during_final_node_loading(
    membership_entities, monkeypatch, surface
):
    runtime = membership_entities
    runtime.app.include_router(graph.router, prefix="/api")
    if surface == "debug":
        await runtime.auth.execute_query(
            "UPDATE organization_members SET role='owner' "
            "WHERE organization_id=$org AND user_id=$user;",
            org=runtime.org,
            user=runtime.owner,
        )
    native = await get_surreal_graph_runtime(runtime.org)
    edges = await native.relationship_manager.list_all(limit=100)
    removed = next(
        edge
        for edge in edges
        if edge.source_id == runtime.anchor_id and edge.target_id == runtime.parent_id
    )
    token = await reader_token(runtime, "matching_key")
    wanted = {runtime.anchor_id, runtime.parent_id, runtime.passage_id}
    async with httpx.AsyncClient(
        base_url=runtime.base, headers={"Authorization": f"Bearer {token}"}
    ) as client:
        path = f"/api/graph/{surface}"
        if surface == "clusters":
            response = await client.get(path)
            assert response.status_code == 200, response.text
            for cluster in response.json()["clusters"]:
                detail = f"/api/graph/clusters/{cluster['id']}"
                response = await client.get(detail)
                if wanted <= {row["id"] for row in response.json()["nodes"]}:
                    path = detail
                    break
            else:
                pytest.fail("Connected fixture has no readable cluster")

        async def read():
            if surface == "subgraph":
                return await client.post(
                    path, json={"entity_id": runtime.anchor_id, "depth": 2, "max_nodes": 20}
                )
            return await client.get(path)

        baseline = await read()
        assert baseline.status_code == 200, baseline.text
        owner = graph_community_clusters if surface == "clusters" else graph
        method = "_current_graph_entities" if surface == "clusters" else "_current_entities"
        load_nodes = getattr(owner, method)
        changed = False

        async def remove_during_node_loading(*args, **kwargs):
            nonlocal changed
            nodes = await load_nodes(*args, **kwargs)
            if wanted <= set(nodes) and not changed:
                assert await native.relationship_manager.delete(removed.id)
                changed = True
            return nodes

        monkeypatch.setattr(owner, method, remove_during_node_loading)
        response = await read()
        assert changed
        assert response.status_code == 200, response.text
        current = await native.entity_manager.get_many(sorted(wanted))
        assert {row.id for row in current} == wanted
        data = response.json()
        if surface == "debug":
            assert data["node_count"] == 3
            assert data["edge_count"] == 2
        else:
            rows = data if surface == "edges" else data["edges"]
            assert len(rows) == 2
            assert not any(
                row["source"] == removed.source_id and row["target"] == removed.target_id
                for row in rows
            )
            if surface != "edges":
                assert {row["id"] for row in data["nodes"]} == wanted
