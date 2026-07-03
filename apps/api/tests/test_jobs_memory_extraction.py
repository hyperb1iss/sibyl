from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from sibyl.config import settings
from sibyl.jobs import memory_extraction
from sibyl.persistence.content_common import DocumentChunkRecord
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
    monkeypatch.setattr(memory_extraction, "get_surreal_graph_runtime", fake_runtime)

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
        max_source_chars=28,
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
    assert "SurrealDB 3.0 adds native RR" in fake.prompts[0]
    assert "native RRF" not in fake.prompts[0]
    created_entities = entity_manager.create_direct_bulk.await_args.args[0]
    assert created_entities[0].metadata["projection_extractor"] == "llm"


@pytest.mark.asyncio
async def test_extract_memory_entities_links_document_chunks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeExtractor()
    document_id = uuid4()
    organization_id = uuid4()
    chunk = DocumentChunkRecord(
        id=uuid4(),
        document_id=document_id,
        organization_id=organization_id,
        source_id="source-1",
        chunk_index=0,
        content="SurrealDB 3.0 adds native RRF.",
    )
    other_chunk = DocumentChunkRecord(
        id=uuid4(),
        document_id=document_id,
        organization_id=organization_id,
        source_id="source-1",
        chunk_index=1,
        content="The second chunk talks about unrelated retrieval plumbing.",
    )
    saved_chunks: list[DocumentChunkRecord] = []
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

    @asynccontextmanager
    async def fake_session():
        yield None

    async def fake_list_chunks(_session, *, document_id: object, organization_id: object):
        assert str(document_id) == str(chunk.document_id)
        assert str(organization_id) == str(chunk.organization_id)
        return [chunk, other_chunk]

    async def fake_save_chunks(_session, *, chunks):
        saved_chunks.extend(chunks)
        return chunks

    monkeypatch.setattr(memory_extraction, "memory_batch_entity_extractor", lambda **_: fake)
    monkeypatch.setattr(memory_extraction, "get_surreal_graph_runtime", fake_runtime)
    monkeypatch.setattr(memory_extraction, "get_content_read_session", fake_session)
    monkeypatch.setattr(memory_extraction, "list_document_chunks", fake_list_chunks)
    monkeypatch.setattr(memory_extraction, "save_document_chunks", fake_save_chunks)

    result = await memory_extraction.extract_memory_entities(
        {},
        [
            {
                "id": "document-original",
                "entity_type": "document",
                "name": "Document",
                "content": "SurrealDB 3.0 adds native RRF for graph retrieval.",
                "organization_id": str(organization_id),
                "metadata": {"document_id": str(document_id)},
            }
        ],
        str(organization_id),
        created_source_ids=["session-created"],
        max_entities_per_source=4,
        max_source_chars=200,
        max_concurrent=1,
        max_tokens=512,
    )

    created_entities = entity_manager.create_direct_bulk.await_args.args[0]
    assert result["linked_chunks"] == 1
    assert saved_chunks == [chunk]
    assert saved_chunks[0].has_entities
    assert saved_chunks[0].entity_ids == [created_entities[0].id]
    assert other_chunk.entity_ids == []


@pytest.mark.asyncio
async def test_extract_memory_entities_rejects_cross_org_chunk_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeExtractor()
    document_id = uuid4()
    organization_id = uuid4()
    other_organization_id = uuid4()
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

    @asynccontextmanager
    async def fake_session():
        yield None

    async def fake_list_chunks(_session, *, document_id: object, organization_id: object):
        raise AssertionError("cross-org payload must not query document chunks")

    async def fake_save_chunks(_session, *, chunks):
        raise AssertionError("cross-org payload must not write document chunks")

    monkeypatch.setattr(memory_extraction, "memory_batch_entity_extractor", lambda **_: fake)
    monkeypatch.setattr(memory_extraction, "get_surreal_graph_runtime", fake_runtime)
    monkeypatch.setattr(memory_extraction, "get_content_read_session", fake_session)
    monkeypatch.setattr(memory_extraction, "list_document_chunks", fake_list_chunks)
    monkeypatch.setattr(memory_extraction, "save_document_chunks", fake_save_chunks)

    result = await memory_extraction.extract_memory_entities(
        {},
        [
            {
                "id": "document-original",
                "entity_type": "document",
                "name": "Document",
                "content": "SurrealDB 3.0 adds native RRF for graph retrieval.",
                "organization_id": str(other_organization_id),
                "metadata": {
                    "document_id": str(document_id),
                    "organization_id": str(other_organization_id),
                },
            }
        ],
        str(organization_id),
        created_source_ids=["session-created"],
        max_entities_per_source=4,
        max_source_chars=200,
        max_concurrent=1,
        max_tokens=512,
    )

    assert result["linked_chunks"] == 0
    assert "session-created:organization_mismatch" in result["projection_errors"]


@pytest.mark.asyncio
async def test_extract_memory_entities_noops_low_signal_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        memory_extraction,
        "memory_batch_entity_extractor",
        lambda **_: pytest.fail("low-signal sources must not call the extractor"),
    )
    monkeypatch.setattr(
        memory_extraction,
        "get_surreal_graph_runtime",
        AsyncMock(side_effect=AssertionError("low-signal sources must not project")),
    )

    result = await memory_extraction.extract_memory_entities(
        {},
        [
            {
                "id": "session-low-signal",
                "entity_type": "session",
                "name": "Tiny acknowledgement",
                "content": "ok thanks",
            }
        ],
        "org-123",
        created_source_ids=["created-low-signal"],
        max_entities_per_source=4,
        max_source_chars=200,
        max_concurrent=1,
        max_tokens=512,
    )

    assert result["sources"] == 0
    assert result["extracted_entities"] == 0
    assert result["projected_entities"] == 0
    assert result["no_op_sources"] == 1
    assert result["low_signal_sources"] == [
        {"source_id": "created-low-signal", "reason": "low_signal"}
    ]
    assert result["extractions"] == []


@pytest.mark.asyncio
async def test_extract_memory_entities_noops_trimmed_low_signal_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        memory_extraction,
        "memory_batch_entity_extractor",
        lambda **_: pytest.fail("trimmed low-signal sources must not call the extractor"),
    )
    monkeypatch.setattr(
        memory_extraction,
        "get_surreal_graph_runtime",
        AsyncMock(side_effect=AssertionError("trimmed low-signal sources must not project")),
    )

    result = await memory_extraction.extract_memory_entities(
        {},
        [
            {
                "id": "session-trimmed-low-signal",
                "entity_type": "session",
                "name": "Ack then details",
                "content": "ok thanks. SurrealDB native RRF powers Lumen graph retrieval.",
            }
        ],
        "org-123",
        created_source_ids=["created-trimmed-low-signal"],
        max_entities_per_source=4,
        max_source_chars=9,
        max_concurrent=1,
        max_tokens=512,
    )

    assert result["sources"] == 0
    assert result["extracted_entities"] == 0
    assert result["projected_entities"] == 0
    assert result["no_op_sources"] == 1
    assert result["low_signal_sources"] == [
        {"source_id": "created-trimmed-low-signal", "reason": "low_signal"}
    ]
    assert result["extractions"] == []


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
async def test_enqueue_memory_extraction_batches_accepts_document_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queue = FakeQueue()
    monkeypatch.setattr(settings, "auto_extract_entities", True)
    monkeypatch.setattr(settings, "memory_extraction_max_queue_depth", 10)
    monkeypatch.setattr(settings, "memory_extraction_max_sources_per_job", 10)
    monkeypatch.setattr(settings, "memory_extraction_max_source_chars", 128)
    monkeypatch.setattr(settings, "memory_extraction_max_job_chars", 1024)
    monkeypatch.setattr(settings, "memory_extraction_max_entities_per_source", 3)
    monkeypatch.setattr(settings, "memory_extraction_max_concurrency", 1)
    monkeypatch.setattr(settings, "memory_extraction_max_tokens", 512)
    monkeypatch.setattr(memory_extraction, "get_queue", lambda: queue)

    result = await memory_extraction.enqueue_memory_extraction_batches(
        [
            {
                "id": "raw-doc",
                "entity_type": "document",
                "name": "Imported capture",
                "content": "SurrealDB is mentioned in an imported email.",
            }
        ],
        "org-123",
        created_source_ids=["raw-doc"],
    )

    assert result.status == "queued"
    assert result.queued_sources == 1
    assert queue.calls[0]["sources_data"][0]["entity_type"] == "document"
    assert queue.calls[0]["kwargs"]["created_source_ids"] == ["raw-doc"]


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
        metric["name"] == "sibyl_memory_extraction_runs_total" for metric in snapshot["metrics"]
    )
