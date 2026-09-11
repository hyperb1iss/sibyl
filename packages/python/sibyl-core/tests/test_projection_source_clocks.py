"""Post-write reconciliation preserves source ordering and authored verdicts."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from sibyl_core.memory_pipeline.lifecycle import graph_lifecycle_stamp, graph_metadata_recallable
from sibyl_core.models.entities import Entity, EntityType
from sibyl_core.projection import reconcile
from sibyl_core.services import memory_lifecycle
from sibyl_core.services.memory_correction import apply_memory_correction
from sibyl_core.services.surreal_content import remember_raw_memory
from tests.test_reflection_identity import content_store as content_store
from tests.test_reflection_identity import runtime as runtime


@pytest.mark.parametrize(
    "own_verdict", [{}, {"excluded_from_recall": True}, {"lifecycle_flags": ["hidden"]}]
)
async def test_postwrite_failure_recovers_without_erasing_own_verdict(
    runtime, content_store, monkeypatch, own_verdict
):
    monkeypatch.setattr(
        memory_lifecycle, "get_surreal_graph_runtime", AsyncMock(return_value=runtime)
    )
    monkeypatch.setattr(reconcile.asyncio, "sleep", AsyncMock())
    raw = await remember_raw_memory(
        organization_id=runtime.client.group_id,
        principal_id="owner",
        source_id="race-source",
        raw_content="Old instructions",
        embedding_provider=None,
    )
    stamp = graph_lifecycle_stamp(raw)
    await apply_memory_correction(
        organization_id=runtime.client.group_id,
        source_id=raw.id,
        principal_id="owner",
        action="hide",
    )
    await runtime.entity_manager.create_direct(
        Entity(
            id="raced-copy",
            name="Raced copy",
            entity_type=EntityType.EPISODE,
            content=raw.raw_content,
            metadata={
                "memory_scope": "private",
                "principal_id": "owner",
                "raw_memory_id": raw.id,
                **stamp,
                **own_verdict,
            },
        ),
        generate_embedding=False,
    )
    read = memory_lifecycle.get_raw_memory
    monkeypatch.setattr(
        memory_lifecycle, "get_raw_memory", AsyncMock(side_effect=ConnectionError("unavailable"))
    )
    failed = await reconcile.reconcile_with_capture(
        runtime.entity_manager,
        organization_id=runtime.client.group_id,
        metadata={"raw_memory_id": raw.id},
        row_ids=["raced-copy"],
    )
    stored = await runtime.entity_manager.get("raced-copy")
    assert not graph_metadata_recallable(stored.metadata)
    if not own_verdict:
        assert failed.unverified == 1
        assert stored.metadata[reconcile.RECONCILE_PENDING_KEY] == {f"capture:{raw.id}": True}
        assert "excluded_from_recall" not in stored.metadata
    monkeypatch.setattr(memory_lifecycle, "get_raw_memory", read)
    await reconcile.reconcile_with_capture(
        runtime.entity_manager,
        organization_id=runtime.client.group_id,
        metadata={"raw_memory_id": raw.id},
        row_ids=["raced-copy"],
    )
    stored = await runtime.entity_manager.get("raced-copy")
    assert reconcile.RECONCILE_PENDING_KEY not in stored.metadata
    assert stored.metadata["correction_blockers"][raw.id]["blocking"] is True
    await apply_memory_correction(
        organization_id=runtime.client.group_id,
        source_id=raw.id,
        principal_id="owner",
        action="restore",
    )
    stored = await runtime.entity_manager.get("raced-copy")
    assert graph_metadata_recallable(stored.metadata) is (not own_verdict)
    for key, value in own_verdict.items():
        assert stored.metadata[key] == value


async def test_fallback_exclusion_does_not_trust_a_clear_clock(runtime):
    await runtime.entity_manager.create_direct(
        Entity(
            id="fallback-copy",
            name="Fallback",
            entity_type=EntityType.EPISODE,
            metadata={"correction_blockers": {"root": {"revision": 1, "blocking": False}}},
        ),
        generate_embedding=False,
    )
    result = await reconcile._force_exclusion(
        runtime.entity_manager, "fallback-copy", "capture", runtime.client.group_id, "capture:root"
    )
    stored = await runtime.entity_manager.get("fallback-copy")
    assert result == "unverified"
    assert not graph_metadata_recallable(stored.metadata)
    assert stored.metadata[reconcile.RECONCILE_PENDING_KEY] == {"capture:root": True}
    assert "excluded_from_recall" not in stored.metadata


@pytest.mark.parametrize("newer_blocking", [False, True])
async def test_reconcile_merges_root_clocks_without_rebinding_text(
    runtime, monkeypatch, newer_blocking
):
    current = {
        "correction_blockers": {
            "root": {"revision": 3, "blocking": newer_blocking},
            "other-root": {"revision": 4, "blocking": True},
        },
        "source_bindings": {"root": 1, "other-root": 2},
        "authored": "Keep this",
    }
    await runtime.entity_manager.create_direct(
        Entity(
            id="ordered-copy",
            name="Ordered",
            entity_type=EntityType.EPISODE,
            metadata=deepcopy(current),
        ),
        generate_embedding=False,
    )
    monkeypatch.setattr(
        "sibyl_core.services.memory.projected_row_lifecycle_stamp",
        AsyncMock(
            return_value={
                "correction_blockers": {
                    "root": {"revision": 2, "blocking": not newer_blocking},
                    "new-root": {"revision": 1, "blocking": True},
                },
                "source_bindings": {"root": 7, "new-root": 5},
            }
        ),
    )
    await reconcile.reconcile_with_capture(
        runtime.entity_manager,
        organization_id=runtime.client.group_id,
        metadata={"raw_memory_id": "root"},
        row_ids=["ordered-copy"],
    )
    stored = await runtime.entity_manager.get("ordered-copy")
    assert stored.metadata["correction_blockers"] == {
        **current["correction_blockers"],
        "new-root": {"revision": 1, "blocking": True},
    }
    assert stored.metadata["source_bindings"] == current["source_bindings"]
    assert stored.metadata["authored"] == "Keep this"


async def test_reconcile_never_fences_on_a_normalized_default_revision(monkeypatch):
    row = Entity(id="unversioned", name="Unversioned", entity_type=EntityType.EPISODE)
    assert row.revision == 1 and row.observed_revision is None
    manager = SimpleNamespace(get=AsyncMock(return_value=row), update=AsyncMock())
    monkeypatch.setattr(reconcile.asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(
        "sibyl_core.services.memory.projected_row_lifecycle_stamp",
        AsyncMock(
            return_value={"correction_blockers": {"root": {"revision": 2, "blocking": True}}}
        ),
    )
    with pytest.raises(reconcile.ReconcileExclusionError):
        await reconcile.reconcile_with_capture(
            manager, organization_id="org", metadata={"raw_memory_id": "root"}, row_ids=[row.id]
        )
    manager.update.assert_not_awaited()


@pytest.mark.parametrize(
    "own_verdict", [{"excluded_from_recall": True}, {"lifecycle_flags": ["hidden"]}]
)
def test_readable_empty_or_flat_verdict_cannot_remove_own_exclusion(own_verdict):
    assert reconcile._desired_patch({}, own_verdict) == {}
    clearing = {"excluded_from_recall": False, "lifecycle_flags": []}
    merged = {**own_verdict, **reconcile._desired_patch(clearing, own_verdict)}
    assert not graph_metadata_recallable(merged)


def test_source_validation_recovery_is_not_an_authored_exclusion():
    patch = reconcile._desired_patch(
        {"source_validation_pending": False}, {"source_validation_pending": True}
    )
    assert patch == {"source_validation_pending": False}
    assert graph_metadata_recallable(patch)


async def test_parent_pending_verdict_keeps_child_excluded_until_parent_recovers(runtime):
    for row_id, metadata in (
        ("pending-parent", {reconcile.RECONCILE_PENDING_KEY: True}),
        ("pending-child", {}),
    ):
        await runtime.entity_manager.create_direct(
            Entity(id=row_id, name=row_id, entity_type=EntityType.EPISODE, metadata=metadata),
            generate_embedding=False,
        )
    for _ in range(2):
        await reconcile.reconcile_with_parent(
            runtime.entity_manager, source_id="pending-parent", row_ids=["pending-child"]
        )
        child = await runtime.entity_manager.get("pending-child")
        assert not graph_metadata_recallable(child.metadata)
        assert child.metadata[reconcile.RECONCILE_PENDING_KEY] == {"parent:pending-parent": True}
    parent = await runtime.entity_manager.get("pending-parent")
    await runtime.entity_manager.update(
        parent.id,
        {"metadata": {reconcile.RECONCILE_PENDING_KEY: None}},
        expected_revision=parent.observed_revision,
    )
    await reconcile.reconcile_with_parent(
        runtime.entity_manager, source_id="pending-parent", row_ids=["pending-child"]
    )
    child = await runtime.entity_manager.get("pending-child")
    assert graph_metadata_recallable(child.metadata)
    assert reconcile.RECONCILE_PENDING_KEY not in child.metadata
