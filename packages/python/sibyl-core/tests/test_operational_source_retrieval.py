from unittest.mock import AsyncMock

import pytest

from sibyl_core.auth.memory_policy import memory_scope_policy_key
from sibyl_core.models.entities import Entity, EntityType
from sibyl_core.projection import operational_experience_manifest_id
from sibyl_core.retrieval.operational_sources import (
    OperationalSourceInventory,
    fetch_operational_source_inventory,
    operational_observation_signal_text,
    select_operational_source_span,
)


def _entity(
    entity_id: str,
    *,
    source_id: str = "capture-1",
    project_id: str = "project-1",
    scope_key: str | None = None,
    memory_scope: str | None = None,
    principal_id: str | None = None,
    ordinal: int,
    part_index: int = 0,
    evidence: str,
    uri: str | None = None,
) -> Entity:
    uri_line = f"URI: {uri}\n" if uri else ""
    return Entity(
        id=entity_id,
        entity_type=EntityType.SESSION,
        name=f"Observation {ordinal}",
        content=(
            "Goal: update an order\n"
            "Reported outcome: success\n"
            f"Observation: {ordinal}\n"
            f"Action producing this observation: action-{ordinal}\n"
            f"Reasoning: reasoning-{ordinal}\n"
            f"{uri_line}"
            "Evidence:\n"
            f"{evidence}"
        ),
        metadata={
            "operational_source_id": source_id,
            "project_id": project_id,
            "scope_key": scope_key,
            "memory_scope": memory_scope,
            "principal_id": principal_id,
            "projection_kind": "raw_observation",
            "observation_ordinal": ordinal,
            "evidence_part_index": part_index,
        },
    )


def _manifest(
    entity_ids: list[str],
    *,
    source_id: str = "capture-1",
    project_id: str = "project-1",
    scope_key: str | None = None,
    memory_scope: str | None = None,
    principal_id: str | None = None,
    state: str = "complete",
) -> Entity:
    manifest_id = operational_experience_manifest_id(source_id)
    return Entity(
        id=manifest_id,
        entity_type=EntityType.ARTIFACT,
        name="Manifest",
        metadata={
            "operational_source_id": source_id,
            "project_id": project_id,
            "scope_key": scope_key,
            "memory_scope": memory_scope,
            "principal_id": principal_id,
            "projection_kind": "manifest",
            "operational_projection_state": state,
            "expected_entity_ids": [*entity_ids, manifest_id],
        },
    )


@pytest.mark.asyncio
async def test_fetch_inventory_uses_manifest_and_orders_raw_observations() -> None:
    later_part = _entity("later-part", ordinal=4, part_index=1, evidence="Ship control")
    earlier = _entity("earlier", ordinal=2, evidence="View control")
    later = _entity("later", ordinal=4, evidence="Tracking control")
    manifest = _manifest([later_part.id, earlier.id, later.id])
    reader = AsyncMock()
    reader.get.return_value = manifest
    reader.get_many.return_value = [manifest, later, later_part, earlier]

    inventory = await fetch_operational_source_inventory(
        reader,
        "capture-1",
        allowed_project_ids={"project-1"},
        allowed_memory_scope_keys={memory_scope_policy_key("private", "user-123")},
        principal_id="user-123",
    )

    assert inventory.status == "complete"
    assert [entity.id for entity in inventory.raw_observations] == [
        "earlier",
        "later",
        "later-part",
    ]
    reader.get.assert_awaited_once_with(operational_experience_manifest_id("capture-1"))
    reader.get_many.assert_awaited_once_with(manifest.metadata["expected_entity_ids"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state", "allowed_projects", "allowed_scopes", "expected_status"),
    [
        ("embedding_pending", {"project-1"}, None, "manifest_not_complete"),
        ("complete", {"project-2"}, None, "project_denied"),
        ("complete", {"project-1"}, {"scope-2"}, "scope_denied"),
    ],
)
async def test_fetch_inventory_rejects_unavailable_or_unauthorized_manifests(
    state: str,
    allowed_projects: set[str],
    allowed_scopes: set[str] | None,
    expected_status: str,
) -> None:
    observation = _entity(
        "observation",
        ordinal=1,
        evidence="View control",
        scope_key="scope-1",
        memory_scope="project",
    )
    manifest = _manifest(
        [observation.id],
        state=state,
        scope_key="scope-1",
        memory_scope="project",
    )
    reader = AsyncMock()
    reader.get.return_value = manifest

    inventory = await fetch_operational_source_inventory(
        reader,
        "capture-1",
        allowed_project_ids=allowed_projects,
        allowed_memory_scope_keys={
            memory_scope_policy_key("project", scope_key) for scope_key in allowed_scopes
        }
        if allowed_scopes is not None
        else None,
    )

    assert inventory.status == expected_status
    reader.get_many.assert_not_awaited()


@pytest.mark.asyncio
async def test_fetch_inventory_accepts_canonical_private_scope_grant() -> None:
    observation = _entity(
        "observation",
        ordinal=1,
        evidence="View control",
        scope_key="user-123",
        memory_scope="private",
    )
    manifest = _manifest(
        [observation.id],
        scope_key="user-123",
        memory_scope="private",
    )
    reader = AsyncMock()
    reader.get.return_value = manifest
    reader.get_many.return_value = [manifest, observation]

    inventory = await fetch_operational_source_inventory(
        reader,
        "capture-1",
        allowed_memory_scope_keys={memory_scope_policy_key("private", "user-123")},
        principal_id="user-123",
    )

    assert inventory.status == "complete"


@pytest.mark.asyncio
async def test_fetch_inventory_denies_private_source_owned_by_another_session() -> None:
    observation = _entity(
        "observation",
        ordinal=1,
        evidence="Private order history",
        memory_scope="private",
        principal_id="user-owner",
    )
    manifest = _manifest(
        [observation.id],
        memory_scope="private",
        principal_id="user-owner",
    )
    reader = AsyncMock()
    reader.get.return_value = manifest

    inventory = await fetch_operational_source_inventory(
        reader,
        "capture-1",
        allowed_project_ids={"project-1"},
        principal_id="user-requester",
    )

    assert inventory.status == "scope_denied"
    reader.get_many.assert_not_awaited()


@pytest.mark.asyncio
async def test_fetch_inventory_accepts_private_source_owned_by_current_session() -> None:
    observation = _entity(
        "observation",
        ordinal=1,
        evidence="Private order history",
        memory_scope="private",
        principal_id="user-owner",
    )
    manifest = _manifest(
        [observation.id],
        memory_scope="private",
        principal_id="user-owner",
    )
    reader = AsyncMock()
    reader.get.return_value = manifest
    reader.get_many.return_value = [manifest, observation]

    inventory = await fetch_operational_source_inventory(
        reader,
        "capture-1",
        allowed_project_ids={"project-1"},
        principal_id="user-owner",
    )

    assert inventory.status == "complete"


@pytest.mark.asyncio
@pytest.mark.parametrize("use_api_key_grant", [False, True])
async def test_fetch_inventory_rejects_conflicting_private_owner_fields(
    use_api_key_grant: bool,
) -> None:
    observation = _entity(
        "observation",
        ordinal=1,
        evidence="Private order history",
        memory_scope="private",
        scope_key="user-requester",
        principal_id="user-owner",
    )
    manifest = _manifest(
        [observation.id],
        memory_scope="private",
        scope_key="user-requester",
        principal_id="user-owner",
    )
    reader = AsyncMock()
    reader.get.return_value = manifest

    inventory = await fetch_operational_source_inventory(
        reader,
        "capture-1",
        allowed_project_ids={"project-1"},
        allowed_memory_scope_keys={memory_scope_policy_key("private", "user-requester")}
        if use_api_key_grant
        else None,
        principal_id="user-requester",
    )

    assert inventory.status == "scope_denied"
    reader.get_many.assert_not_awaited()


@pytest.mark.asyncio
async def test_fetch_inventory_rejects_private_source_without_owner() -> None:
    manifest = _manifest([], memory_scope="private")
    reader = AsyncMock()
    reader.get.return_value = manifest

    inventory = await fetch_operational_source_inventory(
        reader,
        "capture-1",
        allowed_project_ids={"project-1"},
        principal_id="user-requester",
    )

    assert inventory.status == "scope_denied"
    reader.get_many.assert_not_awaited()


@pytest.mark.asyncio
async def test_fetch_inventory_rejects_missing_or_foreign_members() -> None:
    observation = _entity("observation", ordinal=1, evidence="View control")
    manifest = _manifest([observation.id])
    reader = AsyncMock()
    reader.get.return_value = manifest
    reader.get_many.return_value = [manifest]

    incomplete = await fetch_operational_source_inventory(reader, "capture-1")

    assert incomplete.status == "inventory_incomplete"

    foreign = observation.model_copy(
        update={"metadata": {**observation.metadata, "operational_source_id": "capture-2"}}
    )
    reader.get_many.return_value = [manifest, foreign]

    invalid = await fetch_operational_source_inventory(reader, "capture-1")

    assert invalid.status == "inventory_invalid"


@pytest.mark.asyncio
async def test_fetch_inventory_revalidates_manifest_after_inventory_read() -> None:
    observation = _entity("observation", ordinal=1, evidence="View control")
    manifest = _manifest([observation.id])
    pending_manifest = manifest.model_copy(
        update={
            "metadata": {
                **manifest.metadata,
                "operational_projection_state": "embedding_pending",
            }
        }
    )
    reader = AsyncMock()
    reader.get.return_value = manifest
    reader.get_many.return_value = [pending_manifest, observation]

    inventory = await fetch_operational_source_inventory(reader, "capture-1")

    assert inventory.status == "manifest_not_complete"


@pytest.mark.asyncio
async def test_fetch_inventory_reports_missing_manifest_from_optional_reader() -> None:
    reader = AsyncMock()
    reader.get.return_value = None

    inventory = await fetch_operational_source_inventory(reader, "capture-1")

    assert inventory.status == "manifest_missing"
    reader.get_many.assert_not_awaited()


def test_signal_text_removes_repeated_source_headers() -> None:
    entity = _entity(
        "observation",
        ordinal=3,
        evidence="button 'Add Tracking Number'",
        uri="https://example.test/orders/3",
    )

    signal = operational_observation_signal_text(entity)

    assert "Goal:" not in signal
    assert "Reported outcome:" not in signal
    assert "action-3" in signal
    assert "reasoning-3" in signal
    assert "https://example.test/orders/3" in signal
    assert "Add Tracking Number" in signal


def test_span_selection_ranks_a_contiguous_window_and_preserves_order() -> None:
    observations = (
        _entity("one", ordinal=1, evidence="Dashboard and catalog"),
        _entity("two", ordinal=2, evidence="Open unrelated profile"),
        _entity("three", ordinal=3, evidence="View order details"),
        _entity("four", ordinal=4, evidence="Ship the order"),
        _entity("five", ordinal=5, evidence="Add Tracking Number"),
        _entity("six", ordinal=6, evidence="Save carrier tracking"),
    )
    inventory = OperationalSourceInventory(
        source_id="capture-1",
        manifest_id=operational_experience_manifest_id("capture-1"),
        status="complete",
        raw_observations=observations,
    )

    span = select_operational_source_span(
        "Which controls are used to view ship and add tracking?",
        inventory,
        max_observations=4,
        max_entities=4,
    )

    assert [entity.id for entity in span.entities] == ["three", "four", "five", "six"]
    assert span.observation_ordinals == (3, 4, 5, 6)
    assert span.candidate_window_count == 3
    assert span.ranking_applied is True


def test_span_selection_ignores_source_wide_lines_repeated_inside_evidence() -> None:
    repeated_header = "Goal: find the warranty expiration for Kelly's laptop"
    observations = tuple(
        _entity(
            f"observation-{ordinal}",
            ordinal=ordinal,
            evidence=(
                f"{repeated_header}\n"
                + (
                    "Kelly asset warranty expiration appears in this result"
                    if ordinal == 7
                    else f"navigation state {ordinal}"
                )
            ),
        )
        for ordinal in range(8)
    )
    inventory = OperationalSourceInventory(
        source_id="capture-1",
        manifest_id=operational_experience_manifest_id("capture-1"),
        status="complete",
        raw_observations=observations,
    )

    span = select_operational_source_span(
        "What was Kelly's laptop warranty expiration?",
        inventory,
        max_observations=4,
        max_entities=4,
    )

    assert span.observation_ordinals == (4, 5, 6, 7)
    assert span.ranking_applied is True


def test_span_selection_keeps_one_relevant_part_per_observation_when_bounded() -> None:
    observations = (
        _entity("one-a", ordinal=1, evidence="Unrelated overview"),
        _entity("one-b", ordinal=1, part_index=1, evidence="View order details"),
        _entity("two-a", ordinal=2, evidence="Unrelated sidebar"),
        _entity("two-b", ordinal=2, part_index=1, evidence="Ship order control"),
    )
    inventory = OperationalSourceInventory(
        source_id="capture-1",
        manifest_id=operational_experience_manifest_id("capture-1"),
        status="complete",
        raw_observations=observations,
    )

    span = select_operational_source_span(
        "view ship order controls",
        inventory,
        max_observations=2,
        max_entities=2,
    )

    assert [entity.id for entity in span.entities] == ["one-b", "two-b"]
    assert span.observation_ordinals == (1, 2)
