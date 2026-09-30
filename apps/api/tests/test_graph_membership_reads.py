"""Native graph views follow persisted grants, including warm reader caches."""

import httpx
import pytest

from sibyl.api.routes import graph
from tests.test_entity_membership_reads import (
    membership_entities as membership_entities,  # noqa: PLC0414
    reader_token,
)
from tests.test_public_membership_adapters import (
    public_membership_runtime as public_membership_runtime,  # noqa: PLC0414
)


async def assert_graph_reads(runtime, token, *, allowed):
    targets = {runtime.parent_id, runtime.passage_id}
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(base_url=runtime.base, headers=headers) as client:
        for path in ("/api/graph/nodes", "/api/graph/full", "/api/graph/hierarchical"):
            response = await client.get(path)
            assert response.status_code == 200, response.text
            data = response.json()
            nodes = data if isinstance(data, list) else data["nodes"]
            ids = {row["id"] for row in nodes}
            assert (targets <= ids) if allowed else not (targets & ids), response.text
            if path != "/api/graph/hierarchical":
                assert runtime.anchor_id in ids, response.text
        response = await client.get("/api/graph/edges")
        assert response.status_code == 200, response.text
        endpoints = {row[field] for row in response.json() for field in ("source", "target")}
        assert (targets <= endpoints) if allowed else not (targets & endpoints), response.text
        response = await client.get("/api/graph/clusters")
        assert response.status_code == 200, response.text
        for cluster in response.json()["clusters"]:
            detail = await client.get(f"/api/graph/clusters/{cluster['id']}")
            assert detail.status_code == 200, detail.text
            ids = {row["id"] for row in detail.json()["nodes"]}
            if not allowed:
                assert not (targets & ids), detail.text


@pytest.mark.asyncio
@pytest.mark.parametrize("condition", ["matching_key", "wrong_key", "key_only"])
async def test_graph_membership_and_exact_key_ceiling(membership_entities, condition):
    runtime = membership_entities
    runtime.app.include_router(graph.router, prefix="/api")
    token = await reader_token(runtime, condition)
    await assert_graph_reads(runtime, token, allowed=condition == "matching_key")


@pytest.mark.asyncio
async def test_graph_warm_cache_observes_membership_revocation(membership_entities):
    runtime = membership_entities
    runtime.app.include_router(graph.router, prefix="/api")
    token = await reader_token(runtime, "matching_key")
    await assert_graph_reads(runtime, token, allowed=True)
    await assert_graph_reads(runtime, token, allowed=True)
    if runtime.scope == "team":
        await runtime.auth.execute_query(
            "DELETE team_members WHERE team_id=$team AND user_id=$user;",
            team=runtime.team,
            user=runtime.owner,
        )
    else:
        await runtime.auth.execute_query(
            "DELETE memory_space_members WHERE space_id=$space AND principal_id=$user;",
            space=runtime.delegation,
            user=runtime.owner,
        )
    await assert_graph_reads(runtime, token, allowed=False)
