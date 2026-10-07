"""A remembered graph dimension is re-read when the store starts refusing the sweep's writes."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

from sibyl_core.embeddings.providers import EmbeddingMetadata
from sibyl_core.services import graph_embedding_sweep
from sibyl_core.services.embedding_sweep import (
    SWEEP_SKIPPED_DIMENSION_MISMATCH,
    SWEEP_STORE_FAILING,
    EmbeddingSweepResult,
)
from sibyl_core.services.graph_client import graph_schema_fact


class _Client:
    def __init__(self) -> None:
        self.group_id = "org-dim"

    async def execute_query(self, query: str, **params: object) -> object:
        return []


async def test_store_failure_rereads_a_remembered_dimension_and_redecides(monkeypatch) -> None:
    client = _Client()
    runtime = SimpleNamespace(client=client)
    provider = SimpleNamespace(
        metadata=EmbeddingMetadata(
            provider="deterministic",
            model="graph-v1",
            dimensions=1536,
            cache_namespace="graph",
            tokenizer_estimate_method="sha256",
        )
    )
    monkeypatch.setattr(
        graph_embedding_sweep, "graph_sweep_schema_ready", AsyncMock(return_value=True)
    )
    # The namespace was rebuilt to 3072 by another process after 1536 was remembered.
    dimensions = AsyncMock(side_effect=[1536, 3072])
    monkeypatch.setattr(graph_embedding_sweep, "get_schema_embedding_dimension", dimensions)
    assert await graph_embedding_sweep.recorded_graph_embedding_dimension(client) == 1536
    planes = []

    async def run(plane, **_options):
        planes.append(plane.schema_dimensions)
        if len(planes) == 1:
            return EmbeddingSweepResult(plane="graph", status=SWEEP_STORE_FAILING)
        return EmbeddingSweepResult(plane="graph", status=SWEEP_SKIPPED_DIMENSION_MISMATCH)

    monkeypatch.setattr(graph_embedding_sweep, "run_embedding_sweep", run)

    result = await graph_embedding_sweep.sweep_graph_embeddings(
        runtime, embedding_provider=provider
    )

    assert result.status == SWEEP_SKIPPED_DIMENSION_MISMATCH
    assert planes == [1536, 3072], "the pass is decided again on the fresh dimension"
    assert dimensions.await_count == 2
    assert graph_schema_fact("org-dim", "embedding_dimension") == 3072


async def test_store_failure_on_an_unchanged_dimension_stands(monkeypatch) -> None:
    client = _Client()
    runtime = SimpleNamespace(client=client)
    provider = SimpleNamespace(
        metadata=EmbeddingMetadata(
            provider="deterministic",
            model="graph-v1",
            dimensions=1536,
            cache_namespace="graph",
            tokenizer_estimate_method="sha256",
        )
    )
    monkeypatch.setattr(
        graph_embedding_sweep, "graph_sweep_schema_ready", AsyncMock(return_value=True)
    )
    dimensions = AsyncMock(return_value=1536)
    monkeypatch.setattr(graph_embedding_sweep, "get_schema_embedding_dimension", dimensions)
    assert await graph_embedding_sweep.recorded_graph_embedding_dimension(client) == 1536
    failing = EmbeddingSweepResult(plane="graph", status=SWEEP_STORE_FAILING)
    run = AsyncMock(return_value=failing)
    monkeypatch.setattr(graph_embedding_sweep, "run_embedding_sweep", run)

    result = await graph_embedding_sweep.sweep_graph_embeddings(
        runtime, embedding_provider=provider
    )

    assert result is failing
    assert run.await_count == 1, "a genuine store failure is not swept twice"
    assert dimensions.await_count == 2, "the dimension is re-read once, from the database"
