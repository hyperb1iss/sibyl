"""The edge vector lane admits an edge by its endpoints' projects as they stand when it runs.

A project-filtered vector search must return only edges that carry the project
themselves or end on an entity in it (by column or by attribute bag), and an
entity that moves project must move its edges with it on the next search. The
SDK's embedded engine drops a parenthesized group inside an HNSW bracket and
returns nothing for a project-filtered vector lane, so this runs on a server.
"""

from __future__ import annotations

import math
import os
import random
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from surrealdb import RecordID

from sibyl_core.backends.surreal.schema import EMBEDDING_DIM
from sibyl_core.backends.surreal.url_schemes import is_embedded_surreal_url
from sibyl_core.embeddings.providers import EmbeddingMetadata
from sibyl_core.retrieval import _search_sources as source_module
from sibyl_core.retrieval._search_plan import SearchFilter
from sibyl_core.services.graph_client import SurrealGraphClient, prepare_graph_schema

METADATA = EmbeddingMetadata(
    provider="deterministic",
    model="edge-membership-test",
    dimensions=EMBEDDING_DIM,
    cache_namespace="edge-membership-test",
    tokenizer_estimate_method="utf8-byte-length",
)


def _vector(rng: random.Random) -> list[float]:
    raw = [rng.uniform(-1.0, 1.0) for _ in range(EMBEDDING_DIM)]
    norm = math.sqrt(sum(value * value for value in raw))
    return [value / norm for value in raw]


@pytest.fixture
async def graph() -> AsyncIterator[SurrealGraphClient]:
    if os.environ.get("SIBYL_LIVE_SURREAL_TESTS") != "1":
        pytest.skip("live SurrealDB tests are disabled")
    url = os.environ.get("SIBYL_SURREAL_URL", "")
    if not url or is_embedded_surreal_url(url):
        pytest.skip("the embedded engine returns no rows for a project-filtered vector lane")
    client = SurrealGraphClient(
        group_id=f"edge-membership-{uuid4().hex[:10]}",
        url=url,
        username=os.environ.get("SIBYL_SURREAL_USERNAME", "root"),
        password=os.environ.get("SIBYL_SURREAL_PASSWORD", "root"),
        namespace_prefix="verify_",
        database="graph",
        pool_size=2,
    )
    try:
        await prepare_graph_schema(client)
        yield client
    finally:
        await client.execute_query(f"REMOVE NAMESPACE IF EXISTS {client.namespace};")
        await client.close()


async def _seed(client: SurrealGraphClient) -> dict[str, dict[str, Any]]:
    """Entities in projects a and b by column or by attribute bag, the rest in none; edges among them."""
    rng = random.Random(3)
    entities = []
    for index in range(48):
        row: dict[str, Any] = {
            "uuid": f"n{index:02d}",
            "name": f"node {index}",
            "entity_type": "note",
            "group_id": client.group_id,
            "attributes": {},
        }
        if index < 8:
            row["project_id"] = "project-a"
        elif index < 12:
            row["attributes"] = {"project_id": "project-a"}
        elif index < 20:
            row["project_id"] = "project-b"
        entities.append(row)
    await client.execute_query(
        "FOR $row IN $rows { CREATE $row.record CONTENT $row.body; };",
        rows=[{"record": RecordID("entity", row["uuid"]), "body": row} for row in entities],
    )
    edges: dict[str, dict[str, Any]] = {}
    for index in range(160):
        source, target = rng.sample(range(48), 2)
        edge = {
            "uuid": f"r{index:03d}",
            "in": RecordID("entity", f"n{source:02d}"),
            "out": RecordID("entity", f"n{target:02d}"),
            "source": f"n{source:02d}",
            "target": f"n{target:02d}",
            "fact_embedding": _vector(rng),
            "attributes": {
                "embedding_metadata": METADATA.to_dict(),
                **({"project_id": "project-b"} if index % 23 == 0 else {}),
            },
        }
        edges[edge["uuid"]] = edge
    await client.execute_query(
        """
        FOR $edge IN $edges {
            RELATE ($edge.in)->relates_to->($edge.out) CONTENT {
                uuid: $edge.uuid, name: 'RELATES_TO', fact: $edge.uuid, group_id: $group_id,
                source_id: $edge.source, target_id: $edge.target,
                fact_embedding: $edge.fact_embedding, attributes: $edge.attributes
            };
        };
        """,
        edges=list(edges.values()),
        group_id=client.group_id,
    )
    return edges


async def _admitted(client: SurrealGraphClient, uuid: str, projects: set[str]) -> bool:
    """The lane's rule, read independently: the edge or one of its endpoints is in the projects."""
    rows = await client.execute_query(
        "SELECT attributes.project_id AS edge_project,"
        " [in.project_id, in.attributes.project_id, out.project_id, out.attributes.project_id] AS endpoint_projects"
        " FROM relates_to WHERE uuid = $uuid;",
        uuid=uuid,
    )
    row = rows[0]
    return row.get("edge_project") in projects or bool(set(row["endpoint_projects"]) & projects)


async def _lane(
    client: SurrealGraphClient, query: list[float], search_filter: SearchFilter
) -> list[str]:
    candidates = await source_module._edge_vector_candidates(
        client=client,
        plan=SimpleNamespace(organization_id=client.group_id, vector_min_score=0.0),
        search_filter=search_filter,
        query_embedding=query,
        embedding_metadata=METADATA,
        limit=8,
    )
    return [candidate.id for candidate in candidates]


@pytest.mark.parametrize(
    "search_filter",
    [
        pytest.param(SearchFilter(project_ids=("project-a",)), id="one-project"),
        pytest.param(SearchFilter(project_ids=("project-a", "project-b")), id="two-projects"),
        pytest.param(
            SearchFilter(
                project_ids=("project-b",), edge_types=("RELATES_TO",), knn_type_overfetch=4
            ),
            id="overfetch",
        ),
    ],
)
async def test_the_lane_returns_only_edges_inside_the_projects(
    graph: SurrealGraphClient, search_filter: SearchFilter
) -> None:
    edges = await _seed(graph)
    projects = set(search_filter.project_ids)
    for edge in list(edges.values())[:6]:
        found = await _lane(graph, edge["fact_embedding"], search_filter)
        assert found, "every query vector has project edges in reach"
        for uuid in found:
            assert await _admitted(graph, uuid, projects), uuid
        if await _admitted(graph, edge["uuid"], projects):
            # Its own vector puts an admitted edge first.
            assert found[0] == edge["uuid"]
        else:
            assert edge["uuid"] not in found


async def test_an_entity_moving_project_moves_its_edges_on_the_next_search(
    graph: SurrealGraphClient,
) -> None:
    edges = await _seed(graph)
    # An edge in project a through its source alone.
    moving = next(
        edge
        for edge in edges.values()
        if edge["source"] < "n08"
        and edge["target"] >= "n20"
        and "project_id" not in edge["attributes"]
    )
    query = moving["fact_embedding"]
    in_a = SearchFilter(project_ids=("project-a",))
    in_b = SearchFilter(project_ids=("project-b",))

    assert (await _lane(graph, query, in_a))[0] == moving["uuid"]
    assert moving["uuid"] not in await _lane(graph, query, in_b)

    await graph.execute_query(
        "UPDATE entity SET project_id = 'project-b' WHERE uuid = $uuid;", uuid=moving["source"]
    )

    assert moving["uuid"] not in await _lane(graph, query, in_a)
    assert (await _lane(graph, query, in_b))[0] == moving["uuid"]
