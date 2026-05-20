"""LLM extraction helpers for turning prose memories into graph handles."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from pydantic_ai import Agent

from sibyl_core.ai.llm import Extractor, LLMSurface
from sibyl_core.models.memory_extraction import (
    MAX_MEMORY_EXTRACTED_ENTITIES,
    MemoryBatchEntityExtractionResult,
    MemoryEntityExtractionResult,
    MemoryExtractionEntityType,
)

MEMORY_ENTITY_EXTRACTION_SYSTEM_PROMPT = """\
Extract durable graph handles and atomic memory claims from Sibyl memory prose.

Support both personal and technical memory. Future retrieval should be able to
find the original source through the entities you return.

Prefer answer-bearing facts and anchors: people and relationships, places,
objects, purchases, preferences, habits, limits, goals, dated events, tools,
languages, domains, reusable patterns, and procedures.

Use claim for concrete facts, preference for likes/dislikes/habits/limits,
person for named people, place for locations, event for dated activities or
milestones, artifact for objects/media/files/systems, and the technical types
when they are the most specific fit.

Keep names short and canonical, but preserve the answer-bearing detail: for
example, "Instagram screen time average", "coffee limit decreased",
"Air Fryer purchase", or "Rachel's birthday". Evidence should be a short source
span. Skip generic assistant advice unless it is tied to a user-specific fact.
When multiple sources are provided, attach each extraction to the exact source_id
from the prompt.
"""

_ENTITY_TYPES = ", ".join(item.value for item in MemoryExtractionEntityType)


def build_memory_entity_extraction_prompt(
    *,
    title: str,
    content: str,
    source_type: str = "memory",
    max_entities: int = 8,
) -> str:
    bounded_max = max(1, min(max_entities, MAX_MEMORY_EXTRACTED_ENTITIES))
    return "\n\n".join(
        (
            f"Source type: {source_type.strip() or 'memory'}",
            f"Title: {title.strip() or 'Untitled memory'}",
            f"Allowed entity types: {_ENTITY_TYPES}",
            f"Extract up to {bounded_max} entities.",
            "Content:",
            content.strip(),
        )
    )


def build_memory_batch_entity_extraction_prompt(
    *,
    sources: Sequence[Mapping[str, str]],
    max_entities_per_source: int = 8,
) -> str:
    bounded_max = max(1, min(max_entities_per_source, MAX_MEMORY_EXTRACTED_ENTITIES))
    source_blocks: list[str] = []
    for index, source in enumerate(sources, start=1):
        source_id = str(source.get("source_id") or "").strip()
        title = str(source.get("title") or "Untitled memory").strip()
        source_type = str(source.get("source_type") or "memory").strip()
        content = str(source.get("content") or "").strip()
        source_blocks.append(
            "\n".join(
                (
                    f"Source {index}",
                    f"source_id: {source_id}",
                    f"source_type: {source_type or 'memory'}",
                    f"title: {title or 'Untitled memory'}",
                    "content:",
                    content,
                )
            )
        )

    return "\n\n".join(
        (
            f"Allowed entity types: {_ENTITY_TYPES}",
            f"Extract up to {bounded_max} entities per source.",
            "Return one sources[] item per source that has durable entities.",
            "Copy each source_id exactly from the input. Skip sources with no durable memory.",
            "Sources:",
            "\n\n---\n\n".join(source_blocks),
        )
    )


def memory_entity_extractor(
    *,
    agent: Agent[Any, Any] | None = None,
    model_override: str | None = None,
    max_tokens: int | None = 2048,
    output_retries: int | None = 2,
) -> Extractor[MemoryEntityExtractionResult]:
    return Extractor(
        MemoryEntityExtractionResult,
        surface=LLMSurface.MEMORY,
        system_prompt=MEMORY_ENTITY_EXTRACTION_SYSTEM_PROMPT,
        model_override=model_override,
        max_tokens=max_tokens,
        output_retries=output_retries,
        agent=agent,
    )


def memory_batch_entity_extractor(
    *,
    agent: Agent[Any, Any] | None = None,
    model_override: str | None = None,
    max_tokens: int | None = 2048,
    output_retries: int | None = 2,
) -> Extractor[MemoryBatchEntityExtractionResult]:
    return Extractor(
        MemoryBatchEntityExtractionResult,
        surface=LLMSurface.MEMORY,
        system_prompt=MEMORY_ENTITY_EXTRACTION_SYSTEM_PROMPT,
        model_override=model_override,
        max_tokens=max_tokens,
        output_retries=output_retries,
        agent=agent,
    )


__all__ = [
    "MEMORY_ENTITY_EXTRACTION_SYSTEM_PROMPT",
    "build_memory_batch_entity_extraction_prompt",
    "build_memory_entity_extraction_prompt",
    "memory_batch_entity_extractor",
    "memory_entity_extractor",
]
