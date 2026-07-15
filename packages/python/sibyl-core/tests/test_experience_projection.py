from __future__ import annotations

import json

import pytest

from sibyl_core.models import EntityType, RelationshipType
from sibyl_core.models.experience import OperationalExperience, OperationalObservation
from sibyl_core.projection import project_operational_experience


def _experience(*, outcome: str = "success") -> OperationalExperience:
    return OperationalExperience(
        source_id="capture-123",
        goal="Update the incident and close the work item",
        outcome=outcome,
        start_uri="https://example.test/incidents",
        project_id="project-1",
        observations=(
            OperationalObservation(
                id="state-0",
                ordinal=0,
                uri="https://example.test/incidents",
                reasoning="The incident list is visible.",
                evidence="Heading: Incidents\nRow: INC001 Open",
                image_refs=("screens/0.png",),
            ),
            OperationalObservation(
                id="state-1",
                ordinal=1,
                uri="https://example.test/incidents/INC001",
                action="click('INC001')",
                reasoning="The incident form is open.",
                evidence="Incident INC001\nState: Open",
                image_refs=("screens/1.png",),
            ),
            OperationalObservation(
                id="state-2",
                ordinal=2,
                uri="https://example.test/incidents/INC001",
                action="select('state', 'Closed')",
                reasoning="The state field now reads Closed.",
                evidence="Incident INC001\nState: Closed",
                image_refs=("screens/2.png",),
            ),
        ),
        metadata={"environment": "browser"},
    )


def test_projection_preserves_raw_evidence_and_action_boundaries() -> None:
    projection = project_operational_experience(_experience())
    raw = [entity for entity in projection.entities if entity.entity_type is EntityType.SESSION]
    events = [entity for entity in projection.entities if entity.entity_type is EntityType.EVENT]

    assert len(raw) == 3
    assert "Heading: Incidents\nRow: INC001 Open" in raw[0].content
    assert "Initial observation before any recorded action." in raw[0].content
    assert "Action producing this observation: click('INC001')" in raw[1].content
    assert raw[1].metadata["image_refs"] == ["screens/1.png"]

    assert len(events) == 2
    click_event = events[0]
    assert "Action: click('INC001')" in click_event.content
    assert click_event.metadata["source_observation_ids"] == ["state-0", "state-1"]
    derived_targets = {
        relationship.target_id
        for relationship in projection.relationships
        if relationship.source_id == click_event.id
        and relationship.relationship_type is RelationshipType.DERIVED_FROM
    }
    assert derived_targets == {raw[0].id, raw[1].id}


def test_projection_is_deterministic_and_manifest_is_self_describing() -> None:
    first = project_operational_experience(_experience())
    second = project_operational_experience(_experience())

    assert [entity.id for entity in first.entities] == [entity.id for entity in second.entities]
    assert [relationship.id for relationship in first.relationships] == [
        relationship.id for relationship in second.relationships
    ]
    assert first.manifest == second.manifest

    manifest_entity = next(
        entity for entity in first.entities if entity.id == first.manifest.manifest_entity_id
    )
    payload = json.loads(manifest_entity.content)
    assert payload["content_hash"] == first.manifest.content_hash
    assert set(payload["entity_ids"]) == {entity.id for entity in first.entities}
    assert set(payload["relationship_ids"]) == {
        relationship.id for relationship in first.relationships
    }


def test_failure_projection_reports_failure_without_inventing_cause_or_resolution() -> None:
    projection = project_operational_experience(_experience(outcome="failure"))
    errors = [
        entity for entity in projection.entities if entity.entity_type is EntityType.ERROR_PATTERN
    ]

    assert len(errors) == 1
    assert "Reported outcome: failure" in errors[0].content
    assert errors[0].metadata["resolution_status"] == "unknown"
    assert not any(entity.entity_type is EntityType.CLAIM for entity in projection.entities)
    assert not any(
        relationship.relationship_type is RelationshipType.CONTRADICTS
        for relationship in projection.relationships
    )


def test_success_projection_does_not_create_error_pattern() -> None:
    projection = project_operational_experience(_experience(outcome="success"))

    assert not any(entity.entity_type is EntityType.ERROR_PATTERN for entity in projection.entities)


@pytest.mark.parametrize(
    ("field", "duplicate"),
    [("id", "state-0"), ("ordinal", 0)],
)
def test_projection_rejects_ambiguous_observation_identity(
    field: str,
    duplicate: str | int,
) -> None:
    experience = _experience()
    observations = list(experience.observations)
    observations[1] = observations[1].model_copy(update={field: duplicate})

    with pytest.raises(ValueError, match="must be unique"):
        project_operational_experience(experience.model_copy(update={"observations": observations}))
