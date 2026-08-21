from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from sibyl_core.ai.llm import LLMSurface
from sibyl_core.ai.operational_distillation import (
    ACCESSIBILITY_INVENTORY_SCHEMA_VERSION,
    MAX_OPERATIONAL_NOTE_CHARS,
    OPERATIONAL_NOTE_CATEGORY,
    RENDER_V1_CONTENT_ROLES,
    DistilledOperationalNotes,
    ObservedOperationalAbsence,
    RenderV1DistilledOperationalNotes,
    admit_observed_operational_absence,
    build_operational_experience_digest,
    build_operational_experience_digest_with_receipt,
    build_operational_note_distillation_prompt,
    build_operational_note_entities,
    build_operational_note_entities_with_receipt,
    operational_distilled_note_id,
    operational_note_distiller,
)
from sibyl_core.models.entities import EntityType
from sibyl_core.models.experience import (
    OperationalEvidencePart,
    OperationalExperience,
    OperationalObservation,
)


def _inventory_metadata(*, part_count: int = 1) -> dict[str, object]:
    return {
        "accessibility_inventory": {
            "schema_version": ACCESSIBILITY_INVENTORY_SCHEMA_VERSION,
            "source": "test-fixture",
            "complete": True,
            "truncated": False,
            "evidence_part_count": part_count,
        }
    }


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
                            "gridcell 'Status Open'\n"
                            "navigation 'Primary'"
                        ),
                        content_type="text/plain; profile=accessibility-tree",
                    ),
                ),
                metadata={
                    "thought": "The request is still open",
                    "url": "https://example.test/changes/CR123",
                    **_inventory_metadata(),
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
                        content=(
                            "RootWebArea 'Change requests'\n"
                            "cell 'Status Closed'\n"
                            "gridcell 'Status Closed'"
                        ),
                        content_type="text/plain; profile=accessibility-tree",
                    ),
                ),
                metadata=_inventory_metadata(),
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
    assert extractor.output_type is DistilledOperationalNotes
    assert set(DistilledOperationalNotes.model_json_schema()["properties"]) == {
        "workflow",
        "facts",
        "gotchas",
    }
    assert operational_note_distiller(profile="render_v1").output_type is (
        RenderV1DistilledOperationalNotes
    )


def test_render_v1_roles_are_selected_from_the_checked_in_full_corpus_census() -> None:
    census_path = (
        Path(__file__).parents[1] / "fixtures" / "operational_distillation_role_census.json"
    )
    census = json.loads(census_path.read_text())
    counts = census["baseline_role_counts"]
    total = sum(counts.values())
    threshold = census["selection"]["minimum_share"]
    selected = tuple(role for role in counts if counts[role] / total >= threshold)

    assert census["corpus"] == {
        "first_trajectory_id": "00332982",
        "last_trajectory_id": "ffcfdab5",
        "trajectory_count": 1870,
        "state_count": 48609,
        "accessibility_tree_count": 48609,
    }
    assert selected == RENDER_V1_CONTENT_ROLES
    assert census["selection"]["coverage"] == pytest.approx(0.999563, abs=0.000001)


def test_render_v1_digest_receipts_cover_roles_lines_chars_and_truncation() -> None:
    digest, receipt = build_operational_experience_digest_with_receipt(
        _experience(),
        profile="render_v1",
    )

    assert "Partial UI inventory for observation 0" in digest
    assert receipt["profile"] == "render_v1"
    assert tuple(receipt["roles"]) == RENDER_V1_CONTENT_ROLES
    assert receipt["candidate_line_count"] == 3
    assert receipt["admitted_line_count"] == 3
    assert receipt["content_chars"] > 0
    assert receipt["digest_chars"] == len(digest)
    assert receipt["configured_budget"] == {
        "digest_chars": 40_000,
        "lines_per_observation": 8,
        "lines_total": 160,
        "line_chars": 140,
    }
    assert receipt["within_digest_char_budget"] is True
    assert receipt["within_line_budget"] is True
    assert receipt["truncated"] is True
    assert receipt["inventories"][0] == {
        "observation_ordinal": 0,
        "accessibility_tree_count": 1,
        "candidate_line_count": 2,
        "admitted_line_count": 2,
        "candidate_role_counts": {"heading": 1, "gridcell": 1},
        "complete": False,
        "truncated": True,
        "rejection_reasons": ["role_filter"],
        "source_named_node_count": 5,
        "excluded_role_count": 3,
        "excluded_name_count": 0,
        "excluded_noise_count": 0,
    }


def test_render_v1_absence_admits_only_exact_complete_nontruncated_inventory() -> None:
    crowded_lines = "\n".join(f"StaticText 'item {index}'" for index in range(9))
    experience = OperationalExperience(
        source_id="capture-absence",
        goal="Inspect controls",
        observations=(
            OperationalObservation(
                id="complete",
                ordinal=2,
                evidence=(
                    OperationalEvidencePart(
                        id="complete-tree",
                        content="heading 'Settings'\nlink 'Advanced settings'",
                        content_type="text/plain; profile=accessibility-tree",
                    ),
                ),
                metadata=_inventory_metadata(),
            ),
            OperationalObservation(
                id="partial",
                ordinal=7,
                evidence=(
                    OperationalEvidencePart(
                        id="partial-tree",
                        content=crowded_lines,
                        content_type="text/plain; profile=accessibility-tree",
                    ),
                ),
                metadata=_inventory_metadata(),
            ),
        ),
    )
    notes = RenderV1DistilledOperationalNotes(
        facts=["Settings were inspected."],
        observed_absence=[
            ObservedOperationalAbsence(
                observation_ordinal=2,
                statement="No Delete account link was present.",
            ),
            ObservedOperationalAbsence(
                observation_ordinal=7,
                statement="No tenth item was present.",
            ),
            ObservedOperationalAbsence(
                observation_ordinal=99,
                statement="No Save button was present.",
            ),
        ],
    )
    _digest, digest_receipt = build_operational_experience_digest_with_receipt(
        experience,
        profile="render_v1",
    )

    admitted, receipt = admit_observed_operational_absence(
        notes,
        digest_receipt=digest_receipt,
        profile="render_v1",
    )

    assert [item.observation_ordinal for item in admitted] == [2]
    assert receipt["proposed_count"] == 3
    assert receipt["admitted_count"] == 1
    assert receipt["rejected_count"] == 2
    assert [item["reason"] for item in receipt["proposals"]] == [
        "complete_inventory",
        "observation_line_budget",
        "observation_not_found",
    ]
    assert [item["inventory_complete"] for item in receipt["proposals"]] == [
        True,
        False,
        False,
    ]


@pytest.mark.parametrize(
    ("observation_metadata", "evidence_metadata", "tree", "expected_reason"),
    (
        ({}, {}, "heading 'Settings'", "source_inventory_receipt_missing"),
        (
            _inventory_metadata() | {"ui_inventory_truncated": True},
            {},
            "heading 'Settings'",
            "source_inventory_truncated",
        ),
        (
            _inventory_metadata(),
            {"ui_inventory_truncated": True},
            "heading 'Settings'",
            "source_inventory_truncated",
        ),
        (
            _inventory_metadata(),
            {},
            "heading 'Settings'\nrowheader 'Delete account'",
            "role_filter",
        ),
    ),
)
def test_render_v1_absence_rejects_untrusted_or_filtered_inventory(
    observation_metadata: dict[str, object],
    evidence_metadata: dict[str, object],
    tree: str,
    expected_reason: str,
) -> None:
    experience = OperationalExperience(
        source_id="capture-hostile-absence",
        goal="Inspect controls",
        observations=(
            OperationalObservation(
                id="hostile",
                ordinal=0,
                evidence=(
                    OperationalEvidencePart(
                        id="hostile-tree",
                        content=tree,
                        content_type="text/plain; profile=accessibility-tree",
                        metadata=evidence_metadata,
                    ),
                ),
                metadata=observation_metadata,
            ),
        ),
    )
    notes = RenderV1DistilledOperationalNotes(
        facts=["Settings were inspected."],
        observed_absence=[
            ObservedOperationalAbsence(
                observation_ordinal=0,
                statement="No Delete account control was present.",
            )
        ],
    )
    _digest, digest_receipt = build_operational_experience_digest_with_receipt(
        experience,
        profile="render_v1",
    )

    admitted, receipt = admit_observed_operational_absence(
        notes,
        digest_receipt=digest_receipt,
        profile="render_v1",
    )

    assert admitted == []
    assert receipt["admitted_count"] == 0
    assert receipt["proposals"][0]["reason"] == expected_reason


def test_render_v1_projects_only_admitted_absence_with_render_receipts() -> None:
    notes = RenderV1DistilledOperationalNotes(
        facts=["The Status field accepts Closed."],
        observed_absence=[
            ObservedOperationalAbsence(
                observation_ordinal=0,
                statement="No Delete button was present.",
            )
        ],
    )

    entities, receipt = build_operational_note_entities_with_receipt(
        notes,
        experience=_experience(),
        organization_id="org-1",
        created_by="user-1",
        content_hash="content-hash",
        profile="render_v1",
        admitted_observed_absence=notes.observed_absence,
    )

    assert [entity.metadata["note_kind"] for entity in entities] == [
        "facts",
        "observed_absence",
    ]
    assert entities[1].metadata["operational_note_distillation_profile"] == "render_v1"
    assert "Observation 0: No Delete button was present." in entities[1].content
    assert receipt["note_count"] == 2
    assert receipt["lines"] > 0
    assert receipt["chars"] == sum(len(entity.content) for entity in entities)
    assert receipt["truncated"] is False
    assert receipt["max_note_chars"] == MAX_OPERATIONAL_NOTE_CHARS
    assert receipt["within_note_char_budget"] is True


def test_baseline_prompt_and_digest_omit_treatment_surface() -> None:
    digest = build_operational_experience_digest(_experience())
    prompt = build_operational_note_distillation_prompt(digest)

    assert "Complete UI inventory" not in digest
    assert "observed_absence" not in prompt
    assert (
        "operational_note_distillation_profile"
        not in build_operational_note_entities(
            DistilledOperationalNotes(facts=["fact"]),
            experience=_experience(),
            organization_id="org-1",
            created_by="user-1",
            content_hash="content-hash",
        )[0].metadata
    )
