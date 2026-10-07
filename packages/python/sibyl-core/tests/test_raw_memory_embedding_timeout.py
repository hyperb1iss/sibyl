"""The raw write path bounds its embedding call and tokenizes off the loop."""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

import sibyl_core.embeddings.providers as embedding_providers
from sibyl_core.config import settings
from sibyl_core.embeddings.providers import EmbeddingMetadata, OpenAIEmbeddingProvider
from sibyl_core.services import content_raw_persistence as raw_persistence
from sibyl_core.services.content_models import RawMemory
from sibyl_core.services.content_raw_persistence import (
    _raw_memories_with_embeddings,
    _raw_memory_with_embedding,
)


class _Provider:
    """Embeds instantly, or hangs for `delay` seconds first."""

    def __init__(self, *, delay: float = 0.0) -> None:
        self.delay = delay
        self.calls = 0
        self.metadata = EmbeddingMetadata(
            provider="openai",
            model="text-embedding-test",
            dimensions=2,
            cache_namespace="raw-timeout-test",
            tokenizer_estimate_method="test",
        )

    async def embed_texts(self, texts: Sequence[str], *, input_kind: str = "document"):
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        return [[0.5, 0.5] for _ in texts]


def _memory(uuid: str) -> RawMemory:
    captured_at = datetime(2026, 10, 6, 12, 0, tzinfo=UTC).replace(tzinfo=None)
    return RawMemory(
        id=uuid,
        organization_id="org-timeout",
        source_id="source-timeout",
        principal_id="user-timeout",
        title=f"Timeout {uuid}",
        raw_content=f"raw content for {uuid}",
        captured_at=captured_at,
        created_at=captured_at,
    )


@pytest.mark.asyncio
async def test_a_slow_provider_stores_the_raw_memory_without_a_vector() -> None:
    provider = _Provider(delay=5.0)
    started = time.perf_counter()

    stored = await _raw_memory_with_embedding(_memory("raw-slow"), provider, timeout_seconds=0.05)

    assert time.perf_counter() - started < 1.0, "the write path must not wait on the provider"
    assert provider.calls == 1
    assert stored.embedding is None
    assert "embedding_metadata" not in stored.metadata


@pytest.mark.asyncio
async def test_a_fast_provider_still_embeds_inside_the_budget() -> None:
    provider = _Provider()

    stored = await _raw_memory_with_embedding(_memory("raw-fast"), provider, timeout_seconds=0.5)

    assert stored.embedding == [0.5, 0.5]
    assert stored.metadata["embedding_metadata"]["model"] == "text-embedding-test"


@pytest.mark.asyncio
async def test_a_slow_provider_leaves_a_batch_unembedded() -> None:
    provider = _Provider(delay=5.0)
    memories = [_memory("raw-a"), _memory("raw-b")]
    started = time.perf_counter()

    stored = await _raw_memories_with_embeddings(memories, provider, timeout_seconds=0.05)

    assert time.perf_counter() - started < 1.0
    assert [memory.id for memory in stored] == ["raw-a", "raw-b"]
    assert all(memory.embedding is None for memory in stored)


@pytest.mark.asyncio
async def test_the_write_path_uses_the_graph_embedding_budget(monkeypatch) -> None:
    monkeypatch.setattr(settings, "graph_embedding_timeout_seconds", 0.05)
    assert raw_persistence._raw_memory_embedding_timeout_seconds() == 0.05

    provider = _Provider(delay=5.0)
    started = time.perf_counter()
    stored = await raw_persistence._raw_memory_with_save_embedding(_memory("raw-save"), provider)

    assert time.perf_counter() - started < 1.0
    assert stored.embedding is None


@pytest.mark.asyncio
async def test_no_budget_means_no_timeout() -> None:
    provider = _Provider(delay=0.05)

    stored = await _raw_memory_with_embedding(_memory("raw-unbounded"), provider, timeout_seconds=0)

    assert stored.embedding == [0.5, 0.5]


@pytest.mark.asyncio
async def test_openai_tokenization_runs_off_the_event_loop(monkeypatch) -> None:
    """tiktoken's first load and a large body both stall the loop if run inline."""
    threads: list[threading.Thread] = []
    original = embedding_providers._prepare_openai_embedding_text

    def prepare(text: str, *, model: str) -> tuple[str, int]:
        threads.append(threading.current_thread())
        return original(text, model=model)

    monkeypatch.setattr(embedding_providers, "_prepare_openai_embedding_text", prepare)

    class Embeddings:
        async def create(self, **kwargs: object) -> SimpleNamespace:
            inputs = kwargs["input"]
            assert isinstance(inputs, list)
            return SimpleNamespace(
                data=[SimpleNamespace(embedding=[0.1, 0.2]) for _ in inputs],
                usage=SimpleNamespace(prompt_tokens=7, total_tokens=7),
            )

    provider = OpenAIEmbeddingProvider(
        metadata=EmbeddingMetadata(
            provider="openai",
            model="text-embedding-test",
            dimensions=2,
            cache_namespace="thread-test",
            tokenizer_estimate_method="test",
        ),
        client=SimpleNamespace(embeddings=Embeddings()),
    )

    assert await provider.embed_texts(["alpha", "beta"]) == [[0.1, 0.2], [0.1, 0.2]]
    assert len(threads) == 2
    assert all(thread is not threading.main_thread() for thread in threads)
