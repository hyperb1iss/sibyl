"""Capture projection recovery through isolated content and graph stores."""

import pytest

from sibyl_core.memory_pipeline.lifecycle import graph_metadata_recallable
from sibyl_core.models.entities import Entity, EntityType
from sibyl_core.projection import reconcile
from sibyl_core.services.content_models import RawMemory
from sibyl_core.services.content_raw_persistence import save_raw_memory
from sibyl_core.services.memory_lifecycle import projected_row_lifecycle_stamp
from tests.test_reflection_identity import content_store as content_store
from tests.test_reflection_identity import runtime as runtime


@pytest.mark.parametrize("locator", ["raw_memory_id", "raw_source_id"])
async def test_absent_capture_remains_pending_until_stored(runtime, content_store, locator):
    metadata = {locator: "late-capture"}
    stamp, _ = await reconcile.prewrite_capture_stamp(
        organization_id=runtime.client.group_id, metadata=metadata
    )
    assert not graph_metadata_recallable(stamp)
    await runtime.entity_manager.create_direct(
        Entity(
            id="capture-child",
            name="Capture child",
            entity_type=EntityType.EPISODE,
            metadata={**metadata, **stamp},
        ),
        generate_embedding=False,
    )
    await reconcile.reconcile_with_capture(
        runtime.entity_manager,
        organization_id=runtime.client.group_id,
        metadata=metadata,
        row_ids=["capture-child"],
    )
    child = await runtime.entity_manager.get("capture-child")
    assert not graph_metadata_recallable(child.metadata)
    assert child.metadata[reconcile.RECONCILE_PENDING_KEY]
    assert "excluded_from_recall" not in child.metadata

    await save_raw_memory(
        RawMemory(
            id="late-capture",
            organization_id=runtime.client.group_id,
            source_id="late-capture",
            principal_id="user_a",
            raw_content="Recovered source",
        ),
        embedding_provider=None,
    )
    outcome = await reconcile.reconcile_with_capture(
        runtime.entity_manager,
        organization_id=runtime.client.group_id,
        metadata=metadata,
        row_ids=["capture-child"],
    )
    child = await runtime.entity_manager.get("capture-child")
    assert graph_metadata_recallable(child.metadata)
    assert reconcile.RECONCILE_PENDING_KEY not in child.metadata
    assert outcome.cleared == 1


async def test_unbound_projection_has_no_capture_check(content_store):
    assert await projected_row_lifecycle_stamp(organization_id="org", metadata={}) == {}


async def test_alias_capture_recovery_preserves_other_capture_owner(runtime, content_store):
    await runtime.entity_manager.create_direct(
        Entity(id="shared-child", name="Shared child", entity_type=EntityType.EPISODE),
        generate_embedding=False,
    )
    for source_id in ("source-a", "source-b"):
        await reconcile.reconcile_with_capture(
            runtime.entity_manager,
            organization_id=runtime.client.group_id,
            metadata={"raw_source_id": source_id},
            row_ids=["shared-child"],
        )
    child = await runtime.entity_manager.get("shared-child")
    assert child.metadata[reconcile.RECONCILE_PENDING_KEY] == {
        "capture-source:source-a": True,
        "capture-source:source-b": True,
    }
    for index, source_id in enumerate(("source-a", "source-b")):
        await save_raw_memory(
            RawMemory(
                id=f"capture-{source_id}",
                organization_id=runtime.client.group_id,
                source_id=source_id,
                principal_id="user_a",
                raw_content="Recovered evidence",
            ),
            embedding_provider=None,
        )
        await reconcile.reconcile_with_capture(
            runtime.entity_manager,
            organization_id=runtime.client.group_id,
            metadata={"raw_source_id": source_id},
            row_ids=["shared-child"],
        )
        child = await runtime.entity_manager.get("shared-child")
        assert graph_metadata_recallable(child.metadata) is (index == 1)
        if index == 0:
            assert child.metadata[reconcile.RECONCILE_PENDING_KEY] == {
                "capture-source:source-b": True
            }
        else:
            assert reconcile.RECONCILE_PENDING_KEY not in child.metadata
