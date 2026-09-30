"""Registered graph responses reject sources retired during discovery awaits."""

import httpx
import pytest

from sibyl.api.routes import graph
from sibyl.persistence.graph_runtime import GraphQueryAdapter
from sibyl_core.services import graph_community_clusters
from sibyl_core.services.graph_relationships import RelationshipManager
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
