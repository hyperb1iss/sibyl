from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from sibyl_core.models import EntityType, RelationshipType
from sibyl_core.models.experience import (
    MAX_OPERATIONAL_AUXILIARY_JSON_CHARS,
    MAX_OPERATIONAL_EVIDENCE_PART_CHARS,
    MAX_OPERATIONAL_FIELD_CHARS,
    OperationalEvidencePart,
    OperationalExperience,
    OperationalObservation,
)
from sibyl_core.projection import (
    MANIFEST_STATE_COMPLETE,
    operational_experience_manifest_with_state,
    persist_operational_experience,
    project_operational_experience,
)
from sibyl_core.projection.experience import (
    MANIFEST_STATE_EMBEDDING_PENDING,
    MANIFEST_STATE_PENDING,
)


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
                evidence=(
                    OperationalEvidencePart(
                        id="tree-0",
                        content="Heading: Incidents\nRow: INC001 Open",
                    ),
                ),
                image_refs=("screens/0.png",),
            ),
            OperationalObservation(
                id="state-1",
                ordinal=1,
                uri="https://example.test/incidents/INC001",
                action="click('INC001')",
                reasoning="The incident form is open.",
                evidence=(
                    OperationalEvidencePart(
                        id="tree-0",
                        content="Incident INC001\nState: Open",
                    ),
                    OperationalEvidencePart(
                        id="tree-1",
                        content="Activity: incident opened",
                    ),
                ),
                image_refs=("screens/1.png",),
            ),
            OperationalObservation(
                id="state-2",
                ordinal=2,
                uri="https://example.test/incidents/INC001",
                action="select('state', 'Closed')",
                reasoning="The state field now reads Closed.",
                evidence=(
                    OperationalEvidencePart(
                        id="tree-0",
                        content="Incident INC001\nState: Closed",
                    ),
                ),
                image_refs=("screens/2.png",),
            ),
        ),
        metadata={"environment": "browser"},
    )


def test_projection_preserves_raw_evidence_and_action_boundaries() -> None:
    projection = project_operational_experience(_experience())
    raw = [entity for entity in projection.entities if entity.entity_type is EntityType.SESSION]
    events = [entity for entity in projection.entities if entity.entity_type is EntityType.EVENT]

    assert len(raw) == 4
    assert "Heading: Incidents\nRow: INC001 Open" in raw[0].content
    assert "Initial observation before any recorded action." in raw[0].content
    assert "Action producing this observation: click('INC001')" in raw[1].content
    assert "Evidence part: 1/2" in raw[1].content
    assert "Evidence part: 2/2" in raw[2].content
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
    assert derived_targets == {raw[0].id, raw[1].id, raw[2].id}


def test_projection_preserves_evidence_bytes_including_surrounding_whitespace() -> None:
    experience = _experience()
    evidence = (
        experience.observations[0]
        .evidence[0]
        .model_copy(update={"content": "  exact source bytes\n\n"})
    )
    observation = experience.observations[0].model_copy(update={"evidence": (evidence,)})
    projection = project_operational_experience(
        experience.model_copy(update={"observations": (observation, *experience.observations[1:])})
    )
    raw = next(
        entity
        for entity in projection.entities
        if entity.metadata.get("source_observation_id") == observation.id
    )

    assert raw.content.endswith("Evidence:\n  exact source bytes\n\n")


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


def test_projection_provenance_cannot_be_overridden_by_caller_metadata() -> None:
    experience = _experience().model_copy(
        update={
            "metadata": {
                "category": "spoofed",
                "operational_source_id": "spoofed",
                "operational_schema_version": -1,
                "operational_content_hash": "spoofed",
            }
        }
    )

    projection = project_operational_experience(experience)

    assert all(
        entity.metadata["category"] == "operational_experience" for entity in projection.entities
    )
    assert all(
        entity.metadata["operational_source_id"] == experience.source_id
        for entity in projection.entities
    )
    assert all(
        entity.metadata["operational_schema_version"] == projection.manifest.schema_version
        for entity in projection.entities
    )
    assert all(
        entity.metadata["operational_content_hash"] == projection.manifest.content_hash
        for entity in projection.entities
    )


def test_operational_evidence_rejects_unbounded_content() -> None:
    with pytest.raises(ValidationError, match="String should have at most"):
        OperationalEvidencePart(
            id="oversized",
            content="x" * (MAX_OPERATIONAL_EVIDENCE_PART_CHARS + 1),
        )


def test_operational_experience_rejects_unbounded_fields() -> None:
    with pytest.raises(ValidationError, match="String should have at most"):
        OperationalExperience.model_validate(
            {
                **_experience().model_dump(mode="json"),
                "goal": "x" * (MAX_OPERATIONAL_FIELD_CHARS + 1),
            }
        )


def test_projection_rejects_typed_content_overflow() -> None:
    experience = _experience()
    observations = list(experience.observations)
    observations[1] = observations[1].model_copy(update={"action": "x" * 10_000})

    with pytest.raises(ValueError, match="typed entity content limit"):
        project_operational_experience(
            experience.model_copy(
                update={
                    "goal": "g" * 10_000,
                    "observations": tuple(observations),
                }
            )
        )


def test_operational_experience_rejects_unbounded_metadata() -> None:
    payload = _experience().model_dump(mode="json")
    payload["metadata"] = {"oversized": "x" * MAX_OPERATIONAL_AUXILIARY_JSON_CHARS}

    with pytest.raises(ValidationError, match="auxiliary payload limit"):
        OperationalExperience.model_validate(payload)


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


@pytest.mark.asyncio
async def test_persistence_replays_writes_without_duplicate_ids() -> None:
    experience = _experience()
    entity_manager = SimpleNamespace(
        get=AsyncMock(side_effect=KeyError("missing")),
        create_direct_bulk=AsyncMock(
            side_effect=lambda entities, **_: [entity.id for entity in entities]
        ),
        delete=AsyncMock(return_value=True),
    )
    relationship_manager = SimpleNamespace(
        create_direct_bulk=AsyncMock(
            side_effect=lambda relationships, **_: [item.id for item in relationships]
        ),
        delete_bulk=AsyncMock(return_value=0),
    )

    result = await persist_operational_experience(
        entity_manager=entity_manager,
        relationship_manager=relationship_manager,
        experience=experience,
        organization_id="org-1",
        created_by="user-1",
    )

    assert result.written_entity_ids == result.projection.manifest.entity_ids
    assert result.written_relationship_ids == result.projection.manifest.relationship_ids
    assert result.deleted_entity_ids == ()
    assert result.deleted_relationship_ids == ()
    assert entity_manager.create_direct_bulk.await_count == 3
    pending_write = entity_manager.create_direct_bulk.await_args_list[0].args[0]
    assert pending_write[0].metadata["operational_projection_state"] == MANIFEST_STATE_PENDING
    written = entity_manager.create_direct_bulk.await_args_list[1].args[0]
    assert all(entity.organization_id == "org-1" for entity in written)
    assert all(entity.created_by == "user-1" for entity in written)
    manifest_write = entity_manager.create_direct_bulk.await_args_list[2].args[0]
    assert [entity.id for entity in manifest_write] == [
        result.projection.manifest.manifest_entity_id
    ]
    assert manifest_write[0].metadata["operational_projection_state"] == MANIFEST_STATE_COMPLETE
    entity_manager.delete.assert_not_awaited()
    relationship_manager.delete_bulk.assert_not_awaited()


@pytest.mark.asyncio
async def test_persistence_resolves_manifest_relationship_endpoints_before_commit() -> None:
    written_entities: set[str] = set()

    async def create_entities(entities: object, **_kwargs: object) -> list[str]:
        entity_ids = [entity.id for entity in entities]
        written_entities.update(entity_ids)
        return entity_ids

    async def create_relationships(relationships: object, **_kwargs: object) -> list[str]:
        return [
            relationship.id
            for relationship in relationships
            if relationship.source_id in written_entities
            and relationship.target_id in written_entities
        ]

    result = await persist_operational_experience(
        entity_manager=SimpleNamespace(
            get=AsyncMock(side_effect=KeyError("missing")),
            create_direct_bulk=create_entities,
            delete=AsyncMock(return_value=True),
        ),
        relationship_manager=SimpleNamespace(
            create_direct_bulk=create_relationships,
            delete_bulk=AsyncMock(return_value=0),
        ),
        experience=_experience(),
        organization_id="org-1",
    )

    assert result.written_relationship_ids == result.projection.manifest.relationship_ids


@pytest.mark.asyncio
async def test_persistence_refuses_partial_relationship_writes() -> None:
    entity_manager = SimpleNamespace(
        get=AsyncMock(side_effect=KeyError("missing")),
        create_direct_bulk=AsyncMock(
            side_effect=lambda entities, **_: [entity.id for entity in entities]
        ),
        delete=AsyncMock(return_value=True),
    )
    relationship_manager = SimpleNamespace(
        create_direct_bulk=AsyncMock(
            side_effect=lambda relationships, **_: [
                relationship.id
                for relationship in relationships
                if relationship.relationship_type is not RelationshipType.PART_OF
            ]
        ),
        delete_bulk=AsyncMock(return_value=0),
    )

    with pytest.raises(RuntimeError, match="failed to persist relationships"):
        await persist_operational_experience(
            entity_manager=entity_manager,
            relationship_manager=relationship_manager,
            experience=_experience(),
            organization_id="org-1",
        )

    assert entity_manager.create_direct_bulk.await_count == 2
    pending_write = entity_manager.create_direct_bulk.await_args_list[0].args[0]
    assert pending_write[0].metadata["operational_projection_state"] == MANIFEST_STATE_PENDING


@pytest.mark.asyncio
async def test_deferred_persistence_leaves_manifest_uncommitted() -> None:
    entity_manager = SimpleNamespace(
        get=AsyncMock(side_effect=KeyError("missing")),
        create_direct_bulk=AsyncMock(
            side_effect=lambda entities, **_: [entity.id for entity in entities]
        ),
        delete=AsyncMock(return_value=True),
    )
    relationship_manager = SimpleNamespace(
        create_direct_bulk=AsyncMock(
            side_effect=lambda relationships, **_: [item.id for item in relationships]
        ),
        delete_bulk=AsyncMock(return_value=0),
    )

    await persist_operational_experience(
        entity_manager=entity_manager,
        relationship_manager=relationship_manager,
        experience=_experience(),
        organization_id="org-1",
        commit_manifest=False,
    )

    manifest_write = entity_manager.create_direct_bulk.await_args_list[-1].args[0]
    assert (
        manifest_write[0].metadata["operational_projection_state"]
        == MANIFEST_STATE_EMBEDDING_PENDING
    )


@pytest.mark.asyncio
async def test_persistence_skips_unchanged_committed_manifest() -> None:
    experience = _experience()
    projection = project_operational_experience(experience)
    committed_manifest = next(
        entity
        for entity in projection.entities
        if entity.id == projection.manifest.manifest_entity_id
    )
    entity_manager = SimpleNamespace(
        get=AsyncMock(return_value=committed_manifest),
        create_direct_bulk=AsyncMock(),
        delete=AsyncMock(),
    )
    relationship_manager = SimpleNamespace(
        create_direct_bulk=AsyncMock(),
        delete_bulk=AsyncMock(),
    )

    result = await persist_operational_experience(
        entity_manager=entity_manager,
        relationship_manager=relationship_manager,
        experience=experience,
        organization_id="org-1",
    )

    assert result.written_entity_ids == ()
    assert result.written_relationship_ids == ()
    entity_manager.create_direct_bulk.assert_not_awaited()
    relationship_manager.create_direct_bulk.assert_not_awaited()
    entity_manager.delete.assert_not_awaited()
    relationship_manager.delete_bulk.assert_not_awaited()


@pytest.mark.asyncio
async def test_persistence_reuses_embedding_pending_projection_without_rewriting() -> None:
    experience = _experience()
    projection = project_operational_experience(experience)
    pending_manifest = operational_experience_manifest_with_state(
        projection,
        MANIFEST_STATE_EMBEDDING_PENDING,
    )
    entity_manager = SimpleNamespace(
        get=AsyncMock(return_value=pending_manifest),
        create_direct_bulk=AsyncMock(
            side_effect=lambda entities, **_: [entity.id for entity in entities]
        ),
        delete=AsyncMock(return_value=True),
    )
    relationship_manager = SimpleNamespace(
        create_direct_bulk=AsyncMock(
            side_effect=lambda relationships, **_: [item.id for item in relationships]
        ),
        delete_bulk=AsyncMock(return_value=0),
    )

    result = await persist_operational_experience(
        entity_manager=entity_manager,
        relationship_manager=relationship_manager,
        experience=experience,
        organization_id="org-1",
    )

    assert result.written_entity_ids == ()
    assert result.written_relationship_ids == ()
    assert result.embedding_backfill_required is True
    entity_manager.create_direct_bulk.assert_not_awaited()
    relationship_manager.create_direct_bulk.assert_not_awaited()


@pytest.mark.asyncio
async def test_pending_manifest_tracks_old_and_planned_inventory_before_entity_writes() -> None:
    experience = _experience()
    projection = project_operational_experience(experience)
    old_manifest = next(
        entity
        for entity in projection.entities
        if entity.id == projection.manifest.manifest_entity_id
    ).model_copy(
        update={
            "metadata": {
                "expected_entity_ids": ["session_old"],
                "expected_relationship_ids": ["rel_old"],
            }
        }
    )
    entity_manager = SimpleNamespace(
        get=AsyncMock(return_value=old_manifest),
        create_direct_bulk=AsyncMock(
            side_effect=lambda entities, **_: [entity.id for entity in entities]
        ),
        delete=AsyncMock(return_value=True),
    )
    relationship_manager = SimpleNamespace(
        create_direct_bulk=AsyncMock(
            side_effect=lambda relationships, **_: [item.id for item in relationships]
        ),
        delete_bulk=AsyncMock(return_value=1),
    )

    await persist_operational_experience(
        entity_manager=entity_manager,
        relationship_manager=relationship_manager,
        experience=experience,
        organization_id="org-1",
    )

    pending = entity_manager.create_direct_bulk.await_args_list[0].args[0][0]
    assert set(pending.metadata["expected_entity_ids"]) == {
        *projection.manifest.entity_ids,
        "session_old",
    }
    assert set(pending.metadata["expected_relationship_ids"]) == {
        *projection.manifest.relationship_ids,
        "rel_old",
    }


@pytest.mark.asyncio
async def test_persistence_deletes_only_stale_manifest_owned_records() -> None:
    experience = _experience()
    projection = project_operational_experience(experience)
    old_manifest = next(
        entity
        for entity in projection.entities
        if entity.id == projection.manifest.manifest_entity_id
    ).model_copy(
        update={
            "metadata": {
                "expected_entity_ids": [*projection.manifest.entity_ids, "session_stale"],
                "expected_relationship_ids": [
                    *projection.manifest.relationship_ids,
                    "rel_stale",
                ],
            }
        }
    )
    entity_manager = SimpleNamespace(
        get=AsyncMock(return_value=old_manifest),
        create_direct_bulk=AsyncMock(
            side_effect=lambda entities, **_: [entity.id for entity in entities]
        ),
        delete=AsyncMock(return_value=True),
    )
    relationship_manager = SimpleNamespace(
        create_direct_bulk=AsyncMock(
            side_effect=lambda relationships, **_: [item.id for item in relationships]
        ),
        delete_bulk=AsyncMock(return_value=1),
    )

    result = await persist_operational_experience(
        entity_manager=entity_manager,
        relationship_manager=relationship_manager,
        experience=experience,
        organization_id="org-1",
    )

    assert result.deleted_entity_ids == ("session_stale",)
    assert result.deleted_relationship_ids == ("rel_stale",)
    relationship_manager.delete_bulk.assert_awaited_once_with(("rel_stale",))
    entity_manager.delete.assert_awaited_once_with("session_stale")
    manifest_write = entity_manager.create_direct_bulk.await_args_list[-1].args[0]
    assert [entity.id for entity in manifest_write] == [projection.manifest.manifest_entity_id]
