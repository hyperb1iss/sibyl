"""The edge vector lane admits a project's edges by endpoint set, with the same rows.

The lane's project filter runs for every candidate the HNSW search visits,
and a selective filter visits most of the graph. It used to follow `in` and
`out` to read each endpoint's project; it now tests the endpoints against the
entity records those reads would admit, read once beforehand.
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
    model="edge-filter-test",
    dimensions=EMBEDDING_DIM,
    cache_namespace="edge-filter-test",
    tokenizer_estimate_method="utf8-byte-length",
)
PROJECTS = ("project-a", "project-b")


def _vector(rng: random.Random) -> list[float]:
    raw = [rng.uniform(-1.0, 1.0) for _ in range(EMBEDDING_DIM)]
    norm = math.sqrt(sum(value * value for value in raw))
    return [value / norm for value in raw]


@pytest.fixture(params=["embedded", "live"])
async def graph(request) -> AsyncIterator[tuple[SurrealGraphClient, bool]]:
    group_id = f"edge-filter-{uuid4().hex[:10]}"
    if request.param == "embedded":
        client = SurrealGraphClient(group_id=group_id, url="memory://")
    else:
        if os.environ.get("SIBYL_LIVE_SURREAL_TESTS") != "1":
            pytest.skip("live SurrealDB tests are disabled")
        url = os.environ.get("SIBYL_SURREAL_URL", "")
        if not url or is_embedded_surreal_url(url):
            pytest.skip("live SurrealDB tests require SIBYL_SURREAL_URL to point at a server")
        client = SurrealGraphClient(
            group_id=group_id,
            url=url,
            username=os.environ.get("SIBYL_SURREAL_USERNAME", "root"),
            password=os.environ.get("SIBYL_SURREAL_PASSWORD", "root"),
            namespace_prefix="verify_",
            database="graph",
            pool_size=2,
        )
    try:
        await prepare_graph_schema(client)
        yield client, request.param == "live"
    finally:
        if request.param == "live":
            await client.execute_query(f"REMOVE NAMESPACE IF EXISTS {client.namespace};")
        await client.close()


async def _seed(client: SurrealGraphClient) -> list[list[float]]:
    rng = random.Random(7)
    entities: list[dict[str, Any]] = []
    for index in range(60):
        row: dict[str, Any] = {
            "uuid": f"n{index:02d}",
            "name": f"node {index}",
            "entity_type": "note",
            "group_id": client.group_id,
            "attributes": {},
        }
        if index < 12:
            row["project_id"] = PROJECTS[index % 2]
        elif index < 18:
            # Only the attribute bag names the project.
            row["attributes"] = {"project_id": PROJECTS[index % 2]}
        elif index < 22:
            row["project_id"] = "project-elsewhere"
        entities.append(row)
    await client.execute_query(
        "FOR $row IN $rows { CREATE $row.record CONTENT $row.body; };",
        rows=[{"record": RecordID("entity", row["uuid"]), "body": row} for row in entities],
    )
    edges: list[dict[str, Any]] = []
    for index in range(240):
        source, target = rng.sample(range(60), 2)
        attributes: dict[str, Any] = {"embedding_metadata": METADATA.to_dict()}
        if index % 37 == 0:
            attributes["project_id"] = PROJECTS[0]
        edges.append(
            {
                "uuid": f"r{index:03d}",
                "in": RecordID("entity", f"n{source:02d}"),
                "out": RecordID("entity", f"n{target:02d}"),
                "source": f"n{source:02d}",
                "target": f"n{target:02d}",
                "fact": f"edge {index}",
                "fact_embedding": _vector(rng),
                "attributes": attributes,
            }
        )
    await client.execute_query(
        """
        FOR $edge IN $edges {
            RELATE ($edge.in)->relates_to->($edge.out) CONTENT {
                uuid: $edge.uuid, name: 'RELATES_TO', fact: $edge.fact, group_id: $group_id,
                source_id: $edge.source, target_id: $edge.target,
                fact_embedding: $edge.fact_embedding, attributes: $edge.attributes
            };
        };
        """,
        edges=edges,
        group_id=client.group_id,
    )
    return [_vector(rng) for _ in range(6)]


def _ids_and_scores(candidates: list[Any]) -> list[tuple[str, float]]:
    return [(candidate.id, round(float(candidate.score), 9)) for candidate in candidates]


async def test_edge_vector_lane_finds_the_rows_the_link_walk_found(
    graph: tuple[SurrealGraphClient, bool], monkeypatch: pytest.MonkeyPatch
) -> None:
    graph, live = graph
    queries = await _seed(graph)
    # The lane reads only these two fields of a plan.
    plan = SimpleNamespace(organization_id=graph.group_id, vector_min_score=0.0)
    search_filter = SearchFilter(project_ids=PROJECTS)
    seen: list[str] = []
    record = source_module._execute_query_records

    async def recording(client: Any, query: str, **params: object) -> Any:
        seen.append(query)
        return await record(client, query, **params)

    monkeypatch.setattr(source_module, "_execute_query_records", recording)

    async def lane(query: list[float]) -> list[tuple[str, float]]:
        return _ids_and_scores(
            await source_module._edge_vector_candidates(
                client=graph,
                plan=plan,
                search_filter=search_filter,
                query_embedding=query,
                embedding_metadata=METADATA,
                limit=8,
            )
        )

    for query in queries:
        rewritten = await lane(query)
        lane_query = seen[-1]
        assert "in IN $project_endpoints" in lane_query
        assert "in.project_id IN" not in lane_query
        # The link walk the endpoint set replaced is the oracle.
        with monkeypatch.context() as walk:
            walk.setattr(source_module, "_project_endpoints", _no_endpoint_set)
            walked = await lane(query)
        assert "in.project_id IN $project_ids" in seen[-1]
        assert rewritten == walked
        # The embedded engine drops a parenthesized group inside an HNSW
        # bracket, so a project-filtered vector lane is empty there either way.
        if live:
            assert rewritten, "the seeded graph has project edges near every query"


async def _no_endpoint_set(client: Any, search_filter: SearchFilter) -> None:
    del client, search_filter
