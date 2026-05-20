from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from sibyl.config import settings
from sibyl.jobs import memory_extraction
from sibyl_core.models.memory_extraction import (
    ExtractedMemoryEntity,
    MemoryBatchEntityExtractionResult,
    SourceMemoryExtraction,
)
from sibyl_core.observability import telemetry_registry


class FakeExtractor:
    def __init__(self) -> None:
        self.prompts: list[str] = []
        self.max_concurrent: int | None = None

    async def extract_many(
        self,
        prompts: list[str],
        *,
        max_concurrent: int,
    ) -> list[MemoryBatchEntityExtractionResult]:
        self.prompts = prompts
        self.max_concurrent = max_concurrent
        return [
            MemoryBatchEntityExtractionResult(
                sources=[
                    SourceMemoryExtraction(
                        source_id="session-created",
                        entities=[
                            ExtractedMemoryEntity(
                                name="SurrealDB",
                                entity_type="tool",
                                summary="Native graph database",
                                confidence=0.9,
                            )
                        ],
                    )
                ],
            )
            for _ in prompts
        ]


class FakeQueue:
    def __init__(self, *, queue_depth: int = 0) -> None:
        self.queue_depth = queue_depth
        self.calls: list[dict[str, object]] = []

    async def health(self) -> dict[str, object]:
        return {"queue_depth": self.queue_depth}

    async def enqueue_memory_extraction(self, sources_data, group_id, **kwargs) -> str:
        job_id = f"extract-{len(self.calls)}"
        self.calls.append(
            {
                "sources_data": sources_data,
                "group_id": group_id,
                "kwargs": kwargs,
                "job_id": job_id,
            }
        )
        return job_id


@pytest.mark.asyncio
async def test_extract_memory_entities_runs_bounded_llm_extraction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeExtractor()
    entity_manager = SimpleNamespace(
        create_direct_bulk=AsyncMock(
            side_effect=lambda entities, **_: [entity.id for entity in entities]
        )
    )
    relationship_manager = SimpleNamespace(create_bulk=AsyncMock(return_value=(1, 0)))

    async def fake_runtime(*_: object, **__: object) -> SimpleNamespace:
        return SimpleNamespace(
            entity_manager=entity_manager,
            relationship_manager=relationship_manager,
        )

    monkeypatch.setattr(memory_extraction, "memory_batch_entity_extractor", lambda **_: fake)
    monkeypatch.setattr(memory_extraction, "get_native_graph_runtime", fake_runtime)

    result = await memory_extraction.extract_memory_entities(
        {},
        [
            {
                "id": "session-original",
                "entity_type": "session",
                "name": "Session",
                "content": "SurrealDB 3.0 adds native RRF for graph retrieval.",
            }
        ],
        "org-123",
        created_source_ids=["session-created"],
        max_entities_per_source=4,
        max_source_chars=20,
        max_concurrent=1,
        max_tokens=512,
    )

    assert result["sources"] == 1
    assert result["extracted_entities"] == 1
    assert result["projected_entities"] == 1
    assert result["relationships"] == 1
    assert result["extractions"][0]["source_id"] == "session-created"
    assert result["extractions"][0]["entities"][0]["name"] == "SurrealDB"
    assert fake.max_concurrent == 1
    assert len(fake.prompts) == 1
    assert "source_id: session-created" in fake.prompts[0]
    assert "SurrealDB 3.0 adds n" in fake.prompts[0]
    assert "native RRF" not in fake.prompts[0]
    created_entities = entity_manager.create_direct_bulk.await_args.args[0]
    assert created_entities[0].metadata["projection_extractor"] == "llm"


@pytest.mark.asyncio
async def test_enqueue_memory_extraction_batches_skips_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queue = FakeQueue()
    monkeypatch.setattr(settings, "auto_extract_entities", False)
    monkeypatch.setattr(memory_extraction, "get_queue", lambda: queue)

    result = await memory_extraction.enqueue_memory_extraction_batches(
        [{"id": "session", "entity_type": "session", "content": "memory"}],
        "org-123",
    )

    assert result.status == "skipped"
    assert result.reason == "disabled"
    assert queue.calls == []


@pytest.mark.asyncio
async def test_enqueue_memory_extraction_batches_applies_queue_backpressure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queue = FakeQueue(queue_depth=250)
    monkeypatch.setattr(settings, "auto_extract_entities", True)
    monkeypatch.setattr(settings, "memory_extraction_max_queue_depth", 250)
    monkeypatch.setattr(memory_extraction, "get_queue", lambda: queue)

    result = await memory_extraction.enqueue_memory_extraction_batches(
        [{"id": "session", "entity_type": "session", "content": "memory"}],
        "org-123",
    )

    assert result.status == "backpressure"
    assert result.reason == "queue_depth"
    assert queue.calls == []


@pytest.mark.asyncio
async def test_enqueue_memory_extraction_batches_chunks_bounded_jobs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queue = FakeQueue(queue_depth=1)
    monkeypatch.setattr(settings, "auto_extract_entities", True)
    monkeypatch.setattr(settings, "memory_extraction_max_queue_depth", 10)
    monkeypatch.setattr(settings, "memory_extraction_max_sources_per_job", 1)
    monkeypatch.setattr(settings, "memory_extraction_max_source_chars", 8)
    monkeypatch.setattr(settings, "memory_extraction_max_job_chars", 20)
    monkeypatch.setattr(settings, "memory_extraction_max_entities_per_source", 3)
    monkeypatch.setattr(settings, "memory_extraction_max_concurrency", 1)
    monkeypatch.setattr(settings, "memory_extraction_max_tokens", 512)
    monkeypatch.setattr(memory_extraction, "get_queue", lambda: queue)

    result = await memory_extraction.enqueue_memory_extraction_batches(
        [
            {"id": "session-a", "entity_type": "session", "content": "abcdefghijk"},
            {"id": "pattern-a", "entity_type": "pattern", "content": "ignored"},
            {"id": "session-b", "entity_type": "session", "content": "second memory"},
        ],
        "org-123",
        created_source_ids=["created-a", "pattern-a", "created-b"],
    )

    assert result.status == "queued"
    assert result.job_ids == ("extract-0", "extract-1")
    assert result.queued_sources == 2
    assert result.skipped_sources == 0
    assert queue.calls[0]["sources_data"] == [
        {"id": "session-a", "entity_type": "session", "content": "abcdefgh"}
    ]
    first_kwargs = queue.calls[0]["kwargs"]
    assert first_kwargs["created_source_ids"] == ["created-a"]
    assert first_kwargs["max_entities_per_source"] == 3
    assert first_kwargs["max_source_chars"] == 8
    assert first_kwargs["max_concurrent"] == 1
    assert first_kwargs["max_tokens"] == 512


@pytest.mark.asyncio
async def test_enqueue_memory_extraction_batches_reports_partial_backpressure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queue = FakeQueue(queue_depth=9)
    monkeypatch.setattr(settings, "auto_extract_entities", True)
    monkeypatch.setattr(settings, "memory_extraction_max_queue_depth", 10)
    monkeypatch.setattr(settings, "memory_extraction_max_sources_per_job", 1)
    monkeypatch.setattr(settings, "memory_extraction_max_source_chars", 128)
    monkeypatch.setattr(settings, "memory_extraction_max_job_chars", 128)
    monkeypatch.setattr(settings, "memory_extraction_max_entities_per_source", 3)
    monkeypatch.setattr(settings, "memory_extraction_max_concurrency", 1)
    monkeypatch.setattr(settings, "memory_extraction_max_tokens", 512)
    monkeypatch.setattr(memory_extraction, "get_queue", lambda: queue)

    result = await memory_extraction.enqueue_memory_extraction_batches(
        [
            {"id": "session-a", "entity_type": "session", "content": "first memory"},
            {"id": "session-b", "entity_type": "session", "content": "second memory"},
        ],
        "org-123",
        created_source_ids=["created-a", "created-b"],
    )

    assert result.status == "partial"
    assert result.job_ids == ("extract-0",)
    assert result.queued_sources == 1
    assert result.skipped_sources == 1
    assert result.reason == "queue_depth"
    assert len(queue.calls) == 1


def test_memory_extraction_telemetry_records_enqueue_and_run() -> None:
    telemetry_registry().reset()

    telemetry_registry().record_memory_extraction_enqueue(
        status="queued",
        sources=2,
        batches=1,
        queue_depth=3,
    )
    telemetry_registry().record_memory_extraction_run(
        status="ok",
        duration_ms=42,
        sources=2,
        extracted_entities=3,
        estimated_input_tokens=128,
    )

    snapshot = telemetry_registry().snapshot()

    assert snapshot["summaries"]["memory_extraction"]["count"] == 2
    assert any(
        metric["name"] == "sibyl_memory_extraction_runs_total"
        for metric in snapshot["metrics"]
    )
