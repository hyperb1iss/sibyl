"""Writes and reads that race the upgrade's own bookkeeping, on both engines.

Raw capture repair holds no lease, so two processes configured for different
models (a rolling deploy) can repair the same capture at once; a vector lane
can read a plane before its verdict is recorded. Every case runs on embedded
SurrealKV and, when the live server is enabled, on native SurrealDB.
"""

from __future__ import annotations

from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from sibyl_core.backends.surreal import SurrealContentClient, bootstrap_content_schema
from sibyl_core.backends.surreal.content_schema import EMBEDDING_DIM
from sibyl_core.backends.surreal.schema import EMBEDDING_DIM as GRAPH_EMBEDDING_DIM
from sibyl_core.embeddings.providers import DeterministicEmbeddingProvider, EmbeddingMetadata
from sibyl_core.services.graph import EntityManager, SurrealGraphClient, prepare_graph_schema
from tests.embedding_upgrade import previous_release_stamp, upgrade_graph_to_sweep
from tests.test_vector_lane_crowding import _EMBEDDED, _drop_namespace, engine, lane_clock

__all__ = ["engine", "lane_clock"]

_SMALL = "text-embedding-3-small"
_LARGE = "text-embedding-3-large"


def _url(engine: dict[str, str | None], store: str) -> str:
    """One store per client on the embedded engine; one server for all on native."""
    url = str(engine["url"])
    return f"{url}-{store}" if url.startswith(_EMBEDDED) else url


def _provider(model: str, dimensions: int, namespace: str) -> DeterministicEmbeddingProvider:
    return DeterministicEmbeddingProvider(
        EmbeddingMetadata(
            provider="openai",
            model=model,
            dimensions=dimensions,
            cache_namespace=namespace,
            tokenizer_estimate_method="provider-default",
        )
    )


def _content(engine: dict[str, str | None]) -> SurrealContentClient:
    return SurrealContentClient(
        url=_url(engine, "content"),
        username=engine["username"],
        password=engine["password"],
        namespace=f"races_{uuid4().hex}",
        database="content",
    )


def _unit(index: int, dimensions: int) -> list[float]:
    return [1.0 if position == index else 0.0 for position in range(dimensions)]


async def _raw_capture(content, organization_id: str, *, stamp, vector) -> str:
    from sibyl_core.services import content_client

    uuid = str(uuid4())
    await content_client.select_many(
        content,
        "CREATE raw_captures CONTENT $record RETURN NONE;",
        record={
            "uuid": uuid,
            "organization_id": organization_id,
            "principal_id": "owner",
            "source_id": str(uuid4()),
            "raw_content": "captured under the previous release",
            "revision": 7,
            "embedding": vector,
            "metadata": {"embedding_metadata": stamp},
        },
    )
    return uuid


async def _stored(content, uuid: str) -> dict[str, object]:
    from sibyl_core.services import content_client

    rows = await content_client.select_many(
        content,
        "SELECT embedding, metadata.embedding_metadata AS stamp, revision FROM raw_captures "
        "WHERE uuid = $uuid;",
        uuid=uuid,
    )
    return rows[0]


async def _repair_racing(content, organization_id: str, provider, *, before: str, write):
    """Run one raw repair pass; just before its first statement matching ``before``, ``write``."""
    from sibyl_core.services import content_client
    from sibyl_core.services.content_raw_embedding_repair import repair_raw_capture_embeddings

    original = content_client.select_many
    raced = False

    async def racing(client, query, **params):
        nonlocal raced
        if not raced and before in query:
            raced = True
            await write()
        return await original(client, query, **params)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(content_client, "select_many", racing)
        result = await repair_raw_capture_embeddings(
            organization_id, embedding_provider=provider, client=content
        )
    assert raced, "the concurrent write never ran"
    return result


@pytest.mark.asyncio
async def test_a_restamp_never_relabels_a_vector_another_repair_just_wrote(engine) -> None:
    """A reads a legacy A stamp, B writes its own vector and stamp, A's restamp must not land."""
    from sibyl_core.services import content_client
    from sibyl_core.services.content_models import raw_memory_embedding_metadata

    small = _provider(_SMALL, EMBEDDING_DIM, "raw-memory")
    large = _provider(_LARGE, EMBEDDING_DIM, "raw-memory")
    organization_id = str(uuid4())
    content = _content(engine)
    large_stamp = raw_memory_embedding_metadata(large.metadata)
    large_vector = _unit(1, EMBEDDING_DIM)
    try:
        await bootstrap_content_schema(content, reset=True)
        uuid = await _raw_capture(
            content,
            organization_id,
            stamp=previous_release_stamp(raw_memory_embedding_metadata(small.metadata)),
            vector=_unit(0, EMBEDDING_DIM),
        )

        async def other_repair() -> None:
            # A process configured for the large model, keeping the revision.
            await content_client.select_many(
                content,
                "UPDATE raw_captures SET embedding = $vector, "
                "metadata.embedding_metadata = $stamp WHERE uuid = $uuid RETURN NONE;",
                vector=large_vector,
                stamp=large_stamp,
                uuid=uuid,
            )

        result = await _repair_racing(
            content,
            organization_id,
            small,
            before="uuid IN $uuids)\nSET metadata.embedding_metadata",
            write=other_repair,
        )
        stored = await _stored(content, uuid)
    finally:
        await content.close()
        await _drop_namespace(engine, content.namespace)

    assert (result.recovered, result.pending) == (0, 1)
    assert stored["stamp"] == large_stamp
    assert stored["embedding"] == large_vector
    assert stored["revision"] == 7


@pytest.mark.asyncio
async def test_a_reembed_never_overwrites_a_vector_another_repair_just_wrote(engine) -> None:
    from sibyl_core.services import content_client
    from sibyl_core.services.content_models import raw_memory_embedding_metadata

    small = _provider(_SMALL, EMBEDDING_DIM, "raw-memory")
    large = _provider(_LARGE, EMBEDDING_DIM, "raw-memory")
    organization_id = str(uuid4())
    content = _content(engine)
    large_stamp = raw_memory_embedding_metadata(large.metadata)
    large_vector = _unit(1, EMBEDDING_DIM)
    try:
        await bootstrap_content_schema(content, reset=True)
        uuid = await _raw_capture(
            content,
            organization_id,
            stamp=previous_release_stamp({**large_stamp, "model": "an-older-model"}),
            vector=_unit(0, EMBEDDING_DIM),
        )

        async def other_repair() -> None:
            await content_client.select_many(
                content,
                "UPDATE raw_captures SET embedding = $vector, "
                "metadata.embedding_metadata = $stamp WHERE uuid = $uuid RETURN NONE;",
                vector=large_vector,
                stamp=large_stamp,
                uuid=uuid,
            )

        result = await _repair_racing(
            content,
            organization_id,
            small,
            before="embedding = $embedding,",
            write=other_repair,
        )
        stored = await _stored(content, uuid)
    finally:
        await content.close()
        await _drop_namespace(engine, content.namespace)

    assert (result.recovered, result.pending) == (0, 1)
    assert stored["stamp"] == large_stamp
    assert stored["embedding"] == large_vector


@pytest.mark.asyncio
async def test_a_plane_with_a_switch_in_another_organization_never_mixes_models(
    engine, lane_clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """X: unstamped small vectors and newer large stamps. Y: small stamps. Configured: large.

    X's own snapshot names only the configured model, but Y's names the one
    X's unstamped vectors came from. No query on X may score those vectors
    against a large-model query, before, during or after X's verdict, and the
    verdict is a re-embed whichever organization settles first.
    """
    from sibyl_core.services import content_client
    from sibyl_core.services.embedding_verdicts import settle_legacy_verdicts

    async def no_chunks(_rows: object) -> tuple[list[list[float]], dict[str, object]]:
        raise AssertionError("no chunk plane is configured here")

    small = _provider(_SMALL, GRAPH_EMBEDDING_DIM, "graph")
    large = _provider(_LARGE, GRAPH_EMBEDDING_DIM, "graph")
    query = (await large.embed_texts(["mixed query"], input_kind="query"))[0]
    content = _content(engine)
    x = SurrealGraphClient(
        group_id=str(uuid4()),
        url=_url(engine, "x"),
        username=engine["username"],
        password=engine["password"],
    )
    y = SurrealGraphClient(
        group_id=str(uuid4()),
        url=_url(engine, "y"),
        username=engine["username"],
        password=engine["password"],
    )

    async def seed(client, rows) -> None:
        await client.execute_query(
            "INSERT INTO entity $rows RETURN NONE;",
            rows=[
                {
                    "group_id": client.group_id,
                    "entity_type": "topic",
                    "created_at": datetime.now(UTC),
                    **row,
                }
                for row in rows
            ],
        )

    legacy = {f"legacy-{index}" for index in range(3)}
    trace: list[tuple[str, set[str], bool]] = []
    try:
        await bootstrap_content_schema(content, reset=True)

        @asynccontextmanager
        async def session():
            yield content

        monkeypatch.setattr(content_client, "surreal_content_client", session)
        for client in (x, y):
            await prepare_graph_schema(client)
        await seed(
            x,
            [
                {
                    "uuid": uuid,
                    "name": uuid,
                    "name_embedding": list(query),
                    "attributes": {},
                }
                for uuid in sorted(legacy)
            ]
            + [
                {
                    "uuid": "stamped-large",
                    "name": "stamped large",
                    "name_embedding": _unit(5, GRAPH_EMBEDDING_DIM),
                    "attributes": {
                        "embedding_metadata": previous_release_stamp(large.metadata.to_dict())
                    },
                }
            ],
        )
        await seed(
            y,
            [
                {
                    "uuid": "native-small",
                    "name": "native small",
                    "name_embedding": _unit(6, GRAPH_EMBEDDING_DIM),
                    "attributes": {
                        "embedding_metadata": previous_release_stamp(small.metadata.to_dict())
                    },
                }
            ],
        )
        for client in (x, y):
            await upgrade_graph_to_sweep(client)
        searcher = EntityManager(x, group_id=x.group_id, embedding_provider=large)
        organizations = [x.group_id, y.group_id]

        async def look(step: str) -> None:
            from sibyl_core.services.embedding_lane_readiness import vector_lane_readiness

            readiness = await vector_lane_readiness(
                plane="graph",
                organization_id=x.group_id,
                execute=x.execute_query,
                query_stamp=large.metadata.to_dict(),
            )
            hits = await searcher._vector_search(query="mixed query", entity_types=None, limit=5)
            trace.append((step, {entity.id for entity, _ in hits}, readiness.admit_unstamped))
            lane_clock()

        async def settle(client):
            return await settle_legacy_verdicts(
                client.group_id,
                graph_client=client,
                graph_provider=large,
                chunk_stamp=None,
                embed_chunks=no_chunks,
                client=content,
                deployment_organizations=organizations,
            )

        await look("before any verdict")
        first = await settle(x)
        await look("x waiting for y")
        await settle(y)
        second = await settle(x)
        await look("after x's verdict")
    finally:
        await content.close()
        for client in (x, y):
            await client.close()
        with suppress(Exception):
            await _drop_namespace(engine, content.namespace)
        for client in (x, y):
            with suppress(Exception):
                await _drop_namespace(engine, client.namespace)

    assert first.deferred
    assert isinstance(second.graph, dict)
    assert second.graph["legacy_decision"] == "reembed"
    assert second.graph["legacy_basis"] == "deployment_stamps_differ"
    for step, hits, admitted in trace:
        assert not hits & legacy, step
        assert not admitted, step


@pytest.mark.asyncio
async def test_a_capture_stamped_null_is_embedded_on_the_first_pass(engine) -> None:
    """The previous release could keep a client's null stamp; NULL never equals NONE."""
    from sibyl_core.services import content_client
    from sibyl_core.services.content_models import raw_memory_embedding_metadata
    from sibyl_core.services.content_raw_embedding_repair import repair_raw_capture_embeddings

    small = _provider(_SMALL, EMBEDDING_DIM, "raw-memory")
    organization_id = str(uuid4())
    content = _content(engine)
    try:
        await bootstrap_content_schema(content, reset=True)
        uuid = str(uuid4())
        await content_client.select_many(
            content,
            "CREATE raw_captures CONTENT {uuid: $uuid, organization_id: $organization_id, "
            "principal_id: 'owner', source_id: $source, raw_content: 'captured', "
            "embedding: $vector, metadata: {embedding_metadata: NULL}} RETURN NONE;",
            uuid=uuid,
            organization_id=organization_id,
            source=str(uuid4()),
            vector=_unit(0, EMBEDDING_DIM),
        )
        first = await repair_raw_capture_embeddings(
            organization_id, embedding_provider=small, client=content
        )
        second = await repair_raw_capture_embeddings(
            organization_id, embedding_provider=small, client=content
        )
        stored = await _stored(content, uuid)
    finally:
        await content.close()
        await _drop_namespace(engine, content.namespace)

    assert (first.recovered, first.pending) == (1, 0)
    assert second.checked == 0
    assert stored["stamp"] == raw_memory_embedding_metadata(small.metadata)


@pytest.mark.asyncio
async def test_a_graph_vector_stamped_null_is_adopted_and_proves_nothing(engine) -> None:
    from sibyl_core.backends.surreal.records import normalize_records
    from sibyl_core.services.embedding_evidence import read_graph_snapshot
    from sibyl_core.services.graph import RelationshipManager
    from sibyl_core.services.graph_embedding_sweep import sweep_graph_embeddings
    from sibyl_core.services.graph_runtime import GraphRuntime

    small = _provider(_SMALL, GRAPH_EMBEDDING_DIM, "graph")
    client = SurrealGraphClient(
        group_id=str(uuid4()),
        url=_url(engine, "graph"),
        username=engine["username"],
        password=engine["password"],
    )
    try:
        await prepare_graph_schema(client)
        await client.execute_query(
            "INSERT INTO entity $rows RETURN NONE;",
            rows=[
                {
                    "uuid": "stamped",
                    "group_id": client.group_id,
                    "name": "stamped",
                    "entity_type": "topic",
                    "name_embedding": _unit(1, GRAPH_EMBEDDING_DIM),
                    "attributes": {
                        "embedding_metadata": previous_release_stamp(small.metadata.to_dict())
                    },
                }
            ],
        )
        await client.execute_query(
            "CREATE entity CONTENT {uuid: 'null-stamped', group_id: $group, name: 'null', "
            "entity_type: 'topic', name_embedding: $vector, "
            "attributes: {embedding_metadata: NULL}} RETURN NONE;",
            group=client.group_id,
            vector=_unit(2, GRAPH_EMBEDDING_DIM),
        )
        await upgrade_graph_to_sweep(client)
        snapshot = await read_graph_snapshot(client.execute_query, client.group_id)
        runtime = GraphRuntime(
            client=client,
            entity_manager=EntityManager(client, group_id=client.group_id),
            relationship_manager=RelationshipManager(client, group_id=client.group_id),
        )
        swept = await sweep_graph_embeddings(runtime, embedding_provider=small)
        stored = normalize_records(
            await client.execute_query(
                "SELECT attributes.embedding_metadata AS stamp FROM entity "
                "WHERE uuid = 'null-stamped';"
            )
        )
    finally:
        await client.close()
        with suppress(Exception):
            await _drop_namespace(engine, client.namespace)

    # Only the real stamp speaks for the plane; the null one is a vector from
    # before stamping, adopted without an embedding call.
    assert [(stamp["model"], stamp["rows"]) for stamp in snapshot] == [(_SMALL, 1)]
    assert swept.adopted == 1
    assert swept.recovered == 0
    assert stored[0]["stamp"] == small.metadata.to_dict()
