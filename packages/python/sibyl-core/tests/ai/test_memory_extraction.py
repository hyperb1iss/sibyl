from __future__ import annotations

import pytest
from pydantic import ValidationError

from sibyl_core.ai.llm import LLMSurface
from sibyl_core.ai.memory_extraction import (
    build_memory_batch_entity_extraction_prompt,
    build_memory_entity_extraction_prompt,
    memory_batch_entity_extractor,
    memory_entity_extractor,
)
from sibyl_core.models.entities import EntityType
from sibyl_core.models.memory_extraction import (
    ExtractedMemoryEntity,
    MemoryBatchEntityExtractionResult,
    MemoryEntityExtractionResult,
    MemoryExtractionEntityType,
    SourceMemoryExtraction,
)


def test_extracted_memory_entity_normalizes_text_and_maps_entity_type() -> None:
    entity = ExtractedMemoryEntity(
        name="  SurrealDB\n3.0  ",
        entity_type="tool",
        summary=" native   graph database ",
        confidence=0.91,
        evidence=" use SurrealDB   3.0 features ",
    )

    assert entity.name == "SurrealDB 3.0"
    assert entity.summary == "native graph database"
    assert entity.evidence == "use SurrealDB 3.0 features"
    assert entity.entity_type is MemoryExtractionEntityType.TOOL
    assert entity.to_entity_type() is EntityType.TOOL


def test_extracted_personal_memory_entity_types_map_to_graph_types() -> None:
    cases = {
        "claim": EntityType.CLAIM,
        "preference": EntityType.PREFERENCE,
        "person": EntityType.PERSON,
        "place": EntityType.PLACE,
        "event": EntityType.EVENT,
        "artifact": EntityType.ARTIFACT,
    }

    for raw_type, entity_type in cases.items():
        entity = ExtractedMemoryEntity(name=f"{raw_type} memory", entity_type=raw_type)

        assert entity.to_entity_type() is entity_type


def test_memory_entity_extraction_rejects_unbounded_entity_types() -> None:
    with pytest.raises(ValidationError):
        ExtractedMemoryEntity(name="Ship task", entity_type="task")


def test_memory_entity_extraction_result_caps_extracted_entities() -> None:
    entities = [
        ExtractedMemoryEntity(name=f"Topic {index}", entity_type="topic") for index in range(13)
    ]

    with pytest.raises(ValidationError):
        MemoryEntityExtractionResult(entities=entities)


def test_memory_batch_entity_extraction_result_caps_source_entities() -> None:
    entities = [
        ExtractedMemoryEntity(name=f"Topic {index}", entity_type="topic") for index in range(13)
    ]

    with pytest.raises(ValidationError):
        MemoryBatchEntityExtractionResult(
            sources=[SourceMemoryExtraction(source_id="session-1", entities=entities)]
        )


def test_memory_entity_extractor_uses_memory_surface() -> None:
    extractor = memory_entity_extractor(max_tokens=512, output_retries=1)

    assert extractor.surface is LLMSurface.MEMORY
    assert extractor.output_type is MemoryEntityExtractionResult
    assert extractor.max_tokens == 512
    assert extractor.output_retries == 1


def test_memory_batch_entity_extractor_uses_memory_surface() -> None:
    extractor = memory_batch_entity_extractor(max_tokens=512, output_retries=1)

    assert extractor.surface is LLMSurface.MEMORY
    assert extractor.output_type is MemoryBatchEntityExtractionResult
    assert extractor.max_tokens == 512
    assert extractor.output_retries == 1


def test_memory_entity_prompt_bounds_requested_entities() -> None:
    prompt = build_memory_entity_extraction_prompt(
        title="LongMemEval trace",
        content="The session discussed SurrealDB RRF and memory projection.",
        source_type="episode",
        max_entities=99,
    )

    assert "Source type: episode" in prompt
    assert "Allowed entity types: topic, claim, preference, person, place, event" in prompt
    assert "Extract up to 12 entities." in prompt


def test_memory_batch_entity_prompt_preserves_source_ids() -> None:
    prompt = build_memory_batch_entity_extraction_prompt(
        sources=[
            {
                "source_id": "session-created",
                "title": "LongMemEval trace",
                "source_type": "session",
                "content": "The user reduced their coffee limit to two cups.",
            }
        ],
        max_entities_per_source=99,
    )

    assert "source_id: session-created" in prompt
    assert "Extract up to 12 entities per source." in prompt
    assert "Copy each source_id exactly from the input." in prompt
    assert "coffee limit" in prompt
