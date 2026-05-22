"""Queued LLM extraction for prose-bearing memory sources."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import structlog

from sibyl.config import settings
from sibyl.jobs.queue import get_queue
from sibyl_core.ai.errors import LLMError
from sibyl_core.ai.llm.budget import llm_budget_context
from sibyl_core.ai.memory_extraction import (
    build_memory_batch_entity_extraction_prompt,
    memory_batch_entity_extractor,
)
from sibyl_core.embeddings.native import configured_native_embedding_provider
from sibyl_core.models.entities import Entity
from sibyl_core.models.memory_extraction import (
    ExtractedMemoryEntity,
    SourceMemoryExtraction,
)
from sibyl_core.observability import elapsed_ms, telemetry_registry
from sibyl_core.projection import project_extracted_memory_entities
from sibyl_core.services.native_graph import get_native_graph_runtime

log = structlog.get_logger()

_PROJECTABLE_MEMORY_TYPES = frozenset({"episode", "session"})


@dataclass(frozen=True, slots=True)
class MemoryExtractionEnqueueResult:
    status: str
    job_ids: tuple[str, ...] = ()
    queued_sources: int = 0
    skipped_sources: int = 0
    queue_depth: int | None = None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class _SourcePayload:
    source: dict[str, Any]
    source_id: str
    char_count: int


async def enqueue_memory_extraction_batches(
    sources_data: list[dict[str, Any]],
    group_id: str,
    *,
    created_source_ids: list[str] | None = None,
) -> MemoryExtractionEnqueueResult:
    """Queue bounded memory extraction jobs when the feature flag allows it."""
    if not settings.auto_extract_entities:
        telemetry_registry().record_memory_extraction_enqueue(
            status="skipped",
            sources=len(sources_data),
        )
        return MemoryExtractionEnqueueResult(
            status="skipped",
            skipped_sources=len(sources_data),
            reason="disabled",
        )

    queue = get_queue()
    health = await queue.health()
    queue_depth = _int_value(health.get("queue_depth"))
    if queue_depth >= settings.memory_extraction_max_queue_depth:
        telemetry_registry().record_memory_extraction_enqueue(
            status="backpressure",
            sources=len(sources_data),
            queue_depth=queue_depth,
        )
        return MemoryExtractionEnqueueResult(
            status="backpressure",
            skipped_sources=len(sources_data),
            queue_depth=queue_depth,
            reason="queue_depth",
        )

    source_payloads = _source_payloads(
        sources_data,
        created_source_ids=created_source_ids,
        max_source_chars=settings.memory_extraction_max_source_chars,
    )
    batches = _batch_source_payloads(
        source_payloads,
        max_sources=settings.memory_extraction_max_sources_per_job,
        max_chars=settings.memory_extraction_max_job_chars,
    )
    if not batches:
        telemetry_registry().record_memory_extraction_enqueue(
            status="skipped",
            sources=len(sources_data),
            queue_depth=queue_depth,
        )
        return MemoryExtractionEnqueueResult(
            status="skipped",
            skipped_sources=len(sources_data),
            queue_depth=queue_depth,
            reason="no_projectable_sources",
        )

    available_slots = settings.memory_extraction_max_queue_depth - queue_depth
    batches_to_enqueue = batches[:available_slots]
    skipped_sources = len(source_payloads) - sum(len(batch) for batch in batches_to_enqueue)
    job_ids: list[str] = []
    for batch in batches_to_enqueue:
        job_id = await queue.enqueue_memory_extraction(
            [item.source for item in batch],
            group_id,
            created_source_ids=[item.source_id for item in batch],
            max_entities_per_source=settings.memory_extraction_max_entities_per_source,
            max_source_chars=settings.memory_extraction_max_source_chars,
            max_concurrent=settings.memory_extraction_max_concurrency,
            max_tokens=settings.memory_extraction_max_tokens,
        )
        job_ids.append(job_id)

    if job_ids and skipped_sources:
        status = "partial"
    else:
        status = "queued" if job_ids else "backpressure"
    telemetry_registry().record_memory_extraction_enqueue(
        status=status,
        sources=sum(len(batch) for batch in batches_to_enqueue),
        batches=len(job_ids),
        queue_depth=queue_depth,
    )
    return MemoryExtractionEnqueueResult(
        status=status,
        job_ids=tuple(job_ids),
        queued_sources=sum(len(batch) for batch in batches_to_enqueue),
        skipped_sources=skipped_sources,
        queue_depth=queue_depth,
        reason="queue_depth" if skipped_sources or not job_ids else None,
    )


async def extract_memory_entities(
    ctx: dict[str, Any],  # noqa: ARG001
    sources_data: list[dict[str, Any]],
    group_id: str,
    *,
    created_source_ids: list[str] | None = None,
    max_entities_per_source: int = 4,
    max_source_chars: int = 12_000,
    max_concurrent: int = 2,
    max_tokens: int = 8192,
) -> dict[str, Any]:
    """Run bounded LLM entity extraction for memory source prose."""
    started_at = time.perf_counter()
    source_payloads = _source_payloads(
        sources_data,
        created_source_ids=created_source_ids,
        max_source_chars=max_source_chars,
    )
    if not source_payloads:
        duration_ms = elapsed_ms(started_at)
        telemetry_registry().record_memory_extraction_run(
            status="skipped",
            duration_ms=duration_ms,
            sources=0,
            extracted_entities=0,
            estimated_input_tokens=0,
        )
        return {
            "group_id": group_id,
            "sources": 0,
            "extracted_entities": 0,
            "projected_entities": 0,
            "relationships": 0,
            "estimated_input_tokens": 0,
            "errors": [],
            "projection_errors": [],
            "extractions": [],
        }
    prompt = build_memory_batch_entity_extraction_prompt(
        sources=[
            {
                "source_id": source.source_id,
                "title": str(source.source.get("name") or source.source_id),
                "source_type": str(source.source.get("entity_type") or "memory"),
                "content": str(source.source.get("content") or ""),
            }
            for source in source_payloads
        ],
        max_entities_per_source=max_entities_per_source,
    )
    estimated_input_tokens = _estimate_tokens(prompt)
    extractor = memory_batch_entity_extractor(max_tokens=max_tokens)
    user_id = _first_non_empty(
        *(source.source.get("principal_id") for source in source_payloads),
        *(source.source.get("created_by_user_id") for source in source_payloads),
    )
    organization_id = _first_non_empty(
        *(source.source.get("organization_id") for source in source_payloads),
        group_id,
    )

    errors: list[dict[str, str]] = []
    extractions: list[dict[str, Any]] = []
    extracted_by_source_id: dict[str, list[ExtractedMemoryEntity]] = {}
    extracted_entities = 0
    with llm_budget_context(user_id=user_id, organization_id=organization_id):
        results = await extractor.extract_many([prompt], max_concurrent=max_concurrent)
    result = results[0] if results else None
    payloads_by_source_id = {source.source_id: source for source in source_payloads}
    if isinstance(result, LLMError) or result is None:
        error = result if isinstance(result, LLMError) else LLMError("empty extraction result")
        errors.extend(
            {
                "source_id": source.source_id,
                "error_type": type(error).__name__,
                "message": str(error),
            }
            for source in source_payloads
        )
    else:
        for source_result in result.sources:
            source_id = source_result.source_id
            if source_id not in payloads_by_source_id:
                errors.append(
                    {
                        "source_id": source_id,
                        "error_type": "UnknownSourceID",
                        "message": "LLM returned an extraction for an unknown source_id",
                    }
                )
                continue
            limited_entities = _limited_entities(source_result, max_entities_per_source)
            extracted_by_source_id[source_id] = limited_entities
            entities = [entity.model_dump(mode="json") for entity in limited_entities]
            extractions.append({"source_id": source_id, "entities": entities})
            extracted_entities += len(entities)
        for source in source_payloads:
            if source.source_id not in extracted_by_source_id:
                extracted_by_source_id[source.source_id] = []
                extractions.append({"source_id": source.source_id, "entities": []})

    projection = await _project_extracted_entities(
        source_payloads,
        group_id=group_id,
        extracted_by_source_id=extracted_by_source_id,
        max_entities_per_source=max_entities_per_source,
    )
    projection_errors = list(projection["errors"])
    has_errors = bool(errors or projection_errors)
    status = "ok" if not has_errors else "partial" if extractions else "error"
    duration_ms = elapsed_ms(started_at)
    telemetry_registry().record_memory_extraction_run(
        status=status,
        duration_ms=duration_ms,
        sources=len(source_payloads),
        extracted_entities=extracted_entities,
        estimated_input_tokens=estimated_input_tokens,
        projected_entities=int(projection["projected_entities"]),
        relationships=int(projection["relationships"]),
        projection_errors=len(projection_errors),
    )
    result = {
        "group_id": group_id,
        "sources": len(source_payloads),
        "extracted_entities": extracted_entities,
        "projected_entities": projection["projected_entities"],
        "relationships": projection["relationships"],
        "estimated_input_tokens": estimated_input_tokens,
        "errors": errors,
        "projection_errors": projection_errors,
        "extractions": extractions,
    }
    if has_errors:
        log.warning("memory_extraction_complete", status=status, **result)
    else:
        log.info("memory_extraction_complete", status=status, **result)
    return result


def _source_payloads(
    sources_data: list[dict[str, Any]],
    *,
    created_source_ids: list[str] | None,
    max_source_chars: int,
) -> list[_SourcePayload]:
    payloads: list[_SourcePayload] = []
    for index, source in enumerate(sources_data):
        entity_type = str(source.get("entity_type") or "").lower()
        if entity_type not in _PROJECTABLE_MEMORY_TYPES:
            continue
        content = str(source.get("content") or source.get("description") or "").strip()
        if not content:
            continue
        trimmed_content = content[:max_source_chars]
        source_id = (
            created_source_ids[index]
            if created_source_ids is not None and index < len(created_source_ids)
            else str(source.get("id") or "")
        )
        payload = {**source, "content": trimmed_content}
        payloads.append(
            _SourcePayload(
                source=payload,
                source_id=source_id,
                char_count=len(trimmed_content),
            )
        )
    return payloads


def _batch_source_payloads(
    payloads: list[_SourcePayload],
    *,
    max_sources: int,
    max_chars: int,
) -> list[list[_SourcePayload]]:
    batches: list[list[_SourcePayload]] = []
    current: list[_SourcePayload] = []
    current_chars = 0
    for payload in payloads:
        would_exceed_sources = len(current) >= max_sources
        would_exceed_chars = current_chars + payload.char_count > max_chars
        if current and (would_exceed_sources or would_exceed_chars):
            batches.append(current)
            current = []
            current_chars = 0
        current.append(payload)
        current_chars += payload.char_count
    if current:
        batches.append(current)
    return batches


def _limited_entities(
    result: SourceMemoryExtraction,
    max_entities: int,
) -> list[ExtractedMemoryEntity]:
    return result.entities[:max(1, max_entities)]


async def _project_extracted_entities(
    source_payloads: list[_SourcePayload],
    *,
    group_id: str,
    extracted_by_source_id: dict[str, list[ExtractedMemoryEntity]],
    max_entities_per_source: int,
) -> dict[str, Any]:
    if not extracted_by_source_id:
        return {"projected_entities": 0, "relationships": 0, "errors": []}

    try:
        sources = [Entity.model_validate(source.source) for source in source_payloads]
        runtime = await get_native_graph_runtime(
            group_id,
            embedding_provider=configured_native_embedding_provider(),
        )
        projection = await project_extracted_memory_entities(
            entity_manager=runtime.entity_manager,
            relationship_manager=runtime.relationship_manager,
            sources=sources,
            extractions_by_source_id=extracted_by_source_id,
            group_id=group_id,
            created_source_ids=[source.source_id for source in source_payloads],
            max_entities=max_entities_per_source,
            generate_embeddings=False,
        )
        return {
            "projected_entities": projection.projected_entities,
            "relationships": projection.relationships,
            "errors": list(projection.errors),
        }
    except Exception as exc:
        log.warning(
            "memory_extraction_projection_failed",
            group_id=group_id,
            sources=len(source_payloads),
            error_type=type(exc).__name__,
            error=str(exc),
        )
        return {
            "projected_entities": 0,
            "relationships": 0,
            "errors": [str(exc)],
        }


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def _first_non_empty(*values: object) -> str | None:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return None


def _int_value(value: object) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


__all__ = [
    "MemoryExtractionEnqueueResult",
    "enqueue_memory_extraction_batches",
    "extract_memory_entities",
]
