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


async def _write_plane_state(
    execute, organization_id: str, plane: str, state: dict[str, object]
) -> None:
    from sibyl_core.backends.surreal.schema_embedding_states import embedding_state_key

    await execute(
        "UPSERT type::record($key) MERGE $state RETURN NONE;",
        key=embedding_state_key(organization_id, plane),
        state={"organization_id": organization_id, "plane": plane, **state},
    )


_SWITCHED = {"complete_metadata": _PREVIOUS_CHUNK_STAMP}


def _converting(stamp: dict[str, object], in_model: int, pending: int) -> dict[str, object]:
    """A plane mid-sweep: last completed for the old model, partway into the new one."""
    from sibyl_core.embeddings.provenance import vector_space

    return {
        "complete_metadata": _PREVIOUS_CHUNK_STAMP,
        "last_run": {
            "status": "partial",
            "space": vector_space(stamp),
            "in_model": in_model,
            "pending": pending,
        },
    }


@pytest.fixture
def lane_clock(monkeypatch: pytest.MonkeyPatch):
    """Advance past the lanes' cached readiness verdict on demand."""
    from sibyl_core.services import embedding_lane_readiness as readiness

    clock = [5000.0]
    monkeypatch.setattr(readiness.time, "monotonic", lambda: clock[0])

    def advance() -> None:
        clock[0] += 31

    return advance


@pytest.mark.asyncio
async def test_graph_vector_lanes_stand_aside_after_a_switch_and_resume(engine, lane_clock) -> None:
    from sibyl_core.retrieval._search_plan import RetrievalPlan, SearchFilter
    from sibyl_core.retrieval._search_sources import _vector_candidate_sources_detailed

    async with _graph_client(engine) as client:
        new = _SweepTargetProvider(GRAPH_EMBEDDING_DIM, model="live-crowding-new")
        query = (await new.embed_texts(["converted query"], input_kind="query"))[0]
        await client.execute_query(
            "INSERT INTO entity $rows;",
            rows=[
                {
                    "uuid": "new-converted",
                    "group_id": client.group_id,
                    "name": "Converted",
                    "entity_type": "topic",
                    "name_embedding": list(query),
                    "attributes": {"embedding_metadata": new.metadata.to_dict()},
                    "created_at": datetime.now(UTC),
                }
            ],
        )
        searcher = EntityManager(client, group_id=client.group_id, embedding_provider=new)
        plan = RetrievalPlan(
            query="converted query",
            organization_id=client.group_id,
            facets=(),
            facet_types={},
            scopes=(),
            denied_scopes=(),
        )

        async def lanes_after(state: dict[str, object] | None) -> tuple[list[str], str]:
            if state is not None:
                await _write_plane_state(client.execute_query, client.group_id, "graph", state)
                lane_clock()
            hits = await searcher._vector_search(
                query="converted query", entity_types=None, limit=1
            )
            fetch = await _vector_candidate_sources_detailed(
                client=client, plan=plan, search_filter=SearchFilter(), embedding_provider=new
            )
            return [entity.id for entity, _ in hits], fetch.as_metadata()["vector_status"]

        stamp = new.metadata.to_dict()
        trace = [
            await lanes_after(None),
            await lanes_after(_SWITCHED),
            await lanes_after(_converting(stamp, 2, 98)),
            await lanes_after(_converting(stamp, 30, 70)),
        ]

    assert trace == [
        (["new-converted"], "ok"),
        ([], "vector_lane_model_switched"),
        ([], "vector_lane_model_sparse"),
        (["new-converted"], "ok"),
    ]


@pytest.mark.asyncio
async def test_chunk_vector_lanes_stand_aside_after_a_switch_and_resume(
    engine, lane_clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sibyl.persistence.surreal import content as app_content
    from sibyl_core.services import content_client
    from sibyl_core.services.content_documents import search_document_chunks

    namespace = f"chunk_readiness_{uuid4().hex}"
    organization_id, source_id, document_id = str(uuid4()), str(uuid4()), str(uuid4())
    client = _content_client(engine, namespace)

    @asynccontextmanager
    async def session():
        yield client

    monkeypatch.setattr(content_client, "surreal_content_client", session)
    monkeypatch.setattr(app_content, "surreal_content_client", session)
    query = [1.0 / (EMBEDDING_DIM**0.5)] * EMBEDDING_DIM

    async def execute(statement: str, **params: object) -> object:
        return await content_client.select_many(client, statement, **params)

    async def lanes_after(state: dict[str, object] | None) -> dict[str, int]:
        if state is not None:
            await _write_plane_state(execute, organization_id, "document_chunks", state)
            lane_clock()
        vector, lexical = await search_document_chunks(
            organization_id=organization_id,
            query_text="converted",
            query_embedding=list(query),
            limit=5,
            embedding_metadata=_TARGET_CHUNK_STAMP,
        )
        hybrid = await app_content.hybrid_search_chunks(
            None,
            query_text="nothing lexical matches",
            query_embedding=list(query),
            organization_id=organization_id,
            similarity_threshold=0.0,
            match_count=5,
            embedding_metadata=_TARGET_CHUNK_STAMP,
        )
        return {"vector": len(vector), "lexical": len(lexical), "hybrid": len(hybrid)}

    try:
        await bootstrap_content_schema(client, reset=True)
        for table, record in (
            (
                "crawl_sources",
                {
                    "uuid": source_id,
                    "organization_id": organization_id,
                    "name": "Converted guide",
                    "url": "https://docs.example.test",
                },
            ),
            (
                "crawled_documents",
                {
                    "uuid": document_id,
                    "organization_id": organization_id,
                    "source_id": source_id,
                    "url": "https://docs.example.test/converted",
                    "title": "Converted page",
                    "content": "body",
                },
            ),
            (
                "document_chunks",
                {
                    "uuid": str(uuid4()),
                    "organization_id": organization_id,
                    "source_id": source_id,
                    "document_id": document_id,
                    "content": "converted chunk body",
                    "chunk_type": "text",
                    "embedding": list(query),
                    "embedding_metadata": _TARGET_CHUNK_STAMP,
                },
            ),
        ):
            await execute(f"CREATE {table} CONTENT $record RETURN NONE;", record=record)
        trace = [
            await lanes_after(None),
            await lanes_after(_SWITCHED),
            await lanes_after(_converting(_TARGET_CHUNK_STAMP, 1, 1)),
        ]
    finally:
        await client.close()
        await _drop_namespace(engine, namespace)

    # The lexical lane keeps answering while the vector lane stands aside.
    assert trace == [
        {"vector": 1, "lexical": 1, "hybrid": 1},
        {"vector": 0, "lexical": 1, "hybrid": 0},
        {"vector": 1, "lexical": 1, "hybrid": 1},
    ]


def _near(query: list[float], index: int) -> list[float]:
    """A vector close to the query, slightly farther for each index."""
    import math

    nudged = [
        value + (0.02 * (index + 1) if position == 1 else 0.0)
        for position, value in enumerate(query)
    ]
    norm = math.sqrt(sum(value * value for value in nudged)) or 1.0
    return [value / norm for value in nudged]


def _far(seed: int, dimensions: int) -> list[float]:
    import random

    rng = random.Random(seed)  # noqa: S311 - test vectors, not secrets
    return [rng.uniform(-1.0, 1.0) for _ in range(dimensions)]


@pytest.mark.asyncio
async def test_a_never_switched_graph_keeps_full_vector_recall_through_adoption(
    engine, lane_clock
) -> None:
    """Unstamped vectors from before stamping count once the adopt verdict is recorded.

    Before it they wait: another organization's unpublished evidence could
    still turn the verdict into a re-embed.
    """
    from sibyl_core.retrieval._search_plan import RetrievalPlan, SearchFilter
    from sibyl_core.retrieval._search_sources import _vector_candidate_sources_detailed
    from sibyl_core.services.graph import RelationshipManager
    from sibyl_core.services.graph_embedding_sweep import sweep_graph_embeddings
    from sibyl_core.services.graph_runtime import GraphRuntime
    from tests.embedding_upgrade import previous_release_stamp, upgrade_graph_to_sweep

    async with _graph_client(engine) as client:
        model = _SweepTargetProvider(GRAPH_EMBEDDING_DIM, model="live-never-switched")
        stamp = model.metadata.to_dict()
        query = (await model.embed_texts(["unchanged query"], input_kind="query"))[0]
        nearest = {f"legacy-near-{index}" for index in range(5)}
        rows = (
            [
                {
                    "uuid": f"legacy-near-{index}",
                    "group_id": client.group_id,
                    "name": f"Near {index}",
                    "entity_type": "topic",
                    "name_embedding": _near(query, index),
                    "attributes": {},
                }
                for index in range(5)
            ]
            + [
                {
                    "uuid": f"legacy-far-{index:04d}",
                    "group_id": client.group_id,
                    "name": f"Far {index}",
                    "entity_type": "topic",
                    "name_embedding": _far(index, GRAPH_EMBEDDING_DIM),
                    "attributes": {},
                }
                for index in range(1_500)
            ]
            + [
                # A few rows the previous release stamped, naming the same model.
                {
                    "uuid": f"stamped-{index}",
                    "group_id": client.group_id,
                    "name": f"Stamped {index}",
                    "entity_type": "topic",
                    "name_embedding": _far(10_000 + index, GRAPH_EMBEDDING_DIM),
                    "attributes": {"embedding_metadata": previous_release_stamp(stamp)},
                }
                for index in range(5)
            ]
        )
        for start in range(0, len(rows), 500):
            await client.execute_query(
                "INSERT INTO entity $rows RETURN NONE;", rows=rows[start : start + 500]
            )
        await upgrade_graph_to_sweep(client)
        searcher = EntityManager(client, group_id=client.group_id, embedding_provider=model)
        plan = RetrievalPlan(
            query="unchanged query",
            organization_id=client.group_id,
            facets=(),
            facet_types={},
            scopes=(),
            denied_scopes=(),
        )

        async def top_five() -> tuple[set[str], set[str], str]:
            hits = await searcher._vector_search(
                query="unchanged query", entity_types=None, limit=5
            )
            fetch = await _vector_candidate_sources_detailed(
                client=client, plan=plan, search_filter=SearchFilter(), embedding_provider=model
            )
            ranked = sorted(fetch.node_candidates, key=lambda hit: -hit.score)[:5]
            return (
                {entity.id for entity, _ in hits[:5]},
                {hit.id for hit in ranked},
                fetch.as_metadata()["vector_status"],
            )

        # The first query after the upgrade, before any lifecycle pass: the
        # stamped vectors answer, the unstamped ones wait for the verdict.
        first = await top_five()
        # Part way through adoption, as a pass would leave a large plane: the
        # verdict is adopt and 1% of vectors carry a stamp.
        await _write_plane_state(
            client.execute_query,
            client.group_id,
            "graph",
            {
                "legacy_decision": "adopt",
                "legacy_basis": "prior_stamps_match",
                "legacy_metadata": stamp,
                "last_run": {
                    "status": "partial",
                    "space": {key: stamp[key] for key in ("provider", "model", "dimensions")},
                    "in_model": 1_000,
                    "pending": 99_000,
                },
            },
        )
        lane_clock()
        adopting = await top_five()
        runtime = GraphRuntime(
            client=client,
            entity_manager=searcher,
            relationship_manager=RelationshipManager(client, group_id=client.group_id),
        )
        # One pass whose budget is spent before it starts still adopts every vector.
        swept = await sweep_graph_embeddings(
            runtime, embedding_provider=model, budget_seconds=0.001
        )
        lane_clock()
        adopted = await top_five()
        unstamped = await client.execute_query(
            "SELECT count() AS count FROM entity WHERE group_id = $g "
            "AND name_embedding != NONE AND attributes.embedding_metadata = NONE GROUP ALL;",
            g=client.group_id,
        )

    assert not first[0] & nearest
    assert not first[1] & nearest
    for label, (entities, nodes, status) in (("adopting", adopting), ("adopted", adopted)):
        assert entities == nearest, label
        assert nodes == nearest, label
        assert status == "ok", label
    assert not unstamped or unstamped[0].get("count", 0) == 0, unstamped
    assert swept.adopted == 1_505


@pytest.mark.asyncio
async def test_never_switched_chunks_keep_vector_recall_once_adopted(
    engine, lane_clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Chunks carried no stamp before this release; once their plane adopts, every lane reads them.

    Before the verdict they wait, as unstamped graph vectors do.
    """
    from sibyl.persistence.surreal import content as app_content
    from sibyl_core.services import content_client
    from sibyl_core.services.content_documents import search_document_chunks
    from tests.embedding_upgrade import upgrade_content_to_sweep

    namespace = f"chunk_never_switched_{uuid4().hex}"
    organization_id, source_id = str(uuid4()), str(uuid4())
    client = _content_client(engine, namespace)

    @asynccontextmanager
    async def session():
        yield client

    monkeypatch.setattr(content_client, "surreal_content_client", session)
    monkeypatch.setattr(app_content, "surreal_content_client", session)
    query = [1.0 / (EMBEDDING_DIM**0.5)] * EMBEDDING_DIM
    nearest: set[str] = set()
    try:
        await bootstrap_content_schema(client, reset=True)
        await content_client.select_many(
            client,
            "CREATE crawl_sources CONTENT $record RETURN NONE;",
            record={
                "uuid": source_id,
                "organization_id": organization_id,
                "name": "Unchanged guide",
                "url": "https://docs.example.test",
            },
        )
        documents, chunks = [], []
        for index in range(300):
            document_id, chunk_id = str(uuid4()), str(uuid4())
            if index < 3:
                nearest.add(chunk_id)
            documents.append(
                {
                    "uuid": document_id,
                    "organization_id": organization_id,
                    "source_id": source_id,
                    "url": f"https://docs.example.test/{document_id}",
                    "title": f"Page {index}",
                    "content": "body",
                }
            )
            chunks.append(
                {
                    "uuid": chunk_id,
                    "organization_id": organization_id,
                    "source_id": source_id,
                    "document_id": document_id,
                    "content": f"chunk body {index}",
                    "chunk_type": "code",
                    # As the previous release wrote them: a vector, no stamp.
                    "embedding": _near(query, index) if index < 3 else _far(index, EMBEDDING_DIM),
                }
            )
        await content_client.select_many(
            client, "INSERT INTO crawled_documents $rows RETURN NONE;", rows=documents
        )
        await content_client.select_many(
            client, "INSERT INTO document_chunks $rows RETURN NONE;", rows=chunks
        )
        await content_client.select_many(
            client,
            "CREATE raw_captures CONTENT $record RETURN NONE;",
            record={
                "uuid": str(uuid4()),
                "organization_id": organization_id,
                "principal_id": "owner",
                "source_id": str(uuid4()),
                "raw_content": "captured by the previous release",
                "embedding": _far(99, EMBEDDING_DIM),
                "metadata": {"embedding_metadata": dict(_TARGET_CHUNK_STAMP)},
            },
        )
        await upgrade_content_to_sweep(client)

        async def execute(statement: str, **params: object) -> object:
            return await content_client.select_many(client, statement, **params)

        before, _ = await search_document_chunks(
            organization_id=organization_id,
            query_text="",
            query_embedding=list(query),
            limit=3,
            embedding_metadata=_TARGET_CHUNK_STAMP,
        )
        # The verdict a lifecycle pass records for this plane.
        await _write_plane_state(
            execute,
            organization_id,
            "document_chunks",
            {
                "legacy_decision": "adopt",
                "legacy_basis": "prior_stamps_match",
                "legacy_metadata": dict(_TARGET_CHUNK_STAMP),
            },
        )
        lane_clock()
        core, _ = await search_document_chunks(
            organization_id=organization_id,
            query_text="",
            query_embedding=list(query),
            limit=3,
            embedding_metadata=_TARGET_CHUNK_STAMP,
        )
        rag = await app_content.search_rag_chunks(
            None,
            query_embedding=list(query),
            organization_id=organization_id,
            similarity_threshold=0.0,
            match_count=3,
            embedding_metadata=_TARGET_CHUNK_STAMP,
        )
        code = await app_content.search_code_example_chunks(
            None,
            query_embedding=list(query),
            organization_id=organization_id,
            match_count=3,
            embedding_metadata=_TARGET_CHUNK_STAMP,
        )
        hybrid = await app_content.hybrid_search_chunks(
            None,
            query_text="nothing lexical matches",
            query_embedding=list(query),
            organization_id=organization_id,
            similarity_threshold=0.0,
            match_count=3,
            embedding_metadata=_TARGET_CHUNK_STAMP,
        )
    finally:
        await client.close()
        await _drop_namespace(engine, namespace)

    assert not {str(hit[0].id) for hit in before} & nearest
    found = {
        name: {str(hit[0].id) for hit in hits}
        for name, hits in (("core", core), ("rag", rag), ("code", code), ("hybrid", hybrid))
    }
    assert found == dict.fromkeys(found, nearest)
