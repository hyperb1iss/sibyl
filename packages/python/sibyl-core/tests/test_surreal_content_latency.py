from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from math import ceil
from time import perf_counter

import pytest

from sibyl_core.backends.surreal.content_client import SurrealContentClient
from sibyl_core.backends.surreal.content_schema import bootstrap_content_schema
from sibyl_core.services.surreal_content import recall_raw_memory, remember_raw_memory


def _p95(samples: list[float]) -> float:
    ordered = sorted(samples)
    return ordered[ceil(len(ordered) * 0.95) - 1]


@pytest.mark.asyncio
async def test_raw_memory_local_p95_latency_targets(monkeypatch: pytest.MonkeyPatch) -> None:
    client = SurrealContentClient(url="memory://")
    await bootstrap_content_schema(client, reset=True)

    @asynccontextmanager
    async def content_client() -> AsyncIterator[SurrealContentClient]:
        yield client

    from sibyl_core.services import surreal_content as content_service

    monkeypatch.setattr(content_service, "surreal_content_client", content_client)
    try:
        await remember_raw_memory(
            organization_id="org-latency",
            principal_id="user-latency",
            source_id="warmup",
            raw_content="warmup native raw memory latency evidence",
            title="warmup",
        )

        remember_samples: list[float] = []
        for index in range(24):
            started = perf_counter()
            await remember_raw_memory(
                organization_id="org-latency",
                principal_id="user-latency",
                source_id=f"source-{index}",
                raw_content=f"native raw memory latency sample surreal prism {index}",
                title=f"Latency sample {index}",
                tags=["latency"],
            )
            remember_samples.append((perf_counter() - started) * 1000)

        recall_samples: list[float] = []
        for _ in range(12):
            started = perf_counter()
            memories = await recall_raw_memory(
                organization_id="org-latency",
                principal_id="user-latency",
                query="surreal prism",
                limit=8,
            )
            recall_samples.append((perf_counter() - started) * 1000)
            assert memories
            assert {memory.principal_id for memory in memories} == {"user-latency"}

        assert _p95(remember_samples) < 300
        assert _p95(recall_samples) < 1000
    finally:
        await client.close()
