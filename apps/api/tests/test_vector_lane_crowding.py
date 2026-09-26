"""Vector lanes reach new-model rows behind nearer old-model ones, on both engines.

Mid-sweep, rows from an older model can sit nearer a query than every row in
the query's model. The model filter must shape the HNSW read itself, and the
embedded engine treats predicates inside the bracket differently from a
native server, so every case runs on embedded SurrealKV and, when the live
server is enabled, on native SurrealDB.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from sibyl_core.backends.surreal import SurrealContentClient, bootstrap_content_schema
from sibyl_core.backends.surreal.content_schema import EMBEDDING_DIM
from sibyl_core.backends.surreal.schema import EMBEDDING_DIM as GRAPH_EMBEDDING_DIM
from sibyl_core.models.entities import EntityType
from sibyl_core.services.graph import EntityManager, SurrealGraphClient, prepare_graph_schema
from tests.test_surreal_live_runtime import (
    _PREVIOUS_CHUNK_STAMP,
    _TARGET_CHUNK_STAMP,
    _previous_graph_stamp,
    _SweepTargetProvider,
)

_EMBEDDED = ("memory://", "surrealkv://", "rocksdb://", "file://")


@pytest.fixture(params=["embedded", "native"])
def engine(request: pytest.FixtureRequest, tmp_path: Path) -> dict[str, str | None]:
    if request.param == "embedded":
        return {"url": f"surrealkv://{tmp_path}/store", "username": None, "password": None}
    url = os.environ.get("SIBYL_SURREAL_URL", "")
    if os.environ.get("SIBYL_LIVE_SURREAL_TESTS") != "1" or not url or url.startswith(_EMBEDDED):
        pytest.skip("the native engine case needs SIBYL_LIVE_SURREAL_TESTS=1 and a server URL")
    return {
        "url": url,
        "username": os.environ.get("SIBYL_SURREAL_USERNAME", "root"),
        "password": os.environ.get("SIBYL_SURREAL_PASSWORD", "root"),
    }


@asynccontextmanager
async def _graph_client(engine: dict[str, str | None]) -> AsyncIterator[SurrealGraphClient]:
    client = SurrealGraphClient(
        group_id=str(uuid4()),
        url=engine["url"],
        username=engine["username"],
        password=engine["password"],
    )
    try:
        await prepare_graph_schema(client)
        yield client
    finally:
        await client.close()
        with suppress(Exception):
            await _drop_namespace(engine, client.namespace)


def _content_client(engine: dict[str, str | None], namespace: str) -> SurrealContentClient:
    return SurrealContentClient(
        url=engine["url"],
        username=engine["username"],
        password=engine["password"],
        namespace=namespace,
        database="content",
    )


async def _drop_namespace(engine: dict[str, str | None], namespace: str) -> None:
    if str(engine["url"]).startswith(_EMBEDDED):
        return
    from surrealdb import AsyncSurreal

    client = AsyncSurreal(engine["url"])
    try:
        await client.signin({"username": engine["username"], "password": engine["password"]})
        await client.query(f"REMOVE NAMESPACE IF EXISTS {namespace};")
    finally:
        await client.close()


def _crowding_vectors(query: list[float], dimensions: int) -> tuple[list[float], list[float]]:
    """The query itself (nearest possible) and a clearly farther new-model vector."""
    import math

    # A nudge along one axis: about 0.96 cosine to the query, so a genuine
    # near-duplicate, yet farther than rows sitting exactly on it.
    farther = [value + (0.3 if index == 0 else 0.0) for index, value in enumerate(query)]
    norm = math.sqrt(sum(value * value for value in farther)) or 1.0
    return list(query), [value / norm for value in farther[:dimensions]]


@pytest.mark.asyncio
async def test_graph_vector_lanes_reach_new_rows_behind_nearer_old_ones(engine) -> None:
    from sibyl_core.models.entities import RelationshipType
    from sibyl_core.retrieval._search_plan import RetrievalPlan, SearchFilter
    from sibyl_core.retrieval._search_sources import (
        _edge_vector_candidates,
        _node_vector_candidates,
    )

    async with _graph_client(engine) as client:
        new = _SweepTargetProvider(GRAPH_EMBEDDING_DIM, model="live-crowding-new")
        old_stamp = _previous_graph_stamp()
        new_stamp = new.metadata.to_dict()
        query = (await new.embed_texts(["crowded query"], input_kind="query"))[0]
        nearest, farther = _crowding_vectors(query, GRAPH_EMBEDDING_DIM)
        rows = [
            {
                "uuid": f"old-{index:02d}",
                "group_id": client.group_id,
                "name": f"Old {index}",
                "entity_type": "topic",
                "name_embedding": list(nearest),
                "attributes": {"embedding_metadata": old_stamp},
                "created_at": datetime.now(UTC),
            }
            for index in range(64)
        ] + [
            {
                "uuid": f"new-{index}",
                "group_id": client.group_id,
                "name": f"New {index}",
                "entity_type": "topic",
                "name_embedding": list(farther),
                "attributes": {"embedding_metadata": new_stamp},
                "created_at": datetime.now(UTC),
            }
            for index in range(3)
        ]
        # A new-model entity exactly where the old ones sit: its duplicates in
        # its own space lie farther away than 64 old-model rows.
        rows.append(
            {
                "uuid": "new-seed",
                "group_id": client.group_id,
                "name": "New seed",
                "entity_type": "topic",
                "name_embedding": list(nearest),
                "attributes": {"embedding_metadata": new_stamp},
                "created_at": datetime.now(UTC),
            }
        )
        await client.execute_query("INSERT INTO entity $rows;", rows=rows)
        from sibyl_core.models.entities import Relationship
        from sibyl_core.services.graph import RelationshipManager

        await RelationshipManager(client, group_id=client.group_id).create_direct_bulk(
            [
                Relationship(
                    id=f"{kind}-edge-{index:02d}",
                    relationship_type=RelationshipType.RELATED_TO,
                    source_id="new-0",
                    target_id="new-1",
                    metadata={
                        "fact": f"{kind} fact {index}",
                        "fact_embedding": list(nearest if kind == "old" else farther),
                        "embedding_metadata": old_stamp if kind == "old" else new_stamp,
                    },
                )
                for kind, count in (("old", 64), ("new", 3))
                for index in range(count)
            ]
        )
        plan = RetrievalPlan(
            query="crowded query",
            organization_id=client.group_id,
            facets=(),
            facet_types={},
            scopes=(),
            denied_scopes=(),
        )
        searcher = EntityManager(client, group_id=client.group_id, embedding_provider=new)

        entity_hits = await searcher._vector_search(
            query="crowded query", entity_types=None, limit=1
        )
        typed_hits = await searcher._vector_search(
            query="crowded query",
            entity_types=[EntityType.TOPIC],
            limit=1,
            knn_type_overfetch=4,
        )
        node_hits = await _node_vector_candidates(
            client=client,
            plan=plan,
            search_filter=SearchFilter(),
            query_embedding=query,
            embedding_metadata=new.metadata,
            limit=1,
        )
        typed_node_hits = await _node_vector_candidates(
            client=client,
            plan=plan,
            search_filter=SearchFilter(node_types=("topic",), knn_type_overfetch=4),
            query_embedding=query,
            embedding_metadata=new.metadata,
            limit=1,
        )
        edge_hits = await _edge_vector_candidates(
            client=client,
            plan=plan,
            search_filter=SearchFilter(),
            query_embedding=query,
            embedding_metadata=new.metadata,
            limit=1,
        )
        from sibyl_core.retrieval.dedup import DedupConfig, EntityDeduplicator

        pairs = await EntityDeduplicator(
            client=client,
            entity_manager=EntityManager(client, group_id=client.group_id),
            config=DedupConfig(similarity_threshold=0.9, batch_size=4, min_name_overlap=0.0),
        ).find_duplicates(threshold=0.9)

    seed_pairs = [
        {pair.entity1_id, pair.entity2_id}
        for pair in pairs
        if "new-seed" in (pair.entity1_id, pair.entity2_id)
    ]
    lanes = {
        "entity": [entity.id for entity, _score in entity_hits],
        "typed entity": [entity.id for entity, _score in typed_hits],
        "node": [hit.id for hit in node_hits],
        "typed node": [hit.id for hit in typed_node_hits],
        "edge": [hit.id for hit in edge_hits],
        "dedup": sorted(uuid for pair in seed_pairs for uuid in pair),
    }
    starved = {
        name: ids
        for name, ids in lanes.items()
        if not ids or not all(uuid.startswith("new-") for uuid in ids)
    }
    assert not starved, starved


@pytest.mark.asyncio
async def test_chunk_vector_lanes_reach_new_rows_behind_nearer_old_ones(
    engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sibyl.persistence.surreal import content as app_content
    from sibyl_core.services import content_client
    from sibyl_core.services.content_documents import search_document_chunks

    namespace = f"chunk_crowding_{uuid4().hex}"
    organization_id = str(uuid4())
    source_id = str(uuid4())
    client = _content_client(engine, namespace)

    @asynccontextmanager
    async def session():
        yield client

    monkeypatch.setattr(content_client, "surreal_content_client", session)
    monkeypatch.setattr(app_content, "surreal_content_client", session)
    query = [1.0 / (EMBEDDING_DIM**0.5)] * EMBEDDING_DIM
    nearest, farther = _crowding_vectors(query, EMBEDDING_DIM)
    try:
        await bootstrap_content_schema(client, reset=True)
        await content_client.select_many(
            client,
            "CREATE crawl_sources CONTENT $record RETURN NONE;",
            record={
                "uuid": source_id,
                "organization_id": organization_id,
                "name": "Crowded guide",
                "url": "https://docs.example.test",
            },
        )
        new_chunks: set[str] = set()
        for kind, count in (("old", 64), ("new", 3)):
            for index in range(count):
                document_id, chunk_id = str(uuid4()), str(uuid4())
                if kind == "new":
                    new_chunks.add(chunk_id)
                await content_client.select_many(
                    client,
                    "CREATE crawled_documents CONTENT $record RETURN NONE;",
                    record={
                        "uuid": document_id,
                        "organization_id": organization_id,
                        "source_id": source_id,
                        "url": f"https://docs.example.test/{document_id}",
                        "title": f"{kind} page {index}",
                        "content": "body",
                    },
                )
                await content_client.select_many(
                    client,
                    "CREATE document_chunks CONTENT $record RETURN NONE;",
                    record={
                        "uuid": chunk_id,
                        "organization_id": organization_id,
                        "source_id": source_id,
                        "document_id": document_id,
                        "content": f"{kind} chunk body {index}",
                        "chunk_type": "code",
                        "embedding": list(nearest if kind == "old" else farther),
                        "embedding_metadata": (
                            _PREVIOUS_CHUNK_STAMP if kind == "old" else _TARGET_CHUNK_STAMP
                        ),
                    },
                )

        core_hits, _ = await search_document_chunks(
            organization_id=organization_id,
            query_text="",
            query_embedding=list(query),
            limit=1,
            embedding_metadata=_TARGET_CHUNK_STAMP,
        )
        rag_hits = await app_content.search_rag_chunks(
            None,
            query_embedding=list(query),
            organization_id=organization_id,
            similarity_threshold=0.0,
            match_count=1,
            embedding_metadata=_TARGET_CHUNK_STAMP,
        )
        code_hits = await app_content.search_code_example_chunks(
            None,
            query_embedding=list(query),
            organization_id=organization_id,
            match_count=1,
            embedding_metadata=_TARGET_CHUNK_STAMP,
        )
        hybrid_hits = await app_content.hybrid_search_chunks(
            None,
            query_text="nothing lexical matches",
            query_embedding=list(query),
            organization_id=organization_id,
            similarity_threshold=0.0,
            match_count=1,
            embedding_metadata=_TARGET_CHUNK_STAMP,
        )
    finally:
        await client.close()
        await _drop_namespace(engine, namespace)

    starved = {
        name: sorted(str(hit[0].id) for hit in hits)
        for name, hits in (
            ("core", core_hits),
            ("rag", rag_hits),
            ("code", code_hits),
            ("hybrid", hybrid_hits),
        )
        if not hits or not {str(hit[0].id) for hit in hits} <= new_chunks
    }
    assert not starved, starved
