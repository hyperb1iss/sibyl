"""Writes and reads that race the upgrade's own bookkeeping, on both engines.

Raw capture repair holds no lease, so two processes configured for different
models (a rolling deploy) can repair the same capture at once. Every case
runs on embedded SurrealKV and, when the live server is enabled, on native
SurrealDB.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from sibyl_core.backends.surreal import SurrealContentClient, bootstrap_content_schema
from sibyl_core.backends.surreal.content_schema import EMBEDDING_DIM
from sibyl_core.embeddings.providers import DeterministicEmbeddingProvider, EmbeddingMetadata
from tests.embedding_upgrade import previous_release_stamp
from tests.test_vector_lane_crowding import _EMBEDDED, _drop_namespace, engine

__all__ = ["engine"]

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
