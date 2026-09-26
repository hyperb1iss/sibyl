"""Each start records the embedding models it runs with, without ever failing the start."""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest

from sibyl.jobs import embedding_sweep as sweep_jobs
from sibyl_core.embeddings.providers import DeterministicEmbeddingProvider, EmbeddingMetadata


class _Service:
    async def chunk_embedding_metadata(self):
        return {"provider": "bedrock", "model": "cohere.embed-v4:0", "dimensions": 1536}, False


@pytest.mark.asyncio
async def test_start_records_the_graph_and_content_models(monkeypatch) -> None:
    provider = DeterministicEmbeddingProvider(
        EmbeddingMetadata(
            provider="bedrock",
            model="cohere.embed-v4:0",
            dimensions=1024,
            cache_namespace="graph",
            tokenizer_estimate_method="provider-default",
        )
    )
    client = object()

    @asynccontextmanager
    async def session():
        yield client

    record = AsyncMock(return_value={"graph": {}, "content": {}})
    monkeypatch.setattr(sweep_jobs, "configured_embedding_provider", lambda: provider)
    monkeypatch.setattr(sweep_jobs.content_client, "surreal_content_client", session)
    monkeypatch.setattr(sweep_jobs, "record_deployment_models", record)

    assert await sweep_jobs.record_configured_embedding_models(_Service()) == {
        "graph": {},
        "content": {},
    }

    kwargs = record.await_args.kwargs
    assert kwargs["graph"] == provider.metadata.to_dict()
    # A content model this process cannot run is still the configured one.
    assert kwargs["content"]["model"] == "cohere.embed-v4:0"


@pytest.mark.asyncio
async def test_a_failed_record_never_fails_the_start(monkeypatch) -> None:
    @asynccontextmanager
    async def session():
        raise ConnectionError("content store unavailable")
        yield

    monkeypatch.setattr(sweep_jobs, "configured_embedding_provider", lambda: None)
    monkeypatch.setattr(sweep_jobs.content_client, "surreal_content_client", session)

    assert await sweep_jobs.record_configured_embedding_models(_Service()) is None
