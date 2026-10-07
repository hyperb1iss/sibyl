"""One query text is embedded once per vector space.

The raw-memory and document providers embed at the same model and dimensions
but kept separate caches under their own namespaces, so a search that reads
both lanes embedded the same query twice at 1536 dimensions. Providers in
one vector space now share a cache and an in-flight table; a provider in
another space (the 1024-dimension graph lanes) keeps its own.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

import pytest

from sibyl_core.embeddings.providers import (
    CachedEmbeddingProvider,
    EmbeddingInputKind,
    EmbeddingMetadata,
)


class _Inner:
    def __init__(self, metadata: EmbeddingMetadata, *, release: asyncio.Event | None = None):
        self.metadata = metadata
        self.calls: list[list[str]] = []
        self._release = release

    async def embed_texts(
        self, texts: Sequence[str], *, input_kind: EmbeddingInputKind = "document"
    ) -> list[list[float]]:
        self.calls.append(list(texts))
        if self._release is not None:
            await self._release.wait()
        return [[float(len(text))] * self.metadata.dimensions for text in texts]


def _metadata(namespace: str, *, dimensions: int = 1536) -> EmbeddingMetadata:
    return EmbeddingMetadata(
        provider="openai",
        model="text-embedding-3-small",
        dimensions=dimensions,
        cache_namespace=namespace,
        tokenizer_estimate_method="provider-default",
    )


@pytest.mark.asyncio
async def test_providers_in_one_vector_space_share_the_cache() -> None:
    raw = _Inner(_metadata("raw-memory"))
    document = _Inner(_metadata("document"))
    graph = _Inner(_metadata("graph", dimensions=1024))

    vectors = [
        (await CachedEmbeddingProvider(inner).embed_texts(["connection pool"], input_kind="query"))[
            0
        ]
        for inner in (raw, document, graph)
    ]

    assert raw.calls == [["connection pool"]]
    assert document.calls == [], "the document lane reused the raw-memory lane's vector"
    assert graph.calls == [["connection pool"]], "another space embeds for itself"
    assert vectors[0] == vectors[1]
    assert len(vectors[2]) == 1024


@pytest.mark.asyncio
async def test_concurrent_misses_across_providers_in_one_space_coalesce() -> None:
    release = asyncio.Event()
    raw = _Inner(_metadata("raw-memory"), release=release)
    document = _Inner(_metadata("document"), release=release)

    first = asyncio.create_task(
        CachedEmbeddingProvider(raw).embed_texts(["connection pool"], input_kind="query")
    )
    await asyncio.sleep(0)
    second = asyncio.create_task(
        CachedEmbeddingProvider(document).embed_texts(["connection pool"], input_kind="query")
    )
    await asyncio.sleep(0)
    release.set()

    assert (await first) == (await second)
    assert raw.calls == [["connection pool"]]
    assert document.calls == []
