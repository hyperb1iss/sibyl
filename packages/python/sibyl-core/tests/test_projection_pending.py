import pytest

from sibyl_core.memory_pipeline.lifecycle import graph_metadata_recallable
from sibyl_core.models.entities import Entity, EntityType
from sibyl_core.projection import reconcile
from tests.test_reflection_identity import runtime as runtime


@pytest.mark.parametrize(
    "pending_key", [reconcile.RECONCILE_PENDING_KEY, "source_validation_pending"]
)
async def test_healthy_parent_cannot_clear_other_parent_pending(runtime, pending_key):
    for row_id, metadata in (
        ("uncertain-parent", {pending_key: True}),
        ("healthy-parent", {pending_key: False}),
        ("shared-derived", {}),
    ):
        await runtime.entity_manager.create_direct(
            Entity(id=row_id, name=row_id, entity_type=EntityType.EPISODE, metadata=metadata),
            generate_embedding=False,
        )
    await reconcile.reconcile_with_parent(
        runtime.entity_manager, source_id="uncertain-parent", row_ids=["shared-derived"]
    )
    before = await runtime.entity_manager.get("shared-derived")
    assert not graph_metadata_recallable(before.metadata)
    await reconcile.reconcile_with_parent(
        runtime.entity_manager, source_id="healthy-parent", row_ids=["shared-derived"]
    )
    after = await runtime.entity_manager.get("shared-derived")
    assert not graph_metadata_recallable(after.metadata)


@pytest.mark.parametrize(
    "pending_key", [reconcile.RECONCILE_PENDING_KEY, "source_validation_pending"]
)
async def test_each_parent_recovers_only_its_own_pending_state(runtime, pending_key):
    for row_id in ("parent-a", "parent-b", "shared"):
        await runtime.entity_manager.create_direct(
            Entity(
                id=row_id,
                name=row_id,
                entity_type=EntityType.EPISODE,
                metadata={pending_key: True} if row_id != "shared" else {},
            ),
            generate_embedding=False,
        )
    for source_id in ("parent-a", "parent-b"):
        await reconcile.reconcile_with_parent(
            runtime.entity_manager, source_id=source_id, row_ids=["shared"]
        )
    for index, source_id in enumerate(("parent-a", "parent-b")):
        parent = await runtime.entity_manager.get(source_id)
        await runtime.entity_manager.update(
            source_id,
            {"metadata": {pending_key: None}},
            expected_revision=parent.observed_revision,
        )
        await reconcile.reconcile_with_parent(
            runtime.entity_manager, source_id=source_id, row_ids=["shared"]
        )
        child = await runtime.entity_manager.get("shared")
        assert graph_metadata_recallable(child.metadata) is (index == 1)
        if index == 0:
            assert child.metadata[pending_key] == {"parent:parent-b": True}
        else:
            assert pending_key not in child.metadata


async def test_unreadable_parent_is_recorded_when_another_parent_already_blocks(
    runtime, monkeypatch
):
    from unittest.mock import AsyncMock

    for row_id, metadata in (
        ("known-parent", {reconcile.RECONCILE_PENDING_KEY: True}),
        ("shared", {}),
    ):
        await runtime.entity_manager.create_direct(
            Entity(id=row_id, name=row_id, entity_type=EntityType.EPISODE, metadata=metadata),
            generate_embedding=False,
        )
    await reconcile.reconcile_with_parent(
        runtime.entity_manager, source_id="known-parent", row_ids=["shared"]
    )
    get = runtime.entity_manager.get

    async def unavailable_parent(entity_id):
        if entity_id == "unavailable-parent":
            raise ConnectionError("parent unavailable")
        return await get(entity_id)

    monkeypatch.setattr(runtime.entity_manager, "get", unavailable_parent)
    monkeypatch.setattr(reconcile.asyncio, "sleep", AsyncMock())
    await reconcile.reconcile_with_parent(
        runtime.entity_manager, source_id="unavailable-parent", row_ids=["shared"]
    )
    parent = await get("known-parent")
    await runtime.entity_manager.update(
        parent.id,
        {"metadata": {reconcile.RECONCILE_PENDING_KEY: None}},
        expected_revision=parent.observed_revision,
    )
    await reconcile.reconcile_with_parent(
        runtime.entity_manager, source_id=parent.id, row_ids=["shared"]
    )
    child = await get("shared")
    assert not graph_metadata_recallable(child.metadata)
    assert child.metadata[reconcile.RECONCILE_PENDING_KEY] == {"parent:unavailable-parent": True}


def test_public_pending_metadata_does_not_disclose_source_owners():
    from sibyl_core.memory_pipeline.source_lifecycle import public_memory_metadata

    metadata = {
        reconcile.RECONCILE_PENDING_KEY: {"parent:private-parent": True},
        "source_validation_pending": {"capture:private-capture": True},
        "authored": {"parent": "preserve"},
    }
    public = public_memory_metadata(metadata)
    assert public == {
        reconcile.RECONCILE_PENDING_KEY: True,
        "source_validation_pending": True,
        "authored": {"parent": "preserve"},
    }
    assert isinstance(metadata[reconcile.RECONCILE_PENDING_KEY], dict)


@pytest.mark.parametrize("reverse", [False, True])
def test_shared_candidate_keeps_all_parent_pending_state_before_insert(reverse):
    from datetime import UTC, datetime

    from sibyl_core.projection.memory import ProjectedMemoryEntity, _add_projection_candidates

    sources = [
        Entity(
            id=source_id,
            name=source_id,
            entity_type=EntityType.SESSION,
            metadata={"memory_scope": "org", reconcile.RECONCILE_PENDING_KEY: True},
        )
        for source_id in ("parent-a", "parent-b")
    ]
    if reverse:
        sources.reverse()
    projected = {}
    for source in sources:
        _add_projection_candidates(
            group_id="org",
            now=datetime.now(UTC),
            source=source,
            source_id=source.id,
            candidates=[ProjectedMemoryEntity(name="Shared topic", context="Shared evidence")],
            projected_by_id=projected,
            relationships=[],
        )
    assert len(projected) == 1
    entity = next(iter(projected.values()))
    assert entity.metadata[reconcile.RECONCILE_PENDING_KEY] == {
        "parent:parent-a": True,
        "parent:parent-b": True,
    }


async def test_unreadable_capture_is_pending_before_insert(monkeypatch):
    from unittest.mock import AsyncMock

    monkeypatch.setattr(reconcile.asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(
        "sibyl_core.services.memory.projected_row_lifecycle_stamp",
        AsyncMock(side_effect=ConnectionError("capture unavailable")),
    )
    stamp, verified = await reconcile.prewrite_capture_stamp(
        organization_id="org", metadata={"raw_memory_id": "capture-a"}
    )
    assert not verified
    assert not graph_metadata_recallable(stamp)
    assert stamp[reconcile.RECONCILE_PENDING_KEY] == {"capture:capture-a": True}


@pytest.mark.parametrize(
    "pending_key", [reconcile.RECONCILE_PENDING_KEY, "source_validation_pending"]
)
async def test_late_duplicate_insert_preserves_another_batches_pending_owner(runtime, pending_key):
    # Both batches decided this deterministic candidate ID was absent before
    # either inserted it. The second insert lands after A has reconciled.
    first = Entity(
        id="shared-batch-row",
        name="Shared candidate",
        entity_type=EntityType.TOPIC,
        metadata={pending_key: {"parent:a": True}},
    )
    second = first.model_copy(update={"metadata": {pending_key: {"parent:b": True}}})
    await runtime.entity_manager.create_direct(first, generate_embedding=False)
    await runtime.entity_manager.create_direct(second, generate_embedding=False)
    stored = await runtime.entity_manager.get(first.id)
    assert stored.metadata[pending_key] == {"parent:a": True, "parent:b": True}
    await runtime.entity_manager.create_direct(
        Entity(id="b", name="Healthy B", entity_type=EntityType.EPISODE),
        generate_embedding=False,
    )
    await reconcile.reconcile_with_parent(runtime.entity_manager, source_id="b", row_ids=[first.id])
    stored = await runtime.entity_manager.get(first.id)
    assert stored.metadata[pending_key] == {"parent:a": True}
    assert not graph_metadata_recallable(stored.metadata)


def test_raw_capture_drops_caller_supplied_reconciliation_owners():
    from datetime import UTC, datetime

    from sibyl_core.services.content_models import RawMemoryWrite
    from sibyl_core.services.content_raw_persistence import _raw_memory_from_write

    memory = _raw_memory_from_write(
        RawMemoryWrite(
            organization_id="org",
            principal_id="owner",
            source_id="source",
            raw_content="Body",
            metadata={reconcile.RECONCILE_PENDING_KEY: {"capture:other": True}, "note": "keep"},
        ),
        captured_at=datetime.now(UTC),
    )
    assert reconcile.RECONCILE_PENDING_KEY not in memory.metadata
    assert memory.metadata["note"] == "keep"


@pytest.mark.parametrize("missing_result", ["none", "key_error"])
async def test_missing_parent_stays_pending_until_present(runtime, monkeypatch, missing_result):
    await runtime.entity_manager.create_direct(
        Entity(id="child", name="child", entity_type=EntityType.EPISODE, metadata={}),
        generate_embedding=False,
    )
    original_get = runtime.entity_manager.get
    parent_available = False

    async def read(entity_id):
        if entity_id == "parent" and not parent_available:
            if missing_result == "key_error":
                raise KeyError(entity_id)
            return None
        return await original_get(entity_id)

    monkeypatch.setattr(runtime.entity_manager, "get", read)
    outcome = await reconcile.reconcile_with_parent(
        runtime.entity_manager, source_id="parent", row_ids=["child"]
    )
    assert outcome.unverified == 1
    assert outcome.cleared == 0
    repeated = await reconcile.reconcile_with_parent(
        runtime.entity_manager, source_id="parent", row_ids=["child"]
    )
    assert repeated.cleared == 0
    child = await original_get("child")
    assert not graph_metadata_recallable(child.metadata)
    assert child.metadata[reconcile.RECONCILE_PENDING_KEY] == {"parent:parent": True}
    assert "excluded_from_recall" not in child.metadata

    await runtime.entity_manager.create_direct(
        Entity(id="parent", name="parent", entity_type=EntityType.EPISODE, metadata={}),
        generate_embedding=False,
    )
    parent_available = True
    outcome = await reconcile.reconcile_with_parent(
        runtime.entity_manager, source_id="parent", row_ids=["child"]
    )
    assert outcome.cleared == 1
    assert outcome.unverified == 0
    child = await original_get("child")
    assert graph_metadata_recallable(child.metadata)
    assert reconcile.RECONCILE_PENDING_KEY not in child.metadata
