from __future__ import annotations

import pytest
from pydantic import ValidationError

from sibyl_core.ai.llm import LLMSurface
from sibyl_core.ai.operational_distillation import (
    MAX_OPERATIONAL_NOTE_CHARS,
    OPERATIONAL_NOTE_CATEGORY,
    DistilledOperationalNotes,
    build_operational_experience_digest,
    build_operational_note_entities,
    operational_distilled_note_id,
    operational_note_distiller,
)
from sibyl_core.models.entities import EntityType
from sibyl_core.models.experience import (
    OperationalEvidencePart,
    OperationalExperience,
    OperationalObservation,
)


def _experience() -> OperationalExperience:
    return OperationalExperience(
        source_id="capture-1",
        goal="Close change request CR123",
        outcome="success",
        project_id="project-1",
        scope_key="project-1",
        metadata={"capture_surface": "browser-agent"},
        observations=(
            OperationalObservation(
                id="state-0",
                ordinal=0,
                evidence=(
                    OperationalEvidencePart(
                        id="tree-0",
                        content=(
                            "RootWebArea 'Change requests'\n"
                            "heading 'Change request CR123'\n"
                            "cell 'Status Open'\n"
                            "navigation 'Primary'"
                        ),
                        content_type="text/plain; profile=accessibility-tree",
                    ),
                ),
                metadata={
                    "thought": "The request is still open",
                    "url": "https://example.test/changes/CR123",
                },
            ),
            OperationalObservation(
                id="state-1",
                ordinal=1,
                uri="https://example.test/changes/CR123",
                action="select status Closed",
                reasoning="The status now reads Closed",
                evidence=(
                    OperationalEvidencePart(
                        id="tree-1",
                        content="RootWebArea 'Change requests'\ncell 'Status Closed'",
                        content_type="text/plain; profile=accessibility-tree",
                    ),
                ),
            ),
        ),
    )


def test_digest_reads_canonical_and_raw_trajectory_fields() -> None:
    digest = build_operational_experience_digest(_experience())

    assert "URI: https://example.test/changes/CR123" in digest
    assert "Reasoning: The request is still open" in digest
    assert "Action: select status Closed" in digest
    assert "heading: Change request CR123" in digest
    assert "cell: Status Open" in digest
    assert "cell: Status Closed" in digest
    assert "navigation: Primary" not in digest


def test_distilled_notes_reject_empty_output() -> None:
    with pytest.raises(ValidationError, match="contained no notes"):
        DistilledOperationalNotes()


def test_note_entities_are_deterministic_scoped_and_bounded() -> None:
    notes = DistilledOperationalNotes(
        workflow="Open CR123 and set Status to Closed.",
        facts=["The Status field accepts Closed."],
        gotchas=["Saving is required before the list updates."],
    )

    entities = build_operational_note_entities(
        notes,
        experience=_experience(),
        organization_id="org-1",
        created_by="user-1",
        content_hash="content-hash",
        provider="openai",
        model="gpt-5.4-nano",
    )

    assert [entity.id for entity in entities] == [
        operational_distilled_note_id("capture-1", "workflow"),
        operational_distilled_note_id("capture-1", "facts"),
        operational_distilled_note_id("capture-1", "gotchas"),
    ]
    assert all(entity.entity_type is EntityType.NOTE for entity in entities)
    assert all(entity.organization_id == "org-1" for entity in entities)
    assert all(entity.created_by == "user-1" for entity in entities)
    assert all(len(entity.content) <= MAX_OPERATIONAL_NOTE_CHARS for entity in entities)
    assert all(entity.metadata["project_id"] == "project-1" for entity in entities)
    assert all(entity.metadata["category"] == OPERATIONAL_NOTE_CATEGORY for entity in entities)
    assert all(entity.metadata["projection_kind"] == "distilled_note" for entity in entities)
    assert all(entity.metadata["operational_content_hash"] == "content-hash" for entity in entities)
    assert all(entity.metadata["note_distillation_model"] == "gpt-5.4-nano" for entity in entities)


def test_note_distiller_uses_configured_memory_surface() -> None:
    extractor = operational_note_distiller(max_tokens=512)

    assert extractor.surface is LLMSurface.MEMORY
    assert extractor.max_tokens == 512
